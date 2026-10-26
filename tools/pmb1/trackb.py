"""PMB-1 Track B: the capability-regression instrument (T-2518; T-2425 report section 6.3, ratified via ORG-062 section 1).

Does injecting memory into a turn make the model worse at following instructions? IFEval (google/IFEval, 541
prompts) is run on the SAME model under paired arms and scored with the vendored checker (``tools/pmb1/ifeval``):

* ``off``    the bare prompt;
* ``on``     ``<block>\\n\\n<prompt>`` as ONE user message, the block being the candidate substrate's injection;
* ``random`` the same rendering with K random docs from a pool (``build_random``): the "any context at all" control.

Blocks come from ``--inject`` JSONL files ``{"key", "arm", "block"}`` (producing real blocks is out of scope). A
declared arm with a missing block is fatal.

**Verdict (pre-registered, T-2518 Track B brief L4):** FAIL iff the POINT ESTIMATE of (on - off) prompt-level
strict accuracy is below ``-REGRESSION_LIMIT_PP`` (-5.0) percentage points; exactly -5.0 passes ("more than 5").
The paired bootstrap CI (``stats.paired_interval``) is reported, never gated. Pairing is INTENT-TO-TREAT on the
treatment arms (coordinator ruling, D6 rebound): a prompt is dropped only when the OFF arm errored on it (no baseline;
counted); an ``on`` / ``random`` error on a baselined prompt is scored as a FAIL for that arm. The old matched-only
delta is printed as an informational line.

**Thinking:** ``--think off`` (the default) sends Ollama's top-level ``"think": false`` so a thinking model (Qwen3.5)
does not spend the 1280-token budget on reasoning; ``model-default`` sends no ``think`` field. NOT verified against
an Ollama API doc or capture (none is on this host; D11): confirm on the first real call. A reply's separate
``message.thinking`` is never the response and never scored; the journal records only whether it was present.

**Generation** (``generate``): temperature 0, ``NUM_PREDICT`` new tokens (IFEval's lm_eval default
``max_gen_toks: 1280``; ``backbone.NUM_PREDICT`` = 128 is the short-answer tracks' cap and would truncate most
IFEval replies, so this module passes its own cap to ``OllamaBackbone(num_predict=...)``, whose guards reserve it).
Each (model, arm, key) is one independent call, journalled to ``<out>/journal.jsonl`` and never redone, so a killed
run resumes. (realrun's replay journal exists for multi-call units that mutate a substrate; one call per item needs
none of it, so this module reuses realrun's window, budget, lock and unload primitives but not its replay.)
Deterministic failures (prompt or output truncated, a malformed reply) are journalled as errors: never redone,
never scored. Transient ones (timeout, connection, HTTP) stop the run unjournalled: rerun to resume.
Lock-aware: refuses to start a session inside 04:55-05:50Z, runs every call under ``lock_factory()`` (the shared
GPU lock) with the HTTP timeout clamped to the session budget, and unloads the model on every release.

**Output** is counts and rates only (L5). Generations and blocks live in the journal and the inject files.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import logging
import math
import os
import random
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ContextManager, Iterable, Protocol, Sequence

from .backbone import CHARS_PER_TOKEN_CONSERVATIVE, OllamaBackbone, TruncatedPrompt
from .realrun import (
    COLD_S, GAP_S, MAX_LOCK_S, MIN_CALL_TIMEOUT_S, Clock, InWindow, UnloadFailed, _before_session, _is_timeout,
    _session_budget, in_window, window_end,
)
from .stats import CI_LEVEL, paired_interval

FORMAT = "pmb1-trackb/1"
NUM_PREDICT = 1280             # lm_eval 0.4.8 tasks/ifeval/ifeval.yaml: generation_kwargs.max_gen_toks: 1280
NUM_CTX_HEADROOM = 512
REGRESSION_LIMIT_PP = 5.0       # T-2425 section 6.3: a regression of MORE THAN 5 points disqualifies
# Pre-registered by the coordinator (review F4): off-arm errors on MORE than this share of the prompt file (27 of 541
# pass, 28 do not) make the baseline unreliable: NO_VERDICT.
MAX_OFF_ERROR_SHARE = 0.05
ARMS = ("off", "on", "random")
THINK_MODES = ("off", "model-default")
EST_GEN_S = 90.0                # a 1280-token reply's planning estimate until a call has been measured
CALL_TIMEOUT_S = 300.0
KEEP_ALIVE = "2m"
DEFAULT_PROMPTS = Path(__file__).resolve().parent / "ifeval" / "ifeval_input_data.jsonl"
RENDER = "<block>\\n\\n<prompt>"   # recorded in the manifest; ``render`` is the implementation
DOCS_PLACEHOLDER = "{docs}"
PS_TIMEOUT_S = 10.0
LANGDETECT_SEED = 0             # langdetect is nondeterministic unless seeded; the checker's language rules use it


class TrackBError(ValueError):
    """Bad inputs or a refused run (missing block, manifest mismatch, no matched prompts...)."""


class ManifestMismatch(TrackBError):
    """A resume under a different model, digest, options, prompt file, inject files, arms or limit."""


# ------------------------------------------------------------------------------------------------------ inputs

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _jsonl(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if line.strip():
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise TrackBError(f"{path}:{i}: not a JSON object")
                rows.append(obj)
    return rows


def load_prompts(path: Path, limit: int | None = None) -> list[dict]:
    """IFEval records ``{key, prompt, instruction_id_list, kwargs}``, file order; the first ``limit`` if given."""
    rows = _jsonl(path)
    for r in rows:
        if not {"key", "prompt", "instruction_id_list", "kwargs"} <= r.keys():
            raise TrackBError(f"{path}: an IFEval record lacks key/prompt/instruction_id_list/kwargs")
    keys = [r["key"] for r in rows]
    if len(set(keys)) != len(keys):
        raise TrackBError(f"{path}: duplicate keys")
    return rows[:limit] if limit else rows


def load_inject(paths: Sequence[Path]) -> tuple[dict[tuple[str, int], str], dict | None]:
    """``{(arm, key): block}`` over every file, plus the first file's metadata row (``{"meta": {...}}``) if any.
    A duplicate (arm, key), an ``off`` block, an unknown arm or a non-string block is fatal."""
    blocks: dict[tuple[str, int], str] = {}
    meta = None
    for p in paths:
        for r in _jsonl(p):
            if "meta" in r and "block" not in r:
                meta = meta if meta is not None else r["meta"]
                continue
            arm, key, block = r.get("arm"), r.get("key"), r.get("block")
            if arm not in ARMS or arm == "off":
                raise TrackBError(f"{p}: inject row with arm {arm!r} (blocks are for {ARMS[1:]})")
            if not isinstance(key, int) or isinstance(key, bool) or not isinstance(block, str):
                raise TrackBError(f"{p}: inject rows are {{key: int, arm, block: str}}")
            if (arm, key) in blocks:
                raise TrackBError(f"{p}: duplicate block for arm {arm} key {key}")
            blocks[(arm, key)] = block
    return blocks, meta


def check_blocks(blocks: dict[tuple[str, int], str], arms: Sequence[str], prompts: Sequence[dict],
                 all_keys: Iterable[int]) -> None:
    """Every declared non-off arm has a block for every prompt (fatal otherwise); no block names an unknown key."""
    known = set(all_keys)
    unknown = sorted({k for _, k in blocks if k not in known})
    if unknown:
        raise TrackBError(f"{len(unknown)} inject rows name keys not in the prompt file (first: {unknown[:3]})")
    for arm in arms:
        if arm == "off":
            continue
        missing = [p["key"] for p in prompts if (arm, p["key"]) not in blocks]
        if missing:
            raise TrackBError(f"arm {arm!r} is missing blocks for {len(missing)} of {len(prompts)} prompts "
                              f"(first keys: {missing[:5]})")


def render(arm: str, prompt: str, block: str | None) -> str:
    """The ONE user message the model sees: the bare prompt for ``off``, else ``<block>\\n\\n<prompt>``."""
    if arm == "off":
        return prompt
    if block is None:
        raise TrackBError(f"arm {arm!r} needs a block")
    return f"{block}\n\n{prompt}"


# ------------------------------------------------------------------------------------------- random-arm builder

def render_docs(template: str, docs: Sequence[str], doc_sep: str) -> str:
    if DOCS_PLACEHOLDER not in template:
        raise TrackBError(f"the block template must contain {DOCS_PLACEHOLDER}")
    return template.replace(DOCS_PLACEHOLDER, doc_sep.join(docs))   # str.replace: docs may hold braces


def build_random(prompt_keys: Sequence[int], pool: Sequence[str], k: int, seed: int, template: str,
                 doc_sep: str) -> list[dict]:
    """The ``random`` arm's rows: for each key, ``k`` distinct pool docs drawn by an RNG seeded from (seed, key)
    alone (so a key's block does not depend on the others), rendered with the ``on`` arm's template."""
    if k < 1 or len(pool) < k:
        raise TrackBError(f"need 1 <= k <= pool size (k={k}, pool={len(pool)})")
    rows = []
    for key in prompt_keys:
        rng = random.Random(f"pmb1-trackb-random/{seed}/{key}")
        docs = [pool[i] for i in rng.sample(range(len(pool)), k)]
        rows.append({"key": key, "arm": "random", "block": render_docs(template, docs, doc_sep)})
    return rows


def resolve_template(on_meta: dict | None, template: str | None, doc_sep: str | None) -> tuple[str, str]:
    """The ``on`` file's metadata row is the source of truth (the control must be rendered as the treatment was);
    a flag is the fallback when it has none, and a flag that CONTRADICTS the metadata is refused."""
    m_t = (on_meta or {}).get("template")
    m_s = (on_meta or {}).get("doc_sep")
    if m_t is not None and template is not None and template != m_t:
        raise TrackBError("--template contradicts the on-file's metadata template")
    if m_s is not None and doc_sep is not None and doc_sep != m_s:
        raise TrackBError("--doc-sep contradicts the on-file's metadata doc_sep")
    t = m_t if m_t is not None else template
    if t is None:
        raise TrackBError("no block template: the on-file has no {\"meta\": {\"template\"}} row and no --template given")
    s = m_s if m_s is not None else (doc_sep if doc_sep is not None else "\n\n")
    return t, s


def load_pool(path: Path) -> list[str]:
    docs = []
    for r in _jsonl(path):
        if not isinstance(r.get("text"), str):
            raise TrackBError(f"{path}: pool rows are {{\"text\": str, ...}}")
        docs.append(r["text"])
    return docs


# -------------------------------------------------------------------------------------------------- generation

class Generator(Protocol):
    name: str
    timeout: float
    timeout_cap: float | None

    def ask(self, user: str) -> dict: ...

    def loaded_digest(self) -> str: ...

    def unload(self) -> None: ...


class IFEvalOllama(OllamaBackbone):
    """``OllamaBackbone`` with Track B's token cap and no system message. ``ask`` returns the RAW /api/chat reply
    (content unstripped: IFEval checks bytes such as a leading quote) with ``_error`` set for a deterministic
    failure; transient errors (timeout, connection, HTTP) raise. The one HTTP exception (T-2518): Ollama's
    deterministic HTTP 500 ``{"error": "... token repeat limit reached"}`` (the model degenerated into a loop; the
    backbone's one 5xx retry hits it again) is returned as ``_error = "repeat_aborted"``, a per-item failure."""

    def __init__(self, model: str, *, num_ctx: int, keep_alive: str | int = KEEP_ALIVE,
                 timeout: float = CALL_TIMEOUT_S, base_url: str | None = None, think: str = "off") -> None:
        super().__init__(model, "trackb", base_url=base_url, keep_alive=keep_alive, timeout=timeout,
                         num_ctx=num_ctx, num_predict=NUM_PREDICT)
        if think not in THINK_MODES:
            raise ValueError(f"think must be one of {THINK_MODES}")
        self.request_extra = think_body(think)
        self._last: dict | None = None

    def _post_chat(self, req: urllib.request.Request, timeout: float) -> dict:
        data = super()._post_chat(req, timeout)
        self._last = data
        return data

    def ask(self, user: str) -> dict:
        self._last = None
        try:
            self._chat(None, user)
        except TruncatedPrompt as e:   # the pre-check (no request sent) or the post-check (the reply was read)
            return {"_error": "prompt_truncated", "_detail": str(e), "_reached": self._last is not None,
                    "prompt_eval_count": self.last_prompt_eval_count}
        except (KeyError, TypeError, AttributeError):
            return {"_error": "malformed", "_reached": True}
        except urllib.error.HTTPError as e:
            detail = _repeat_abort_detail(e)
            if detail is None:
                raise
            return {"_error": "repeat_aborted", "_detail": detail[:200], "_reached": True}
        return dict(self._last or {})

    def loaded_digest(self) -> str:
        """GET /api/ps: the full digest of this model's loaded weights (the shape ``OllamaEmbedder.verify_digest``
        was captured against). Raises if the model is not listed."""
        with urllib.request.urlopen(self.base_url + "/api/ps", timeout=PS_TIMEOUT_S) as resp:  # noqa: S310
            data = json.loads(resp.read())
        want = {self.model} | ({self.model + ":latest"} if ":" not in self.model else set())
        hits = [m for m in data.get("models") or [] if isinstance(m, dict)
                and (m.get("model") in want or m.get("name") in want) and isinstance(m.get("digest"), str)]
        if not hits:
            raise TrackBError(f"/api/ps lists no loaded {self.model!r}: cannot record its digest")
        return str(hits[0]["digest"])


REPEAT_LIMIT_MARKER = "token repeat limit reached"


def _repeat_abort_detail(e: urllib.error.HTTPError) -> str | None:
    """The ``error`` string of an HTTP 500 whose body is Ollama's repeat-limit abort, else None (any other code,
    an unreadable or non-JSON body, or another error message). Never raises."""
    if e.code != 500:
        return None
    try:
        data = json.loads(e.read())
    except Exception:   # noqa: BLE001 (no fp, a read failure or a non-JSON body: not the abort)
        return None
    err = data.get("error") if isinstance(data, dict) else None
    return err if isinstance(err, str) and REPEAT_LIMIT_MARKER in err else None


def think_body(think: str) -> dict:
    """The top-level /api/chat fields for a ``--think`` mode: ``off`` -> ``{"think": False}``; ``model-default`` ->
    nothing (the model's own default)."""
    return {"think": False} if think == "off" else {}


def make_generator(model: str, num_ctx: int, keep_alive: str | int, timeout: float, think: str = "off") -> Generator:
    """The real generator (a module seam: tests replace it with a fake model)."""
    return IFEvalOllama(model, num_ctx=num_ctx, keep_alive=keep_alive, timeout=timeout, think=think)


def classify(raw: dict, num_predict: int = NUM_PREDICT) -> tuple[str | None, str | None]:
    """(response, None) for a usable reply, else (None, error code). Fail closed: a reply that was or may have
    been cut (done_reason other than "stop", or eval_count at the cap) is never a response."""
    if raw.get("_error"):
        return None, str(raw["_error"])
    msg = raw.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, str):
        return None, "malformed"
    if raw.get("done_reason") != "stop":
        return None, "output_truncated" if raw.get("done_reason") == "length" else "bad_done_reason"
    ec = raw.get("eval_count")
    if isinstance(ec, int) and not isinstance(ec, bool) and ec >= num_predict:
        return None, "output_truncated"
    return content, None


@dataclass
class GenConfig:
    model: str
    out: Path
    arms: Sequence[str] = ARMS
    prompts: Path = DEFAULT_PROMPTS
    inject: Sequence[Path] = ()
    limit: int | None = None
    max_lock_s: float = MAX_LOCK_S
    gap_s: float = GAP_S
    call_timeout: float = CALL_TIMEOUT_S
    keep_alive: str = KEEP_ALIVE
    est_gen_s: float = EST_GEN_S
    think: str = "off"


@dataclass
class Plan:
    prompts: list[dict]
    blocks: dict[tuple[str, int], str]
    num_ctx: int
    manifest: dict
    items: list[tuple[str, int]] = field(default_factory=list)   # (arm, key), key-major so arms stay matched


def required_num_ctx(messages: Iterable[str]) -> int:
    """The longest message at the guard's conservative chars per token, plus NUM_PREDICT and headroom, rounded up
    to a multiple of 1024 (floor 4096). One value for the whole run, so every arm runs under the same options."""
    longest = max((len(m) for m in messages), default=0)
    est = math.ceil(longest / CHARS_PER_TOKEN_CONSERVATIVE) + NUM_PREDICT + NUM_CTX_HEADROOM
    return max(4096, math.ceil(est / 1024) * 1024)


def _identity(m: dict) -> dict:
    return {k: m.get(k) for k in ("format", "model", "options", "think", "prompts_sha256", "n_prompts", "limit", "arms",
                              "inject", "render")}


def plan(cfg: GenConfig) -> Plan:
    arms = list(cfg.arms)
    if cfg.think not in THINK_MODES:
        raise TrackBError(f"--think must be one of {THINK_MODES} (got {cfg.think!r})")
    if "off" not in arms or len(set(arms)) != len(arms) or not set(arms) <= set(ARMS):
        raise TrackBError(f"--arms must be distinct, among {ARMS}, and include off (got {arms})")
    every = load_prompts(cfg.prompts)
    prompts = every[:cfg.limit] if cfg.limit else every
    blocks, _ = load_inject(cfg.inject)
    check_blocks(blocks, arms, prompts, (p["key"] for p in every))
    items = [(arm, p["key"]) for p in prompts for arm in arms]
    by_key = {p["key"]: p for p in prompts}
    num_ctx = required_num_ctx(render(a, by_key[k]["prompt"], blocks.get((a, k))) for a, k in items)
    manifest = {
        "format": FORMAT, "model": cfg.model, "model_digest": None,
        "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": NUM_PREDICT},
        "keep_alive": cfg.keep_alive,
        "prompts_file": str(cfg.prompts), "prompts_sha256": sha256_file(cfg.prompts), "n_prompts": len(prompts),
        "limit": cfg.limit, "arms": arms,
        "inject": [{"path": str(p), "sha256": sha256_file(p)} for p in cfg.inject],
        "render": RENDER, "system_message": None, "think": cfg.think, "request_extra": think_body(cfg.think),
    }
    return Plan(prompts, blocks, num_ctx, manifest, items)


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def journal_path(out: Path) -> Path:
    return out / "journal.jsonl"


