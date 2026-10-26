#!/usr/bin/env python3
"""Acceptance + guard suite for the ORG-058 correction / supersession /
verified-forgetting operations (T-3198).

The four acceptance clauses of the ticket (A1-A4) run end to end through real
files: a real ``init_store`` directory, real FTS5 + subject projection
rebuilds, and real file-backed sources. There is no mocked store. The guards
(CAS, source-rewrite, transitions, tombstone, deny-by-default policy, expire,
destroy-store, receipts, idempotent repair) each have a named test that goes
RED when the guard is removed.

Evidence labels: citations of Mason / T-2425 use the T-2425 vocabulary
(measured | vendor claim | inference | roadmap). All content is synthetic.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import io
import hashlib
import json
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest
from enum import Enum
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_envelope as me  # noqa: E402
import memory_lifecycle_ops as ops  # noqa: E402
import memory_policy_evaluator as ev  # noqa: E402
import memory_store as ms  # noqa: E402
from memory_lifecycle_ops import LifecycleMode as Mode  # noqa: E402
from memory_lifecycle_ops import LifecycleOutcome as Outcome  # noqa: E402
from memory_lifecycle_ops import LifecycleRefusal as LR  # noqa: E402
from memory_lifecycle_ops import SurfaceKind as Kind  # noqa: E402
from memory_lifecycle_ops import SurfaceStatus as St  # noqa: E402
from test_memory_store import (  # noqa: E402
    LC_REV,
    NOW,
    SRC_OBJECT,
    SRC_SYSTEM,
    SUBJECT,
    THURSDAY,
    TUESDAY,
    make_env,
    retrieval_request,
    source_ref,
    tree_digest,
    trust_policy,
)

AUTH = "auth:team-owner"
ORPHAN_TAG = "orphan (crash residue, never admitted)"
FORGET_ANY = frozenset({Mode.REMOVE_DERIVED, Mode.REQUEST_SOURCE_ERASURE, Mode.EXPIRE})
PURGE_AFTER = "2026-12-01T00:00:00Z"
AFTER_PURGE = "2027-06-01T00:00:00Z"  # well after PURGE_AFTER


def lifecycle_policies(revision: str = LC_REV) -> dict[str, ops.LifecyclePolicy]:
    policy = ops.make_lifecycle_policy(
        "LP-test",
        revision,
        correction_policies={"corr-std": AUTH},
        forgetting_policies={
            "forget-std": ops.ForgettingRule(AUTH, FORGET_ANY),
            "forget-tomb": ops.ForgettingRule(AUTH, FORGET_ANY, tombstone_required=True),
            "expire-only": ops.ForgettingRule(AUTH, frozenset({Mode.EXPIRE})),
            "expire-later": ops.ForgettingRule(
                AUTH, frozenset({Mode.EXPIRE}), expire_purge="scheduled", purge_after=PURGE_AFTER
            ),
            "expire-later-or-remove": ops.ForgettingRule(
                AUTH, frozenset({Mode.EXPIRE, Mode.REMOVE_DERIVED}), expire_purge="scheduled", purge_after=PURGE_AFTER
            ),
            "destroy": ops.ForgettingRule("auth:operator", frozenset({Mode.DESTROY_STORE})),
        },
    )
    return {revision: policy}


def _minted_policy() -> ops.LifecyclePolicy:
    """ADVERSARIAL: other content under the legit revision string, granting an
    intruder authority; its digest recomputes over its own content."""
    return ops.make_lifecycle_policy(
        "LP-test",
        LC_REV,
        correction_policies={"corr-std": "auth:intruder"},
        forgetting_policies={"forget-std": ops.ForgettingRule("auth:intruder", frozenset({Mode.REMOVE_DERIVED}))},
    )


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class LyingSource:
    """ADVERSARIAL, tests only: reads truthfully, but answers every request
    with a verified-looking receipt (bound request_id/object_id, the true
    before-digest) while changing nothing."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def read_digest(self, ref: me.SourceRef) -> str | None:
        path = self.root / ref.object_id
        return file_digest(path) if path.is_file() else None

    def request_correction(self, ref, correction, authority_ref, request_id):
        receipt = ops.SourceReceipt(
            request_id, ref.object_id, self.read_digest(ref), ms.payload_digest(correction), "lying-source"
        )
        return ops.SourceOutcome(St.VERIFIED, receipt, "corrected (not really)")

    def request_erasure(self, ref, authority_ref, request_id):
        receipt = ops.SourceReceipt(request_id, ref.object_id, self.read_digest(ref), None, "lying-source")
        return ops.SourceOutcome(St.VERIFIED, receipt, "erased (not really)")


class MovingRefuser:
    """ADVERSARIAL, tests only: answers every request REFUSED (or
    retained-by-policy) while in fact deleting the object."""

    def __init__(self, root: Path, status=None) -> None:
        self.root, self.status = root, status or St.REFUSED

    def read_digest(self, ref: me.SourceRef) -> str | None:
        path = self.root / ref.object_id
        return file_digest(path) if path.is_file() else None

    def request_erasure(self, ref, authority_ref, request_id):
        (self.root / ref.object_id).unlink()
        return ops.SourceOutcome(self.status, None, "declined (but deleted anyway)")

    def request_correction(self, ref, correction, authority_ref, request_id):
        (self.root / ref.object_id).write_text(correction)
        return ops.SourceOutcome(self.status, None, "declined (but rewrote anyway)")


class _Case(unittest.TestCase):
    """Fresh store + synthetic file-backed source per test."""

    backup_surfaces: list = []
    media_scrubbed = True

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.src_root = self.tmp / "source"
        self.src_root.mkdir()
        self.src_file = self.src_root / SRC_OBJECT
        self.src_file.write_text("synthetic source record: team standup notes\n")
        self.root = self.tmp / "store"
        self.store = ms.init_store(
            self.root, backup_surfaces=self.backup_surfaces, media_scrubbed=self.media_scrubbed
        )
        self.fs = ops.FileSource(self.src_root)
        self.policies = lifecycle_policies()

    # --- helpers
    def live_ref(self, **kw) -> me.SourceRef:
        return source_ref(digest=file_digest(self.src_file), **kw)

    def seed(self, memory_id: str = "m-1", version_id: str = "v-1", payload: str = TUESDAY, **kw) -> me.MemoryEnvelope:
        kw.setdefault("canonical_source_ref", self.live_ref())
        env = make_env(memory_id, version_id, payload, **kw)
        res = self.store.append_version(env, payload, lifecycle_policy=self.policies[LC_REV], trust_policy=trust_policy(), now=NOW)
        self.assertIsNone(res.refusal, res.detail)
        return env

    def successor(self, memory_id: str = "m-1", prior: str = "v-1", version_id: str = "v-2",
                  payload: str = THURSDAY, **kw) -> me.MemoryEnvelope:
        prior_env = self.store.stored_envelope(memory_id, prior)
        assert prior_env is not None
        # The prior with only the per-version fields replaced: every field a
        # correction may NOT change is inherited unchanged by construction.
        fields = dict(
            version_id=version_id, supersedes=(prior,), superseded_by=(),
            payload_ref=f"payload://{memory_id}/{version_id}", payload_digest=ms.payload_digest(payload),
        )
        fields.update(kw)
        return dataclasses.replace(prior_env, **fields)

    def correct(self, *, memory_id: str = "m-1", prior: str = "v-1", version_id: str = "v-2",
                payload: str = THURSDAY, operation_id: str = "corr-1", successor=None, **kw) -> ops.CorrectionReceipt:
        kw.setdefault("expected_version_digest", self.store.version_digest(memory_id, prior) or "sha256:none")
        kw.setdefault("authority_ref", AUTH)
        kw.setdefault("lifecycle_policies", self.policies)
        kw.setdefault("sources", {SRC_SYSTEM: self.fs})
        kw.setdefault("reason", ops.CorrectionReason.CONTRADICTION)
        kw.setdefault("trust_policy", trust_policy())
        return ops.correct(
            self.store,
            operation_id=operation_id,
            memory_id=memory_id,
            expected_version_id=prior,
            successor=successor or self.successor(memory_id, prior, version_id, payload),
            successor_payload=payload,
            actor="test-actor",
            now=NOW,
            **kw,
        )

    def forget(self, mode, *, operation_id: str = "forget-1", targets=("m-1",), **kw) -> ops.ForgetReceipt:
        kw.setdefault("sources", {SRC_SYSTEM: self.fs})
        kw.setdefault("authority_ref", AUTH)
        kw.setdefault("lifecycle_policies", self.policies)
        kw.setdefault("now", NOW)
        return ops.forget(self.store, operation_id=operation_id, mode=mode, target_ids=targets, **kw)

    def surface(self, receipt, name: str) -> ops.SurfaceRecord:
        hits = [s for s in receipt.surfaces if s.surface == name]
        self.assertEqual(len(hits), 1, f"surface {name!r} not enumerated exactly once: {[s.surface for s in receipt.surfaces]}")
        return hits[0]

    def assertValidCorrection(self, r: ops.CorrectionReceipt) -> None:
        d = ops.validate_correction_receipt(r)
        self.assertTrue(d.allowed, d.detail)

    def assertValidForget(self, r: ops.ForgetReceipt) -> None:
        d = ops.validate_forget_receipt(r)
        self.assertTrue(d.allowed, d.detail)

    def assertNoTargetInAnyGeneration(self, store: ms.MemoryStore, memory_id: str, terms=()) -> None:
        for name in ms.PROJECTIONS:
            for g in store.generations_on_disk(name):
                with self.subTest(projection=name, generation=g):
                    self.assertEqual({r for r in store.raw_refs(name, g) if r[0] == memory_id}, set())
                    for term in terms:
                        if name == "content":
                            self.assertEqual(store.raw_lookup_content(term, generation=g), ())


# ===========================================================================
# A1 — corrected versions win (real FTS5 rebuild)
# ===========================================================================
class TestA1CorrectedVersionsWin(_Case):
    def test_a1_corrected_version_wins_through_real_rebuild(self) -> None:
        self.seed(payload=TUESDAY)
        self.store.rebuild_projections()
        self.assertEqual(self.store.lookup_content("tuesday"), (("m-1", "v-1"),))
        v1_bytes = (self.root / "versions" / "m-1" / "v-1.json").read_bytes()

        r = self.correct()
        self.assertValidCorrection(r)
        self.assertIsNone(r.refusal, r.refusal_detail)
        self.assertEqual(r.successor_version_id, "v-2")
        self.assertEqual(r.source_status, "unchanged")
        # Superseded payload + rollback generation are honestly RETAINED.
        self.assertEqual(r.outcome, Outcome.VERIFIED_WITH_RETENTION)

        self.assertEqual(self.store.lookup_content("thursday"), (("m-1", "v-2"),))
        self.assertEqual(self.store.lookup_content("tuesday"), ())
        self.assertEqual(self.store.raw_lookup_content("tuesday"), ())  # CURRENT, unfiltered
        self.assertEqual(self.store.raw_lookup_content("thursday"), (("m-1", "v-2"),))
        self.assertEqual(self.store.lookup_subject(SUBJECT), (("m-1", "v-2"),))
        for p in r.index_purge_receipts:
            with self.subTest(projection=p.projection):
                self.assertTrue(p.negative_lookup_verified, p.evidence)
                self.assertEqual(p.target_refs, ("m-1/v-1",))

        view = ev.evaluate_retrieval(retrieval_request(), self.store.retrievable_envelopes(), trust_policy())
        self.assertEqual([(i.memory_id, i.version_id) for i in view.items], [("m-1", "v-2")])

        folded = self.store.current_envelope("m-1", "v-1")
        assert folded is not None
        self.assertEqual(folded.lifecycle_state, me.LifecycleState.SUPERSEDED)
        self.assertEqual(folded.superseded_by, ("v-2",))
        self.assertEqual((self.root / "versions" / "m-1" / "v-1.json").read_bytes(), v1_bytes)


# ===========================================================================
# A2 — forgotten derived data stays absent
# ===========================================================================
class TestA2ForgottenStaysAbsent(_Case):
    def test_a2_remove_derived_purges_every_version_and_stays_absent(self) -> None:
        self.seed(payload=TUESDAY)
        self.store.rebuild_projections()
        self.assertIsNone(self.correct().refusal)
        payloads = self.root / "payloads" / "m-1"
        self.assertTrue((payloads / "v-1").is_file())  # the SUPERSEDED version still has content
        self.assertTrue((payloads / "v-2").is_file())

        r = self.forget(Mode.REMOVE_DERIVED)
        self.assertValidForget(r)
        self.assertEqual(r.outcome, Outcome.VERIFIED_WITH_RETENTION, r.unverifiable_surfaces)
        self.assertFalse((payloads / "v-1").exists())
        self.assertFalse((payloads / "v-2").exists())
        for vid in ("v-1", "v-2"):
            self.assertEqual(self.surface(r, f"payload:m-1/{vid}").status, St.VERIFIED)
        self.assertEqual(r.derived_payload_disposition, "verified")
        for name in ms.PROJECTIONS:
            self.assertEqual(len(self.store.generations_on_disk(name)), 1)  # no rollback copy left
        self.assertNoTargetInAnyGeneration(self.store, "m-1", ("tuesday", "thursday"))
        self.assertIn("payload_digest", self.surface(r, "envelope-metadata:m-1").evidence)
        # Rollback straight after the forget has no generation to resurrect.
        self.assertEqual(self.store.rollback_projections(), {"content": None, "subject": None})
        self.assertNoTargetInAnyGeneration(self.store, "m-1", ("tuesday", "thursday"))

        # A fresh from-scratch rebuild by a NEW store object on the same root.
        fresh = ms.MemoryStore(self.root)
        fresh.rebuild_projections()
        self.assertNoTargetInAnyGeneration(fresh, "m-1", ("tuesday", "thursday"))
        # Rollback cannot resurrect it.
        fresh.rollback_projections()
        self.assertNoTargetInAnyGeneration(fresh, "m-1", ("tuesday", "thursday"))
        self.assertEqual(fresh.lookup_content("tuesday"), ())
        self.assertEqual(fresh.lookup_subject(SUBJECT), ())

    def test_a2_media_scrub_attestation_is_retained_by_policy_not_verified(self) -> None:
        # ``verified`` is reserved for what the op itself verified; an operator
        # attestation (media_scrubbed: true) is not a scrub this op performed.
        self.seed()
        r = self.forget(Mode.REMOVE_DERIVED)
        rec = self.surface(r, "media-residue")
        self.assertEqual(rec.status, St.RETAINED_BY_POLICY)
        self.assertEqual(rec.evidence, ops.MEDIA_SCRUBBED_EVIDENCE)
        self.assertEqual(
            ops.MEDIA_SCRUBBED_EVIDENCE,
            "operator attests scrub-on-unlink; residue disposition governed by the storage policy; "
            "this op performed no physical scrub",
        )

    def test_a2_view_matches_a_no_storage_control(self) -> None:
        # T-2425 **measured** an "amnesiac floor" for Mason Forget: the bare
        # model scored 47.50 on the forget task and the 4B/9B memory variants
        # scored BELOW it. An answer-level score therefore cannot show that
        # forgetting happened, since a system that never stored the fact passes
        # too. So forget evaluation needs a no-storage control and proof beyond
        # an answer-level score. Here the forgotten store's view must equal a
        # control store in which the memory never existed; the physical proof
        # is in the test above.
        keeper = "synthetic note: the retro is on friday"
        self.seed("m-keep", "v-1", keeper, canonical_source_ref=source_ref(object_id="retro.md"))
        self.seed("m-1", "v-1", TUESDAY)
        self.store.rebuild_projections()
        self.assertIsNone(self.correct().refusal)
        self.assertEqual(self.forget(Mode.REMOVE_DERIVED).outcome, Outcome.VERIFIED_WITH_RETENTION)

        with tempfile.TemporaryDirectory() as d:
            control = ms.init_store(Path(d) / "control", backup_surfaces=[], media_scrubbed=True)
            env = make_env("m-keep", "v-1", keeper, canonical_source_ref=source_ref(object_id="retro.md"))
            self.assertIsNone(control.append_version(env, keeper, lifecycle_policy=self.policies[LC_REV], trust_policy=trust_policy(), now=NOW).refusal)
            control.rebuild_projections()

            def view(store: ms.MemoryStore, request_id: str) -> dict:
                v = ev.evaluate_retrieval(
                    retrieval_request(request_id=request_id), store.retrievable_envelopes(), trust_policy()
                )
                return dataclasses.asdict(dataclasses.replace(v, request_id="", view_id=""))

            forgotten, never = view(self.store, "rq-forgotten"), view(control, "rq-control")
            self.assertEqual(forgotten, never)
            self.assertEqual([i["memory_id"] for i in forgotten["items"]], ["m-keep"])
            self.assertEqual(self.store.raw_lookup_subject(SUBJECT), control.raw_lookup_subject(SUBJECT))
            for term in ("tuesday", "thursday", "standup", "friday"):
                self.assertEqual(self.store.raw_lookup_content(term), control.raw_lookup_content(term))


class TestA2WithoutMediaScrub(_Case):
    media_scrubbed = False

    def test_a2_without_media_scrub_is_incomplete_on_media_residue(self) -> None:
        self.seed(payload=TUESDAY)
        self.store.rebuild_projections()
        self.assertIsNone(self.correct().refusal)
        r = self.forget(Mode.REMOVE_DERIVED)
        self.assertValidForget(r)
        for vid in ("v-1", "v-2"):
            self.assertEqual(self.surface(r, f"payload:m-1/{vid}").status, St.VERIFIED)
        self.assertNoTargetInAnyGeneration(self.store, "m-1", ("tuesday", "thursday"))
        self.assertEqual(self.surface(r, "media-residue").status, St.UNVERIFIABLE)
        self.assertEqual(r.unverifiable_surfaces, ("media-residue",))
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)


