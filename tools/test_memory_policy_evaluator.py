#!/usr/bin/env python3
"""Fixture-driven falsifier suite for the ORG-058 deny-by-default memory policy
evaluator (T-3197), implementing the T-2447 H1/H2 acceptance design.

One method per H1 red-case (H1.1..H1.8, including one per H1.5 receipt sub-
failure and per H1.6 binding/digest/canonicalization sub-failure) plus the
deny-by-default-unknown-policy default, then the H2 evaluator-level cases (meet
{reviewed,untrusted}=untrusted with no averaging/voting; high model confidence
over untrusted keeps the floor; contradiction stays visible; self-issued /
expired receipt leaves effective unchanged; a valid independently-authorized
receipt raises effective ONLY for the bound claim while inherited stays
untrusted, both survive retrieval, no source rewrite, no action authority) and
the authorize-before-rank retrieval invariants.

Builders are seeded from the report §6 field lists + the T-2447 typed surface;
the fixture policy uses the two-element lattice untrusted <= reviewed (a FIXTURE
lattice, NOT asserted as Pantheon's taxonomy).
"""

from __future__ import annotations

import dataclasses
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_envelope as me  # noqa: E402
import memory_policy_evaluator as ev  # noqa: E402
from memory_policy_evaluator import MemoryPolicyReason as R  # noqa: E402


# --- fixture policies -------------------------------------------------------
def policy_lattice(**kw) -> me.TrustPolicy:
    """P-lattice: untrusted <= reviewed, conservative=untrusted."""
    d = dict(
        policy_ref="P-lattice", revision="rev-1", digest="sha256:d",
        labels=frozenset({"untrusted", "reviewed"}),
        order_pairs=frozenset({("untrusted", "reviewed")}),
        conservative_state="untrusted",
        purpose_floors={"companionship": "untrusted", "assertion": "reviewed"},
        privacy_labels=frozenset({"internal", "personal"}),
        privacy_order_pairs=frozenset({("internal", "personal")}),
    )
    d.update(kw)
    return me.TrustPolicy(**d)


def policy_nomeet() -> me.TrustPolicy:
    """P-nomeet: two incomparable labels, no defined meet."""
    return me.TrustPolicy("P-nomeet", "rev-1", "sha256:d", frozenset({"alpha", "beta"}), frozenset(), "alpha")


# --- fixture building blocks ------------------------------------------------
SRC = me.SourceRef("echo", "obj-1", "srev-1", "sha256:sd-1", "2026-08-01T00:00:00Z", "echo-sig")
SI1 = me.SourceIntegrity("e1", "srev-1", "sha256:sd-e1", "reviewed", "P-lattice", "rev-1", "sha256:d", me.EvidenceRole.SUPPORT)
SI2 = me.SourceIntegrity("e2", "srev-2", "sha256:sd-e2", "untrusted", "P-lattice", "rev-1", "sha256:d", me.EvidenceRole.SUPPORT)


def base_env(**kw) -> me.MemoryEnvelope:
    """Valid envelope: claim c1 over reviewed(e1)+untrusted(e2) -> meet untrusted;
    effective == inherited == untrusted; no adjudication."""
    c1 = me.ClaimTrust("c1", ("e1", "e2"), (), "untrusted", "untrusted", None)
    d = dict(
        memory_id="m-1", version_id="v-1", memory_class=me.MemoryClass.SEMANTIC,
        lifecycle_state=me.LifecycleState.RETAINED_DERIVED, canonical_source_ref=SRC,
        created_at="2026-08-01T00:00:00Z", observed_at="2026-08-01T00:00:00Z",
        valid_from="2026-08-01T00:00:00Z", valid_to=None, owner="operator", subjects=("operator",),
        audience="self", privacy_class="internal", confidence_kind=me.ConfidenceKind.INFERRED,
        confidence=0.5, supporting_refs=("e1", "e2"), contradicting_refs=(), retention_until=None,
        correction_policy="supersede", forgetting_policy="remove-derived", retrieval_authority="self",
        action_authority_ref=None, supersedes=(), superseded_by=(), last_consolidated_at=None,
        payload_ref="payload://m-1", payload_digest="sha256:pd-1", policy_revision="rev-1",
        trust_policy_ref="P-lattice", trust_policy_revision="rev-1", trust_policy_digest="sha256:d",
        source_integrity=(SI1, SI2), claim_trust=(c1,), inherited_trust_floor="untrusted",
        effective_trust_floor="untrusted", trust_adjudication_receipt_refs=(),
    )
    d.update(kw)
    return me.MemoryEnvelope(**d)


