#!/usr/bin/env python3
"""Adversarial conformance tests for the deterministic ORG-060 Child-D simulator."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import socket
import subprocess
import tempfile
import time
import unittest
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools import capability_navigator_contract as cn
from tools import capability_navigator_engine as engine
from tools import org060_device_presence_simulator as sim
from tools import org060_presence_contract as c
from tools import org060_sensory_actuation_contract as b
from tools import test_capability_navigator_engine as engine_test
from tools import test_org060_presence_contract as presence_test
from tools import test_org060_sensory_actuation_contract as b_test


NOW = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
NOW_Z = "2026-10-01T00:00:00Z"
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
MODULE_PATH = Path(__file__).with_name("org060_device_presence_simulator.py")


def binding(adapter_id: str = "component-fixed-microphone") -> sim.DeviceBinding:
    return sim.DeviceBinding(
        adapter_id=adapter_id,
        device_id=f"device:{adapter_id}",
        device_revision="device-revision-1",
        calibration_revision="calibration-revision-1",
        calibration_device_id=f"device:{adapter_id}",
        location_ref="location:room-a",
        calibration_valid_through_tick=100,
    )


def read_plan(bind: sim.DeviceBinding, **changes: object) -> sim.SensorReadPlan:
    values: dict[str, object] = {
        "event_id": "read-1",
        "logical_tick": 10,
        "expected_device_id": bind.device_id,
        "expected_device_revision": bind.device_revision,
        "expected_calibration_revision": bind.calibration_revision,
        "expected_location_ref": bind.location_ref,
        "purpose": "companionship",
        "subject_scope": "known_subject",
    }
    values.update(changes)
    return sim.SensorReadPlan(**values)  # type: ignore[arg-type]


def sensor_projection(
    bind: sim.DeviceBinding,
    capability_id: str,
    contract: str,
    schema: str,
) -> cn.NavigatorProjection:
    provider = dataclasses.replace(
        b_test._provider(capability_id),
        provider_component_id=bind.adapter_id,
        contract=contract,
        schema=schema,
    )
    return b_test._projection(provider)


def owner_actuation_objects() -> tuple[
    b.ActuationPlan,
    b.AuthenticatedActuationContext,
    cn.NavigatorProjection,
]:
    record = engine_test._visible_record(
        "component-light",
        capability="light-control",
        contract="org060-actuator",
        schema="org060-light-v1",
    )
    projection = engine_test._projection(record)
    context = b_test._context()
    target = b.make_target_state(
        target_ref="target:light",
        provider=projection.capabilities[0],
        projection=projection,
        observed_at=NOW_Z,
        state_digest=SHA_B,
        current_preconditions=({"state": "off"},),
    )
    plan = b.plan_actuation(
        b_test._request(),
        projection=projection,
        target_states=(target,),
        context=context,
        now=NOW,
    )
    assert isinstance(plan, b.ActuationPlan), plan
    return plan, context, projection


def actuator_authority(
    bind: sim.DeviceBinding,
    plan: b.ActuationPlan | None = None,
    context: b.AuthenticatedActuationContext | None = None,
    projection: cn.NavigatorProjection | None = None,
) -> sim._ActuatorAuthorizationAnchor:
    default_plan, default_context, default_projection = owner_actuation_objects()
    plan = default_plan if plan is None else plan
    context = default_context if context is None else context
    projection = default_projection if projection is None else projection
    provider = projection.capabilities[0]
    authority = sim._bind_server_actuator_authorization(
        adapter_id=bind.adapter_id,
        approved_plan=plan,
        context=context,
        provider=provider,
        projection=projection,
    )
    assert isinstance(authority, sim._ActuatorAuthorizationAnchor), authority
    return authority


def actuator_adapter(
    bind: sim.DeviceBinding,
    *,
    plan: b.ActuationPlan | None = None,
    context: b.AuthenticatedActuationContext | None = None,
    projection: cn.NavigatorProjection | None = None,
    outcome_script: bytes | None = None,
    **state_changes: object,
) -> sim.ReversibleIoTActuatorAdapter:
    plan = owner_actuation_objects()[0] if plan is None else plan
    authority = actuator_authority(bind, plan, context, projection)
    target_state_digest = state_changes.pop(
        "target_state_digest", str(plan.data["target_state_digest"])
    )
    current_preconditions = state_changes.pop(
        "current_preconditions", plan.data["preconditions"]
    )
    evaluated_at = state_changes.pop("evaluated_at", NOW_Z)
    state = sim.actuator_state_bytes(
        authority,
        target_state_digest=target_state_digest,  # type: ignore[arg-type]
        current_preconditions=current_preconditions,  # type: ignore[arg-type]
        evaluated_at=evaluated_at,  # type: ignore[arg-type]
        **state_changes,
    )
    return sim.ReversibleIoTActuatorAdapter(
        bind,
        sim.outcome_script_bytes() if outcome_script is None else outcome_script,
        authority,
        state,
    )


def drone_adapter(
    bind: sim.DeviceBinding,
    *,
    outcome_script: bytes | None = None,
) -> sim.MobileDroneAdapter:
    plan = owner_actuation_objects()[0]
    authority = actuator_authority(bind, plan)
    return sim.MobileDroneAdapter(
        bind,
        NOW_Z,
        sim.outcome_script_bytes() if outcome_script is None else outcome_script,
        authority,
        sim.actuator_state_bytes(
            authority,
            target_state_digest=str(plan.data["target_state_digest"]),
            current_preconditions=plan.data["preconditions"],  # type: ignore[arg-type]
            evaluated_at=NOW_Z,
        ),
    )


def recovery_anchor(
    genesis: sim.DeterministicKernel,
    checkpoint: sim.KernelTransition,
) -> sim._KernelRecoveryAnchor:
    anchor = sim._bind_server_recovery_anchor(
        trusted_genesis=genesis,
        trusted_checkpoint=checkpoint,
    )
    assert isinstance(anchor, sim._KernelRecoveryAnchor), anchor
    return anchor


def recover_checkpoint(
    genesis: sim.DeterministicKernel,
    checkpoint: sim.KernelTransition,
    durable_state: bytes | None = None,
) -> sim.DeterministicKernel:
    return sim.DeterministicKernel.recover(
        checkpoint.durable_state if durable_state is None else durable_state,
        anchor=recovery_anchor(genesis, checkpoint),
    )


def resigned_plan(plan: b.ActuationPlan, **changes: object) -> b.ActuationPlan:
    data = dict(plan.data)
    data.update(changes)
    data["plan_digest"] = b._digest_record(data, "plan_digest")
    parsed = b.parse_actuation_plan(data)
    assert isinstance(parsed, b.ActuationPlan), parsed
    return parsed


def resigned_receipt(receipt: b.ActuationReceipt, **changes: object) -> dict[str, object]:
    data = dict(receipt.data)
    data.update(changes)
    data["receipt_digest"] = b._digest_record(data, "receipt_digest")
    parsed = b.parse_actuation_receipt(data)
    assert isinstance(parsed, b.ActuationReceipt), parsed
    return dict(parsed.data)


def lifecycle_event(
    adapter_id: str,
    tick: int,
    leg: str,
    outcome: str,
    *,
    suffix: str = "",
    target_revision: str | None = None,
    crash_point: str | None = None,
) -> sim.SimulationEvent:
    return sim.SimulationEvent(
        event_id=f"{tick:04d}-{adapter_id}-{leg}-{outcome}{suffix}",
        logical_tick=tick,
        adapter_id=adapter_id,
        kind="lifecycle",
        payload={
            "leg": leg,
            "outcome": outcome,
            "evidence_ref": f"evidence:{adapter_id}:{leg}:{outcome}{suffix}",
            "target_revision": target_revision,
        },
        crash_point=crash_point,
    )


def five_leg_events(adapter_id: str, start: int = 1) -> list[sim.SimulationEvent]:
    return [
        lifecycle_event(adapter_id, start, "install", "verified"),
        lifecycle_event(adapter_id, start + 1, "configure", "verified"),
        lifecycle_event(adapter_id, start + 2, "health", "healthy"),
        lifecycle_event(
            adapter_id,
            start + 3,
            "update",
            "verified",
            target_revision="fixture-revision-2",
        ),
        lifecycle_event(
            adapter_id, start + 4, "health", "healthy", suffix="-post-update"
        ),
        lifecycle_event(adapter_id, start + 5, "remove", "rehearsed"),
    ]


def ok(test: unittest.TestCase, value: object, expected: type) -> object:
    test.assertNotIsInstance(value, (sim.SimulatorRefusal, b.TypedRefusal), value)
    test.assertIsInstance(value, expected)
    return value


class Org060DevicePresenceSimulatorTests(unittest.TestCase):
    maxDiff = None

    def assert_sim_refusal(self, value: object, reason: str) -> sim.SimulatorRefusal:
        self.assertIsInstance(value, sim.SimulatorRefusal, value)
        self.assertEqual(value.reason, reason)  # type: ignore[union-attr]
        return value  # type: ignore[return-value]

    def test_simulator_clock_and_replay_are_byte_deterministic(self) -> None:
        bind = binding()
        first = sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=0)
        second = sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=0)
        events = five_leg_events(bind.adapter_id)
        forward = ok(self, first.apply(events), sim.KernelTransition)
        reverse = ok(self, second.apply(tuple(reversed(events))), sim.KernelTransition)
        self.assertEqual(forward.durable_state, reverse.durable_state)  # type: ignore[union-attr]
        self.assertEqual(forward.transcript, reverse.transcript)  # type: ignore[union-attr]

        crashed_events = list(events)
        crashed_events[2] = dataclasses.replace(crashed_events[2], crash_point="after")
        crash = ok(self, first.apply(crashed_events), sim.KernelTransition)
        self.assertEqual(crash.crashed_at, f"after:{events[2].event_id}")  # type: ignore[union-attr]
        recovered = recover_checkpoint(first, crash)  # type: ignore[arg-type]
        replayed = ok(self, recovered.apply(events), sim.KernelTransition)
        self.assertEqual(replayed.durable_state, forward.durable_state)  # type: ignore[union-attr]
        self.assertEqual(replayed.transcript, forward.transcript)  # type: ignore[union-attr]
        self.assertEqual(recovered.durable_state, crash.durable_state)  # type: ignore[union-attr]
        self.assert_sim_refusal(
            first.apply((dataclasses.replace(events[0], logical_tick=-1),)),
            "event-schema-invalid",
        )

        future_leg = json.loads(forward.durable_state)  # type: ignore[union-attr]
        future_leg["adapters"][bind.adapter_id]["lifecycle"]["legs"]["remove"][
            "logical_tick"
        ] = future_leg["logical_tick"] + 1
        future_leg_bytes = sim.canonical_bytes(future_leg)
        for entrypoint, result in (
            ("decode", sim._decode_kernel_state(future_leg_bytes)),
            ("apply", sim.DeterministicKernel(future_leg_bytes).apply(())),
        ):
            with self.subTest(future_lifecycle_entrypoint=entrypoint):
                self.assert_sim_refusal(result, "kernel-state-schema-invalid")

        future_transcript = json.loads(forward.durable_state)  # type: ignore[union-attr]
        future_row = future_transcript["transcript"][-1]
        future_row["logical_tick"] = future_transcript["logical_tick"] + 1
        future_row["receipt_digest"] = sim._sha(
            {
                key: future_row[key]
                for key in (
                    "event_id",
                    "logical_tick",
                    "adapter_id",
                    "kind",
                    "payload",
                    "evidence_class",
                )
            }
        )
        future_transcript_bytes = sim.canonical_bytes(future_transcript)
        for entrypoint, result in (
            ("decode", sim._decode_kernel_state(future_transcript_bytes)),
            ("apply", sim.DeterministicKernel(future_transcript_bytes).apply(())),
        ):
            with self.subTest(future_transcript_entrypoint=entrypoint):
                self.assert_sim_refusal(result, "kernel-state-schema-invalid")

        # A self-consistent checkpoint is not necessarily an authorized one.
        # Rewriting event metadata while retaining the payload-only digest is
        # the pre-fix false accept this regression must close.
        metadata_rewrite = json.loads(forward.durable_state)  # type: ignore[union-attr]
        metadata_rewrite["transcript"][0]["event_id"] = "forged-event-id"
        metadata_rewrite["applied_event_ids"][0] = "forged-event-id"
        self.assertIsInstance(
            sim._decode_kernel_state(sim.canonical_bytes(metadata_rewrite)),
            sim.SimulatorRefusal,
        )
        with self.assertRaises(ValueError):
            recover_checkpoint(first, forward, sim.canonical_bytes(metadata_rewrite))  # type: ignore[arg-type]

        state_rewrite = json.loads(forward.durable_state)  # type: ignore[union-attr]
        state_rewrite["adapters"][bind.adapter_id]["availability"]["provider"] = False
        with self.assertRaises(ValueError):
            recover_checkpoint(first, forward, sim.canonical_bytes(state_rewrite))  # type: ignore[arg-type]
        forged_state_transition = first.apply(events)
        assert isinstance(forged_state_transition, sim.KernelTransition)
        object.__setattr__(
            forged_state_transition,
            "durable_state",
            sim.canonical_bytes(state_rewrite),
        )
        self.assertIsInstance(
            sim._bind_server_recovery_anchor(
                trusted_genesis=first,
                trusted_checkpoint=forged_state_transition,
            ),
            sim.SimulatorRefusal,
        )

        for label, mutate in (
            ("logical-tick", lambda state: state.update(logical_tick=999)),
            (
                "lifecycle-evidence",
                lambda state: state["adapters"][bind.adapter_id]["lifecycle"]["legs"][
                    "update"
                ].update(evidence_ref="evidence:forged"),
            ),
            (
                "lifecycle-outcome",
                lambda state: state["adapters"][bind.adapter_id]["lifecycle"]["legs"][
                    "update"
                ].update(outcome="rolled_back"),
            ),
            (
                "lifecycle-revision",
                lambda state: state["adapters"][bind.adapter_id]["lifecycle"].update(
                    revision="fixture-revision-forged"
                ),
            ),
            (
                "transcript-payload",
                lambda state: state["transcript"][0]["payload"].update(
                    evidence_ref="evidence:forged"
                ),
            ),
            (
                "inner-digest",
                lambda state: state["transcript"][0].update(receipt_digest=SHA_A),
            ),
        ):
            hostile = json.loads(forward.durable_state)  # type: ignore[union-attr]
            mutate(hostile)
            with self.subTest(checkpoint_drift=label), self.assertRaises(ValueError):
                recover_checkpoint(first, forward, sim.canonical_bytes(hostile))  # type: ignore[arg-type]

        coordinated = json.loads(forward.durable_state)  # type: ignore[union-attr]
        coordinated["transcript"][0]["payload"]["evidence_ref"] = "evidence:coordinated"
        coordinated["adapters"][bind.adapter_id]["lifecycle"]["legs"]["install"][
            "evidence_ref"
        ] = "evidence:coordinated"
        coordinated["transcript"][0]["receipt_digest"] = sim._sha(
            {
                key: coordinated["transcript"][0][key]
                for key in (
                    "event_id",
                    "logical_tick",
                    "adapter_id",
                    "kind",
                    "payload",
                    "evidence_class",
                )
            }
        )
        with self.assertRaises(ValueError):
            recover_checkpoint(first, forward, sim.canonical_bytes(coordinated))  # type: ignore[arg-type]

        coordinated_receipts = list(forward.receipts)  # type: ignore[union-attr]
        coordinated_receipts[0] = dataclasses.replace(
            coordinated_receipts[0],
            payload_digest=coordinated["transcript"][0]["receipt_digest"],
        )
        try:
            coordinated_transition = dataclasses.replace(
                forward,
                durable_state=sim.canonical_bytes(coordinated),  # type: ignore[arg-type]
                transcript=sim.canonical_bytes(coordinated["transcript"]),
                receipts=tuple(coordinated_receipts),
            )
        except TypeError:
            coordinated_transition = None
        if coordinated_transition is not None:
            self.assertIsInstance(
                sim._bind_server_recovery_anchor(
                    trusted_genesis=first,
                    trusted_checkpoint=coordinated_transition,
                ),
                sim.SimulatorRefusal,
            )

        with self.assertRaises(TypeError):
            sim.KernelTransition(
                forward.durable_state,  # type: ignore[union-attr]
                forward.transcript,  # type: ignore[union-attr]
                forward.receipts,  # type: ignore[union-attr]
                forward.crashed_at,  # type: ignore[union-attr]
            )
        with self.assertRaises(TypeError):
            dataclasses.replace(forward, crashed_at="after:forged")  # type: ignore[arg-type]

        for forged_transition in (
            object.__new__(sim.KernelTransition),
            dataclasses.make_dataclass("KernelTransition", [])(),
        ):
            self.assertIsInstance(
                sim._bind_server_recovery_anchor(
                    trusted_genesis=first,
                    trusted_checkpoint=forged_transition,  # type: ignore[arg-type]
                ),
                sim.SimulatorRefusal,
            )
        try:
            zero_arg_transition = sim.KernelTransition()
        except TypeError:
            zero_arg_transition = None
        if zero_arg_transition is not None:
            self.assertIsInstance(
                sim._bind_server_recovery_anchor(
                    trusted_genesis=first,
                    trusted_checkpoint=zero_arg_transition,
                ),
                sim.SimulatorRefusal,
            )

        no_op = first.apply(())
        assert isinstance(no_op, sim.KernelTransition)
        for authentic_transition in (no_op, crash, forward):
            self.assertIsInstance(
                recovery_anchor(first, authentic_transition),
                sim._KernelRecoveryAnchor,
            )

        closed_nonempty = first.apply(
            (
                sim.SimulationEvent(
                    "genesis-outage",
                    1,
                    bind.adapter_id,
                    "outage",
                    {"branch": "provider"},
                ),
                sim.SimulationEvent(
                    "genesis-recovery",
                    2,
                    bind.adapter_id,
                    "recovery",
                    {"branch": "provider"},
                ),
            )
        )
        assert isinstance(closed_nonempty, sim.KernelTransition)
        self.assertIsInstance(
            sim._validate_trusted_genesis(closed_nonempty.durable_state),
            sim.SimulatorRefusal,
        )
        corrupt_transcript_transition = first.apply(events)
        assert isinstance(corrupt_transcript_transition, sim.KernelTransition)
        object.__setattr__(corrupt_transcript_transition, "transcript", b"[]\n")
        self.assertIsInstance(
            sim._bind_server_recovery_anchor(
                trusted_genesis=first,
                trusted_checkpoint=corrupt_transcript_transition,
            ),
            sim.SimulatorRefusal,
        )
        corrupt_digest_anchor = recovery_anchor(first, crash)  # type: ignore[arg-type]
        object.__setattr__(corrupt_digest_anchor, "checkpoint_digest", SHA_A)
        with self.assertRaises(ValueError):
            sim.DeterministicKernel.recover(
                crash.durable_state,  # type: ignore[union-attr]
                anchor=corrupt_digest_anchor,
            )

        forged_receipt = dataclasses.replace(forward.receipts[0], payload_digest=SHA_A)  # type: ignore[union-attr]
        forged_transition = first.apply(events)
        assert isinstance(forged_transition, sim.KernelTransition)
        object.__setattr__(
            forged_transition,
            "receipts",
            (forged_receipt, *forward.receipts[1:]),  # type: ignore[union-attr]
        )
        self.assertIsInstance(
            sim._bind_server_recovery_anchor(
                trusted_genesis=first,
                trusted_checkpoint=forged_transition,
            ),
            sim.SimulatorRefusal,
        )

        authentic_anchor = recovery_anchor(first, crash)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            sim._KernelRecoveryAnchor(
                genesis_bytes=authentic_anchor.genesis_bytes,
                genesis_digest=authentic_anchor.genesis_digest,
                checkpoint_bytes=authentic_anchor.checkpoint_bytes,
                checkpoint_digest=authentic_anchor.checkpoint_digest,
            )
        with self.assertRaises(TypeError):
            sim._KernelRecoveryAnchor(
                _factory_token=object(),
                genesis_bytes=authentic_anchor.genesis_bytes,
                genesis_digest=authentic_anchor.genesis_digest,
                checkpoint_bytes=authentic_anchor.checkpoint_bytes,
                checkpoint_digest=authentic_anchor.checkpoint_digest,
            )
        with self.assertRaises(TypeError):
            dataclasses.replace(
                authentic_anchor,
                checkpoint_bytes=forward.durable_state,  # type: ignore[union-attr]
                checkpoint_digest=sim._sha_bytes(forward.durable_state),  # type: ignore[union-attr]
            )
        uninitialized_anchor = object.__new__(sim._KernelRecoveryAnchor)
        zero_arg_anchor = sim._KernelRecoveryAnchor()
        for forged_anchor in (uninitialized_anchor, zero_arg_anchor):
            with self.assertRaises(ValueError):
                sim.DeterministicKernel.recover(
                    forward.durable_state,  # type: ignore[union-attr]
                    anchor=forged_anchor,
                )

    def test_fixed_microphone_binds_identity_geometry_calibration_location_and_consent(self) -> None:
        bind = binding()
        adapter = sim.FixedMicrophoneAdapter(bind, NOW_Z)
        plan = read_plan(bind)
        state = sim.device_state_bytes(bind)
        result = ok(self, adapter.read(plan, durable_state=state), sim.SensorReadResult)
        observation = result.observation  # type: ignore[union-attr]
        self.assertEqual(observation["component_id"], bind.adapter_id)
        self.assertEqual(observation["device_revision"], bind.device_revision)
        expected_receipt = sim._fixture_receipt(
            receipt_id="receipt:read-1",
            adapter_id=bind.adapter_id,
            tick=10,
            operation="observe",
            payload={
                "device_id": bind.device_id,
                "device_revision": bind.device_revision,
                "calibration_revision": bind.calibration_revision,
                "location_ref": bind.location_ref,
                "observation": observation,
            },
        )
        self.assertEqual(result.receipt, expected_receipt)  # type: ignore[union-attr]
        projection = sensor_projection(
            bind,
            "fixed-microphone-observe",
            "org060-fixed-microphone",
            "org060-fixed-microphone/v1",
        )
        envelope = ok(
            self,
            sim.normalize_sensor_read(
                adapter,
                plan,
                durable_state=state,
                projection=projection,
                now=NOW,
                freshness=b.FreshnessWindow(60),
            ),
            b.SensoryEnvelope,
        )
        for scope in ("known_subject", "known_guest"):
            grant = b_test._grant(subject_scope=scope, consent_ref=f"consent:{scope}")
            decision = b.evaluate_capture_policy(
                envelope,  # type: ignore[arg-type]
                stage="capture",
                consent_evidence=(grant,),
                grants=(),
                purpose="companionship",
                availability="available",
                replay_cache=b.ConsentReplayCache(),
                now=NOW,
            )
            self.assertIsInstance(decision, b.CapturePolicyDecision)

    def test_microphone_refuses_bystander_revocation_and_stale_or_swapped_calibration(self) -> None:
        bind = binding()
        adapter = sim.FixedMicrophoneAdapter(bind, NOW_Z)
        projection = sensor_projection(
            bind,
            "fixed-microphone-observe",
            "org060-fixed-microphone",
            "org060-fixed-microphone/v1",
        )
        threats = (
            (read_plan(bind, logical_tick=101), sim.device_state_bytes(bind), "stale-calibration"),
            (
                read_plan(bind),
                sim.device_state_bytes(bind, calibration_device_id="device:swapped"),
                "device-binding-mismatch",
            ),
            (
                read_plan(bind, expected_calibration_revision="calibration:stale"),
                sim.device_state_bytes(bind),
                "device-binding-mismatch",
            ),
            (
                read_plan(bind, expected_location_ref="location:room-b"),
                sim.device_state_bytes(bind),
                "device-binding-mismatch",
            ),
        )
        for plan, state, reason in threats:
            with self.subTest(reason=reason):
                refused = sim.normalize_sensor_read(
                    adapter,
                    plan,
                    durable_state=state,
                    projection=projection,
                    now=NOW,
                    freshness=b.FreshnessWindow(60),
                )
                self.assert_sim_refusal(refused, reason)
        bystander = adapter.read(
            read_plan(bind, subject_scope="bystander"), durable_state=sim.device_state_bytes(bind)
        )
        assert isinstance(bystander, sim.SensorReadResult)
        envelope = b.normalize_observation(
            projection,
            bystander.observation,
            now=NOW,
            freshness=b.FreshnessWindow(60),
        )
        assert isinstance(envelope, b.SensoryEnvelope)
        self.assertEqual(
            b.evaluate_capture_policy(
                envelope,
                stage="capture",
                consent_evidence=(b_test._grant(subject_scope="bystander"),),
                grants=(),
                purpose="companionship",
                availability="available",
                replay_cache=b.ConsentReplayCache(),
                now=NOW,
            ).reason,
            "missing-consent",
        )
        known = dataclasses.replace(envelope, data={**envelope.data, "subject_scope": "known_subject"})
        self.assertEqual(
            b.evaluate_capture_policy(
                known,
                stage="capture",
                consent_evidence=(b_test._grant(revoked=True),),
                grants=(),
                purpose="companionship",
                availability="available",
                replay_cache=b.ConsentReplayCache(),
                now=NOW,
            ).reason,
            "revoked-consent",
        )
        consent_cases = (
            ((), "missing-consent"),
            ((b_test._grant(expires_at="2026-09-30T00:00:00Z"),), "expired-consent"),
            (
                (
                    b_test._grant(),
                    b_test._grant(consent_ref="consent:guest", subject_scope="known_guest"),
                ),
                "contradictory-consent",
            ),
            ((b_test._grant(purpose="different-purpose"),), "purpose-drift"),
        )
        for grants, reason in consent_cases:
            with self.subTest(consent=reason):
                self.assertEqual(
                    b.evaluate_capture_policy(
                        known,
                        stage="capture",
                        consent_evidence=grants,
                        grants=(),
                        purpose="companionship",
                        availability="available",
                        replay_cache=b.ConsentReplayCache(),
                        now=NOW,
                    ).reason,
                    reason,
                )
        replay_cache = b.ConsentReplayCache()
        replay_grant = b_test._grant(consent_ref="consent:replay")
        self.assertIsInstance(
            b.evaluate_capture_policy(
                known,
                stage="capture",
                consent_evidence=(replay_grant,),
                grants=(),
                purpose="companionship",
                availability="available",
                replay_cache=replay_cache,
                now=NOW,
            ),
            b.CapturePolicyDecision,
        )
        self.assertEqual(
            b.evaluate_capture_policy(
                known,
                stage="capture",
                consent_evidence=(replay_grant,),
                grants=(),
                purpose="companionship",
                availability="available",
                replay_cache=replay_cache,
                now=NOW,
            ).reason,
            "replayed-consent",
        )

    def test_camera_keeps_raw_local_and_emits_only_permitted_derived_event(self) -> None:
        bind = binding("component-local-camera")
        adapter = sim.LocalCameraAdapter(bind, NOW_Z)
        projection = sensor_projection(
            bind, "camera-observe", "org060-local-camera", "org060-local-camera/v1"
        )
        envelope = ok(
            self,
            sim.normalize_sensor_read(
                adapter,
                read_plan(bind),
                durable_state=sim.device_state_bytes(bind),
                projection=projection,
                now=NOW,
                freshness=b.FreshnessWindow(60),
            ),
            b.SensoryEnvelope,
        )
        result = ok(
            self,
            adapter.derive_permitted_event(
                envelope,  # type: ignore[arg-type]
                raw_media_ref="local://camera/read-1",
                raw_media_digest=SHA_A,
                payload_ref="payload:person-entered",
                payload_digest=SHA_B,
                provenance=b.DerivationProvenance(
                    envelope.digest,  # type: ignore[union-attr]
                    ("transform:redact",),
                    ({"model_ref": "model:fixture-1"},),
                    NOW_Z,
                ),
                redaction=b.RedactionEvidence(("evidence:redaction",), SHA_A),
                egress_evidence_refs=("evidence:derived-egress",),
            ),
            sim.CameraDerivedResult,
        )
        self.assertTrue(result.raw_media.data["local_ref"].startswith("local://"))  # type: ignore[union-attr]
        self.assertEqual(result.derived.data["provenance"]["source_digest"], envelope.digest)  # type: ignore[union-attr,index]
        self.assertEqual(result.egress.data["class"], "redacted_derived")  # type: ignore[union-attr]

    def test_camera_refuses_occluded_ambiguous_or_forbidden_egress(self) -> None:
        bind = binding("component-local-camera")
        adapter = sim.LocalCameraAdapter(bind, NOW_Z)
        for changes, reason in (
            ({"occluded": True}, "camera-occluded"),
            ({"ambiguous_subject": True}, "ambiguous-subject"),
        ):
            with self.subTest(reason=reason):
                self.assert_sim_refusal(
                    adapter.read(read_plan(bind), durable_state=sim.device_state_bytes(bind, **changes)),
                    reason,
                )
        projection = sensor_projection(
            bind, "camera-observe", "org060-local-camera", "org060-local-camera/v1"
        )
        envelope = sim.normalize_sensor_read(
            adapter,
            read_plan(bind),
            durable_state=sim.device_state_bytes(bind),
            projection=projection,
            now=NOW,
            freshness=b.FreshnessWindow(60),
        )
        assert isinstance(envelope, b.SensoryEnvelope)
        base = dict(
            envelope=envelope,
            raw_media_ref="local://camera/read-1",
            raw_media_digest=SHA_A,
            payload_ref="payload:event",
            payload_digest=SHA_B,
            provenance=b.DerivationProvenance(
                envelope.digest, ("transform:redact",), ({"model_ref": "model:1"},), NOW_Z
            ),
            redaction=b.RedactionEvidence(("evidence:redaction",), SHA_A),
            egress_evidence_refs=("evidence:egress",),
        )
        cases = (
            ({**base, "raw_media_ref": "https://remote/raw"}, "raw-media-remote-forbidden"),
            ({**base, "redaction": b.RedactionEvidence((), SHA_A)}, "redaction-evidence-missing"),
            ({**base, "egress_evidence_refs": ()}, "egress-evidence-missing"),
            (
                {
                    **base,
                    "provenance": b.DerivationProvenance(
                        SHA_A, ("transform:redact",), ({"model_ref": "model:1"},), NOW_Z
                    ),
                },
                "derivation-source-stale",
            ),
        )
        for kwargs, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(adapter.derive_permitted_event(**kwargs).reason, reason)

    def test_mobile_drone_tracks_position_battery_connectivity_and_bounded_motion(self) -> None:
        bind = binding("component-mobile-drone")
        adapter = drone_adapter(bind)
        initial = sim.device_state_bytes(bind, position=[0, 0, 0], battery=20, connected=True)
        result = ok(
            self,
            adapter.move(
                sim.DroneMotionRequest("move-1", 10, 9, (2, 1, 0), 3, 2, True),
                durable_state=initial,
            ),
            sim.DeviceStateTransition,
        )
        state = json.loads(result.durable_state)  # type: ignore[union-attr]
        self.assertEqual(state["position"], [2, 1, 0])
        self.assertEqual(state["battery"], 14)
        self.assertTrue(state["connected"])
        self.assertEqual(result.receipt.operation, "bounded-motion")  # type: ignore[union-attr]
        signed = adapter.move(
            sim.DroneMotionRequest("move-signed", 11, 10, (-1, -1, 0), 5, 0, True),
            durable_state=result.durable_state,  # type: ignore[union-attr]
        )
        self.assertIsInstance(signed, sim.DeviceStateTransition)
        self.assertEqual(json.loads(signed.durable_state)["battery"], 14)  # type: ignore[union-attr]

    def test_drone_refuses_stale_location_authority_denial_and_unsafe_motion(self) -> None:
        bind = binding("component-mobile-drone")
        adapter = drone_adapter(bind)
        original = sim.device_state_bytes(bind, position=[1, 1, 1], battery=5, connected=True)
        cases = (
            (sim.DroneMotionRequest("stale", 20, 1, (1, 1, 2), 5, 1, True), "stale-location"),
            (sim.DroneMotionRequest("denied", 10, 9, (1, 1, 2), 5, 1, False), "native-authority-denied"),
            (sim.DroneMotionRequest("distance", 10, 9, (9, 9, 9), 2, 1, True), "unsafe-motion"),
            (sim.DroneMotionRequest("energy", 10, 9, (1, 1, 4), 5, 3, True), "unsafe-motion"),
        )
        for request, reason in cases:
            with self.subTest(reason=reason):
                refused = adapter.move(request, durable_state=original)
                self.assert_sim_refusal(refused, reason)
                self.assertEqual(original, sim.device_state_bytes(bind, position=[1, 1, 1], battery=5, connected=True))
        disconnected = sim.device_state_bytes(bind, position=[1, 1, 1], battery=5, connected=False)
        self.assert_sim_refusal(
            adapter.move(
                sim.DroneMotionRequest("offline", 10, 9, (1, 1, 2), 5, 1, True),
                durable_state=disconnected,
            ),
            "device-unavailable",
        )
        hostile_cases = (
            (
                sim.DroneMotionRequest("future", 10, 11, (1, 1, 2), 5, 1, True),
                original,
                "logical-time-inconsistent",
            ),
            (
                sim.DroneMotionRequest("nonbool-authority", 10, 9, (1, 1, 2), 5, 1, 1),
                original,
                "native-authority-denied",
            ),
            (
                sim.DroneMotionRequest("negative-energy", 10, 9, (1, 1, 2), 5, -1, True),
                original,
                "invalid-motion-number",
            ),
            (
                sim.DroneMotionRequest("bool-distance", 10, 9, (1, 1, 2), True, 1, True),
                original,
                "invalid-motion-number",
            ),
            (
                sim.DroneMotionRequest("provider-outage", 10, 9, (1, 1, 2), 5, 1, True),
                sim.device_state_bytes(bind, position=[1, 1, 1], battery=5, provider="outage"),
                "provider-outage",
            ),
            (
                sim.DroneMotionRequest("swapped-device", 10, 9, (1, 1, 2), 5, 1, True),
                sim.device_state_bytes(bind, position=[1, 1, 1], battery=5, device_id="device:other"),
                "device-binding-mismatch",
            ),
            (
                sim.DroneMotionRequest("swapped-revision", 10, 9, (1, 1, 2), 5, 1, True),
                sim.device_state_bytes(bind, position=[1, 1, 1], battery=5, device_revision="revision:other"),
                "device-binding-mismatch",
            ),
            (
                sim.DroneMotionRequest("swapped-calibration", 10, 9, (1, 1, 2), 5, 1, True),
                sim.device_state_bytes(bind, position=[1, 1, 1], battery=5, calibration_revision="calibration:other"),
                "device-binding-mismatch",
            ),
            (
                sim.DroneMotionRequest("swapped-location", 10, 9, (1, 1, 2), 5, 1, True),
                sim.device_state_bytes(bind, position=[1, 1, 1], battery=5, location_ref="location:other"),
                "device-binding-mismatch",
            ),
            (
                sim.DroneMotionRequest("network-outage", 10, 9, (1, 1, 2), 5, 1, True),
                sim.device_state_bytes(bind, position=[1, 1, 1], battery=5, network="outage"),
                "network-outage",
            ),
            (
                sim.DroneMotionRequest("negative-distance", 10, 9, (1, 1, 2), -1, 1, True),
                original,
                "invalid-motion-number",
            ),
            (
                sim.DroneMotionRequest("float-coordinate", 10, 9, (1, 1, 2.0), 5, 1, True),
                original,
                "invalid-motion-number",
            ),
            (
                sim.DroneMotionRequest("bool-tick", True, 0, (1, 1, 2), 5, 1, True),
                original,
                "logical-time-invalid",
            ),
            (
                sim.DroneMotionRequest("bool-location-tick", 10, True, (1, 1, 2), 5, 1, True),
                original,
                "logical-time-invalid",
            ),
            (
                sim.DroneMotionRequest("float-location-tick", 10, 9.0, (1, 1, 2), 5, 1, True),
                original,
                "logical-time-invalid",
            ),
            (
                sim.DroneMotionRequest("negative-location-tick", 10, -1, (1, 1, 2), 5, 1, True),
                original,
                "logical-time-invalid",
            ),
            (
                sim.DroneMotionRequest("negative-battery", 10, 9, (1, 1, 2), 5, 1, True),
                sim.device_state_bytes(bind, position=[1, 1, 1], battery=-1),
                "device-state-schema-invalid",
            ),
        )
        for request, state, reason in hostile_cases:
            with self.subTest(hostile=reason):
                self.assert_sim_refusal(adapter.move(request, durable_state=state), reason)
                self.assertEqual(state, bytes(state))

    def test_iot_sensor_observation_consumes_real_child_b_normalization_and_policy(self) -> None:
        bind = binding("component-iot-sensor")
        adapter = sim.IoTSensorAdapter(bind, NOW_Z)
        projection = sensor_projection(
            bind, "iot-sensor-observe", "org060-iot-sensor", "org060-iot-sensor/v1"
        )
        envelope = sim.normalize_sensor_read(
            adapter,
            read_plan(bind),
            durable_state=sim.device_state_bytes(bind),
            projection=projection,
            now=NOW,
            freshness=b.FreshnessWindow(60),
        )
        self.assertIsInstance(projection, cn.NavigatorProjection)
        self.assertIsInstance(envelope, b.SensoryEnvelope)
        decision = b.evaluate_capture_policy(
            envelope,  # type: ignore[arg-type]
            stage="capture",
            consent_evidence=(b_test._grant(),),
            grants=(),
            purpose="companionship",
            availability="available",
            replay_cache=b.ConsentReplayCache(),
            now=NOW,
        )
        self.assertIsInstance(decision, b.CapturePolicyDecision)
        fake = dataclasses.make_dataclass("NavigatorProjection", [("capabilities", tuple)])(())
        self.assert_sim_refusal(
            sim.normalize_sensor_read(
                adapter,
                read_plan(bind),
                durable_state=sim.device_state_bytes(bind),
                projection=fake,
                now=NOW,
                freshness=b.FreshnessWindow(60),
            ),
            "owner-type-required",
        )

    def test_reversible_actuator_consumes_real_child_b_plan_invoke_verify_cancel_and_rollback(self) -> None:
        adapter = actuator_adapter(binding("component-iot-actuator"))
        plan, context, _projection = owner_actuation_objects()
        invoked = ok(self, b.invoke(plan, context=context, adapter=adapter, now=NOW), b.ActuationReceipt)
        self.assertEqual(invoked.data["dispatch_state"], "provider_accepted")  # type: ignore[union-attr]
        self.assertEqual(invoked.data["verification_state"], "pending_observation")  # type: ignore[union-attr]
        verified = ok(
            self,
            b.observe_effect(
                invoked,  # type: ignore[arg-type]
                b.TypedEffectObservation("observation:effect", SHA_A, NOW_Z, True),
                now=NOW,
            ),
            b.ActuationReceipt,
        )
        cancellable = b.invoke(plan, context=context, adapter=adapter, now=NOW)
        assert isinstance(cancellable, b.ActuationReceipt)
        accepted = ok(
            self,
            b.request_cancellation(
                cancellable,
                context,
                adapter=adapter,
                evidence_refs=("evidence:cancel-request",),
                now=NOW,
            ),
            b.ActuationReceipt,
        )
        stopped = ok(
            self,
            b.observe_cancellation(
                accepted,  # type: ignore[arg-type]
                b.TypedCancellationObservation("observation:stopped", SHA_B, NOW_Z, True),
                now=NOW,
            ),
            b.ActuationReceipt,
        )
        rollback = ok(
            self,
            b.rollback_effect(
                verified,  # type: ignore[arg-type]
                context=context,
                adapter=adapter,
                approval_evidence_ref="approval:rollback",
                now=NOW,
            ),
            b.ActuationReceipt,
        )
        restored = ok(
            self,
            b.observe_rollback(
                rollback,  # type: ignore[arg-type]
                b.TypedRollbackObservation("observation:restored", SHA_A, NOW_Z, True),
                None,
                now=NOW,
            ),
            b.ActuationReceipt,
        )
        self.assertEqual(stopped.data["cancellation_state"], "observed_stopped")  # type: ignore[union-attr]
        self.assertEqual(restored.data["rollback_state"], "verified")  # type: ignore[union-attr]

    def test_actuator_distinguishes_dispatch_verification_cancellation_and_residual_rollback_risk(self) -> None:
        bind = binding("component-iot-actuator")
        plan, context, owner_projection = owner_actuation_objects()
        accepted_adapter = actuator_adapter(bind)
        authority = accepted_adapter.actuator_binding
        self.assertFalse(hasattr(sim, "_initialize_server_anchor"))
        self.assertFalse(hasattr(sim, "_ANCHOR_FACTORY_TOKEN"))
        with self.assertRaises(TypeError):
            sim._ActuatorAuthorizationAnchor(**dataclasses.asdict(authority))
        authority_values = {
            item.name: getattr(authority, item.name)
            for item in dataclasses.fields(authority)
            if item.name != "_seal"
        }
        with self.assertRaises(TypeError):
            sim._ActuatorAuthorizationAnchor(
                _factory_token=object(),
                **authority_values,
            )
        with self.assertRaises(TypeError):
            dataclasses.replace(
                authority,
                approved_plan_digest=resigned_plan(
                    plan, effect={"set": "forged"}
                ).digest,
            )
        uninitialized_authority = object.__new__(sim._ActuatorAuthorizationAnchor)
        zero_arg_authority = sim._ActuatorAuthorizationAnchor()
        for forged_authority in (uninitialized_authority, zero_arg_authority):
            self.assertIsInstance(
                sim._validate_actuation_dispatch(
                    plan,
                    context,
                    forged_authority,
                    accepted_adapter.actuator_state,
            ),
            sim.SimulatorRefusal,
        )
        outsider_projection = engine_test._projection(
            engine_test._visible_record(
                "component-outsider",
                capability="light-control",
                contract="org060-actuator",
                schema="org060-light-v1",
            )
        )
        outsider_provider = outsider_projection.capabilities[0]
        outsider_bound_plan = resigned_plan(
            plan,
            capability_binding_digest=b.provider_binding_digest(
                outsider_provider,
                owner_projection,
            ),
        )
        self.assertIsInstance(
            sim._bind_server_actuator_authorization(
                adapter_id=bind.adapter_id,
                approved_plan=outsider_bound_plan,
                context=context,
                provider=outsider_provider,
                projection=owner_projection,
            ),
            sim.SimulatorRefusal,
        )
        noncanonical_plan = b.ActuationPlan(
            {**plan.data, "preconditions": tuple(plan.data["preconditions"])}
        )
        self.assertIsInstance(
            sim._bind_server_actuator_authorization(
                adapter_id=bind.adapter_id,
                approved_plan=noncanonical_plan,
                context=context,
                provider=owner_projection.capabilities[0],
                projection=owner_projection,
            ),
            sim.SimulatorRefusal,
        )
        for changed_plan in (
            resigned_plan(plan, capability_id="capability:wrong"),
            resigned_plan(plan, capability_binding_digest=SHA_A),
            resigned_plan(plan, projection_sha256=SHA_A),
            resigned_plan(plan, catalogue_sha256=SHA_A),
            resigned_plan(plan, lock_sha256=SHA_A),
        ):
            self.assertIsInstance(
                sim._bind_server_actuator_authorization(
                    adapter_id=bind.adapter_id,
                    approved_plan=changed_plan,
                    context=context,
                    provider=owner_projection.capabilities[0],
                    projection=owner_projection,
                ),
                sim.SimulatorRefusal,
            )
        fake_authenticated_context = dataclasses.make_dataclass(
            "AuthenticatedContext", [("subject_ref", str)]
        )("subject:operator")
        for hostile_context in (
            dataclasses.replace(context, native_authority_ref=""),
            dataclasses.replace(context, native_authority_ref="native-authority:wrong"),
            dataclasses.replace(context, approval_evidence_ref="approval:wrong"),
            dataclasses.replace(
                context,
                authenticated_context=fake_authenticated_context,  # type: ignore[arg-type]
            ),
            dataclasses.replace(
                context,
                authenticated_context=dataclasses.replace(
                    context.authenticated_context, subject_ref="subject:wrong"
                ),
            ),
            dataclasses.replace(
                context,
                authenticated_context=dataclasses.replace(
                    context.authenticated_context, uid=context.authenticated_context.uid + 1
                ),
            ),
            *(
                dataclasses.replace(
                    context,
                    authenticated_context=dataclasses.replace(
                        context.authenticated_context, **{field: value}
                    ),
                )
                for field, value in (
                    ("persona_ref", "persona:wrong"),
                    ("role_refs", ("role:wrong",)),
                    ("instance_ref", "instance:wrong"),
                    ("trust_zone", "wrong-zone"),
                    ("registry_revision", "registry:wrong"),
                )
            ),
        ):
            self.assertEqual(
                accepted_adapter.dispatch(plan, context=hostile_context).state,
                "provider_denied",
            )
        tampered = b.ActuationPlan({**plan.data, "target_ref": "target:wrong"})
        self.assertEqual(
            accepted_adapter.dispatch(tampered, context=context).state,
            "provider_denied",
        )
        digest_only_tamper = b.ActuationPlan({**plan.data, "plan_digest": SHA_A})
        self.assertEqual(
            accepted_adapter.dispatch(digest_only_tamper, context=context).state,
            "provider_denied",
        )
        hostile_plans = (
            resigned_plan(plan, target_ref="target:wrong"),
            resigned_plan(plan, capability_id="capability:wrong"),
            resigned_plan(plan, capability_binding_digest=SHA_A),
            resigned_plan(plan, expires_at=NOW_Z),
            resigned_plan(plan, effect={"set": "forged"}),
            resigned_plan(plan, expected_result={"state": "forged"}),
            resigned_plan(
                plan,
                verification_steps=[
                    {
                        "step_id": "verify:forged",
                        "observation_capability_id": "light-control",
                        "expected_result": {"state": "forged"},
                        "evidence_refs": ["evidence:forged"],
                    }
                ],
            ),
            resigned_plan(
                plan,
                rollback_steps=[
                    {
                        "step_id": "rollback:forged",
                        "effect": {"set": "off"},
                        "preconditions": [{"state": "on"}],
                        "expected_result": {"state": "off"},
                        "evidence_refs": ["evidence:forged"],
                    }
                ],
            ),
            resigned_plan(
                plan,
                idempotency={"key": "forged", "class": "idempotent_same_plan", "max_attempts": 1},
            ),
            resigned_plan(plan, request_digest=SHA_A),
            resigned_plan(plan, projection_sha256=SHA_A),
            resigned_plan(plan, catalogue_sha256=SHA_A),
            resigned_plan(plan, lock_sha256=SHA_A),
        )
        for hostile_plan in hostile_plans:
            with self.subTest(plan=hostile_plan.data):
                self.assertEqual(
                    accepted_adapter.dispatch(hostile_plan, context=context).state,
                    "provider_denied",
                )
        hostile_states = (
            {"target_ref": "target:wrong"},
            {"capability_id": "capability:wrong"},
            {"provider_component_id": "component:wrong"},
            {"provider_revision": "revision:wrong"},
            {"target_state_digest": SHA_A},
            {"current_preconditions": [{"state": "on"}]},
            {"network": "outage"},
            {"provider": "outage"},
            {"device_available": False},
        )
        for state_changes in hostile_states:
            with self.subTest(state=state_changes):
                hostile_adapter = actuator_adapter(bind, **state_changes)
                result = hostile_adapter.dispatch(plan, context=context)
                self.assertEqual(result.state, "provider_denied")
                self.assertTrue(result.status_evidence[0].startswith("fixture:refusal:"))
        dispatched = b.invoke(plan, context=context, adapter=accepted_adapter, now=NOW)
        assert isinstance(dispatched, b.ActuationReceipt)
        self.assertNotEqual(dispatched.data["dispatch_state"], dispatched.data["verification_state"])
        timeout_adapter = actuator_adapter(
            bind, outcome_script=sim.outcome_script_bytes(dispatch="provider_timeout")
        )
        timed_out = b.invoke(plan, context=context, adapter=timeout_adapter, now=NOW)
        assert isinstance(timed_out, b.ActuationReceipt)
        self.assertEqual(timed_out.data["dispatch_state"], "provider_timeout")
        self.assertEqual(
            b.observe_effect(
                timed_out,
                b.TypedEffectObservation("obs:timeout", SHA_A, NOW_Z, True),
                now=NOW,
            ).reason,
            "dispatch-not-completion",
        )
        denied_dispatch = b.invoke(
            plan,
            context=context,
            adapter=actuator_adapter(
                bind, outcome_script=sim.outcome_script_bytes(dispatch="provider_denied")
            ),
            now=NOW,
        )
        self.assertEqual(denied_dispatch.reason, "provider-denied")
        mismatch = b.observe_effect(
            dispatched,
            b.TypedEffectObservation("obs:mismatch", SHA_A, NOW_Z, False),
            now=NOW,
        )
        self.assertEqual(mismatch.reason, "verification-mismatch")
        requested_only = b.request_cancellation(
            dispatched,
            context,
            evidence_refs=("evidence:cancel-requested",),
            now=NOW,
        )
        assert isinstance(requested_only, b.ActuationReceipt)
        self.assertEqual(requested_only.data["cancellation_state"], "requested")
        cancel = b.request_cancellation(
            dispatched,
            context,
            adapter=accepted_adapter,
            evidence_refs=("evidence:cancel",),
            now=NOW,
        )
        assert isinstance(cancel, b.ActuationReceipt)
        self.assertEqual(cancel.data["cancellation_state"], "provider_accepted")
        self.assertNotEqual(cancel.data["cancellation_state"], "observed_stopped")
        denied_cancel = b.request_cancellation(
            dispatched,
            context,
            adapter=actuator_adapter(
                bind, outcome_script=sim.outcome_script_bytes(cancel="denied")
            ),
            evidence_refs=("evidence:cancel-denied",),
            now=NOW,
        )
        assert isinstance(denied_cancel, b.ActuationReceipt)
        self.assertEqual(denied_cancel.data["cancellation_state"], "denied")
        verified = b.observe_effect(
            dispatched, b.TypedEffectObservation("obs:effect", SHA_A, NOW_Z, True), now=NOW
        )
        assert isinstance(verified, b.ActuationReceipt)
        self.assertEqual(
            b.rollback_effect(
                verified,
                context=context,
                adapter=accepted_adapter,
                approval_evidence_ref="",
                now=NOW,
            ).reason,
            "rollback-approval-missing",
        )
        rollback = b.rollback_effect(
            verified,
            context=context,
            adapter=accepted_adapter,
            approval_evidence_ref="approval:separate",
            now=NOW,
        )
        assert isinstance(rollback, b.ActuationReceipt)
        partial = b.observe_rollback(
            rollback,
            b.TypedRollbackObservation("obs:partial", SHA_B, NOW_Z, False),
            b.ResidualRisk("bounded", "fixture state remains changed", ("evidence:residual",)),
            now=NOW,
        )
        assert isinstance(partial, b.ActuationReceipt)
        self.assertEqual(partial.data["rollback_state"], "partial")
        self.assertEqual(partial.data["residual_risk"]["class"], "bounded")  # type: ignore[index]
        self.assertEqual(
            b.observe_rollback(
                rollback,
                b.TypedRollbackObservation("obs:partial", SHA_B, NOW_Z, False),
                None,
                now=NOW,
            ).reason,
            "residual-risk-required",
        )
        denied_adapter = actuator_adapter(
            bind, outcome_script=sim.outcome_script_bytes(rollback="denied")
        )
        denied = b.rollback_effect(
            verified,
            context=context,
            adapter=denied_adapter,
            approval_evidence_ref="approval:separate",
            now=NOW,
        )
        self.assertEqual(denied.reason, "rollback-denied")
        self.assertEqual(verified.data["rollback_state"], "not_requested")

        malformed_receipt = {"plan_digest": plan.digest}
        self.assertEqual(
            accepted_adapter.cancel(malformed_receipt, context=context).state,
            "denied",
        )
        self.assertEqual(
            accepted_adapter.rollback(malformed_receipt, context=context).state,
            "denied",
        )
        for receipt_change in (
            {"plan_digest": SHA_A},
            {"request_digest": SHA_A},
            {"capability_id": "capability:wrong"},
            {"target_ref": "target:wrong"},
            {"target_state_digest": SHA_A},
        ):
            hostile_receipt = resigned_receipt(dispatched, **receipt_change)
            with self.subTest(receipt=receipt_change):
                self.assertEqual(
                    accepted_adapter.cancel(hostile_receipt, context=context).state,
                    "denied",
                )
                self.assertEqual(
                    accepted_adapter.rollback(hostile_receipt, context=context).state,
                    "denied",
                )

    def test_display_and_wearable_expose_truthful_privacy_recording_action_and_availability(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            world = presence_test.World(Path(root))
            for availability in c.AVAILABILITY:
                projection = world.projection(companion=world.companion(availability=availability))
                for cls, surface_id, kind in (
                    (sim.FixtureDisplayAdapter, "surface-display", "display"),
                    (sim.FixtureWearableAdapter, "surface-wearable", "wearable"),
                ):
                    state = presence_test.ready_surface(surface_id, surface_kind=kind)
                    adapter = cls(f"adapter:{surface_id}", presence_test.ago(1))
                    result = c.render_presence(
                        projection,
                        surface=state,
                        readiness=presence_test.ready(state),
                        adapter=adapter,
                        now=presence_test.NOW,
                        freshness=presence_test.FRESH,
                    )
                    self.assertIsInstance(result, c.PresenceRenderResult, result)
                    indicators = result.data["indicators"]  # type: ignore[union-attr]
                    self.assertEqual(indicators["availability"], availability)
                    self.assertEqual(indicators["privacy"], "local_only")
                    self.assertEqual(indicators["recording"], "recording")
                    self.assertEqual(indicators["action"], "idle")
            projection = world.projection()
            for connectivity in c.CONNECTIVITY:
                state = presence_test.observe(presence_test.ready_surface(), connectivity)
                result = c.render_presence(
                    projection,
                    surface=state,
                    readiness=presence_test.ready(state),
                    adapter=sim.FixtureDisplayAdapter("adapter:display", presence_test.ago(1)),
                    now=presence_test.NOW,
                    freshness=presence_test.FRESH,
                )
                self.assertEqual(result.data["indicators"]["connectivity"], connectivity)  # type: ignore[union-attr,index]
            projection = world.projection()
            state = presence_test.ready_surface()
            interaction_request = presence_test.request(projection)
            interaction_envelope = presence_test.envelope(interaction_request)
            interaction_adapter = sim.FixtureDisplayAdapter(
                "adapter:interaction",
                presence_test.ago(1),
                sim.canonical_bytes(interaction_envelope),
            )
            adapter_input = interaction_adapter.read_interaction(surface_id="surface-display")
            decision = c.accept_presence_interaction(
                adapter_input,
                current=projection,
                surface=state,
                readiness=presence_test.ready(state),
                context=world.context,
                consent_evidence=(presence_test.grant(world),),
                replay_cache=b.ConsentReplayCache(),
                now=presence_test.NOW,
                freshness=presence_test.FRESH,
            )
            self.assertIsInstance(decision, c.PresenceInteractionDecision, decision)
            quiet_projection = world.projection(companion=world.companion(availability="quiet"))
            quiet_request = presence_test.request(quiet_projection)
            quiet_decision = c.accept_presence_interaction(
                presence_test.envelope(quiet_request),
                current=quiet_projection,
                surface=state,
                readiness=presence_test.ready(state),
                context=world.context,
                consent_evidence=(presence_test.grant(world),),
                replay_cache=b.ConsentReplayCache(),
                now=presence_test.NOW,
                freshness=presence_test.FRESH,
            )
            self.assertEqual(quiet_decision.reason, "availability-override")

    def test_two_presence_surfaces_share_one_checkpoint_and_replace_without_continuity_fork(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            world = presence_test.World(Path(root))
            projection = world.projection()
            projection_bytes = b.canonical_json_bytes(c.presence_projection_to_json(projection))
            surfaces = (
                (
                    sim.FixtureDesktopPresenceAdapter("adapter:desktop", presence_test.ago(1)),
                    presence_test.ready_surface("surface-desktop", surface_kind="desktop_pet"),
                ),
                (
                    sim.FixtureBadgePresenceAdapter("adapter:badge", presence_test.ago(1)),
                    presence_test.ready_surface("surface-badge", surface_kind="badge"),
                ),
            )
            results = []
            for adapter, state in surfaces:
                results.append(
                    c.render_presence(
                        projection,
                        surface=state,
                        readiness=presence_test.ready(state),
                        adapter=adapter,
                        now=presence_test.NOW,
                        freshness=presence_test.FRESH,
                    )
                )
            self.assertTrue(all(isinstance(item, c.PresenceRenderResult) for item in results))
            self.assertEqual(results[0].data["projection_digest"], results[1].data["projection_digest"])  # type: ignore[union-attr]
            self.assertEqual(
                projection.data["continuity"]["checkpoint_id"],  # type: ignore[index]
                c.parse_presence_projection(projection_bytes).data["continuity"]["checkpoint_id"],  # type: ignore[union-attr,index]
            )
            removed = presence_test.step(surfaces[0][1], presence_test.event("remove", "executed"))
            replacement = presence_test.ready_surface("surface-desktop-2", surface_kind="desktop_pet")
            replacement_result = c.render_presence(
                projection,
                surface=replacement,
                readiness=presence_test.ready(replacement),
                adapter=sim.FixtureDesktopPresenceAdapter("adapter:desktop-2", presence_test.ago(1)),
                now=presence_test.NOW,
                freshness=presence_test.FRESH,
            )
            self.assertIsInstance(replacement_result, c.PresenceRenderResult)
            self.assertEqual(replacement_result.data["projection_digest"], projection.digest)  # type: ignore[union-attr]
            self.assertEqual(
                c.render_presence(
                    projection,
                    surface=removed,
                    readiness=presence_test.ready(surfaces[0][1]),
                    adapter=surfaces[0][0],
                    now=presence_test.NOW,
                    freshness=presence_test.FRESH,
                ).reason,
                "surface-removed",
            )
            self.assertEqual(projection_bytes, b.canonical_json_bytes(c.presence_projection_to_json(projection)))
            for adapter, _state in surfaces:
                self.assertFalse(hasattr(adapter, "advance_continuity"))

    def test_presence_outage_degradation_restart_and_durable_recovery_use_real_child_c_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            world = presence_test.World(Path(root))
            projection = world.projection()
            durable = b.canonical_json_bytes(c.presence_projection_to_json(projection))
            degraded = presence_test.observe(presence_test.ready_surface(), "degraded")
            rendered = c.render_presence(
                projection,
                surface=degraded,
                readiness=presence_test.ready(degraded),
                adapter=sim.FixtureDisplayAdapter("adapter:display", presence_test.ago(1)),
                now=presence_test.NOW,
                freshness=presence_test.FRESH,
            )
            self.assertEqual(rendered.data["indicators"]["connectivity"], "degraded")  # type: ignore[union-attr,index]
            restarted = c.recover_presence_projection(
                durable,
                view_path=world.view_path,
                checkpoint_receipt=world.receipt,
                checkpoint_root=world.fx.checkpoints,
                navigator=world.navigator,
                context=world.context,
                companion=world.companion(),
                now=presence_test.NOW,
            )
            self.assertIsInstance(restarted, c.PresenceProjection, restarted)
            self.assertIsNot(restarted, projection)
            self.assertEqual(restarted, projection)
            for session_local in (projection, dict(projection.data), durable.decode(), bytearray(durable)):
                refused = c.recover_presence_projection(
                    session_local,  # type: ignore[arg-type]
                    view_path=world.view_path,
                    checkpoint_receipt=world.receipt,
                    checkpoint_root=world.fx.checkpoints,
                    navigator=world.navigator,
                    context=world.context,
                    companion=world.companion(),
                    now=presence_test.NOW,
                )
                self.assertEqual(refused.reason, "recovery-source-invalid")
            fresh_adapter = sim.FixtureDisplayAdapter("adapter:restart", presence_test.ago(1))
            state = presence_test.ready_surface()
            self.assertIsInstance(
                c.render_presence(
                    restarted,  # type: ignore[arg-type]
                    surface=state,
                    readiness=presence_test.ready(state),
                    adapter=fresh_adapter,
                    now=presence_test.NOW,
                    freshness=presence_test.FRESH,
                ),
                c.PresenceRenderResult,
            )

    def test_all_fixture_adapters_require_five_lifecycle_legs_and_externally_legible_health(self) -> None:
        adapter_ids = (
            "component-fixed-microphone",
            "component-local-camera",
            "component-mobile-drone",
            "component-iot-sensor",
            "component-iot-actuator",
            "surface-display",
            "surface-wearable",
            "surface-desktop",
            "surface-badge",
        )
        binds = tuple(binding(item) for item in adapter_ids)
        kernel = sim.DeterministicKernel.create(adapter_bindings=binds, initial_tick=0)
        events: list[sim.SimulationEvent] = []
        for index, adapter_id in enumerate(adapter_ids):
            events.extend(five_leg_events(adapter_id, 1 + index * 10))
        transition = ok(self, kernel.apply(tuple(reversed(events))), sim.KernelTransition)
        ready_kernel = recover_checkpoint(kernel, transition)  # type: ignore[arg-type]
        for index, adapter_id in enumerate(adapter_ids):
            readiness = ready_kernel.evaluate_lifecycle(
                adapter_id,
                now_tick=95,
                health_freshness_ticks=100,
            )
            self.assertIsInstance(readiness, sim.LifecycleReadiness, (adapter_id, readiness))
        missing = sim.DeterministicKernel.create(adapter_bindings=(binding("missing"),), initial_tick=0)
        partial = missing.apply(tuple(five_leg_events("missing")[:-3]))
        assert isinstance(partial, sim.KernelTransition)
        self.assert_sim_refusal(
            recover_checkpoint(missing, partial).evaluate_lifecycle(
                "missing", now_tick=5, health_freshness_ticks=10
            ),
            "lifecycle-leg-missing",
        )
        unknown_events = five_leg_events("unknown")
        unknown_events[4] = lifecycle_event(
            "unknown", 5, "health", "unknown", suffix="-post-update"
        )
        unknown_kernel = sim.DeterministicKernel.create(
            adapter_bindings=(binding("unknown"),), initial_tick=0
        )
        unknown = unknown_kernel.apply(unknown_events)
        assert isinstance(unknown, sim.KernelTransition)
        self.assert_sim_refusal(
            recover_checkpoint(unknown_kernel, unknown).evaluate_lifecycle(
                "unknown", now_tick=6, health_freshness_ticks=10
            ),
            "health-unknown",
        )
        for health_outcome in ("degraded", "unhealthy"):
            with self.subTest(health=health_outcome):
                health_events = five_leg_events(health_outcome)
                health_events[4] = lifecycle_event(
                    health_outcome,
                    5,
                    "health",
                    health_outcome,
                    suffix="-post-update",
                )
                health_kernel = sim.DeterministicKernel.create(
                    adapter_bindings=(binding(health_outcome),), initial_tick=0
                )
                health_transition = health_kernel.apply(health_events)
                assert isinstance(health_transition, sim.KernelTransition)
                self.assert_sim_refusal(
                    recover_checkpoint(health_kernel, health_transition).evaluate_lifecycle(
                        health_outcome, now_tick=6, health_freshness_ticks=10
                    ),
                    "health-unhealthy",
                )
        first_ready = ready_kernel.evaluate_lifecycle(
            adapter_ids[0], now_tick=1000, health_freshness_ticks=10
        )
        self.assert_sim_refusal(first_ready, "health-stale")
        invalid = binding("invalid-order")
        for bad_events in (
            (
                lifecycle_event(invalid.adapter_id, 1, "install", "verified"),
                lifecycle_event(
                    invalid.adapter_id,
                    2,
                    "install",
                    "verified",
                    suffix="-again",
                ),
            ),
            (
                lifecycle_event(invalid.adapter_id, 1, "install", "verified"),
                lifecycle_event(invalid.adapter_id, 2, "configure", "verified"),
                lifecycle_event(invalid.adapter_id, 3, "update", "verified"),
            ),
            (
                lifecycle_event(invalid.adapter_id, 1, "install", "verified"),
                lifecycle_event(invalid.adapter_id, 2, "configure", "verified"),
                lifecycle_event(invalid.adapter_id, 3, "health", "healthy"),
                lifecycle_event(invalid.adapter_id, 4, "update", "verified"),
                lifecycle_event(invalid.adapter_id, 5, "remove", "executed"),
            ),
            (
                lifecycle_event(invalid.adapter_id, 1, "install", "verified"),
                lifecycle_event(invalid.adapter_id, 2, "configure", "verified"),
                lifecycle_event(invalid.adapter_id, 3, "configure", "verified", suffix="-again"),
            ),
            (
                lifecycle_event(invalid.adapter_id, 1, "install", "verified"),
                lifecycle_event(
                    invalid.adapter_id,
                    2,
                    "configure",
                    "verified",
                    target_revision="irrelevant",
                ),
            ),
            (
                lifecycle_event(invalid.adapter_id, 1, "install", "verified"),
                lifecycle_event(invalid.adapter_id, 2, "configure", "verified"),
                lifecycle_event(
                    invalid.adapter_id,
                    3,
                    "health",
                    "healthy",
                    target_revision="irrelevant",
                ),
            ),
            (
                lifecycle_event(invalid.adapter_id, 1, "install", "verified"),
                lifecycle_event(invalid.adapter_id, 2, "configure", "verified"),
                lifecycle_event(invalid.adapter_id, 3, "health", "healthy"),
                lifecycle_event(
                    invalid.adapter_id,
                    4,
                    "update",
                    "interrupted",
                    target_revision="irrelevant",
                ),
            ),
            (
                lifecycle_event(invalid.adapter_id, 1, "install", "verified"),
                lifecycle_event(invalid.adapter_id, 2, "configure", "verified"),
                lifecycle_event(invalid.adapter_id, 3, "health", "healthy"),
                lifecycle_event(invalid.adapter_id, 4, "update", "interrupted"),
                lifecycle_event(
                    invalid.adapter_id,
                    5,
                    "update",
                    "rolled_back",
                    target_revision="irrelevant",
                ),
            ),
            (
                *five_leg_events(invalid.adapter_id)[:-1],
                lifecycle_event(
                    invalid.adapter_id,
                    6,
                    "remove",
                    "rehearsed",
                    target_revision="irrelevant",
                ),
            ),
        ):
            kernel = sim.DeterministicKernel.create(adapter_bindings=(invalid,), initial_tick=0)
            self.assertIsInstance(kernel.apply(bad_events), sim.SimulatorRefusal)

        update_bind = binding("update-health-invalidation")
        update_kernel = sim.DeterministicKernel.create(
            adapter_bindings=(update_bind,), initial_tick=0
        )
        update_events = five_leg_events(update_bind.adapter_id)
        updated = update_kernel.apply(update_events[:4])
        assert isinstance(updated, sim.KernelTransition)
        pre_update = update_kernel.apply(update_events[:3])
        assert isinstance(pre_update, sim.KernelTransition)
        old_epoch_state = json.loads(updated.durable_state)
        old_epoch_state["adapters"][update_bind.adapter_id]["lifecycle"]["legs"][
            "health"
        ] = json.loads(pre_update.durable_state)["adapters"][update_bind.adapter_id][
            "lifecycle"
        ]["legs"]["health"]
        old_epoch_state["adapters"][update_bind.adapter_id]["lifecycle"]["legs"][
            "health"
        ]["logical_tick"] = 4
        self.assertIsInstance(
            sim._decode_kernel_state(sim.canonical_bytes(old_epoch_state)),
            sim.SimulatorRefusal,
        )
        self.assert_sim_refusal(
            recover_checkpoint(update_kernel, updated).evaluate_lifecycle(
                update_bind.adapter_id, now_tick=4, health_freshness_ticks=10
            ),
            "health-unknown",
        )
        refreshed = recover_checkpoint(update_kernel, updated).apply(
            (update_events[4], update_events[5])
        )
        assert isinstance(refreshed, sim.KernelTransition)
        refreshed_readiness = recover_checkpoint(update_kernel, refreshed).evaluate_lifecycle(
                update_bind.adapter_id, now_tick=6, health_freshness_ticks=10
        )
        self.assertIsInstance(refreshed_readiness, sim.LifecycleReadiness)
        self.assertEqual(refreshed_readiness.lifecycle_epoch, 2)  # type: ignore[union-attr]
        self.assertEqual(
            refreshed_readiness.lifecycle_revision,  # type: ignore[union-attr]
            "fixture-revision-2",
        )

        child_c_ready = presence_test.ready_surface()
        child_c_updated = presence_test.step(
            child_c_ready,
            presence_test.event("update", "verified", to_revision="rev-3"),
        )
        child_c_invalidated = presence_test.readiness(child_c_updated)
        self.assertIsInstance(child_c_invalidated, c.PresenceRefusal)
        self.assertEqual(child_c_invalidated.reason, "health-unknown")
        child_c_refreshed = presence_test.step(
            child_c_updated,
            presence_test.event("health", "healthy"),
        )
        self.assertIsInstance(
            presence_test.readiness(child_c_refreshed),
            c.PresenceReadiness,
        )

    def test_interrupted_update_remove_reprovision_and_crash_points_recover_deterministically(self) -> None:
        bind = binding("lifecycle-device")
        events = [
            lifecycle_event(bind.adapter_id, 1, "install", "verified"),
            lifecycle_event(bind.adapter_id, 2, "configure", "verified"),
            lifecycle_event(bind.adapter_id, 3, "health", "healthy"),
            lifecycle_event(bind.adapter_id, 4, "update", "interrupted"),
            lifecycle_event(bind.adapter_id, 5, "update", "rolled_back", suffix="-rollback"),
            lifecycle_event(bind.adapter_id, 6, "health", "healthy", suffix="-rollback"),
            lifecycle_event(bind.adapter_id, 7, "remove", "rehearsed"),
            lifecycle_event(bind.adapter_id, 8, "remove", "executed", suffix="-execute"),
            lifecycle_event(
                bind.adapter_id,
                9,
                "install",
                "verified",
                suffix="-reprovision",
                target_revision="fixture-revision-3",
            ),
            lifecycle_event(bind.adapter_id, 10, "configure", "verified", suffix="-reprovision"),
            lifecycle_event(bind.adapter_id, 11, "health", "healthy", suffix="-reprovision"),
            lifecycle_event(
                bind.adapter_id,
                12,
                "update",
                "verified",
                suffix="-reprovision",
                target_revision="fixture-revision-4",
            ),
            lifecycle_event(
                bind.adapter_id, 13, "health", "healthy", suffix="-reprovision-update"
            ),
            lifecycle_event(bind.adapter_id, 14, "remove", "rehearsed", suffix="-reprovision"),
        ]
        seed = sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=0)
        full = seed.apply(events)
        assert isinstance(full, sim.KernelTransition)
        interrupted = seed.apply(events[:4])
        assert isinstance(interrupted, sim.KernelTransition)
        interrupted_with_remove = seed.apply(events[:4] + [events[6]])
        self.assertIsInstance(interrupted_with_remove, sim.SimulatorRefusal)
        self.assert_sim_refusal(
            recover_checkpoint(seed, interrupted).evaluate_lifecycle(
                bind.adapter_id, now_tick=4, health_freshness_ticks=10
            ),
            "update-interrupted",
        )
        rolled_back = seed.apply(events[:5])
        assert isinstance(rolled_back, sim.KernelTransition)
        self.assert_sim_refusal(
            recover_checkpoint(seed, rolled_back).evaluate_lifecycle(
                bind.adapter_id, now_tick=5, health_freshness_ticks=10
            ),
            "health-unknown",
        )
        child_c_ready = presence_test.ready_surface("surface-interrupted")
        child_c_interrupted = presence_test.step(
            child_c_ready,
            presence_test.event("update", "interrupted"),
        )
        child_c_rolled_back = presence_test.step(
            child_c_interrupted,
            presence_test.event("update", "rolled_back"),
        )
        child_c_unready = presence_test.readiness(child_c_rolled_back)
        self.assertIsInstance(child_c_unready, c.PresenceRefusal)
        self.assertEqual(child_c_unready.reason, "health-unknown")
        child_c_post_rollback_health = presence_test.step(
            child_c_rolled_back,
            presence_test.event("health", "healthy"),
        )
        self.assertIsInstance(
            presence_test.readiness(child_c_post_rollback_health),
            c.PresenceReadiness,
        )
        rolled_back_refreshed = seed.apply(events[:7])
        assert isinstance(rolled_back_refreshed, sim.KernelTransition)
        self.assertIsInstance(
            recover_checkpoint(seed, rolled_back_refreshed).evaluate_lifecycle(
                bind.adapter_id, now_tick=7, health_freshness_ticks=10
            ),
            sim.LifecycleReadiness,
        )
        removed_transition = seed.apply(events[:8])
        assert isinstance(removed_transition, sim.KernelTransition)
        self.assert_sim_refusal(
            recover_checkpoint(seed, removed_transition).evaluate_lifecycle(
                bind.adapter_id, now_tick=8, health_freshness_ticks=10
            ),
            "lifecycle-not-present",
        )
        resurrect = recover_checkpoint(seed, removed_transition).apply(
            (
                lifecycle_event(
                    bind.adapter_id, 9, "health", "healthy", suffix="-resurrect"
                ),
            )
        )
        self.assertIsInstance(resurrect, sim.SimulatorRefusal)
        reprovisioned_only = seed.apply(events[:9])
        assert isinstance(reprovisioned_only, sim.KernelTransition)
        self.assert_sim_refusal(
            recover_checkpoint(seed, reprovisioned_only).evaluate_lifecycle(
                bind.adapter_id, now_tick=9, health_freshness_ticks=10
            ),
            "health-unknown",
        )
        for index, event_value in enumerate(events):
            with self.subTest(crash_at=event_value.event_id):
                crash_events = list(events)
                crash_events[index] = dataclasses.replace(event_value, crash_point="after")
                crashed = seed.apply(crash_events)
                assert isinstance(crashed, sim.KernelTransition)
                replayed = recover_checkpoint(seed, crashed).apply(events)
                assert isinstance(replayed, sim.KernelTransition)
                self.assertEqual(replayed.durable_state, full.durable_state)
                self.assertEqual(replayed.transcript, full.transcript)
        ready_kernel = recover_checkpoint(seed, full)
        self.assertIsInstance(
            ready_kernel.evaluate_lifecycle(bind.adapter_id, now_tick=14, health_freshness_ticks=10),
            sim.LifecycleReadiness,
        )
        self.assert_sim_refusal(
            ready_kernel.evaluate_lifecycle(
                bind.adapter_id, now_tick=13, health_freshness_ticks=10
            ),
            "logical-time-regression",
        )

    def test_provider_network_and_verification_outages_fail_closed_then_recover(self) -> None:
        bind = binding("outage-device")
        sensor = sim.IoTSensorAdapter(bind, NOW_Z)
        for state_change, reason in (
            ({"provider": "outage"}, "provider-outage"),
            ({"network": "outage"}, "network-outage"),
        ):
            with self.subTest(sensor_outage=reason):
                self.assert_sim_refusal(
                    sensor.read(
                        read_plan(bind),
                        durable_state=sim.device_state_bytes(bind, **state_change),
                    ),
                    reason,
                )
        kernel = sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=0)
        ready = kernel.apply(five_leg_events(bind.adapter_id))
        assert isinstance(ready, sim.KernelTransition)
        current = recover_checkpoint(kernel, ready)
        for index, branch in enumerate(("provider", "network", "verification"), start=10):
            outage = current.apply(
                (
                    sim.SimulationEvent(
                        f"outage-{branch}", index, bind.adapter_id, "outage", {"branch": branch}
                    ),
                )
            )
            assert isinstance(outage, sim.KernelTransition)
            unavailable = recover_checkpoint(kernel, outage)
            self.assert_sim_refusal(
                unavailable.evaluate_lifecycle(
                    bind.adapter_id, now_tick=index, health_freshness_ticks=20
                ),
                "operational-outage",
            )
            recovered = unavailable.apply(
                (
                    sim.SimulationEvent(
                        f"recovery-{branch}", index + 1, bind.adapter_id, "recovery", {"branch": branch}
                    ),
                )
            )
            assert isinstance(recovered, sim.KernelTransition)
            current = recover_checkpoint(kernel, recovered)
            self.assertIsInstance(
                current.evaluate_lifecycle(
                    bind.adapter_id, now_tick=index + 1, health_freshness_ticks=20
                ),
                sim.LifecycleReadiness,
            )

        forged = json.loads(ready.durable_state)
        forged["adapters"][bind.adapter_id]["availability"]["provider"] = False
        with self.assertRaises(ValueError):
            recover_checkpoint(kernel, ready, sim.canonical_bytes(forged))

    def test_real_t3219_discovery_and_planning_choose_only_declared_fixture_provider(self) -> None:
        record = engine_test._visible_record(
            "fixture-actuator",
            capability="fixture-effect",
            contract="org060-fixture-actuator",
            schema="org060-fixture-actuator-v1",
        )
        projection = engine_test._projection(record)
        health = {"fixture-effect": engine_test._healthy("fixture-effect")}
        evidence = engine_test._snapshot()
        search = engine_test._parsed_request(
            payload=engine_test._search_payload(capability_ids=["fixture-effect"])
        )
        searched = engine.handle_request(
            engine_test._auth(), search, projection, health, evidence, engine_test.NOW
        )
        self.assertIsInstance(searched, cn.NavigatorResult, searched)
        candidate = searched.payload["candidates"][0]  # type: ignore[union-attr,index]
        self.assertEqual(candidate["provider_component_id"], "fixture-actuator")
        describe = engine_test._parsed_request(
            "describe", {"capability_id": "fixture-effect", "requested_detail": "full"}
        )
        described = engine.handle_request(
            engine_test._auth(), describe, projection, health, evidence, engine_test.NOW
        )
        self.assertIsInstance(described, cn.NavigatorResult)
        self.assertEqual(described.payload["provider_component_id"], "fixture-actuator")  # type: ignore[union-attr]
        engine_plan = engine_test._parsed_request(
            "plan",
            {
                "capability_id": "fixture-effect",
                "typed_input": {"set": "on"},
                "bounded_purpose": "fixture conformance",
            },
        )
        refused = engine.handle_request(
            engine_test._auth(), engine_plan, projection, health, evidence, engine_test.NOW
        )
        self.assertEqual(refused.code, engine.EngineRefusalCode.MISSING_EFFECT_CONTRACT_AUTHORITY)  # type: ignore[union-attr]
        request = b.make_actuation_request(
            {
                "schema_version": b.SCHEMA_VERSION,
                "request_id": "request:fixture-effect",
                "capability_id": "fixture-effect",
                "target_ref": "target:fixture",
                "effect": {"set": "on"},
                "purpose": "fixture conformance",
                "preconditions": ({"state": "off"},),
                "expected_result": {"state": "on"},
                "deadline": "2026-08-26T12:05:00Z",
                "idempotency_key": "idempotency:fixture",
                "cancellation_ref": "cancel:fixture",
            }
        )
        assert isinstance(request, b.ActuationRequest)
        auth_context = b.make_authenticated_actuation_context(
            engine_test._auth(),
            native_authority_ref="native-authority:fixture",
            approval_evidence_ref="approval:fixture",
        )
        assert isinstance(auth_context, b.AuthenticatedActuationContext)
        target = b.make_target_state(
            target_ref="target:fixture",
            provider=projection.capabilities[0],
            projection=projection,
            observed_at="2026-08-26T12:00:00Z",
            state_digest=SHA_A,
            current_preconditions=({"state": "off"},),
        )
        child_b_plan = b.plan_actuation(
            request,
            projection=projection,
            target_states=(target,),
            context=auth_context,
            now=engine_test.NOW,
        )
        self.assertIsInstance(child_b_plan, b.ActuationPlan, child_b_plan)
        self.assertEqual(
            child_b_plan.data["capability_binding_digest"],  # type: ignore[union-attr]
            b.provider_binding_digest(projection.capabilities[0], projection),
        )
        self.assertEqual(target.provider.provider_component_id, "fixture-actuator")
        bind = binding("fixture-actuator")
        authority = sim._bind_server_actuator_authorization(
            adapter_id=bind.adapter_id,
            approved_plan=child_b_plan,  # type: ignore[arg-type]
            context=auth_context,
            provider=projection.capabilities[0],
            projection=projection,
        )
        self.assertIsInstance(authority, sim._ActuatorAuthorizationAnchor, authority)
        adapter = sim.ReversibleIoTActuatorAdapter(
            bind,
            sim.outcome_script_bytes(),
            authority,  # type: ignore[arg-type]
            sim.actuator_state_bytes(
                authority,  # type: ignore[arg-type]
                target_state_digest=SHA_A,
                current_preconditions=({"state": "off"},),
                evaluated_at="2026-08-26T12:00:00Z",
            ),
        )
        self.assertEqual(
            adapter.dispatch(child_b_plan, context=auth_context).state,  # type: ignore[arg-type]
            "provider_accepted",
        )
        component_forge = engine_test._projection(
            engine_test._visible_record(
                "forged-actuator",
                capability="fixture-effect",
                contract="org060-fixture-actuator",
                schema="org060-fixture-actuator-v1",
            )
        )
        revision_provider = dataclasses.replace(
            projection.capabilities[0],
            source_identity=dataclasses.replace(
                projection.capabilities[0].source_identity,
                locked_revision="revision:mutually-forged",
            ),
        )
        revision_forge = engine_test._resign_projection(
            dataclasses.replace(projection, capabilities=(revision_provider,))
        )
        for forged_projection in (component_forge, revision_forge):
            forged_target = b.make_target_state(
                target_ref="target:fixture",
                provider=forged_projection.capabilities[0],
                projection=forged_projection,
                observed_at="2026-08-26T12:00:00Z",
                state_digest=SHA_A,
                current_preconditions=({"state": "off"},),
            )
            mutually_forged = b.plan_actuation(
                request,
                projection=forged_projection,
                target_states=(forged_target,),
                context=auth_context,
                now=engine_test.NOW,
            )
            self.assertIsInstance(mutually_forged, b.ActuationPlan, mutually_forged)
            self.assertEqual(
                adapter.dispatch(mutually_forged, context=auth_context).state,  # type: ignore[arg-type]
                "provider_denied",
            )
            mutually_forged_adapter = dataclasses.replace(
                adapter,
                actuator_state=sim.actuator_state_bytes(
                    authority,  # type: ignore[arg-type]
                    target_state_digest=SHA_A,
                    current_preconditions=({"state": "off"},),
                    evaluated_at="2026-08-26T12:00:00Z",
                    provider_component_id=forged_projection.capabilities[
                        0
                    ].provider_component_id,
                    provider_revision=forged_projection.capabilities[
                        0
                    ].source_identity.locked_revision,
                ),
            )
            self.assertEqual(
                mutually_forged_adapter.dispatch(
                    mutually_forged, context=auth_context  # type: ignore[arg-type]
                ).state,
                "provider_denied",
            )
        fallback_record = engine_test._visible_record(
            "undeclared-fallback",
            capability="fixture-effect",
            contract="other-contract",
            schema="other-schema",
        )
        ambiguous = engine_test._projection(record, fallback_record)
        refused_search = engine.handle_request(
            engine_test._auth(), search, ambiguous, health, evidence, engine_test.NOW
        )
        self.assertEqual(refused_search.code, engine.EngineRefusalCode.AMBIGUOUS_PROVIDER)  # type: ignore[union-attr]
        fallback_request = engine_test._parsed_request(
            payload=engine_test._search_payload(
                equivalence_classes=["equiv-alpha"],
                trust_classes=["trust-local"],
                require_declared_equivalence=True,
            )
        )
        fallback_refused = engine.handle_request(
            engine_test._auth(),
            fallback_request,
            projection,
            health,
            evidence,
            engine_test.NOW,
        )
        self.assertEqual(
            fallback_refused.code,  # type: ignore[union-attr]
            engine.EngineRefusalCode.FALLBACK_SELECTION_AUTHORITY_UNAVAILABLE,
        )

    def test_fixture_only_marker_cannot_be_bypassed_or_claim_live_org061_evidence(self) -> None:
        receipt = sim._fixture_receipt(
            receipt_id="receipt:fixture",
            adapter_id="fixture",
            tick=1,
            operation="test",
            payload={"bounded": True},
        )
        self.assertEqual(receipt.evidence_class, sim.FIXTURE_EVIDENCE_CLASS)
        with self.assertRaises(TypeError):
            sim.FixtureReceipt(
                "receipt:forged",
                "fixture",
                1,
                "test",
                None,
                SHA_A,
                evidence_class="live",  # type: ignore[call-arg]
            )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            receipt.evidence_class = "live"  # type: ignore[misc]
        bind = binding("fixture-marker")
        kernel = sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=0)
        for payload in (
            {"evidence_class": "live"},
            {"nested": {"live_evidence_class": "org-stage"}},
            {"nested": [{"org061_evidence": True}]},
            {"liveEvidenceClass": "org-stage"},
            {"nestedCredential": {"accessToken": "secret"}},
        ):
            self.assertIsInstance(
                kernel.apply((sim.SimulationEvent("forged", 1, bind.adapter_id, "record", payload),)),
                sim.SimulatorRefusal,
            )
        lifecycle_with_extra = lifecycle_event(
            bind.adapter_id, 1, "install", "verified"
        )
        lifecycle_with_extra = dataclasses.replace(
            lifecycle_with_extra,
            payload={**lifecycle_with_extra.payload, "callerAuthority": "forged"},
        )
        self.assert_sim_refusal(
            kernel.apply((lifecycle_with_extra,)), "event-schema-invalid"
        )
        with self.assertRaises(ValueError):
            sim.DeterministicKernel.create(
                adapter_bindings=(bind, dataclasses.replace(bind)), initial_tick=0
            )
        recorded = kernel.apply(
            (sim.SimulationEvent("recorded", 1, bind.adapter_id, "record", {}),)
        )
        assert isinstance(recorded, sim.KernelTransition)
        for mutate in (
            lambda state: state.update(schema_version="other"),
            lambda state: state["adapters"][bind.adapter_id]["binding"].update(
                credentials={"token": "secret"}
            ),
            lambda state: state["adapters"][bind.adapter_id]["binding"].update(
                calibration_device_id="device:swapped"
            ),
            lambda state: state["transcript"][0].update(
                liveEvidenceClass="org-stage"
            ),
            lambda state: state["transcript"][0].update(evidence_class="live"),
            lambda state: state["transcript"][0].update(logical_tick=2),
        ):
            hostile_state = json.loads(recorded.durable_state)
            mutate(hostile_state)
            with self.assertRaises(ValueError):
                recover_checkpoint(kernel, recorded, sim.canonical_bytes(hostile_state))
        lifecycle_state = kernel.apply(five_leg_events(bind.adapter_id))
        assert isinstance(lifecycle_state, sim.KernelTransition)
        hostile_order = json.loads(lifecycle_state.durable_state)
        hostile_order["adapters"][bind.adapter_id]["lifecycle"]["legs"]["health"][
            "logical_tick"
        ] = 1
        with self.assertRaises(ValueError):
            recover_checkpoint(kernel, lifecycle_state, sim.canonical_bytes(hostile_order))
        for cls in (
            sim.FixedMicrophoneAdapter,
            sim.LocalCameraAdapter,
            sim.MobileDroneAdapter,
            sim.IoTSensorAdapter,
            sim.ReversibleIoTActuatorAdapter,
            sim.FixtureDisplayAdapter,
            sim.FixtureWearableAdapter,
            sim.FixtureDesktopPresenceAdapter,
            sim.FixtureBadgePresenceAdapter,
        ):
            self.assertEqual(cls.__dataclass_fields__["fixture_only"].default, True)
            self.assertFalse(cls.__dataclass_fields__["fixture_only"].init)
        for cls in (
            sim.FixedMicrophoneAdapter,
            sim.LocalCameraAdapter,
            sim.MobileDroneAdapter,
            sim.IoTSensorAdapter,
        ):
            self.assertIn(sim.NativeSensorAdapter, cls.__mro__)
            signature = inspect.signature(cls.read)
            self.assertEqual(tuple(signature.parameters), ("self", "plan", "durable_state"))
            self.assertEqual(
                signature.parameters["durable_state"].kind,
                inspect.Parameter.KEYWORD_ONLY,
            )
        for cls in (sim.MobileDroneAdapter, sim.ReversibleIoTActuatorAdapter):
            self.assertIn(b.NativeActuatorAdapter, cls.__mro__)
            for method_name, argument_name in (
                ("dispatch", "plan"),
                ("cancel", "receipt"),
                ("rollback", "receipt"),
            ):
                signature = inspect.signature(getattr(cls, method_name))
                self.assertEqual(
                    tuple(signature.parameters),
                    ("self", argument_name, "context"),
                )
                self.assertEqual(
                    signature.parameters["context"].kind,
                    inspect.Parameter.KEYWORD_ONLY,
                )
        for cls in (
            sim.FixtureDisplayAdapter,
            sim.FixtureWearableAdapter,
            sim.FixtureDesktopPresenceAdapter,
            sim.FixtureBadgePresenceAdapter,
        ):
            self.assertIn(c.BridgePresenceAdapter, cls.__mro__)
            render_signature = inspect.signature(cls.render)
            self.assertEqual(
                tuple(render_signature.parameters),
                ("self", "projection_json", "surface_id", "indicators"),
            )
            self.assertEqual(
                render_signature.parameters["surface_id"].kind,
                inspect.Parameter.KEYWORD_ONLY,
            )
            interaction_signature = inspect.signature(cls.read_interaction)
            self.assertEqual(
                tuple(interaction_signature.parameters), ("self", "surface_id")
            )
            self.assertEqual(
                interaction_signature.parameters["surface_id"].kind,
                inspect.Parameter.KEYWORD_ONLY,
            )
        actuator = actuator_adapter(binding("fixture-evidence-actuator"))
        plan, context, _projection = owner_actuation_objects()
        native_results = (
            actuator.dispatch(plan, context=context),
            actuator.cancel({}, context=context),
            actuator.rollback({}, context=context),
        )
        for native_result in native_results:
            self.assertEqual(
                native_result.fixture_receipt.evidence_class,
                sim.FIXTURE_EVIDENCE_CLASS,
            )

    def test_no_simulator_path_performs_io_or_accepts_caller_identity_authority_lookalikes(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text())
        forbidden_imports = {"os", "pathlib", "random", "socket", "subprocess", "time", "uuid"}
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertFalse(forbidden_imports & imported)
        calls = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))
        }
        self.assertFalse({"open", "read_text", "read_bytes", "write_text", "write_bytes", "now", "utcnow"} & calls)
        bind = binding("no-io")
        kernel = sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=0)
        with (
            mock.patch("builtins.open", side_effect=AssertionError("open called")),
            mock.patch.object(socket, "socket", side_effect=AssertionError("socket called")),
            mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess called")),
            mock.patch.object(time, "time", side_effect=AssertionError("clock called")),
        ):
            transition = kernel.apply(five_leg_events(bind.adapter_id))
            self.assertIsInstance(transition, sim.KernelTransition)
            sensor = sim.FixedMicrophoneAdapter(bind, NOW_Z)
            self.assertIsInstance(
                sensor.read(read_plan(bind), durable_state=sim.device_state_bytes(bind)),
                sim.SensorReadResult,
            )
        lookalike = kernel.apply(
            (
                sim.SimulationEvent(
                    "lookalike",
                    1,
                    bind.adapter_id,
                    "record",
                    {"nested": [{"caller_authority": "forged"}]},
                ),
            )
        )
        self.assert_sim_refusal(lookalike, "event-schema-invalid")
        camel_lookalike = kernel.apply(
            (
                sim.SimulationEvent(
                    "lookalike-camel",
                    1,
                    bind.adapter_id,
                    "record",
                    {"callerAuthority": "forged"},
                ),
            )
        )
        self.assertIsInstance(camel_lookalike, sim.SimulatorRefusal)
        wrong_schema = json.loads(sim.device_state_bytes(bind))
        wrong_schema["schema_version"] = "other"
        self.assert_sim_refusal(
            sim.FixedMicrophoneAdapter(bind, NOW_Z).read(
                read_plan(bind), durable_state=sim.canonical_bytes(wrong_schema)
            ),
            "device-state-schema-invalid",
        )
        credential_extra = json.loads(sim.device_state_bytes(bind))
        credential_extra["credentials"] = {"token": "secret"}
        self.assert_sim_refusal(
            sim.FixedMicrophoneAdapter(bind, NOW_Z).read(
                read_plan(bind), durable_state=sim.canonical_bytes(credential_extra)
            ),
            "device-state-schema-invalid",
        )

        class FakeContext:
            authenticated_context = b_test._auth()
            native_authority_ref = "native-authority:fake"
            approval_evidence_ref = "approval:fake"

        actuator = actuator_adapter(bind)
        plan = owner_actuation_objects()[0]
        self.assertEqual(actuator.dispatch(plan, context=FakeContext()).state, "provider_denied")  # type: ignore[arg-type]
        FakeAnchor = dataclasses.make_dataclass("ActuatorAuthorizationAnchor", [("adapter_id", str)])
        with self.assertRaises(ValueError):
            dataclasses.replace(
                actuator,
                actuator_binding=FakeAnchor(bind.adapter_id),  # type: ignore[arg-type]
            )
        checkpoint = kernel.apply(five_leg_events(bind.adapter_id))
        assert isinstance(checkpoint, sim.KernelTransition)
        FakeRecoveryAnchor = dataclasses.make_dataclass(
            "KernelRecoveryAnchor",
            [("checkpoint_bytes", bytes)],
        )
        with self.assertRaises(ValueError):
            sim.DeterministicKernel.recover(
                checkpoint.durable_state,
                anchor=FakeRecoveryAnchor(checkpoint.durable_state),  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            sim.DeterministicKernel.recover(
                checkpoint.durable_state,
                anchor={"checkpoint_bytes": checkpoint.durable_state},  # type: ignore[arg-type]
            )
        fake_projection = dataclasses.make_dataclass(
            "PresenceProjection", [("data", Mapping[str, object])]
        )({})
        with tempfile.TemporaryDirectory() as root:
            world = presence_test.World(Path(root))
            state = presence_test.ready_surface()
            refused = c.render_presence(
                fake_projection,  # type: ignore[arg-type]
                surface=state,
                readiness=presence_test.ready(state),
                adapter=sim.FixtureDisplayAdapter("adapter:fake", presence_test.ago(1)),
                now=presence_test.NOW,
                freshness=presence_test.FRESH,
            )
            self.assertIsInstance(refused, c.PresenceRefusal)

    def test_every_positive_conformance_case_has_named_adversarial_counterpart(self) -> None:
        exact_positive_names = {
            name for name in dir(type(self)) if name.startswith("test_") and name != self._testMethodName  # T-5690: not frame state
        }
        self.assertEqual(set(sim.CONFORMANCE_MANIFEST), exact_positive_names)
        declared_order = tuple(
            name for name in type(self).__dict__ if name.startswith("test_")
        )
        self.assertEqual(
            declared_order,
            (*sim.CONFORMANCE_MANIFEST, self._testMethodName),
        )
        threat_names: set[str] = set()
        method_names: set[str] = set()
        for positive, (threat, method_name) in sim.CONFORMANCE_MANIFEST.items():
            with self.subTest(positive=positive, threat=threat):
                self.assertTrue(threat.startswith("threat:"))
                self.assertNotIn(threat, threat_names)
                self.assertNotIn(method_name, method_names)
                threat_names.add(threat)
                method_names.add(method_name)
                method = getattr(self, method_name)
                self.assertTrue(callable(method))
                outcome = method()
                self.assertIsInstance(outcome, sim.SimulatorRefusal)

    # These are executable adversarial counterparts, not auto-collected tests.
    def _threat_01(self) -> sim.SimulatorRefusal:
        return sim._decode(b"{}")  # type: ignore[return-value]

    def _threat_02(self) -> sim.SimulatorRefusal:
        bind = binding("t02")
        return sim.FixedMicrophoneAdapter(bind, NOW_Z).read(
            read_plan(bind, expected_location_ref="wrong"), durable_state=sim.device_state_bytes(bind)
        )  # type: ignore[return-value]

    def _threat_03(self) -> sim.SimulatorRefusal:
        bind = binding("t03")
        return sim.FixedMicrophoneAdapter(bind, NOW_Z).read(
            read_plan(bind, logical_tick=101), durable_state=sim.device_state_bytes(bind)
        )  # type: ignore[return-value]

    def _threat_04(self) -> sim.SimulatorRefusal:
        bind = binding("t04")
        return sim.LocalCameraAdapter(bind, NOW_Z).read(
            read_plan(bind), durable_state=sim.device_state_bytes(bind, occluded=True)
        )  # type: ignore[return-value]

    def _threat_05(self) -> sim.SimulatorRefusal:
        return sim._decode(b"not-json\n")  # type: ignore[return-value]

    def _threat_06(self) -> sim.SimulatorRefusal:
        bind = binding("t06")
        adapter = drone_adapter(bind)
        return adapter.move(
            sim.DroneMotionRequest("unsafe", 1, 1, (9, 9, 9), 1, 1, True),
            durable_state=sim.device_state_bytes(bind),
        )  # type: ignore[return-value]

    def _threat_07(self) -> sim.SimulatorRefusal:
        bind = binding("t07")
        adapter = drone_adapter(bind)
        return adapter.move(
            sim.DroneMotionRequest("denied", 1, 1, (0, 0, 1), 2, 1, False),
            durable_state=sim.device_state_bytes(bind),
        )  # type: ignore[return-value]

    def _threat_08(self) -> sim.SimulatorRefusal:
        return sim._decode("{}\n")  # type: ignore[arg-type,return-value]

    def _threat_09(self) -> sim.SimulatorRefusal:
        bind = binding("t09")
        kernel = sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=0)
        return kernel.apply((sim.SimulationEvent("past", -1, bind.adapter_id, "record", {}),))  # type: ignore[return-value]

    def _threat_10(self) -> sim.SimulatorRefusal:
        bind = binding("t10")
        kernel = sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=0)
        return kernel.evaluate_lifecycle(bind.adapter_id, now_tick=0, health_freshness_ticks=1)  # type: ignore[return-value]

    def _threat_11(self) -> sim.SimulatorRefusal:
        bind = binding("t11")
        kernel = sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=0)
        return kernel.apply((sim.SimulationEvent("live", 1, bind.adapter_id, "record", {"evidence_class": "live"}),))  # type: ignore[return-value]

    def _threat_12(self) -> sim.SimulatorRefusal:
        bind = binding("t12")
        kernel = sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=0)
        return kernel.apply((sim.SimulationEvent("auth", 1, bind.adapter_id, "record", {"nested": {"caller_authority": "x"}}),))  # type: ignore[return-value]

    def _threat_13(self) -> sim.SimulatorRefusal:
        try:
            sim.DeterministicKernel.recover("{}", anchor=object())  # type: ignore[arg-type]
        except ValueError as exc:
            return sim.SimulatorRefusal("durable-bytes-required", str(exc))
        raise AssertionError("non-bytes recovery unexpectedly accepted")

    def _threat_14(self) -> sim.SimulatorRefusal:
        bind = binding("t14")
        kernel = sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=0)
        partial = kernel.apply(five_leg_events(bind.adapter_id)[:-1])
        assert isinstance(partial, sim.KernelTransition)
        return recover_checkpoint(kernel, partial).evaluate_lifecycle(
            bind.adapter_id, now_tick=4, health_freshness_ticks=10
        )  # type: ignore[return-value]

    def _threat_15(self) -> sim.SimulatorRefusal:
        bind = binding("t15")
        return sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=2).apply(
            (sim.SimulationEvent("regress", 1, bind.adapter_id, "record", {}),)
        )  # type: ignore[return-value]

    def _threat_16(self) -> sim.SimulatorRefusal:
        bind = binding("t16")
        kernel = sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=0)
        ready = kernel.apply(five_leg_events(bind.adapter_id))
        assert isinstance(ready, sim.KernelTransition)
        outage = recover_checkpoint(kernel, ready).apply(
            (sim.SimulationEvent("outage", 10, bind.adapter_id, "outage", {"branch": "provider"}),)
        )
        assert isinstance(outage, sim.KernelTransition)
        return recover_checkpoint(kernel, outage).evaluate_lifecycle(
            bind.adapter_id, now_tick=10, health_freshness_ticks=20
        )  # type: ignore[return-value]

    def _threat_17(self) -> sim.SimulatorRefusal:
        bind = binding("t17")
        return sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=0).apply(
            (sim.SimulationEvent("unknown", 1, "not-declared", "record", {}),)
        )  # type: ignore[return-value]

    def _threat_18(self) -> sim.SimulatorRefusal:
        bind = binding("t18")
        return sim.DeterministicKernel.create(adapter_bindings=(bind,), initial_tick=0).apply(
            (sim.SimulationEvent("claim", 1, bind.adapter_id, "record", {"nested": {"org061_evidence": True}}),)
        )  # type: ignore[return-value]

    def _threat_19(self) -> sim.SimulatorRefusal:
        bind = binding("t19")
        adapter = sim.FixedMicrophoneAdapter(bind, NOW_Z)
        fake = dataclasses.make_dataclass("NavigatorProjection", [("capabilities", tuple)])(())
        return sim.normalize_sensor_read(
            adapter,
            read_plan(bind),
            durable_state=sim.device_state_bytes(bind),
            projection=fake,
            now=NOW,
            freshness=b.FreshnessWindow(60),
        )  # type: ignore[arg-type,return-value]


if __name__ == "__main__":
    unittest.main()
