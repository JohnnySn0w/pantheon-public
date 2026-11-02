#!/usr/bin/env python3
"""Tests for the ORG-060 presence projection/interaction dark contract (T-3227).

Continuity objects come from the real T-3176 producer and strict loaders
(ContinuityFixture from test_persona_continuity); navigator projections come from
the real cn.build_navigator_projection path (_fixture_projection from
test_capability_navigator_contract). No wire shape is defined here: every record
is produced by tools/org060_presence_contract.py.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import inspect
import json
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
from pathlib import Path

from tools import capability_navigator_contract as cn
from tools import org060_presence_contract as pr
from tools import org060_sensory_actuation_contract as b
from tools import persona_continuity as pc
from tools.test_capability_navigator_contract import _fixture_projection
from tools.test_persona_continuity import ContinuityFixture

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "tools" / "org060_presence_contract.py"
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
FRESH = b.FreshnessWindow(60)
SHA_X = "sha256:" + "e" * 64
SHA_Y = "sha256:" + "f" * 64
_NAVIGATOR: cn.NavigatorProjection | None = None


def z(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(seconds: int) -> str:
    return z(NOW - timedelta(seconds=seconds))


def navigator() -> cn.NavigatorProjection:
    global _NAVIGATOR
    if _NAVIGATOR is None:
        _NAVIGATOR = _fixture_projection()[3]
    return _NAVIGATOR


def auth(**changes: object) -> cn.AuthenticatedContext:
    data: dict[str, object] = {
        "subject_ref": "subject:operator",
        "uid": 1000,
        "persona_ref": "persona:consultant",
        "role_refs": ("role:operator",),
        "instance_ref": "instance:test",
        "trust_zone": "test-zone",
        "registry_revision": "registry:1",
    }
    data.update(changes)
    return cn.AuthenticatedContext(**data)  # type: ignore[arg-type]


def resign(data: dict[str, object], field: str) -> dict[str, object]:
    data[field] = None
    data[field] = pr._digest_record(data, field)
    return data


def ok(test: unittest.TestCase, value: object, cls: type) -> object:
    test.assertNotIsInstance(value, b.TypedRefusal, getattr(value, "reason", None))
    test.assertIsInstance(value, cls)
    return value


class World:
    """One temp root with a real continuity checkpoint, view file and navigator."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.fx = ContinuityFixture(root / "continuity")
        self.view, self.receipt = self.fx.checkpoint()
        self.view_path = root / "view.json"
        self.view_path.write_text(json.dumps(dataclasses.asdict(self.view), sort_keys=True))
        self.view_path.chmod(0o600)
        self.navigator = navigator()
        self.context = auth()

    def companion(self, **changes: object) -> pr.PresenceCompanionState:
        data: dict[str, object] = {
            "availability": "available",
            "privacy": "local_only",
            "recording": "recording",
            "action": "idle",
            "fallback_channels": pr.FALLBACK_CHANNELS,
            "observed_at": ago(5),
        }
        data.update(changes)
        return pr.PresenceCompanionState(**data)  # type: ignore[arg-type]

    def build(self, *, receipt: object = None, prior: object = None, **changes: object) -> object:
        kwargs: dict[str, object] = {
            "view_path": self.view_path,
            "checkpoint_receipt": self.receipt if receipt is None else receipt,
            "checkpoint_root": self.fx.checkpoints,
            "navigator": self.navigator,
            "context": self.context,
            "companion": self.companion(),
            "prior": prior,
            "now": NOW,
        }
        kwargs.update(changes)
        return pr.build_presence_projection(**kwargs)  # type: ignore[arg-type]

    def projection(self, **changes: object) -> pr.PresenceProjection:
        result = self.build(**changes)
        assert isinstance(result, pr.PresenceProjection), result
        return result

    def chained_receipt(self, boundary_id: str = "boundary-2", *, chained: bool = True) -> pc.CheckpointReceipt:
        boundary = self.fx.boundary(
            boundary_id=boundary_id,
            supersedes_receipt_path=self.receipt.path if chained else None,
            supersedes_payload_sha256=self.receipt.payload_sha256 if chained else None,
        )
        return pc.write_checkpoint(
            self.fx.resolve(),
            content=self.fx.content(),
            boundary=boundary,
            runtime_observation=self.fx.observation(),
            checkpoint_root=self.fx.checkpoints,
            producer_revision="r2",
        )


def new_surface(surface_id: str = "surface-display", **changes: object) -> pr.PresenceSurfaceState:
    kwargs: dict[str, object] = {
        "surface_id": surface_id,
        "surface_kind": "display",
        "connectivity": "live",
        "connectivity_observed_at": ago(5),
        "interaction_modes": ("control", "text", "voice"),
        "adapter_id": "adapter:fixture-display",
        "adapter_revision": "adapter-rev-1",
        "exact_revision": "rev-1",
        "rollback_ref": "rollback:rev-1",
        "now": NOW,
    }
    kwargs.update(changes)
    state = pr.make_presence_surface_state(**kwargs)  # type: ignore[arg-type]
    assert isinstance(state, pr.PresenceSurfaceState), state
    return state


def event(leg: str, outcome: str, *, to_revision: str | None = None, observed_at: str | None = None) -> pr.PresenceLifecycleEvent:
    return pr.PresenceLifecycleEvent(
        leg=leg,
        evidence_ref=f"evidence:{leg}-{outcome}",
        evidence_digest=SHA_X,
        observed_at=observed_at or ago(10),
        outcome=outcome,
        to_revision=to_revision,
    )


def apply(state: pr.PresenceSurfaceState, ev: pr.PresenceLifecycleEvent) -> object:
    return pr.record_presence_lifecycle(state, ev, expected_state_digest=state.digest, now=NOW)


def step(state: pr.PresenceSurfaceState, ev: pr.PresenceLifecycleEvent) -> pr.PresenceSurfaceState:
    result = apply(state, ev)
    assert isinstance(result, pr.PresenceSurfaceState), (ev, result)
    return result


FULL_LEGS = (
    ("install", "verified", None),
    ("configure", "verified", None),
    ("update", "verified", "rev-2"),
    ("remove", "rehearsed", None),
    ("health", "healthy", None),
)


def ready_surface(surface_id: str = "surface-display", *, skip: tuple[str, ...] = (), health: str = "healthy", **changes: object) -> pr.PresenceSurfaceState:
    state = new_surface(surface_id, **changes)
    for leg, outcome, revision in FULL_LEGS:
        if leg in skip:
            continue
        state = step(state, event(leg, health if leg == "health" else outcome, to_revision=revision))
    return state


def readiness(state: pr.PresenceSurfaceState, reviewed: frozenset[str] = frozenset()) -> object:
    return pr.evaluate_presence_readiness(state, reviewed_waiver_digests=reviewed, now=NOW, health_freshness=b.FreshnessWindow(300))


def ready(state: pr.PresenceSurfaceState) -> pr.PresenceReadiness:
    result = readiness(state)
    assert isinstance(result, pr.PresenceReadiness), result
    return result


def observe(state: pr.PresenceSurfaceState, connectivity: str, observed_at: str | None = None) -> pr.PresenceSurfaceState:
    result = pr.observe_presence_connectivity(
        state, connectivity=connectivity, observed_at=observed_at or ago(2), expected_state_digest=state.digest, now=NOW
    )
    assert isinstance(result, pr.PresenceSurfaceState), result
    return result


def adapter(**changes: object) -> pr.FixtureBridgePresenceAdapter:
    kwargs: dict[str, object] = {"clock": ago(1)}
    kwargs.update(changes)
    return pr.FixtureBridgePresenceAdapter(**kwargs)  # type: ignore[arg-type]


def render(projection: pr.PresenceProjection, state: pr.PresenceSurfaceState, fixture: object | None = None, **changes: object) -> object:
    kwargs: dict[str, object] = {
        "surface": state,
        "adapter": fixture if fixture is not None else adapter(),
        "now": NOW,
        "freshness": FRESH,
    }
    if "readiness" not in changes:
        kwargs["readiness"] = ready(state) if isinstance(state, pr.PresenceSurfaceState) and state.data["phase"] == "present" else None
    kwargs.update(changes)
    return pr.render_presence(projection, **kwargs)  # type: ignore[arg-type]


def grant(world: World, surface_id: str = "surface-display", *, context: cn.AuthenticatedContext | None = None, **changes: object) -> b.ConsentGrant:
    fields: dict[str, object] = {
        "stage": pr.PRESENCE_CONSENT_STAGE,
        "purpose": "companionship",
        "subject_scope": "known_subject",
        "consent_ref": "consent:1",
        "consented_at": ago(3600),
        "expires_at": z(NOW + timedelta(hours=1)),
        "revoked": False,
    }
    fields.update(changes)
    digest = pr.presence_consent_digest(
        purpose=str(fields["purpose"]),
        subject_scope=str(fields["subject_scope"]),
        consent_ref=str(fields["consent_ref"]),
        consented_at=str(fields["consented_at"]),
        expires_at=str(fields["expires_at"]),
        surface_id=surface_id,
        auth_context_digest=str(pr.presence_auth_context_digest(context or world.context)),
    )
    fields.setdefault("consent_digest", digest)
    return b.ConsentGrant(**fields)  # type: ignore[arg-type]


def request(projection: pr.PresenceProjection, **changes: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "request_id": "req:1",
        "surface_id": "surface-display",
        "purpose": "companionship",
        "mode": "text",
        "content": "hello there",
        "observed_projection_revision": projection.revision,
        "observed_projection_digest": projection.digest,
        "consent_ref": "consent:1",
    }
    kwargs.update(changes)
    result = pr.make_presence_interaction_request(**kwargs)  # type: ignore[arg-type]
    assert isinstance(result, pr.PresenceInteractionRequest), result
    return pr.presence_interaction_request_to_json(result)


def envelope(req: dict[str, object], surface_id: str = "surface-display", read_at: str | None = None) -> dict[str, object]:
    return {"surface_id": surface_id, "read_at": read_at or ago(1), "request": req}


def accept(world: World, projection: pr.PresenceProjection, state: pr.PresenceSurfaceState, interaction: object, **changes: object) -> object:
    kwargs: dict[str, object] = {
        "current": projection,
        "surface": state,
        "context": world.context,
        "consent_evidence": [grant(world)],
        "replay_cache": b.ConsentReplayCache(),
        "now": NOW,
        "freshness": FRESH,
    }
    if "readiness" not in changes:
        kwargs["readiness"] = ready(state)
    kwargs.update(changes)
    return pr.accept_presence_interaction(interaction, **kwargs)  # type: ignore[arg-type]


class PresenceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.world = World(Path(self._tmp.name))

    def reason(self, value: object, expected: str) -> None:
        self.assertIsInstance(value, pr.PresenceRefusal, value)
        self.assertIs(type(value), pr.PresenceRefusal)
        self.assertEqual(value.reason, expected, value.detail)  # type: ignore[union-attr]

    # ------------------------------------------------------------- EXACT TESTS (ticket, verbatim)

    def test_presence_projection_roundtrip_is_canonical_bounded_and_closed(self) -> None:
        projection = self.world.projection()
        raw = b.canonical_json_bytes(pr.presence_projection_to_json(projection))
        self.assertEqual(pr.parse_presence_projection(raw), projection)
        self.reason(pr.parse_presence_projection(json.dumps(pr.presence_projection_to_json(projection))), "noncanonical-json")
        self.reason(pr.parse_presence_projection(raw.replace(b'"schema_version"', b'"schema_version": "x",\n  "schema_version"', 1)), "duplicate-key")
        extra = pr.presence_projection_to_json(projection)
        extra["note"] = "x"
        self.reason(pr.parse_presence_projection(resign(extra, "projection_digest")), "closed-object")
        missing = pr.presence_projection_to_json(projection)
        del missing["issued_at"]
        self.reason(pr.parse_presence_projection(missing), "missing-field")
        self.reason(pr.parse_presence_projection(b"{" + b" " * pr.WIRE_LIMIT + b"}"), "byte-bound-exceeded")
        state = ready_surface()
        rendered = ok(self, render(projection, state), pr.PresenceRenderResult)
        decision = ok(self, accept(self.world, projection, state, envelope(request(projection))), pr.PresenceInteractionDecision)
        req = request(projection)
        for parser, record in (
            (pr.parse_presence_surface_state, state.data),
            (pr.parse_presence_render_result, rendered.data),  # type: ignore[union-attr]
            (pr.parse_presence_interaction_request, req),
            (pr.parse_presence_interaction_decision, decision.data),  # type: ignore[union-attr]
        ):
            with self.subTest(parser=parser.__name__):
                first = parser(b.canonical_json_bytes(record))
                self.assertNotIsInstance(first, b.TypedRefusal)
                self.assertEqual(b.canonical_json_bytes(first.data), b.canonical_json_bytes(record))  # type: ignore[union-attr]

    def test_presence_public_projection_rejects_recursive_identity_authority_lookalikes(self) -> None:
        projection = self.world.projection()
        base = request(projection)
        for part in pr.PRESENCE_FORBIDDEN_PARTS:
            key = f"x_{part}_ref"
            top = copy.deepcopy(base)
            top[key] = "x"
            nested = copy.deepcopy(base)
            nested["input"][key] = "x"  # type: ignore[index]
            deep = copy.deepcopy(base)
            deep["input"]["content"] = [{"wrapper": {key: "x"}}]  # type: ignore[index]
            for label, value in (("top", top), ("nested", nested), ("deep", deep)):
                with self.subTest(part=part, where=label):
                    self.reason(pr.parse_presence_interaction_request(value), "identity-authority-lookalike")
            with self.subTest(part=part, where="envelope"):
                env = envelope(base)
                env[key] = "x"
                self.reason(accept(self.world, projection, ready_surface(), env), "identity-authority-lookalike")
        state = ready_surface()
        self.reason(render(projection, state, adapter(receipt_overrides={"account_hint": "a"})), "identity-authority-lookalike")
        self.reason(render(projection, state, adapter(displayed_overrides={"approval": "yes"})), "identity-authority-lookalike")

    def test_presence_projection_binds_real_continuity_view_and_checkpoint(self) -> None:
        projection = self.world.projection()
        envelope_ = pc.load_checkpoint_envelope(Path(self.world.receipt.path), checkpoint_root=self.world.fx.checkpoints)
        continuity = projection.data["continuity"]
        self.assertEqual(
            continuity,
            {
                "persona_id": envelope_.persona_id,
                "organism_id": envelope_.organism_id,
                "view_sha256": self.world.view.view_sha256,
                "checkpoint_id": envelope_.checkpoint_id,
                "checkpoint_payload_sha256": envelope_.payload_sha256,
                "checkpoint_receipt_sha256": self.world.receipt.receipt_sha256,
                "prior_checkpoint_sha256": None,
            },
        )
        self.assertEqual(projection.data["projection_id"], "presence-projection:consultant")
        self.reason(self.world.build(receipt=dataclasses.asdict(self.world.receipt)), "continuity-evidence-required")
        self.reason(self.world.build(view_path=str(self.world.view_path)), "continuity-evidence-required")
        view_data = json.loads(self.world.view_path.read_text())
        view_data["resolved_at"] = "2026-09-07T00:00:00Z"
        self.world.view_path.write_text(json.dumps(view_data))
        self.reason(self.world.build(), "continuity-binding-mismatch")

    def test_presence_projection_refuses_mismatched_persona_or_checkpoint_digest(self) -> None:
        receipt = self.world.receipt

        def resigned(**changes: object) -> pc.CheckpointReceipt:
            changed = dataclasses.replace(receipt, **changes)
            return dataclasses.replace(changed, receipt_sha256=pr.checkpoint_receipt_digest(changed))

        for label, bad in (
            ("persona", resigned(persona_id="artisan")),
            ("view", resigned(view_sha256="1" * 64)),
            ("payload", resigned(payload_sha256="2" * 64)),
            ("checkpoint-id", resigned(checkpoint_id="consultant-other")),
            ("receipt-digest", dataclasses.replace(receipt, receipt_sha256="3" * 64)),
            ("created-at", resigned(created_at="2026-09-06T00:00:01Z")),
        ):
            with self.subTest(label):
                self.reason(self.world.build(receipt=bad), "continuity-binding-mismatch")
        path = Path(receipt.path)
        path.chmod(0o600)
        tampered = json.loads(path.read_text())
        tampered["producer_revision"] = "tampered"
        path.write_text(json.dumps(tampered, sort_keys=True, separators=(",", ":")) + "\n")
        path.chmod(0o600)
        result = self.world.build()
        self.reason(result, "continuity-binding-mismatch")
        self.assertEqual(result.detail, "checkpoint-digest-drift")  # type: ignore[union-attr]

    def test_presence_projection_binds_real_navigator_projection_and_auth_context(self) -> None:
        projection = self.world.projection()
        nav = self.world.navigator
        self.assertEqual(
            projection.data["navigator"],
            {
                "assembly_id": nav.assembly_id,
                "projection_sha256": nav.projection_sha256,
                "catalogue_sha256": nav.catalogue_sha256,
                "lock_sha256": nav.lock_sha256,
                "auth_context_digest": pr.presence_auth_context_digest(self.world.context),
            },
        )
        self.reason(self.world.build(navigator=dataclasses.replace(nav, projection_sha256=SHA_X)), "projection-binding-mismatch")
        self.reason(self.world.build(navigator=dataclasses.replace(nav, assembly_id="forged-assembly")), "projection-binding-mismatch")
        self.reason(self.world.build(navigator=cn.projection_to_json(nav)), "projection-binding-mismatch")
        for bad in (dataclasses.asdict(self.world.context), object(), auth(uid=True), auth(role_refs=["role:operator"]), auth(subject_ref="")):
            with self.subTest(context=repr(bad)[:40]):
                self.reason(self.world.build(context=bad), "authenticated-context-required")
        other = self.world.build(context=auth(subject_ref="subject:guest"))
        self.assertNotEqual(other.digest, projection.digest)  # type: ignore[union-attr]
        text = json.dumps(projection.data)
        self.assertNotIn("subject:operator", text)
        self.assertNotIn("role:operator", text)

    def test_presence_surface_state_is_server_owned_and_not_public_input(self) -> None:
        projection = self.world.projection()
        state = ready_surface()
        for key in ("surface_state", "phase", "connectivity", "lifecycle", "interaction_modes"):
            with self.subTest(key=key):
                req = request(projection)
                req[key] = "x"
                self.reason(pr.parse_presence_interaction_request(resign(req, "request_digest")), "closed-object")
        self.reason(render(projection, dict(state.data)), "wrong-type")  # type: ignore[arg-type]
        self.reason(render(projection, state, adapter(receipt_overrides={"phase": "present"})), "closed-object")
        forged_data = copy.deepcopy(dict(state.data))
        forged_data["connectivity"] = "live"
        forged_data["surface_kind"] = "card"
        self.reason(render(projection, pr.PresenceSurfaceState(forged_data), readiness=ready(state)), "tampered-source-binding")
        before = state.digest
        result = ok(self, render(projection, state, adapter(displayed_overrides={"connectivity": "live"})), pr.PresenceRenderResult)
        self.assertEqual(state.digest, before)
        self.assertEqual(result.data["surface_state_digest"], before)  # type: ignore[union-attr]
        schema = json.loads(pr.generate_presence_interaction_request_schema())
        self.assertEqual(
            set(schema["properties"]),
            {"schema_version", "request_id", "surface_id", "purpose", "input", "observed_projection_revision", "observed_projection_digest", "consent_ref", "request_digest"},
        )

    def test_presence_render_result_binds_adapter_surface_and_projection_digest(self) -> None:
        projection = self.world.projection()
        surface_a = ready_surface("surface-display")
        surface_b = ready_surface("surface-wearable", surface_kind="wearable")
        fixture = adapter()
        result_a = ok(self, render(projection, surface_a, fixture), pr.PresenceRenderResult)
        result_b = ok(self, render(projection, surface_b, fixture), pr.PresenceRenderResult)
        for result, state in ((result_a, surface_a), (result_b, surface_b)):
            data = result.data  # type: ignore[union-attr]
            self.assertEqual(data["surface_id"], state.surface_id)
            self.assertEqual(data["surface_state_digest"], state.digest)
            self.assertEqual(data["projection_revision"], projection.revision)
            self.assertEqual(data["observed_projection_revision"], projection.revision)
            self.assertEqual(data["projection_digest"], projection.digest)
            receipt = {
                "surface_id": state.surface_id,
                "projection_id": projection.data["projection_id"],
                "projection_revision": projection.revision,
                "projection_digest": projection.digest,
                "displayed": data["indicators"],
                "rendered_at": ago(1),
            }
            self.assertEqual(data["adapter_receipt_digest"], pr._sha_json(receipt))
        self.assertEqual(fixture.rendered[0], fixture.rendered[1])
        self.assertEqual(self.world.projection().revision, projection.revision)
        self.reason(render(projection, surface_a, adapter(receipt_overrides={"surface_id": "surface-wearable"})), "surface-mismatch")
        self.reason(render(projection, surface_a, readiness=ready(surface_b)), "readiness-required")

    def test_presence_render_refuses_stale_live_projection(self) -> None:
        projection = self.world.projection()
        stale_surface = ready_surface(connectivity_observed_at=ago(600))
        self.reason(render(projection, stale_surface), "stale-live-projection")
        stale_projection = self.world.projection(companion=self.world.companion(observed_at=ago(600)))
        self.reason(render(stale_projection, ready_surface()), "stale-live-projection")
        honest = observe(stale_surface, "degraded", ago(600))
        result = ok(self, render(stale_projection, honest), pr.PresenceRenderResult)
        self.assertEqual(result.data["indicators"]["connectivity"], "degraded")  # type: ignore[union-attr]

    def test_presence_render_exposes_degraded_disconnected_provisional_indicators(self) -> None:
        projection = self.world.projection()
        for connectivity in ("degraded", "disconnected", "provisional", "live"):
            with self.subTest(connectivity=connectivity):
                state = observe(ready_surface(), connectivity)
                result = ok(self, render(projection, state), pr.PresenceRenderResult)
                self.assertEqual(result.data["indicators"]["connectivity"], connectivity)  # type: ignore[union-attr]
                self.assertEqual(set(result.data["indicators"]), set(pr.INDICATORS))  # type: ignore[union-attr]

    def test_presence_render_refuses_hidden_privacy_recording_action_indicator(self) -> None:
        projection = self.world.projection()
        state = ready_surface()
        for key in pr.INDICATORS:
            with self.subTest(dropped=key):
                self.reason(render(projection, state, adapter(drop_displayed=(key,))), "indicator-hidden")
            with self.subTest(nulled=key):
                self.reason(render(projection, state, adapter(displayed_overrides={key: None})), "indicator-hidden")
        self.reason(render(projection, state, adapter(receipt_overrides={"displayed": None})), "indicator-hidden")

    def test_presence_interaction_requires_surface_actor_purpose_consent_and_revision(self) -> None:
        world = self.world
        projection = world.projection()
        state = ready_surface()
        decision = ok(self, accept(world, projection, state, envelope(request(projection))), pr.PresenceInteractionDecision)
        data = decision.data  # type: ignore[union-attr]
        self.assertEqual(data["intent"], {"kind": "conversation_turn", "mode": "text", "content": "hello there", "purpose": "companionship"})
        self.assertEqual(data["auth_context_digest"], pr.presence_auth_context_digest(world.context))
        self.reason(accept(world, projection, state, envelope(request(projection), surface_id="surface-other")), "surface-mismatch")
        self.reason(accept(world, projection, state, envelope(request(projection, surface_id="surface-other"))), "surface-mismatch")
        self.reason(accept(world, projection, state, envelope(request(projection)), context=None), "authenticated-context-required")
        self.reason(accept(world, projection, state, envelope(request(projection, purpose="advertising"))), "purpose-drift")
        self.reason(accept(world, projection, state, envelope(request(projection)), consent_evidence=[]), "missing-consent")
        self.reason(accept(world, projection, state, envelope(request(projection, consent_ref="consent:other"))), "missing-consent")
        self.reason(accept(world, projection, state, envelope(request(projection, observed_projection_revision=2))), "projection-revision-mismatch")
        self.reason(accept(world, projection, state, envelope(request(projection)), consent_evidence=[grant(world, "surface-wearable")]), "consent-binding-mismatch")
        self.reason(accept(world, projection, state, envelope(request(projection)), consent_evidence=[grant(world, context=auth(subject_ref="subject:guest"))]), "consent-binding-mismatch")
        self.reason(accept(world, projection, state, envelope(request(projection)), consent_evidence=[grant(world, expires_at=ago(1))]), "expired-consent")
        self.reason(accept(world, projection, state, envelope(request(projection)), consent_evidence=[grant(world, revoked=True)]), "revoked-consent")
        self.reason(accept(world, projection, state, envelope(request(projection)), consent_evidence=[grant(world, subject_scope="bystander")]), "missing-consent")
        self.reason(
            accept(world, projection, state, envelope(request(projection)), consent_evidence=[grant(world), grant(world, subject_scope="known_guest")]),
            "contradictory-consent",
        )
        text_only = ready_surface(interaction_modes=("text",))
        self.reason(accept(world, projection, text_only, envelope(request(projection, mode="voice"))), "interaction-mode-unavailable")
        quiet = world.projection(companion=world.companion(availability="quiet"))
        self.reason(accept(world, quiet, state, envelope(request(quiet))), "availability-override")
        self.reason(accept(world, quiet, state, envelope(request(quiet, mode="control", content="request_available"))), "availability-override")
        reducing = ok(self, accept(world, quiet, state, envelope(request(quiet, mode="control", content="request_quiet"))), pr.PresenceInteractionDecision)
        self.assertEqual(reducing.data["intent"]["kind"], "control_proposal")  # type: ignore[union-attr]

    def test_presence_interaction_refuses_projection_replay(self) -> None:
        world = self.world
        first = world.projection()
        second = world.projection(receipt=world.chained_receipt(), prior=first)
        self.assertEqual(second.revision, first.revision + 1)
        self.assertEqual(second.data["continuity"]["prior_checkpoint_sha256"], first.data["continuity"]["checkpoint_payload_sha256"])  # type: ignore[index]
        state = ready_surface()
        self.reason(accept(world, second, state, envelope(request(first))), "projection-replay")
        replay_fixture = adapter(receipt_overrides={"projection_revision": first.revision, "projection_digest": first.digest})
        self.reason(render(second, state, replay_fixture), "projection-replay")
        ok(self, accept(world, second, state, envelope(request(second))), pr.PresenceInteractionDecision)

    def test_presence_interaction_reuses_consent_replay_cache_for_presence_scope(self) -> None:
        world = self.world
        projection = world.projection()
        state = ready_surface()
        cache = b.ConsentReplayCache()
        refused = accept(world, projection, state, envelope(request(projection, purpose="advertising")), replay_cache=cache)
        self.reason(refused, "purpose-drift")
        self.reason(accept(world, projection, state, envelope(request(projection, observed_projection_revision=9)), replay_cache=cache), "projection-revision-mismatch")
        self.assertEqual(cache.seen, set(), "a refused attempt must not use up the grant")
        self.assertTrue(cache.mark("consent:1"), "a B sensory key of the same ref is a different namespace")
        ok(self, accept(world, projection, state, envelope(request(projection)), replay_cache=cache), pr.PresenceInteractionDecision)
        self.assertEqual(cache.seen, {"consent:1", "presence_recording_interaction|consent:1"})
        self.reason(accept(world, projection, state, envelope(request(projection, request_id="req:2", content="again")), replay_cache=cache), "replayed-consent")
        self.reason(accept(world, projection, state, envelope(request(projection)), replay_cache=cache), "replayed-consent")
        b_cache = b.ConsentReplayCache()
        ok(self, accept(world, projection, state, envelope(request(projection)), replay_cache=b_cache), pr.PresenceInteractionDecision)
        self.assertTrue(b_cache.mark("consent:1"), "the presence key cannot collide with a B key")
        self.reason(accept(world, projection, state, envelope(request(projection)), replay_cache=set()), "wrong-type")

    def test_presence_surface_cannot_mint_identity_memory_authority_or_direct_actuation(self) -> None:
        world = self.world
        projection = world.projection()
        state = ready_surface()
        for key in ("persona_id", "memory_ref", "identity", "authority", "relationship_truth", "credential", "effect", "actuation_plan"):
            with self.subTest(key=key):
                req = request(projection)
                req["input"][key] = "x"  # type: ignore[index]
                self.reason(accept(world, projection, state, envelope(req)), "identity-authority-lookalike")
        for content in ("unlock_door", "invoke:light-control", "set brightness 100"):
            with self.subTest(content=content):
                req = request(projection, mode="text")
                req["input"] = {"mode": "control", "content": content}
                self.reason(pr.parse_presence_interaction_request(resign(req, "request_digest")), "direct-actuation-forbidden")
        req = request(projection)
        req["input"]["capability_id"] = "light-control"  # type: ignore[index]
        self.reason(pr.parse_presence_interaction_request(resign(req, "request_digest")), "closed-object")
        decision = ok(self, accept(world, projection, state, envelope(request(projection, mode="control", content="request_stop"))), pr.PresenceInteractionDecision)
        self.assertIn(decision.data["intent"]["kind"], pr.INTENT_KIND)  # type: ignore[union-attr]
        self.assertFalse({"continuity", "persona_id", "navigator", "plan_digest", "effect"} & set(decision.data))  # type: ignore[union-attr]
        tree = ast.parse(MODULE_PATH.read_text())
        b_attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "b"}
        self.assertFalse({a for a in b_attrs if "actuat" in a or a in ("invoke", "rollback_effect", "request_cancellation", "plan_actuation")})
        self.assertFalse(any(isinstance(n, ast.Attribute) and n.attr in ("write_text", "write_bytes", "open") for n in ast.walk(tree) if not _in_main(tree, n)))

    def test_presence_lifecycle_readiness_requires_all_five_legs_or_reviewed_waiver(self) -> None:
        full = ok(self, readiness(ready_surface()), pr.PresenceReadiness)
        self.assertEqual(full.waived_legs, ())  # type: ignore[union-attr]
        for leg in pr.LIFECYCLE_LEGS:
            with self.subTest(missing=leg):
                state = ready_surface(skip=(leg,))
                self.reason(readiness(state), "health-unknown" if leg == "health" else "lifecycle-leg-missing")
        partial = ready_surface(skip=("update", "remove"))
        waived = pr.waive_presence_lifecycle_legs(
            partial,
            waiver_id="waiver:1",
            waived_legs=("update", "remove"),
            reason="fixture display has no update channel",
            review_ref="review:t-3227",
            reviewed_at=ago(60),
            expires_at=z(NOW + timedelta(days=1)),
            expected_state_digest=partial.digest,
            now=NOW,
        )
        assert isinstance(waived, pr.PresenceSurfaceState), waived
        waiver = waived.data["lifecycle"]["waiver"]  # type: ignore[index]
        self.reason(readiness(waived), "lifecycle-waiver-invalid")
        result = ok(self, readiness(waived, frozenset({waiver["waiver_digest"]})), pr.PresenceReadiness)
        self.assertEqual(result.waived_legs, ("remove", "update"))  # type: ignore[union-attr]
        more = step(waived, event("remove", "rehearsed"))
        self.reason(readiness(more, frozenset({waiver["waiver_digest"]})), "lifecycle-waiver-invalid")
        self.reason(
            pr.waive_presence_lifecycle_legs(
                partial, waiver_id="waiver:2", waived_legs=("health",), reason="r", review_ref="review:x",
                reviewed_at=ago(60), expires_at=z(NOW + timedelta(days=1)), expected_state_digest=partial.digest, now=NOW,
            ),
            "lifecycle-waiver-invalid",
        )
        self.reason(readiness(ready_surface(health="unknown")), "health-unknown")
        self.reason(readiness(ready_surface(health="degraded")), "health-unhealthy")
        stale = new_surface()
        for leg, outcome, revision in FULL_LEGS[:-1]:
            stale = step(stale, event(leg, outcome, to_revision=revision))
        stale = step(stale, event("health", "healthy", observed_at=ago(3600)))
        self.reason(readiness(stale), "health-stale")
        interrupted = step(ready_surface(), event("update", "interrupted"))
        self.reason(readiness(interrupted), "lifecycle-transition-invalid")
        removed = step(ready_surface(), event("remove", "executed"))
        self.reason(readiness(removed), "surface-removed")

    def test_presence_replacement_preserves_text_voice_control_fallback(self) -> None:
        world = self.world
        projection = world.projection()
        surface_a = ready_surface("surface-display")
        surface_b = ready_surface("surface-wearable", surface_kind="wearable")
        removed_a = step(surface_a, event("remove", "executed"))
        self.reason(render(projection, removed_a, readiness=ready(surface_a)), "surface-removed")
        self.reason(accept(world, projection, removed_a, envelope(request(projection)), readiness=ready(surface_a)), "surface-removed")
        surface_c = ready_surface("surface-card", surface_kind="card")
        results = [ok(self, render(projection, s), pr.PresenceRenderResult) for s in (surface_b, surface_c)]
        for result in results:
            self.assertEqual(result.data["fallback_channels"], ["control", "text", "voice"])  # type: ignore[union-attr]
            self.assertEqual(result.data["projection_digest"], projection.digest)  # type: ignore[union-attr]
        self.assertEqual(projection.data["state"]["fallback_channels"], ["control", "text", "voice"])  # type: ignore[index]
        reprovisioned = step(removed_a, event("install", "verified"))
        self.assertEqual(reprovisioned.data["phase"], "present")
        self.assertIsNone(reprovisioned.data["lifecycle"]["health"])  # type: ignore[index]
        self.reason(readiness(reprovisioned), "health-unknown")
        for subset in (("text",), ("control", "text"), ("text", "voice"), ()):
            with self.subTest(subset=subset):
                self.reason(world.build(companion=world.companion(fallback_channels=subset)), "fallback-incomplete")

    def test_presence_recovery_uses_durable_projection_not_session_local_truth(self) -> None:
        world = self.world
        projection = world.projection()
        durable = b.canonical_json_bytes(pr.presence_projection_to_json(projection))

        def recover(source: object, **changes: object) -> object:
            kwargs: dict[str, object] = {
                "view_path": world.view_path,
                "checkpoint_receipt": world.receipt,
                "checkpoint_root": world.fx.checkpoints,
                "navigator": world.navigator,
                "context": world.context,
                "companion": world.companion(),
                "now": NOW,
            }
            kwargs.update(changes)
            return pr.recover_presence_projection(source, **kwargs)  # type: ignore[arg-type]

        recovered = ok(self, recover(durable), pr.PresenceProjection)
        self.assertEqual(recovered, projection)
        for session_local in (projection, dict(projection.data), durable.decode("utf-8"), bytearray(durable)):
            with self.subTest(source=type(session_local).__name__):
                self.reason(recover(session_local), "recovery-source-invalid")
        tampered = pr.presence_projection_to_json(projection)
        tampered["continuity"]["checkpoint_receipt_sha256"] = "9" * 64  # type: ignore[index]
        self.reason(recover(b.canonical_json_bytes(resign(tampered, "projection_digest"))), "continuity-binding-mismatch")
        self.reason(recover(durable, context=auth(subject_ref="subject:guest")), "projection-binding-mismatch")
        self.reason(recover(durable, checkpoint_receipt=world.chained_receipt()), "continuity-binding-mismatch")
        restarted = ready_surface(connectivity_observed_at=ago(900))
        self.reason(render(recovered, restarted), "stale-live-projection")  # type: ignore[arg-type]
        ok(self, render(recovered, observe(restarted, "live")), pr.PresenceRenderResult)  # type: ignore[arg-type]

    def test_bridge_presence_adapter_is_fixture_only_and_does_not_require_live_bridge(self) -> None:
        self.assertIs(pr.FixtureBridgePresenceAdapter.fixture_only, True)
        self.assertEqual(pr.ADAPTER_CLASS, ("fixture",))
        self.assertEqual(pr.BridgePresenceAdapter.__annotations__["fixture_only"], "Literal[True]")
        projection = self.world.projection()
        state = ready_surface()

        class LiveAdapter(pr.FixtureBridgePresenceAdapter):
            fixture_only = False  # type: ignore[assignment]

        class TruthyAdapter(pr.FixtureBridgePresenceAdapter):
            fixture_only = "yes"  # type: ignore[assignment]

        class NoFlag:
            def render(self, projection_json: object, **_: object) -> dict[str, object]:
                return {}

        for bad in (LiveAdapter(clock=ago(1)), TruthyAdapter(clock=ago(1)), NoFlag()):
            with self.subTest(adapter=type(bad).__name__):
                self.reason(render(projection, state, bad), "adapter-not-fixture")
        surface_schema = json.loads(pr.generate_presence_surface_state_schema())
        self.assertEqual(surface_schema["properties"]["adapter"]["properties"]["adapter_class"]["enum"], ["fixture"])
        tree = ast.parse(MODULE_PATH.read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        allowed = {"__future__", "copy", "functools", "hashlib", "json", "sys", "collections.abc", "dataclasses", "datetime", "pathlib", "typing", "tools"}
        self.assertLessEqual(imported, allowed)
        source = MODULE_PATH.read_text()
        for token in ("socket", "urllib", "http.client", "requests", "subprocess", "dbus", "bridge_client"):
            self.assertNotIn(f"import {token}", source)
        broken = adapter()
        broken.render = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("bridge down"))  # type: ignore[method-assign]
        self.reason(render(projection, state, broken), "adapter-receipt-mismatch")

    # ------------------------------------------------------------- CORRECTIVE TESTS (old bypasses)

    def test_presence_generated_schemas_accept_every_valid_runtime_record(self) -> None:
        world = self.world
        committed = {name: json.loads((REPO_ROOT / "schemas" / name).read_bytes()) for name in pr.generate_schemas()}

        def valid(kind: str, record: object) -> None:
            data = record.data if hasattr(record, "data") else record  # type: ignore[union-attr]
            schema = committed[f"org060-presence-{kind}.schema.json"]
            self.assertIsNone(b._validate_json_schema_subset(data, schema), (kind, data))

        first = world.projection()
        valid("projection", first)
        second = world.projection(receipt=world.chained_receipt(), prior=first, companion=world.companion(availability="away", privacy="redacted_egress", recording="recording_paused", action="acting"))
        valid("projection", second)
        self.assertIsNotNone(second.data["continuity"]["prior_checkpoint_sha256"])  # type: ignore[index]
        state = new_surface(interaction_modes=("display_only",))
        valid("surface-state", state)
        for leg, outcome, revision in FULL_LEGS:
            state = step(state, event(leg, outcome, to_revision=revision))
            valid("surface-state", state)
        valid("surface-state", step(state, event("update", "interrupted")))
        valid("surface-state", step(state, event("update", "rolled_back")))
        valid("surface-state", step(state, event("remove", "executed")))
        partial = ready_surface(skip=("configure",))
        waived = pr.waive_presence_lifecycle_legs(
            partial, waiver_id="waiver:1", waived_legs=("configure",), reason="r", review_ref="review:1",
            reviewed_at=ago(60), expires_at=z(NOW + timedelta(days=1)), expected_state_digest=partial.digest, now=NOW,
        )
        valid("surface-state", waived)
        for connectivity in pr.CONNECTIVITY:
            surface = observe(ready_surface(), connectivity)
            valid("render-result", render(first, surface))
        for mode, content in (("text", "hi"), ("voice", "transcribed words"), ("control", "request_stop")):
            req = request(first, mode=mode, content=content)
            valid("interaction-request", req)
            valid("interaction-decision", accept(world, first, ready_surface(), envelope(req)))
        # Schema-accepted, runtime-refused: each sample is schema-valid and maps to one documented gap.
        base = pr.presence_projection_to_json(first)
        surface = presence_json(ready_surface())
        rendered = pr.presence_render_result_to_json(render(first, ready_surface()))  # type: ignore[arg-type]

        def mutated(record: dict[str, object], path: tuple[str, ...], value: object, digest: str) -> dict[str, object]:
            out = copy.deepcopy(record)
            target = out
            for key in path[:-1]:
                target = target[key]  # type: ignore[assignment]
            target[path[-1]] = value
            return resign(out, digest)

        gaps = {
            "digest-binding": ("projection", dict(base, projection_digest=SHA_X), "tampered-source-binding"),
            "calendar-validity": ("projection", mutated(base, ("observed_at",), "2026-02-30T00:00:00Z", "projection_digest"), "invalid-pattern"),
            "time-ordering": ("projection", mutated(base, ("issued_at",), "2026-01-01T00:00:00Z", "projection_digest"), "invalid-pattern"),
            "canonical-array-order": ("surface-state", mutated(surface, ("interaction_modes",), ["text", "control"], "state_digest"), "invalid-enum"),
            "validator-capability-min-unique-items": ("surface-state", mutated(surface, ("interaction_modes",), ["text", "text"], "state_digest"), "invalid-enum"),
            "lifecycle-cross-field": ("surface-state", mutated(surface, ("phase",), "removed", "state_digest"), "lifecycle-transition-invalid"),
            "revision-chain": ("surface-state", mutated(surface, ("prior_state_digest",), None, "state_digest"), "tampered-source-binding"),
            "projection-identity": ("projection", mutated(base, ("projection_id",), "presence-projection:artisan", "projection_digest"), "continuity-binding-mismatch"),
            "observed-revision-binding": ("render-result", mutated(rendered, ("observed_projection_revision",), 7, "render_digest"), "projection-revision-mismatch"),
        }
        self.assertEqual(set(gaps), set(pr.SCHEMA_RUNTIME_GAPS))
        parsers = {
            "projection": pr.parse_presence_projection,
            "surface-state": pr.parse_presence_surface_state,
            "render-result": pr.parse_presence_render_result,
        }
        for gap, (kind, record, expected) in gaps.items():
            with self.subTest(gap=gap):
                valid(kind, record)
                self.reason(parsers[kind](record), expected)

    def test_presence_mapping_parsers_enforce_bounds_and_types(self) -> None:
        world = self.world
        projection = world.projection()
        req = request(projection)
        surface = presence_json(ready_surface())
        proj = pr.presence_projection_to_json(projection)

        def change(record: dict[str, object], path: tuple[str, ...], value: object, digest: str) -> dict[str, object]:
            out = copy.deepcopy(record)
            target = out
            for key in path[:-1]:
                target = target[key]  # type: ignore[assignment]
            target[path[-1]] = value
            return resign(out, digest) if _jsonable(out) else out

        cases = [
            ("request content oversize", pr.parse_presence_interaction_request, change(req, ("input", "content"), "x" * (pr.CONTENT_LIMIT + 1), "request_digest"), "byte-bound-exceeded"),
            ("request purpose oversize", pr.parse_presence_interaction_request, change(req, ("purpose",), "p" * (pr.SUMMARY_LIMIT + 1), "request_digest"), "byte-bound-exceeded"),
            ("request revision str", pr.parse_presence_interaction_request, change(req, ("observed_projection_revision",), "1", "request_digest"), "wrong-type"),
            ("request revision bool", pr.parse_presence_interaction_request, change(req, ("observed_projection_revision",), True, "request_digest"), "wrong-type"),
            ("request revision zero", pr.parse_presence_interaction_request, change(req, ("observed_projection_revision",), 0, "request_digest"), "invalid-enum"),
            ("request input list", pr.parse_presence_interaction_request, change(req, ("input",), ["text"], "request_digest"), "wrong-type"),
            ("request nested extra", pr.parse_presence_interaction_request, change(req, ("input", "extra"), "x", "request_digest"), "closed-object"),
            ("request nested lookalike", pr.parse_presence_interaction_request, change(req, ("input", "uid"), 1000, "request_digest"), "identity-authority-lookalike"),
            ("request non-json value", pr.parse_presence_interaction_request, change(req, ("purpose",), NOW, "request_digest"), "noncanonical-json"),
            ("request whole oversize", pr.parse_presence_interaction_request, dict(req, padding="x" * pr.WIRE_LIMIT), "byte-bound-exceeded"),
            ("surface modes oversize", pr.parse_presence_surface_state, change(surface, ("interaction_modes",), ["control", "display_only", "text", "voice", "voice"], "state_digest"), "byte-bound-exceeded"),
            ("surface revision bool", pr.parse_presence_surface_state, change(surface, ("state_revision",), True, "state_digest"), "wrong-type"),
            ("surface nested extra", pr.parse_presence_surface_state, change(surface, ("lifecycle", "install", "fixture"), True, "state_digest"), "closed-object"),
            ("surface leg wrong outcome", pr.parse_presence_surface_state, change(surface, ("lifecycle", "install", "outcome"), "healthy", "state_digest"), "invalid-enum"),
            ("surface id pattern", pr.parse_presence_surface_state, change(surface, ("surface_id",), "Surface One", "state_digest"), "invalid-pattern"),
            ("projection nested lookalike-shaped extra", pr.parse_presence_projection, change(proj, ("continuity", "role_refs"), ["role:x"], "projection_digest"), "closed-object"),
            ("projection revision float", pr.parse_presence_projection, change(proj, ("projection_revision",), 1.0, "projection_digest"), "wrong-type"),
            ("projection state enum", pr.parse_presence_projection, change(proj, ("state", "recording"), "hidden", "projection_digest"), "invalid-enum"),
            ("projection hostile control", pr.parse_presence_projection, change(proj, ("observed_at",), "2026-10-26T11:59:55Z\u202e", "projection_digest"), "hostile-control"),
            ("projection navigator sha", pr.parse_presence_projection, change(proj, ("navigator", "lock_sha256"), "a" * 64, "projection_digest"), "invalid-pattern"),
        ]
        for label, parser, value, expected in cases:
            with self.subTest(label):
                self.reason(parser(value), expected)
        for label, kwargs, expected in (
            ("ctor content oversize", {"content": "x" * (pr.CONTENT_LIMIT + 1)}, "byte-bound-exceeded"),
            ("ctor revision bool", {"observed_projection_revision": True}, "wrong-type"),
            ("ctor mode", {"mode": "telepathy"}, "invalid-enum"),
            ("ctor non-json", {"purpose": object()}, "wrong-type"),
        ):
            with self.subTest(label):
                base_kwargs = {
                    "request_id": "req:1", "surface_id": "surface-display", "purpose": "companionship", "mode": "text", "content": "hi",
                    "observed_projection_revision": 1, "observed_projection_digest": projection.digest, "consent_ref": "consent:1",
                }
                base_kwargs.update(kwargs)
                self.reason(pr.make_presence_interaction_request(**base_kwargs), expected)  # type: ignore[arg-type]
        with self.subTest("ctor surface modes oversize"):
            self.reason(pr.make_presence_surface_state(
                surface_id="surface-x", surface_kind="display", connectivity="live", connectivity_observed_at=ago(1),
                interaction_modes=("control", "display_only", "text", "voice", "text"), adapter_id="adapter:x",
                adapter_revision="r", exact_revision="r", rollback_ref="r", now=NOW,
            ), "byte-bound-exceeded")

    def test_presence_refuses_split_or_forged_live_state(self) -> None:
        world = self.world
        first = world.projection()
        self.reason(world.build(receipt=world.chained_receipt("boundary-3", chained=False), prior=first), "split-projection")
        state = ready_surface()
        self.reason(render(first, state, adapter(receipt_overrides={"projection_digest": SHA_Y})), "split-projection")
        self.reason(render(first, state, adapter(receipt_overrides={"projection_revision": first.revision + 1})), "projection-revision-mismatch")
        self.reason(accept(world, first, state, envelope(request(first, observed_projection_digest=SHA_Y))), "split-projection")
        forged_data = pr.presence_projection_to_json(first)
        forged_data["state"]["recording"] = "not_recording"  # type: ignore[index]
        self.reason(render(pr.PresenceProjection(forged_data), state), "tampered-source-binding")
        resigned = pr.parse_presence_projection(resign(copy.deepcopy(forged_data), "projection_digest"))
        assert isinstance(resigned, pr.PresenceProjection)
        self.reason(accept(world, first, state, envelope(request(resigned))), "split-projection")
        degraded = observe(state, "degraded")
        self.reason(render(first, degraded, adapter(displayed_overrides={"connectivity": "live"})), "forged-live-state")
        self.reason(render(first, ready_surface(connectivity_observed_at=ago(120))), "stale-live-projection")
        self.reason(render(first, state, adapter(clock=z(NOW + timedelta(seconds=30)))), "future-timestamp")
        self.reason(render(first, state, adapter(clock=ago(600))), "adapter-receipt-mismatch")

    def test_presence_indicators_cannot_contradict_server_state(self) -> None:
        world = self.world
        recording = world.projection()
        state = ready_surface()
        for key, lie in (("recording", "not_recording"), ("privacy", "redacted_egress"), ("action", "acting"), ("availability", "quiet"), ("connectivity", "degraded")):
            with self.subTest(key=key):
                self.reason(render(recording, state, adapter(displayed_overrides={key: lie})), "indicator-contradiction")
        quiet = world.projection(companion=world.companion(availability="quiet", recording="not_recording"))
        self.reason(render(quiet, state, adapter(displayed_overrides={"availability": "available"})), "indicator-contradiction")
        self.reason(render(quiet, state, adapter(displayed_overrides={"recording": "recording"})), "indicator-contradiction")
        result = ok(self, render(quiet, state), pr.PresenceRenderResult)
        self.assertEqual(
            result.data["indicators"],  # type: ignore[union-attr]
            {"connectivity": "live", "availability": "quiet", "privacy": "local_only", "recording": "not_recording", "action": "idle"},
        )

    def test_presence_lifecycle_waiver_digest_has_no_fixture_bypass(self) -> None:
        partial = ready_surface(skip=("configure",))

        def waive(**changes: object) -> object:
            kwargs: dict[str, object] = {
                "waiver_id": "waiver:1", "waived_legs": ("configure",), "reason": "r", "review_ref": "review:1",
                "reviewed_at": ago(60), "expires_at": z(NOW + timedelta(days=1)), "expected_state_digest": partial.digest, "now": NOW,
            }
            kwargs.update(changes)
            return pr.waive_presence_lifecycle_legs(partial, **kwargs)  # type: ignore[arg-type]

        waived = waive()
        assert isinstance(waived, pr.PresenceSurfaceState)
        real_digest = waived.data["lifecycle"]["waiver"]["waiver_digest"]  # type: ignore[index]
        ok(self, readiness(waived, frozenset({real_digest})), pr.PresenceReadiness)
        flagged = presence_json(waived)
        flagged["lifecycle"]["waiver"]["fixture"] = True  # type: ignore[index]
        self.reason(pr.parse_presence_surface_state(resign(flagged, "state_digest")), "closed-object")
        magic = presence_json(waived)
        magic["lifecycle"]["waiver"]["waiver_digest"] = "sha256:" + "0" * 64  # type: ignore[index]
        self.reason(pr.parse_presence_surface_state(resign(magic, "state_digest")), "lifecycle-waiver-invalid")
        self.reason(readiness(waived, frozenset({"sha256:" + "0" * 64})), "lifecycle-waiver-invalid")
        self.reason(readiness(waived, frozenset()), "lifecycle-waiver-invalid")
        self.reason(readiness(waived, frozenset({"*"})), "lifecycle-waiver-invalid")
        self.reason(readiness(waived, frozenset({real_digest.upper()})), "lifecycle-waiver-invalid")
        self.reason(waive(waived_legs=()), "lifecycle-waiver-invalid")
        self.reason(waive(waived_legs=("configure", "configure")), "lifecycle-waiver-invalid")
        self.reason(waive(waived_legs="configure"), "lifecycle-waiver-invalid")
        self.reason(waive(waived_legs=("health",)), "lifecycle-waiver-invalid")
        self.reason(waive(reviewed_at=z(NOW + timedelta(hours=1))), "future-timestamp")
        expired = waive(reviewed_at=ago(7200), expires_at=ago(3600))
        assert isinstance(expired, pr.PresenceSurfaceState)
        self.reason(readiness(expired, frozenset({expired.data["lifecycle"]["waiver"]["waiver_digest"]})), "lifecycle-waiver-invalid")  # type: ignore[index]
        self.reason(readiness(waived, [real_digest]), "wrong-type")  # type: ignore[arg-type]
        signature = inspect.signature(pr.waive_presence_lifecycle_legs)
        self.assertTrue(all(p.default is inspect.Parameter.empty for p in signature.parameters.values()))
        self.assertFalse([f.name for f in dataclasses.fields(pr.PresenceReadiness) if f.default is not dataclasses.MISSING])
        readiness_source = inspect.getsource(pr.evaluate_presence_readiness)
        for token in ("fixture", "test", "bypass", "skip"):
            self.assertNotIn(token, readiness_source)

    def test_presence_recovery_refuses_fallback_subsets(self) -> None:
        world = self.world
        projection = world.projection()

        def recover(source: bytes, companion: pr.PresenceCompanionState | None = None) -> object:
            return pr.recover_presence_projection(
                source, view_path=world.view_path, checkpoint_receipt=world.receipt, checkpoint_root=world.fx.checkpoints,
                navigator=world.navigator, context=world.context, companion=companion or world.companion(), now=NOW,
            )

        for subset in (["text"], ["control", "text"], ["text", "voice"], ["control", "voice"], [], ["control", "text", "voice", "voice"]):
            with self.subTest(subset=subset):
                data = pr.presence_projection_to_json(projection)
                data["state"]["fallback_channels"] = subset  # type: ignore[index]
                self.reason(recover(b.canonical_json_bytes(resign(data, "projection_digest"))), "fallback-incomplete")
        durable = b.canonical_json_bytes(pr.presence_projection_to_json(projection))
        self.reason(recover(durable, world.companion(fallback_channels=("text", "voice"))), "fallback-incomplete")
        self.reason(recover(durable, world.companion(fallback_channels=["control", "text", "voice"])), "fallback-incomplete")  # type: ignore[arg-type]
        ok(self, recover(durable), pr.PresenceProjection)

    def test_presence_refusals_use_the_shared_vocabulary_guard(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text())
        parents = _parents(tree)
        used: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            owner = _enclosing_function(node, parents)
            if name in ("PresenceRefusal", "TypedRefusal"):
                self.assertEqual(owner, "_refuse", ast.dump(node)[:120])
            if name == "_refuse":
                first = node.args[0] if node.args else None
                if owner == "_lift":
                    continue
                self.assertIsInstance(first, ast.Constant, f"{owner}: non-literal refusal reason")
                self.assertIn(first.value, pr.PRESENCE_REFUSAL_CODES, owner)  # type: ignore[union-attr]
                used.add(first.value)  # type: ignore[union-attr]
        self.assertEqual(pr.PRESENCE_ONLY_REFUSAL_CODES - used, set(), "every presence-only code is reachable")
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and not _in_main(tree, node):
                self.assertEqual(_enclosing_function(node, parents), "__post_init__")
        with self.assertRaises(ValueError):
            pr.PresenceRefusal("made-up-reason", "x")
        refusal = pr._lift(b.TypedRefusal("closed-object", "x"))
        self.assertIs(type(refusal), pr.PresenceRefusal)
        self.assertIsInstance(refusal, b.TypedRefusal)
        self.assertTrue(b.REFUSAL_CODES <= pr.PRESENCE_REFUSAL_CODES)
        world = self.world
        projection = world.projection()
        state = ready_surface()
        corpus = [
            pr.parse_presence_projection(b"not json"),
            pr.parse_presence_interaction_request({"x": 1}),
            world.build(companion=world.companion(fallback_channels=("text",))),
            render(projection, state, adapter(drop_displayed=("privacy",))),
            accept(world, projection, state, envelope(request(projection)), consent_evidence=[]),
            readiness(new_surface()),
            apply(state, event("install", "verified")),
            pr.recover_presence_projection(
                "x", view_path=world.view_path, checkpoint_receipt=world.receipt, checkpoint_root=world.fx.checkpoints,  # type: ignore[arg-type]
                navigator=world.navigator, context=world.context, companion=world.companion(), now=NOW,
            ),
            pr.build_presence_projection(
                view_path=world.view_path, checkpoint_receipt=world.receipt, checkpoint_root=world.fx.checkpoints,
                navigator=world.navigator, context=world.context, companion=world.companion(), prior=None, now="now",  # type: ignore[arg-type]
            ),
        ]
        for item in corpus:
            self.assertIs(type(item), pr.PresenceRefusal, item)
            self.assertIn(item.reason, pr.PRESENCE_REFUSAL_CODES)  # type: ignore[union-attr]

    # ------------------------------------------------------------- support tests

    def test_presence_generated_schemas_match_committed_files_and_ci_covers_paths(self) -> None:
        def walk(node: object, path: str = "$") -> None:
            if isinstance(node, dict):
                if node.get("type") == "object" and "properties" in node:
                    self.assertIs(node.get("additionalProperties"), False, path)
                    self.assertEqual(set(node.get("required", ())), set(node["properties"]), path)
                if node.get("type") == "array":
                    self.assertIn("maxItems", node, path)
                if node.get("type") == "string":
                    self.assertTrue({"maxLength", "enum", "pattern"} & set(node), path)
                for key, value in node.items():
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for idx, value in enumerate(node):
                    walk(value, f"{path}[{idx}]")

        expected = {
            "org060-presence-projection.schema.json",
            "org060-presence-surface-state.schema.json",
            "org060-presence-render-result.schema.json",
            "org060-presence-interaction-request.schema.json",
            "org060-presence-interaction-decision.schema.json",
        }
        self.assertEqual(set(pr.generate_schemas()), expected)
        workflows = [(REPO_ROOT / ".github" / "workflows" / name).read_text() for name in ("ci.yml", "dbus-tiers.yml")]
        for filename, generated in pr.generate_schemas().items():
            with self.subTest(filename=filename):
                self.assertEqual((REPO_ROOT / "schemas" / filename).read_bytes(), generated)
                schema = json.loads(generated)
                self.assertEqual(schema["$id"], f"https://pantheon.local/schemas/{filename}")
                walk(schema)
                for text in workflows:
                    # T-5001: pull_request is the single path list (no push trigger).
                    self.assertEqual(text.count(f"schemas/{filename}"), 1, filename)

    def test_presence_digest_preimages_are_pinned(self) -> None:
        world = self.world
        projection = world.projection()
        preimage = dict(projection.data, projection_digest=None)
        self.assertEqual(projection.digest, "sha256:" + hashlib.sha256(b.canonical_json_bytes(preimage)).hexdigest())
        sample = {"a": [1, "b"], "c": None}
        self.assertEqual(pr._sha_json(sample), b._sha_json(sample))
        self.assertEqual(pr._digest_record({"x": 1, "d": "y"}, "d"), b._digest_record({"x": 1, "d": "y"}, "d"))
        ctx = world.context
        self.assertEqual(
            pr.presence_auth_context_digest(ctx),
            pr._sha_json({
                "schema": "org060-presence-auth-context/v1", "subject_ref": ctx.subject_ref, "uid": ctx.uid,
                "persona_ref": ctx.persona_ref, "role_refs": list(ctx.role_refs), "instance_ref": ctx.instance_ref,
                "trust_zone": ctx.trust_zone, "registry_revision": ctx.registry_revision,
            }),
        )
        consent = dict(purpose="p", subject_scope="known_subject", consent_ref="consent:1", consented_at=ago(1), expires_at=ago(0), surface_id="surface-a", auth_context_digest=SHA_X)
        self.assertEqual(
            pr.presence_consent_digest(**consent),
            pr._sha_json(dict(consent, schema="org060-presence-consent/v1", stage="presence_recording_interaction")),
        )
        for receipt in (world.receipt, world.chained_receipt()):
            with self.subTest(receipt=receipt.checkpoint_id):
                self.assertEqual(pr.checkpoint_receipt_digest(receipt), receipt.receipt_sha256)
        state = ready_surface()
        self.assertEqual(state.digest, pr._digest_record(state.data, "state_digest"))
        result = render(projection, state)
        self.assertEqual(result.digest, pr._digest_record(result.data, "render_digest"))  # type: ignore[union-attr]

    def test_presence_lifecycle_state_transitions_follow_the_locked_table(self) -> None:
        fresh = new_surface()
        installed = step(fresh, event("install", "verified"))
        self.reason(apply(installed, event("install", "verified")), "lifecycle-transition-invalid")
        configured = step(installed, event("configure", "verified"))
        healthy = step(configured, event("health", "healthy"))
        updated = step(healthy, event("update", "verified", to_revision="rev-9"))
        self.assertEqual(updated.data["lifecycle"]["exact_revision"], "rev-9")  # type: ignore[index]
        self.assertIsNone(updated.data["lifecycle"]["health"])  # type: ignore[index]
        rolled = step(healthy, event("update", "rolled_back"))
        self.assertEqual(rolled.data["lifecycle"]["exact_revision"], "rev-1")  # type: ignore[index]
        self.assertIsNone(rolled.data["lifecycle"]["health"])  # type: ignore[index]
        interrupted = step(healthy, event("update", "interrupted"))
        self.assertEqual(interrupted.data["phase"], "update_interrupted")
        for ev in (event("health", "healthy"), event("configure", "verified"), event("update", "interrupted"), event("remove", "rehearsed"), event("install", "verified")):
            with self.subTest(during_interrupted=ev.leg):
                self.reason(apply(interrupted, ev), "lifecycle-transition-invalid")
        resumed = step(interrupted, event("update", "verified", to_revision="rev-3"))
        self.assertEqual(resumed.data["phase"], "present")
        self.assertEqual(step(interrupted, event("remove", "executed")).data["phase"], "removed")
        rehearsed = step(healthy, event("remove", "rehearsed"))
        self.assertEqual(rehearsed.data["phase"], "present")
        removed = step(rehearsed, event("remove", "executed"))
        for leg, outcome in (("configure", "verified"), ("health", "healthy"), ("update", "verified"), ("remove", "executed")):
            with self.subTest(after_removed=leg):
                self.reason(apply(removed, event(leg, outcome, to_revision="rev-4" if leg == "update" else None)), "lifecycle-transition-invalid")
        reinstalled = step(removed, event("install", "verified"))
        self.assertEqual([reinstalled.data["lifecycle"][leg] is None for leg in pr.LIFECYCLE_LEGS], [False, True, True, True, True])  # type: ignore[index]
        self.assertEqual(reinstalled.data["state_revision"], removed.data["state_revision"] + 1)  # type: ignore[operator]
        self.assertEqual(reinstalled.data["prior_state_digest"], removed.digest)
        self.reason(pr.record_presence_lifecycle(healthy, event("configure", "verified"), expected_state_digest=configured.digest, now=NOW), "stale-surface-state")
        self.reason(apply(healthy, event("update", "verified")), "lifecycle-transition-invalid")
        self.reason(apply(healthy, event("configure", "verified", to_revision="rev-2")), "lifecycle-transition-invalid")
        self.reason(apply(healthy, event("health", "verified")), "invalid-enum")
        self.reason(apply(healthy, event("teleport", "verified")), "invalid-enum")
        self.reason(apply(healthy, event("health", "healthy", observed_at=z(NOW + timedelta(minutes=1)))), "future-timestamp")

    def test_presence_private_b_imports_are_pinned(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text())
        private: dict[str, set[str]] = {"b": set(), "pc": set(), "cn": set()}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in private and node.attr.startswith("_"):
                private[node.value.id].add(node.attr)
            if isinstance(node, ast.ImportFrom):
                self.assertFalse([a.name for a in node.names if a.name.startswith("_") and a.name != "__future__"])
        self.assertEqual(private["b"], PINNED_PRIVATE_B_NAMES)
        self.assertEqual(private["pc"], set())
        self.assertEqual(private["cn"], set())

    def test_presence_lookalike_scan_covers_keys_not_free_text_values(self) -> None:
        world = self.world
        projection = world.projection()
        words = " ".join(pr.PRESENCE_FORBIDDEN_PARTS)
        purpose = " ".join(pr.PRESENCE_FORBIDDEN_PARTS[-14:])
        self.assertLessEqual(len(purpose), pr.SUMMARY_LIMIT)
        state = ready_surface()
        grant_ = grant(world, purpose=purpose)
        req = request(projection, content=f"please remember: {words}", purpose=purpose)
        ok(self, accept(world, projection, state, envelope(req), consent_evidence=[grant_]), pr.PresenceInteractionDecision)
        for part in pr.PRESENCE_FORBIDDEN_PARTS:
            with self.subTest(part=part):
                keyed = copy.deepcopy(req)
                keyed["input"][part] = "value"  # type: ignore[index]
                self.reason(pr.parse_presence_interaction_request(keyed), "identity-authority-lookalike")

    def test_presence_interaction_envelope_freshness_refuses_stale_or_future_reads(self) -> None:
        world = self.world
        projection = world.projection()
        state = ready_surface()
        self.reason(accept(world, projection, state, envelope(request(projection), read_at=ago(61))), "stale-interaction")
        self.reason(accept(world, projection, state, envelope(request(projection), read_at=z(NOW + timedelta(seconds=5)))), "future-timestamp")
        self.reason(accept(world, projection, state, envelope(request(projection), read_at="2026-02-30T00:00:00Z")), "invalid-pattern")
        self.reason(accept(world, projection, state, envelope(request(projection)), freshness=60), "wrong-type")
        ok(self, accept(world, projection, state, envelope(request(projection), read_at=ago(60))), pr.PresenceInteractionDecision)

    # ------------------------------------------------------------- review r1 tests

    def test_presence_recovery_refuses_companion_state_drift(self) -> None:
        world = self.world
        projection = world.projection()
        durable = b.canonical_json_bytes(pr.presence_projection_to_json(projection))

        def recover(companion: pr.PresenceCompanionState) -> object:
            return pr.recover_presence_projection(
                durable, view_path=world.view_path, checkpoint_receipt=world.receipt, checkpoint_root=world.fx.checkpoints,
                navigator=world.navigator, context=world.context, companion=companion, now=NOW,
            )

        for field, drifted in (("availability", "off"), ("privacy", "redacted_egress"), ("recording", "not_recording"), ("action", "acting")):
            with self.subTest(field=field):
                self.reason(recover(world.companion(**{field: drifted})), "recovery-state-drift")
        with self.subTest(field="fallback_channels"):
            self.reason(recover(world.companion(fallback_channels=("text", "voice"))), "fallback-incomplete")
        same = ok(self, recover(world.companion(observed_at=ago(1))), pr.PresenceProjection)
        self.assertEqual(same, projection)
        rebuilt = ok(self, world.build(prior=same, companion=world.companion(availability="off")), pr.PresenceProjection)
        self.assertEqual(rebuilt.revision, projection.revision + 1)  # type: ignore[union-attr]

    def test_presence_waiver_is_bound_to_surface_adapter_and_provisioning(self) -> None:
        def waived(state: pr.PresenceSurfaceState) -> pr.PresenceSurfaceState:
            result = pr.waive_presence_lifecycle_legs(
                state, waiver_id="waiver:1", waived_legs=("configure",), reason="no config channel", review_ref="review:1",
                reviewed_at=ago(60), expires_at=z(NOW + timedelta(days=1)), expected_state_digest=state.digest, now=NOW,
            )
            assert isinstance(result, pr.PresenceSurfaceState), result
            return result

        def attach(state: pr.PresenceSurfaceState, waiver: dict[str, object]) -> pr.PresenceSurfaceState:
            data = presence_json(state)
            data["lifecycle"]["waiver"] = copy.deepcopy(waiver)  # type: ignore[index]
            result = pr.parse_presence_surface_state(resign(data, "state_digest"))
            assert isinstance(result, pr.PresenceSurfaceState), result
            return result

        surface_a = waived(ready_surface("surface-a", skip=("configure",)))
        waiver = dict(surface_a.data["lifecycle"]["waiver"])  # type: ignore[index]
        reviewed = frozenset({str(waiver["waiver_digest"])})
        self.assertEqual(waiver["surface_id"], "surface-a")
        self.assertEqual(waiver["adapter_id"], "adapter:fixture-display")
        self.assertEqual(waiver["provisioning_anchor"], surface_a.data["lifecycle"]["provisioning_anchor"])  # type: ignore[index]
        ok(self, readiness(surface_a, reviewed), pr.PresenceReadiness)
        surface_b = ready_surface("surface-b", skip=("configure",))
        self.reason(readiness(attach(surface_b, waiver), reviewed), "lifecycle-waiver-invalid")
        other_adapter = ready_surface("surface-a", skip=("configure",), adapter_id="adapter:other")
        self.reason(readiness(attach(other_adapter, waiver), reviewed), "lifecycle-waiver-invalid")
        removed = step(surface_a, event("remove", "executed"))
        again = step(removed, event("install", "verified"))
        for leg, outcome, revision in FULL_LEGS:
            if leg in ("install", "configure"):
                continue
            again = step(again, event(leg, outcome, to_revision=revision))
        self.assertNotEqual(again.data["lifecycle"]["provisioning_anchor"], waiver["provisioning_anchor"])  # type: ignore[index]
        self.reason(readiness(attach(again, waiver), reviewed), "lifecycle-waiver-invalid")
        fresh_waiver = waived(again)
        ok(self, readiness(fresh_waiver, frozenset({fresh_waiver.data["lifecycle"]["waiver"]["waiver_digest"]})), pr.PresenceReadiness)  # type: ignore[index]
        self.assertNotEqual(fresh_waiver.data["lifecycle"]["waiver"]["waiver_digest"], waiver["waiver_digest"])  # type: ignore[index]

    def test_presence_public_apis_never_raise_on_hostile_input(self) -> None:
        world = self.world
        rng = random.Random(3227)
        projection = world.projection()
        state = ready_surface()
        good_ready = ready(state)

        class RaisingMapping(Mapping):  # type: ignore[type-arg]
            def __getitem__(self, key: object) -> object:
                raise RuntimeError("getitem")

            def __iter__(self):  # type: ignore[no-untyped-def]
                raise RuntimeError("iter")

            def __len__(self) -> int:
                raise RuntimeError("len")

        class RaisingIterDict(dict):  # type: ignore[type-arg]
            def __iter__(self):  # type: ignore[no-untyped-def]
                raise RuntimeError("iter")

            def items(self):  # type: ignore[no-untyped-def]
                raise RuntimeError("items")

        class RaisingFlagAdapter:
            @property
            def fixture_only(self) -> bool:
                raise RuntimeError("flag")

        class ReturningAdapter(pr.FixtureBridgePresenceAdapter):
            def __init__(self, value: object) -> None:
                super().__init__(clock=ago(1))
                self.value = value

            def render(self, projection_json, *, surface_id, indicators):  # type: ignore[no-untyped-def,override]
                return self.value

        class RaisingAttrs:
            def __getattr__(self, name: str) -> object:
                raise RuntimeError(name)

        lone = "\ud800"
        hostile_values: list[object] = [
            lone, f"x{lone}y", {"k": lone}, {lone: 1}, [lone], b"\xff\xfe", b"{", "", "null", "[]", 0, -1, True, 1.5,
            float("nan"), 2**200, None, object(), [[]], [{}], {"a": {}}, set(), frozenset(), RaisingMapping(),
            RaisingIterDict(a=1), RaisingAttrs(), ("text", ["x"]), [["control"]], "x" * 20000,
        ]
        hostile_adapters: list[object] = [
            RaisingFlagAdapter(), RaisingAttrs(), ReturningAdapter(RaisingMapping()), ReturningAdapter(RaisingIterDict(a=1)),
            ReturningAdapter(None), ReturningAdapter(lone), ReturningAdapter({"surface_id": lone}), ReturningAdapter([1]),
            ReturningAdapter({"displayed": RaisingMapping()}),
        ]

        def grant_like(**changes: object) -> b.ConsentGrant:
            return dataclasses.replace(grant(world), **changes)

        valid_env = envelope(request(projection))
        apis: list[tuple[str, object, dict[str, object], tuple[type, ...]]] = [
            ("parse_presence_projection", pr.parse_presence_projection, {"raw": pr.presence_projection_to_json(projection)}, (pr.PresenceProjection,)),
            ("parse_presence_surface_state", pr.parse_presence_surface_state, {"raw": presence_json(state)}, (pr.PresenceSurfaceState,)),
            ("parse_presence_render_result", pr.parse_presence_render_result, {"raw": {}}, (pr.PresenceRenderResult,)),
            ("parse_presence_interaction_request", pr.parse_presence_interaction_request, {"raw": request(projection)}, (pr.PresenceInteractionRequest,)),
            ("parse_presence_interaction_decision", pr.parse_presence_interaction_decision, {"raw": {}}, (pr.PresenceInteractionDecision,)),
            ("build_presence_projection", pr.build_presence_projection, {
                "view_path": world.view_path, "checkpoint_receipt": world.receipt, "checkpoint_root": world.fx.checkpoints,
                "navigator": world.navigator, "context": world.context, "companion": world.companion(), "prior": None, "now": NOW,
            }, (pr.PresenceProjection,)),
            ("render_presence", pr.render_presence, {
                "current": projection, "surface": state, "readiness": good_ready, "adapter": adapter(), "now": NOW, "freshness": FRESH,
            }, (pr.PresenceRenderResult,)),
            ("accept_presence_interaction", pr.accept_presence_interaction, {
                "interaction": valid_env, "current": projection, "surface": state, "readiness": good_ready, "context": world.context,
                "consent_evidence": [grant(world)], "replay_cache": b.ConsentReplayCache(), "now": NOW, "freshness": FRESH,
            }, (pr.PresenceInteractionDecision,)),
            ("evaluate_presence_readiness", pr.evaluate_presence_readiness, {
                "surface": state, "reviewed_waiver_digests": frozenset(), "now": NOW, "health_freshness": b.FreshnessWindow(300),
            }, (pr.PresenceReadiness,)),
            ("record_presence_lifecycle", pr.record_presence_lifecycle, {
                "surface": state, "event": event("health", "healthy"), "expected_state_digest": state.digest, "now": NOW,
            }, (pr.PresenceSurfaceState,)),
            ("recover_presence_projection", pr.recover_presence_projection, {
                "durable_projection": b.canonical_json_bytes(pr.presence_projection_to_json(projection)), "view_path": world.view_path,
                "checkpoint_receipt": world.receipt, "checkpoint_root": world.fx.checkpoints, "navigator": world.navigator,
                "context": world.context, "companion": world.companion(), "now": NOW,
            }, (pr.PresenceProjection,)),
            ("make_presence_surface_state", pr.make_presence_surface_state, {
                "surface_id": "surface-x", "surface_kind": "display", "connectivity": "live", "connectivity_observed_at": ago(1),
                "interaction_modes": ("text",), "adapter_id": "adapter:x", "adapter_revision": "r", "exact_revision": "r",
                "rollback_ref": "r", "now": NOW,
            }, (pr.PresenceSurfaceState,)),
            ("observe_presence_connectivity", pr.observe_presence_connectivity, {
                "surface": state, "connectivity": "degraded", "observed_at": ago(1), "expected_state_digest": state.digest, "now": NOW,
            }, (pr.PresenceSurfaceState,)),
            ("waive_presence_lifecycle_legs", pr.waive_presence_lifecycle_legs, {
                "surface": state, "waiver_id": "waiver:1", "waived_legs": ("configure",), "reason": "r", "review_ref": "review:1",
                "reviewed_at": ago(60), "expires_at": z(NOW + timedelta(days=1)), "expected_state_digest": state.digest, "now": NOW,
            }, (pr.PresenceSurfaceState,)),
            ("make_presence_interaction_request", pr.make_presence_interaction_request, {
                "request_id": "req:1", "surface_id": "surface-display", "purpose": "p", "mode": "text", "content": "c",
                "observed_projection_revision": 1, "observed_projection_digest": projection.digest, "consent_ref": "consent:1",
            }, (pr.PresenceInteractionRequest,)),
            ("presence_auth_context_digest", pr.presence_auth_context_digest, {"context": world.context}, (str,)),
            ("presence_consent_digest", pr.presence_consent_digest, {
                "purpose": "p", "subject_scope": "known_subject", "consent_ref": "consent:1", "consented_at": ago(1),
                "expires_at": ago(0), "surface_id": "surface-a", "auth_context_digest": SHA_X,
            }, (str,)),
            ("checkpoint_receipt_digest", pr.checkpoint_receipt_digest, {"receipt": world.receipt}, (str,)),
            ("presence_projection_to_json", pr.presence_projection_to_json, {"record": projection}, (dict,)),
            ("presence_surface_state_to_json", pr.presence_surface_state_to_json, {"record": state}, (dict,)),
            ("presence_render_result_to_json", pr.presence_render_result_to_json, {"record": None}, (dict,)),
            ("presence_interaction_request_to_json", pr.presence_interaction_request_to_json, {"record": None}, (dict,)),
            ("presence_interaction_decision_to_json", pr.presence_interaction_decision_to_json, {"record": None}, (dict,)),
        ]
        public = {name for name, obj in vars(pr).items() if callable(obj) and not name.startswith("_") and getattr(obj, "__module__", "") == pr.__name__ and inspect.isfunction(obj)}
        exempt = {"generate_presence_projection_schema", "generate_presence_surface_state_schema", "generate_presence_render_result_schema",
                  "generate_presence_interaction_request_schema", "generate_presence_interaction_decision_schema", "generate_schemas"}
        self.assertEqual(public - exempt, {name for name, *_ in apis}, "every public API is fuzzed (schema generators take no input)")

        def check(name: str, func: object, kwargs: dict[str, object], success: tuple[type, ...]) -> None:
            try:
                if name.startswith("parse_") or name.endswith("_to_json") or name in ("presence_auth_context_digest", "checkpoint_receipt_digest"):
                    result = func(*kwargs.values())  # type: ignore[operator]
                elif name in ("render_presence",):
                    rest = dict(kwargs)
                    result = func(rest.pop("current"), **rest)  # type: ignore[operator]
                elif name in ("accept_presence_interaction",):
                    rest = dict(kwargs)
                    result = func(rest.pop("interaction"), **rest)  # type: ignore[operator]
                elif name in ("recover_presence_projection",):
                    rest = dict(kwargs)
                    result = func(rest.pop("durable_projection"), **rest)  # type: ignore[operator]
                elif name in ("evaluate_presence_readiness", "record_presence_lifecycle", "observe_presence_connectivity", "waive_presence_lifecycle_legs"):
                    rest = dict(kwargs)
                    surface = rest.pop("surface")
                    if name == "record_presence_lifecycle":
                        result = func(surface, rest.pop("event"), **rest)  # type: ignore[operator]
                    else:
                        result = func(surface, **rest)  # type: ignore[operator]
                else:
                    result = func(**kwargs)  # type: ignore[operator]
            except Exception as exc:  # the property under test: nothing escapes
                self.fail(f"{name} raised {exc.__class__.__name__}: {exc} for {kwargs!r}"[:400])
            self.assertTrue(isinstance(result, success) or type(result) is pr.PresenceRefusal, (name, result))

        for name, func, kwargs, success in apis:
            check(name, func, kwargs, success)  # baseline
            for _ in range(40):
                mutated = dict(kwargs)
                key = rng.choice(list(mutated))
                mutated[key] = rng.choice(hostile_values)
                check(name, func, mutated, success)
        for bad in hostile_adapters:
            check("render_presence", pr.render_presence, {
                "current": projection, "surface": state, "readiness": good_ready, "adapter": bad, "now": NOW, "freshness": FRESH,
            }, (pr.PresenceRenderResult,))
        for legs in ([["x"]], [{}], [lone], [None], RaisingMapping(), [set()]):
            check("waive_presence_lifecycle_legs", pr.waive_presence_lifecycle_legs, {
                "surface": state, "waiver_id": "waiver:1", "waived_legs": legs, "reason": "r", "review_ref": "review:1",
                "reviewed_at": ago(60), "expires_at": z(NOW + timedelta(days=1)), "expected_state_digest": state.digest, "now": NOW,
            }, (pr.PresenceSurfaceState,))
        for field in ("purpose", "subject_scope", "consent_ref", "consented_at", "expires_at", "consent_digest", "stage", "revoked"):
            for value in (lone, ["x"], None):
                check("accept_presence_interaction", pr.accept_presence_interaction, {
                    "interaction": valid_env, "current": projection, "surface": state, "readiness": good_ready, "context": world.context,
                    "consent_evidence": [grant_like(**{field: value})], "replay_cache": b.ConsentReplayCache(), "now": NOW, "freshness": FRESH,
                }, (pr.PresenceInteractionDecision,))
        for field in ("subject_ref", "persona_ref", "trust_zone", "uid", "role_refs"):
            for value in (lone, (lone,), ["x"]):
                check("presence_auth_context_digest", pr.presence_auth_context_digest, {"context": dataclasses.replace(world.context, **{field: value})}, (str,))
        for parser in ("parse_presence_projection", "parse_presence_interaction_request"):
            func = getattr(pr, parser)
            for raw in (lone, json.dumps({"x": lone}), '{"a": "\\ud800"}'):
                check(parser, func, {"raw": raw}, (pr.PresenceProjection, pr.PresenceInteractionRequest))
        self.reason(pr.parse_presence_interaction_request(lone), "malformed-input")
        self.reason(accept(world, projection, state, lone), "malformed-input")
        self.reason(render(projection, state, RaisingFlagAdapter()), "adapter-not-fixture")
        self.reason(render(projection, state, ReturningAdapter(RaisingIterDict(a=1))), "malformed-input")
        self.reason(pr.waive_presence_lifecycle_legs(
            state, waiver_id="waiver:1", waived_legs=[["x"]], reason="r", review_ref="review:1", reviewed_at=ago(60),
            expires_at=z(NOW + timedelta(days=1)), expected_state_digest=state.digest, now=NOW,
        ), "lifecycle-waiver-invalid")

    def test_presence_free_text_control_codepoints_refuse_in_schema_and_runtime(self) -> None:
        world = self.world
        projection = world.projection()
        request_schema = json.loads((REPO_ROOT / "schemas" / "org060-presence-interaction-request.schema.json").read_bytes())
        surface_schema = json.loads((REPO_ROOT / "schemas" / "org060-presence-surface-state.schema.json").read_bytes())
        partial = ready_surface(skip=("configure",))
        waived = pr.waive_presence_lifecycle_legs(
            partial, waiver_id="waiver:1", waived_legs=("configure",), reason="plain reason", review_ref="review:1",
            reviewed_at=ago(60), expires_at=z(NOW + timedelta(days=1)), expected_state_digest=partial.digest, now=NOW,
        )
        assert isinstance(waived, pr.PresenceSurfaceState)
        mismatched = [cp for cp in range(0x10000) if re_fullmatch_fails(pr.FREE_TEXT_PATTERN, chr(cp)) != (cp in b.CONTROL_CODEPOINTS)]
        self.assertEqual(mismatched, [], "schema pattern excludes exactly b.CONTROL_CODEPOINTS")
        for char in ("\x00", "\u202e", "\u200b", "\x07", "\ufeff", "\x9f"):
            for where in ("purpose", "content"):
                with self.subTest(char=hex(ord(char)), where=where):
                    req = request(projection)
                    if where == "purpose":
                        req["purpose"] = f"a{char}b"
                    else:
                        req["input"]["content"] = f"a{char}b"  # type: ignore[index]
                    resign(req, "request_digest")
                    schema_result = b._validate_json_schema_subset(req, request_schema)
                    self.assertIsNotNone(schema_result)
                    self.assertEqual(schema_result.reason, "invalid-pattern")  # type: ignore[union-attr]
                    self.reason(pr.parse_presence_interaction_request(req), "hostile-control")
            with self.subTest(char=hex(ord(char)), where="waiver.reason"):
                data = presence_json(waived)
                data["lifecycle"]["waiver"]["reason"] = f"a{char}b"  # type: ignore[index]
                resign(data["lifecycle"]["waiver"], "waiver_digest")  # type: ignore[arg-type]
                resign(data, "state_digest")
                schema_result = b._validate_json_schema_subset(data, surface_schema)
                self.assertEqual(getattr(schema_result, "reason", None), "invalid-pattern")
                self.reason(pr.parse_presence_surface_state(data), "hostile-control")
        benign = request(projection, content="caf\u00e9 \u4e2d\u6587 emoji \U0001f600", purpose="plain")
        self.assertIsNone(b._validate_json_schema_subset(benign, request_schema))

    def test_presence_projection_refuses_mismatched_organism_id(self) -> None:
        world = self.world
        receipt = world.receipt
        original = json.loads(Path(receipt.path).read_text())
        crafted = dict(original)
        crafted["organism_id"] = "elsewhere"
        crafted.pop("payload_sha256")
        payload = hashlib.sha256((json.dumps(crafted, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
        crafted["payload_sha256"] = payload
        self.assertEqual(crafted["view_sha256"], world.view.view_sha256)
        path = Path(receipt.path).with_name("crafted-organism.json")
        path.write_text(json.dumps(crafted, sort_keys=True, separators=(",", ":")) + "\n")
        path.chmod(0o600)
        loaded = pc.load_checkpoint_envelope(path, checkpoint_root=world.fx.checkpoints)
        self.assertEqual(loaded.organism_id, "elsewhere")
        forged = dataclasses.replace(receipt, path=str(path), payload_sha256=payload)
        forged = dataclasses.replace(forged, receipt_sha256=pr.checkpoint_receipt_digest(forged))
        result = world.build(receipt=forged)
        self.reason(result, "continuity-binding-mismatch")
        self.assertIn("organism_id", result.detail)  # type: ignore[union-attr]

    def test_presence_interaction_refuses_disconnected_surface(self) -> None:
        world = self.world
        projection = world.projection()
        disconnected = observe(ready_surface(), "disconnected")
        self.reason(accept(world, projection, disconnected, envelope(request(projection))), "surface-disconnected")
        for connectivity in ("degraded", "provisional"):
            with self.subTest(connectivity=connectivity):
                ok(self, accept(world, projection, observe(ready_surface(), connectivity), envelope(request(projection))), pr.PresenceInteractionDecision)

    def test_presence_consent_subject_scope_is_the_shared_closed_set(self) -> None:
        world = self.world
        projection = world.projection()
        state = ready_surface()
        for scope in ("everyone", "", "KNOWN_SUBJECT", "household"):
            with self.subTest(scope=scope):
                self.reason(accept(world, projection, state, envelope(request(projection)), consent_evidence=[grant(world, subject_scope=scope)]), "invalid-enum")
        ok(self, accept(world, projection, state, envelope(request(projection)), consent_evidence=[grant(world, subject_scope="known_guest")]), pr.PresenceInteractionDecision)
        source = inspect.getsource(pr._evaluate_presence_consent)
        self.assertIn("b.SUBJECT_SCOPE", source)

    def test_presence_replay_key_cannot_collide_with_b_consent_refs(self) -> None:
        world = self.world
        projection = world.projection()
        state = ready_surface()
        self.assertIsNone(b.ID_RE.fullmatch(pr._presence_replay_key("consent:1")), "the presence key is outside b.ID_RE")
        cache = b.ConsentReplayCache()
        b_ref = "presence_recording_interaction:consent:1"
        self.assertIsNotNone(b.ID_RE.fullmatch(b_ref))
        self.assertTrue(cache.mark(b_ref))
        ok(self, accept(world, projection, state, envelope(request(projection)), replay_cache=cache), pr.PresenceInteractionDecision)
        self.assertTrue(cache.mark("presence_recording_interaction:consent:2"))
        self.reason(accept(world, projection, state, envelope(request(projection, request_id="req:9")), replay_cache=cache), "replayed-consent")

    def test_presence_validator_capability_gap_min_unique_items_is_pinned(self) -> None:
        """The ONE validator-capability (non-semantic) gap: minItems/uniqueItems."""
        self.assertIn("VALIDATOR-CAPABILITY", pr.SCHEMA_RUNTIME_GAPS["validator-capability-min-unique-items"])
        surface_schema = json.loads((REPO_ROOT / "schemas" / "org060-presence-surface-state.schema.json").read_bytes())
        modes = surface_schema["properties"]["interaction_modes"]
        self.assertEqual((modes["minItems"], modes["uniqueItems"]), (1, True))
        for value in ([], ["text", "text"]):
            with self.subTest(value=value):
                data = presence_json(ready_surface())
                data["interaction_modes"] = value
                resign(data, "state_digest")
                self.assertIsNone(b._validate_json_schema_subset(data, surface_schema), "B subset validator cannot evaluate it")
                self.reason(pr.parse_presence_surface_state(data), "invalid-enum")
        semantic = set(pr.SCHEMA_RUNTIME_GAPS) - {"validator-capability-min-unique-items"}
        self.assertTrue(all("VALIDATOR-CAPABILITY" not in pr.SCHEMA_RUNTIME_GAPS[key] for key in semantic))


PINNED_PRIVATE_B_NAMES = frozenset({"_validate_json_schema_subset", "_reject_hostile_control"})


def presence_json(state: pr.PresenceSurfaceState) -> dict[str, object]:
    return pr.presence_surface_state_to_json(state)


def re_fullmatch_fails(pattern: str, text: str) -> bool:
    import re

    return re.fullmatch(pattern, text) is None


def _jsonable(value: object) -> bool:
    try:
        b.canonical_json_bytes(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return True


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(current)
    return None


def _in_main(tree: ast.Module, node: ast.AST) -> bool:
    for top in tree.body:
        if isinstance(top, ast.If) and "__main__" in ast.dump(top.test):
            if any(child is node for child in ast.walk(top)):
                return True
    return False


if __name__ == "__main__":
    unittest.main()
