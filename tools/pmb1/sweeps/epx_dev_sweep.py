"""T-2518 epx phase C: the pre-registered DEV sweep that chose the tier-1 semantic leg's ``DEFAULT_TAU`` /
``DEFAULT_SEM_K`` (``tools/memory_tier_episodic.py``).

Setup: PMB-1 v3 small DEV seeds 1-9 ONLY (never the gate seeds 10-27), stub backbone (strict), every vector
replayed from a READ-ONLY real-embedder cache through ``CachedEmbedder`` (a cache miss raises: stop, never embed).
Grid: sem_k in {3, 5, 10, 20} x tau in {0.60, 0.65, 0.70, 0.75, 0.80}. Reference rows: ``lexical`` (no embedder)
and ``main-fullfusion`` (the pre-epx semantic leg: full-list RRF, tau 0.70, sem_k 5). That row is reproduced by a
RUNTIME shim of ``fuse_ranks`` inside the worker for that row only; the committed tier code is never edited.

Selection rule (pre-registered; ``table`` applies it step by step):
  1. discard any cell with governance-leak > 0, A2-stale > 0, or canonical recall below 0.995;
  2. maximise paraphrase recall;
  3. among cells within 0.005 of the best, take the lowest E3 mean_tokens;
  4. then the higher tau;
  5. then the smaller sem_k.
A selected cell on a grid edge is reported, never answered by extending the grid.

usage (from the repo root; foreground, resumable: one JSON per (cell, seed) in OUT; jobs not STARTED before the
deadline are deferred, so re-run the same command to continue):
  python3 -m tools.pmb1.sweeps.epx_dev_sweep run   OUT DEADLINE_S --cache DIR --embedder ollama:<model>@<digest>
  python3 -m tools.pmb1.sweeps.epx_dev_sweep table OUT
"""
from __future__ import annotations

import argparse
import json
import os
import time
from multiprocessing import Pool
from pathlib import Path

SEEDS = tuple(range(1, 10))           # DEV only. Never 10-27.
TAUS = (0.60, 0.65, 0.70, 0.75, 0.80)
SEMKS = (3, 5, 10, 20)
LEXICAL, MAIN_REF = "lexical", "main-fullfusion"
MAIN_REF_TAU, MAIN_REF_SEMK = 0.70, 5
TRACKS = ("A1", "A-retrieve", "E3", "A2", "GOV")
CANONICAL_FLOOR, NEAR_BEST = 0.995, 0.005
CELLS = [LEXICAL, MAIN_REF] + [f"{t:.2f}_{k}" for t in TAUS for k in SEMKS]

_EMB = None
_CFG: dict = {}


def _init(cache: str, embedder: str, out: str, deadline: float) -> None:
    global _EMB
    from tools.pmb1 import offline
    offline.install()
    from tools.pmb1 import embedder as em
    _EMB = em.CachedEmbedder(embedder, Path(cache), 768)   # replay-only; never written
    _CFG.update(out=Path(out), deadline=deadline)


def _full_list_fuse_ranks(tier):
    """The pre-epx S2 (reference row only): the FULL semantic list feeds the fused score."""
    def fuse_ranks(lex_list, sem_list, sem_k):
        fused = tier.rrf_scores(lex_list, sem_list)
        return sorted(set(lex_list) | set(sem_list[:max(sem_k, 0)]), key=lambda k: (-fused[k], k))
    return fuse_ranks


def _job(args: tuple[str, int]) -> str:
    cell, seed = args
    if seed not in SEEDS:
        raise ValueError(f"seed {seed} is not a DEV seed")
    f = _CFG["out"] / f"{cell}__seed-{seed}.json"
    if f.exists():
        return f"have {f.name}"
    if time.time() > _CFG["deadline"]:
        return f"deferred {f.name}"
    from unittest import mock

    from tools.pmb1.backbone import StubBackbone
    from tools.pmb1.episodic import EpisodicTierSubstrate, tier   # the tier module, as the substrate imports it
    from tools.pmb1.fixtures_v3 import generate_by_version
    from tools.pmb1.runner import _store_kwargs, run_one
    fx = generate_by_version(seed, "small", 3)
    if cell == LEXICAL:
        make = lambda: EpisodicTierSubstrate(**_store_kwargs(fx))  # noqa: E731
    else:
        tau, semk = (MAIN_REF_TAU, MAIN_REF_SEMK) if cell == MAIN_REF else (float(cell.split("_")[0]), int(cell.split("_")[1]))
        make = lambda: EpisodicTierSubstrate(**_store_kwargs(fx), embedder=_EMB, tau=tau, sem_k=semk)  # noqa: E731
    shim = mock.patch.object(tier, "fuse_ranks", _full_list_fuse_ranks(tier)) if cell == MAIN_REF else mock.MagicMock()
    t0, misses0 = time.time(), _EMB.misses
    with shim:
        rec = run_one(make, StubBackbone("strict"), fx, gate=False, tracks=TRACKS)
    if _EMB.misses != misses0:
        raise RuntimeError(f"embed cache miss in {cell} seed {seed}: STOP")
    modes = [p.mode for p in fx.probes]
    rec_s, a1_s = rec["samples"]["A-retrieve.recall"], rec["samples"]["A1.accuracy"]
    if not len(modes) == len(rec_s) == len(a1_s):
        raise RuntimeError("probe / sample count mismatch")
    out = {"cell": cell, "seed": seed, "secs": round(time.time() - t0, 1), "embedding_model": rec.get("embedding_model"),
           "recall": rec_s, "a1": a1_s, "modes": modes,
           "precision": rec["tracks"]["A-retrieve"]["metrics"]["precision"],
           "mean_returned": rec["tracks"]["A-retrieve"]["metrics"]["mean_returned"],
           "e3_mean_tokens": rec["tracks"]["E3"]["metrics"]["mean_tokens"],
           "gov_leaks": sum(rec["samples"]["GOV.leak"]), "a2_stale": sum(rec["samples"]["A2.stale_evidence"])}
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(out))
    os.replace(tmp, f)
    return f"done {f.name} {out['secs']}s"