# ===========================================================================
# A3 — source records unchanged unless the source returns its own verified receipt
# ===========================================================================
class TestA3SourceUnchangedUnlessVerified(_Case):
    def test_a3_supersede_leaves_source_bytes_unchanged(self) -> None:
        self.seed()
        before = file_digest(self.src_file)  # the test hashes the bytes itself
        r = self.correct()
        self.assertEqual(file_digest(self.src_file), before)
        self.assertEqual(r.source_status, "unchanged")
        self.assertIn(before, self.surface(r, f"source-unchanged:{SRC_SYSTEM}/{SRC_OBJECT}").evidence)

    def test_a3_remove_derived_leaves_source_bytes_unchanged(self) -> None:
        self.seed()
        before = file_digest(self.src_file)
        r = self.forget(Mode.REMOVE_DERIVED)
        self.assertEqual(file_digest(self.src_file), before)
        self.assertEqual(self.surface(r, f"source-unchanged:{SRC_SYSTEM}/{SRC_OBJECT}").status, St.VERIFIED)
        # consistent with correct(): a deliberately untouched, verified-unchanged
        # source reads "unchanged", never "verified" (which would suggest erasure)
        self.assertEqual(r.source_disposition, "unchanged")

    def test_a3_expire_leaves_source_bytes_unchanged(self) -> None:
        self.seed(forgetting_policy="expire-only", retention_until="2026-09-01T00:00:00Z")
        before = file_digest(self.src_file)
        r = self.forget(Mode.EXPIRE)
        self.assertEqual(file_digest(self.src_file), before)
        self.assertEqual(r.source_disposition, "unchanged")

    def test_a3_no_adapter_means_unchanged_is_unverifiable(self) -> None:
        self.seed()
        r = self.correct(sources={})
        self.assertEqual(r.source_status, "unverifiable")
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)

    def test_a3_file_source_erasure_verified_and_source_gone(self) -> None:
        self.seed()
        r = self.forget(Mode.REQUEST_SOURCE_ERASURE)
        self.assertValidForget(r)
        self.assertFalse(self.src_file.exists())
        self.assertEqual(r.source_disposition, "verified")  # an erasure leg is not "unchanged"
        self.assertEqual(self.surface(r, f"source-erasure:{SRC_SYSTEM}/{SRC_OBJECT}").status, St.VERIFIED)
        # Local derived data is untouched by this mode, and says so.
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-1",))
        self.assertEqual(self.surface(r, "payload:m-1/v-1").status, St.RETAINED_BY_POLICY)

    def test_a3_erasure_rerun_of_the_same_operation_converges_on_its_prior_receipt(self) -> None:
        # Review r12c: re-running the SAME operation after a verified erasure
        # finds the object absent; that converges to verified, citing the
        # earlier receipt, instead of the source's "nothing to erase" refusal.
        self.seed()
        name = f"source-erasure:{SRC_SYSTEM}/{SRC_OBJECT}"
        manifest = self.root / ms.STORE_MANIFEST
        declared = manifest.read_bytes()
        manifest.unlink()  # the first attempt ends INCOMPLETE (backups unknown), so it is not a finished op
        first = self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id="erase-1")
        self.assertEqual((first.outcome, self.surface(first, name).status), (Outcome.INCOMPLETE, St.VERIFIED))
        manifest.write_bytes(declared)
        again = self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id="erase-1")
        self.assertValidForget(again)
        rec = self.surface(again, name)
        self.assertEqual(rec.status, St.VERIFIED)
        self.assertEqual(
            rec.evidence,
            "object absent on this op's fresh read; erasure attributed to prior receipt erase-1.json "
            "(integrity-checked, not authenticated: unkeyed digest)",
        )
        self.assertEqual(again.outcome, Outcome.VERIFIED_WITH_RETENTION)
        self.assertEqual([p.name for p in self.store.receipt_paths("erase-1")], ["erase-1.json", "erase-1+attempt-2.json"])
        # Another operation cannot borrow that receipt: the source's answer stands.
        other = self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id="erase-2")
        self.assertEqual(self.surface(other, name).status, St.REFUSED)

    def test_a3_erasure_rerun_never_attributes_to_a_tampered_incomplete_prior_receipt(self) -> None:
        # The #14 convergence cites an earlier receipt only if it validates.
        # (The op journal still proves the one send, so the surface converges
        # either way; what must never happen is citing a tampered receipt.)
        self.seed()
        manifest = self.root / ms.STORE_MANIFEST
        declared = manifest.read_bytes()
        manifest.unlink()
        first = self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id="erase-1")
        self.assertEqual(first.outcome, Outcome.INCOMPLETE)
        manifest.write_bytes(declared)
        path = self.root / "receipts" / "erase-1.json"
        body = json.loads(path.read_text())
        body["authority_ref"] = "auth:someone-else"  # breaks the digest; outcome stays incomplete
        path.write_text(json.dumps(body))
        again = self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id="erase-1")
        rec = self.surface(again, f"source-erasure:{SRC_SYSTEM}/{SRC_OBJECT}")
        self.assertNotIn("attributed to prior receipt", rec.evidence)
        self.assertIn("op journal", rec.evidence)

    def test_a3_erasure_rerun_does_not_trust_a_tampered_prior_receipt(self) -> None:
        # RB3 N1(c): a finished op is re-read and validated before it is
        # returned; a tampered success receipt is refused malformed-input with
        # no side effects (no source request, no new receipt file).
        self.seed()
        self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id="erase-1")
        path = self.root / "receipts" / "erase-1.json"
        body = json.loads(path.read_text())
        body["authority_ref"] = "auth:someone-else"  # breaks the digest
        path.write_text(json.dumps(body))
        self.src_file.write_text("synthetic source record: re-created\n")
        before = tree_digest(self.root, exclude=())
        again = self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id="erase-1")
        self.assertEqual(again.refusal, LR.MALFORMED_INPUT, again.refusal_detail)
        self.assertEqual(tree_digest(self.root, exclude=()), before)
        self.assertTrue(self.src_file.exists())

    def test_a3_source_keys_are_injective_so_colliding_objects_are_both_handled(self) -> None:
        # Review n4: under the old "system/object" string key, (a/b, c) and
        # (a, b/c) were one key, so only one object was requested/verified.
        (self.tmp / "src-a" / "b").mkdir(parents=True)
        (self.tmp / "src-a" / "b" / "c").write_text("object two\n")
        (self.tmp / "src-ab").mkdir()
        (self.tmp / "src-ab" / "c").write_text("object one\n")
        ref_ab = source_ref(system="a/b", object_id="c", digest=file_digest(self.tmp / "src-ab" / "c"))
        ref_a = source_ref(system="a", object_id="b/c", digest=file_digest(self.tmp / "src-a" / "b" / "c"))
        self.assertNotEqual(ops._src_key(ref_ab), ops._src_key(ref_a))  # tuple keys never collide
        env = make_env("m-ab", "v-1", TUESDAY, canonical_source_ref=ref_ab)
        res = self.store.append_version(env, TUESDAY, lifecycle_policy=self.policies[LC_REV], trust_policy=trust_policy(), now=NOW)
        self.assertEqual(res.refusal, LR.MALFORMED_INPUT)  # a system with "/" is refused at admission
        self.seed("m-a", "v-1", TUESDAY, canonical_source_ref=ref_a)
        self.seed("m-x", "v-1", THURSDAY, canonical_source_ref=source_ref(system="ab", object_id="c",
                                                                          digest=file_digest(self.tmp / "src-ab" / "c")))
        r = self.forget(Mode.REQUEST_SOURCE_ERASURE, targets=("m-a", "m-x"),
                        sources={"a": ops.FileSource(self.tmp / "src-a"), "ab": ops.FileSource(self.tmp / "src-ab")})
        names = {s.surface: s.status for s in r.surfaces if s.kind == Kind.SOURCE}
        self.assertEqual(names, {"source-erasure:a/b/c": St.VERIFIED, "source-erasure:ab/c": St.VERIFIED})
        self.assertFalse((self.tmp / "src-a" / "b" / "c").exists())
        self.assertFalse((self.tmp / "src-ab" / "c").exists())

    def test_a3_read_only_canon_refuses_and_bytes_unchanged(self) -> None:
        self.seed()
        canon = ops.ReadOnlyCanonSource(self.src_root)
        before = file_digest(self.src_file)
        r = self.forget(Mode.REQUEST_SOURCE_ERASURE, sources={SRC_SYSTEM: canon})
        self.assertEqual(self.surface(r, f"source-erasure:{SRC_SYSTEM}/{SRC_OBJECT}").status, St.REFUSED)
        self.assertEqual(file_digest(self.src_file), before)
        c = self.correct(sources={SRC_SYSTEM: canon}, source_correction="synthetic corrected source text\n")
        self.assertEqual(c.source_status, "refused")
        self.assertEqual(file_digest(self.src_file), before)
        # The supersession leg is independent of the source leg.
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-2",))

    def test_a3_file_source_correction_is_verified_by_reread(self) -> None:
        self.seed()
        text = "synthetic corrected source text\n"
        r = self.correct(source_correction=text)
        self.assertValidCorrection(r)
        self.assertEqual(self.src_file.read_text(), text)
        self.assertEqual(r.source_status, "verified")


# ===========================================================================
# A4 — unverifiable deletion is never reported as success
# ===========================================================================
class TestA4UnverifiableNeverSuccess(_Case):
    def test_a4_lying_source_erasure_is_unverifiable_incomplete(self) -> None:
        self.seed()
        r = self.forget(Mode.REQUEST_SOURCE_ERASURE, sources={SRC_SYSTEM: LyingSource(self.src_root)})
        self.assertValidForget(r)
        self.assertTrue(self.src_file.exists())
        rec = self.surface(r, f"source-erasure:{SRC_SYSTEM}/{SRC_OBJECT}")
        self.assertEqual(rec.status, St.UNVERIFIABLE)
        self.assertIn("still present", rec.evidence)
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)
        self.assertIn(rec.surface, r.unverifiable_surfaces)

    def test_a4_a_refusal_whose_bytes_moved_is_unverifiable(self) -> None:
        # A source that answers "refused" / "retained-by-policy" is believed
        # only if the op's own re-read shows its bytes did not move.
        for status in (St.REFUSED, St.RETAINED_BY_POLICY):
            with self.subTest(claimed=status.value):
                self.src_file.write_text("synthetic source record: team standup notes\n")
                mid = f"m-{status.value}"
                self.seed(mid, "v-1", TUESDAY)
                r = self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id=f"erase-{status.value}", targets=(mid,),
                                sources={SRC_SYSTEM: MovingRefuser(self.src_root, status)})
                rec = self.surface(r, f"source-erasure:{SRC_SYSTEM}/{SRC_OBJECT}")
                self.assertEqual(rec.status, St.UNVERIFIABLE)
                self.assertIn("digest moved", rec.evidence)
                self.assertEqual(r.outcome, Outcome.INCOMPLETE)

    def test_a4_lying_source_correction_is_unverifiable(self) -> None:
        self.seed()
        r = self.correct(sources={SRC_SYSTEM: LyingSource(self.src_root)}, source_correction="x\n")
        self.assertEqual(r.source_status, "unverifiable")
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)

    def test_a4_missing_manifest_makes_backups_unverifiable(self) -> None:
        (self.root / ms.STORE_MANIFEST).unlink()
        self.seed()
        r = self.forget(Mode.REMOVE_DERIVED)
        self.assertValidForget(r)
        self.assertEqual(self.surface(r, "backup:undeclared").status, St.UNVERIFIABLE)
        self.assertEqual(r.backup_disposition, "unverifiable")
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)

    def test_a_torn_trailing_event_line_is_crash_residue_not_corruption(self) -> None:
        # Review n11: a crash mid-append leaves a torn FINAL line (no newline);
        # one such line made folded() raise for every memory, forever.
        self.seed()
        self.store.rebuild_projections()
        events = self.root / "events.jsonl"
        with open(events, "ab") as fh:
            fh.write(b'{"seq": 3, "kind": "ret')  # torn: no terminating newline
        r = self.forget(Mode.REMOVE_DERIVED)
        self.assertValidForget(r)
        self.assertEqual(self.store.payload_files("m-1"), ())  # the forget itself went through
        rec = self.surface(r, "event-log")
        self.assertEqual(rec.status, St.UNVERIFIABLE)
        self.assertIn("torn trailing line (crash residue)", rec.evidence)
        lines = events.read_bytes().split(b"\n")
        torn = lines.index(b'{"seq": 3, "kind": "ret')  # it stayed on its own line: nothing concatenated
        self.assertIn("torn_predecessor", json.loads(lines[torn + 1]))
        again = self.forget(Mode.REMOVE_DERIVED, operation_id="forget-2")
        self.assertEqual(again.outcome, Outcome.VERIFIED_WITH_RETENTION, again.unverifiable_surfaces)

    def test_a_torn_tail_that_parses_is_void_and_never_resurrects(self) -> None:
        # Review p3 (ii): only the newline was lost, so the torn tail is a
        # COMPLETE record; after the healing append it must not come back.
        self.seed()
        self.store.rebuild_projections()
        lost = {"seq": 2, "kind": "forget-derived", "mode": "remove-derived", "operation_id": "f-lost", "at": NOW,
                "transitions": [{"memory_id": "m-1", "version_id": "v-1", "from": "RETAINED_DERIVED",
                                 "to": "FORGOTTEN_DERIVED"}], "payload_purged": []}
        with open(self.root / "events.jsonl", "ab") as fh:
            fh.write(json.dumps(lost, sort_keys=True, separators=(",", ":")).encode())  # no newline
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-1",))  # void before any append
        r = self.forget(Mode.REMOVE_DERIVED)
        self.assertEqual(self.surface(r, "event-log").status, St.UNVERIFIABLE)
        self.assertNotIn("f-lost", [e["operation_id"] for e in self.store.events()])  # void after the heal too
        self.assertEqual(self.store.folded()[("m-1", "v-1")].state, me.LifecycleState.FORGOTTEN_DERIVED)
        again = self.forget(Mode.REMOVE_DERIVED, operation_id="forget-2")
        self.assertEqual(again.outcome, Outcome.VERIFIED_WITH_RETENTION, again.unverifiable_surfaces)

    def test_a_forget_whose_tombstone_write_tears_is_not_reported_verified(self) -> None:
        # A torn tombstone line means its forget never completed: that op's
        # receipt is not verified, and its re-run rewrites the tombstone.
        self.seed(forgetting_policy="forget-tomb")
        real = ms._append_jsonl

        def tearing(path, record):
            if path.name == "tombstones.jsonl":
                with open(path, "ab") as fh:
                    fh.write(json.dumps(record).encode()[:-5])  # a partial write, then the device fills
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, record)

        with mock.patch.object(ms, "_append_jsonl", tearing):
            r = self.forget(Mode.REMOVE_DERIVED)
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)
        self.assertEqual(self.store.tombstones(), ())  # the torn line is void
        again = self.forget(Mode.REMOVE_DERIVED)
        self.assertEqual(again.outcome, Outcome.VERIFIED_WITH_RETENTION, again.unverifiable_surfaces)
        self.assertEqual([(t["memory_id"], t["object_id"]) for t in self.store.tombstones()], [("m-1", SRC_OBJECT)])

    def test_a_malformed_line_mid_file_is_still_an_integrity_error(self) -> None:
        # Honest-incomplete, unchanged: a TERMINATED unparseable line nobody
        # acknowledged is corruption, not crash residue (no repair tool here).
        self.seed()
        with open(self.root / "events.jsonl", "ab") as fh:
            fh.write(b'{"seq": 3, "kind": "ret\n')
        r = self.forget(Mode.REMOVE_DERIVED)
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)
        ev = self.surface(r, "op-error:remove-derived:preflight").evidence
        # corrupt DATA, not a code defect and not an I/O fault: say which
        self.assertTrue(ev.startswith("store-integrity-error StoreIntegrityError"), ev)
        self.assertNotIn("defect", ev)
        self.assertEqual(self.store.payload_files("m-1"), ("v-1",))

    def test_a4_explicit_empty_backup_declaration_is_enumerated_not_elided(self) -> None:
        # Explicit [] ("none declared") differs from unknown, but it is still a
        # BACKUP surface on the receipt: an attestation, not a verification.
        self.seed()
        r = self.forget(Mode.REMOVE_DERIVED)
        rec = self.surface(r, "backup:none-declared")
        self.assertEqual(rec.status, St.RETAINED_BY_POLICY)
        self.assertIn("verified nothing about backups", rec.evidence)
        self.assertEqual(r.backup_disposition, "retained-by-policy")

    def test_a4_rebuild_raising_mid_forget_is_incomplete_then_rerun_repairs(self) -> None:
        self.seed()
        self.store.rebuild_projections()

        def fault(step: str) -> None:
            if step == "build:content:before-manifest":
                raise RuntimeError("injected rebuild failure")

        r = self.forget(Mode.REMOVE_DERIVED, operation_id="f-1", _fault=fault)
        self.assertValidForget(r)
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)
        held = self.surface(r, "projection:content:gen-1")  # the prior generation is NAMED
        self.assertEqual(held.status, St.UNVERIFIABLE)
        self.assertIn("m-1/v-1", held.evidence)
        self.assertFalse(r.projection_purge_receipts[0].negative_lookup_verified)
        self.assertEqual(self.store.lookup_content("tuesday"), ())  # query path still filters

        again = self.forget(Mode.REMOVE_DERIVED, operation_id="f-1")
        self.assertValidForget(again)
        self.assertEqual(again.outcome, Outcome.VERIFIED_WITH_RETENTION, again.unverifiable_surfaces)
        self.assertNoTargetInAnyGeneration(self.store, "m-1", ("tuesday",))
        paths = self.store.receipt_paths("f-1")
        self.assertEqual([p.name for p in paths], ["f-1.json", "f-1+attempt-2.json"])
        self.assertEqual(json.loads(paths[0].read_text())["outcome"], "incomplete")  # kept for repair

    def test_a4_correction_receipt_flags_a_rebuild_that_still_holds_the_prior(self) -> None:
        # The negative check reads the new CURRENT itself: a projection builder
        # that fails to drop the superseded version must not yield "verified".
        self.seed()
        self.store.rebuild_projections()
        real = ms._BUILDERS["content"]

        def leaky(gen_dir, rows):
            prior_text = self.store.read_payload("m-1", "v-1")
            return real(gen_dir, [*rows, ("m-1", "v-1", prior_text or "", ())])

        with mock.patch.dict(ms._BUILDERS, {"content": leaky}):
            r = self.correct()
        self.assertValidCorrection(r)
        cur = self.store.current_generation("content")
        rec = self.surface(r, f"projection:content:{cur}")
        self.assertEqual(rec.status, St.UNVERIFIABLE)
        self.assertIn("STILL PRESENT", rec.evidence)
        ipr = [p for p in r.index_purge_receipts if p.projection == "content"][0]
        self.assertFalse(ipr.negative_lookup_verified)
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)

    def test_a4_rebuild_raising_mid_correction_is_incomplete(self) -> None:
        self.seed()
        self.store.rebuild_projections()
        with mock.patch.object(self.store, "rebuild_projections", side_effect=RuntimeError("injected")):
            r = self.correct()
        self.assertValidCorrection(r)
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)
        self.assertEqual(self.surface(r, "projection:content:gen-1").status, St.UNVERIFIABLE)
        self.assertFalse(any(p.negative_lookup_verified for p in r.index_purge_receipts))
        # The supersession itself landed; the stale generation is filtered on the query path.
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-2",))
        self.assertEqual(self.store.raw_lookup_content("tuesday"), (("m-1", "v-1"),))
        self.assertEqual(self.store.lookup_content("tuesday"), ())

    def test_a4_hand_edited_verified_receipt_is_denied(self) -> None:
        (self.root / ms.STORE_MANIFEST).unlink()
        self.seed()
        r = self.forget(Mode.REMOVE_DERIVED)
        self.assertTrue(r.unverifiable_surfaces)
        claimed = dataclasses.replace(r, outcome=Outcome.VERIFIED, unverifiable_surfaces=())
        self.assertFalse(ops.validate_forget_receipt(claimed).allowed)
        # Even with the digest recomputed over the lie, the recompute catches it.
        resealed = dataclasses.replace(claimed, receipt_digest=ops.compute_forget_receipt_digest(claimed))
        d = ops.validate_forget_receipt(resealed)
        self.assertFalse(d.allowed)
        self.assertIn("outcome", d.detail)


# ===========================================================================
# Guards
# ===========================================================================
def _race_correct(root: str, src_root: str, successor_vid: str, barrier, results) -> None:
    store = ms.MemoryStore(root)
    prior = store.stored_envelope("m-1", "v-1")
    digest = store.version_digest("m-1", "v-1")
    assert prior is not None and digest is not None
    succ = make_env(
        "m-1", successor_vid, THURSDAY, supersedes=("v-1",), canonical_source_ref=prior.canonical_source_ref
    )
    barrier.wait(timeout=60)  # both processes hold "v-1 is current" before either corrects
    r = ops.correct(
        store, operation_id=f"race-{successor_vid}", memory_id="m-1", expected_version_id="v-1",
        expected_version_digest=digest, successor=succ, successor_payload=THURSDAY, reason="stale",
        actor=f"proc-{successor_vid}", authority_ref=AUTH, lifecycle_policies=lifecycle_policies(),
        trust_policy=trust_policy(), sources={SRC_SYSTEM: ops.FileSource(src_root)}, now=NOW,
    )
    results.put((successor_vid, r.outcome.value, r.refusal.value if r.refusal else None))


class TestCAS(_Case):
    def test_stale_expected_version_is_refused_without_mutation(self) -> None:
        self.seed()
        self.assertIsNone(self.correct().refusal)
        before = tree_digest(self.root)
        r = self.correct(version_id="v-3", operation_id="corr-2", successor=self.successor(version_id="v-3"),
                         expected_version_digest=self.store.version_digest("m-1", "v-1"))
        self.assertEqual(r.refusal, LR.STALE_VERSION)
        self.assertEqual(r.outcome, Outcome.REFUSED)
        self.assertEqual(tree_digest(self.root), before)

    def test_digest_mismatch_is_refused_without_mutation(self) -> None:
        self.seed()
        before = tree_digest(self.root)
        r = self.correct(expected_version_digest="sha256:" + "f" * 64)
        self.assertEqual(r.refusal, LR.STALE_VERSION)
        self.assertEqual(tree_digest(self.root), before)
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-1",))

    def test_concurrent_corrections_exactly_one_wins(self) -> None:
        self.seed()
        ctx = multiprocessing.get_context("spawn")
        barrier, results = ctx.Barrier(2), ctx.Queue()
        procs = [
            ctx.Process(target=_race_correct, args=(str(self.root), str(self.src_root), vid, barrier, results))
            for vid in ("v-2a", "v-2b")
        ]
        for p in procs:
            p.start()
        got = [results.get(timeout=120) for _ in procs]
        for p in procs:
            p.join(timeout=60)
            self.assertEqual(p.exitcode, 0)
        refusals = sorted(x[2] for x in got if x[2] is not None)
        winners = [x for x in got if x[2] is None]
        self.assertEqual(len(winners), 1, got)
        self.assertEqual(refusals, [LR.STALE_VERSION.value], got)
        successors = [v for v in self.store.version_ids("m-1") if v != "v-1"]
        self.assertEqual(successors, [winners[0][0]])
        self.assertEqual(self.store.retrievable_version_ids("m-1"), (winners[0][0],))

    def test_concurrent_corrections_from_two_threads_on_one_store_object(self) -> None:
        # Threads sharing ONE MemoryStore object must serialize on the lock like
        # processes do. The sleep only widens the CAS -> append window.
        self.seed()
        digest = self.store.version_digest("m-1", "v-1")
        real = ops._validate_successor

        def slow(*a, **k):
            time.sleep(0.3)
            return real(*a, **k)

        got: dict[str, tuple[str, str | None]] = {}

        def run(vid: str) -> None:
            r = self.correct(version_id=vid, operation_id=f"race-{vid}", expected_version_digest=digest,
                             successor=self.successor(version_id=vid))
            got[vid] = (r.outcome.value, r.refusal.value if r.refusal else None)

        with mock.patch.object(ops, "_validate_successor", side_effect=slow):
            threads = [threading.Thread(target=run, args=(vid,)) for vid in ("v-2a", "v-2b")]
            threads[0].start()
            time.sleep(0.1)  # the second corrector arrives inside the first's critical section
            threads[1].start()
            for t in threads:
                t.join(60)
        self.assertEqual(sorted(x[1] for x in got.values() if x[1]), [LR.STALE_VERSION.value], got)
        winner = [vid for vid, x in got.items() if x[1] is None]
        self.assertEqual(len(winner), 1, got)
        self.assertEqual([v for v in self.store.version_ids("m-1") if v != "v-1"], winner)
        self.assertEqual(self.store.retrievable_version_ids("m-1"), tuple(winner))