def raised_env(**kw) -> me.MemoryEnvelope:
    """Envelope whose claim c1 has effective=reviewed under receipt R-1."""
    c1 = me.ClaimTrust("c1", ("e1", "e2"), (), "untrusted", "reviewed", "R-1")
    return base_env(
        claim_trust=(c1,), effective_trust_floor="reviewed",
        trust_adjudication_receipt_refs=("R-1",), **kw,
    )


def base_receipt(**kw) -> me.TrustAdjudicationReceipt:
    """Valid receipt R-1 binding claim c1 of raised_env; digest recomputed."""
    b = (
        me.EvidenceBinding("e1", "srev-1", "sha256:sd-e1"),
        me.EvidenceBinding("e2", "srev-2", "sha256:sd-e2"),
    )
    d = dict(
        receipt_schema_version=me.RECEIPT_SCHEMA_VERSION, canonicalization_ref=me.CANONICALIZATION_REF,
        receipt_id="R-1", adjudication_id="adj-1", memory_id="m-1", version_id="v-1", claim_ids=("c1",),
        proposer_ref="consolidator", adjudicator_ref="reviewer", authority_ref="auth:goal",
        trust_policy_ref="P-lattice", trust_policy_revision="rev-1", trust_policy_digest="sha256:d",
        input_evidence_bindings=b, derived_payload_digest="sha256:pd-1", before_inherited_floor="untrusted",
        before_effective_floor="untrusted", after_effective_floor="reviewed", purpose_scope=("assertion",),
        rationale="independent goal-authority review", decision="raise", issued_at="2026-08-01T00:00:00Z",
        expires_at="2026-12-01T00:00:00Z", supersedes_receipt_ref=None, receipt_digest="",
    )
    d.update(kw)
    r = me.TrustAdjudicationReceipt(**d)
    # Recompute the exclusive-preimage digest so ONLY the mutated field under
    # test is wrong (except for digest/self-referential cases, which override).
    if "receipt_digest" not in kw:
        r = dataclasses.replace(r, receipt_digest=me.compute_receipt_digest(r))
    return r


def retrieval_request(**kw) -> me.RetrievalRequest:
    d = dict(
        request_id="rq-1", caller_identity="bridge", persona="bridge", purpose="companionship",
        subject_scope=("operator",), memory_classes=(me.MemoryClass.SEMANTIC,), recency="recent",
        depth=3, token_budget=1000, item_budget=10, privacy_ceiling="personal", request_time="t",
    )
    d.update(kw)
    return me.RetrievalRequest(**d)


class TestBaselineValid(unittest.TestCase):
    """Sanity: the base fixtures must be ACCEPTED so red-cases isolate one guard."""

    def test_base_envelope_valid(self) -> None:
        self.assertTrue(ev.validate_envelope(base_env(), policy_lattice()).allowed)

    def test_raised_envelope_valid(self) -> None:
        # F1: a raised envelope is only valid when the bounding receipt is
        # supplied and verifies (fail-closed otherwise).
        self.assertTrue(
            ev.validate_envelope(raised_env(), policy_lattice(), receipts={"R-1": base_receipt()}).allowed
        )

    def test_base_receipt_valid(self) -> None:
        d = ev.validate_receipt(base_receipt(), raised_env(), policy_lattice())
        self.assertTrue(d.allowed, d.detail)


