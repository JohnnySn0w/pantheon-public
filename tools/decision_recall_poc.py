#!/usr/bin/env python3
"""Decision-recall PoC (T-2816) — offline retrieval-quality eval, NOT a live hook.

The Dei recall-hook design (T-0672 / notes/dei-foundational-wip/recall-hook.md)
DECLINED broad fuzzy recall as bloat-positive and locked in only a narrow
exact-token slice for `feedback`/`project` memories (tools/hooks/recall-feedback.py,
T-1090). Decisions (ADRs) are today in NO recall path at all. This PoC asks
whether surfacing the RELEVANT past ADR at point-of-need — without the agent
knowing to look for it — clears the bar that design set, by measuring hit-rate,
precision, and (separately) a false-trigger-rate against a hand-labeled eval set.

Design (mirrors recall-feedback.py's framing where it applies):
  * Retriever  : hand-rolled BM25 (no new dependency — rank_bm25/sentence_transformers
                 are both absent from this repo and a PoC must not add either) over a
                 CANDIDATE POOL = the hand-authored trigger-sidecar ADR ids UNION a
                 recency-window (the K most-recently-dated ADRs, safety net for ADRs
                 the sidecar hasn't caught up to yet — title-only doc text for those).
  * Doc text   : trigger phrase + title for sidecar entries; title-only fallback for
                 recency-only entries. NOT full ADR body — mirrors recall-feedback.py's
                 "match on the description/subject, not incidental body mentions" precision
                 floor (tools/hooks/recall-feedback.py `_parse_memory` docstring).
  * Injection  : score > 0 gate (any lexical overlap at all — deliberately the LOOSEST
                 possible gate; a tuned magnitude threshold is exactly what the false-
                 trigger-rate number tells us whether we need), capped at k=2 (mirrors
                 recall-feedback.py's MAX_SURFACED default).
  * Fail-open  : a missing/malformed trigger sidecar or a malformed ADR file must not
                 crash retrieval for the rest of the corpus.

Scope: this file is an OFFLINE eval CLI, not a PreToolUse hook. No live wiring, no ADR
frontmatter mutation (ADRs are read-only source-of-truth here), no vector/embedding
retrieval (deferred — see decision_recall_poc_RESULTS.md follow-up note).

Run:
    python3 -m tools.decision_recall_poc                       # eval mode (real corpus)
    python3 -m tools.decision_recall_poc --query "..." --k 2   # ad-hoc single query
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = REPO_ROOT / "architecture" / "adr"
TRIGGERS_PATH = Path(__file__).resolve().parent / "decision_recall_triggers.json"
EVAL_SET_PATH = Path(__file__).resolve().parent / "decision_recall_eval_set.json"

# Recency-window size (Last_K) — brief's range was 3-5; picked the top of the
# range so the safety net covers a fuller "sidecar hasn't caught up yet" gap.
RECENCY_K = 5
# Precision cap — mirrors recall-feedback.py's MAX_SURFACED default.
PRECISION_CAP = 2
# BM25 (Robertson/Sparck Jones) textbook defaults — chosen a priori, NOT tuned
# against the eval set (that would be fitting the eval to the retriever, the
# exact discipline decision_recall_eval_set.json's _note guards against).
BM25_K1 = 1.5
BM25_B = 0.75

# Standard NLTK English stopwords corpus (the well-known ~180-word list,
# contraction fragments included because `tokenize()`'s regex splits on the
# apostrophe: "don't" -> "don","t"). Picked WHOLESALE, off-the-shelf, a
# priori — NOT hand-curated from this PoC's own eval scenarios. (First pass
# used a short ad-hoc list; it under-filtered "what"/"how"/etc., which a
# synthetic-fixture-driven unit test run did NOT catch but the real-corpus
# eval run did — see decision_recall_poc_RESULTS.md's iteration note. Once
# that gap was visible, swapping in a *complete standard* list rather than
# adding just the missing words is what keeps this a-priori: a standard list
# is the same list regardless of which words happen to have burned us.)
_STOPWORDS = frozenset("""
i me my myself we our ours ourselves you your yours yourself yourselves he
him his himself she her hers herself it its itself they them their theirs
themselves what which who whom this that these those am is are was were be
been being have has had having do does did doing a an the and but if or
because as until while of at by for with about against between into through
during before after above below to from up down in out on off over under
again further then once here there when where why how all any both each
few more most other some such no nor not only own same so than too very s t
can will just don should now d ll m o re ve y ain aren couldn didn doesn
hadn hasn haven isn ma mightn mustn needn shan shouldn wasn weren won wouldn
""".split())
# ORG-NNN / T-NNNN style ids kept as one token (e.g. "org-017", "t-1216") —
# split on generic alnum runs otherwise loses the identifier as a unit.
_RE_TOKEN = re.compile(r"[a-z]+-\d+|[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase + tokenize, dropping stopwords. `ORG-017`/`T-1216`-style ids
    survive as single hyphenated tokens; everything else is a bare alnum run."""
    tokens = _RE_TOKEN.findall(text.lower())
    return [t for t in tokens if t not in _STOPWORDS]