class TestSourceRewriteGuard(_Case):
    def test_successor_claiming_other_source_digest_is_refused_without_mutation(self) -> None:
        self.seed()
        self.store.rebuild_projections()
        before = tree_digest(self.root)
        forged = source_ref(digest="sha256:" + "a" * 64, revision="r2")
        r = self.correct(successor=self.successor(canonical_source_ref=forged))
        self.assertEqual(r.refusal, LR.SOURCE_REWRITE_ATTEMPT)
        self.assertEqual(tree_digest(self.root), before)

    def test_successor_rehoming_the_source_is_refused_identity_change(self) -> None:
        # Review r4: re-homing a memory to another source system or object is
        # not a correction, even when the file-src root happens to hold a file
        # whose digest the successor quotes.
        self.seed()
        other = self.src_root / "ADR-999.md"
        other.write_text("synthetic: an unrelated file in the file-src root\n")
        before = tree_digest(self.root)
        cases = {
            "system": source_ref(system="adr-canon", object_id="ADR-999.md", revision="r-claimed", digest=file_digest(other)),
            "object": source_ref(object_id="ADR-999.md", revision="r-claimed", digest=file_digest(other)),
        }
        for label, ref in cases.items():
            with self.subTest(changed=label):
                r = self.correct(operation_id=f"corr-{label}", successor=self.successor(canonical_source_ref=ref))
                self.assertEqual(r.refusal, LR.SOURCE_IDENTITY_CHANGE, r.refusal_detail)
                self.assertEqual(tree_digest(self.root), before)

    def test_a_source_system_only_change_is_refused_identity_change(self) -> None:
        # object_id, revision and digest identical; only the system moves
        # (review S1: an identity check on object_id alone would pass this).
        self.seed()
        p = self.store.stored_envelope("m-1", "v-1").canonical_source_ref  # type: ignore[union-attr]
        moved = me.SourceRef("other-src", p.object_id, p.source_revision, p.source_digest, p.observed_at, p.verifier)
        before = tree_digest(self.root)
        # correct() must refuse at its OWN guard, before attempting admission
        # (the store refuses too: that second layer is tested in the store suite).
        with mock.patch.object(ms.MemoryStore, "append_version", autospec=True,
                               side_effect=ms.MemoryStore.append_version) as admit:
            r = self.correct(successor=self.successor(canonical_source_ref=moved),
                             sources={SRC_SYSTEM: self.fs, "other-src": self.fs})
        self.assertEqual(r.refusal, LR.SOURCE_IDENTITY_CHANGE, r.refusal_detail)
        admit.assert_not_called()
        self.assertEqual(tree_digest(self.root), before)

    def test_revision_change_with_unchanged_digest_is_a_fabricated_label(self) -> None:
        self.seed()
        before = tree_digest(self.root)
        r = self.correct(successor=self.successor(canonical_source_ref=self.live_ref(revision="r-999-fabricated")))
        self.assertEqual(r.refusal, LR.SOURCE_REWRITE_ATTEMPT, r.refusal_detail)
        self.assertEqual(tree_digest(self.root), before)

    def test_moved_digest_is_verified_only_through_the_adapter_keyed_by_its_system(self) -> None:
        self.seed()
        self.src_file.write_text("synthetic source record: standup moved to thursday\n")  # the source's own edit
        before = tree_digest(self.root)
        successor = self.successor(canonical_source_ref=self.live_ref(revision="r2"))  # the TRUE live digest
        for label, sources in (("none", {}), ("keyed-elsewhere", {"adr-canon": self.fs})):
            with self.subTest(sources=label):
                r = self.correct(operation_id=f"corr-{label}", successor=successor, sources=sources)
                self.assertEqual(r.refusal, LR.SOURCE_REWRITE_ATTEMPT, r.refusal_detail)
                self.assertIn("fail-closed", r.refusal_detail)
                self.assertEqual(tree_digest(self.root), before)

    def test_successor_citing_the_live_new_source_is_accepted(self) -> None:
        self.seed()
        self.src_file.write_text("synthetic source record: standup moved to thursday\n")  # the source's own edit
        r = self.correct(successor=self.successor(canonical_source_ref=self.live_ref(revision="r2")))
        self.assertIsNone(r.refusal, r.refusal_detail)


def grep_tree(root: Path, needle: str) -> list[str]:
    """Every file under ``root`` whose BYTES contain ``needle`` (a physical scan:
    FTS shadow tables, journals, temp files, events, tombstones, receipts)."""
    return [str(p.relative_to(root)) for p in sorted(root.rglob("*")) if p.is_file() and needle.encode() in p.read_bytes()]


class TestReceiptsCarryNoContent(_Case):
    NOTE = "user said: 'the standup moved to tuesday' is wrong, it is thursday"

    def test_reason_note_is_purged_and_no_byte_of_content_survives_anywhere(self) -> None:
        # Review r3/r13: the receipts directory is a store surface. A
        # correction's free text is a purgeable derived payload, never receipt
        # text; after remove-derived NO byte of it survives under the root.
        self.seed(payload=TUESDAY)
        self.store.rebuild_projections()
        c = self.correct(reason=ops.CorrectionReason.CONTRADICTION, reason_note=self.NOTE)
        self.assertIsNone(c.refusal, c.refusal_detail)
        self.assertTrue((self.root / "payloads" / "m-1" / "v-2.reason").is_file())
        self.assertEqual(c.reason_note_ref, "reason-note://m-1/v-2")
        self.assertEqual(self.surface(c, "payload:m-1/v-2.reason").status, St.VERIFIED)
        self.assertEqual(grep_tree(self.root / "receipts", "standup moved to"), [])
        f = self.forget(Mode.REMOVE_DERIVED)
        self.assertValidForget(f)
        self.assertEqual(self.surface(f, "payload:m-1/v-2.reason").status, St.VERIFIED)
        rec = self.surface(f, "receipts")
        self.assertEqual((rec.kind, rec.status), (Kind.RECEIPTS, St.RETAINED_BY_POLICY))
        self.assertIn("receipts carry ids, digests and closed codes only", rec.evidence)
        for needle in (self.NOTE, TUESDAY, THURSDAY, "standup moved to"):
            with self.subTest(needle=needle):
                self.assertEqual(grep_tree(self.root, needle), [])

    def test_the_note_digest_is_salted_so_it_cannot_confirm_a_guess_after_the_purge(self) -> None:
        # A bare sha256(note) on the receipt would outlive the note and confirm
        # a guessed text. The salt lives IN the purgeable note payload.
        self.seed()
        c = self.correct(reason_note=self.NOTE)
        note_file = self.root / "payloads" / "m-1" / "v-2.reason"
        stored = note_file.read_bytes()
        self.assertEqual(stored[16:], self.NOTE.encode())  # 16-byte salt, then the note
        self.assertEqual(c.reason_note_digest, "sha256:" + hashlib.sha256(stored).hexdigest())  # integrity pre-purge
        self.assertNotEqual(c.reason_note_digest, ms.payload_digest(self.NOTE))
        self.forget(Mode.REMOVE_DERIVED)
        self.assertFalse(note_file.exists())  # the salt went with it
        self.assertEqual(grep_tree(self.root, stored[:16].hex()), [])

    def test_a_crashed_correction_retry_keeps_the_salted_note(self) -> None:
        self.seed()
        with mock.patch.object(ms.MemoryStore, "append_event", side_effect=ops.SimulatedCrash("before the event")):
            with self.assertRaises(ops.SimulatedCrash):
                self.correct(reason_note=self.NOTE)
        salted = (self.root / "payloads" / "m-1" / "v-2.reason").read_bytes()
        c = self.correct(reason_note=self.NOTE)  # same op, same note: the orphan completes
        self.assertIsNone(c.refusal, c.refusal_detail)
        self.assertEqual((self.root / "payloads" / "m-1" / "v-2.reason").read_bytes(), salted)
        self.assertEqual(c.reason_note_digest, "sha256:" + hashlib.sha256(salted).hexdigest())

    def test_expire_now_purges_reason_notes_too(self) -> None:
        self.seed(payload=TUESDAY, forgetting_policy="expire-only", retention_until="2026-09-01T00:00:00Z")
        self.assertIsNone(self.correct(reason=ops.CorrectionReason.STALE, reason_note=self.NOTE).refusal)
        f = self.forget(Mode.EXPIRE)
        self.assertValidForget(f)
        self.assertEqual(self.surface(f, "payload:m-1/v-2.reason").status, St.VERIFIED)
        self.assertEqual(grep_tree(self.root, "standup moved to"), [])

    def test_free_text_reason_is_refused_and_never_echoed(self) -> None:
        self.seed()
        before = tree_digest(self.root)
        r = self.correct(reason=self.NOTE)
        self.assertEqual(r.refusal, LR.MALFORMED_INPUT)
        self.assertIsNone(r.reason)
        self.assertEqual(tree_digest(self.root), before)
        self.assertEqual(grep_tree(self.root / "receipts", "is wrong, it is thursday"), [])  # never echoed


# The ruled allowlist (RB2 ruling 3) MINUS the four fields VERIFY-FIRST moved to
# the identical side because memory_policy_evaluator uses them to GRANT/WIDEN
# access: subjects (L629 subject-scope authorization), source_integrity +
# claim_trust (L154-165: asserted labels / chosen evidence feed the floor) and
# effective_trust_floor (L646 purpose disposition).
EXPECTED_CORRECTABLE = frozenset({
    "version_id", "created_at", "observed_at", "valid_from", "valid_to", "confidence_kind", "confidence",
    "supporting_refs", "contradicting_refs", "supersedes", "superseded_by", "last_consolidated_at",
    "payload_ref", "payload_digest", "inherited_trust_floor", "trust_adjudication_receipt_refs",
})
# Correctable only under their own rules (tested elsewhere): the source guard
# and the retained-kind rule. memory_id is refused earlier, as malformed input.
SPECIAL_RULE_FIELDS = frozenset({"canonical_source_ref", "lifecycle_state", "memory_id"})


def _changed(value):
    """A different value of the same shape, for the fail-closed field sweep."""
    if isinstance(value, Enum):
        return next(m for m in type(value) if m != value)
    if isinstance(value, float):
        return value / 2
    if isinstance(value, str):
        return value + "-changed"
    if value is None:
        return "2999-01-01T00:00:00Z"
    if isinstance(value, tuple) and value and dataclasses.is_dataclass(value[0]):
        return value[:-1] if len(value) > 1 else (dataclasses.replace(value[0], **{dataclasses.fields(value[0])[0].name: "changed"}),)
    if isinstance(value, tuple):
        return (*value, "changed")
    raise AssertionError(f"no mutation for {type(value).__name__}: extend _changed")


class TestCorrectableAllowlist(_Case):
    def test_every_field_off_the_allowlist_is_refused_fail_closed(self) -> None:
        # Structural (P1): iterate the dataclass, so a field added to
        # MemoryEnvelope later is refused until someone allowlists it.
        self.seed(privacy_class="personal")
        prior = self.store.stored_envelope("m-1", "v-1")
        assert prior is not None
        before = tree_digest(self.root)
        identical = [f.name for f in dataclasses.fields(me.MemoryEnvelope)
                     if f.name not in EXPECTED_CORRECTABLE | SPECIAL_RULE_FIELDS]
        self.assertTrue({"privacy_class", "memory_class", "audience", "action_authority_ref", "subjects",
                         "source_integrity", "claim_trust", "effective_trust_floor"} <= set(identical))
        for name in identical:
            with self.subTest(field=name):
                changed = _changed(getattr(prior, name))
                r = self.correct(operation_id=f"corr-{name}", successor=self.successor(**{name: changed}))
                self.assertEqual(r.refusal, LR.POLICY_FIELD_CHANGE, f"{name}: {r.refusal_detail}")
                self.assertIn(name, r.refusal_detail)
                self.assertEqual(tree_digest(self.root), before)
        self.assertEqual(ops.CORRECTABLE_FIELDS, EXPECTED_CORRECTABLE)

    def test_an_allowlisted_field_may_be_corrected(self) -> None:
        # n1 control: valid_to is a correction, not a policy change.
        self.seed()
        r = self.correct(successor=self.successor(valid_to="2026-09-02T00:00:00Z", confidence=0.5))
        self.assertIsNone(r.refusal, r.refusal_detail)


CANARY = "zqxj-canary-diary-line"


class ChattySource(ops.FileSource):
    """ADVERSARIAL, tests only: a source whose free text (detail, issued_by)
    quotes content (review n2 F). ``fake_digest`` makes read_digest return
    content instead of a digest."""

    fake_digest = False

    def read_digest(self, ref):
        return f"{CANARY} as a digest" if self.fake_digest else super().read_digest(ref)

    def request_correction(self, ref, correction, authority_ref, request_id):
        out = super().request_correction(ref, correction, authority_ref, request_id)
        receipt = dataclasses.replace(out.source_receipt, issued_by=f"{CANARY} issuer") if out.source_receipt else None
        return ops.SourceOutcome(out.status, receipt, f"corrected: object says '{CANARY}'")

    def request_erasure(self, ref, authority_ref, request_id):
        return ops.SourceOutcome(St.REFUSED, None, f"refused: object says '{CANARY}'")


class TestAdapterTextNeverReachesReceipts(_Case):
    def test_adapter_detail_issuer_and_fake_digests_never_reach_a_receipt(self) -> None:
        self.seed()
        chatty = ChattySource(self.src_root)
        c = self.correct(sources={SRC_SYSTEM: chatty}, source_correction="synthetic corrected text\n")
        self.assertEqual(c.source_status, "verified")  # still verified: the text is only quoted, not trusted
        rec = self.surface(c, f"source-correction:{SRC_SYSTEM}/{SRC_OBJECT}")
        self.assertIn("issued_by sha256:", rec.evidence)
        self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id="erase-1", sources={SRC_SYSTEM: chatty})
        chatty.fake_digest = True
        f = self.forget(Mode.REMOVE_DERIVED, sources={SRC_SYSTEM: chatty})
        self.assertEqual(self.surface(f, f"source-unchanged:{SRC_SYSTEM}/{SRC_OBJECT}").status, St.UNVERIFIABLE)
        self.assertEqual(grep_tree(self.root, CANARY), [])

    def test_no_canary_in_any_raw_hex_or_digest_form_reaches_a_receipt(self) -> None:
        # Review RB2 suspicions: hex-encoded smuggling through a "digest", an
        # unsalted sha256 of a short detail (an oracle), and a canary-named
        # exception class. None may reach a receipt in any form.
        canary_exc = type(f"{CANARY} exc", (Exception,), {})
        hex_canary = "sha256:" + CANARY.encode().hex()  # hex-shaped, but not 64 hex

        class Smuggler(ops.FileSource):
            mode = "hex"

            def read_digest(self, ref):
                if self.mode == "raise":
                    raise canary_exc("x")
                return hex_canary if self.mode == "hex" else super().read_digest(ref)

            def request_erasure(self, ref, authority_ref, request_id):
                if self.mode == "request-raise":
                    raise canary_exc("x")
                return ops.SourceOutcome(St.REFUSED, None, CANARY)

        self.seed()
        smuggler = Smuggler(self.src_root)
        for i, mode in enumerate(("hex", "raise", "real", "request-raise")):
            smuggler.mode = mode
            self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id=f"erase-{i}", sources={SRC_SYSTEM: smuggler})
        with mock.patch.object(ops, "_verify_local", side_effect=canary_exc("x")), contextlib.redirect_stderr(io.StringIO()) as err:
            self.forget(Mode.REMOVE_DERIVED, operation_id="f-defect", sources={SRC_SYSTEM: smuggler})
        blob = b"".join(p.read_bytes() for p in (self.root / "receipts").iterdir())
        forms = {"raw": CANARY.encode(), "hex": CANARY.encode().hex().encode(),
                 "sha256": hashlib.sha256(CANARY.encode()).hexdigest().encode()}
        for label, needle in forms.items():
            with self.subTest(form=label):
                self.assertNotIn(needle, blob)
                self.assertNotIn(needle, err.getvalue().encode())
        self.assertIn(b"non-digest (len ", blob)

    def test_a_well_formed_issuer_is_kept_verbatim(self) -> None:
        self.seed()
        c = self.correct(source_correction="synthetic corrected text\n")
        self.assertIn("by file-source", self.surface(c, f"source-correction:{SRC_SYSTEM}/{SRC_OBJECT}").evidence)


class TestPolicyFieldsFixedAcrossCorrection(_Case):
    def test_successor_changing_a_policy_bearing_field_is_refused(self) -> None:
        # Review r1: a holder of CORRECTION authority must not be able to move
        # the memory out from under the FORGETTING authority. Changing policy
        # is not a correction.
        cases = {
            "policy_revision": dict(policy_revision="lc-rev-DOES-NOT-EXIST"),
            "correction_policy": dict(correction_policy="corr-other"),
            "forgetting_policy": dict(forgetting_policy="expire-only"),
            "retrieval_authority": dict(retrieval_authority="everyone"),
            "owner": dict(owner="someone-else"),
            "retained-kind": dict(lifecycle_state=me.LifecycleState.RETAINED_SOURCE_REF),
            "retention_until": dict(retention_until="2999-01-01T00:00:00Z"),
        }
        self.seed(retention_until="2026-09-01T00:00:00Z")
        before = tree_digest(self.root)
        for field_name, kw in cases.items():
            with self.subTest(field=field_name):
                r = self.correct(operation_id=f"corr-{field_name}", successor=self.successor(**kw))
                self.assertEqual(r.refusal, LR.POLICY_FIELD_CHANGE, r.refusal_detail)
                self.assertIn(field_name.split("-")[-1], r.refusal_detail)
                self.assertEqual(tree_digest(self.root), before)
        self.assertIsNone(self.correct(operation_id="corr-ok").refusal)  # control: same fields pass