class TestH1(unittest.TestCase):
    P = staticmethod(policy_lattice)

    # H1.1 — derived claim missing trust_policy_ref or source labels -> deny
    def test_h1_1_missing_trust_policy_ref(self) -> None:
        env = base_env(trust_policy_ref="")
        self.assertEqual(ev.validate_envelope(env, self.P()).reason_code, R.MISSING_TRUST_POLICY_REF)

    def test_h1_1_missing_source_label(self) -> None:
        # claim contributes an evidence ref with no SourceIntegrity entry.
        c = me.ClaimTrust("c1", ("e1", "e_absent"), (), "untrusted", "untrusted", None)
        env = base_env(claim_trust=(c,))
        self.assertEqual(ev.validate_envelope(env, self.P()).reason_code, R.MISSING_INTEGRITY_METADATA)

    # H1.2 — missing or unknown meet -> deny / conservative
    def test_h1_2_unknown_policy(self) -> None:
        self.assertEqual(ev.validate_envelope(base_env(), None).reason_code, R.UNKNOWN_POLICY)

    def test_h1_2_absent_meet_refuses(self) -> None:
        p = policy_nomeet()
        si_a = me.SourceIntegrity("ea", "r", "d", "alpha", "P-nomeet", "rev-1", "sha256:d", me.EvidenceRole.SUPPORT)
        si_b = me.SourceIntegrity("eb", "r", "d", "beta", "P-nomeet", "rev-1", "sha256:d", me.EvidenceRole.SUPPORT)
        c = me.ClaimTrust("c1", ("ea", "eb"), (), "alpha", "alpha", None)
        env = base_env(
            trust_policy_ref="P-nomeet", trust_policy_revision="rev-1", trust_policy_digest="sha256:d",
            source_integrity=(si_a, si_b), claim_trust=(c,), inherited_trust_floor="alpha",
            effective_trust_floor="alpha",
        )
        self.assertEqual(ev.validate_envelope(env, p).reason_code, R.NO_MEET)

    # H1.3 — inherited floor mutated after creation vs recomputed value -> reject
    def test_h1_3_inherited_floor_mutated(self) -> None:
        c = me.ClaimTrust("c1", ("e1", "e2"), (), "reviewed", "reviewed", None)  # meet is untrusted
        env = base_env(claim_trust=(c,), inherited_trust_floor="reviewed", effective_trust_floor="reviewed")
        self.assertEqual(ev.validate_envelope(env, self.P()).reason_code, R.INHERITED_FLOOR_MISMATCH)

    # H1.4 — effective floor != inherited with NO receipt -> reject
    def test_h1_4_effective_without_receipt(self) -> None:
        c = me.ClaimTrust("c1", ("e1", "e2"), (), "untrusted", "reviewed", None)  # no receipt ref
        env = base_env(claim_trust=(c,), effective_trust_floor="reviewed", trust_adjudication_receipt_refs=())
        self.assertEqual(ev.validate_envelope(env, self.P()).reason_code, R.EFFECTIVE_WITHOUT_RECEIPT)

    def test_h1_4_env_level_effective_without_receipt(self) -> None:
        # Envelope-level raise with no claims and no receipts (isolates the
        # envelope-level guard, distinct from the per-claim guard above).
        env = base_env(claim_trust=(), inherited_trust_floor="untrusted", effective_trust_floor="reviewed")
        self.assertEqual(ev.validate_envelope(env, self.P()).reason_code, R.EFFECTIVE_WITHOUT_RECEIPT)

    # H1.5 — receipt wrong on each binding facet (one sub-case each)
    def test_h1_5_authority(self) -> None:
        r = base_receipt(authority_ref="")
        self.assertEqual(ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_AUTHORITY_INVALID)

    def test_h1_5_authority_not_independent(self) -> None:
        r = base_receipt(authority_ref="consolidator")  # == proposer
        self.assertEqual(ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_AUTHORITY_INVALID)

    def test_h1_5_self_adjudication(self) -> None:
        r = base_receipt(proposer_ref="same", adjudicator_ref="same", authority_ref="auth:goal")
        self.assertEqual(ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_SELF_ADJUDICATION)

    def test_h1_5_version(self) -> None:
        r = base_receipt(version_id="v-OTHER")
        self.assertEqual(ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_VERSION_MISMATCH)

    def test_h1_5_claim(self) -> None:
        r = base_receipt(claim_ids=("c-OTHER",))
        self.assertEqual(ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_CLAIM_UNBOUND)

    def test_h1_5_bound_evidence_entry(self) -> None:
        b = (me.EvidenceBinding("e1", "srev-1", "sha256:sd-e1"), me.EvidenceBinding("e2", "srev-2", "sha256:WRONG"))
        r = base_receipt(input_evidence_bindings=b)
        self.assertEqual(ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_EVIDENCE_MISMATCH)

    def test_h1_5_policy_revision(self) -> None:
        r = base_receipt(trust_policy_revision="rev-OTHER")
        self.assertEqual(ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_POLICY_MISMATCH)

    def test_h1_5_policy_digest(self) -> None:
        r = base_receipt(trust_policy_digest="sha256:OTHER")
        self.assertEqual(ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_POLICY_MISMATCH)

    def test_h1_5_purpose(self) -> None:
        r = base_receipt(purpose_scope=())
        self.assertEqual(ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_PURPOSE_MISMATCH)

    def test_h1_5_time_window(self) -> None:
        r = base_receipt(issued_at="2026-12-01T00:00:00Z", expires_at="2026-08-01T00:00:00Z")
        self.assertEqual(ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_EXPIRED)

    def test_h1_5_supersession(self) -> None:
        # Receipt R-1 is referenced by the claim, but is NOT among the envelope's
        # active adjudication receipts (it was superseded / removed).
        c = me.ClaimTrust("c1", ("e1", "e2"), (), "untrusted", "reviewed", "R-1")
        env = base_env(claim_trust=(c,), effective_trust_floor="reviewed", trust_adjudication_receipt_refs=())
        self.assertEqual(ev.validate_receipt(base_receipt(), env, self.P()).reason_code, R.RECEIPT_SUPERSEDED)

    def test_h1_5_payload_digest(self) -> None:
        r = base_receipt(derived_payload_digest="sha256:OTHER")
        self.assertEqual(
            ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_PAYLOAD_DIGEST_MISMATCH
        )

    # H1.6 — parallel arrays / missing / duplicate binding / self-ref digest / canon
    def test_h1_6_parallel_arrays_rejected_at_parse(self) -> None:
        with self.assertRaises(me.SchemaError):
            me.receipt_from_json(
                {
                    "receipt_schema_version": me.RECEIPT_SCHEMA_VERSION,
                    "canonicalization_ref": me.CANONICALIZATION_REF, "receipt_id": "R-1",
                    "adjudication_id": "a", "memory_id": "m-1", "version_id": "v-1", "claim_ids": ["c1"],
                    "proposer_ref": "p", "adjudicator_ref": "j", "authority_ref": "auth",
                    "trust_policy_ref": "P-lattice", "trust_policy_revision": "rev-1",
                    "trust_policy_digest": "sha256:d",
                    # Both a valid bindings list AND parallel arrays are present:
                    # the parallel-array shape must be rejected on its own, not
                    # only because bindings happen to be missing.
                    "input_evidence_bindings": [
                        {"evidence_ref": "e1", "source_revision": "srev-1", "digest": "sha256:sd-e1"},
                        {"evidence_ref": "e2", "source_revision": "srev-2", "digest": "sha256:sd-e2"},
                    ],
                    "evidence_refs": ["e1", "e2"], "evidence_digests": ["sha256:sd-e1", "sha256:sd-e2"],
                    "derived_payload_digest": "sha256:pd-1", "before_inherited_floor": "untrusted",
                    "before_effective_floor": "untrusted", "after_effective_floor": "reviewed",
                    "purpose_scope": ["assertion"], "rationale": "r", "decision": "raise",
                    "issued_at": "2026-08-01T00:00:00Z", "expires_at": "2026-12-01T00:00:00Z",
                    "supersedes_receipt_ref": None, "receipt_digest": "sha256:x",
                }
            )

    def test_h1_6_missing_binding(self) -> None:
        b = (me.EvidenceBinding("e1", "srev-1", "sha256:sd-e1"),)  # e2 missing
        r = base_receipt(input_evidence_bindings=b)
        self.assertEqual(ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_EVIDENCE_MISMATCH)

    def test_h1_6_duplicate_binding(self) -> None:
        b = (
            me.EvidenceBinding("e1", "srev-1", "sha256:sd-e1"),
            me.EvidenceBinding("e1", "srev-1", "sha256:sd-e1"),
            me.EvidenceBinding("e2", "srev-2", "sha256:sd-e2"),
        )
        r = base_receipt(input_evidence_bindings=b)
        self.assertEqual(ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_EVIDENCE_MISMATCH)

    def test_h1_6_self_referential_digest(self) -> None:
        r = base_receipt()
        # Digest computed over the INCLUSIVE preimage (bytes that include the
        # receipt_digest field) can never match the exclusive recompute.
        inclusive = "sha256:" + __import__("hashlib").sha256(
            me.canonical_json_bytes(me.receipt_to_json(r, include_digest=True))
        ).hexdigest()
        bad = dataclasses.replace(r, receipt_digest=inclusive)
        self.assertEqual(ev.validate_receipt(bad, raised_env(), self.P()).reason_code, R.RECEIPT_DIGEST_MISMATCH)

    def test_h1_6_wrong_canonicalization(self) -> None:
        r = base_receipt(canonicalization_ref="other-canon/v9")
        self.assertEqual(
            ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_WRONG_CANONICALIZATION
        )

    # H1.7 — a foreign-dimension value copied into a trust field -> reject (INV-1)
    def test_h1_7_confidence_kind_in_trust_field(self) -> None:
        env = base_env(inherited_trust_floor="inferred")  # epistemic value, not an integrity label
        self.assertEqual(
            ev.validate_envelope(env, self.P()).reason_code, R.DIMENSION_CROSS_CONTAMINATION
        )

    def test_h1_7_action_authority_in_trust_field(self) -> None:
        env = base_env(action_authority_ref="cap:write", effective_trust_floor="cap:write")
        self.assertEqual(
            ev.validate_envelope(env, self.P()).reason_code, R.DIMENSION_CROSS_CONTAMINATION
        )

    def test_h1_7_privacy_class_in_trust_field(self) -> None:
        env = base_env(inherited_trust_floor="internal")  # privacy value copied in
        self.assertEqual(
            ev.validate_envelope(env, self.P()).reason_code, R.DIMENSION_CROSS_CONTAMINATION
        )

    # H1.8 — MemoryView returns content but omits floor / use_disposition -> reject
    def test_h1_8_view_missing_floor_rejected(self) -> None:
        view = me.memory_view_from_json(
            {
                "request_id": "rq", "view_id": "v", "purpose": "assertion", "generated_at": "t",
                "items": [
                    {
                        "memory_id": "m-1", "version_id": "v-1", "trust_policy_ref": "P-lattice",
                        "effective_trust_floor": "reviewed", "use_disposition": "allowed",
                        # inherited_trust_floor omitted
                    }
                ],
                "denied_count": 0,
            }
        )
        self.assertEqual(ev.validate_memory_view(view).reason_code, R.VIEW_INCOMPLETE)

    def test_h1_8_view_missing_disposition_rejected(self) -> None:
        view = me.memory_view_from_json(
            {
                "request_id": "rq", "view_id": "v", "purpose": "assertion", "generated_at": "t",
                "items": [
                    {
                        "memory_id": "m-1", "version_id": "v-1", "trust_policy_ref": "P-lattice",
                        "inherited_trust_floor": "untrusted", "effective_trust_floor": "reviewed",
                        # use_disposition omitted
                    }
                ],
                "denied_count": 0,
            }
        )
        self.assertEqual(ev.validate_memory_view(view).reason_code, R.VIEW_INCOMPLETE)

    def test_h1_8_complete_view_allowed(self) -> None:
        view = ev.evaluate_retrieval(retrieval_request(), [base_env()], self.P())
        self.assertTrue(view.items)
        self.assertTrue(ev.validate_memory_view(view).allowed)

    # deny-by-default default: unknown policy always denies / conservative
    def test_unknown_policy_default_denies_everywhere(self) -> None:
        self.assertEqual(ev.validate_envelope(base_env(), None).reason_code, R.UNKNOWN_POLICY)
        self.assertEqual(ev.validate_receipt(base_receipt(), raised_env(), None).reason_code, R.RECEIPT_POLICY_MISMATCH)
        self.assertIsNone(ev.compute_inherited_floor(base_env().claim_trust[0], base_env().source_integrity, None))
        view = ev.evaluate_retrieval(retrieval_request(), [base_env()], None)
        self.assertEqual(view.items, ())
        self.assertEqual(view.omitted_reason_counts.get("unknown_policy"), 1)