def run(out: Path, deadline_s: float, cache: str, embedder: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    jobs = [(c, s) for c in CELLS for s in SEEDS]
    with Pool(4, initializer=_init, initargs=(cache, embedder, str(out), time.time() + deadline_s)) as p:
        for r in p.imap_unordered(_job, jobs):
            if not r.startswith("have"):
                print(r, flush=True)
    print("complete:", sum(1 for c, s in jobs if (out / f"{c}__seed-{s}.json").exists()), "/", len(jobs), flush=True)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def pool_rows(out: Path) -> dict[str, dict]:
    rows = {}
    for c in CELLS:
        rs = [json.loads((out / f"{c}__seed-{s}.json").read_text()) for s in SEEDS if (out / f"{c}__seed-{s}.json").exists()]
        if len(rs) != len(SEEDS):
            print(f"{c}: {len(rs)}/{len(SEEDS)} seeds")
            continue
        rec = [x for r in rs for x in r["recall"]]
        a1 = [x for r in rs for x in r["a1"]]
        mode = [m for r in rs for m in r["modes"]]
        rows[c] = dict(
            recall=_mean(rec), canonical=_mean([x for x, m in zip(rec, mode) if m == "canonical"]),
            paraphrase=_mean([x for x, m in zip(rec, mode) if m == "paraphrase"]), a1=_mean(a1),
            n=f"{len(rec)}={mode.count('canonical')}c+{mode.count('paraphrase')}p",
            precision=_mean([r["precision"] for r in rs]), mean_returned=_mean([r["mean_returned"] for r in rs]),
            e3=_mean([r["e3_mean_tokens"] for r in rs]), leaks=sum(r["gov_leaks"] for r in rs),
            stale=sum(r["a2_stale"] for r in rs), emb={r["embedding_model"] for r in rs})
    return rows


def select(rows: dict[str, dict]) -> list[str]:
    """The pre-registered rule, one line per step; the last line names the selected cell."""
    grid = {c: r for c, r in rows.items() if c not in (LEXICAL, MAIN_REF)}
    if len(grid) != len(TAUS) * len(SEMKS):
        return [f"grid incomplete: {len(grid)}/{len(TAUS) * len(SEMKS)} cells"]
    tau = lambda c: float(c.split("_")[0])  # noqa: E731
    semk = lambda c: int(c.split("_")[1])  # noqa: E731
    lines = []
    s1 = {c: r for c, r in grid.items() if r["leaks"] == 0 and r["stale"] == 0 and r["canonical"] >= CANONICAL_FLOOR}
    lines.append(f"step 1 (gov-leak 0, A2-stale 0, canonical >= {CANONICAL_FLOOR}) keeps {len(s1)}/{len(grid)}; "
                 f"dropped: {sorted(set(grid) - set(s1))}")
    if not s1:
        return lines + ["no cell survives step 1"]
    best = max(r["paraphrase"] for r in s1.values())
    lines.append(f"step 2 best paraphrase recall {best:.4f} at {sorted(c for c, r in s1.items() if r['paraphrase'] == best)}")
    s3 = {c: r for c, r in s1.items() if r["paraphrase"] >= best - NEAR_BEST}
    lo = min(r["e3"] for r in s3.values())
    s3b = {c: r for c, r in s3.items() if r["e3"] == lo}
    lines.append(f"step 3 within {NEAR_BEST} of best: {sorted(s3)}; lowest E3 mean_tokens {lo:.4f} at {sorted(s3b)}")
    hi = max(tau(c) for c in s3b)
    s4 = [c for c in s3b if tau(c) == hi]
    lines.append(f"step 4 higher tau ({hi:.2f}): {sorted(s4)}")
    pick = min(s4, key=semk)
    edge = [w for w, on in (("tau", tau(pick) in (TAUS[0], TAUS[-1])), ("sem_k", semk(pick) in (SEMKS[0], SEMKS[-1]))) if on]
    lines.append(f"step 5 smaller sem_k -> SELECTED {pick} (tau {tau(pick):.2f}, sem_k {semk(pick)})"
                 + (f"; ON THE GRID EDGE in {', '.join(edge)} (reported, grid not extended)" if edge else "; interior"))
    return lines


def table(out: Path) -> None:
    rows = pool_rows(out)
    print("| cell (tau_semk) | recall | canonical | paraphrase | A1 acc (stub) | precision | mean_returned | "
          "E3 mean_tokens | gov-leak | A2-stale |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for c, r in rows.items():
        print(f"| {c} | {r['recall']:.4f} | {r['canonical']:.4f} | {r['paraphrase']:.4f} | {r['a1']:.4f} | "
              f"{r['precision']:.4f} | {r['mean_returned']:.3f} | {r['e3']:.3f} | {r['leaks']} | {r['stale']} |")
    print("probes:", sorted({r["n"] for r in rows.values()}),
          "embedders:", sorted(x for x in set().union(*(r["emb"] for r in rows.values())) if x))
    for line in select(rows):
        print(line)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("out", type=Path)
    r.add_argument("deadline_s", type=float)
    r.add_argument("--cache", required=True, help="read-only embed cache dir (replay only)")
    r.add_argument("--embedder", required=True, help="the cache's embedder name, ollama:<model>@<digest>")
    t = sub.add_parser("table")
    t.add_argument("out", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        run(a.out, a.deadline_s, a.cache, a.embedder)
    else:
        table(a.out)


if __name__ == "__main__":
    main()