class TestTransitions(_Case):
    def test_remove_derived_on_a_source_ref_memory_forgets_and_purges(self) -> None:
        # Review r11: a source-ref memory had no exit before retention_until,
        # and kept serving an erased source's content. T-4275: a subject-
        # initiated forget of a source-ref memory (a derived artifact: a
        # reference, not the source) ends in FORGOTTEN_DERIVED, the state that
        # says what happened; DECAYED is "expired by policy" and is only
        # EXPIRE's. The §5 map carries the RETAINED_SOURCE_REF ->
        # FORGOTTEN_DERIVED edge.
        self.seed(lifecycle_state=me.LifecycleState.RETAINED_SOURCE_REF)
        self.store.rebuild_projections()
        self.assertEqual(self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id="erase").outcome, Outcome.VERIFIED_WITH_RETENTION)
        self.assertEqual(self.store.lookup_content("tuesday"), (("m-1", "v-1"),))  # erasure leaves local data
        r = self.forget(Mode.REMOVE_DERIVED)
        self.assertIsNone(r.refusal, r.refusal_detail)
        self.assertValidForget(r)
        cur = self.store.current_envelope("m-1", "v-1")
        assert cur is not None
        self.assertEqual(cur.lifecycle_state, me.LifecycleState.FORGOTTEN_DERIVED)
        self.assertEqual(self.store.payload_files("m-1"), ())
        self.assertEqual(self.store.retrievable_envelopes(), ())
        self.assertNoTargetInAnyGeneration(self.store, "m-1", ("tuesday",))
        forgot = [e for e in self.store.events() if e["operation_id"] == "forget-1" and e["transitions"]]
        self.assertEqual(
            [(e["mode"], [t["to"] for t in e["transitions"]]) for e in forgot], [("remove-derived", ["FORGOTTEN_DERIVED"])]
        )
        for rec in r.surfaces:  # the old "DECAYED used because ..." workaround note is gone everywhere
            self.assertNotIn("DECAYED used because", rec.evidence, rec.surface)

    def test_a_second_remove_derived_on_a_forgotten_source_ref_purges_without_a_transition(self) -> None:
        # T-4275: the FORGOTTEN_DERIVED source-ref head is terminal, so a new
        # op on it takes the _PURGE_ONLY_STATES path (a payload-purged event,
        # no transition), exactly as for a forgotten derived memory. The first
        # op's purge faults AFTER its transition, so the payload is still there
        # for the second op to purge (a re-run repairs an incomplete forget).
        self.seed(lifecycle_state=me.LifecycleState.RETAINED_SOURCE_REF)
        self.store.rebuild_projections()
        fault = PermissionError(errno.EACCES, "denied")
        with mock.patch.object(ms.MemoryStore, "purge_payload_file", side_effect=fault):
            first = self.forget(Mode.REMOVE_DERIVED, operation_id="rd-1")
        self.assertEqual(first.outcome, Outcome.INCOMPLETE)
        head = self.store.current_envelope("m-1", "v-1")
        assert head is not None
        self.assertEqual(head.lifecycle_state, me.LifecycleState.FORGOTTEN_DERIVED)
        self.assertEqual(self.store.payload_files("m-1"), ("v-1",))  # the faulted purge left it
        n_events = len(self.store.events())
        r = self.forget(Mode.REMOVE_DERIVED, operation_id="rd-2")
        self.assertIsNone(r.refusal, r.refusal_detail)
        self.assertValidForget(r)
        self.assertEqual(self.store.payload_files("m-1"), ())
        cur = self.store.current_envelope("m-1", "v-1")
        assert cur is not None
        self.assertEqual(cur.lifecycle_state, me.LifecycleState.FORGOTTEN_DERIVED)  # terminal: stays
        new = self.store.events()[n_events:]
        self.assertEqual([(e["kind"], e["transitions"]) for e in new], [("payload-purged", [])])
        self.assertEqual(r.outcome, Outcome.VERIFIED_WITH_RETENTION, r.unverifiable_surfaces)

    def test_expire_on_a_source_ref_memory_still_decays(self) -> None:
        # T-4275: EXPIRE is policy expiry, which IS what DECAYED means; only the
        # subject-initiated forget moved to FORGOTTEN_DERIVED.
        self.seed(lifecycle_state=me.LifecycleState.RETAINED_SOURCE_REF,
                  forgetting_policy="expire-only", retention_until="2026-09-01T00:00:00Z")
        self.store.rebuild_projections()
        r = self.forget(Mode.EXPIRE)
        self.assertIsNone(r.refusal, r.refusal_detail)
        self.assertValidForget(r)
        cur = self.store.current_envelope("m-1", "v-1")
        assert cur is not None
        self.assertEqual(cur.lifecycle_state, me.LifecycleState.DECAYED)
        expired = [e for e in self.store.events() if e["operation_id"] == "forget-1" and e["transitions"]]
        self.assertEqual([(e["mode"], [t["to"] for t in e["transitions"]]) for e in expired], [("expire", ["DECAYED"])])
        self.assertEqual(self.store.payload_files("m-1"), ())
        self.assertEqual(self.store.retrievable_envelopes(), ())

    def test_refused_target_does_not_block_the_others(self) -> None:
        self.seed("m-ref", "v-1", TUESDAY, forgetting_policy="expire-only",
                  canonical_source_ref=source_ref(object_id="ref.md"))
        self.seed("m-1", "v-1", THURSDAY)
        r = self.forget(Mode.REMOVE_DERIVED, targets=("m-ref", "m-1"))
        self.assertIsNone(r.refusal)
        self.assertEqual(self.surface(r, "target:m-ref").status, St.REFUSED)
        self.assertEqual(self.surface(r, "payload:m-1/v-1").status, St.VERIFIED)
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)
        self.assertEqual(r.derived_payload_disposition, "mixed")

    def test_correcting_a_superseded_version_is_refused(self) -> None:
        self.seed()
        self.assertIsNone(self.correct().refusal)
        before = tree_digest(self.root)
        r = self.correct(version_id="v-3", operation_id="corr-2", successor=self.successor(version_id="v-3"),
                         expected_version_digest=self.store.version_digest("m-1", "v-1"))
        self.assertEqual(r.outcome, Outcome.REFUSED)
        self.assertEqual(tree_digest(self.root), before)

    def test_successor_that_is_not_retained_is_illegal(self) -> None:
        self.seed()
        r = self.correct(successor=self.successor(lifecycle_state=me.LifecycleState.SUPERSEDED))
        self.assertEqual(r.refusal, LR.ILLEGAL_TRANSITION)


class TestTombstone(_Case):
    def test_tombstone_blocks_reingestion_of_the_same_source_object(self) -> None:
        self.seed(forgetting_policy="forget-tomb")
        r = self.forget(Mode.REMOVE_DERIVED)
        self.assertEqual(self.surface(r, "tombstone:m-1").status, St.RETAINED_BY_POLICY)
        env = make_env("m-2", "v-1", TUESDAY, canonical_source_ref=self.live_ref())
        res = self.store.append_version(env, TUESDAY, lifecycle_policy=self.policies[LC_REV], trust_policy=trust_policy(), now=NOW)
        self.assertEqual(res.refusal, LR.TOMBSTONED)
        self.assertEqual(self.store.version_ids("m-2"), ())

    def test_tombstone_blocks_new_memories_not_successors_of_a_live_one(self) -> None:
        # Review r17: forgetting m-1 must not make an unrelated, still-live m-2
        # that cites the same source object uncorrectable.
        self.seed("m-1", "v-1", TUESDAY, forgetting_policy="forget-tomb")
        self.seed("m-2", "v-1", "synthetic note: attendance was five people")
        self.forget(Mode.REMOVE_DERIVED)
        r = self.correct(memory_id="m-2", payload="synthetic note: attendance was six people", operation_id="corr-m2")
        self.assertIsNone(r.refusal, r.refusal_detail)
        self.assertEqual(self.store.retrievable_version_ids("m-2"), ("v-2",))
        env = make_env("m-3", "v-1", TUESDAY, canonical_source_ref=self.live_ref())
        res = self.store.append_version(env, TUESDAY, lifecycle_policy=self.policies[LC_REV], trust_policy=trust_policy(), now=NOW)
        self.assertEqual(res.refusal, LR.TOMBSTONED)  # a NEW memory is still blocked

    def test_tombstone_cannot_be_bypassed_by_an_alias_object_id(self) -> None:
        # Review r2: FileSource resolves './x', 'x/' and 'x' to the same file.
        self.seed(forgetting_policy="forget-tomb")
        self.forget(Mode.REMOVE_DERIVED)
        for i, alias in enumerate((SRC_OBJECT, "./" + SRC_OBJECT, SRC_OBJECT + "/", "././" + SRC_OBJECT)):
            with self.subTest(object_id=alias):
                ref = self.live_ref(object_id=alias)
                self.assertEqual(self.fs.read_digest(ref), file_digest(self.src_file))  # same object
                env = make_env(f"m-re-{i}", "v-1", TUESDAY, canonical_source_ref=ref)
                res = self.store.append_version(env, TUESDAY, lifecycle_policy=self.policies[LC_REV], trust_policy=trust_policy(), now=NOW)
                self.assertIn(res.refusal, (LR.TOMBSTONED, LR.MALFORMED_INPUT))
                self.assertEqual(self.store.version_ids(f"m-re-{i}"), ())
        self.store.rebuild_projections()
        self.assertEqual(self.store.raw_lookup_content("tuesday"), ())


class TestPolicyDenyByDefault(_Case):
    def _forget_zero_mutation(self, expected: LR, **kw) -> ops.ForgetReceipt:
        before = tree_digest(self.root)
        r = self.forget(Mode.REMOVE_DERIVED, **kw)
        self.assertEqual(r.refusal, expected)
        self.assertEqual(r.outcome, Outcome.REFUSED)
        self.assertEqual(tree_digest(self.root), before)  # only receipts/ may change
        self.assertTrue(self.store.receipt_paths(r.operation_id))
        return r

    def test_forget_unknown_revision_refused(self) -> None:
        self.seed()
        # A registry that knows OTHER revisions must not lend one to this memory.
        self._forget_zero_mutation(
            LR.UNKNOWN_LIFECYCLE_POLICY, lifecycle_policies=lifecycle_policies("lc-rev-other")
        )
        self._forget_zero_mutation(LR.UNKNOWN_LIFECYCLE_POLICY, operation_id="forget-2", lifecycle_policies={})

    def test_forget_unknown_policy_name_refused(self) -> None:
        self.seed(forgetting_policy="no-such-policy")
        self._forget_zero_mutation(LR.UNKNOWN_LIFECYCLE_POLICY)

    def test_forget_mode_not_allowed_refused(self) -> None:
        self.seed(forgetting_policy="expire-only")
        self._forget_zero_mutation(LR.MODE_NOT_ALLOWED)

    def test_forget_wrong_authority_refused(self) -> None:
        self.seed()
        self._forget_zero_mutation(LR.AUTHORITY_MISMATCH, authority_ref="auth:intruder")

    def test_forget_alias_or_tampered_policy_refused(self) -> None:
        self.seed()
        policy = self.policies[LC_REV]
        self._forget_zero_mutation(LR.UNKNOWN_LIFECYCLE_POLICY, lifecycle_policies={LC_REV: dataclasses.replace(policy, revision="lc-rev-2")})
        tampered = dataclasses.replace(policy, correction_policies={"corr-std": "auth:anyone"})
        self._forget_zero_mutation(LR.UNKNOWN_LIFECYCLE_POLICY, operation_id="forget-2", lifecycle_policies={LC_REV: tampered})

    def test_forget_under_a_same_revision_minted_policy_is_refused(self) -> None:
        # Review r8: a caller mints DIFFERENT content under the SAME revision
        # string, with a digest that recomputes over its own content. The
        # revision string is not an identity; the digest pinned at admission is.
        self.seed()
        minted = _minted_policy()
        self.assertIsNone(ops.resolve_lifecycle_policy(LC_REV, {LC_REV: minted})[1])  # self-consistent
        self.assertNotEqual(minted.digest, self.policies[LC_REV].digest)
        self._forget_zero_mutation(
            LR.UNKNOWN_LIFECYCLE_POLICY, authority_ref="auth:intruder", lifecycle_policies={LC_REV: minted}
        )
        self.assertTrue(self.store.payload_files("m-1"))

    def test_receipts_record_the_pinned_policy_identity(self) -> None:
        self.seed()
        pin = ops.PolicyPin(f"LP-test@{LC_REV}", self.policies[LC_REV].digest)
        c = self.correct()
        f = self.forget(Mode.REMOVE_DERIVED)
        self.assertEqual(c.lifecycle_policy_pins, (pin,))
        self.assertEqual(f.lifecycle_policy_pins, (pin,))
        self.assertEqual(self.store.policy_pin("m-1", "v-2"), pin)  # the successor carries the prior's pin
        stripped = dataclasses.replace(f, lifecycle_policy_pins=())
        stripped = dataclasses.replace(stripped, receipt_digest=ops.compute_forget_receipt_digest(stripped))
        self.assertFalse(ops.validate_forget_receipt(stripped).allowed)

    def test_correct_under_a_same_revision_minted_policy_is_refused(self) -> None:
        self.seed()
        before = tree_digest(self.root)
        r = self.correct(authority_ref="auth:intruder", lifecycle_policies={LC_REV: _minted_policy()})
        self.assertEqual(r.refusal, LR.UNKNOWN_LIFECYCLE_POLICY)
        self.assertEqual(tree_digest(self.root), before)

    def test_correct_policy_refusals_have_zero_mutation(self) -> None:
        self.seed()
        cases = [
            ("unknown-revision", LR.UNKNOWN_LIFECYCLE_POLICY, {"lifecycle_policies": lifecycle_policies("lc-rev-other")}),
            ("empty-registry", LR.UNKNOWN_LIFECYCLE_POLICY, {"lifecycle_policies": {}}),
            ("wrong-authority", LR.AUTHORITY_MISMATCH, {"authority_ref": "auth:intruder"}),
        ]
        for label, code, kw in cases:
            with self.subTest(case=label):
                before = tree_digest(self.root)
                r = self.correct(operation_id=f"corr-{label}", **kw)
                self.assertEqual(r.refusal, code)
                self.assertEqual(tree_digest(self.root), before)

    def test_correct_unknown_correction_policy_refused(self) -> None:
        self.seed(correction_policy="no-such-correction")
        before = tree_digest(self.root)
        r = self.correct()
        self.assertEqual(r.refusal, LR.UNKNOWN_LIFECYCLE_POLICY)
        self.assertEqual(tree_digest(self.root), before)

    def test_mode_ownership(self) -> None:
        self.seed()
        for mode in (Mode.SUPERSEDE, Mode.REQUEST_SOURCE_CORRECTION):
            with self.subTest(mode=mode.value):
                self.assertEqual(self.forget(mode, operation_id=f"own-{mode.value}").refusal, LR.MODE_NOT_OWNED)
        self.assertEqual(self.forget("obliterate", operation_id="own-bad").refusal, LR.MALFORMED_INPUT)


class TestExpire(_Case):
    def test_expire_before_retention_until_is_refused(self) -> None:
        self.seed(forgetting_policy="expire-only", retention_until="2026-12-31T00:00:00Z")
        before = tree_digest(self.root)
        r = self.forget(Mode.EXPIRE)
        self.assertEqual(r.refusal, LR.RETENTION_NOT_ELAPSED)
        self.assertEqual(tree_digest(self.root), before)

    def test_expire_without_retention_until_is_refused(self) -> None:
        self.seed(forgetting_policy="expire-only")
        self.assertEqual(self.forget(Mode.EXPIRE).refusal, LR.RETENTION_NOT_ELAPSED)

    def test_expire_purge_now_verifies_payload_gone(self) -> None:
        self.seed(forgetting_policy="expire-only", retention_until="2026-09-01T00:00:00Z")
        self.store.rebuild_projections()
        r = self.forget(Mode.EXPIRE)
        self.assertValidForget(r)
        self.assertEqual(self.surface(r, "payload:m-1/v-1").status, St.VERIFIED)
        self.assertFalse((self.root / "payloads" / "m-1" / "v-1").exists())
        cur = self.store.current_envelope("m-1", "v-1")
        assert cur is not None
        self.assertEqual(cur.lifecycle_state, me.LifecycleState.DECAYED)
        self.assertEqual(self.store.retrievable_envelopes(), ())
        self.assertEqual(r.outcome, Outcome.VERIFIED_WITH_RETENTION, r.unverifiable_surfaces)

    def test_expire_scheduled_retains_payload_by_policy(self) -> None:
        self.seed(forgetting_policy="expire-later", retention_until="2026-09-01T00:00:00Z")
        self.store.rebuild_projections()
        r = self.forget(Mode.EXPIRE)
        self.assertValidForget(r)
        rec = self.surface(r, "payload:m-1/v-1")
        self.assertEqual(rec.status, St.RETAINED_BY_POLICY)
        self.assertIn(PURGE_AFTER, rec.evidence)
        self.assertTrue((self.root / "payloads" / "m-1" / "v-1").exists())
        self.assertEqual(self.store.retrievable_envelopes(), ())  # stops being retrievable NOW
        self.assertEqual(self.store.lookup_content("tuesday"), ())


class TestScheduledPurge(_Case):
    """Review r16: a scheduled purge must actually run once it is due."""

    def test_expire_after_purge_after_executes_the_scheduled_purge(self) -> None:
        self.seed(forgetting_policy="expire-later", retention_until="2026-09-01T00:00:00Z")
        self.store.rebuild_projections()
        self.assertIsNone(self.correct(reason_note="synthetic note: moved again").refusal)
        first = self.forget(Mode.EXPIRE, operation_id="exp-1")
        self.assertEqual(self.surface(first, "payload:m-1/v-2").status, St.RETAINED_BY_POLICY)
        self.assertEqual(set(self.store.payload_files("m-1")), {"v-1", "v-2", "v-2.reason"})

        due = self.forget(Mode.EXPIRE, operation_id="exp-2", now=AFTER_PURGE)
        self.assertValidForget(due)
        self.assertEqual(self.store.payload_files("m-1"), ())
        for name in ("v-1", "v-2", "v-2.reason"):
            self.assertEqual(self.surface(due, f"payload:m-1/{name}").status, St.VERIFIED)
        for proj in ms.PROJECTIONS:
            self.assertEqual(len(self.store.generations_on_disk(proj)), 1)  # no rollback copy left
        self.assertNoTargetInAnyGeneration(self.store, "m-1", ("tuesday", "thursday"))
        self.assertIn("scheduled purge executed", self.surface(due, "envelope-metadata:m-1").evidence)
        self.assertEqual(due.outcome, Outcome.VERIFIED_WITH_RETENTION, due.unverifiable_surfaces)
        self.assertEqual(grep_tree(self.root, "standup moved to"), [])

    def test_expire_before_purge_after_keeps_the_payload_retained(self) -> None:
        self.seed(forgetting_policy="expire-later", retention_until="2026-09-01T00:00:00Z")
        self.forget(Mode.EXPIRE, operation_id="exp-1")
        again = self.forget(Mode.EXPIRE, operation_id="exp-2", now="2026-11-30T00:00:00Z")
        self.assertEqual(self.surface(again, "payload:m-1/v-1").status, St.RETAINED_BY_POLICY)
        self.assertEqual(self.store.payload_files("m-1"), ("v-1",))

    def test_remove_derived_on_a_decayed_target_purges_without_a_transition(self) -> None:
        self.seed(forgetting_policy="expire-later-or-remove", retention_until="2026-09-01T00:00:00Z")
        self.store.rebuild_projections()
        self.forget(Mode.EXPIRE, operation_id="exp-1")
        n_events = len(self.store.events())
        r = self.forget(Mode.REMOVE_DERIVED, operation_id="rd-1")
        self.assertIsNone(r.refusal, r.refusal_detail)
        self.assertValidForget(r)
        self.assertEqual(self.store.payload_files("m-1"), ())
        cur = self.store.current_envelope("m-1", "v-1")
        assert cur is not None
        self.assertEqual(cur.lifecycle_state, me.LifecycleState.DECAYED)  # terminal: stays DECAYED
        new = self.store.events()[n_events:]
        self.assertEqual([(e["kind"], e["transitions"]) for e in new], [("payload-purged", [])])
        self.assertEqual(r.outcome, Outcome.VERIFIED_WITH_RETENTION, r.unverifiable_surfaces)