class TestH2(unittest.TestCase):
    P = staticmethod(policy_lattice)

    # H2.1/H2.2 — meet {reviewed, untrusted} = untrusted; no averaging/voting
    def test_meet_reviewed_untrusted_is_untrusted(self) -> None:
        env = base_env()
        floor = ev.compute_inherited_floor(env.claim_trust[0], env.source_integrity, self.P())
        self.assertEqual(floor, "untrusted")

    def test_no_averaging_two_untrusted_one_reviewed(self) -> None:
        si3 = me.SourceIntegrity("e3", "srev-3", "sha256:sd-e3", "untrusted", "P-lattice", "rev-1", "sha256:d", me.EvidenceRole.SUPPORT)
        c = me.ClaimTrust("c1", ("e1", "e2", "e3"), (), "untrusted", "untrusted", None)
        env = base_env(source_integrity=(SI1, SI2, si3), claim_trust=(c,))
        self.assertEqual(
            ev.compute_inherited_floor(env.claim_trust[0], env.source_integrity, self.P()), "untrusted"
        )
        self.assertTrue(ev.validate_envelope(env, self.P()).allowed)

    # H2.4 — high model confidence over untrusted keeps floor untrusted
    def test_high_confidence_does_not_raise_floor(self) -> None:
        env = base_env(confidence=0.99)
        d = ev.validate_envelope(env, self.P())
        self.assertTrue(d.allowed)
        self.assertEqual(d.inherited_floor, "untrusted")
        self.assertEqual(d.effective_floor, "untrusted")

    def test_confidence_cannot_authorize_effective_floor(self) -> None:
        # A raised claim (effective=reviewed, no receipt) alongside an untrusted
        # claim that pulls the envelope meet back to untrusted: the per-claim
        # guard must still refuse, and NO amount of model confidence authorizes
        # the raise (INV-1 / T-2447 sabotage 2).
        c1 = me.ClaimTrust("c1", ("e2",), (), "untrusted", "reviewed", None)
        c2 = me.ClaimTrust("c2", ("e2",), (), "untrusted", "untrusted", None)
        env = base_env(
            claim_trust=(c1, c2), inherited_trust_floor="untrusted",
            effective_trust_floor="untrusted", trust_adjudication_receipt_refs=(), confidence=0.99,
        )
        self.assertEqual(ev.validate_envelope(env, self.P()).reason_code, R.EFFECTIVE_WITHOUT_RECEIPT)

    # H2.3 — contradiction stays visible & independent; floor unchanged
    def test_contradiction_visible_and_independent(self) -> None:
        si_c = me.SourceIntegrity("e_contra", "r", "d", "reviewed", "P-lattice", "rev-1", "sha256:d", me.EvidenceRole.CONTRADICTION)
        c = me.ClaimTrust("c1", ("e1", "e2"), ("e_contra",), "untrusted", "untrusted", None)
        env = base_env(source_integrity=(SI1, SI2, si_c), claim_trust=(c,), contradicting_refs=("e_contra",))
        self.assertTrue(ev.validate_envelope(env, self.P()).allowed)
        view = ev.evaluate_retrieval(retrieval_request(), [env], self.P())
        self.assertEqual(view.items[0].contradiction_notice, "contradicted")
        self.assertEqual(view.items[0].inherited_trust_floor, "untrusted")

    # H2.6 — self-issued / unauthorized receipt leaves effective unchanged
    def test_self_issued_receipt_denied(self) -> None:
        r = base_receipt(proposer_ref="agent", adjudicator_ref="agent", authority_ref="auth:goal")
        d = ev.validate_receipt(r, raised_env(), self.P())
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason_code, R.RECEIPT_SELF_ADJUDICATION)
        # Because the receipt is invalid, the reverted state (effective==inherited,
        # no receipt) is what validates — the floor is NOT raised.
        self.assertTrue(ev.validate_envelope(base_env(), self.P()).allowed)

    # H2.7 — expired receipt leaves floor unchanged (exact-binding time check)
    def test_expired_receipt_denied(self) -> None:
        d = ev.validate_receipt(base_receipt(), raised_env(), self.P(), now="2027-01-01T00:00:00Z")
        self.assertEqual(d.reason_code, R.RECEIPT_EXPIRED)

    # H2.8 — valid independently-authorized receipt: effective=reviewed ONLY for
    # the bound claim; inherited stays untrusted; both survive retrieval; no
    # source rewrite; no action authority.
    def test_valid_receipt_raises_effective_only_for_bound_claim(self) -> None:
        p = self.P()
        env = raised_env()
        receipt = base_receipt()
        rd = ev.validate_receipt(receipt, env, p)
        self.assertTrue(rd.allowed, rd.detail)
        self.assertEqual(rd.inherited_floor, "untrusted")
        self.assertEqual(rd.effective_floor, "reviewed")

        ed = ev.validate_envelope(env, p, receipts={"R-1": receipt})
        self.assertTrue(ed.allowed)
        self.assertEqual(ed.inherited_floor, "untrusted")   # inherited immutable
        self.assertEqual(ed.effective_floor, "reviewed")    # raised only via receipt

    def test_valid_receipt_both_floors_survive_retrieval_no_action_authority(self) -> None:
        p = self.P()
        env = raised_env()
        source_before = env.canonical_source_ref
        view = ev.evaluate_retrieval(retrieval_request(purpose="assertion"), [env], p)
        self.assertTrue(ev.validate_memory_view(view).allowed)
        item = view.items[0]
        self.assertEqual(item.inherited_trust_floor, "untrusted")
        self.assertEqual(item.effective_trust_floor, "reviewed")
        self.assertEqual(item.use_disposition, me.UseDisposition.ALLOWED)
        # INV-5: the evaluator never rewrites the source.
        self.assertIs(env.canonical_source_ref, source_before)
        # INV-2: no action-authority field is conferred by retrieval.
        self.assertFalse(any("action" in f.name for f in dataclasses.fields(item)))


