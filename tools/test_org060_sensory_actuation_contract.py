#!/usr/bin/env python3
"""Tests for the ORG-060 sensory/actuation dark contract (T-3226)."""

from __future__ import annotations

import dataclasses
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools import capability_navigator_contract as cn
from tools import org060_sensory_actuation_contract as org060

me = org060.me
ops = org060.ops


REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64


def _auth() -> cn.AuthenticatedContext:
    return cn.AuthenticatedContext(
        subject_ref="subject:operator",
        uid=1000,
        persona_ref="persona:consultant",
        role_refs=("role:operator",),
        instance_ref="instance:test",
        trust_zone="test-zone",
        registry_revision="registry:1",
    )


def _provider(capability_id: str = "light-control") -> cn.NavigatorCapabilityProvider:
    return cn.NavigatorCapabilityProvider(
        capability_id=capability_id,
        provider_component_id="component-light",
        contract="org060-actuator",
        schema="org060-light/v1",
        version="1.0.0",
        enabled_features=("brightness",),
        requirement_closure=("req-safe",),
        equivalence_class="room-light",
        trust_class="local-trusted",
        deployment_trust_zone="test-zone",
        subject_visibility=cn.NavigatorSubjectVisibilityPolicy(
            subject_refs=("subject:operator",),
            role_refs=("role:operator",),
            instance_refs=("instance:test",),
            trust_zones=("test-zone",),
            policy_refs=("policy:org060",),
            evidence_refs=("evidence:visibility",),
        ),
        navigator_provider_selection=None,
        source_identity=cn.SourceIdentity(
            logical_repo="pantheon-config",
            path="components/light.json",
            artifact=None,
            record_sha256=SHA_A,
            tested_revision="rev-tested",
            locked_revision="rev-locked",
            artifact_digest=SHA_B,
        ),
        compatibility_evidence=("evidence:compat",),
        implications=(
            cn.NavigatorImplication(
                kind="physical-actuation",
                implication_id="imp-light",
                scope="room",
                evidence_refs=("evidence:implication",),
            ),
        ),
        upkeep=cn.NavigatorUpkeep(
            steward="consultant",
            health_evidence_refs=("evidence:health",),
            monitoring_refs=("evidence:monitoring",),
            upgrade_rollback_refs=("evidence:upgrade",),
            credential_rotation_refs=("evidence:credential-rotation",),
            storage_retention_refs=("evidence:storage-retention",),
            costs=(),
            review_obligations=("review:org060",),
        ),
    )


def _projection(provider: cn.NavigatorCapabilityProvider | None = None) -> cn.NavigatorProjection:
    provider = _provider() if provider is None else provider
    projection = cn.NavigatorProjection(
        schema_version=cn.SCHEMA_VERSION,
        assembly_id="assembly-org060",
        manifest_sha256=SHA_A,
        catalogue_sha256=SHA_B,
        raw_lock_sha256=SHA_C,
        lock_sha256=SHA_C,
        generator_id="test-generator",
        generator_version="1.0.0",
        resolution_order=("component-light",),
        member_component_ids=("component-light",),
        acknowledged_implications=("imp-light",),
        owned_obligations=("review:org060",),
        smoke_evidence=("evidence:smoke",),
        health_evidence=("evidence:health",),
        capabilities=(provider,),
        projection_sha256=SHA_D,
    )
    return projection


def _observation(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "envelope_id": "obs-1",
        "component_id": "component-light",
        "capability_id": "light-control",
        "contract": "org060-actuator",
        "schema": "org060-light/v1",
        "version": "1.0.0",
        "device_revision": "device-rev-1",
        "calibration": {
            "calibration_ref": "calibration:1",
            "calibrated_at": "2026-10-01T00:00:00Z",
        },
        "source_observed_at": "2026-10-01T00:00:00Z",
        "source_digest": SHA_A,
        "quality": {"score": 0.99},
        "uncertainty": {"class": "low"},
        "subject_scope": "known_subject",
        "purpose": "companionship",
        "capture_stage": "capture",
        "consent": {"refs": ["consent:1"]},
        "raw_media": {"class": "none"},
        "locality": "local_only",
        "egress": "none",
        "redaction": {"refs": ["redaction:1"], "digest": SHA_B},
        "retention": {"shape": "derived"},
        "access": {"audience": "self"},
        "captured_at": "2026-10-01T00:00:00Z",
        "received_at": "2026-10-01T00:00:00Z",
    }
    data.update(overrides)
    return data