class TestDestroyStore(_Case):
    def _pin_store(self) -> Path:
        pin = self.tmp / "proj-x" / "memory"
        pin.mkdir(parents=True)
        (pin / "MEMORY.md").write_text("- [x](user_x.md) synthetic\n")
        (pin / "user_x.md").write_text("synthetic pin\n")
        return pin

    def _destroy(self, pin: Path, **kw) -> ops.ForgetReceipt:
        kw.setdefault("authority_ref", "auth:operator")
        return ops.forget(
            self.store, operation_id=kw.pop("operation_id", "destroy-1"), mode=Mode.DESTROY_STORE,
            lifecycle_policies=self.policies, now=NOW, pin_store=pin, archive_root=self.tmp / "archives",
            pin_store_policy_revision=LC_REV, pin_store_forgetting_policy="destroy", **kw,
        )

    def test_destroy_store_reports_archive_retained_and_off_box_unverifiable(self) -> None:
        pin = self._pin_store()
        r = self._destroy(pin)
        self.assertValidForget(r)
        self.assertFalse(pin.exists())
        archives = list((self.tmp / "archives").glob("*.tar.gz"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(self.surface(r, f"store:{pin}").status, St.VERIFIED)
        arch = self.surface(r, f"backup:archive:{archives[0]}")
        self.assertEqual(arch.status, St.RETAINED_BY_POLICY)
        self.assertIn(str(archives[0]), arch.evidence)
        off = self.surface(r, "backup:off-box")
        self.assertEqual(off.status, St.UNVERIFIABLE)
        self.assertEqual(off.evidence, ops.OFF_BOX_BACKUP_EVIDENCE)
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)

    def test_non_pin_store_is_refused_and_left_intact(self) -> None:
        plain = self.tmp / "not-a-pin-store"
        plain.mkdir()
        (plain / "notes.txt").write_text("synthetic\n")
        r = self._destroy(plain)
        self.assertEqual(r.outcome, Outcome.REFUSED)
        self.assertTrue((plain / "notes.txt").is_file())
        self.assertFalse((self.tmp / "archives").exists())

    def test_teardown_raising_is_incomplete_and_store_kept(self) -> None:
        pin = self._pin_store()
        with mock.patch.object(ops.memory_store_teardown, "teardown", side_effect=RuntimeError("archive failed")):
            r = self._destroy(pin)
        self.assertValidForget(r)
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)
        self.assertTrue((pin / "MEMORY.md").is_file())
        self.assertEqual(self.surface(r, f"store:{pin}").status, St.UNVERIFIABLE)

    def test_destroy_store_wrong_authority_refused_and_intact(self) -> None:
        pin = self._pin_store()
        r = self._destroy(pin, authority_ref=AUTH)
        self.assertEqual(r.refusal, LR.AUTHORITY_MISMATCH)
        self.assertTrue((pin / "user_x.md").is_file())


class TestReceipts(_Case):
    def test_digest_is_deterministic_across_identical_runs(self) -> None:
        self.seed()
        r1 = self.correct()
        with tempfile.TemporaryDirectory() as d:
            other = ms.init_store(Path(d) / "store", backup_surfaces=[], media_scrubbed=True)
            env = make_env(canonical_source_ref=self.live_ref())
            self.assertIsNone(other.append_version(env, TUESDAY, lifecycle_policy=self.policies[LC_REV], trust_policy=trust_policy(), now=NOW).refusal)
            r2 = ops.correct(
                other, operation_id="corr-1", memory_id="m-1", expected_version_id="v-1",
                expected_version_digest=other.version_digest("m-1", "v-1") or "", successor=self.successor(),
                successor_payload=THURSDAY, reason=ops.CorrectionReason.CONTRADICTION, actor="test-actor",
                authority_ref=AUTH, lifecycle_policies=self.policies, trust_policy=trust_policy(),
                sources={SRC_SYSTEM: self.fs}, now=NOW,
            )
        self.assertEqual(r1.receipt_digest, r2.receipt_digest)
        self.assertEqual(r1.receipt_digest, ops.compute_correction_receipt_digest(r1))

    def test_round_trip_to_and_from_json_including_the_written_file(self) -> None:
        self.seed()
        c = self.correct()
        f = self.forget(Mode.REMOVE_DERIVED)
        self.assertEqual(ops.correction_receipt_from_json(ops.correction_receipt_to_json(c)), c)
        self.assertEqual(ops.forget_receipt_from_json(ops.forget_receipt_to_json(f)), f)
        on_disk = json.loads((self.root / "receipts" / "forget-1.json").read_text())
        self.assertEqual(ops.forget_receipt_from_json(on_disk), f)
        on_disk = json.loads((self.root / "receipts" / "corr-1.json").read_text())
        self.assertEqual(ops.correction_receipt_from_json(on_disk), c)

    def _tamper(self, value):
        if isinstance(value, bool):
            return not value
        if isinstance(value, str):
            return value + "x"
        if value is None:
            return "x"
        if isinstance(value, list):
            return value[:-1] if value else ["x"]
        return "x"

    def test_from_json_rejects_unknown_keys_at_every_level(self) -> None:
        # Review r12b: a receipt FILE with extra keys ({"success": true, ...})
        # must fail closed at parse, not validate with the extras ignored.
        self.seed()
        cases = [
            (ops.correction_receipt_to_json(self.correct()), ops.correction_receipt_from_json, "index_purge_receipts"),
            (ops.forget_receipt_to_json(self.forget(Mode.REMOVE_DERIVED)), ops.forget_receipt_from_json, "projection_purge_receipts"),
        ]
        for good, from_json, ipr_key in cases:
            from_json(good)  # the untouched receipt parses
            injections = {
                "top-level": lambda b: b.update(success=True, erased_everywhere=True),
                "surface": lambda b: b["surfaces"][0].update(verified_by="anyone"),
                "purge-receipt": lambda b, k=ipr_key: b[k][0].update(all_clear=True),
                "policy-pin": lambda b: b["lifecycle_policy_pins"][0].update(trusted=True),
            }
            for label, inject in injections.items():
                with self.subTest(receipt=good["receipt_schema_version"], level=label):
                    bad = json.loads(json.dumps(good))
                    inject(bad)
                    with self.assertRaises(me.SchemaError):
                        from_json(bad)

    def test_tampering_any_field_is_denied(self) -> None:
        self.seed()
        receipts = [
            (self.correct(), ops.correction_receipt_to_json, ops.correction_receipt_from_json, ops.validate_correction_receipt),
            (self.forget(Mode.REMOVE_DERIVED), ops.forget_receipt_to_json, ops.forget_receipt_from_json, ops.validate_forget_receipt),
        ]
        for receipt, to_json, from_json, validate in receipts:
            good = to_json(receipt)
            self.assertTrue(validate(from_json(good)).allowed)
            for key in sorted(good):
                with self.subTest(receipt=type(receipt).__name__, field=key):
                    bad = dict(good)
                    bad[key] = self._tamper(good[key])
                    try:
                        parsed = from_json(bad)
                    except me.SchemaError:
                        continue  # fails closed at parse
                    self.assertFalse(validate(parsed).allowed)
            # nested surface fields too
            for i, rec in enumerate(good["surfaces"]):
                for key in ("surface", "evidence"):
                    with self.subTest(receipt=type(receipt).__name__, surface=i, field=key):
                        bad = json.loads(json.dumps(good))
                        bad["surfaces"][i][key] += "x"
                        self.assertFalse(validate(from_json(bad)).allowed)

    def test_outcome_and_summaries_are_recomputed_and_enforced(self) -> None:
        self.seed()
        f = self.forget(Mode.REMOVE_DERIVED, sources={})  # no adapter -> source unverifiable
        self.assertEqual(f.outcome, Outcome.INCOMPLETE)
        lies = {
            "outcome": Outcome.VERIFIED_WITH_RETENTION,
            "source_disposition": "verified",
            "derived_payload_disposition": "retained-by-policy",
            "backup_disposition": "verified",
            "unverifiable_surfaces": (),
        }
        for field_name, value in lies.items():
            with self.subTest(field=field_name):
                bad = dataclasses.replace(f, **{field_name: value})
                bad = dataclasses.replace(bad, receipt_digest=ops.compute_forget_receipt_digest(bad))
                self.assertFalse(ops.validate_forget_receipt(bad).allowed)
        c = self.correct(sources={})
        bad = dataclasses.replace(c, source_status="unchanged", outcome=Outcome.VERIFIED_WITH_RETENTION)
        bad = dataclasses.replace(bad, receipt_digest=ops.compute_correction_receipt_digest(bad))
        self.assertFalse(ops.validate_correction_receipt(bad).allowed)

    def test_derived_outcome_table(self) -> None:
        def rec(status: St) -> ops.SurfaceRecord:
            return ops.SurfaceRecord("s", Kind.PAYLOAD, status, "")

        self.assertEqual(ops.derive_outcome([rec(St.VERIFIED)], None), Outcome.VERIFIED)
        self.assertEqual(
            ops.derive_outcome([rec(St.VERIFIED), rec(St.RETAINED_BY_POLICY)], None),
            Outcome.VERIFIED_WITH_RETENTION,
        )
        self.assertEqual(ops.derive_outcome([rec(St.VERIFIED), rec(St.UNVERIFIABLE)], None), Outcome.INCOMPLETE)
        self.assertEqual(ops.derive_outcome([rec(St.VERIFIED), rec(St.REFUSED)], None), Outcome.INCOMPLETE)
        self.assertEqual(ops.derive_outcome([], None), Outcome.INCOMPLETE)
        self.assertEqual(ops.derive_outcome([rec(St.VERIFIED)], LR.STALE_VERSION), Outcome.REFUSED)

    def test_every_op_writes_a_receipt_including_refused(self) -> None:
        self.seed()
        r = self.correct(expected_version_digest="sha256:" + "f" * 64)
        self.assertEqual(r.outcome, Outcome.REFUSED)
        on_disk = json.loads((self.root / "receipts" / "corr-1.json").read_text())
        self.assertEqual(on_disk["refusal"], "stale-version")
        self.assertTrue(ops.validate_correction_receipt(ops.correction_receipt_from_json(on_disk)).allowed)


class TestResidueSurfacesAreEnumerated(_Case):
    backup_surfaces = [
        {"name": "nightly", "retention": "30d", "verifiable": False},
        {"name": "adhoc", "retention": None, "verifiable": True},
    ]
    media_scrubbed = False

    def test_every_surface_kind_is_enumerated_never_assumed(self) -> None:
        self.seed()
        self.store.rebuild_projections()
        self.assertIsNone(self.correct().refusal)
        r = self.forget(Mode.REMOVE_DERIVED)
        self.assertValidForget(r)
        names = {s.surface for s in r.surfaces}
        expected = {
            "payload:m-1/v-1", "payload:m-1/v-2", "projection:retrievable-set:m-1",
            "projection:content:gen-3", "projection:subject:gen-3", "envelope-metadata:m-1",
            "event-log", "tombstone:m-1", f"source-unchanged:{SRC_SYSTEM}/{SRC_OBJECT}",
            "backup:nightly", "backup:adhoc", "media-residue", "receipts",
        }
        self.assertEqual(names, expected)
        self.assertEqual(self.surface(r, "backup:nightly").status, St.RETAINED_BY_POLICY)
        self.assertIn("30d", self.surface(r, "backup:nightly").evidence)
        self.assertEqual(self.surface(r, "backup:adhoc").status, St.UNVERIFIABLE)  # no retention stated
        self.assertEqual(self.surface(r, "media-residue").status, St.UNVERIFIABLE)
        self.assertEqual(r.backup_disposition, "mixed")
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)

    def _reseal(self, r, surfaces):
        """Re-derive every summary with the module's OWN derive_* functions and
        re-digest: what a forger holding the unkeyed digest can do (review r6)."""
        surfaces = tuple(surfaces)
        fields = dict(surfaces=surfaces, outcome=ops.derive_outcome(surfaces, r.refusal), receipt_digest="")
        if isinstance(r, ops.ForgetReceipt):
            fields.update(
                source_disposition=ops.derive_source_status(surfaces),
                derived_payload_disposition=ops.derive_disposition(surfaces, Kind.PAYLOAD),
                backup_disposition=ops.derive_disposition(surfaces, Kind.BACKUP),
                unverifiable_surfaces=ops.unverifiable_surface_names(surfaces),
            )
            r = dataclasses.replace(r, **fields)
            return dataclasses.replace(r, receipt_digest=ops.compute_forget_receipt_digest(r))
        fields.update(source_status=ops.derive_source_status(surfaces))
        r = dataclasses.replace(r, **fields)
        return dataclasses.replace(r, receipt_digest=ops.compute_correction_receipt_digest(r))

    def test_validator_denies_a_receipt_that_elides_a_required_surface_kind(self) -> None:
        self.seed()
        self.store.rebuild_projections()
        c = self.correct(sources={})
        real = self.forget(Mode.REMOVE_DERIVED, sources={})
        self.assertEqual(real.outcome, Outcome.INCOMPLETE)
        forged = {
            "elided-unverifiable": self._reseal(real, [s for s in real.surfaces if s.status != St.UNVERIFIABLE]),
            "payload-only": self._reseal(real, [s for s in real.surfaces if s.kind == Kind.PAYLOAD]),
            "correction-without-source": self._reseal(c, [s for s in c.surfaces if s.kind != Kind.SOURCE]),
        }
        self.assertIn(forged["elided-unverifiable"].outcome, (Outcome.VERIFIED, Outcome.VERIFIED_WITH_RETENTION))
        for label, r in forged.items():
            with self.subTest(forgery=label):
                validate = ops.validate_forget_receipt if isinstance(r, ops.ForgetReceipt) else ops.validate_correction_receipt
                d = validate(r)
                self.assertFalse(d.allowed, d.detail)
                self.assertIn("surface kind", d.detail)

    def test_purge_receipt_cannot_disagree_with_surfaces(self) -> None:
        self.seed()
        r = self.forget(Mode.REMOVE_DERIVED)
        ipr = dataclasses.replace(r.projection_purge_receipts[0], negative_lookup_verified=False)
        bad = dataclasses.replace(r, projection_purge_receipts=(ipr, *r.projection_purge_receipts[1:]))
        bad = dataclasses.replace(bad, receipt_digest=ops.compute_forget_receipt_digest(bad))
        self.assertFalse(ops.validate_forget_receipt(bad).allowed)


class TestOrphanVersions(_Case):
    """A crash inside correct() after the successor's version file is written
    but before its event leaves an orphan (review r9 / r12d)."""

    def _crash_correct(self, **kw) -> None:
        class Crash(BaseException):
            pass

        real = ms.MemoryStore.append_event

        def crashing(store, kind, **ekw):
            if kind == "supersede":
                raise Crash("before the supersede event")
            return real(store, kind, **ekw)

        with mock.patch.object(ms.MemoryStore, "append_event", crashing):
            with self.assertRaises(Crash):
                self.correct(**kw)

    def test_correct_retry_with_the_same_successor_completes_the_orphan(self) -> None:
        self.seed()
        self.store.rebuild_projections()
        self._crash_correct()
        self.assertEqual(self.store.version_ids("m-1"), ("v-1", "v-2"))
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-1",))
        r = self.correct()  # same operation_id, same successor
        self.assertIsNone(r.refusal, r.refusal_detail)
        self.assertEqual(r.outcome, Outcome.VERIFIED_WITH_RETENTION, [s for s in r.surfaces if s.status != St.VERIFIED])
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-2",))
        self.assertEqual([e["kind"] for e in self.store.events()], ["retain", "supersede"])

    def test_correct_retry_with_different_bytes_under_the_orphan_id_is_version_exists(self) -> None:
        self.seed()
        self._crash_correct()
        # ops/ is bookkeeping like receipts/: the op binds its id to its
        # request just before its admission attempt (RB3 N1a), even when the
        # store then refuses that admission.
        before = tree_digest(self.root, exclude=("receipts", "ops"))
        r = self.correct(payload="synthetic note: the standup moved to wednesday", operation_id="corr-2")
        self.assertEqual(r.refusal, LR.VERSION_EXISTS)
        self.assertEqual(tree_digest(self.root, exclude=("receipts", "ops")), before)

    def _orphan_citing_an_unrelated_object(self, **env_kw) -> Path:
        """Review n5: an admission crashes between its version file and its
        event; the orphan cites an object the memory was never admitted from."""
        other = self.src_root / "unrelated-object.md"
        other.write_text("someone else's record\n")
        env = make_env("m-1", "v-0", TUESDAY, subjects=("carol",),
                       canonical_source_ref=source_ref(object_id="unrelated-object.md", digest=file_digest(other)), **env_kw)
        with mock.patch.object(ms.MemoryStore, "append_event", side_effect=ops.SimulatedCrash("before the event")):
            with self.assertRaises(ops.SimulatedCrash):
                self.store.append_version(env, TUESDAY, lifecycle_policy=self.policies[LC_REV], trust_policy=trust_policy(), now=NOW)
        return other

    def test_an_orphan_never_causes_a_source_request_or_a_tombstone(self) -> None:
        other = self._orphan_citing_an_unrelated_object(forgetting_policy="forget-tomb")
        self.seed("m-1", "v-1", TUESDAY, forgetting_policy="forget-tomb")
        self.assertEqual(self.store.version_ids("m-1"), ("v-0", "v-1"))
        r = self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id="erase-1")
        self.assertEqual([s.surface for s in r.surfaces if s.kind == Kind.SOURCE], [f"source-erasure:{SRC_SYSTEM}/{SRC_OBJECT}"])
        self.assertTrue(other.exists())  # never requested
        f = self.forget(Mode.REMOVE_DERIVED, operation_id="forget-1")
        self.assertEqual([(t["source_system"], t["object_id"]) for t in self.store.tombstones()], [(SRC_SYSTEM, SRC_OBJECT)])
        self.assertIn(ORPHAN_TAG, self.surface(f, "payload:m-1/v-0").evidence)  # still reported...
        self.assertEqual(self.store.payload_files("m-1"), ())  # ...and still purged
        fresh = make_env("m-9", "v-1", THURSDAY, canonical_source_ref=source_ref(object_id="unrelated-object.md"))
        res = self.store.append_version(fresh, THURSDAY, lifecycle_policy=self.policies[LC_REV], trust_policy=trust_policy(), now=NOW)
        self.assertIsNone(res.refusal, res.detail)  # the unrelated object was never tombstoned

    def test_an_orphans_subjects_never_select_a_target_for_source_erasure(self) -> None:
        self._orphan_citing_an_unrelated_object()
        self.seed("m-1", "v-1", TUESDAY)
        r = self.forget(Mode.REQUEST_SOURCE_ERASURE, targets=(), subject_scope=("carol",))
        self.assertEqual(r.refusal, LR.MALFORMED_INPUT)  # empty scope: carol only ever lived in the orphan
        self.assertTrue(self.src_file.exists())

    def test_an_orphans_subjects_never_select_a_target_in_any_mode(self) -> None:
        # Review p2: an orphan's subject selected the ADMITTED memory for a
        # local forget, which then forgot it and tombstoned its admitted source.
        self._orphan_citing_an_unrelated_object(forgetting_policy="forget-tomb")
        self.seed("m-1", "v-1", TUESDAY, forgetting_policy="forget-tomb")  # admitted: never about carol
        before = tree_digest(self.root)
        for mode in (Mode.REMOVE_DERIVED, Mode.EXPIRE, Mode.REQUEST_SOURCE_ERASURE):
            with self.subTest(mode=mode.value):
                r = self.forget(mode, operation_id=f"f-{mode.value}", targets=(), subject_scope=("carol",))
                self.assertEqual(r.refusal, LR.MALFORMED_INPUT, r.refusal_detail)  # empty scope
        self.assertEqual(tree_digest(self.root), before)
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-1",))
        self.assertEqual(self.store.tombstones(), ())
        # ...but once m-1 IS targeted, its orphan is purged with it, labelled as residue.
        f = self.forget(Mode.REMOVE_DERIVED, operation_id="f-by-id")
        self.assertIn(ORPHAN_TAG, self.surface(f, "payload:m-1/v-0").evidence)
        self.assertEqual(self.store.payload_files("m-1"), ())

    def test_correction_receipt_labels_an_orphan_as_crash_residue(self) -> None:
        self.seed()
        self._crash_correct()
        r = self.correct(version_id="v-3", operation_id="corr-2")
        self.assertIsNone(r.refusal, r.refusal_detail)
        rec = self.surface(r, "payload:m-1/v-2")
        self.assertIn("orphan (crash residue, never admitted)", rec.evidence)
        self.assertNotIn("superseded", rec.evidence)
        self.assertEqual(rec.status, St.UNVERIFIABLE)


