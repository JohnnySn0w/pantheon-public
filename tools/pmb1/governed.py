"""``GovernedStoreSubstrate``: the ORG-058 memory store, as-is, behind the
PMB-1 adapter interface. This is the baseline tiers must beat or match.

Writes go through the store's own admission path (``append_version``);
supersede is ``correct``; forget is ``forget`` (REMOVE_DERIVED); retrieval is
``lookup_content`` / ``lookup_subject`` and THEN ``evaluate_retrieval`` for the
purpose, so authorization precedes ranking (INV-6).

Envelope shapes come from the store's own test builders (``make_env`` and
friends), reused rather than reinvented. PMB-1 measures the store; it does not
modify ``memory_store`` / ``memory_lifecycle_ops`` / ``memory_policy_evaluator``
/ ``memory_envelope``.

Two measured facts about the evaluator shape this adapter (see the T-2518
report): ``evaluate_retrieval`` ranks alphabetically by (memory_id, version_id)
and meters ``token_budget`` against ``len(payload_ref)`` (the reference string,
not the content). So the evaluator is used for AUTHORIZATION only; ranking and
the content-token budget are applied here, after it.
"""
from __future__ import annotations

import dataclasses
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

from .substrate import DEFAULT_TOP_K, ROLE_CANDIDATE, Evidence, Item, Substrate
from .tokens import count_tokens, words

_TOOLS = str(Path(__file__).resolve().parent.parent)
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import memory_envelope as me  # noqa: E402
import memory_lifecycle_ops as ops  # noqa: E402
import memory_policy_evaluator as ev  # noqa: E402
import memory_store as ms  # noqa: E402
import test_memory_store as fx  # noqa: E402  (the store's own synthetic builders)

AUTH = "auth:pmb1"
_STOP = frozenset({"what", "is", "s", "the", "a", "an", "of", "tell", "me", "about", "who", "which", "does", "do"})


def _policy() -> "ops.LifecyclePolicy":
    return ops.make_lifecycle_policy(
        "LP-pmb1", fx.LC_REV,
        correction_policies={"corr-std": AUTH},
        forgetting_policies={"forget-std": ops.ForgettingRule(AUTH, frozenset({ops.LifecycleMode.REMOVE_DERIVED}))},
    )