def load_journal(out: Path) -> dict[tuple[str, str, int], dict]:
    """``{(model, arm, key): entry}``. A torn last line (a kill mid-write) is ignored; a duplicate key is fatal."""
    p = journal_path(out)
    done: dict[tuple[str, str, int], dict] = {}
    if not p.exists():
        return done
    lines = p.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break
            raise
        k = (e["model"], e["arm"], e["key"])
        if k in done:
            raise TrackBError(f"journal has (model, arm, key) {k} twice")
        done[k] = e
    return done


def repair_tail(out: Path) -> bool:
    """Cut a torn last line (a kill mid-write) so the next append starts on a fresh line. True if one was cut."""
    p = journal_path(out)
    if not p.exists():
        return False
    data = p.read_bytes()
    if not data or data.endswith(b"\n"):
        return False
    with open(p, "r+b") as f:
        f.truncate(data.rfind(b"\n") + 1)
    return True


def _append(out: Path, entry: dict) -> None:
    p = journal_path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _manifest(cfg: GenConfig, pl: Plan) -> dict:
    """Write the manifest, or check a resume against it (every identity field must match)."""
    path = cfg.out / "manifest.json"
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if _identity(old) != _identity(pl.manifest):
            diff = sorted(k for k in _identity(old) if _identity(old)[k] != _identity(pl.manifest)[k])
            raise ManifestMismatch(f"{path} was made under different {diff}; use a new --out")
        return old
    cfg.out.mkdir(parents=True, exist_ok=True)
    m = dict(pl.manifest, created=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    _write_json(path, m)
    return m


def generate(cfg: GenConfig, lock_factory: Callable[[], ContextManager], *, clock: Clock | None = None,
             log: Callable[[str], None] = lambda s: None,
             generator_factory: Callable[..., Generator] | None = None) -> dict:
    """Run every pending (arm, key) for ``cfg.model``. Returns a counts-only summary."""
    clock = clock or Clock()
    factory = generator_factory or make_generator
    pl = plan(cfg)
    man = _manifest(cfg, pl)
    if repair_tail(cfg.out):
        log("journal: cut a torn last line (its item will be redone)")
    done = load_journal(cfg.out)
    by_key = {p["key"]: p for p in pl.prompts}
    pending = [(a, k) for a, k in pl.items if (cfg.model, a, k) not in done]
    skipped = len(pl.items) - len(pending)
    gen = factory(cfg.model, pl.num_ctx, cfg.keep_alive, cfg.call_timeout, cfg.think)
    worst = [0.0]

    def est() -> float:
        return worst[0] or cfg.est_gen_s

    sessions, new_ok, new_err = 0, 0, 0
    while pending:
        need = COLD_S + est()
        _before_session(cfg, clock, sessions, need)   # window, gap, budget: before even asking for the lock
        with lock_factory():
            now = clock.utc()   # the lock may have blocked: the window may be closer now
            if in_window(now):
                raise InWindow(window_end(now))
            budget = _session_budget(cfg, now, need)
            sessions += 1
            start, calls, touched, digest_checked = clock.mono(), 0, False, False
            log(f"session {sessions}: {len(pending)} items pending, budget {budget:.0f}s")
            try:
                while pending:
                    left = budget - (clock.mono() - start)
                    if calls and left < est():
                        log(f"  batch boundary after {calls} calls")
                        break
                    arm, key = pending[0]
                    msg = render(arm, by_key[key]["prompt"], pl.blocks.get((arm, key)))
                    gen.timeout_cap = max(left, MIN_CALL_TIMEOUT_S)
                    clamped = gen.timeout_cap < gen.timeout
                    touched = True
                    t0 = clock.mono()
                    try:
                        raw = gen.ask(msg)
                    except Exception as e:
                        if clamped and calls and _is_timeout(e):
                            log(f"  batch boundary: a clamped call timed out after {calls} calls")
                            break
                        raise
                    lat = round(clock.mono() - t0, 3)
                    calls += 1
                    if not digest_checked and raw.get("_reached", True):   # the model is loaded now
                        digest_checked = True
                        d = gen.loaded_digest()
                        if man.get("model_digest") is None:
                            man["model_digest"] = d
                            _write_json(cfg.out / "manifest.json", man)
                        elif d != man["model_digest"]:
                            raise ManifestMismatch(f"loaded {cfg.model} digest {d[:12]} differs from the "
                                                   f"manifest's {man['model_digest'][:12]}")
                    response, err = classify(raw)
                    entry = {"model": cfg.model, "arm": arm, "key": key, "status": "error" if err else "ok",
                             "s": lat, "done_reason": raw.get("done_reason"), "eval_count": raw.get("eval_count"),
                             "prompt_eval_count": raw.get("prompt_eval_count"),
                             "thinking_present": bool((raw.get("message") or {}).get("thinking"))
                             if isinstance(raw.get("message"), dict) else False}
                    if err:
                        entry["error"] = err
                        new_err += 1
                    else:
                        entry["response"] = response
                        new_ok += 1
                    if raw.get("_reached", True):
                        worst[0] = max(worst[0], lat)
                    _append(cfg.out, entry)
                    pending.pop(0)
            finally:
                gen.timeout_cap = None
                if touched:
                    try:
                        gen.unload()
                        log("  unloaded")
                    except Exception as e:  # noqa: BLE001
                        raise UnloadFailed(str(e)) from e
    return {"model": cfg.model, "items": len(pl.items), "skipped_done": skipped, "new_ok": new_ok,
            "new_errors": new_err, "sessions": sessions, "num_ctx": pl.num_ctx, "num_predict": NUM_PREDICT}


def dry_run_plan(cfg: GenConfig) -> str:
    pl = plan(cfg)
    done = load_journal(cfg.out)
    pending = sum(1 for a, k in pl.items if (cfg.model, a, k) not in done)
    return (f"trackb plan: model {cfg.model}, arms {list(cfg.arms)}, {len(pl.prompts)} prompts, {len(pl.items)} items, "
            f"{pending} pending, num_ctx {pl.num_ctx}, num_predict {NUM_PREDICT}; "
            f"~{pending * cfg.est_gen_s / 3600:.1f} GPU-hours at {cfg.est_gen_s:g}s per call")


# ----------------------------------------------------------------------------------------------------- scoring

@contextlib.contextmanager
def _quiet_checker():
    """The checker logs a response's TEXT at ERROR level when langdetect fails (instructions.py:178); L5 keeps
    generations off every output stream, so silence logging while scoring. langdetect is seeded for determinism."""
    import langdetect
    langdetect.DetectorFactory.seed = LANGDETECT_SEED
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


def score_one(prompt: dict, response: str) -> dict:
    """The four IFEval metrics for one response (the vendored lm_eval ``process_results`` logic)."""
    from .ifeval import utils as ifu
    inp = ifu.InputExample(key=prompt["key"], instruction_id_list=prompt["instruction_id_list"],
                           prompt=prompt["prompt"], kwargs=prompt["kwargs"])
    s = ifu.test_instruction_following_strict(inp, response)
    lo = ifu.test_instruction_following_loose(inp, response)
    return {"prompt_strict": bool(s.follow_all_instructions), "inst_strict": [bool(x) for x in s.follow_instruction_list],
            "prompt_loose": bool(lo.follow_all_instructions), "inst_loose": [bool(x) for x in lo.follow_instruction_list]}


def matched_keys(prompts: Sequence[dict], journal: dict[tuple[str, str, int], dict], model: str,
                 arms: Sequence[str]) -> list[int]:
    """Keys every arm completed with an ``ok`` reply: the INFORMATIONAL matched-only pairing (design rule 6)."""
    return [p["key"] for p in prompts
            if all(journal.get((model, a, p["key"]), {}).get("status") == "ok" for a in arms)]


def itt_keys(prompts: Sequence[dict], journal: dict[tuple[str, str, int], dict], model: str,
             arms: Sequence[str]) -> list[int]:
    """The BLOCKING pairing, intent-to-treat on the treatment arms (coordinator ruling, T-2518 Track B): a prompt
    is dropped only when the OFF arm did not complete it (no baseline) or an arm has not run it yet. A treatment
    arm's ERROR keeps the prompt in, and ``arm_results`` scores it as a fail for that arm."""
    return [p["key"] for p in prompts
            if journal.get((model, "off", p["key"]), {}).get("status") == "ok"
            and all(journal.get((model, a, p["key"]), {}).get("status") in ("ok", "error") for a in arms)]


def arm_results(prompt: dict, entry: dict) -> dict:
    """The four metrics for one (arm, prompt): scored when ok; an error FAILS every instruction (never a pass)."""
    if entry.get("status") == "ok":
        return score_one(prompt, entry["response"])
    n = len(prompt["instruction_id_list"])
    return {"prompt_strict": False, "inst_strict": [False] * n, "prompt_loose": False, "inst_loose": [False] * n}


def verdict_of(delta_pp: float) -> str:
    """FAIL iff the (on - off) prompt-level strict point estimate is BELOW -REGRESSION_LIMIT_PP (-5.0 passes)."""
    return "FAIL" if delta_pp < -REGRESSION_LIMIT_PP else "PASS"


def _rate(xs: list[bool]) -> float:
    return round(100.0 * sum(xs) / len(xs), 4) if xs else 0.0


def score(out: Path, prompts_path: Path | None = None) -> dict:
    """Counts and rates only. Raises ``TrackBError`` when no prompt has a baseline."""
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    ppath = Path(prompts_path or man["prompts_file"])
    if sha256_file(ppath) != man["prompts_sha256"]:
        raise ManifestMismatch(f"{ppath} is not the prompt file this run generated against")
    n_file = len(load_prompts(ppath))
    prompts = load_prompts(ppath, man["limit"])
    model, arms = man["model"], list(man["arms"])
    journal = load_journal(out)
    keys = itt_keys(prompts, journal, model, arms)
    if not keys:
        raise TrackBError("no prompt has a completed off arm and a result in every arm: nothing to score")
    by_key = {p["key"]: p for p in prompts}
    off_status = [journal.get((model, "off", p["key"]), {}).get("status") for p in prompts]
    n_off_error = off_status.count("error")
    n_incomplete = len(prompts) - len(keys) - n_off_error
    per: dict[str, list[dict]] = {}
    with _quiet_checker():
        for a in arms:
            per[a] = [arm_results(by_key[k], journal[(model, a, k)]) for k in keys]
    counts = {}
    for a in arms:
        st = [journal.get((model, a, p["key"]), {}).get("status") for p in prompts]
        errs: dict[str, int] = {}
        for p in prompts:
            e = journal.get((model, a, p["key"]))
            if e and e.get("status") == "error":
                errs[e.get("error", "?")] = errs.get(e.get("error", "?"), 0) + 1
        counts[a] = {"ok": st.count("ok"), "error": st.count("error"), "missing": st.count(None), "errors_by_kind": errs}
    rates = {a: {"prompt_strict": _rate([r["prompt_strict"] for r in per[a]]),
                 "inst_strict": _rate([x for r in per[a] for x in r["inst_strict"]]),
                 "prompt_loose": _rate([r["prompt_loose"] for r in per[a]]),
                 "inst_loose": _rate([x for r in per[a] for x in r["inst_loose"]])} for a in arms}
    deltas = {}
    matched = set(matched_keys(prompts, journal, model, arms))
    for a in arms:
        if a == "off":
            continue
        d = {}
        for metric in ("prompt_strict", "prompt_loose"):
            iv = paired_interval([100.0 * r[metric] for r in per[a]], [100.0 * r[metric] for r in per["off"]],
                                 f"trackb/{model}/{a}-off/{metric}")
            d[metric] = iv.to_dict()
        for metric in ("inst_strict", "inst_loose"):
            d[metric] = {"diff": round(rates[a][metric] - rates["off"][metric], 4)}   # informational: no CI
        # Informational: the old matched-only pairing (a prompt any arm errored on is dropped from every arm).
        mo = [(r["prompt_strict"], o["prompt_strict"]) for k, r, o in zip(keys, per[a], per["off"]) if k in matched]
        d["prompt_strict_matched_only"] = {
            "diff": round(100.0 * sum(x - y for x, y in mo) / len(mo), 4) if mo else None, "n": len(mo)}
        deltas[f"{a}-off"] = d
    res = {"format": FORMAT, "model": model, "model_digest": man.get("model_digest"), "options": man["options"],
           "prompts_sha256": man["prompts_sha256"], "inject": man["inject"], "arms": arms,
           "n_prompts": len(prompts), "n_scored": len(keys), "n_dropped_off_error": n_off_error,
           "n_dropped_incomplete": n_incomplete, "n_matched_only": len(matched),
           "counts": counts, "rates_pct": rates, "deltas_pp": deltas, "ci_level": CI_LEVEL,
           "langdetect_seed": LANGDETECT_SEED,
           "rule": (f"FAIL iff (on - off) prompt-level strict point estimate < -{REGRESSION_LIMIT_PP} pp, "
                    "intent-to-treat (a treatment-arm error is a fail; only off-arm errors drop a prompt); "
                    "CI reported, not gated")}
    res["n_prompt_file"] = n_file
    off_error_limit = MAX_OFF_ERROR_SHARE * n_file
    if "on" not in arms:
        res["verdict"], res["verdict_reason"] = "NO_VERDICT", "no on arm"
    elif man.get("limit") or len(prompts) != n_file:
        # review F1: a verdict is only ever drawn on the WHOLE prompt file; a --limit run is a smoke run
        res["verdict"] = "NO_VERDICT"
        res["verdict_reason"] = f"smoke run: {len(prompts)} of {n_file} prompts (a verdict needs all {n_file})"
    elif n_off_error > off_error_limit:
        res["verdict"] = "NO_VERDICT"
        res["verdict_reason"] = (f"baseline unreliable: the off arm errored on {n_off_error} of {n_file} prompts "
                                 f"(more than {MAX_OFF_ERROR_SHARE:.0%})")
    elif n_incomplete:
        res["verdict"] = "INCOMPLETE"   # an arm has not run every baselined prompt yet: resume gen first
        res["verdict_delta_pp"] = deltas["on-off"]["prompt_strict"]["diff"]
    else:
        dpp = deltas["on-off"]["prompt_strict"]["diff"]
        res["verdict"] = verdict_of(dpp)
        res["verdict_delta_pp"] = dpp
    return res


def render_summary(res: dict) -> str:
    lines = [f"PMB-1 Track B (IFEval) {res['model']} digest {str(res.get('model_digest'))[:12]}",
             f"prompts {res['n_prompts']}, scored {res['n_scored']}, dropped: off-arm error {res['n_dropped_off_error']}, "
             f"incomplete {res['n_dropped_incomplete']} (matched-only n {res['n_matched_only']})"]
    for a, c in res["counts"].items():
        lines.append(f"  {a:<7} ok {c['ok']} error {c['error']} missing {c['missing']} {c['errors_by_kind'] or ''}")
    for a, r in res["rates_pct"].items():
        lines.append(f"  {a:<7} prompt-strict {r['prompt_strict']:.2f}%  inst-strict {r['inst_strict']:.2f}%  "
                     f"prompt-loose {r['prompt_loose']:.2f}%  inst-loose {r['inst_loose']:.2f}%")
    for name, d in res["deltas_pp"].items():
        ps, mo = d["prompt_strict"], d["prompt_strict_matched_only"]
        lines.append(f"  {name:<10} prompt-strict {ps['diff']:+.2f} pp [{int(100 * res['ci_level'])}% CI {ps['lo']:+.2f}, "
                     f"{ps['hi']:+.2f}] (n={ps['n']})")
        if mo["diff"] is not None:
            lines.append(f"  {name:<10} matched-only (informational, not gated) {mo['diff']:+.2f} pp (n={mo['n']})")
    if res["verdict"] == "NO_VERDICT":
        lines.append(f"VERDICT NO_VERDICT: {res['verdict_reason']}")
    elif res["verdict"] == "INCOMPLETE":
        lines.append(f"VERDICT INCOMPLETE: {res['n_dropped_incomplete']} baselined prompts lack a result in some arm; resume gen")
    else:
        lines.append(f"VERDICT {res['verdict']} (on-off {res['verdict_delta_pp']:+.2f} pp; {res['rule']})")
    return "\n".join(lines)