class TestRetrievalAuthorizeBeforeRank(unittest.TestCase):
    P = staticmethod(policy_lattice)

    def test_privacy_denied_item_is_aggregate_only(self) -> None:
        # A too-sensitive item is denied and leaks ONLY as denied_count — never
        # as an item, snippet, or floor (INV-6).
        allowed_env = base_env(memory_id="m-ok", privacy_class="internal")
        secret_env = base_env(memory_id="m-secret", privacy_class="personal")
        req = retrieval_request(privacy_ceiling="internal", purpose="companionship")
        view = ev.evaluate_retrieval(req, [allowed_env, secret_env], self.P())
        self.assertEqual([i.memory_id for i in view.items], ["m-ok"])
        self.assertEqual(view.denied_count, 1)
        self.assertFalse(any(i.memory_id == "m-secret" for i in view.items))

    def test_reviewed_only_purpose_quarantines_untrusted(self) -> None:
        # effective=untrusted content under a reviewed-only purpose is returned
        # QUARANTINED (with its floor), never ALLOWED (sabotage-5 target).
        view = ev.evaluate_retrieval(retrieval_request(purpose="assertion"), [base_env()], self.P())
        self.assertEqual(len(view.items), 1)
        self.assertEqual(view.items[0].use_disposition, me.UseDisposition.QUARANTINED)

    def test_unmapped_purpose_refuses_all(self) -> None:
        view = ev.evaluate_retrieval(retrieval_request(purpose="no-such-purpose"), [base_env()], self.P())
        self.assertEqual(view.items, ())
        self.assertEqual(view.omitted_reason_counts.get("below_purpose_floor"), 1)

    def test_out_of_subject_scope_omitted(self) -> None:
        view = ev.evaluate_retrieval(retrieval_request(subject_scope=("someone-else",)), [base_env()], self.P())
        self.assertEqual(view.items, ())
        self.assertEqual(view.omitted_reason_counts.get("out_of_subject_scope"), 1)

    def test_malformed_request_returns_empty_view(self) -> None:
        view = ev.evaluate_retrieval(object(), [base_env()], self.P())
        self.assertEqual(view.items, ())
        self.assertEqual(view.omitted_reason_counts.get("malformed_request"), 1)

    # T-4810 — pin the content-blind contract.
    def test_token_count_is_ref_length_not_content_length(self) -> None:
        # Equal refs, wildly different payload digests/content stand-ins =>
        # identical token_count; it is the sum of len(payload_ref) over items.
        a = base_env(memory_id="m-a", payload_ref="payload://same", payload_digest="sha256:" + "a" * 10)
        b = base_env(memory_id="m-b", payload_ref="payload://same", payload_digest="sha256:" + "b" * 5000)
        v_small = ev.evaluate_retrieval(retrieval_request(), [a], self.P())
        v_big = ev.evaluate_retrieval(retrieval_request(), [b], self.P())
        self.assertEqual(v_small.token_count, len("payload://same"))
        self.assertEqual(v_small.token_count, v_big.token_count)
        both = ev.evaluate_retrieval(retrieval_request(), [a, b], self.P())
        self.assertEqual(both.token_count, sum(len(i.bounded_content) for i in both.items))
        self.assertEqual([i.bounded_content for i in both.items], ["payload://same"] * 2)

    def test_order_is_memory_id_version_id_regardless_of_input_order(self) -> None:
        envs = [
            base_env(memory_id="m-b", version_id="v-1", payload_ref="payload://zzzzzzzz"),
            base_env(memory_id="m-a", version_id="v-2"),
            base_env(memory_id="m-a", version_id="v-1"),
        ]
        want = [("m-a", "v-1"), ("m-a", "v-2"), ("m-b", "v-1")]
        for perm in (envs, envs[::-1], [envs[1], envs[2], envs[0]]):
            view = ev.evaluate_retrieval(retrieval_request(), perm, self.P())
            self.assertEqual([(i.memory_id, i.version_id) for i in view.items], want)