class GovernedStoreSubstrate(Substrate):
    name = "governed-store"
    role = ROLE_CANDIDATE

    def __init__(self, top_k: int = DEFAULT_TOP_K, privacy_ceiling: str = "personal") -> None:
        super().__init__()
        self.top_k = top_k
        self.privacy_ceiling = privacy_ceiling
        self._tmp = tempfile.mkdtemp(prefix="pmb1-store-")
        self.store = ms.init_store(Path(self._tmp) / "store", backup_surfaces=[], media_scrubbed=False)
        self._install_memo()
        self._policy = _policy()
        self._policies = {fx.LC_REV: self._policy}
        self._trust = fx.trust_policy()
        self._n = 0
        self._ops = 0
        self._versions: dict[str, int] = {}
        self._dirty = False
        self._memo: tuple | None = None  # retrievable_envelopes() cache, valid only between mutations
        self._memo_on = False
        self._audit_memo: tuple | None = None  # audit()'s event-log / retrievable-set pass, valid only between mutations
        self.outcomes: Counter = Counter()  # lifecycle outcome / refusal tallies, for the record

    # --- helpers ------------------------------------------------------------
    def _install_memo(self) -> None:
        """``lookup_content`` / ``lookup_subject`` each re-derive the whole
        retrievable set (a full read + digest pass) for their post-filter. This
        memoises that one call on THIS store instance while a retrieve is in
        flight and nothing has mutated, so the store's own lookup path is still
        what runs, ~5x cheaper. Mutating ops (correct / forget) always run
        unmemoised."""
        real = self.store.retrievable_envelopes

        def memoised() -> tuple:
            if not self._memo_on:
                return real()
            if self._memo is None:
                self._memo = real()
            return self._memo

        self.store.retrievable_envelopes = memoised  # type: ignore[method-assign]

    def _mutated(self) -> None:
        self._memo = None
        self._audit_memo = None
        self._dirty = True

    @staticmethod
    def _split(ref: str) -> tuple[str, str]:
        mid, vid = ref.split("/", 1)
        return mid, vid

    def _op(self) -> str:
        self._ops += 1
        return f"op-{self._ops}"

    # --- adapter surface ------------------------------------------------------
    def _write(self, item: Item) -> str:
        self._n += 1
        mid, vid = f"m-{self._n}", "v-1"
        env = fx.make_env(
            mid, vid, item.text, subjects=(item.subject,), privacy_class=item.privacy_class,
            canonical_source_ref=fx.source_ref(object_id=f"{mid}.md"),
            **({} if item.valid_from is None else {"valid_from": item.valid_from}),  # None: the builder's default, as before
        )
        res = self.store.append_version(
            env, item.text, lifecycle_policy=self._policy, trust_policy=self._trust, now=fx.NOW
        )
        if res.refusal is not None:
            raise RuntimeError(f"store refused write: {res.refusal.value} {res.detail}")
        self._versions[mid] = 1
        self._mutated()
        return f"{mid}/{vid}"

    def _supersede(self, ref: str, new_item: Item) -> str:
        mid, vid = self._split(ref)
        prior = self.store.stored_envelope(mid, vid)
        nv = self._versions[mid] + 1
        nvid = f"v-{nv}"
        succ = dataclasses.replace(
            prior, version_id=nvid, supersedes=(vid,), superseded_by=(), subjects=(new_item.subject,),
            payload_ref=f"payload://{mid}/{nvid}", payload_digest=ms.payload_digest(new_item.text),
            **({} if new_item.valid_from is None else {"valid_from": new_item.valid_from}),  # a correctable field
        )
        r = ops.correct(
            self.store, operation_id=self._op(), memory_id=mid, expected_version_id=vid,
            expected_version_digest=self.store.version_digest(mid, vid) or "",
            successor=succ, successor_payload=new_item.text,
            reason=ops.CorrectionReason.CONTRADICTION, actor="pmb1", authority_ref=AUTH,
            lifecycle_policies=self._policies, trust_policy=self._trust, now=fx.NOW,
        )
        self.outcomes[f"correct:{r.refusal.value if r.refusal else r.outcome.value}"] += 1
        if r.refusal is not None:
            raise RuntimeError(f"store refused correction: {r.refusal.value} {r.refusal_detail}")
        self._versions[mid] = nv
        self._mutated()
        return f"{mid}/{nvid}"

    def _forget(self, ref: str) -> None:
        mid, _ = self._split(ref)
        r = ops.forget(
            self.store, operation_id=self._op(), mode=ops.LifecycleMode.REMOVE_DERIVED,
            target_ids=[mid], authority_ref=AUTH, lifecycle_policies=self._policies, now=fx.NOW,
        )
        self.outcomes[f"forget:{r.refusal.value if r.refusal else r.outcome.value}"] += 1
        if r.refusal is not None:
            raise RuntimeError(f"store refused forget: {r.refusal.value} {r.refusal_detail}")
        self._mutated()

    def _retrieve(self, query: str, purpose: str, budget_tokens: int) -> list[Evidence]:
        if self._dirty:
            self.store.rebuild_projections()  # a write-path cost, paid lazily
            self._dirty = False
            self._memo = None
        self._memo_on = True
        try:
            return self._retrieve_memoised(query, purpose, budget_tokens)
        finally:
            self._memo_on = False

    def _retrieve_memoised(self, query: str, purpose: str, budget_tokens: int) -> list[Evidence]:
        score: Counter = Counter()
        for tok in dict.fromkeys(t for t in words(query) if t not in _STOP):
            for hit in self.store.lookup_content(tok):
                score[hit] += 1
            for hit in self.store.lookup_subject(tok):
                score[hit] += 1
        if not score:
            return []
        live = {(e.memory_id, e.version_id): e for e in self.store.retrievable_envelopes()}
        cands = [live[h] for h in score if h in live]
        req = fx.retrieval_request(
            request_id="pmb1", purpose=purpose, subject_scope=(), item_budget=0, token_budget=0,
            privacy_ceiling=self.privacy_ceiling,
        )
        view = ev.evaluate_retrieval(req, cands, self._trust)  # authorization BEFORE ranking
        ranked = sorted(view.items, key=lambda i: (-score[(i.memory_id, i.version_id)], i.memory_id, i.version_id))
        out: list[Evidence] = []
        used = 0
        for it in ranked:
            text = self.store.read_payload(it.memory_id, it.version_id)
            if text is None:
                continue
            n = count_tokens(text)
            if used + n > budget_tokens:
                continue
            out.append(Evidence(text, f"{it.memory_id}/{it.version_id}", it.use_disposition.value))
            used += n
            if len(out) >= self.top_k:
                break
        return out

    def audit(self, ref: str) -> dict | None:
        """C5 lineage (T-2518 t2): ids, times and the payload DIGEST for ``memory_id/version_id``, never payload text.

        ``retrievable`` is the store's own answer (``retrievable_envelopes``: a RETAINED_* state, payload present
        and verified, source not tombstoned). ``retained_at`` is the time of the event that admitted the version.
        ``None`` when the ref is not a version this store ever admitted."""
        try:
            mid, vid = self._split(ref)
        except ValueError:
            return None
        if self._audit_memo is None:  # one pass over the event log and the retrievable set per mutation
            self._memo_on = True
            try:
                live = {(e.memory_id, e.version_id) for e in self.store.retrievable_envelopes()}
            finally:
                self._memo_on = False
            admitted = {(t["memory_id"], t["version_id"]): e["at"] for e in self.store.events()
                        for t in e["transitions"] if t["from"] is None}
            self._audit_memo = (self.store.folded(), live, admitted, {})
        fold, live, admitted, cache = self._audit_memo
        if (mid, vid) not in cache:
            fv = fold.get((mid, vid))
            env = None if fv is None else self.store.stored_envelope(mid, vid)
            cache[(mid, vid)] = None if env is None else {
                "memory_id": mid, "version_id": vid, "state": fv.state.value, "retrievable": (mid, vid) in live,
                "observed_at": env.observed_at, "valid_from": env.valid_from, "valid_to": env.valid_to,
                "retained_at": admitted.get((mid, vid)),
                "payload_digest": env.payload_digest,  # "sha256:<hex>" of the payload: a hash, never the text
            }
        hit = cache[(mid, vid)]
        return None if hit is None else dict(hit)

    def lineage_store(self):
        """The ORG-058 store itself: C5 checks every audited (memory_id, version_id) against it (REBOUND 3)."""
        return self.store

    def _extra(self) -> dict:
        return {"lifecycle_outcomes": dict(sorted(self.outcomes.items()))}

    def close(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