def _envelope(**overrides: object) -> org060.SensoryEnvelope:
    result = org060.normalize_observation(
        _projection(),
        _observation(**overrides),
        now=NOW,
        freshness=org060.FreshnessWindow(60),
    )
    assert isinstance(result, org060.SensoryEnvelope), result
    return result


def _grant(**overrides: object) -> org060.ConsentGrant:
    data = dict(
        stage="capture",
        purpose="companionship",
        subject_scope="known_subject",
        consent_ref="consent:1",
        consent_digest=SHA_A,
        consented_at="2026-10-01T00:00:00Z",
        expires_at="2026-10-02T00:00:00Z",
        revoked=False,
    )
    data.update(overrides)
    return org060.ConsentGrant(**data)


def _capture_decision() -> org060.CapturePolicyDecision:
    decision = org060.evaluate_capture_policy(
        _envelope(),
        stage="capture",
        consent_evidence=(_grant(),),
        grants=(),
        purpose="companionship",
        availability="available",
        replay_cache=org060.ConsentReplayCache(),
        now=NOW,
    )
    assert isinstance(decision, org060.CapturePolicyDecision), decision
    return decision


def _policy() -> me.TrustPolicy:
    return me.TrustPolicy(
        policy_ref="P-org060",
        revision="rev-1",
        digest=SHA_A,
        labels=frozenset({"untrusted", "reviewed"}),
        order_pairs=frozenset({("untrusted", "reviewed")}),
        conservative_state="untrusted",
    )


def _request(**overrides: object) -> org060.ActuationRequest:
    data: dict[str, object] = {
        "schema_version": org060.SCHEMA_VERSION,
        "request_id": "request-1",
        "capability_id": "light-control",
        "target_ref": "target:light",
        "effect": {"set": "on"},
        "purpose": "companionship",
        "preconditions": ({"state": "off"},),
        "expected_result": {"state": "on"},
        "deadline": "2026-10-01T00:05:00Z",
        "idempotency_key": "idem-1",
        "cancellation_ref": "cancel-1",
    }
    data.update(overrides)
    result = org060.make_actuation_request(data)
    assert isinstance(result, org060.ActuationRequest), result
    return result


def _context() -> org060.AuthenticatedActuationContext:
    result = org060.make_authenticated_actuation_context(
        _auth(),
        native_authority_ref="native-authority:light",
        approval_evidence_ref="approval:evidence",
    )
    assert isinstance(result, org060.AuthenticatedActuationContext), result
    return result


def _target(projection: cn.NavigatorProjection | None = None) -> org060.TargetState:
    projection = _projection() if projection is None else projection
    return org060.make_target_state(
        target_ref="target:light",
        provider=projection.capabilities[0],
        projection=projection,
        observed_at="2026-10-01T00:00:00Z",
        state_digest=SHA_C,
        current_preconditions=({"state": "off"},),
    )


def _plan() -> org060.ActuationPlan:
    projection = _projection()
    result = org060.plan_actuation(
        _request(),
        projection=projection,
        target_states=(_target(projection),),
        context=_context(),
        now=NOW,
    )
    assert isinstance(result, org060.ActuationPlan), result
    return result


def _receipt(state: str = "provider_accepted") -> org060.ActuationReceipt:
    result = org060.invoke(
        _plan(),
        context=_context(),
        provider_result=org060.NativeDispatchResult(
            state=state,
            native_receipt_ref="native:receipt",
            status_evidence=("evidence:native",),
        ),
        now=NOW,
    )
    assert isinstance(result, org060.ActuationReceipt), result
    return result


def _forget_receipt_bytes(global_claim: bool = False) -> bytes:
    receipt = ops.ForgetReceipt(
        operation_id="op-1",
        subject_scope=("subject:operator",),
        target_ids=("memory:1",),
        mode="remove-derived",
        source_disposition=ops.DISPOSITION_NOT_APPLICABLE,
        derived_payload_disposition=ops.DISPOSITION_NOT_APPLICABLE,
        projection_purge_receipts=(),
        backup_disposition=ops.DISPOSITION_NOT_APPLICABLE,
        unverifiable_surfaces=(),
        authority_ref="authority:forget",
        completed_at="2026-10-01T00:00:00Z",
        surfaces=(),
        lifecycle_policy_pins=(),
        outcome=ops.LifecycleOutcome.REFUSED,
        refusal=ops.LifecycleRefusal.MALFORMED_INPUT,
        refusal_detail="fixture refusal",
        receipt_schema_version=ops.FORGET_RECEIPT_SCHEMA_VERSION,
        canonicalization_ref=me.CANONICALIZATION_REF,
        receipt_digest="",
    )
    receipt = dataclasses.replace(
        receipt, receipt_digest=ops.compute_forget_receipt_digest(receipt)
    )
    body = ops.forget_receipt_to_json(receipt)
    if global_claim:
        body["refusal_detail"] = "global-erasure claimed"
        body["receipt_digest"] = ops.compute_forget_receipt_digest(
            ops.forget_receipt_from_json(body)  # type: ignore[arg-type]
        )
    return me.canonical_json_bytes(body)


