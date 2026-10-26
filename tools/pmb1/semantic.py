"""``SemanticTierSubstrate``: the ORG-062 tier-2 semantic/relational view as a PMB-1 candidate (T-2518 step S4).

Reuses the episodic tier's (and so the governed store's) write / supersede / forget paths, ``audit`` and
``lineage_store``, and replaces only retrieve with ``tools/memory_tier_semantic.py`` (the production module). It
declares ``over = "episodic"``: the tier-2 gate holds it to non-inferiority vs the episodic tier and to superiority
on A2c / A9 (tools/pmb1/gate.py ``_tier2_criteria``).

``Evidence.ref`` is the unit's source version ``memory_id/version_id``; ``Evidence.support`` names every other
version the unit depends on (a conflict names every conflicting source). ``as_of`` reaches the module as a
STRUCTURED parameter (R7), never as query text. The extractor is recorded in ``describe()`` (``extractor`` and
``extractor_is_pattern_stub``, R6).
"""
from __future__ import annotations

from .episodic import EpisodicTierSubstrate
from .governed import fx
from .substrate import Evidence

import memory_tier_semantic as tier2  # noqa: E402  (tools/ is on sys.path once .governed is imported)


class SemanticTierSubstrate(EpisodicTierSubstrate):
    name = "semantic-tier"
    over = "episodic"
    template_words = tier2.DEFAULT_EXTRACTOR.template_words

    def __init__(self, *a, extractor: tier2.Extractor | None = None, **kw) -> None:
        super().__init__(*a, **kw)
        self.extractor = extractor or tier2.DEFAULT_EXTRACTOR
        self.template_words = tuple(self.extractor.template_words)
        self._as_of: str | None = None

    def _retrieve_memoised(self, query: str, purpose: str, budget_tokens: int) -> list[Evidence]:
        return self._units(query, purpose, budget_tokens, self._as_of)

    def _retrieve_as_of(self, query: str, purpose: str, budget_tokens: int, as_of: str) -> list[Evidence]:
        """The structured as_of (R7) rides on the instance for exactly one _retrieve (its projection rebuild and
        retrievable-set memo are the governed adapter's), then is cleared."""
        self._as_of = as_of
        try:
            return self._retrieve(query, purpose, budget_tokens)
        finally:
            self._as_of = None

    def _units(self, query: str, purpose: str, budget_tokens: int, as_of: str | None) -> list[Evidence]:
        req = fx.retrieval_request(
            request_id="pmb1", purpose=purpose, subject_scope=(), item_budget=0, token_budget=0,
            privacy_ceiling=self.privacy_ceiling,
        )
        units = tier2.retrieve_units(self.store, self._trust, req, query, budget_tokens=budget_tokens,
                                     top_k=self.top_k, as_of=as_of, extractor=self.extractor,
                                     **({} if self.embedder is None else
                                        {"embedder": self.embedder, "tau": self.tau, "sem_k": self.sem_k}))
        self.last_backlinks = [u.backlink for u in units]
        return [Evidence(u.text, f"{u.memory_id}/{u.version_id}", u.use_disposition,
                         support=tuple(f"{m}/{v}" for m, v in u.support)) for u in units]

    def describe(self) -> dict:
        d = super().describe()
        d["selection_method"] = (
            "tier-2 slot view over the episodic tier: authorize the FULL retrievable set first, read only authorized "
            "payloads, extract (entity, predicate, value) claims per sentence; the value of a slot at t (as_of, or "
            "request_time) is the set at its latest valid_from <= t (two or more = a conflict, all surfaced); drop "
            "episodic extracts asserting a value not valid at t, add the valid source sentence VERBATIM for every "
            "touched slot; support = conflicting sources; reading order best LAST"
        )
        return d

    def _extra(self) -> dict:
        return {**super()._extra(), "extractor": self.extractor.name,
                "extractor_is_pattern_stub": bool(self.extractor.is_pattern_stub)}