class BM25:
    """Minimal BM25 over a small, fixed, in-memory doc set (no index persistence
    needed — the candidate pool is at most a few dozen ADRs per call)."""

    def __init__(self, docs: dict[str, list[str]], k1: float = BM25_K1, b: float = BM25_B):
        self.k1 = k1
        self.b = b
        self.doc_ids = list(docs.keys())
        self.doc_len = {did: len(toks) for did, toks in docs.items()}
        self.n = len(self.doc_ids)
        self.avgdl = (sum(self.doc_len.values()) / self.n) if self.n else 0.0
        self.tf = {did: Counter(toks) for did, toks in docs.items()}
        self.df: Counter = Counter()
        for toks in docs.values():
            for term in set(toks):
                self.df[term] += 1

    def _idf(self, term: str) -> float:
        n_q = self.df.get(term, 0)
        # +0.5/+0.5 smoothing; floored at 0 so a term present in every doc of a
        # tiny corpus can't drag a doc's score negative.
        return max(0.0, math.log((self.n - n_q + 0.5) / (n_q + 0.5) + 1.0))

    def score(self, query_tokens: list[str], doc_id: str) -> float:
        if doc_id not in self.tf or self.n == 0:
            return 0.0
        tf = self.tf[doc_id]
        dl = self.doc_len[doc_id] or 1
        total = 0.0
        for term in query_tokens:
            f = tf.get(term, 0)
            if f == 0:
                continue
            idf = self._idf(term)
            denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
            total += idf * (f * (self.k1 + 1)) / denom
        return total

    def scores(self, query_tokens: list[str]) -> dict[str, float]:
        return {did: self.score(query_tokens, did) for did in self.doc_ids}


# ---------------------------------------------------------------------------
# ADR corpus loading — READ-ONLY. Never writes to architecture/adr/*.md (D2-protected
# source-of-truth); a malformed file is skipped, never fatal to the rest.
# ---------------------------------------------------------------------------

_RE_FM_ALIASES = re.compile(r"^aliases:\s*\[([^\]]*)\]", re.MULTILINE)
_RE_FM_TITLE = re.compile(r"^title:\s*\"?([^\"\n]+?)\"?\s*$", re.MULTILINE)
_RE_FM_DATE = re.compile(r"^date:\s*(\S+)", re.MULTILINE)
_RE_ID_FROM_FILENAME = re.compile(r"^(ORG-\d{3})", re.IGNORECASE)


def _parse_adr_frontmatter(text: str, filename: str) -> tuple[str | None, str | None, str | None]:
    """Return (org_id, title, date) parsed from frontmatter, or Nones on failure.
    Fails open per-field — a missing date doesn't block extracting the title/id."""
    if not text.startswith("---"):
        return None, None, None
    end = text.find("\n---", 3)
    fm = text[:end] if end != -1 else text[:800]

    org_id = None
    m = _RE_FM_ALIASES.search(fm)
    if m:
        first = m.group(1).split(",")[0].strip()
        if first:
            org_id = first.upper()
    if org_id is None:
        m2 = _RE_ID_FROM_FILENAME.match(filename)
        if m2:
            org_id = m2.group(1).upper()

    title_m = _RE_FM_TITLE.search(fm)
    title = title_m.group(1).strip() if title_m else None

    date_m = _RE_FM_DATE.search(fm)
    date = date_m.group(1).strip() if date_m else None

    return org_id, title, date