class TestRealFaultsWriteAReceipt(_Case):
    """A REAL fault (not the simulated crash) after mutation began is caught:
    the op writes an ``incomplete`` receipt whose failing surface is
    ``unverifiable`` with evidence = exception class + errno ONLY (review r10)."""

    CONTENT_IN_MESSAGE = "standup moved to tuesday"

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_real_io_error_mid_forget_writes_an_incomplete_receipt_then_rerun_repairs(self) -> None:
        self.seed()
        self.store.rebuild_projections()
        pdir = self.root / "payloads" / "m-1"
        os.chmod(pdir, 0o500)  # r-x: unlink inside it fails with EACCES
        try:
            r = self.forget(Mode.REMOVE_DERIVED, operation_id="f-io")
        finally:
            os.chmod(pdir, 0o700)
        self.assertValidForget(r)
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)
        err = self.surface(r, "op-error:remove-derived:payload-purge")
        self.assertEqual((err.kind, err.status), (Kind.PAYLOAD, St.UNVERIFIABLE))
        self.assertTrue(err.evidence.startswith("PermissionError errno=13 (EACCES)"), err.evidence)
        self.assertNotIn(str(self.root), err.evidence)
        first = self.store.receipt_paths("f-io")
        self.assertEqual([p.name for p in first], ["f-io.json"])
        first_bytes = first[0].read_bytes()
        again = self.forget(Mode.REMOVE_DERIVED, operation_id="f-io")
        self.assertEqual(again.outcome, Outcome.VERIFIED_WITH_RETENTION, again.unverifiable_surfaces)
        paths = self.store.receipt_paths("f-io")
        self.assertEqual([p.name for p in paths], ["f-io.json", "f-io+attempt-2.json"])
        self.assertEqual(paths[0].read_bytes(), first_bytes)  # the first receipt is never overwritten

    def test_fault_evidence_is_exception_class_and_errno_only(self) -> None:
        self.seed()
        exc = PermissionError(errno.EACCES, f"denied: {self.CONTENT_IN_MESSAGE}", f"/x/{self.CONTENT_IN_MESSAGE}")
        with mock.patch.object(ms.MemoryStore, "purge_payload_file", side_effect=exc):
            r = self.forget(Mode.REMOVE_DERIVED, operation_id="f-io")
        self.assertValidForget(r)
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)
        self.assertEqual(grep_tree(self.root / "receipts", self.CONTENT_IN_MESSAGE), [])
        for rec in r.surfaces:
            self.assertNotIn(self.CONTENT_IN_MESSAGE, rec.evidence)
        # every kind remove-derived always enumerates is still on the receipt
        self.assertEqual({s.kind for s in r.surfaces} >= ops.REQUIRED_SURFACE_KINDS[Mode.REMOVE_DERIVED], True)

    def test_a_fault_before_any_mutation_yields_a_receipt_that_validates(self) -> None:
        # The op never resolved a policy, so the receipt names none; it is
        # incomplete (claims nothing) and must still validate on disk.
        self.seed()
        succ, digest = self.successor(), self.store.version_digest("m-1", "v-1")
        with mock.patch.object(ms.MemoryStore, "stored_envelope", side_effect=OSError(errno.EIO, "x")):
            c = ops.correct(
                self.store, operation_id="corr-pre", memory_id="m-1", expected_version_id="v-1",
                expected_version_digest=digest or "", successor=succ, successor_payload=THURSDAY,
                reason=ops.CorrectionReason.STALE, actor="test-actor", authority_ref=AUTH,
                lifecycle_policies=self.policies, trust_policy=trust_policy(), now=NOW,
            )
        with mock.patch.object(ops, "_resolve_targets", side_effect=OSError(errno.EIO, "x")):
            f = self.forget(Mode.REMOVE_DERIVED, operation_id="forget-pre")
        for r, validate, step in ((c, ops.validate_correction_receipt, "supersede"), (f, ops.validate_forget_receipt, "remove-derived")):
            with self.subTest(receipt=type(r).__name__):
                self.assertEqual((r.outcome, r.lifecycle_policy_pins), (Outcome.INCOMPLETE, ()))
                err = self.surface(r, f"op-error:{step}:preflight")
                self.assertIn("before any mutation", err.evidence)
                d = validate(r)
                self.assertTrue(d.allowed, d.detail)
        # ...while a receipt CLAIMING success must still name its policy.
        good = self.forget(Mode.REMOVE_DERIVED, operation_id="forget-ok")
        self.assertEqual(good.outcome, Outcome.VERIFIED_WITH_RETENTION)
        stripped = dataclasses.replace(good, lifecycle_policy_pins=())
        stripped = dataclasses.replace(stripped, receipt_digest=ops.compute_forget_receipt_digest(stripped))
        self.assertFalse(ops.validate_forget_receipt(stripped).allowed)

    def test_a_non_oserror_fault_inside_forget_still_writes_an_incomplete_receipt(self) -> None:
        # Kills the "handler narrowed to OSError" mutation (review S6): a defect
        # must leave a receipt for repair exactly like an I/O fault does.
        self.seed()
        with mock.patch.object(ops, "_verify_local", side_effect=KeyError("zqxj-bug")), \
                contextlib.redirect_stderr(io.StringIO()):
            r = self.forget(Mode.REMOVE_DERIVED, operation_id="f-bug")
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)
        self.assertEqual([p.name for p in self.store.receipt_paths("f-bug")], ["f-bug.json"])
        self.assertValidForget(r)

    def test_a_defect_is_reported_as_an_internal_error_with_frames_on_stderr(self) -> None:
        # Review n3 (b1): a programming error is not an I/O fault; the receipt
        # says so, and the traceback FRAMES (never the message) go to stderr.
        self.seed()
        err = io.StringIO()
        with mock.patch.object(ops, "_verify_local", side_effect=KeyError("zqxj-bug")), contextlib.redirect_stderr(err):
            r = self.forget(Mode.REMOVE_DERIVED, operation_id="f-bug")
        rec = self.surface(r, "op-error:remove-derived:post-verify")
        self.assertTrue(rec.evidence.startswith("internal-error KeyError (not an I/O fault; defect)"), rec.evidence)
        self.assertIn("memory_lifecycle_ops.py:", err.getvalue())  # file:line:function frames
        self.assertNotIn("zqxj-bug", err.getvalue() + json.dumps(ops.forget_receipt_to_json(r)))
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)

    def test_wrong_typed_inputs_are_a_typed_refusal_not_a_fault(self) -> None:
        # Review n3 (b2): an input no preflight checked became an "incomplete"
        # fault receipt. Every public op now type-checks before its lock.
        self.seed()
        before = tree_digest(self.root)
        correct_cases = {
            "trust_receipts": dict(trust_receipts=42),
            "trust_receipts-str": dict(trust_receipts="receipts"),
            "trust_policy": dict(trust_policy="P-lattice"),
            "lifecycle_policies": dict(lifecycle_policies=[self.policies[LC_REV]]),
            "sources-value": dict(sources={SRC_SYSTEM: 42}),
        }
        for label, kw in correct_cases.items():
            with self.subTest(op="correct", arg=label):
                r = self.correct(operation_id=f"c-{label}", **kw)
                self.assertEqual(r.refusal, LR.MALFORMED_INPUT, r.refusal_detail)
        forget_cases = {
            "sources-value": dict(sources={SRC_SYSTEM: "file-source"}),
            "lifecycle_policies": dict(lifecycle_policies=None),
            "_fault": dict(_fault=42),
            "archive_root": dict(archive_root=42),
            "pin_store_policy_revision": dict(pin_store_policy_revision=7),
        }
        for label, kw in forget_cases.items():
            with self.subTest(op="forget", arg=label):
                r = self.forget(Mode.REMOVE_DERIVED, operation_id=f"f-{label}", **kw)
                self.assertEqual(r.refusal, LR.MALFORMED_INPUT, r.refusal_detail)
        self.assertEqual(tree_digest(self.root), before)
        res = self.store.append_version(make_env("m-2"), TUESDAY, lifecycle_policy=self.policies[LC_REV],
                                        trust_policy=trust_policy(), trust_receipts=42, now=NOW)
        self.assertEqual(res.refusal, LR.MALFORMED_INPUT)

    def test_a_correction_fault_after_admission_converges_on_the_same_operation(self) -> None:
        # Review n3 (a): the fault receipt says "re-run the same operation_id
        # to repair", but the re-run found its OWN successor current and
        # refused stale-version. It now completes the remaining steps.
        self.seed()
        self.store.rebuild_projections()
        with mock.patch.object(ops, "_correction_projection_evidence", side_effect=OSError(errno.EIO, "x")):
            first = self.correct()
        self.assertEqual((first.outcome, first.successor_version_id), (Outcome.INCOMPLETE, "v-2"))
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-2",))
        other = self.correct(operation_id="corr-other")  # a DIFFERENT operation: still stale
        self.assertEqual(other.refusal, LR.STALE_VERSION)
        before = tree_digest(self.root)
        drifted = self.correct(version_id="v-9")  # same op id, another request: bound to one request
        self.assertEqual(drifted.refusal, LR.OPERATION_ID_REUSE, drifted.refusal_detail)
        self.assertEqual(tree_digest(self.root), before)
        again = self.correct()  # same operation_id, same successor
        self.assertIsNone(again.refusal, again.refusal_detail)
        self.assertEqual(again.outcome, Outcome.VERIFIED_WITH_RETENTION)
        self.assertValidCorrection(again)
        self.assertIn("resumed", self.surface(again, "lineage:m-1/v-2").evidence)
        self.assertEqual([e["kind"] for e in self.store.events()], ["retain", "supersede"])  # nothing re-admitted
        outcomes = [json.loads(p.read_bytes())["outcome"] for p in self.store.receipt_paths("corr-1")]
        self.assertEqual(outcomes, ["incomplete", "refused", "verified-with-retention"])

    def test_real_io_error_mid_correction_writes_an_incomplete_receipt_then_retry_completes(self) -> None:
        self.seed()
        real = ms.MemoryStore.append_event

        def failing(store, kind, **kw):
            if kind == "supersede":
                raise OSError(errno.EIO, f"I/O error writing {self.CONTENT_IN_MESSAGE}")
            return real(store, kind, **kw)

        with mock.patch.object(ms.MemoryStore, "append_event", failing):
            r = self.correct()
        self.assertValidCorrection(r)
        self.assertEqual(r.outcome, Outcome.INCOMPLETE)
        err = self.surface(r, "op-error:supersede:admit")
        self.assertEqual(err.status, St.UNVERIFIABLE)
        self.assertTrue(err.evidence.startswith("OSError errno=5 (EIO)"), err.evidence)
        self.assertEqual(grep_tree(self.root / "receipts", self.CONTENT_IN_MESSAGE), [])
        again = self.correct()  # same operation_id + successor: completes the orphan
        self.assertIsNone(again.refusal, again.refusal_detail)
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-2",))
        self.assertEqual([p.name for p in self.store.receipt_paths("corr-1")], ["corr-1.json", "corr-1+attempt-2.json"])


class CountingSource(ops.FileSource):
    """Counts requests; ``crash_after_send`` performs the request then dies
    (SimulatedCrash) before the op can journal it; ``crash_before_send`` dies
    without touching the source (the can't-tell case)."""

    crash_after_send = False
    crash_before_send = False
    idempotent_request_ids = False  # the pre-T-4504 can't-tell tests need an adapter that makes no promise

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.calls = 0
        self.request_ids: list[str] = []

    def _maybe_crash(self, out):
        self.calls += 1
        if self.crash_after_send or self.crash_before_send:
            raise ops.SimulatedCrash("process died around the source request")
        return out

    def request_correction(self, ref, correction, authority_ref, request_id):
        self.request_ids.append(request_id)
        if self.crash_before_send:
            return self._maybe_crash(None)
        return self._maybe_crash(super().request_correction(ref, correction, authority_ref, request_id))

    def request_erasure(self, ref, authority_ref, request_id):
        self.request_ids.append(request_id)
        if self.crash_before_send:
            return self._maybe_crash(None)
        return self._maybe_crash(super().request_erasure(ref, authority_ref, request_id))


class TestOneOperationOneRequest(_Case):
    """RB3 N1: an operation_id means exactly one request, and a completed op
    is never re-executed."""

    def test_replaying_a_completed_correction_returns_its_receipt_and_never_touches_the_source(self) -> None:
        # Review p1: a replay of a COMPLETED op with a new source_correction
        # rewrote the source every time, read verified, and wrote no event.
        # (Fix-up M1: another request under the finished op_id is now refused
        # operation-id-reuse by the gate; only an IDENTICAL replay gets the
        # original receipt back.)
        self.seed()
        text = "source text v1 (requested by the original op)\n"
        first = self.correct(source_correction=text)
        self.assertEqual(first.outcome, Outcome.VERIFIED_WITH_RETENTION)
        source_bytes, events = self.src_file.read_bytes(), self.store.events()
        first_file = self.store.receipt_paths("corr-1")[0].read_bytes()
        v1 = self.store.version_digest("m-1", "v-1")
        for i, text_b in enumerate(("REPLAYED text never requested\n", "and again\n")):
            with self.subTest(replay=i):
                again = self.correct(source_correction=text_b, expected_version_digest=v1)
                self.assertEqual(again.refusal, LR.OPERATION_ID_REUSE, again.refusal_detail)
        other = self.correct(version_id="v-3", source_correction=text, expected_version_digest=v1)
        self.assertEqual(other.refusal, LR.OPERATION_ID_REUSE)
        self.assertEqual(self.src_file.read_bytes(), source_bytes)
        self.assertEqual(self.store.events(), events)
        self.assertEqual(self.store.receipt_paths("corr-1")[0].read_bytes(), first_file)
        n = len(self.store.receipt_paths("corr-1"))
        same = self.correct(source_correction=text, expected_version_digest=v1)  # IDENTICAL replay
        self.assertEqual(same, first)
        self.assertEqual(len(self.store.receipt_paths("corr-1")), n)  # no attempt file for it

    def test_the_request_journal_holds_ids_digests_and_closed_codes_only(self) -> None:
        self.seed()
        text = "synthetic corrected source text\n"
        self.correct(source_correction=text, reason_note="synthetic note: private detail")
        journal = (self.root / "ops" / "corr-1.request.json").read_bytes()
        self.assertEqual(set(json.loads(journal)), {"schema", "operation_id", "op", "request_digest"})
        for needle in (text, "private detail", "synthetic"):
            self.assertNotIn(needle.encode(), journal)

    def test_a_different_request_under_a_finished_op_id_is_operation_id_reuse(self) -> None:
        # Review q1: the finished-op short-circuit ran BEFORE the reuse gate,
        # so another request under a finished op_id silently got the old
        # success receipt. The gate now runs first.
        self.seed("m-1")
        self.seed("m-2", canonical_source_ref=source_ref(object_id="two.md"))
        first = self.forget(Mode.REMOVE_DERIVED, operation_id="f-1", targets=("m-1",))
        self.assertEqual(first.outcome, Outcome.VERIFIED_WITH_RETENTION)
        first_file = self.store.receipt_paths("f-1")[0].read_bytes()
        before = tree_digest(self.root)
        other_target = self.forget(Mode.REMOVE_DERIVED, operation_id="f-1", targets=("m-2",))
        self.assertEqual(other_target.refusal, LR.OPERATION_ID_REUSE, other_target.refusal_detail)
        self.assertEqual(self.store.retrievable_version_ids("m-2"), ("v-1",))  # untouched
        other_mode = self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id="f-1", targets=("m-1",))
        self.assertEqual(other_mode.refusal, LR.OPERATION_ID_REUSE, other_mode.refusal_detail)
        self.assertTrue(self.src_file.exists())
        self.assertEqual(tree_digest(self.root), before)
        self._only_reuse_refusals_were_added("f-1", first_file)
        self.assertEqual(self.forget(Mode.REMOVE_DERIVED, operation_id="f-1", targets=("m-1",)), first)  # identical

    def _only_reuse_refusals_were_added(self, op: str, first_file: bytes) -> None:
        """The first (success) receipt is byte-identical, and every receipt
        added since is a refused operation-id-reuse (a refusal still writes
        its receipt, brief §2.6); no success receipt was added."""
        paths = self.store.receipt_paths(op)
        self.assertEqual(paths[0].read_bytes(), first_file)
        for path in paths[1:]:
            body = json.loads(path.read_bytes())
            self.assertEqual((body["outcome"], body["refusal"]), ("refused", "operation-id-reuse"), path.name)

    def test_a_different_request_under_a_finished_correction_is_operation_id_reuse(self) -> None:
        # M1 addendum, correct() side: the reuse gate runs BEFORE the finished-
        # op short-circuit, so another successor or another source text under
        # a finished correction's op_id is refused, and nothing runs.
        self.seed()
        src = CountingSource(self.src_root)
        first = self.correct(source_correction="text A\n", sources={SRC_SYSTEM: src})
        self.assertEqual((first.outcome, src.calls), (Outcome.VERIFIED_WITH_RETENTION, 1))
        first_file = self.store.receipt_paths("corr-1")[0].read_bytes()
        source_bytes, events = self.src_file.read_bytes(), self.store.events()
        v1 = self.store.version_digest("m-1", "v-1")
        cases = {
            "other successor": dict(version_id="v-3", source_correction="text A\n"),
            "other source text": dict(source_correction="text B\n"),
        }
        for label, kw in cases.items():
            with self.subTest(changed=label):
                r = self.correct(sources={SRC_SYSTEM: src}, expected_version_digest=v1, **kw)
                self.assertEqual(r.refusal, LR.OPERATION_ID_REUSE, r.refusal_detail)
        self.assertEqual((src.calls, self.src_file.read_bytes(), self.store.events()), (1, source_bytes, events))
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-2",))
        self._only_reuse_refusals_were_added("corr-1", first_file)
        self.assertEqual(self.correct(source_correction="text A\n", sources={SRC_SYSTEM: src}, expected_version_digest=v1), first)

    def test_another_op_kind_under_a_finished_op_id_is_operation_id_reuse(self) -> None:
        self.seed()
        done = self.correct(operation_id="op-x")
        self.assertEqual(done.outcome, Outcome.VERIFIED_WITH_RETENTION)
        f = self.forget(Mode.REMOVE_DERIVED, operation_id="op-x")
        self.assertEqual(f.refusal, LR.OPERATION_ID_REUSE, f.refusal_detail)  # was a misleading malformed-input
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-2",))

    def test_an_identical_rerun_of_a_finished_forget_is_not_re_executed(self) -> None:
        # Kills "forget re-executes a finished op": no source request, no
        # rebuild, no new attempt file; the original receipt comes back.
        self.seed()
        self.store.rebuild_projections()
        src = CountingSource(self.src_root)
        for mode, op in ((Mode.REMOVE_DERIVED, "f-local"), (Mode.REQUEST_SOURCE_ERASURE, "f-erase")):
            with self.subTest(mode=mode.value):
                first = self.forget(mode, operation_id=op, sources={SRC_SYSTEM: src})
                self.assertEqual(first.outcome, Outcome.VERIFIED_WITH_RETENTION, first.unverifiable_surfaces)
                calls, gens, events = src.calls, self.store.generations_on_disk("content"), self.store.events()
                again = self.forget(mode, operation_id=op, sources={SRC_SYSTEM: src})
                self.assertEqual(again, first)
                self.assertEqual((src.calls, self.store.generations_on_disk("content"), self.store.events()),
                                 (calls, gens, events))
                self.assertEqual([p.name for p in self.store.receipt_paths(op)], [f"{op}.json"])

    def test_a_resume_with_a_different_note_text_is_operation_id_reuse(self) -> None:
        # Review q5: the request binds note PRESENCE; once this op has stored
        # its note, a resume carrying other note text is another request.
        for label, target in (("after admission", "rebuild_projections"), ("before the admission event", "append_event")):
            with self.subTest(crash=label):
                self.setUp()
                self.seed()
                self.store.rebuild_projections()
                kw = dict(successor=self.successor(), expected_version_digest=self.store.version_digest("m-1", "v-1"))
                with mock.patch.object(ms.MemoryStore, target, side_effect=ops.SimulatedCrash(label)):
                    with self.assertRaises(ops.SimulatedCrash):
                        self.correct(reason_note="note A", **kw)
                r = self.correct(reason_note="note B (different text)", **kw)
                self.assertEqual(r.refusal, LR.OPERATION_ID_REUSE, r.refusal_detail)
                self.assertEqual((self.root / "payloads" / "m-1" / "v-2.reason").read_bytes()[16:], b"note A")
                self.assertIsNone(self.correct(reason_note="note A", **kw).refusal)  # the same text still resumes

    def test_a_resume_with_a_different_source_correction_is_operation_id_reuse(self) -> None:
        # The journal holds no standalone digest of the correction text, but
        # the op-request digest DOES cover it: a crash BEFORE the send, then a
        # resume asking the source for other text, is refused, never sent.
        self.seed()
        src = CountingSource(self.src_root)
        source_bytes = self.src_file.read_bytes()
        with mock.patch.object(ops, "_source_request_surface", side_effect=ops.SimulatedCrash("before the send")):
            with self.assertRaises(ops.SimulatedCrash):
                self.correct(source_correction="text A\n", sources={SRC_SYSTEM: src})
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-2",))  # admitted, then crashed
        prior_digest = self.store.version_digest("m-1", "v-1")
        before = tree_digest(self.root)
        r = self.correct(source_correction="text B\n", sources={SRC_SYSTEM: src}, expected_version_digest=prior_digest)
        self.assertEqual(r.refusal, LR.OPERATION_ID_REUSE, r.refusal_detail)
        self.assertEqual((src.calls, self.src_file.read_bytes()), (0, source_bytes))
        self.assertEqual(tree_digest(self.root), before)
        ok = self.correct(source_correction="text A\n", sources={SRC_SYSTEM: src}, expected_version_digest=prior_digest)
        self.assertEqual((ok.source_status, src.calls, self.src_file.read_text()), ("verified", 1, "text A\n"))

    def test_a_crash_after_the_send_never_resends_on_resume(self) -> None:
        # Crash between the source request and its journal record: the resume
        # must not send again; the fresh read decides.
        for mode in (Mode.REQUEST_SOURCE_ERASURE, Mode.REQUEST_SOURCE_CORRECTION):
            with self.subTest(mode=mode.value):
                self.setUp()
                self.seed()
                src = CountingSource(self.src_root)
                src.crash_after_send = True
                text = "synthetic corrected source text\n"
                run = (lambda: self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id="erase-1", sources={SRC_SYSTEM: src})) \
                    if mode is Mode.REQUEST_SOURCE_ERASURE else \
                    (lambda: self.correct(source_correction=text, sources={SRC_SYSTEM: src},
                                          expected_version_digest=self.store.version_digest("m-1", "v-1")))
                with self.assertRaises(ops.SimulatedCrash):
                    run()
                self.assertEqual(src.calls, 1)
                src.crash_after_send = False
                r = run()
                self.assertEqual(src.calls, 1, "the resume re-sent the source request")
                name = ("source-erasure:" if mode is Mode.REQUEST_SOURCE_ERASURE else "source-correction:") + f"{SRC_SYSTEM}/{SRC_OBJECT}"
                self.assertEqual(self.surface(r, name).status, St.VERIFIED)  # the fresh read shows the requested state

    def test_a_crash_that_cannot_be_told_apart_is_unverifiable_never_a_second_send(self) -> None:
        self.seed()
        src = CountingSource(self.src_root)
        src.crash_before_send = True
        with self.assertRaises(ops.SimulatedCrash):
            self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id="erase-1", sources={SRC_SYSTEM: src})
        src.crash_before_send = False
        r = self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id="erase-1", sources={SRC_SYSTEM: src})
        self.assertEqual(src.calls, 1)
        self.assertEqual(self.surface(r, f"source-erasure:{SRC_SYSTEM}/{SRC_OBJECT}").status, St.UNVERIFIABLE)
        self.assertTrue(self.src_file.exists())


