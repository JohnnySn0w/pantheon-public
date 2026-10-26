#!/usr/bin/env python3
"""Tests for the ORG-058 memory-envelope schema, trust lattice, lifecycle map,
and canonicalization/digest primitives (T-3197).

Covers: verbatim enum value sets; TrustPolicy meet (lattice GLB, absent-meet ->
None, unknown-label -> None, conservative-state/total-order construction
guards); privacy_admits; the §5 lifecycle transition map (legal/illegal/
terminal); dataclass immutability; canonical receipt-digest determinism +
golden round-trip + the non-self-referential exclusion property; and the
from_json parsers including the parallel-evidence-array rejection (H1.6 at
parse) and the incomplete-view-item construction that INV-7 validation catches.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_envelope as me  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "memory_envelope")


def _lattice_policy(**kw) -> me.TrustPolicy:
    d = dict(
        policy_ref="P-lattice",
        revision="rev-1",
        digest="sha256:d",
        labels=frozenset({"untrusted", "reviewed"}),
        order_pairs=frozenset({("untrusted", "reviewed")}),
        conservative_state="untrusted",
    )
    d.update(kw)
    return me.TrustPolicy(**d)


class TestEnumValues(unittest.TestCase):
    def test_confidence_kind_values_verbatim(self) -> None:
        self.assertEqual(
            [c.value for c in me.ConfidenceKind],
            ["observed", "asserted", "inferred", "generated"],
        )

    def test_use_disposition_values_verbatim(self) -> None:
        self.assertEqual(
            {d.value for d in me.UseDisposition},
            {"allowed", "corroboration_required", "quarantined", "refused"},
        )

    def test_memory_class_values_verbatim(self) -> None:
        self.assertEqual(
            {c.value for c in me.MemoryClass},
            {"working", "episodic", "semantic", "procedural", "relational", "identity_canon"},
        )

    def test_retention_shape_and_evidence_role(self) -> None:
        self.assertEqual({s.value for s in me.RetentionShape}, {"reference", "excerpt", "derived"})
        self.assertEqual(
            {r.value for r in me.EvidenceRole}, {"support", "contradiction", "quoted", "context"}
        )


class TestTrustPolicyMeet(unittest.TestCase):
    def test_meet_of_reviewed_and_untrusted_is_untrusted(self) -> None:
        p = _lattice_policy()
        self.assertEqual(p.meet(["reviewed", "untrusted"]), "untrusted")

    def test_meet_of_single_label_is_itself(self) -> None:
        self.assertEqual(_lattice_policy().meet(["reviewed"]), "reviewed")

    def test_meet_with_unknown_label_is_none(self) -> None:
        self.assertIsNone(_lattice_policy().meet(["reviewed", "mystery"]))

    def test_meet_of_empty_is_none(self) -> None:
        self.assertIsNone(_lattice_policy().meet([]))

    def test_absent_meet_incomparable_labels_is_none(self) -> None:
        # P-nomeet: two incomparable labels with no common lower bound.
        p = me.TrustPolicy("P-nomeet", "r", "d", frozenset({"a", "b"}), frozenset(), "a")
        self.assertIsNone(p.meet(["a", "b"]))

    def test_leq_reflexive_and_transitive(self) -> None:
        p = me.TrustPolicy(
            "P", "r", "d", frozenset({"lo", "mid", "hi"}),
            frozenset({("lo", "mid"), ("mid", "hi")}), "lo",
        )
        self.assertTrue(p.leq("lo", "lo"))
        self.assertTrue(p.leq("lo", "hi"))  # transitive
        self.assertFalse(p.leq("hi", "lo"))
        self.assertEqual(p.meet(["hi", "lo"]), "lo")

    def test_conservative_state_must_be_a_label(self) -> None:
        with self.assertRaises(me.SchemaError):
            _lattice_policy(conservative_state="ghost")

    def test_order_pair_unknown_label_raises(self) -> None:
        with self.assertRaises(me.SchemaError):
            me.TrustPolicy("P", "r", "d", frozenset({"a"}), frozenset({("a", "z")}), "a")

    def test_total_order_declared_but_incomparable_raises(self) -> None:
        with self.assertRaises(me.SchemaError):
            me.TrustPolicy("P", "r", "d", frozenset({"a", "b"}), frozenset(), "a", total_order=True)

    def test_purpose_floor_unknown_label_raises(self) -> None:
        with self.assertRaises(me.SchemaError):
            _lattice_policy(purpose_floors={"p": "ghost"})


class TestPrivacyAdmits(unittest.TestCase):
    def _p(self) -> me.TrustPolicy:
        return _lattice_policy(
            privacy_labels=frozenset({"internal", "personal"}),
            privacy_order_pairs=frozenset({("internal", "personal")}),
        )

    def test_ceiling_admits_less_sensitive(self) -> None:
        self.assertTrue(self._p().privacy_admits("personal", "internal"))

    def test_ceiling_denies_more_sensitive(self) -> None:
        self.assertFalse(self._p().privacy_admits("internal", "personal"))

    def test_unknown_privacy_label_denies(self) -> None:
        self.assertFalse(self._p().privacy_admits("personal", "secret"))

    def test_no_declared_privacy_lattice_requires_exact_match(self) -> None:
        p = _lattice_policy()  # no privacy labels
        self.assertTrue(p.privacy_admits("internal", "internal"))
        self.assertFalse(p.privacy_admits("personal", "internal"))


class TestLifecycleTransitions(unittest.TestCase):
    def test_legal_transitions(self) -> None:
        legal = [
            (me.LifecycleState.OBSERVED, me.LifecycleState.RETENTION_PROPOSED),
            (me.LifecycleState.RETENTION_PROPOSED, me.LifecycleState.RETAINED_DERIVED),
            (me.LifecycleState.RETAINED_DERIVED, me.LifecycleState.RETRIEVAL_CANDIDATE),
            (me.LifecycleState.RETAINED_SOURCE_REF, me.LifecycleState.RETRIEVAL_CANDIDATE),
            (me.LifecycleState.RETAINED_SOURCE_REF, me.LifecycleState.FORGOTTEN_DERIVED),  # T-4275
            (me.LifecycleState.RETRIEVAL_CANDIDATE, me.LifecycleState.MEMORY_VIEW),
            (me.LifecycleState.RETRIEVAL_CANDIDATE, me.LifecycleState.DENIED),
            (me.LifecycleState.MEMORY_VIEW, me.LifecycleState.ACTION_REQUESTED),
            (me.LifecycleState.ACTION_REQUESTED, me.LifecycleState.USE_EVALUATED),
        ]
        for src, dst in legal:
            self.assertTrue(me.is_legal_transition(src, dst), f"{src}->{dst} should be legal")

    def test_illegal_transitions(self) -> None:
        illegal = [
            (me.LifecycleState.OBSERVED, me.LifecycleState.MEMORY_VIEW),
            (me.LifecycleState.OBSERVED, me.LifecycleState.RETAINED_DERIVED),
            (me.LifecycleState.MEMORY_VIEW, me.LifecycleState.RETAINED_DERIVED),
            (me.LifecycleState.RETRIEVAL_CANDIDATE, me.LifecycleState.OBSERVED),
        ]
        for src, dst in illegal:
            self.assertFalse(me.is_legal_transition(src, dst), f"{src}->{dst} should be illegal")

    def test_terminal_states_have_no_exits(self) -> None:
        for terminal in (
            me.LifecycleState.REJECTED,
            me.LifecycleState.EXPIRES_UNRETAINED,
            me.LifecycleState.SUPERSEDED,
            me.LifecycleState.DECAYED,
            me.LifecycleState.FORGOTTEN_DERIVED,
            me.LifecycleState.DENIED,
            me.LifecycleState.USE_EVALUATED,
        ):
            self.assertEqual(me.LIFECYCLE_TRANSITIONS[terminal], frozenset())

    def test_every_state_is_mapped(self) -> None:
        self.assertEqual(set(me.LIFECYCLE_TRANSITIONS), set(me.LifecycleState))


class TestImmutability(unittest.TestCase):
    def test_source_ref_is_frozen(self) -> None:
        s = me.SourceRef("sys", "obj", "rev", "dig", "t", "v")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            s.object_id = "other"  # type: ignore[misc]


class TestReceiptDigest(unittest.TestCase):
    def _receipt(self, **kw) -> me.TrustAdjudicationReceipt:
        b = (
            me.EvidenceBinding("e1", "srev-1", "sha256:sd-e1"),
            me.EvidenceBinding("e2", "srev-2", "sha256:sd-e2"),
        )
        d = dict(
            receipt_schema_version=me.RECEIPT_SCHEMA_VERSION,
            canonicalization_ref=me.CANONICALIZATION_REF,
            receipt_id="R-1", adjudication_id="adj-1", memory_id="m-1", version_id="v-1",
            claim_ids=("c1",), proposer_ref="consolidator", adjudicator_ref="reviewer",
            authority_ref="auth:goal", trust_policy_ref="P-lattice", trust_policy_revision="rev-1",
            trust_policy_digest="sha256:d", input_evidence_bindings=b, derived_payload_digest="sha256:pd-1",
            before_inherited_floor="untrusted", before_effective_floor="untrusted",
            after_effective_floor="reviewed", purpose_scope=("assertion",), rationale="review",
            decision="raise", issued_at="2026-08-01T00:00:00Z", expires_at="2026-12-01T00:00:00Z",
            supersedes_receipt_ref=None, receipt_digest="",
        )
        d.update(kw)
        r = me.TrustAdjudicationReceipt(**d)
        return dataclasses.replace(r, receipt_digest=me.compute_receipt_digest(r))

    def test_digest_is_deterministic_and_prefixed(self) -> None:
        r = self._receipt()
        self.assertTrue(r.receipt_digest.startswith("sha256:"))
        self.assertEqual(r.receipt_digest, me.compute_receipt_digest(r))

    def test_digest_excludes_itself(self) -> None:
        # Two receipts identical except for the stored receipt_digest field must
        # produce the SAME computed digest — the field is not in the preimage.
        r = self._receipt()
        tampered = dataclasses.replace(r, receipt_digest="sha256:whatever")
        self.assertEqual(me.compute_receipt_digest(r), me.compute_receipt_digest(tampered))

    def test_digest_changes_when_a_bound_field_changes(self) -> None:
        r = self._receipt()
        moved = self._receipt(after_effective_floor="untrusted")
        self.assertNotEqual(r.receipt_digest, moved.receipt_digest)

    def test_golden_fixture_round_trips(self) -> None:
        with open(os.path.join(FIXTURES, "receipt_golden.json")) as f:
            golden = json.load(f)
        receipt = me.receipt_from_json(golden)
        # Recompute over the exclusive preimage; must equal the stored digest.
        self.assertEqual(me.compute_receipt_digest(receipt), receipt.receipt_digest)


class TestFromJson(unittest.TestCase):
    def test_receipt_rejects_parallel_evidence_arrays(self) -> None:
        with open(os.path.join(FIXTURES, "receipt_golden.json")) as f:
            good = json.load(f)
        bad = dict(good)
        bad.pop("input_evidence_bindings")
        bad["evidence_refs"] = ["e1", "e2"]
        bad["evidence_digests"] = ["sha256:sd-e1", "sha256:sd-e2"]
        with self.assertRaises(me.SchemaError):
            me.receipt_from_json(bad)

    def test_receipt_rejects_binding_missing_a_key(self) -> None:
        with open(os.path.join(FIXTURES, "receipt_golden.json")) as f:
            good = json.load(f)
        bad = dict(good)
        bad["input_evidence_bindings"] = [{"evidence_ref": "e1", "source_revision": "srev-1"}]
        with self.assertRaises(me.SchemaError):
            me.receipt_from_json(bad)

    def test_envelope_round_trips(self) -> None:
        with open(os.path.join(FIXTURES, "envelope_clean.json")) as f:
            env = me.envelope_from_json(json.load(f))
        self.assertEqual(env.memory_id, "m-1")
        self.assertEqual(env.inherited_trust_floor, "untrusted")
        self.assertEqual(len(env.claim_trust), 1)

    def test_policy_round_trips(self) -> None:
        with open(os.path.join(FIXTURES, "policy_lattice.json")) as f:
            policy = me.policy_from_json(json.load(f))
        self.assertEqual(policy.meet(["reviewed", "untrusted"]), "untrusted")
        self.assertEqual(policy.purpose_floors["assertion"], "reviewed")

    def test_view_item_may_be_constructed_incomplete(self) -> None:
        # INV-7 is a CHECK, not a type: an incomplete item must be constructible
        # from JSON so validate_memory_view can catch it.
        view = me.memory_view_from_json(
            {
                "request_id": "rq", "view_id": "v", "purpose": "p", "generated_at": "t",
                "items": [{"memory_id": "m", "version_id": "v"}],  # floors/disposition omitted
                "denied_count": 0,
            }
        )
        item = view.items[0]
        self.assertEqual(item.inherited_trust_floor, "")
        self.assertIsNone(item.use_disposition)


class TestTrustPolicyContentDigest(unittest.TestCase):
    def test_digest_is_stable_and_ignores_the_declared_digest(self) -> None:
        a = me.trust_policy_content_digest(_lattice_policy())
        self.assertRegex(a, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(a, me.trust_policy_content_digest(_lattice_policy(digest="sha256:other")))

    def test_changing_any_defining_field_changes_the_digest(self) -> None:
        base = me.trust_policy_content_digest(_lattice_policy())
        three = dict(labels=frozenset({"untrusted", "reviewed", "audited"}))
        variants = [
            dict(order_pairs=frozenset({("reviewed", "untrusted")})),  # reversed
            dict(order_pairs=frozenset()),
            dict(**three, order_pairs=frozenset({("untrusted", "reviewed"), ("untrusted", "audited")})),
            dict(labels=frozenset({"untrusted", "reviewed", "x"})),
            dict(revision="rev-2"),
            dict(conservative_state="reviewed"),
            dict(total_order=True),
            dict(self_adjudication_allowed=True),
            dict(purpose_floors={"p": "reviewed"}),
            dict(privacy_labels=frozenset({"a"})),
            dict(privacy_labels=frozenset({"a", "b"}), privacy_order_pairs=frozenset({("a", "b")})),
        ]
        for v in variants:
            with self.subTest(v=v):
                self.assertNotEqual(me.trust_policy_content_digest(_lattice_policy(**v)), base)


if __name__ == "__main__":
    unittest.main()