def load_adrs(adr_dir: Path = ADR_DIR) -> dict[str, dict]:
    """Parse `architecture/adr/*.md` frontmatter -> {org_id: {"title", "date", "path"}}.

    A file with no parseable id (no `aliases:` and no ORG-NNN filename prefix)
    is skipped — fail-open, since one malformed ADR must not take down
    retrieval for the other 73."""
    out: dict[str, dict] = {}
    if not adr_dir.is_dir():
        return out
    for path in sorted(adr_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        org_id, title, date = _parse_adr_frontmatter(text, path.name)
        if org_id is None:
            continue
        out[org_id] = {"title": title or "", "date": date or "", "path": str(path)}
    return out


def load_triggers(path: Path = TRIGGERS_PATH) -> dict[str, str]:
    """Load the hand-authored trigger sidecar. FAIL-OPEN: a missing file,
    malformed JSON, or a non-dict top level all resolve to `{}` — the
    recency-window still supplies a (weaker) candidate pool on its own."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).upper(): v for k, v in raw.items() if isinstance(v, str)}


def recency_window(adrs: dict[str, dict], k: int = RECENCY_K) -> set[str]:
    """Org ids of the k most-recently-dated ADRs. Entries with a missing/empty
    date are excluded entirely (never displace a dated ADR, never a false
    "recent" claim of their own)."""
    if k <= 0:
        return set()
    dated = [(org_id, meta.get("date") or "") for org_id, meta in adrs.items()]
    dated = [(org_id, d) for org_id, d in dated if d]
    dated.sort(key=lambda pair: (pair[1], pair[0]), reverse=True)
    return {org_id for org_id, _ in dated[:k]}


def build_documents(adrs: dict[str, dict], triggers: dict[str, str], pool: set[str]) -> dict[str, list[str]]:
    """Doc text per candidate id: trigger phrase + title when a trigger exists,
    title-only fallback otherwise (the recency-window safety net). Ids in
    `pool` that don't resolve to a loaded ADR are silently skipped."""
    docs: dict[str, list[str]] = {}
    for org_id in pool:
        meta = adrs.get(org_id)
        if meta is None:
            continue
        parts = []
        trig = triggers.get(org_id)
        if trig:
            parts.append(trig)
        parts.append(meta.get("title", ""))
        docs[org_id] = tokenize(" ".join(parts))
    return docs


def retrieve(
    query: str,
    adrs: dict[str, dict],
    triggers: dict[str, str],
    k: int = PRECISION_CAP,
    recency_k: int = RECENCY_K,
    min_score: float = 0.0,
) -> list[tuple[str, float]]:
    """Return up to k (org_id, score) pairs, score > min_score, ranked
    descending by score then org_id. Candidate pool = sidecar trigger ids
    UNION the recency-window; anything outside that pool is structurally
    unreachable, by design (this is a scoped PoC, not a full-corpus search —
    see module docstring).

    `min_score` defaults to 0.0 — the LOOSEST possible gate (any lexical
    overlap at all surfaces). That default is the PoC's headline number on
    purpose (see decision_recall_poc_RESULTS.md); the parameter exists so the
    RESULTS.md threshold-sensitivity appendix can report what a raised gate
    would buy, without that exploration changing the headline baseline."""
    pool = (set(triggers.keys()) | recency_window(adrs, recency_k)) & set(adrs.keys())
    docs = build_documents(adrs, triggers, pool)
    if not docs:
        return []
    bm25 = BM25(docs)
    q_tokens = tokenize(query)
    scored = bm25.scores(q_tokens)
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(org_id, score) for org_id, score in ranked if score > min_score][:k]


# ---------------------------------------------------------------------------
# Eval harness
# ---------------------------------------------------------------------------