class TestRetention(unittest.TestCase):
    def _proposal(self, **kw) -> me.RetentionProposal:
        d = dict(
            candidate_id="cand-1", proposed_class=me.MemoryClass.SEMANTIC, canonical_source_ref=SRC,
            retention_shape=me.RetentionShape.DERIVED, owner="operator", subjects=("operator",),
            audience="self", privacy_class="internal", confidence_kind=me.ConfidenceKind.INFERRED,
            retention_until=None, correction_policy="supersede", forgetting_policy="remove-derived",
            required_authority="auth:goal", rationale="derive a companion fact",
        )
        d.update(kw)
        return me.RetentionProposal(**d)

    def test_retention_authorized(self) -> None:
        self.assertTrue(ev.evaluate_retention(self._proposal(), "auth:goal", policy_lattice()).allowed)

    def test_retention_missing_authority_denied(self) -> None:
        d = ev.evaluate_retention(self._proposal(), "wrong-authority", policy_lattice())
        self.assertEqual(d.reason_code, R.RETENTION_AUTHORITY_MISSING)

    def test_retention_missing_canonical_source_denied(self) -> None:
        bad_src = me.SourceRef("", "", "r", "d", "t", "v")
        d = ev.evaluate_retention(self._proposal(canonical_source_ref=bad_src), "auth:goal", policy_lattice())
        self.assertEqual(d.reason_code, R.RETENTION_MISSING_CANONICAL_SOURCE)

    def test_malformed_input_denied(self) -> None:
        self.assertEqual(ev.evaluate_retention(object(), "auth", policy_lattice()).reason_code, R.MALFORMED_INPUT)


