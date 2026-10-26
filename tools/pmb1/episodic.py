"""``EpisodicTierSubstrate``: the ORG-062 tier-1 episodic view as a PMB-1 candidate (T-2518 step 2d).

Reuses ``GovernedStoreSubstrate``'s write / supersede / forget paths and replaces
only retrieve with ``tools/memory_tier_episodic.py`` (the production module).
It declares ``over = "governed"``, so the gate holds it to non-inferiority vs the
governed store on A1 / A-retrieve and to superiority on E3.

An ``Evidence.ref`` is the version backlink ``memory_id/version_id`` (what the
tracks resolve against); the sentence index lives on the production module's
``Extract`` and is recorded here in ``last_backlinks``.
"""
from __future__ import annotations

from . import governed as _g
from .governed import GovernedStoreSubstrate, fx
from .substrate import ROLE_CANDIDATE, Evidence

import memory_tier_episodic as tier  # noqa: E402  (tools/ is on sys.path once .governed is imported)


class EpisodicTierSubstrate(GovernedStoreSubstrate):
    name = "episodic-tier"
    role = ROLE_CANDIDATE
    over = "governed"

    def __init__(self, top_k: int = _g.DEFAULT_TOP_K, privacy_ceiling: str = "personal", embedder: object | None = None,
                 tau: float | None = None, sem_k: int | None = None) -> None:
        super().__init__(top_k=top_k, privacy_ceiling=privacy_ceiling)
        self.last_backlinks: list[tuple[str, str, int]] = []
        # T-2518 semleg: an optional embedder switches on the tier's semantic leg (None: lexical, unchanged).
        self.embedder = embedder
        self.tau, self.sem_k = tau, sem_k
        if embedder is not None:
            self.embedding_model = embedder.name  # type: ignore[attr-defined]

    def _retrieve_memoised(self, query: str, purpose: str, budget_tokens: int) -> list[Evidence]:
        req = fx.retrieval_request(
            request_id="pmb1", purpose=purpose, subject_scope=(), item_budget=0, token_budget=0,
            privacy_ceiling=self.privacy_ceiling,
        )
        extracts = tier.retrieve_extracts(
            self.store, self._trust, req, query, budget_tokens=budget_tokens, top_k=self.top_k,
            **({} if self.embedder is None else {"embedder": self.embedder, "tau": self.tau, "sem_k": self.sem_k}),
        )
        self.last_backlinks = [x.backlink for x in extracts]
        return [Evidence(x.text, f"{x.memory_id}/{x.version_id}", x.use_disposition) for x in extracts]

    def describe(self) -> dict:
        d = super().describe()
        d["selection_method"] = (
            "query-time sentence extracts over the governed store: authorize (evaluate_retrieval) then rank episodes "
            "by lookup score; every sentence with >= 1 distinct query term is a candidate, filled under a "
            "content-token budget by score, episode rank, source order; returned in reading order (best LAST); "
            "backlink = memory/version + sentence index"
        )
        if self.embedder is not None:
            d["selection_method"] += (
                "; SEMANTIC LEG: authorize the full retrievable set first, embed only authorized sentences, rank "
                "episodes by true top-N RRF (k=60) of the lexical list and the TOP sem_k of the max-sentence-cosine "
                "list, candidates = lexical UNION that top sem_k; a sentence is a candidate on overlap >= 1 OR "
                "cosine >= tau"
            )
            d["semantic_tau"] = tier.DEFAULT_TAU if self.tau is None else self.tau
            d["semantic_sem_k"] = tier.DEFAULT_SEM_K if self.sem_k is None else self.sem_k
        return d