def load_eval_set(path: Path = EVAL_SET_PATH) -> list[dict]:
    """Accepts either the wrapped `{"_note": ..., "scenarios": [...]}` shape
    (the authored form, so the anti-fitting discipline note travels with the
    data) or a bare list, for test convenience."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        return raw.get("scenarios", [])
    return raw


def evaluate(
    scenarios: list[dict],
    adrs: dict[str, dict],
    triggers: dict[str, str],
    k: int = PRECISION_CAP,
    min_score: float = 0.0,
) -> dict:
    """Compute hit-rate@k (over scenarios with a non-empty `expected`),
    precision@k (over scenarios that returned >=1 result, positive or
    negative), and false-trigger-rate (over scenarios with an EMPTY expected
    set — the direct bloat-risk number: how often does the retriever speak
    when it should stay silent)."""
    n_pos = n_neg = hits = false_triggers = 0
    precision_sum = 0.0
    precision_n = 0
    details = []
    for item in scenarios:
        scenario = item.get("scenario", "")
        expected = {x.upper() for x in item.get("expected", [])}
        results = retrieve(scenario, adrs, triggers, k=k, min_score=min_score)
        result_ids = [r[0] for r in results]

        hit = None
        if expected:
            n_pos += 1
            hit = bool(expected & set(result_ids))
            if hit:
                hits += 1
        else:
            n_neg += 1
            if result_ids:
                false_triggers += 1

        if result_ids:
            relevant = len(set(result_ids) & expected)
            precision_sum += relevant / len(result_ids)
            precision_n += 1

        details.append({
            "scenario": scenario, "expected": sorted(expected),
            "got": result_ids, "hit": hit,
        })

    return {
        "n_scenarios": len(scenarios),
        "n_positive": n_pos,
        "n_negative": n_neg,
        "hit_rate_at_k": (hits / n_pos) if n_pos else None,
        "precision_at_k": (precision_sum / precision_n) if precision_n else None,
        "false_trigger_rate": (false_triggers / n_neg) if n_neg else None,
        "details": details,
    }


def _fmt_pct(x: float | None) -> str:
    return f"{x:.1%}" if x is not None else "n/a"


def main() -> None:
    parser = argparse.ArgumentParser(description="Decision-recall PoC — offline retrieval eval (T-2816)")
    parser.add_argument("--query", help="ad-hoc single-query retrieval (debug), skips eval mode")
    parser.add_argument("--k", type=int, default=PRECISION_CAP)
    parser.add_argument("--recency-k", type=int, default=RECENCY_K)
    parser.add_argument("--min-score", type=float, default=0.0,
                         help="score gate for injection (default 0.0 = loosest possible: any overlap)")
    parser.add_argument("--adr-dir", type=Path, default=ADR_DIR)
    parser.add_argument("--triggers", type=Path, default=TRIGGERS_PATH)
    parser.add_argument("--eval-set", type=Path, default=EVAL_SET_PATH)
    args = parser.parse_args()

    adrs = load_adrs(args.adr_dir)
    triggers = load_triggers(args.triggers)

    if args.query:
        results = retrieve(args.query, adrs, triggers, k=args.k, recency_k=args.recency_k,
                            min_score=args.min_score)
        if not results:
            print(f"(no candidates cleared the score>{args.min_score} gate)")
        for org_id, score in results:
            label = triggers.get(org_id) or adrs.get(org_id, {}).get("title", "")
            print(f"{org_id}\t{score:.3f}\t{label}")
        return

    scenarios = load_eval_set(args.eval_set)
    result = evaluate(scenarios, adrs, triggers, k=args.k, min_score=args.min_score)
    print(f"corpus: {len(adrs)} ADRs loaded, {len(triggers)} in trigger sidecar, "
          f"recency_k={args.recency_k}, min_score={args.min_score}")
    print(f"scenarios: {result['n_scenarios']} "
          f"(positive={result['n_positive']}, negative={result['n_negative']})")
    print(f"hit_rate@{args.k}: {_fmt_pct(result['hit_rate_at_k'])}")
    print(f"precision@{args.k}: {_fmt_pct(result['precision_at_k'])}")
    print(f"false_trigger_rate: {_fmt_pct(result['false_trigger_rate'])}")
    for d in result["details"]:
        if d["hit"] is True:
            mark = "HIT"
        elif d["hit"] is False:
            mark = "MISS"
        elif d["got"]:
            mark = "FALSE-TRIGGER"
        else:
            mark = "OK"
        print(f"  [{mark}] {d['scenario'][:78]!r} expected={d['expected']} got={d['got']}")


if __name__ == "__main__":
    main()