class IdemSource(CountingSource):
    idempotent_request_ids = True


class TestIntentOnlyResume(_Case):
    """T-4504 (ORG-058 L1): a crash after the source-request INTENT and before
    SENT either completes safely (declared idempotent adapter: ONE resend, same
    request_id) or is an explicit stuck item. Never a duplicate send."""

    def _run(self, kind: str, src, op: str):
        if kind == "erasure":
            return self.forget(Mode.REQUEST_SOURCE_ERASURE, operation_id=op, sources={SRC_SYSTEM: src})
        return self.correct(operation_id=op, source_correction="synthetic corrected text\n", sources={SRC_SYSTEM: src},
                            expected_version_digest=self.store.version_digest("m-1", "v-1"))

    def _name(self, kind: str) -> str:
        return ("source-erasure:" if kind == "erasure" else "source-correction:") + f"{SRC_SYSTEM}/{SRC_OBJECT}"

    def _journal(self, op: str) -> list[str]:
        return [r["record"] for r in self.store.op_journal(op)]

    def _crashed(self, kind: str, src, op: str):
        src.crash_before_send = True
        with self.assertRaises(ops.SimulatedCrash):
            self._run(kind, src, op)
        src.crash_before_send = False

    def test_idempotent_adapter_is_resent_exactly_once_with_the_same_request_id(self) -> None:
        for kind in ("erasure", "correction"):
            with self.subTest(kind=kind):
                self.setUp()
                self.seed()
                src = IdemSource(self.src_root)
                self._crashed(kind, src, "op-i")
                self.assertEqual((src.calls, self._journal("op-i")), (1, ["source-request-intent"]))
                r = self._run(kind, src, "op-i")
                self.assertEqual(src.calls, 2)
                self.assertEqual(len(set(src.request_ids)), 1, src.request_ids)
                self.assertEqual(self._journal("op-i"),
                                 ["source-request-intent", "source-request-resend-intent", "source-request-sent"])
                sent = self.store.op_journal("op-i")[-1]
                self.assertIs(sent.get("resend"), True)
                self.assertEqual(sent["request_id"], src.request_ids[0])
                self.assertEqual(self.surface(r, self._name(kind)).status, St.VERIFIED)
                self.assertEqual(ops.stuck_source_requests(self.store), ())

    def test_non_idempotent_adapter_is_never_resent_and_is_stuck(self) -> None:
        for kind in ("erasure", "correction"):
            with self.subTest(kind=kind):
                self.setUp()
                self.seed()
                src = CountingSource(self.src_root)
                self._crashed(kind, src, "op-n")
                r = self._run(kind, src, "op-n")
                self.assertEqual(src.calls, 1)
                s = self.surface(r, self._name(kind))
                self.assertEqual(s.status, St.UNVERIFIABLE)
                self.assertTrue(s.evidence.startswith("intent-only-stuck:"), s.evidence)
                self.assertEqual(self._journal("op-n"), ["source-request-intent"])
                self.assertEqual(ops.stuck_source_requests(self.store),
                                 (("op-n", (SRC_SYSTEM, SRC_OBJECT), src.request_ids[0]),))

    def test_a_source_changed_since_the_intent_is_never_resent(self) -> None:
        # Stale replay: the world moved between the crash and the resume.
        for kind, mutate in (
            ("correction", lambda f: f.write_text("third-party edit landed meanwhile\n")),
            ("erasure", lambda f: (f.unlink(), f.write_text("object re-created with new content\n"))),
        ):
            with self.subTest(kind=kind):
                self.setUp()
                self.seed()
                src = IdemSource(self.src_root)
                self._crashed(kind, src, "op-x")
                mutate(self.src_file)
                moved = self.src_file.read_bytes()
                r = self._run(kind, src, "op-x")
                self.assertEqual(src.calls, 1, "resent over a changed source")
                self.assertEqual(self.src_file.read_bytes(), moved)
                s = self.surface(r, self._name(kind))
                self.assertEqual(s.status, St.UNVERIFIABLE)
                self.assertTrue(s.evidence.startswith("intent-only-stuck:"), s.evidence)
                self.assertIn("source changed since the intent", s.evidence)
                self.assertEqual(self._journal("op-x"), ["source-request-intent"])
                self.assertEqual(len(ops.stuck_source_requests(self.store)), 1)

    def test_a_crash_during_the_resend_is_not_resent_again(self) -> None:
        for kind in ("erasure", "correction"):
            with self.subTest(kind=kind):
                self.setUp()
                self.seed()
                src = IdemSource(self.src_root)
                self._crashed(kind, src, "op-r")
                src.crash_before_send = True
                with self.assertRaises(ops.SimulatedCrash):
                    self._run(kind, src, "op-r")  # the ONE resend, which dies too
                src.crash_before_send = False
                self.assertEqual(src.calls, 2)
                self.assertEqual(self._journal("op-r"), ["source-request-intent", "source-request-resend-intent"])
                r = self._run(kind, src, "op-r")
                self.assertEqual(src.calls, 2, "a second resend was sent")
                s = self.surface(r, self._name(kind))
                self.assertEqual(s.status, St.UNVERIFIABLE)
                self.assertTrue(s.evidence.startswith("intent-only-stuck:"), s.evidence)
                self.assertIn("exhausted", s.evidence)
                self.assertEqual(len(ops.stuck_source_requests(self.store)), 1)

    def test_only_a_strict_true_declares_idempotency(self) -> None:
        for value in ("yes", 1, [1], object()):
            with self.subTest(value=repr(value)):
                self.setUp()
                self.seed()
                src = CountingSource(self.src_root)
                src.idempotent_request_ids = value
                self._crashed("erasure", src, "op-t")
                r = self._run("erasure", src, "op-t")
                self.assertEqual(src.calls, 1)
                self.assertTrue(self.surface(r, self._name("erasure")).evidence.startswith("intent-only-stuck:"))

    def test_the_fresh_read_showing_the_state_verifies_without_a_resend(self) -> None:
        self.seed()
        src = IdemSource(self.src_root)
        src.crash_after_send = True
        with self.assertRaises(ops.SimulatedCrash):
            self._run("erasure", src, "op-v")
        src.crash_after_send = False
        r = self._run("erasure", src, "op-v")
        self.assertEqual(src.calls, 1)
        self.assertEqual(self.surface(r, self._name("erasure")).status, St.VERIFIED)
        self.assertEqual(self._journal("op-v"), ["source-request-intent", "source-request-verified"])
        self.assertEqual(ops.stuck_source_requests(self.store), ())  # R1: settled by fresh read, not stuck
        self._run("erasure", src, "op-v")  # a re-run appends nothing more
        self.assertEqual(self._journal("op-v"), ["source-request-intent", "source-request-verified"])

    def test_a_stuck_op_settled_by_a_later_op_id_is_no_longer_listed(self) -> None:
        self.seed()
        src = CountingSource(self.src_root)
        self._crashed("erasure", src, "op-old")
        self.assertEqual(len(ops.stuck_source_requests(self.store)), 1)  # genuinely stuck
        # Operator resolution: the source was checked by hand, re-issue under a NEW op_id.
        new = self._run("erasure", CountingSource(self.src_root), "op-new")
        self.assertEqual(self.surface(new, self._name("erasure")).status, St.VERIFIED)
        self.assertEqual(ops.stuck_source_requests(self.store), ())

    def test_an_older_op_that_saw_an_earlier_source_state_does_not_hide_a_stuck_one(self) -> None:
        # Op A erased X; X was re-created; op B forgets it again and dies intent-only.
        self.seed()
        src_key = [SRC_SYSTEM, SRC_OBJECT]
        rid = f"op-a:source-erasure:{SRC_SYSTEM}/{SRC_OBJECT}"
        self.store.append_op_journal("op-a", {"record": "source-request-intent", "source": src_key,
                                              "request_id": rid, "before_digest": "sha256:" + "0" * 64})
        self.store.append_op_journal("op-a", {"record": "source-request-sent", "source": src_key, "request_id": rid})
        self._crashed("erasure", CountingSource(self.src_root), "op-b")
        self.assertEqual([o for o, _, _ in ops.stuck_source_requests(self.store)], ["op-b"])

    def test_an_id_with_neither_kind_marker_never_clears_a_stuck_one(self) -> None:
        self.seed()
        d = file_digest(self.src_file)
        self.store.append_op_journal("op-b", {"record": "source-request-intent", "source": [SRC_SYSTEM, SRC_OBJECT],
                                              "request_id": "opaque-b", "before_digest": d})
        for k in ("source-request-intent", "source-request-sent"):
            self.store.append_op_journal("op-z", {"record": k, "source": [SRC_SYSTEM, SRC_OBJECT],
                                                  "request_id": "opaque", "before_digest": d})
        self.assertEqual([o for o, _, _ in ops.stuck_source_requests(self.store)], ["op-b"])

    def test_a_later_op_of_another_kind_does_not_clear_a_stuck_one(self) -> None:
        self.seed()
        self._crashed("correction", CountingSource(self.src_root), "op-c1")
        self.store.append_op_journal("op-e", {"record": "source-request-sent", "source": [SRC_SYSTEM, SRC_OBJECT],
                                              "request_id": f"op-e:source-erasure:{SRC_SYSTEM}/{SRC_OBJECT}"})
        self.assertEqual([o for o, _, _ in ops.stuck_source_requests(self.store)], ["op-c1"])

    def test_a_sent_journal_is_never_resent_even_for_an_idempotent_adapter(self) -> None:
        self.seed()
        src = IdemSource(self.src_root)
        self.store.append_op_journal("op-s", {"record": "source-request-intent", "source": [SRC_SYSTEM, SRC_OBJECT],
                                              "request_id": "x", "before_digest": file_digest(self.src_file)})
        self.store.append_op_journal("op-s", {"record": "source-request-sent", "source": [SRC_SYSTEM, SRC_OBJECT],
                                              "request_id": "x", "receipt_digest": None, "after_digest": None})
        self.assertEqual(ops.stuck_source_requests(self.store), ())
        ref = source_ref(digest=file_digest(self.src_file))
        s = ops._source_request_surface(erasure=True, ref=ref, adapter=src, request_id="x", authority_ref=AUTH,
                                        store=self.store, operation_id="op-s")
        self.assertEqual((src.calls, s.status), (0, St.UNVERIFIABLE))
        self.assertFalse(s.evidence.startswith("intent-only-stuck:"))

    def test_a_completed_op_replay_never_calls_the_adapter_even_if_idempotent(self) -> None:
        for kind in ("erasure", "correction"):
            with self.subTest(kind=kind):
                self.setUp()
                self.seed()
                src = IdemSource(self.src_root)
                first = self._run(kind, src, "op-c")
                self.assertEqual(first.outcome, Outcome.VERIFIED_WITH_RETENTION)
                calls = src.calls
                self.assertEqual(self._run(kind, src, "op-c"), first)
                self.assertEqual(src.calls, calls)

    def test_file_source_declares_idempotency_and_canon_does_not(self) -> None:
        self.assertIs(ops.FileSource(self.src_root).idempotent_request_ids, True)
        self.assertIsNot(getattr(ops.ReadOnlyCanonSource(self.src_root), "idempotent_request_ids", False), True)


class TestIdempotentRepair(_Case):
    def _crash_at(self, step: str):
        # SimulatedCrash is a BaseException: it bypasses the op's Exception
        # handler exactly as a real process death would, so nothing is written.
        def fault(s: str) -> None:
            if s == step:
                raise ops.SimulatedCrash(step)

        return ops.SimulatedCrash, fault

    def test_rerun_after_crash_between_steps_repairs(self) -> None:
        for step in ("remove-derived:after-event", "remove-derived:after-payload-purge", "remove-derived:after-tombstone"):
            with self.subTest(step=step):
                mid = f"m-{step.rsplit(':', 1)[1]}"
                self.seed(mid, "v-1", TUESDAY, forgetting_policy="forget-tomb",
                          canonical_source_ref=source_ref(object_id=f"{mid}.md"))
                self.store.rebuild_projections()
                crash, fault = self._crash_at(step)
                with self.assertRaises(crash):
                    self.forget(Mode.REMOVE_DERIVED, targets=(mid,), operation_id=f"op-{mid}", sources={}, _fault=fault)
                self.assertEqual(self.store.receipt_paths(f"op-{mid}"), ())  # a real crash writes nothing
                self.assertNotIn(mid, {e.memory_id for e in self.store.retrievable_envelopes()})
                r = self.forget(Mode.REMOVE_DERIVED, targets=(mid,), operation_id=f"op-{mid}", sources={})
                self.assertValidForget(r)
                self.assertFalse((self.root / "payloads" / mid).exists())
                self.assertEqual(self.surface(r, f"tombstone:{mid}").status, St.RETAINED_BY_POLICY)
                self.assertNoTargetInAnyGeneration(self.store, mid, ("tuesday",))
                # only the (adapter-less) source surface keeps it from success
                self.assertEqual(r.unverifiable_surfaces, (f"source-unchanged:{SRC_SYSTEM}/{mid}.md",))

    def test_new_op_on_already_forgotten_target_completes(self) -> None:
        self.seed()
        self.assertEqual(self.forget(Mode.REMOVE_DERIVED).outcome, Outcome.VERIFIED_WITH_RETENTION)
        again = self.forget(Mode.REMOVE_DERIVED, operation_id="forget-2")
        self.assertValidForget(again)
        self.assertEqual(again.outcome, Outcome.VERIFIED_WITH_RETENTION)

    def test_subject_scope_fans_out(self) -> None:
        self.seed("m-1", "v-1", TUESDAY)
        self.seed("m-2", "v-1", THURSDAY, canonical_source_ref=source_ref(object_id="two.md"))
        self.seed("m-3", "v-1", "synthetic other", subjects=("other-team",), canonical_source_ref=source_ref(object_id="three.md"))
        r = self.forget(Mode.REMOVE_DERIVED, targets=(), subject_scope=(SUBJECT,))
        self.assertEqual(r.target_ids, ("m-1", "m-2"))
        self.assertEqual([e.memory_id for e in self.store.retrievable_envelopes()], ["m-3"])


# ===========================================================================
# T-4502 — the authorized RECLASSIFY op: narrowing self-serve, widening needs
# an operator ref. Synthetic data only.
# ===========================================================================
EXPECTED_RECLASSIFIABLE = frozenset({
    "subjects", "source_integrity", "claim_trust", "effective_trust_floor", "inherited_trust_floor",
    "trust_adjudication_receipt_refs", "version_id", "created_at", "supersedes", "superseded_by",
})
OTHER_SUBJECT = "other-team"
OPERATOR = "operator:tg:123"
# One claim over the reviewed evidence only (floor "reviewed") / over both
# pieces of evidence (meet "untrusted"); source_integrity stays SI1, SI2.
CLAIM_REVIEWED = me.ClaimTrust("c1", ("e1",), (), "reviewed", "reviewed", None)
CLAIM_UNTRUSTED = me.ClaimTrust("c1", ("e1", "e2"), (), "untrusted", "untrusted", None)
REVIEWED_FIELDS = dict(claim_trust=(CLAIM_REVIEWED,), inherited_trust_floor="reviewed", effective_trust_floor="reviewed")
UNTRUSTED_FIELDS = dict(claim_trust=(CLAIM_UNTRUSTED,), inherited_trust_floor="untrusted", effective_trust_floor="untrusted")


def bound_trust_policy(**kw) -> me.TrustPolicy:
    """The fixture lattice with its declared digest set to its recomputed
    CONTENT digest, as reclassify requires (R1: the policy is bound)."""
    p = dataclasses.replace(trust_policy(), **kw)
    return dataclasses.replace(p, digest=me.trust_policy_content_digest(p))


class _ReclassCase(_Case):
    def seed(self, memory_id: str = "m-1", version_id: str = "v-1", payload: str = TUESDAY, **kw):
        kw.setdefault("canonical_source_ref", self.live_ref())
        kw.setdefault("trust_policy_digest", bound_trust_policy().digest)
        env = make_env(memory_id, version_id, payload, **kw)
        res = self.store.append_version(env, payload, lifecycle_policy=self.policies[LC_REV],
                                        trust_policy=bound_trust_policy(), now=NOW)
        self.assertIsNone(res.refusal, res.detail)
        return env

    def seed_two_subjects(self, **kw) -> me.MemoryEnvelope:
        return self.seed(subjects=(SUBJECT, OTHER_SUBJECT), **kw)

    def rsucc(self, version_id: str = "v-2", prior: str = "v-1", **kw) -> me.MemoryEnvelope:
        """The prior with ONLY the successor identity replaced (content identical)."""
        prior_env = self.store.stored_envelope("m-1", prior)
        assert prior_env is not None
        return dataclasses.replace(prior_env, version_id=version_id, supersedes=(prior,), superseded_by=(), **kw)

    def reclassify(self, successor, *, operation_id: str = "rc-1", prior: str = "v-1", **kw) -> ops.ReclassifyReceipt:
        kw.setdefault("expected_version_digest", self.store.version_digest("m-1", prior) or "sha256:none")
        kw.setdefault("authority_ref", AUTH)
        kw.setdefault("lifecycle_policies", self.policies)
        kw.setdefault("trust_policy", bound_trust_policy())
        return ops.reclassify(
            self.store, operation_id=operation_id, memory_id="m-1", expected_version_id=prior,
            successor=successor, actor="test-actor", now=NOW, **kw,
        )

    def retrieve(self, **kw):
        req = retrieval_request(**kw)
        return ev.evaluate_retrieval(req, self.store.retrievable_envelopes(), trust_policy()).items

    def assertValidReclassify(self, r: ops.ReclassifyReceipt) -> None:
        d = ops.validate_reclassify_receipt(r)
        self.assertTrue(d.allowed, d.detail)