def _correction_receipt_bytes() -> bytes:
    receipt = ops.CorrectionReceipt(
        operation_id="op-correct-1",
        target_memory_id="memory:1",
        prior_version_digest=SHA_A,
        successor_version_id=None,
        source_status=ops.DISPOSITION_NOT_APPLICABLE,
        reason=None,
        actor="actor:test",
        authority_ref="authority:correct",
        index_purge_receipts=(),
        completed_at="2026-10-01T00:00:00Z",
        surfaces=(),
        lifecycle_policy_pins=(),
        reason_note_ref=None,
        reason_note_digest=None,
        outcome=ops.LifecycleOutcome.REFUSED,
        refusal=ops.LifecycleRefusal.MALFORMED_INPUT,
        refusal_detail="fixture refusal",
        receipt_schema_version=ops.CORRECTION_RECEIPT_SCHEMA_VERSION,
        canonicalization_ref=me.CANONICALIZATION_REF,
        receipt_digest="",
    )
    receipt = dataclasses.replace(
        receipt, receipt_digest=ops.compute_correction_receipt_digest(receipt)
    )
    return me.canonical_json_bytes(ops.correction_receipt_to_json(receipt))


class Org060SensoryActuationContractTests(unittest.TestCase):
    def test_sensory_envelope_roundtrip_is_canonical_bounded_and_closed(self) -> None:
        envelope = _envelope()
        env_json = org060.sensory_envelope_to_json(envelope)
        env_bytes = org060.canonical_json_bytes(env_json)
        self.assertEqual(org060.parse_sensory_envelope(env_bytes), envelope)
        self.assertEqual(
            org060.parse_sensory_envelope(env_bytes.replace(b'  "access"', b' "access"', 1)).reason,
            "noncanonical-json",
        )
        duplicate = b'{"schema_version":"org060-sensory-actuation/v1","schema_version":"x"}\n'
        self.assertEqual(org060.parse_sensory_envelope(duplicate).reason, "duplicate-key")
        unknown = dict(env_json)
        unknown["caller_authority"] = "bad"
        self.assertEqual(org060.parse_sensory_envelope(unknown).reason, "closed-object")
        nested_unknown = dict(env_json)
        nested_unknown["source"] = {**nested_unknown["source"], "nested_extra": "bad"}  # type: ignore[dict-item]
        nested_unknown["envelope_digest"] = org060._digest_record(nested_unknown, "envelope_digest")
        self.assertEqual(org060.parse_sensory_envelope(nested_unknown).reason, "closed-object")
        oversized = dict(env_json)
        oversized["purpose"] = "x" * org060.WIRE_LIMIT
        self.assertEqual(org060.parse_sensory_envelope(oversized).reason, "byte-bound-exceeded")
        hostile = dict(org060.actuation_request_to_json(_request()))
        hostile["purpose"] = "companionship\u0000control"
        hostile["request_digest"] = org060._digest_record(hostile, "request_digest")
        self.assertEqual(org060.parse_actuation_request(hostile).reason, "hostile-control")
        self.assertEqual(
            org060.make_actuation_request({**org060.actuation_request_to_json(_request()), "purpose": "companionship\u0000control"}).reason,
            "hostile-control",
        )
        timestamp_cases = (
            (org060.parse_sensory_envelope, env_json, "captured_at", "envelope_digest"),
            (org060.parse_actuation_request, org060.actuation_request_to_json(_request()), "deadline", "request_digest"),
            (org060.parse_actuation_plan, org060.actuation_plan_to_json(_plan()), "expires_at", "plan_digest"),
            (org060.parse_actuation_receipt, org060.actuation_receipt_to_json(_receipt()), "started_at", "receipt_digest"),
            (org060.parse_actuation_receipt, org060.actuation_receipt_to_json(_receipt()), "completed_at", "receipt_digest"),
        )
        for parser, record, field, digest_field in timestamp_cases:
            with self.subTest(timestamp=field):
                invalid = {**record, field: "not-a-canonical-instant"}
                invalid[digest_field] = org060._digest_record(invalid, digest_field)
                self.assertEqual(parser(invalid).reason, "invalid-pattern")

    def test_normalize_observation_binds_exact_catalogue_and_refuses_stale_or_tampered_sources(self) -> None:
        envelope = _envelope()
        self.assertEqual(envelope.data["schema_version"], org060.SCHEMA_VERSION)
        self.assertEqual(envelope.data["source"]["projection_sha256"], SHA_D)  # type: ignore[index]
        self.assertEqual(envelope.digest, org060._digest_record(envelope.data, "envelope_digest"))
        stale = org060.normalize_observation(_projection(), _observation(source_observed_at="2026-09-30T23:00:00Z"), now=NOW, freshness=org060.FreshnessWindow(60))
        self.assertEqual(stale.reason, "stale-source")
        mismatch = org060.normalize_observation(_projection(), _observation(projection_sha256=SHA_A), now=NOW, freshness=org060.FreshnessWindow(60))
        self.assertEqual(mismatch.reason, "projection-binding-mismatch")
        lookalike = org060.normalize_observation(_projection(), _observation(consent={"provider_authority": "caller"}), now=NOW, freshness=org060.FreshnessWindow(60))
        self.assertEqual(lookalike.reason, "identity-authority-lookalike")

    def test_capture_policy_refuses_missing_expired_revoked_contradictory_or_replayed_consent(self) -> None:
        decision = _capture_decision()
        self.assertTrue(decision.allowed)
        cases = [
            ((), "missing-consent"),
            ((_grant(expires_at="2026-09-30T00:00:00Z"),), "expired-consent"),
            ((_grant(revoked=True),), "revoked-consent"),
            ((_grant(), _grant(consent_ref="consent:2", subject_scope="known_guest")), "contradictory-consent"),
        ]
        for grants, reason in cases:
            with self.subTest(reason=reason):
                result = org060.evaluate_capture_policy(_envelope(), stage="capture", consent_evidence=grants, grants=(), purpose="companionship", availability="available", replay_cache=org060.ConsentReplayCache(), now=NOW)
                self.assertEqual(result.reason, reason)
        cache = org060.ConsentReplayCache()
        self.assertIsInstance(org060.evaluate_capture_policy(_envelope(), stage="capture", consent_evidence=(_grant(),), grants=(), purpose="companionship", availability="available", replay_cache=cache, now=NOW), org060.CapturePolicyDecision)
        self.assertEqual(org060.evaluate_capture_policy(_envelope(), stage="capture", consent_evidence=(_grant(),), grants=(), purpose="companionship", availability="available", replay_cache=cache, now=NOW).reason, "replayed-consent")

    def test_capture_policy_separates_stage_scopes_and_quiet_off_away_overrides(self) -> None:
        for stage in org060.CONSENT_STAGES:
            with self.subTest(stage=stage):
                grant = _grant(stage=stage)
                decision = org060.evaluate_capture_policy(_envelope(), stage=stage, consent_evidence=(grant,), grants=(), purpose="companionship", availability="available", replay_cache=org060.ConsentReplayCache(), now=NOW)
                self.assertIsInstance(decision, org060.CapturePolicyDecision)
        for availability in ("quiet", "off", "away"):
            with self.subTest(availability=availability):
                refused = org060.evaluate_capture_policy(_envelope(), stage="capture", consent_evidence=(_grant(),), grants=(), purpose="companionship", availability=availability, replay_cache=org060.ConsentReplayCache(), now=NOW)
                self.assertEqual(refused.reason, "availability-override")
        for subject_scope in ("bystander", "unknown"):
            with self.subTest(subject_scope=subject_scope):
                refused = org060.evaluate_capture_policy(_envelope(subject_scope=subject_scope), stage="capture", consent_evidence=(_grant(),), grants=(), purpose="companionship", availability="available", replay_cache=org060.ConsentReplayCache(), now=NOW)
                self.assertEqual(refused.reason, "missing-consent")

    def test_raw_media_is_local_ref_and_egress_requires_bounded_redacted_evidence(self) -> None:
        self.assertIsInstance(org060.raw_media_ref(local_ref="local://capture/1", media_digest=SHA_A), org060.RawMedia)
        self.assertEqual(org060.raw_media_ref(local_ref="https://remote", media_digest=SHA_A).reason, "raw-media-remote-forbidden")
        self.assertEqual(org060.egress_ref(purpose="companionship", evidence_refs=(), redaction=org060.RedactionEvidence(("evidence:redaction",), SHA_B)).reason, "egress-evidence-missing")
        egress = org060.egress_ref(purpose="companionship", evidence_refs=("evidence:egress",), redaction=org060.RedactionEvidence(("evidence:redaction",), SHA_B))
        self.assertIsInstance(egress, org060.EgressPolicy)

    def test_derivation_provenance_cannot_raise_missing_or_stale_source_trust(self) -> None:
        env = _envelope()
        derived = org060.derivation(env, payload_ref="payload:1", payload_digest=SHA_C, provenance=org060.DerivationProvenance(env.digest, ("transform:1",), ({"model_ref": "model:1"},), "2026-10-01T00:00:00Z"))
        self.assertIsInstance(derived, org060.DerivedPayload)
        self.assertEqual(
            org060.derivation(env, payload_ref="payload:1", payload_digest=SHA_C, provenance=org060.DerivationProvenance(env.digest, (), (), "2026-10-01T00:00:00Z")).reason,
            "derivation-provenance-missing",
        )
        self.assertEqual(org060.derivation(env, payload_ref="payload:1", payload_digest=SHA_C, provenance=org060.DerivationProvenance(SHA_A, ("transform:1",), (), "2026-10-01T00:00:00Z")).reason, "derivation-source-stale")
        self.assertEqual(org060.derivation(env, payload_ref="payload:1", payload_digest=SHA_C, provenance=org060.DerivationProvenance(env.digest, ("transform:1",), ({"trust": "raised"},), "2026-10-01T00:00:00Z")).reason, "trust-raise-forbidden")

    def test_retention_proposal_only_after_capture_and_references_org058_policy(self) -> None:
        proposal = org060.build_retention_proposal(_envelope(), _capture_decision(), "authority:retain")
        self.assertIsInstance(proposal, me.RetentionProposal)
        self.assertEqual(proposal.required_authority, "authority:retain")
        denied_capture = org060.CapturePolicyDecision(False, "capture", "companionship", "auth", ())
        self.assertEqual(org060.build_retention_proposal(_envelope(), denied_capture, "authority:retain").reason, "capture-required-before-retention")
        self.assertEqual(org060.propose_retention(proposal, capture_decision=_capture_decision(), authority="authority:other", policy=_policy(), required_authority="authority:retain").reason, "retention-denied")
        self.assertEqual(org060.propose_retention(proposal, capture_decision=_capture_decision(), authority="authority:retain", policy=None, required_authority="authority:retain").reason, "policy-evidence-missing")
        allowed = org060.propose_retention(proposal, capture_decision=_capture_decision(), authority="authority:retain", policy=_policy(), required_authority="authority:retain")
        self.assertIsInstance(allowed, org060.RetentionDispositionRef)
        self.assertEqual(allowed.policy_digest, SHA_A)

    def test_retention_and_forgetting_receipts_preserve_honest_scope_and_no_global_erasure(self) -> None:
        correction = org060.consume_lifecycle_receipt(_correction_receipt_bytes(), "correction")
        self.assertIsInstance(correction, org060.LifecycleReceiptEvidence)
        self.assertEqual(correction.outcome, ops.LifecycleOutcome.REFUSED.value)
        forget = org060.consume_lifecycle_receipt(_forget_receipt_bytes(), "forget")
        self.assertIsInstance(forget, org060.LifecycleReceiptEvidence)
        self.assertFalse(forget.global_erasure_verified)
        self.assertEqual(org060.consume_lifecycle_receipt(_forget_receipt_bytes(global_claim=True), "forget").reason, "global-erasure-claim-forbidden")
        duplicate = b'{"operation_id":"x","operation_id":"y","receipt_digest":"sha256:' + b"0" * 64 + b'"}\n'
        self.assertEqual(org060.consume_lifecycle_receipt(duplicate, "forget").reason, "duplicate-key")

    def test_actuation_request_rejects_caller_identity_authority_and_provider_lookalikes(self) -> None:
        request = org060.actuation_request_to_json(_request())
        request["approval_ref"] = "caller:approval"
        self.assertEqual(org060.parse_actuation_request(request).reason, "closed-object")
        nested_extra = org060.actuation_request_to_json(_request())
        nested_extra["effect"] = {**nested_extra["effect"], "nested_extra": "bad"}  # type: ignore[dict-item]
        nested_extra["request_digest"] = org060._digest_record(nested_extra, "request_digest")
        self.assertEqual(org060.parse_actuation_request(nested_extra).reason, "closed-object")
        maker_extra = dict(nested_extra)
        maker_extra.pop("request_digest")
        self.assertEqual(org060.make_actuation_request(maker_extra).reason, "closed-object")
        nested = org060.make_actuation_request({
            "schema_version": org060.SCHEMA_VERSION,
            "request_id": "request-bad",
            "capability_id": "light-control",
            "target_ref": "target:light",
            "effect": {"provider_hint": "bad"},
            "purpose": "companionship",
            "preconditions": [{"state": "off"}],
            "expected_result": {"state": "on"},
            "deadline": "2026-10-01T00:05:00Z",
            "idempotency_key": "idem-bad",
            "cancellation_ref": "cancel-bad",
        })
        self.assertEqual(nested.reason, "identity-authority-lookalike")

    def test_authenticated_actuation_context_specializes_server_context_and_binds_native_authority_approval(self) -> None:
        context = _context()
        self.assertIs(context.authenticated_context.__class__, cn.AuthenticatedContext)
        self.assertEqual(context.native_authority_ref, "native-authority:light")
        self.assertEqual(org060.make_authenticated_actuation_context(object(), native_authority_ref="native", approval_evidence_ref="approval").reason, "authenticated-context-required")
        self.assertEqual(org060.make_authenticated_actuation_context(_auth(), native_authority_ref="", approval_evidence_ref="approval").reason, "native-authority-missing")
        self.assertEqual(org060.make_authenticated_actuation_context(_auth(), native_authority_ref="native", approval_evidence_ref="").reason, "approval-evidence-missing")

    def test_plan_actuation_resolves_one_exact_target_and_refuses_ambiguous_stale_or_unsafe_state(self) -> None:
        projection = _projection()
        plan = org060.plan_actuation(_request(), projection=projection, target_states=(_target(projection),), context=_context(), now=NOW)
        self.assertIsInstance(plan, org060.ActuationPlan)
        self.assertEqual(plan.data["projection_sha256"], SHA_D)
        self.assertEqual(org060.plan_actuation(_request(), projection=projection, target_states=(), context=_context(), now=NOW).reason, "no-matching-capability")
        self.assertEqual(org060.plan_actuation(_request(), projection=projection, target_states=(_target(projection), _target(projection)), context=_context(), now=NOW).reason, "ambiguous-target")
        stale = dataclasses.replace(_target(projection), observed_at="2026-09-30T00:00:00Z")
        self.assertEqual(org060.plan_actuation(_request(), projection=projection, target_states=(stale,), context=_context(), now=NOW).reason, "target-state-stale")
        unsafe = _request(preconditions=({"state": "off"}, {"state": "locked"}))
        self.assertEqual(org060.plan_actuation(unsafe, projection=projection, target_states=(_target(projection),), context=_context(), now=NOW).reason, "unsafe-precondition")

    def test_actuation_plan_binds_implications_idempotency_verification_rollback_and_evidence(self) -> None:
        plan = _plan()
        self.assertEqual(plan.data["implications"], [{"kind": "physical-actuation", "id": "imp-light", "scope": "room", "evidence_refs": ["evidence:implication"]}])
        self.assertEqual(plan.data["idempotency"], {"key": "idem-1", "class": "idempotent_same_plan", "max_attempts": org060.MAX_RETRIES})
        self.assertEqual(plan.data["verification_steps"], [{"step_id": "verify:1", "observation_capability_id": "light-control", "expected_result": {"state": "on"}, "evidence_refs": ["evidence:plan-binding"]}])
        self.assertEqual(plan.data["rollback_steps"], [{"step_id": "rollback:1", "effect": {"set": "on"}, "preconditions": [{"state": "off"}], "expected_result": {"state": "on"}, "evidence_refs": ["evidence:plan-binding"]}])
        self.assertEqual(plan.data["evidence_refs"], ["evidence:plan-binding"])
        plan_bytes = org060.canonical_json_bytes(org060.actuation_plan_to_json(plan))
        self.assertEqual(org060.parse_actuation_plan(plan_bytes), plan)
        tampered = dict(org060.actuation_plan_to_json(plan))
        tampered["plan_digest"] = SHA_A
        self.assertEqual(org060.parse_actuation_plan(tampered).reason, "tampered-source-binding")
        invalid_idempotency = dict(org060.actuation_plan_to_json(plan))
        invalid_idempotency["idempotency"] = {**invalid_idempotency["idempotency"], "class": "replay_anyway"}  # type: ignore[dict-item]
        invalid_idempotency["plan_digest"] = org060._digest_record(invalid_idempotency, "plan_digest")
        self.assertEqual(org060.parse_actuation_plan(invalid_idempotency).reason, "invalid-enum")

    def test_invoke_refuses_drift_denial_expiry_missing_approval_and_never_falls_back(self) -> None:
        self.assertEqual(org060.invoke(_plan(), context=object(), provider_result=org060.NativeDispatchResult("provider_accepted", "native", ()), now=NOW).reason, "authenticated-context-required")
        self.assertEqual(org060.invoke(_plan(), context=_context(), adapter=None, provider_result=None, now=NOW).reason, "provider-fallback-forbidden")
        self.assertEqual(org060.invoke(_plan(), context=_context(), provider_result=org060.NativeDispatchResult("provider_denied", None, ("evidence:denied",)), now=NOW).reason, "provider-denied")
        expired = org060.plan_actuation(_request(deadline="2026-10-01T00:00:01Z"), projection=_projection(), target_states=(_target(_projection()),), context=_context(), now=datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc))
        assert isinstance(expired, org060.ActuationPlan)
        self.assertEqual(org060.invoke(expired, context=_context(), provider_result=org060.NativeDispatchResult("provider_accepted", "native", ()), now=datetime(2026, 10, 1, 0, 0, 2, tzinfo=timezone.utc)).reason, "plan-expired")
        tampered = dataclasses.replace(_plan(), data={**_plan().data, "plan_digest": SHA_A})
        self.assertEqual(org060.invoke(tampered, context=_context(), provider_result=org060.NativeDispatchResult("provider_accepted", "native", ()), now=NOW).reason, "plan-digest-mismatch")

    def test_provider_dispatch_is_not_completion_and_retry_respects_idempotency(self) -> None:
        first = _receipt()
        self.assertEqual(org060.invoke(_plan(), context=_context(), provider_result=org060.NativeDispatchResult("provider_accepted", "native", ()), prior=first, attempt=4, now=NOW).reason, "retry-attempt-invalid")
        self.assertEqual(org060.invoke(_plan(), context=_context(), provider_result=org060.NativeDispatchResult("provider_accepted", "native", ()), prior=first, attempt=3, now=NOW).reason, "retry-attempt-invalid")
        other_plan = dataclasses.replace(_plan(), data={**_plan().data, "plan_id": "plan:other"})
        self.assertEqual(org060.invoke(other_plan, context=_context(), provider_result=org060.NativeDispatchResult("provider_accepted", "native", ()), prior=first, attempt=2, now=NOW).reason, "plan-digest-mismatch")
        non_idem = dataclasses.replace(_plan(), data={**_plan().data, "idempotency": {"key": "idem-1", "class": "non_idempotent", "max_attempts": 2}})
        non_idem = dataclasses.replace(non_idem, data={**non_idem.data, "plan_digest": org060._digest_record(non_idem.data, "plan_digest")})
        matching_prior = dataclasses.replace(first, data={**first.data, "plan_digest": non_idem.digest})
        self.assertEqual(org060.invoke(non_idem, context=_context(), provider_result=org060.NativeDispatchResult("provider_accepted", "native", ()), prior=matching_prior, attempt=2, now=NOW).reason, "non-idempotent-retry")
        second = org060.invoke(_plan(), context=_context(), provider_result=org060.NativeDispatchResult("provider_accepted", "native:2", ("evidence:retry",)), prior=first, attempt=2, now=NOW)
        self.assertIsInstance(second, org060.ActuationReceipt)

    def test_cancellation_and_rollback_states_and_residual_risk_remain_distinct(self) -> None:
        pending = org060.invoke(_plan(), context=_context(), provider_result=org060.NativeDispatchResult("provider_timeout", None, ("evidence:timeout",)), now=NOW)
        assert isinstance(pending, org060.ActuationReceipt)
        self.assertEqual(org060.observe_effect(pending, org060.TypedEffectObservation("obs:1", SHA_A, "2026-10-01T00:00:01Z", True), now=NOW).reason, "dispatch-not-completion")
        accepted = org060.request_cancellation(_receipt(), _context(), provider_result=org060.NativeCancellationResult("provider_accepted", ("evidence:cancel",)), evidence_refs=("evidence:request-cancel",), now=NOW)
        assert isinstance(accepted, org060.ActuationReceipt)
        observed = org060.observe_cancellation(accepted, org060.TypedCancellationObservation("obs:cancel", SHA_A, "2026-10-01T00:00:02Z", True), now=NOW)
        self.assertEqual(observed.data["cancellation_state"], "observed_stopped")
        verified = org060.observe_effect(_receipt(), org060.TypedEffectObservation("obs:1", SHA_A, "2026-10-01T00:00:01Z", True), now=NOW)
        assert isinstance(verified, org060.ActuationReceipt)
        rolled = org060.rollback_effect(verified, context=_context(), approval_evidence_ref="approval:rollback", provider_result=org060.NativeRollbackResult("provider_accepted", "native:rollback", ("evidence:rollback",)), now=NOW)
        assert isinstance(rolled, org060.ActuationReceipt)
        self.assertEqual(org060.observe_rollback(rolled, org060.TypedRollbackObservation("obs:rollback", SHA_B, "2026-10-01T00:00:03Z", False), None, now=NOW).reason, "residual-risk-required")
        open_observation = org060.actuation_receipt_to_json(verified)
        assert isinstance(open_observation["observed_postcondition"], dict)
        open_observation["observed_postcondition"] = {**open_observation["observed_postcondition"], "nested_extra": "bad"}  # type: ignore[dict-item]
        open_observation["receipt_digest"] = org060._digest_record(open_observation, "receipt_digest")
        self.assertEqual(org060.parse_actuation_receipt(open_observation).reason, "closed-object")
        nullable_base = org060.actuation_receipt_to_json(_receipt())
        self.assertIsInstance(org060.parse_actuation_receipt(nullable_base), org060.ActuationReceipt)
        observations = (
            dataclasses.asdict(org060.TypedEffectObservation("obs:effect", SHA_A, "2026-10-01T00:00:01Z", True)),
            dataclasses.asdict(org060.TypedCancellationObservation("obs:cancel", SHA_A, "2026-10-01T00:00:01Z", True)),
            dataclasses.asdict(org060.TypedRollbackObservation("obs:rollback", SHA_A, "2026-10-01T00:00:01Z", True)),
        )
        for observation in observations:
            with self.subTest(nullable_observation=sorted(observation)):
                branch = dict(nullable_base)
                branch["observed_postcondition"] = observation
                branch["completed_at"] = "2026-10-01T00:00:01Z"
                branch["receipt_digest"] = org060._digest_record(branch, "receipt_digest")
                self.assertIsInstance(org060.parse_actuation_receipt(branch), org060.ActuationReceipt)
        residual = dict(nullable_base)
        residual["residual_risk"] = {"class": "partial", "detail": "lamp state uncertain", "evidence_refs": ["evidence:risk"]}
        residual["receipt_digest"] = org060._digest_record(residual, "receipt_digest")
        self.assertIsInstance(org060.parse_actuation_receipt(residual), org060.ActuationReceipt)

    def test_catalogue_to_capture_retention_and_actuation_paths_use_canonical_receipts(self) -> None:
        self.assertEqual(org060.normalize_observation(object(), _observation(), now=NOW, freshness=org060.FreshnessWindow(60)).reason, "projection-binding-mismatch")
        self.assertEqual(org060.make_authenticated_actuation_context(object(), native_authority_ref="native", approval_evidence_ref="approval").reason, "authenticated-context-required")
        proposal = org060.build_retention_proposal(_envelope(), _capture_decision(), "authority:retain")
        self.assertIsInstance(proposal, me.RetentionProposal)
        self.assertIsInstance(org060.propose_retention(proposal, capture_decision=_capture_decision(), authority="authority:retain", policy=_policy(), required_authority="authority:retain"), org060.RetentionDispositionRef)
        self.assertIsInstance(org060.consume_lifecycle_receipt(_forget_receipt_bytes(), "forget"), org060.LifecycleReceiptEvidence)

    def test_generated_schemas_match_runtime_and_ci_covers_paths(self) -> None:
        def walk(node: object, path: str = "$", in_oneof: bool = False) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertFalse(node.get("additionalProperties"), path)
                    self.assertEqual(set(node.get("required", ())), set(node.get("properties", {})), path)
                if node.get("type") == "array":
                    self.assertIn("maxItems", node, path)
                for key, value in node.items():
                    walk(value, f"{path}.{key}", in_oneof or key == "oneOf")
            elif isinstance(node, list):
                for idx, value in enumerate(node):
                    walk(value, f"{path}[{idx}]", in_oneof)
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
        for filename, generator in org060.generate_schemas().items():
            with self.subTest(filename=filename):
                self.assertEqual((REPO_ROOT / "schemas" / filename).read_bytes(), generator)
                schema = json.loads(generator)
                walk(schema)
                # T-5001: one trigger list (pull_request); ci.yml no longer has a push trigger.
                self.assertEqual(ci.count(f"schemas/{filename}"), 1, filename)


if __name__ == "__main__":
    unittest.main()
