"""CLI: ``python3 -m tools.pmb1 run ...``, ``python3 -m tools.pmb1 gate --tier 1 <records...>`` and
``python3 -m tools.pmb1 trackb {gen,score,build-random} ...`` (Track B, IFEval)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .backbone import Backbone, OllamaBackbone, StubBackbone, TruncatedPrompt
from .embedder import EmbedderError, OllamaEmbedder, parse_embedder_spec
from .fixtures_v3 import generate_from_spec, parse_fixture_spec
from .gate import gate
from .records import load_records
from .realrun import (
    EST_CALL_S, GAP_S, KEEP_ALIVE, MAX_LOCK_S, InWindow, JournalMismatch, LockSession, RunConfig, UnloadFailed, dry_run_plan,
    realrun,
)
from .embedphase import DEFAULT_BATCH
from .runner import GateRunRefused, run
from . import trackb


def make_backbone(spec: str) -> Backbone:
    """``stub`` | ``stub-lenient`` | ``ollama:<model>:<family>``."""
    if spec == "stub":
        return StubBackbone("strict")
    if spec == "stub-lenient":
        return StubBackbone("lenient")
    if spec.startswith("ollama:"):
        parts = spec.split(":")
        if len(parts) < 3:
            raise ValueError("ollama backbone is ollama:<model>:<family>")
        return OllamaBackbone(":".join(parts[1:-1]), parts[-1])
    raise ValueError(f"unknown backbone {spec!r}")


def _cmd_run(a: argparse.Namespace) -> int:
    seeds = [int(x) for x in a.seeds.split(",") if x] if a.seeds else [None]
    base_seed, size, version = parse_fixture_spec(a.fixture)
    for seed in seeds:
        spec = f"seed={base_seed if seed is None else seed},size={size},version={version}"
        out = Path(a.out) if seed is None else Path(a.out) / f"seed-{seed}"
        try:
            backbones = [make_backbone(s) for s in a.backbone.split(",") if s]
            records, stab = run(
                [s for s in a.substrate.split(",") if s], backbones, generate_from_spec(spec), gate=a.gate,
                out_dir=out, embedder_is_lexical_stub=a.lexical_stub_embedder,
            )
        except (GateRunRefused, ValueError) as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
        print(f"fixture {spec}")
        for r in records:
            t = r["tracks"]
            print(f"{r['substrate']['name']:<22} {r['backbone']['name']:<14} "
                  f"A1={t['A1']['metrics']['accuracy']:.3f} "
                  f"A-retrieve.recall={t['A-retrieve']['metrics']['recall']:.3f} "
                  f"E3.utility={t['E3']['metrics']['utility']:.3f}")
        print(f"not run (named stubs): {', '.join(records[0]['not_run'])}")
        print(f"rank order stable across families: {stab['stable']}")
        print(f"records written to {out}")
    return 0


def _lock_factory(spec: str | None):
    """``module:callable`` returning one lock session (a context manager) per call; default: no lock (dry runs, tests)."""
    if not spec:
        return LockSession
    import importlib
    mod, _, attr = spec.partition(":")
    return getattr(importlib.import_module(mod), attr)


def _cmd_realrun(a: argparse.Namespace) -> int:
    seed0, size, version = parse_fixture_spec(a.fixture)
    seeds = [int(x) for x in a.seeds.split(",") if x] if a.seeds else [seed0]
    cfg = RunConfig(
        out=Path(a.out), fixture_size=size, fixture_version=version, seeds=seeds,
        backbones=[s for s in a.backbones.split(",") if s], substrates=[s for s in a.substrate.split(",") if s],
        embedder_is_lexical_stub=a.lexical_stub_embedder, max_lock_s=a.max_lock_s, gap_s=a.gap_s, est_call_s=a.est_call_s,
        embedder=a.embedder, embed_batch=a.embed_batch, extractor_is_pattern_stub=a.pattern_stub_extractor,
    )

    def build(spec: str, num_ctx: int) -> Backbone:
        if not spec.startswith("ollama:"):
            return make_backbone(spec)
        parts = spec.split(":")
        return OllamaBackbone(":".join(parts[1:-1]), parts[-1], keep_alive=a.keep_alive, num_ctx=num_ctx, timeout=a.call_timeout)

    def build_embedder(spec: str) -> OllamaEmbedder:
        model, digest = parse_embedder_spec(spec)
        return OllamaEmbedder(model, digest, keep_alive=a.keep_alive, timeout=a.call_timeout)

    try:
        if a.dry_run:
            print(dry_run_plan(cfg))
            return 0
        summary = realrun(cfg, build, _lock_factory(a.lock_session), log=lambda s: print(s, flush=True),
                          embedder_factory=build_embedder if a.embedder else None)
    except InWindow as e:
        print(f"blocked: {e}", file=sys.stderr)
        return 3
    except (GateRunRefused, ValueError, JournalMismatch, TruncatedPrompt, EmbedderError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except UnloadFailed as e:
        print(f"unload failed (a model may still be resident on the GPU): {e}", file=sys.stderr)
        return 4
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _cmd_gate(a: argparse.Namespace) -> int:
    try:
        v = gate(a.tier, load_records(a.records), candidate=a.candidate)
    except NotImplementedError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(json.dumps(v.to_dict(), indent=2) if a.json else v.render())
    return 0 if v.passed else 1


def _cmd_trackb_gen(a: argparse.Namespace) -> int:
    cfg = trackb.GenConfig(
        model=a.model, out=Path(a.out), arms=[s for s in a.arms.split(",") if s], prompts=Path(a.prompts),
        inject=[Path(p) for p in a.inject or []], limit=a.limit, max_lock_s=a.max_lock_s, gap_s=a.gap_s,
        call_timeout=a.call_timeout, keep_alive=a.keep_alive, est_gen_s=a.est_gen_s, think=a.think,
    )
    try:
        if a.dry_run:
            print(trackb.dry_run_plan(cfg))
            return 0
        if not a.lock_session:
            # Fail closed: _lock_factory(None) is a NO-OP lock, so a real run without this flag would hold no GPU lock.
            print("refused: a real trackb gen needs --lock-session module:callable (the shared GPU lock); without it "
                  "the run would hold no lock. Use --dry-run to plan without one.", file=sys.stderr)
            return 2
        summary = trackb.generate(cfg, _lock_factory(a.lock_session), log=lambda s: print(s, flush=True))
    except InWindow as e:
        print(f"blocked: {e}", file=sys.stderr)
        return 3
    except (trackb.TrackBError, ValueError, TruncatedPrompt) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except UnloadFailed as e:
        print(f"unload failed (a model may still be resident on the GPU): {e}", file=sys.stderr)
        return 4
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _cmd_trackb_score(a: argparse.Namespace) -> int:
    try:
        res = trackb.score(Path(a.out), Path(a.prompts) if a.prompts else None)
    except (trackb.TrackBError, FileNotFoundError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    if a.write:
        Path(a.write).write_text(json.dumps(res, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(res, indent=2, sort_keys=True) if a.json else trackb.render_summary(res))
    return {"PASS": 0, "FAIL": 1}.get(res["verdict"], 2)


def _cmd_trackb_build_random(a: argparse.Namespace) -> int:
    try:
        blocks, meta = trackb.load_inject([Path(a.on)])
        keys = sorted({k for arm, k in blocks if arm == "on"})
        if not keys:
            raise trackb.TrackBError(f"{a.on} has no on-arm rows")
        template, sep = trackb.resolve_template(meta, a.template, a.doc_sep)
        rows = trackb.build_random(keys, trackb.load_pool(Path(a.pool)), a.k, a.seed, template, sep)
    except (trackb.TrackBError, ValueError, FileNotFoundError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps({"meta": {"template": template, "doc_sep": sep, "k": a.k, "seed": a.seed,
                                     "pool_sha256": trackb.sha256_file(Path(a.pool))}}) + "\n")
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} random-arm blocks (k={a.k}, seed={a.seed}) to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pmb1")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run substrates x backbones over a synthetic fixture")
    r.add_argument("--substrate", default="governed,rag,random,full,amnesiac")
    r.add_argument("--backbone", default="stub")
    r.add_argument("--fixture", default="seed=1,size=small", help="seed=N,size=S[,version=2|3] (v2: episodic, discriminating; v3: + governance and supersede, what tier 1 should gate on)")
    r.add_argument("--seeds", help="comma list of seeds: one run per seed into <out>/seed-N (a v2 gate pools >= 3)")
    r.add_argument("--lexical-stub-embedder", action="store_true",
                   help="declare the RAG baseline uses the hashing stub embedder (a v2 gate run refuses it otherwise)")
    r.add_argument("--out", required=True)
    r.add_argument("--gate", action="store_true", help="mark as a gate run (needs >=2 backbone families)")
    r.set_defaults(fn=_cmd_run)
    rr = sub.add_parser("realrun", help="batched, lock-aware real-model gate run (resumable; see tools/pmb1/realrun.py)")
    rr.add_argument("--fixture", required=True, help="size=S,version=3|4[,seed=N]")
    rr.add_argument("--seeds", help="comma list of seeds (default: the fixture's seed)")
    rr.add_argument("--backbones", required=True, help="comma list of ollama:<model>:<family> (>= 2 families)")
    rr.add_argument("--substrate", default="governed,episodic,rag,random,full,amnesiac")
    emb = rr.add_mutually_exclusive_group()
    emb.add_argument("--lexical-stub-embedder", action="store_true")
    emb.add_argument("--embedder", help="ollama:<model>@<12-hex digest>: the real embedder of the RAG baseline and the "
                                        "episodic tier's semantic leg, precomputed in "
                                        "its own lock sessions before any backbone session (default: the hashing stub)")
    rr.add_argument("--pattern-stub-extractor", action="store_true",
                    help="v4 (R6): declare that a substrate's relation extractor is a pattern stub (the semantic tier's "
                         "is); a v4 gate run refuses it otherwise")
    rr.add_argument("--embed-batch", type=int, default=DEFAULT_BATCH, help="texts per /api/embed request in the precompute")
    rr.add_argument("--out", required=True)
    rr.add_argument("--max-lock-s", type=float, default=MAX_LOCK_S, help="longest a lock session may last")
    rr.add_argument("--gap-s", type=float, default=GAP_S, help="release between sessions")
    rr.add_argument("--est-call-s", type=float, default=EST_CALL_S, help="per-call estimate for a model with no measured entry")
    rr.add_argument("--keep-alive", default=KEEP_ALIVE, help="warm inside a session; unloaded explicitly on release")
    rr.add_argument("--call-timeout", type=float, default=120.0)
    rr.add_argument("--lock-session", help="module:callable returning one lock session (context manager); default none")
    rr.add_argument("--dry-run", action="store_true", help="print the unit plan and estimated sessions; call nothing")
    rr.set_defaults(fn=_cmd_realrun)
    g = sub.add_parser("gate", help="evaluate a tier gate over run records")
    g.add_argument("--tier", type=int, required=True)
    g.add_argument("--candidate")
    g.add_argument("--json", action="store_true")
    g.add_argument("records", nargs="+")
    g.set_defaults(fn=_cmd_gate)
    tb = sub.add_parser("trackb", help="Track B: the IFEval capability-regression instrument (see tools/pmb1/trackb.py)")
    tbs = tb.add_subparsers(dest="trackb_cmd", required=True)
    tg = tbs.add_parser("gen", help="lock-aware, resumable generation of every (arm, prompt) for one model")
    tg.add_argument("--model", required=True,
                    help="the Ollama model tag, e.g. qwen2.5:14b (PMB1_OLLAMA_URL names the server)")
    tg.add_argument("--out", required=True, help="run dir: manifest.json + journal.jsonl (resume by rerunning)")
    tg.add_argument("--arms", default=",".join(trackb.ARMS))
    tg.add_argument("--inject", action="append",
                    help="JSONL {key, arm, block}; repeatable (e.g. the on file and the random file)")
    tg.add_argument("--prompts", default=str(trackb.DEFAULT_PROMPTS))
    tg.add_argument("--limit", type=int, help="only the first N prompts (a smoke run; the verdict needs all 541)")
    tg.add_argument("--max-lock-s", type=float, default=MAX_LOCK_S)
    tg.add_argument("--gap-s", type=float, default=GAP_S)
    tg.add_argument("--call-timeout", type=float, default=trackb.CALL_TIMEOUT_S)
    tg.add_argument("--keep-alive", default=trackb.KEEP_ALIVE)
    tg.add_argument("--est-gen-s", type=float, default=trackb.EST_GEN_S, help="per-call estimate until one is measured")
    tg.add_argument("--lock-session", help="module:callable returning one lock session (context manager); REQUIRED "
                                           "for a real run (refused without it), optional with --dry-run")
    tg.add_argument("--think", choices=trackb.THINK_MODES, default="off",
                    help="off (default): send Ollama's top-level think=false; model-default: send no think field")
    tg.add_argument("--dry-run", action="store_true", help="print the plan; call nothing")
    tg.set_defaults(fn=_cmd_trackb_gen)
    ts = tbs.add_parser("score", help="score a run dir: rates, paired deltas with CI, the verdict (exit 0 PASS, 1 FAIL)")
    ts.add_argument("--out", required=True)
    ts.add_argument("--prompts", help="the prompt file (default: the manifest's)")
    ts.add_argument("--json", action="store_true")
    ts.add_argument("--write", help="also write the JSON result here")
    ts.set_defaults(fn=_cmd_trackb_score)
    tr = tbs.add_parser("build-random", help="build the random arm's inject file from a doc pool")
    tr.add_argument("--on", required=True,
                    help="the on arm's inject file (its keys, and its {\"meta\": {template, doc_sep}} row)")
    tr.add_argument("--pool", required=True, help="JSONL {\"text\": ...} docs")
    tr.add_argument("--k", type=int, required=True, help="docs per prompt")
    tr.add_argument("--seed", type=int, required=True)
    tr.add_argument("--template", help="block template containing {docs}; only when the on file has no meta row")
    tr.add_argument("--doc-sep", help="separator between docs (default a blank line); only when the on file has no meta row")
    tr.add_argument("--out", required=True)
    tr.set_defaults(fn=_cmd_trackb_build_random)
    a = p.parse_args(argv)
    return a.fn(a)