class TestReclassifyNarrowing(_ReclassCase):
    def test_removing_a_subject_is_narrowing_and_self_serve(self) -> None:
        self.seed_two_subjects()
        self.store.rebuild_projections()
        self.assertEqual(len(self.retrieve(subject_scope=(OTHER_SUBJECT,))), 1)
        r = self.reclassify(self.rsucc(subjects=(SUBJECT,)))
        self.assertIsNone(r.refusal, r.refusal_detail)
        self.assertValidReclassify(r)
        self.assertEqual((r.direction, r.changed_fields, r.widening_authority_ref), ("narrowing", ("subjects",), None))
        self.assertEqual((r.prior_version_id, r.successor_version_id), ("v-1", "v-2"))
        self.assertEqual(self.retrieve(subject_scope=(OTHER_SUBJECT,)), ())
        self.assertEqual([(i.memory_id, i.version_id) for i in self.retrieve()], [("m-1", "v-2")])
        self.assertEqual(self.store.lookup_subject(OTHER_SUBJECT), ())
        for p in r.index_purge_receipts:
            self.assertTrue(p.negative_lookup_verified, p.evidence)
        # nothing of the source was touched: no SOURCE surface
        self.assertNotIn(Kind.SOURCE, {s.kind for s in r.surfaces})
        self.assertEqual(self.store.read_payload("m-1", "v-2"), TUESDAY)

    def test_lowering_the_effective_floor_is_narrowing(self) -> None:
        self.seed(**REVIEWED_FIELDS)
        self.store.rebuild_projections()
        before = self.retrieve(purpose="assertion")
        self.assertEqual([i.use_disposition for i in before], [ev.UseDisposition.ALLOWED])
        r = self.reclassify(self.rsucc(**UNTRUSTED_FIELDS))
        self.assertIsNone(r.refusal, r.refusal_detail)
        self.assertValidReclassify(r)
        self.assertEqual(r.direction, "narrowing")
        self.assertEqual(r.changed_fields, ("claim_trust", "effective_trust_floor", "inherited_trust_floor"))
        after = self.retrieve(purpose="assertion")
        self.assertNotEqual([i.use_disposition for i in after], [ev.UseDisposition.ALLOWED])

    def test_a_malformed_ref_is_ignored_by_a_narrowing_and_not_recorded(self) -> None:
        self.seed_two_subjects()
        r = self.reclassify(self.rsucc(subjects=(SUBJECT,)), widening_authority_ref="orchestrator:tg:1")
        self.assertIsNone(r.refusal, r.refusal_detail)
        self.assertIsNone(r.widening_authority_ref)


class TestReclassifyWidening(_ReclassCase):
    def assertRefusedUntouched(self, r, code) -> None:
        self.assertEqual(r.refusal, code, r.refusal_detail)
        self.assertValidReclassify(r)
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-1",))
        self.assertIsNone(self.store.stored_envelope("m-1", "v-2"))

    def test_adding_a_subject_without_a_ref_is_refused_and_nothing_mutates(self) -> None:
        self.seed()
        before = tree_digest(self.root)
        r = self.reclassify(self.rsucc(subjects=(SUBJECT, OTHER_SUBJECT)))
        self.assertRefusedUntouched(r, LR.WIDENING_UNAUTHORIZED)
        self.assertEqual(r.direction, "widening")
        self.assertEqual(tree_digest(self.root), before)

    def test_raising_the_floor_without_a_ref_is_refused(self) -> None:
        self.seed()  # floor untrusted
        before = tree_digest(self.root)
        r = self.reclassify(self.rsucc(**REVIEWED_FIELDS))
        self.assertRefusedUntouched(r, LR.WIDENING_UNAUTHORIZED)
        self.assertEqual(tree_digest(self.root), before)

    def test_a_widening_with_a_operator_ref_succeeds_and_the_receipt_carries_it(self) -> None:
        self.seed()
        self.store.rebuild_projections()
        self.assertEqual(self.retrieve(subject_scope=(OTHER_SUBJECT,)), ())
        r = self.reclassify(self.rsucc(subjects=(SUBJECT, OTHER_SUBJECT)), widening_authority_ref=OPERATOR)
        self.assertIsNone(r.refusal, r.refusal_detail)
        self.assertValidReclassify(r)
        self.assertEqual((r.direction, r.widening_authority_ref, r.changed_fields), ("widening", OPERATOR, ("subjects",)))
        self.assertEqual(len(self.retrieve(subject_scope=(OTHER_SUBJECT,))), 1)

    def test_a_ticket_ref_also_authorizes(self) -> None:
        self.seed()
        r = self.reclassify(self.rsucc(**REVIEWED_FIELDS), widening_authority_ref="operator:T-4502")
        self.assertIsNone(r.refusal, r.refusal_detail)
        self.assertEqual(r.direction, "widening")

    def test_malformed_refs_are_refused(self) -> None:
        self.seed()
        for i, bad in enumerate(("operator:", "orchestrator:tg:1", "operator:tg:abc", "operator:tg:", "operator:T-12",
                                 "operator:tg:1\n", " operator:tg:1", "", AUTH)):
            with self.subTest(ref=bad):
                r = self.reclassify(self.rsucc(subjects=(SUBJECT, OTHER_SUBJECT)),
                                    operation_id=f"rc-bad-{i}", widening_authority_ref=bad)
                self.assertRefusedUntouched(r, LR.WIDENING_UNAUTHORIZED)

    def test_the_ref_never_substitutes_for_the_correction_authority(self) -> None:
        self.seed()
        r = self.reclassify(self.rsucc(subjects=(SUBJECT, OTHER_SUBJECT)), authority_ref="auth:intruder",
                            widening_authority_ref=OPERATOR)
        self.assertEqual(r.refusal, LR.AUTHORITY_MISMATCH)
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-1",))

    def test_a_mixed_change_is_a_widening(self) -> None:
        # drops one subject but raises the floor: not provably narrower
        self.seed_two_subjects()
        r = self.reclassify(self.rsucc(subjects=(SUBJECT,), **REVIEWED_FIELDS))
        self.assertRefusedUntouched(r, LR.WIDENING_UNAUTHORIZED)

    def test_incomparable_and_unknown_labels_are_widening(self) -> None:
        lat = me.TrustPolicy(
            policy_ref="P-lattice", revision="rev-1", digest="sha256:d",
            labels=frozenset({"untrusted", "reviewed", "audited"}),
            order_pairs=frozenset({("untrusted", "reviewed"), ("untrusted", "audited")}),
            conservative_state="untrusted", purpose_floors={"assertion": "reviewed"},
        )
        base = make_env(**REVIEWED_FIELDS)
        # reviewed vs audited are incomparable
        audited = dataclasses.replace(base, effective_trust_floor="audited")
        self.assertEqual(ops.reclassify_direction(base, audited, lat), "widening")
        self.assertEqual(ops.reclassify_direction(audited, base, lat), "widening")
        # an unknown label, and no policy at all
        unknown = dataclasses.replace(base, effective_trust_floor="made-up")
        self.assertEqual(ops.reclassify_direction(base, unknown, lat), "widening")
        low = dataclasses.replace(base, effective_trust_floor="untrusted")
        self.assertEqual(ops.reclassify_direction(base, low, lat), "narrowing")
        self.assertEqual(ops.reclassify_direction(base, low, None), "widening")
        # a claim the prior never had, and a shared claim whose floor rose
        extra = dataclasses.replace(base, claim_trust=(*base.claim_trust, me.ClaimTrust("c2", ("e1",), (), "reviewed", "reviewed", None)))
        self.assertEqual(ops.reclassify_direction(base, extra, lat), "widening")
        lower_env = dataclasses.replace(base, **UNTRUSTED_FIELDS)
        self.assertEqual(ops.reclassify_direction(base, lower_env, lat), "narrowing")
        self.assertEqual(ops.reclassify_direction(lower_env, base, lat), "widening")
        self.assertEqual(ops.reclassify_direction(base, base, lat), "unchanged")


class TestReclassifyBindsTheTrustPolicy(_ReclassCase):
    """R1: the lattice is the access gate, so the direction is computed only
    under the policy the prior pins, recomputed from content."""

    def assertMismatch(self, r) -> None:
        self.assertEqual(r.refusal, LR.TRUST_POLICY_MISMATCH, r.refusal_detail)
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-1",))
        self.assertIsNone(self.store.stored_envelope("m-1", "v-2"))

    def test_the_reversed_lattice_repro_is_refused_and_nothing_mutates(self) -> None:
        self.seed()  # floor untrusted
        before = tree_digest(self.root)
        good = bound_trust_policy()
        reversed_ = dataclasses.replace(good, order_pairs=frozenset({("reviewed", "untrusted")}))
        # raising untrusted -> reviewed would read as NARROWING under the reversed order
        self.assertEqual(ops.reclassify_direction(self.store.stored_envelope("m-1", "v-1"),
                                                  self.rsucc(**REVIEWED_FIELDS), reversed_), "narrowing")
        r = self.reclassify(self.rsucc(**REVIEWED_FIELDS), trust_policy=reversed_)
        self.assertMismatch(r)
        self.assertEqual(tree_digest(self.root), before)

    def test_a_reversed_lattice_that_also_rewrites_its_declared_digest_is_refused(self) -> None:
        # content == declared, but not what the prior pins
        self.seed()
        p = dataclasses.replace(bound_trust_policy(), order_pairs=frozenset({("reviewed", "untrusted")}))
        p = dataclasses.replace(p, digest=me.trust_policy_content_digest(p))
        self.assertMismatch(self.reclassify(self.rsucc(**REVIEWED_FIELDS), trust_policy=p))

    def test_a_relabel_or_an_added_order_pair_is_refused(self) -> None:
        self.seed_two_subjects(**REVIEWED_FIELDS)
        good = bound_trust_policy()
        tamper = [
            dataclasses.replace(good, labels=good.labels | {"audited"}),
            dataclasses.replace(good, order_pairs=good.order_pairs | {("reviewed", "untrusted")}),
            dataclasses.replace(good, purpose_floors={"assertion": "untrusted", "companionship": "untrusted"}),
        ]
        for i, p in enumerate(tamper):
            with self.subTest(i=i):
                self.assertMismatch(self.reclassify(self.rsucc(subjects=(SUBJECT,)), trust_policy=p, operation_id=f"rc-{i}"))

    def test_no_trust_policy_is_refused(self) -> None:
        self.seed_two_subjects()
        self.assertMismatch(self.reclassify(self.rsucc(subjects=(SUBJECT,)), trust_policy=None))

    def test_the_legit_policy_still_works(self) -> None:
        self.seed_two_subjects()
        r = self.reclassify(self.rsucc(subjects=(SUBJECT,)))
        self.assertIsNone(r.refusal, r.refusal_detail)


class TestReclassifySubjectsShape(_ReclassCase):
    def test_empty_subjects_are_refused_and_nothing_mutates(self) -> None:
        self.seed_two_subjects()
        before = tree_digest(self.root)
        r = self.reclassify(self.rsucc(subjects=()))
        self.assertEqual(r.refusal, LR.MALFORMED_INPUT, r.refusal_detail)
        self.assertEqual(tree_digest(self.root), before)

    def test_duplicate_subjects_are_refused_not_normalized(self) -> None:
        self.seed_two_subjects()
        before = tree_digest(self.root)
        r = self.reclassify(self.rsucc(subjects=(SUBJECT, SUBJECT)))
        self.assertEqual(r.refusal, LR.MALFORMED_INPUT, r.refusal_detail)
        self.assertEqual(tree_digest(self.root), before)
        r = self.reclassify(self.rsucc(subjects=(SUBJECT, OTHER_SUBJECT, SUBJECT)), operation_id="rc-2",
                            widening_authority_ref=OPERATOR)
        self.assertEqual(r.refusal, LR.MALFORMED_INPUT, r.refusal_detail)


class TestReclassifyNoContentChange(_ReclassCase):
    def test_a_payload_digest_change_is_refused(self) -> None:
        self.seed_two_subjects()
        before = tree_digest(self.root)
        r = self.reclassify(self.rsucc(subjects=(SUBJECT,), payload_digest=ms.payload_digest(THURSDAY)))
        self.assertEqual(r.refusal, LR.POLICY_FIELD_CHANGE, r.refusal_detail)
        self.assertIn("payload_digest", r.refusal_detail)
        self.assertEqual(tree_digest(self.root), before)

    def test_every_field_off_the_reclassify_allowlist_is_refused_fail_closed(self) -> None:
        # Structural (P1): iterate the dataclass, so a field added to
        # MemoryEnvelope later is refused until someone allowlists it.
        self.assertEqual(ops.RECLASSIFIABLE_FIELDS, EXPECTED_RECLASSIFIABLE)
        self.seed_two_subjects(privacy_class="personal")
        prior = self.store.stored_envelope("m-1", "v-1")
        assert prior is not None
        before = tree_digest(self.root)
        identical = [f.name for f in dataclasses.fields(me.MemoryEnvelope)
                     if f.name not in EXPECTED_RECLASSIFIABLE and f.name != "memory_id"]
        self.assertTrue({"privacy_class", "payload_digest", "payload_ref", "canonical_source_ref", "lifecycle_state",
                         "audience", "action_authority_ref", "policy_revision"} <= set(identical))
        for name in identical:
            with self.subTest(field=name):
                # a legal narrowing PLUS one forbidden change: never smuggled through
                old = getattr(prior, name)
                new = dataclasses.replace(old, source_revision="r2") if isinstance(old, me.SourceRef) else _changed(old)
                succ = self.rsucc(subjects=(SUBJECT,), **{name: new})
                r = self.reclassify(succ, operation_id=f"rc-{name}")
                self.assertIsNotNone(r.refusal, name)
                self.assertNotEqual(r.outcome, Outcome.VERIFIED_WITH_RETENTION)
                self.assertEqual(tree_digest(self.root), before)
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-1",))

    def test_every_reclassifiable_field_is_actually_changeable(self) -> None:
        # the other half of the allowlist: no listed field is silently refused
        self.seed_two_subjects(**REVIEWED_FIELDS)
        r = self.reclassify(self.rsucc(subjects=(SUBJECT,), created_at="2026-09-02T00:00:00Z", **UNTRUSTED_FIELDS))
        self.assertIsNone(r.refusal, r.refusal_detail)

    def test_an_unchanged_reclassify_is_refused_as_a_no_op(self) -> None:
        self.seed()
        r = self.reclassify(self.rsucc())
        self.assertEqual(r.refusal, LR.MALFORMED_INPUT)
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-1",))

    def test_a_successor_whose_floors_fail_validate_envelope_is_refused(self) -> None:
        self.seed(**REVIEWED_FIELDS)
        # floors lowered but not what source_integrity recomputes to
        bad = dict(UNTRUSTED_FIELDS, claim_trust=(dataclasses.replace(CLAIM_REVIEWED, effective_trust_floor="untrusted",
                                                                       inherited_trust_floor="untrusted"),))
        r = self.reclassify(self.rsucc(**bad))
        self.assertEqual(r.refusal, LR.ENVELOPE_INVALID, r.refusal_detail)


class TestReclassifyOpDiscipline(_ReclassCase):
    def test_an_identical_replay_returns_the_receipt_and_mutates_nothing(self) -> None:
        self.seed_two_subjects()
        succ = self.rsucc(subjects=(SUBJECT,))
        first = self.reclassify(succ)
        self.assertIsNone(first.refusal, first.refusal_detail)
        before = tree_digest(self.root, exclude=())
        again = self.reclassify(succ)
        self.assertEqual(again, first)
        self.assertEqual(tree_digest(self.root, exclude=()), before)

    def test_the_same_op_id_with_a_different_successor_is_operation_id_reuse(self) -> None:
        self.seed_two_subjects()
        self.assertIsNone(self.reclassify(self.rsucc(subjects=(SUBJECT,))).refusal)
        before = tree_digest(self.root)
        r = self.reclassify(self.rsucc(subjects=(OTHER_SUBJECT,)))
        self.assertEqual(r.refusal, LR.OPERATION_ID_REUSE, r.refusal_detail)
        self.assertEqual(tree_digest(self.root), before)

    def test_the_same_op_id_with_a_different_widening_ref_is_operation_id_reuse(self) -> None:
        self.seed()
        succ = self.rsucc(subjects=(SUBJECT, OTHER_SUBJECT))
        self.assertIsNone(self.reclassify(succ, widening_authority_ref=OPERATOR).refusal)
        r = self.reclassify(succ, widening_authority_ref="operator:tg:999")
        self.assertEqual(r.refusal, LR.OPERATION_ID_REUSE, r.refusal_detail)

    def test_cas_mismatch_is_refused(self) -> None:
        self.seed_two_subjects()
        before = tree_digest(self.root)
        r = self.reclassify(self.rsucc(subjects=(SUBJECT,)), expected_version_digest="sha256:" + "0" * 64)
        self.assertEqual(r.refusal, LR.STALE_VERSION)
        self.assertEqual(tree_digest(self.root), before)

    def test_a_superseded_expected_version_is_refused(self) -> None:
        self.seed_two_subjects()
        self.assertIsNone(self.reclassify(self.rsucc(subjects=(SUBJECT,))).refusal)
        r = self.reclassify(self.rsucc(version_id="v-3", subjects=(OTHER_SUBJECT,)), operation_id="rc-2")
        self.assertIsNotNone(r.refusal)
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-2",))

    def test_a_non_retrievable_head_is_refused_never_resurrected(self) -> None:
        self.seed_two_subjects()
        self.assertIsNone(self.forget(Mode.REMOVE_DERIVED).refusal)
        r = self.reclassify(self.rsucc(subjects=(SUBJECT,)))
        self.assertEqual(r.refusal, LR.ILLEGAL_TRANSITION, r.refusal_detail)
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ())

    def test_a_crash_after_admission_resumes_on_rerun(self) -> None:
        self.seed_two_subjects()
        succ = self.rsucc(subjects=(SUBJECT,))
        with mock.patch.object(self.store, "rebuild_projections", side_effect=OSError(errno.EIO, "x")):
            broken = self.reclassify(succ)
        self.assertNotIn(broken.outcome, (Outcome.VERIFIED, Outcome.VERIFIED_WITH_RETENTION))
        done = self.reclassify(succ)
        self.assertIsNone(done.refusal, done.refusal_detail)
        self.assertValidReclassify(done)
        self.assertEqual(self.store.retrievable_version_ids("m-1"), ("v-2",))


class TestReclassifyReceipt(_ReclassCase):
    def test_round_trips_validates_and_carries_no_values(self) -> None:
        self.seed(subjects=(SUBJECT, "zqxj-canary-subject"))
        r = self.reclassify(self.rsucc(subjects=(SUBJECT,)))
        self.assertIsNone(r.refusal, r.refusal_detail)
        raw = ops.reclassify_receipt_to_json(r)
        self.assertEqual(ops.reclassify_receipt_from_json(json.loads(json.dumps(raw))), r)
        self.assertEqual(grep_tree(self.root / "receipts", "zqxj-canary-subject"), [])
        self.assertEqual(grep_tree(self.root / "receipts", SUBJECT), [])

    def test_a_tampered_receipt_is_denied(self) -> None:
        self.seed()
        r = self.reclassify(self.rsucc(subjects=(SUBJECT, OTHER_SUBJECT)), widening_authority_ref=OPERATOR)
        self.assertValidReclassify(r)
        bad = dataclasses.replace(r, direction="narrowing")
        self.assertFalse(ops.validate_reclassify_receipt(bad).allowed)
        no_ref = dataclasses.replace(r, widening_authority_ref=None)
        self.assertFalse(ops.validate_reclassify_receipt(no_ref).allowed)
        no_ref = dataclasses.replace(no_ref, receipt_digest=ops.compute_reclassify_receipt_digest(no_ref))
        self.assertFalse(ops.validate_reclassify_receipt(no_ref).allowed)  # digest ok, still no authority
        val = dataclasses.replace(r, changed_fields=("subjects", "payload_digest"))
        val = dataclasses.replace(val, receipt_digest=ops.compute_reclassify_receipt_digest(val))
        self.assertFalse(ops.validate_reclassify_receipt(val).allowed)
        self.assertFalse(ops.validate_reclassify_receipt("x").allowed)


class TestCorrectIsUnchangedByReclassify(_ReclassCase):
    def test_correct_still_refuses_a_subject_change(self) -> None:
        self.seed_two_subjects()
        r = self.correct(reason=ops.CorrectionReason.SUBJECT_CORRECTION,
                         successor=self.successor(subjects=(SUBJECT,)))
        self.assertEqual(r.refusal, LR.POLICY_FIELD_CHANGE, r.refusal_detail)
        self.assertIn("subjects", r.refusal_detail)

    def test_the_correctable_allowlist_has_no_reclassify_only_field(self) -> None:
        self.assertEqual(ops.CORRECTABLE_FIELDS, EXPECTED_CORRECTABLE)
        self.assertFalse(ops.RECLASSIFY_SEMANTIC_FIELDS & {"subjects", "source_integrity", "claim_trust",
                                                          "effective_trust_floor"} - ops.RECLASSIFIABLE_FIELDS)
        for name in ("subjects", "source_integrity", "claim_trust", "effective_trust_floor"):
            self.assertNotIn(name, ops.CORRECTABLE_FIELDS)


if __name__ == "__main__":
    unittest.main()