def dangling_env() -> me.MemoryEnvelope:
    """Laundering shape (F1): claim c1 references receipt R-1 whose claim_ids
    bind ONLY c2. c1's raise is unadjudicated but the ref exists and R-1 is a
    real, otherwise-valid receipt for c2."""
    c1 = me.ClaimTrust("c1", ("e2",), (), "untrusted", "reviewed", "R-1")  # laundered
    c2 = me.ClaimTrust("c2", ("e2",), (), "untrusted", "reviewed", "R-1")  # legitimately bound
    return base_env(
        claim_trust=(c1, c2), inherited_trust_floor="untrusted", effective_trust_floor="reviewed",
        trust_adjudication_receipt_refs=("R-1",),
    )


def receipt_binds_c2() -> me.TrustAdjudicationReceipt:
    b = (me.EvidenceBinding("e2", "srev-2", "sha256:sd-e2"),)
    return base_receipt(claim_ids=("c2",), input_evidence_bindings=b)


class TestFixPass(unittest.TestCase):
    P = staticmethod(policy_lattice)

    # F1 — HIGH: laundered trust floor via a dangling receipt ref.
    def test_f1_dangling_ref_denied_at_receipt(self) -> None:
        d = ev.validate_receipt(receipt_binds_c2(), dangling_env(), self.P())
        self.assertEqual(d.reason_code, R.RECEIPT_DOES_NOT_BIND_CLAIM)

    def test_f1_dangling_ref_denied_at_envelope(self) -> None:
        d = ev.validate_envelope(dangling_env(), self.P(), receipts={"R-1": receipt_binds_c2()})
        self.assertEqual(d.reason_code, R.RECEIPT_DOES_NOT_BIND_CLAIM)

    def test_f1_raised_claim_without_receipts_fails_closed(self) -> None:
        # No receipts provided => the raise cannot be verified => deny.
        self.assertEqual(
            ev.validate_envelope(raised_env(), self.P()).reason_code, R.EFFECTIVE_WITHOUT_RECEIPT
        )

    def test_f1_raised_claim_with_valid_receipt_allowed(self) -> None:
        self.assertTrue(
            ev.validate_envelope(raised_env(), self.P(), receipts={"R-1": base_receipt()}).allowed
        )

    def test_f1_raised_claim_with_missing_receipt_in_map_fails_closed(self) -> None:
        # ref present but the receipt object is not among those supplied.
        self.assertEqual(
            ev.validate_envelope(raised_env(), self.P(), receipts={}).reason_code,
            R.EFFECTIVE_WITHOUT_RECEIPT,
        )

    # F2 — before_effective_floor validated.
    def test_f2_foreign_before_effective_floor_rejected(self) -> None:
        r = base_receipt(before_effective_floor="inferred")  # not an integrity label
        self.assertEqual(
            ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_BEFORE_FLOOR_MISMATCH
        )

    def test_f2_before_effective_below_inherited_rejected(self) -> None:
        # before_effective must be >= before_inherited (a prior effective cannot
        # sit below the inherited floor). before_inherited reviewed, before_eff
        # untrusted => reviewed <= untrusted is false => reject.
        r = base_receipt(before_inherited_floor="reviewed", before_effective_floor="untrusted")
        # (also trips before-floor claim check, but the guard under test denies too)
        self.assertFalse(ev.validate_receipt(r, raised_env(), self.P()).allowed)

    # F3 — token budget enforced in retrieval.
    def test_f3_token_budget_truncation(self) -> None:
        big = base_env(payload_ref="X" * 5000)
        view = ev.evaluate_retrieval(retrieval_request(token_budget=1), [big], self.P())
        self.assertEqual(view.items, ())
        self.assertEqual(view.omitted_reason_counts.get("token_budget_truncated"), 1)
        self.assertLessEqual(view.token_count, 1)

    # F4 — circular authority: authority must be independent of the adjudicator.
    def test_f4_authority_equals_adjudicator_rejected(self) -> None:
        r = base_receipt(authority_ref="reviewer")  # adjudicator_ref is "reviewer"
        self.assertEqual(
            ev.validate_receipt(r, raised_env(), self.P()).reason_code, R.RECEIPT_AUTHORITY_INVALID
        )

    # H1.7 (added) — retrieval-permission value copied into a trust field.
    def test_h1_7_retrieval_authority_in_trust_field(self) -> None:
        env = base_env(inherited_trust_floor="self")  # retrieval_authority value
        self.assertEqual(
            ev.validate_envelope(env, self.P()).reason_code, R.DIMENSION_CROSS_CONTAMINATION
        )


class TestLintFilesEndToEnd(unittest.TestCase):
    FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "memory_envelope")

    def _f(self, name: str) -> str:
        return os.path.join(self.FIXTURES, name)

    def test_clean_envelope_no_findings(self) -> None:
        findings = ev.lint_files(self._f("policy_lattice.json"), self._f("envelope_clean.json"))
        self.assertEqual(findings, [])

    def test_raised_envelope_with_receipt_no_findings(self) -> None:
        findings = ev.lint_files(
            self._f("policy_lattice.json"), self._f("envelope_raised.json"), self._f("receipt_golden.json")
        )
        self.assertEqual(findings, [])

    def test_effective_without_receipt_flagged(self) -> None:
        findings = ev.lint_files(
            self._f("policy_lattice.json"), self._f("envelope_effective_without_receipt.json")
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["reason"], R.EFFECTIVE_WITHOUT_RECEIPT.value)


if __name__ == "__main__":
    unittest.main()
