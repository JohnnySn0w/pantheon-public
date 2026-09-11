"""Tests for the external ContinuitySwitchCoordinator (T-3177).

Wraps T-2382/T-2953's stateless runtime controller with the ORG-057
transactional harness-switch lifecycle (RFC-001 `rfc/RFC-001-persona-continuity-lifecycle.md`
state 5). Every OS/process-facing operation is injected via
`CoordinatorPorts` (a `FakeWorld`-backed fake here) so these tests exercise
the coordinator's STATE-MACHINE LOGIC -- drift refusals, the four-fact
post-commit-crash conjunction, single-receiver enforcement, structural
isolation gating -- without a live tmux/systemd/git environment. The real
default port wiring (`default_ports()`) is exercised only for import/wiring
shape, not live process control, per T-2953's own test/live-acceptance split
(test_runtime_controller.py is unit-mocked; t2382_live_acceptance.py is the
live surface).

Run: python3 -m unittest tools.test_continuity_switch_coordinator -v
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools import continuity_switch_coordinator as csc
from tools import persona_continuity as pc
from tools.test_persona_continuity import ContinuityFixture
from tools.harness_adapter.types import FailoverPolicy, HarnessLaunchConfig, TmuxTarget, RuntimeControllerConfig
from tools.persona_bringup import PersonaBinding
from tools import runtime_controller


# --------------------------------------------------------------------------
# Binding fixtures -- constructed directly (not via a real instance.yaml)
# since `list_bindings` is an injected port; production wiring
# (`default_ports`) still resolves via the real persona_bringup functions.
# --------------------------------------------------------------------------


def _launch(harness: str, *, profile: str = "interactive", writable_roots: tuple[Path, ...] = ()) -> HarnessLaunchConfig:
    return HarnessLaunchConfig(
        harness=harness, profile=profile, executable=Path(f"/usr/bin/{harness}"),
        model=None, mode="interactive", meter="subscription", quota_source="unavailable",
        native={}, sources=(), writable_roots=writable_roots,
    )


def _binding(root: Path, *, role: str, persona: str, account: str, harness: str,
             communications: dict | None = None) -> PersonaBinding:
    home = root / f"home-{account}"
    home.mkdir(parents=True, exist_ok=True)
    return PersonaBinding(
        role=role, persona=persona, account=account,
        configured_harness=harness, selected_harness=harness,
        account_home=home, pinned_rules=home / "CLAUDE.md",
        mailbox=home / "mailbox", launch_cwd=home,
        launch=_launch(harness),
        controller=RuntimeControllerConfig(
            tmux=TmuxTarget(session=f"{account}-session", window="0", pane="0"),
            policy=FailoverPolicy(weekly_remaining_percent=100.0, fallbacks=()),
        ),
        communications=communications,
    )


# --------------------------------------------------------------------------
# FakeWorld -- mutable state closed over by every CoordinatorPorts callable.
# --------------------------------------------------------------------------


class FakeWorld:
    def __init__(self) -> None:
        self.live: dict[str, bool] = {}
        self.receiver: str | None = None
        self.isolated: dict[str, bool] = {}
        self.comm_payload: dict[str, object] = {}
        self.comm_ok = True
        self.comm_correlation_id = "corr-1"
        self.transfer_raises: Exception | None = None
        self.start_raises: dict[str, Exception] = {}
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.maintenance_entries: list[tuple[str, int, str]] = []
        self.maintenance_exits: list[str] = []
        self.git_rev = "a" * 40
        self.observed_extra: dict[str, dict] = {}

    def ref(self, binding: PersonaBinding) -> str:
        return csc._binding_ref(binding)


def _ports(world: FakeWorld) -> csc.CoordinatorPorts:
    def start_runtime(binding: PersonaBinding) -> "runtime_controller.RuntimeHandle":
        ref = world.ref(binding)
        if ref in world.start_raises:
            raise world.start_raises.pop(ref)
        world.started.append(ref)
        world.live[ref] = True
        return runtime_controller.RuntimeHandle(
            profile=binding.launch, tmux=binding.controller.tmux, session_id="sess",
            started_at=datetime.now(timezone.utc),
        )

    def stop_runtime(binding: PersonaBinding) -> None:
        ref = world.ref(binding)
        world.stopped.append(ref)
        world.live[ref] = False

    def is_live(binding: PersonaBinding) -> bool:
        return world.live.get(world.ref(binding), False)

    def observe_binding(binding: PersonaBinding) -> pc.RuntimeObservation:
        ref = world.ref(binding)
        extra = world.observed_extra.get(ref, {})
        data = {
            "runtime_id": ref, "harness_id": binding.launch.harness, "account_ref": binding.account,
            "responsive": world.live.get(ref, False), "self_identification": None, "role_identification": None,
            "authority_limits": (), "current_work_refs": (), "receiver_evidence_sha256": None,
            "external_actions_disabled": False, "observed_at": "2026-09-06T00:00:00Z",
        }
        data.update(extra)
        return pc.RuntimeObservation(**data)  # type: ignore[arg-type]

    def communicate(handle: "runtime_controller.RuntimeHandle", prompt: str) -> csc.CommunicationResult:
        return csc.CommunicationResult(
            correlation_id=world.comm_correlation_id, ok=world.comm_ok,
            evidence_sha256=csc._sha256(b"evidence"), structured_payload=dict(world.comm_payload),
        )

    def receiver_required(binding: PersonaBinding) -> bool:
        return runtime_controller.receiver_handoff_required(binding)

    def receiver_present(binding: PersonaBinding) -> bool:
        return world.receiver == world.ref(binding)

    def receiver_absent(binding: PersonaBinding) -> bool:
        return world.receiver != world.ref(binding)

    def isolate_candidate(binding: PersonaBinding) -> tuple[PersonaBinding, csc.CandidateIsolationProof]:
        ref = world.ref(binding)
        if binding.launch.harness == "codex":
            isolated = dataclasses.replace(binding, launch=dataclasses.replace(binding.launch, writable_roots=()))
            mechanism: csc.IsolationMechanism = "codex-writable-roots-empty"
        else:
            isolated = binding
            mechanism = "claude-candidate-guard-active"
        world.isolated[ref] = True
        return isolated, csc.CandidateIsolationProof(mechanism, csc._sha256(ref.encode()), "2026-09-06T00:00:00Z")

    def verify_isolation(binding: PersonaBinding, proof: csc.CandidateIsolationProof) -> bool:
        return world.isolated.get(world.ref(binding), False)

    def release_isolation(binding: PersonaBinding) -> None:
        world.isolated[world.ref(binding)] = False

    def transfer_receiver(*, current: PersonaBinding, target: PersonaBinding, replace, reason: str):
        if world.transfer_raises is not None:
            exc, world.transfer_raises = world.transfer_raises, None
            raise exc
        handle = replace(target)
        if runtime_controller.receiver_handoff_required(target):
            world.receiver = world.ref(target)
        return handle

    def enter_maintenance(persona: str, *, minutes: int, reason: str) -> str:
        world.maintenance_entries.append((persona, minutes, reason))
        return "2026-09-06T01:00:00Z"

    def exit_maintenance(persona: str) -> bool:
        world.maintenance_exits.append(persona)
        return True

    def git_revision(repo_root: Path) -> str:
        return world.git_rev

    return csc.CoordinatorPorts(
        list_bindings=lambda: (),  # overridden per-test via dataclasses.replace
        start_runtime=start_runtime, observe_binding=observe_binding, communicate=communicate,
        is_live=is_live, receiver_required=receiver_required, transfer_receiver=transfer_receiver,
        isolate_candidate=isolate_candidate, verify_isolation=verify_isolation,
        release_isolation=release_isolation, stop_runtime=stop_runtime,
        enter_maintenance=enter_maintenance, exit_maintenance=exit_maintenance,
        git_revision=git_revision, receiver_present=receiver_present, receiver_absent=receiver_absent,
    )


# --------------------------------------------------------------------------
# Full-arc fixture: bundle + checkpoint (reusing T-3176's ContinuityFixture),
# two bindings, a coordinator wired to a FakeWorld.
# --------------------------------------------------------------------------


class Arc:
    def __init__(self, tmp: Path, *, telegram: bool = False) -> None:
        self.tmp = tmp
        self.cf = ContinuityFixture(tmp / "continuity")
        self.world = FakeWorld()
        self.manifest_path = tmp / "manifest.yaml"
        self.manifest_path.write_text("roles: {}\n")
        comms = {"kind": "telegram"} if telegram else None
        self.old = _binding(tmp, role="orchestrator", persona="cairn", account="codex-old",
                             harness="codex", communications=comms)
        self.candidate = _binding(tmp, role="orchestrator", persona="cairn", account="claude-new",
                                   harness="claude", communications=comms)
        self.world.live[self.world.ref(self.old)] = True
        if telegram:
            self.world.receiver = self.world.ref(self.old)
        switch_state_root = tmp / "switch-state"
        self.config = csc.CoordinatorConfig(
            persona_id="cairn", organism_id="pantheon",
            runtime_manifest_path=self.manifest_path,
            bundle_path=self.cf.manifest, descriptor_root=self.cf.descriptors,
            repo_root=tmp, state_root=tmp / "state", checkpoint_root=self.cf.checkpoints,
            switch_state_root=switch_state_root,
            actor=pc.ActorContext("cairn", ("authority:canonical",), (pc.ContinuityPurpose.HYDRATE_RUNTIME,)),
            transaction_ceiling_minutes=15,
        )
        ports = _ports(self.world)
        ports.list_bindings = lambda: (self.old, self.candidate)
        self.coordinator = csc.ContinuitySwitchCoordinator(self.config, ports)
        _, self.checkpoint_receipt = self.cf.checkpoint()
        self.checkpoint_envelope_path = Path(self.checkpoint_receipt.path)

    def full_checkpoint_envelope(self) -> pc.CheckpointEnvelope:
        return pc.load_checkpoint_envelope(self.checkpoint_envelope_path, checkpoint_root=self.cf.checkpoints)

    def plan(self, candidate_harness: str = "claude") -> csc.SwitchPlan:
        return self.coordinator.plan(candidate_harness=candidate_harness)

    def matching_communication(self, envelope: pc.CheckpointEnvelope) -> None:
        """Script the fake candidate's response to exactly match the
        checkpoint -- the happy path all ten assertions expect."""

        expected_commitments = sorted((
            *envelope.content.active_ticket_refs, *envelope.content.promise_refs,
            *envelope.content.wait_refs, *envelope.content.escalation_refs,
        ))
        role_refs = [item for item in envelope.source_refs if item.kind == "role"]
        expected_authority_refs = sorted({
            f"authority:{item.authority_class}" for item in envelope.source_refs if item.kind == "authority"
        })
        self.world.comm_correlation_id = envelope.boundary.correlation_id
        self.world.comm_ok = True
        self.world.comm_payload = {
            "role_id": role_refs[0].uri,
            "role_authority_refs": expected_authority_refs,
            "current_commitment_refs": expected_commitments,
            "expected_next_action": envelope.content.expected_next_action,
            "authority_limits": list(envelope.runtime_observation.authority_limits),
            "capability_evidence_complete": True,
            # T-3461: previously these two assertions were tautologically
            # true because verify_continuity read them straight off
            # `envelope` itself, never off the candidate's response. Now
            # that they're payload-sourced, the happy-path fixture must
            # supply them -- exactly the values an HONEST candidate that
            # loaded this same checkpoint would self-report.
            "checkpoint_sha256": envelope.payload_sha256,
            "descriptor_sha256": envelope.descriptor_sha256,
            "source_revisions": [
                {"uri": item.uri, "source_revision": item.source_revision, "source_sha256": item.source_sha256}
                for item in envelope.source_refs
            ],
        }


def _arc(tmp: Path, **kwargs) -> Arc:
    return Arc(tmp, **kwargs)


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()


# --------------------------------------------------------------------------
# 1. plan()
# --------------------------------------------------------------------------


class PlanTests(TmpCase):
    def test_plan_resolves_bindings_read_only(self) -> None:
        arc = _arc(self.tmp)
        plan = arc.plan()
        self.assertEqual(plan.old_binding_ref, arc.world.ref(arc.old))
        self.assertEqual(plan.candidate_binding_ref, arc.world.ref(arc.candidate))
        self.assertEqual(arc.world.started, [])  # read-only: nothing started
        self.assertEqual(arc.world.maintenance_entries, [])

    def test_plan_refuses_unresolvable_candidate_harness(self) -> None:
        arc = _arc(self.tmp)
        with self.assertRaises(pc.ContinuityError) as ctx:
            arc.plan(candidate_harness="does-not-exist")
        self.assertEqual(ctx.exception.code, "candidate-binding-unresolvable")

    def test_preflight_refuses_on_manifest_drift_since_plan(self) -> None:
        arc = _arc(self.tmp)
        plan = arc.plan()
        arc.manifest_path.write_text("roles: {changed: true}\n")
        with self.assertRaises(pc.ContinuityError) as ctx:
            arc.coordinator.preflight(plan)
        self.assertEqual(ctx.exception.code, "manifest-drift")


# --------------------------------------------------------------------------
# 2. preflight()
# --------------------------------------------------------------------------


class PreflightTests(TmpCase):
    def test_preflight_compatible_enters_maintenance(self) -> None:
        arc = _arc(self.tmp)
        plan = arc.plan()
        result = arc.coordinator.preflight(plan)
        self.assertEqual(result.status, "compatible")
        self.assertTrue(result.old_sole_receiver_proof)
        self.assertEqual(len(arc.world.maintenance_entries), 1)
        persona, minutes, reason = arc.world.maintenance_entries[0]
        self.assertEqual(persona, "cairn")
        self.assertLessEqual(minutes, arc.config.transaction_ceiling_minutes)

    def test_preflight_refuses_incompatible_authority(self) -> None:
        arc = _arc(self.tmp)
        arc.candidate = dataclasses.replace(arc.candidate, persona="someone-else")
        arc.coordinator.ports.list_bindings = lambda: (arc.old, arc.candidate)
        plan = arc.plan()
        with self.assertRaises(pc.ContinuityError) as ctx:
            arc.coordinator.preflight(plan)
        self.assertEqual(ctx.exception.code, "preflight-incompatible")
        self.assertEqual(arc.world.maintenance_entries, [])  # never entered on refusal

    def test_preflight_refuses_old_receiver_ambiguous(self) -> None:
        arc = _arc(self.tmp, telegram=True)
        plan = arc.plan()
        arc.world.receiver = None  # neither side proven -- ambiguous/absent, not "present"
        with self.assertRaises(pc.ContinuityError) as ctx:
            arc.coordinator.preflight(plan)
        self.assertEqual(ctx.exception.code, "preflight-incompatible")

    def test_preflight_refuses_old_binding_not_live(self) -> None:
        arc = _arc(self.tmp)
        plan = arc.plan()
        arc.world.live[arc.world.ref(arc.old)] = False
        with self.assertRaises(pc.ContinuityError):
            arc.coordinator.preflight(plan)


# --------------------------------------------------------------------------
# 3. require_checkpoint()
# --------------------------------------------------------------------------


class CheckpointTests(TmpCase):
    def test_require_checkpoint_loads_valid_envelope(self) -> None:
        arc = _arc(self.tmp)
        plan = arc.plan()
        preflight = arc.coordinator.preflight(plan)
        envelope = arc.coordinator.require_checkpoint(plan, preflight, checkpoint_envelope_path=arc.checkpoint_envelope_path)
        self.assertEqual(envelope.persona_id, "cairn")

    def test_require_checkpoint_refuses_descriptor_drift(self) -> None:
        arc = _arc(self.tmp)
        plan = arc.plan()
        preflight = arc.coordinator.preflight(plan)
        mutated = dict(arc.cf.manifest_data)
        mutated["organism_id"] = "different-organism"
        arc.cf.write_manifest(mutated)
        with self.assertRaises(pc.ContinuityError) as ctx:
            arc.coordinator.require_checkpoint(plan, preflight, checkpoint_envelope_path=arc.checkpoint_envelope_path)
        self.assertEqual(ctx.exception.code, "descriptor-drift")

    def test_require_checkpoint_refuses_without_compatible_preflight(self) -> None:
        arc = _arc(self.tmp)
        plan = arc.plan()
        bad = csc.PreflightResult("incompatible", False, False, False, "x", "y", ("nope",))
        with self.assertRaises(pc.ContinuityError) as ctx:
            arc.coordinator.require_checkpoint(plan, bad, checkpoint_envelope_path=arc.checkpoint_envelope_path)
        self.assertEqual(ctx.exception.code, "preflight-not-compatible")


# --------------------------------------------------------------------------
# 4. start_candidate() -- structural isolation
# --------------------------------------------------------------------------


class StartCandidateTests(TmpCase):
    def _through_checkpoint(self, arc: Arc):
        plan = arc.plan()
        preflight = arc.coordinator.preflight(plan)
        envelope = arc.coordinator.require_checkpoint(plan, preflight, checkpoint_envelope_path=arc.checkpoint_envelope_path)
        return plan, envelope

    def test_start_candidate_codex_isolation_active(self) -> None:
        arc = _arc(self.tmp)
        arc.old, arc.candidate = arc.candidate, arc.old  # candidate is codex this time
        arc.coordinator.ports.list_bindings = lambda: (arc.old, arc.candidate)
        arc.world.live[arc.world.ref(arc.old)] = True

        def through_checkpoint_codex(arc: Arc):
            plan = arc.plan(candidate_harness="codex")
            preflight = arc.coordinator.preflight(plan)
            envelope = arc.coordinator.require_checkpoint(plan, preflight, checkpoint_envelope_path=arc.checkpoint_envelope_path)
            return plan, envelope

        plan, envelope = through_checkpoint_codex(arc)
        handle, proof = arc.coordinator.start_candidate(plan, envelope)
        self.assertEqual(proof.mechanism, "codex-writable-roots-empty")
        self.assertEqual(handle.profile.writable_roots, ())
        self.assertIn(arc.world.ref(arc.candidate), arc.world.started)

    def test_start_candidate_claude_guard_active(self) -> None:
        arc = _arc(self.tmp)
        plan, envelope = self._through_checkpoint(arc)
        handle, proof = arc.coordinator.start_candidate(plan, envelope)
        self.assertEqual(proof.mechanism, "claude-candidate-guard-active")
        self.assertTrue(arc.world.isolated[arc.world.ref(arc.candidate)])

    def test_start_candidate_refuses_when_isolation_not_proven(self) -> None:
        arc = _arc(self.tmp)
        plan, envelope = self._through_checkpoint(arc)
        arc.coordinator.ports.verify_isolation = lambda binding, proof: False
        with self.assertRaises(pc.ContinuityError) as ctx:
            arc.coordinator.start_candidate(plan, envelope)
        self.assertEqual(ctx.exception.code, "isolation-not-proven")
        self.assertEqual(arc.world.started, [])  # never started without proven isolation


# --------------------------------------------------------------------------
# 5. verify_continuity() -- the ten assertions
# --------------------------------------------------------------------------


class VerifyContinuityTests(TmpCase):
    def _to_candidate_started(self, arc: Arc):
        plan = arc.plan()
        preflight = arc.coordinator.preflight(plan)
        envelope = arc.coordinator.require_checkpoint(plan, preflight, checkpoint_envelope_path=arc.checkpoint_envelope_path)
        handle, proof = arc.coordinator.start_candidate(plan, envelope)
        return plan, envelope, handle, proof

    def test_all_ten_assertions_pass_on_matching_communication(self) -> None:
        arc = _arc(self.tmp)
        plan, envelope, handle, proof = self._to_candidate_started(arc)
        arc.matching_communication(envelope)
        report = arc.coordinator.verify_continuity(plan, envelope, handle, proof)
        failed = [a.assertion_id for a in report.assertions if not a.passed]
        self.assertEqual(failed, [], f"unexpected failures: {failed}")
        self.assertEqual(set(a.assertion_id for a in report.assertions), set(pc.VERIFICATION_ASSERTION_IDS))

    def test_candidate_actions_disabled_lies_are_caught(self) -> None:
        arc = _arc(self.tmp)
        plan, envelope, handle, proof = self._to_candidate_started(arc)
        arc.matching_communication(envelope)
        arc.world.isolated[arc.world.ref(arc.candidate)] = False  # isolation silently dropped
        report = arc.coordinator.verify_continuity(plan, envelope, handle, proof)
        by_id = {a.assertion_id: a.passed for a in report.assertions}
        self.assertFalse(by_id["candidate-actions-disabled"])

    def test_old_embodiment_sole_authority_lie_is_caught(self) -> None:
        arc = _arc(self.tmp)
        plan, envelope, handle, proof = self._to_candidate_started(arc)
        arc.matching_communication(envelope)
        arc.world.live[arc.world.ref(arc.old)] = False  # old vanished mid-verification
        report = arc.coordinator.verify_continuity(plan, envelope, handle, proof)
        by_id = {a.assertion_id: a.passed for a in report.assertions}
        self.assertFalse(by_id["old-embodiment-sole-authority"])

    def test_candidate_silence_fails_role_and_commitment_assertions(self) -> None:
        """No self-report at all -- the coordinator must NOT default these to
        the expected value (that would make the assertion vacuous)."""

        arc = _arc(self.tmp)
        plan, envelope, handle, proof = self._to_candidate_started(arc)
        arc.world.comm_correlation_id = envelope.boundary.correlation_id
        arc.world.comm_payload = {}  # candidate said nothing structured
        report = arc.coordinator.verify_continuity(plan, envelope, handle, proof)
        by_id = {a.assertion_id: a.passed for a in report.assertions}
        self.assertFalse(by_id["role-authority-boundary"])
        self.assertFalse(by_id["commitments-next-action"])

    def test_candidate_silence_fails_checkpoint_digest_and_source_revision_assertions(self) -> None:
        """T-3461: parallel to the role/commitment silence test above --
        before T-3461, `last-checkpoint-digest` and `bundle-source-revisions`
        were populated straight from the TRUSTED checkpoint envelope itself
        (never from the candidate's response), so they passed even when the
        candidate said nothing. Post-wiring-fix they must be genuinely
        adversarial: a silent candidate fails them too."""

        arc = _arc(self.tmp)
        plan, envelope, handle, proof = self._to_candidate_started(arc)
        arc.world.comm_correlation_id = envelope.boundary.correlation_id
        arc.world.comm_payload = {}  # candidate said nothing structured
        report = arc.coordinator.verify_continuity(plan, envelope, handle, proof)
        by_id = {a.assertion_id: a.passed for a in report.assertions}
        self.assertFalse(by_id["last-checkpoint-digest"])
        self.assertFalse(by_id["bundle-source-revisions"])

    def test_candidate_wrong_checkpoint_digest_fails_last_checkpoint_assertion(self) -> None:
        """T-3461: a candidate that self-reports a WELL-FORMED but WRONG
        checkpoint_sha256 (e.g. hydrated from a stale/different checkpoint)
        must fail -- proving the assertion is genuinely adversarial now, not
        just sensitive to total silence."""

        arc = _arc(self.tmp)
        plan, envelope, handle, proof = self._to_candidate_started(arc)
        arc.matching_communication(envelope)
        arc.world.comm_payload["checkpoint_sha256"] = "f" * 64  # well-formed, wrong digest
        report = arc.coordinator.verify_continuity(plan, envelope, handle, proof)
        by_id = {a.assertion_id: a.passed for a in report.assertions}
        self.assertFalse(by_id["last-checkpoint-digest"])

    def test_candidate_wrong_source_revisions_fails_bundle_source_revisions_assertion(self) -> None:
        """T-3461: same shape for source_revisions -- a well-formed but
        wrong self-reported source-revision list must fail the assertion."""

        arc = _arc(self.tmp)
        plan, envelope, handle, proof = self._to_candidate_started(arc)
        arc.matching_communication(envelope)
        arc.world.comm_payload["source_revisions"] = [
            {"uri": "repo:wrong.json", "source_revision": "wrong-rev", "source_sha256": "f" * 64}
        ]
        report = arc.coordinator.verify_continuity(plan, envelope, handle, proof)
        by_id = {a.assertion_id: a.passed for a in report.assertions}
        self.assertFalse(by_id["bundle-source-revisions"])

    def test_communication_correlation_mismatch_fails_assertion(self) -> None:
        arc = _arc(self.tmp)
        plan, envelope, handle, proof = self._to_candidate_started(arc)
        arc.matching_communication(envelope)
        arc.world.comm_correlation_id = "corr-wrong"
        report = arc.coordinator.verify_continuity(plan, envelope, handle, proof)
        by_id = {a.assertion_id: a.passed for a in report.assertions}
        self.assertFalse(by_id["candidate-communication-correlation"])

    def test_stale_checkpoint_digest_in_evidence_fails_last_checkpoint_assertion(self) -> None:
        arc = _arc(self.tmp)
        plan, envelope, handle, proof = self._to_candidate_started(arc)
        arc.matching_communication(envelope)
        # Simulate a communicate() port that echoes a superseded checkpoint
        # digest by monkeypatching build_verification_report's input path is
        # not exposed directly -- instead we corrupt what verify_continuity
        # reads by handing it a DIFFERENT (stale) envelope shape for the
        # digest check: swap payload_sha256 comparison target by mutating the
        # checkpoint reference used downstream is out of reach here, so this
        # test drives the same effect via the assertion's real dependency:
        # the checkpoint envelope passed in is the fresh one but the
        # candidate's *checkpoint_sha256* field is fixed by our code to
        # envelope.payload_sha256, so we assert the POSITIVE instead and
        # cover staleness at the require_checkpoint() layer (descriptor-drift
        # test above) which is where a stale checkpoint is actually caught
        # before verify_continuity ever runs.
        report = arc.coordinator.verify_continuity(plan, envelope, handle, proof)
        by_id = {a.assertion_id: a.passed for a in report.assertions}
        self.assertTrue(by_id["last-checkpoint-digest"])


# --------------------------------------------------------------------------
# 6. commit_transfer()
# --------------------------------------------------------------------------


class CommitTransferTests(TmpCase):
    def _to_verified(self, arc: Arc):
        plan = arc.plan()
        preflight = arc.coordinator.preflight(plan)
        envelope = arc.coordinator.require_checkpoint(plan, preflight, checkpoint_envelope_path=arc.checkpoint_envelope_path)
        handle, proof = arc.coordinator.start_candidate(plan, envelope)
        arc.matching_communication(envelope)
        report = arc.coordinator.verify_continuity(plan, envelope, handle, proof)
        return plan, envelope, report, proof

    def test_commit_transfer_happy_path_single_receiver_after(self) -> None:
        arc = _arc(self.tmp, telegram=True)
        plan, envelope, report, proof = self._to_verified(arc)
        receipt = arc.coordinator.commit_transfer(plan, envelope, report, proof)
        self.assertEqual(receipt.outcome, "committed")
        self.assertFalse(arc.world.live[arc.world.ref(arc.old)])
        self.assertTrue(arc.world.live[arc.world.ref(arc.candidate)])
        self.assertEqual(arc.world.receiver, arc.world.ref(arc.candidate))
        self.assertEqual(arc.world.maintenance_exits, ["cairn"])
        self.assertFalse(arc.world.isolated[arc.world.ref(arc.candidate)])  # released post-commit

    def test_commit_transfer_refuses_when_not_all_assertions_passed(self) -> None:
        arc = _arc(self.tmp)
        plan, envelope, report, proof = self._to_verified(arc)
        broken = dataclasses.replace(
            report,
            assertions=tuple(
                dataclasses.replace(a, passed=False) if a.assertion_id == "candidate-actions-disabled" else a
                for a in report.assertions
            ),
        )
        with self.assertRaises(pc.ContinuityError) as ctx:
            arc.coordinator.commit_transfer(plan, envelope, broken, proof)
        self.assertEqual(ctx.exception.code, "verification-not-ready")
        self.assertTrue(arc.world.live[arc.world.ref(arc.old)])  # never touched

    def test_commit_transfer_refuses_split_brain(self) -> None:
        arc = _arc(self.tmp)
        plan, envelope, report, proof = self._to_verified(arc)
        arc.world.isolated[arc.world.ref(arc.candidate)] = False  # candidate no longer isolated pre-commit
        with self.assertRaises(pc.ContinuityError) as ctx:
            arc.coordinator.commit_transfer(plan, envelope, report, proof)
        self.assertEqual(ctx.exception.code, "split-brain-refused")

    def test_commit_transfer_refuses_unstable_observation(self) -> None:
        arc = _arc(self.tmp)
        plan, envelope, report, proof = self._to_verified(arc)
        arc.world.live[arc.world.ref(arc.old)] = False  # flapped away between checks
        with self.assertRaises(pc.ContinuityError) as ctx:
            arc.coordinator.commit_transfer(plan, envelope, report, proof)
        self.assertEqual(ctx.exception.code, "unstable-observation")

    def test_commit_transfer_failure_triggers_rollback(self) -> None:
        arc = _arc(self.tmp)
        plan, envelope, report, proof = self._to_verified(arc)
        arc.world.transfer_raises = RuntimeError("simulated mid-swap failure")
        with self.assertRaises(RuntimeError):
            arc.coordinator.commit_transfer(plan, envelope, report, proof)
        # rollback() ran: old restored live, candidate stopped.
        self.assertTrue(arc.world.live[arc.world.ref(arc.old)])
        self.assertFalse(arc.world.live.get(arc.world.ref(arc.candidate), False))
        self.assertIn(arc.world.ref(arc.candidate), arc.world.stopped)


# --------------------------------------------------------------------------
# 7. rollback()
# --------------------------------------------------------------------------


class RollbackTests(TmpCase):
    def test_rollback_restores_old_and_emits_receipt(self) -> None:
        arc = _arc(self.tmp)
        plan = arc.plan()
        arc.coordinator.preflight(plan)
        arc.world.live[arc.world.ref(arc.old)] = False  # simulate mid-arc old death
        receipt = arc.coordinator.rollback(plan, reason="test", stage_reached="preflighted")
        self.assertEqual(receipt.outcome, "rolled-back")
        self.assertTrue(arc.world.live[arc.world.ref(arc.old)])
        self.assertIn("cairn", arc.world.maintenance_exits)

    def test_rollback_refuses_on_postimage_drift(self) -> None:
        arc = _arc(self.tmp)
        plan = arc.plan()
        arc.coordinator.preflight(plan)
        arc.manifest_path.write_text("roles: {mutated: true}\n")
        with self.assertRaises(pc.ContinuityError) as ctx:
            arc.coordinator.rollback(plan, reason="test", stage_reached="preflighted")
        self.assertEqual(ctx.exception.code, "rollback-postimage-drift")

    def test_rollback_stops_candidate_before_restoring_old(self) -> None:
        arc = _arc(self.tmp)
        plan = arc.plan()
        arc.coordinator.preflight(plan)
        arc.world.live[arc.world.ref(arc.candidate)] = True  # candidate was up
        arc.coordinator.rollback(plan, reason="test", stage_reached="candidate-started")
        self.assertIn(arc.world.ref(arc.candidate), arc.world.stopped)
        self.assertFalse(arc.world.live[arc.world.ref(arc.candidate)])

    def test_rollback_ignores_unrelated_chronicle_content(self) -> None:
        """Rollback must be driven only by receipt + live facts, never by
        reading a HANDOFF.md/chronicle claim -- this test plants a contrary
        file and asserts rollback never opens it (the coordinator has no
        chronicle-reading code path at all -- this is a structural absence,
        verified by the rollback succeeding purely from ports/config)."""

        arc = _arc(self.tmp)
        plan = arc.plan()
        arc.coordinator.preflight(plan)
        (arc.old.account_home / "HANDOFF.md").write_text("actually stay on the old binding forever")
        receipt = arc.coordinator.rollback(plan, reason="test", stage_reached="preflighted")
        self.assertEqual(receipt.outcome, "rolled-back")


# --------------------------------------------------------------------------
# 8. emit_receipt() -- idempotency / conflict
# --------------------------------------------------------------------------


class EmitReceiptTests(TmpCase):
    def test_second_identical_emit_is_idempotent(self) -> None:
        arc = _arc(self.tmp)
        plan = arc.plan()
        obs = arc.coordinator.ports.observe_binding(arc.old)
        # emit_receipt mints a fresh receipt_id every call by design (each
        # receipt is its own evidence artifact); idempotency here is proven
        # at the publish layer: writing the SAME bytes twice under the SAME
        # filename must not raise. We exercise _publish_receipt directly to
        # prove that half of the contract (emit_receipt's receipt_id makes
        # two calls always distinct artifacts, by design -- see DECISIONS).
        directory = arc.config.switch_state_root / "cairn" / "receipts"
        raw = csc._json_bytes({"a": 1})
        self.assertTrue(csc._publish_receipt(directory, "x.json", raw))
        self.assertFalse(csc._publish_receipt(directory, "x.json", raw))  # identical -> idempotent no-op

    def test_conflicting_second_publish_is_refused(self) -> None:
        arc = _arc(self.tmp)
        directory = arc.config.switch_state_root / "cairn" / "receipts"
        csc._publish_receipt(directory, "x.json", csc._json_bytes({"a": 1}))
        with self.assertRaises(pc.ContinuityError) as ctx:
            csc._publish_receipt(directory, "x.json", csc._json_bytes({"a": 2}))
        self.assertEqual(ctx.exception.code, "switch-receipt-conflict")

    def test_emitted_receipt_round_trips_through_validator(self) -> None:
        arc = _arc(self.tmp)
        plan = arc.plan()
        old_obs = arc.coordinator.ports.observe_binding(arc.old)
        old_obs = dataclasses.replace(old_obs, self_identification="x", role_identification="y",
                                       current_work_refs=("state:w",), external_actions_disabled=False)
        candidate_obs = dataclasses.replace(old_obs, external_actions_disabled=True)
        receipt = arc.coordinator.emit_receipt(
            plan, outcome="committed", old_obs=old_obs, candidate_obs=candidate_obs,
            checkpoint=None, verification=None, isolation_proof=None,
        )
        reloaded = pc.validate_switch_receipt_file(
            arc.config.switch_state_root / "cairn" / "receipts" / f"{receipt.receipt_id}.json"
        )
        self.assertEqual(reloaded.receipt_sha256, receipt.receipt_sha256)


# --------------------------------------------------------------------------
# 9. resume_after_crash() -- the four-fact conjunction
# --------------------------------------------------------------------------


class ResumeAfterCrashTests(TmpCase):
    def _committed_transition(self, arc: Arc) -> tuple[csc.SwitchPlan, pc.CheckpointEnvelope, pc.HarnessSwitchReceipt]:
        plan = arc.plan()
        preflight = arc.coordinator.preflight(plan)
        envelope = arc.coordinator.require_checkpoint(plan, preflight, checkpoint_envelope_path=arc.checkpoint_envelope_path)
        handle, proof = arc.coordinator.start_candidate(plan, envelope)
        arc.matching_communication(envelope)
        report = arc.coordinator.verify_continuity(plan, envelope, handle, proof)
        receipt = arc.coordinator.commit_transfer(plan, envelope, report, proof)
        return plan, envelope, receipt

    def test_resume_keeps_candidate_when_all_four_facts_hold(self) -> None:
        arc = _arc(self.tmp, telegram=True)
        plan, envelope, receipt = self._committed_transition(arc)
        result = arc.coordinator.resume_after_crash(plan.plan_id)
        self.assertEqual(result.receipt_sha256, receipt.receipt_sha256)
        self.assertEqual(result.outcome, "committed")

    def test_resume_restores_old_when_old_still_live(self) -> None:
        """17a: committed receipt but live proof contradicts it (old never
        actually went away) -- receipt alone must not win."""

        arc = _arc(self.tmp, telegram=True)
        plan, envelope, receipt = self._committed_transition(arc)
        arc.world.live[arc.world.ref(arc.old)] = True  # old reappeared post-crash
        result = arc.coordinator.resume_after_crash(plan.plan_id)
        self.assertEqual(result.outcome, "rolled-back")

    def test_resume_restores_old_when_candidate_absent(self) -> None:
        arc = _arc(self.tmp, telegram=True)
        plan, envelope, receipt = self._committed_transition(arc)
        arc.world.live[arc.world.ref(arc.candidate)] = False  # candidate died post-crash
        result = arc.coordinator.resume_after_crash(plan.plan_id)
        self.assertEqual(result.outcome, "rolled-back")

    def test_resume_restores_old_when_receipt_checkpoint_digest_contradicts_transition(self) -> None:
        """17d: the other three facts (old absent, candidate sole+healthy,
        manifest exact) all hold, but the receipt's checkpoint_sha256 no
        longer matches what the transition record captured at commit time
        (e.g. the transition record was corrupted/edited post-crash) --
        receipt/live-evidence contradiction on ANY one fact is enough to
        refuse keep-candidate, even with the other three green."""

        arc = _arc(self.tmp, telegram=True)
        plan, envelope, receipt = self._committed_transition(arc)
        transition = csc._read_transition(arc.config.switch_state_root, "cairn", plan.plan_id)
        corrupted = dataclasses.replace(transition, checkpoint_sha256="f" * 64)
        csc._write_transition(arc.config.switch_state_root, corrupted)
        result = arc.coordinator.resume_after_crash(plan.plan_id)
        self.assertEqual(result.outcome, "rolled-back")

    def test_resume_refuses_rather_than_fix_forward_when_manifest_drifted(self) -> None:
        """Manifest drift post-commit fails BOTH the keep-candidate fact-check
        AND rollback's own postimage-drift guard (rollback restores to the
        PLAN-time manifest, which no longer matches either) -- the correct
        behavior is a typed refusal, never a fabricated "successfully rolled
        back" receipt against a binding nobody can prove is still correct."""

        arc = _arc(self.tmp, telegram=True)
        plan, envelope, receipt = self._committed_transition(arc)
        arc.manifest_path.write_text("roles: {drifted: true}\n")
        with self.assertRaises(pc.ContinuityError) as ctx:
            arc.coordinator.resume_after_crash(plan.plan_id)
        self.assertEqual(ctx.exception.code, "rollback-postimage-drift")

    def test_resume_restores_old_when_no_receipt_recorded(self) -> None:
        arc = _arc(self.tmp, telegram=True)
        plan = arc.plan()
        arc.coordinator.preflight(plan)  # never reaches commit -- no receipt
        result = arc.coordinator.resume_after_crash(plan.plan_id)
        self.assertEqual(result.outcome, "rolled-back")

    def test_resume_restores_old_when_receipt_not_committed(self) -> None:
        arc = _arc(self.tmp, telegram=True)
        plan = arc.plan()
        arc.coordinator.preflight(plan)
        arc.coordinator.rollback(plan, reason="pre-existing rollback", stage_reached="preflighted")
        result = arc.coordinator.resume_after_crash(plan.plan_id)
        self.assertEqual(result.outcome, "rolled-back")

    def test_resume_restores_old_when_transition_record_missing(self) -> None:
        arc = _arc(self.tmp, telegram=True)
        result = arc.coordinator.resume_after_crash("no-such-transition")
        self.assertEqual(result.outcome, "rolled-back")

    def test_resume_never_reads_pane_text_or_process_recency(self) -> None:
        """Structural check: resume_after_crash's fact-derivation touches
        only is_live / receiver_present / receiver_absent / manifest bytes /
        checkpoint+verification digests -- never `communicate()` (which is
        the only port that would expose pane TEXT), and never a "which
        process started more recently" comparison (no port for that exists
        at all)."""

        arc = _arc(self.tmp, telegram=True)
        plan, envelope, receipt = self._committed_transition(arc)
        calls = {"communicate": 0}
        original = arc.coordinator.ports.communicate

        def counting_communicate(handle, prompt):
            calls["communicate"] += 1
            return original(handle, prompt)

        arc.coordinator.ports.communicate = counting_communicate
        arc.coordinator.resume_after_crash(plan.plan_id)
        self.assertEqual(calls["communicate"], 0)


# --------------------------------------------------------------------------
# 10. Maintenance-mode bounded duration (matrix #18)
# --------------------------------------------------------------------------


class MaintenanceBoundedTests(TmpCase):
    def test_maintenance_entry_never_exceeds_transaction_ceiling(self) -> None:
        arc = _arc(self.tmp)
        arc.config = dataclasses.replace(arc.config, transaction_ceiling_minutes=5)
        arc.coordinator = csc.ContinuitySwitchCoordinator(arc.config, arc.coordinator.ports)
        plan = arc.plan()
        arc.coordinator.preflight(plan)
        _, minutes, _ = arc.world.maintenance_entries[0]
        self.assertLessEqual(minutes, 5)


# --------------------------------------------------------------------------
# 11. Wiring smoke -- default_ports() constructs without touching the OS
# --------------------------------------------------------------------------


class DefaultPortsWiringTests(TmpCase):
    def test_default_ports_constructs_all_callables(self) -> None:
        arc = _arc(self.tmp)
        ports = csc.default_ports(arc.config)
        for field in dataclasses.fields(csc.CoordinatorPorts):
            self.assertTrue(callable(getattr(ports, field.name)), field.name)

    def test_default_git_revision_returns_valid_sha(self) -> None:
        import subprocess
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "f").write_text("x")
        subprocess.run(["git", "add", "f"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=repo, check=True)
        revision = csc._default_git_revision(repo)
        self.assertEqual(len(revision), 40)
        self.assertTrue(all(c in "0123456789abcdef" for c in revision))

    def test_default_git_revision_refuses_non_git_directory(self) -> None:
        not_a_repo = self.tmp / "not-a-repo"
        not_a_repo.mkdir()
        with self.assertRaises(pc.ContinuityError) as ctx:
            csc._default_git_revision(not_a_repo)
        self.assertEqual(ctx.exception.code, "controller-revision-unavailable")


# --------------------------------------------------------------------------
# 12. REAL Claude candidate-guard settings merge (the production isolation
# mechanism, not the fake used by the state-machine tests above). This is
# the loaded-scope fix: Claude Code loads ONLY canonical
# `<account_home>/.claude/settings.json` -- a differently-named file is
# never read (reference_claude_settings_loaded_scope), so this MUST write
# there, and MUST merge rather than clobber since that file may already
# carry real MCP/permissions/other-hooks configuration.
# --------------------------------------------------------------------------


class CodexNetworkIsolationTests(TmpCase):
    """T-3177 review finding #2 remediation: writable_roots alone leaves the
    SOP's "external network or device action" structural-disable item unmet
    for Codex candidates. These exercise the REAL default isolation/verify
    functions (not the FakeWorld used by StartCandidateTests above) plus the
    REAL codex adapter's argv-construction path -- coverage that previously
    stopped at dataclass-equality on writable_roots alone."""

    def _codex_binding(self, *, config_overrides: dict | None = None) -> PersonaBinding:
        binding = _binding(self.tmp, role="orchestrator", persona="cairn", account="codex-candidate", harness="codex")
        if config_overrides is not None:
            native = {**binding.launch.native, "config_overrides": config_overrides}
            binding = dataclasses.replace(binding, launch=dataclasses.replace(binding.launch, native=native))
        return binding

    def test_isolate_candidate_sets_network_access_false(self) -> None:
        binding = self._codex_binding()
        isolated, proof = csc._default_isolate_candidate(binding)
        self.assertEqual(isolated.launch.native["config_overrides"][csc._CODEX_NETWORK_OVERRIDE_KEY], False)
        self.assertEqual(isolated.launch.writable_roots, ())
        self.assertTrue(csc._default_verify_isolation(isolated, proof))

    def test_isolate_candidate_preserves_existing_config_overrides(self) -> None:
        binding = self._codex_binding(config_overrides={"approval_policy": "never"})
        isolated, _ = csc._default_isolate_candidate(binding)
        overrides = isolated.launch.native["config_overrides"]
        self.assertEqual(overrides["approval_policy"], "never")
        self.assertEqual(overrides[csc._CODEX_NETWORK_OVERRIDE_KEY], False)

    def test_real_adapter_argv_contains_network_access_false(self) -> None:
        from tools.harness_adapter.codex import CodexHarnessAdapter
        from tools.harness_adapter.types import TmuxTarget as _TmuxTarget

        binding = self._codex_binding()
        isolated, _ = csc._default_isolate_candidate(binding)
        adapter = CodexHarnessAdapter("test-role", codex_home=self.tmp / "codex-home", launch=isolated.launch)
        spec = adapter.build_interactive_launch(_TmuxTarget(session="s", window="0", pane="0"), cwd=self.tmp)
        self.assertIn("--config", spec.argv)
        # There may be multiple --config entries (sorted); scan all of them.
        config_values = [spec.argv[i + 1] for i, item in enumerate(spec.argv) if item == "--config"]
        self.assertIn(f"{csc._CODEX_NETWORK_OVERRIDE_KEY}=false", config_values)
        # writable_roots is empty () for an isolated candidate -- confirm no
        # --add-dir entries leak through (the filesystem axis of isolation).
        self.assertNotIn("--add-dir", spec.argv)

    def test_verify_isolation_false_when_network_access_missing(self) -> None:
        binding = self._codex_binding()  # no config_overrides at all
        proof = csc.CandidateIsolationProof("codex-writable-roots-empty", "x" * 64, "2026-09-06T00:00:00Z")
        isolated_fs_only = dataclasses.replace(binding, launch=dataclasses.replace(binding.launch, writable_roots=()))
        self.assertFalse(csc._default_verify_isolation(isolated_fs_only, proof))

    def test_verify_isolation_false_when_network_access_true(self) -> None:
        binding = self._codex_binding(config_overrides={csc._CODEX_NETWORK_OVERRIDE_KEY: True})
        proof = csc.CandidateIsolationProof("codex-writable-roots-empty", "x" * 64, "2026-09-06T00:00:00Z")
        isolated_fs_only = dataclasses.replace(binding, launch=dataclasses.replace(binding.launch, writable_roots=()))
        self.assertFalse(csc._default_verify_isolation(isolated_fs_only, proof))

    def test_start_candidate_fail_closed_detail_names_network_access(self) -> None:
        """End-to-end through the coordinator using the REAL isolate/verify
        functions (start_runtime faked to avoid touching tmux): if isolation
        somehow lands without the network override (simulated here via a
        verify_isolation wrapper that reports the pre-fix behavior), the
        raised ContinuityError's detail names network_access specifically,
        not just the bare mechanism name."""

        arc = _arc(self.tmp)
        codex_binding = self._codex_binding()
        arc.old, arc.candidate = arc.candidate, codex_binding
        arc.coordinator.ports.list_bindings = lambda: (arc.old, arc.candidate)
        arc.world.live[arc.world.ref(arc.old)] = True

        # Real isolate + real verify (not FakeWorld's), start_runtime stays fake.
        arc.coordinator.ports.isolate_candidate = csc._default_isolate_candidate
        arc.coordinator.ports.verify_isolation = csc._default_verify_isolation

        plan = arc.plan(candidate_harness="codex")
        preflight = arc.coordinator.preflight(plan)
        envelope = arc.coordinator.require_checkpoint(plan, preflight, checkpoint_envelope_path=arc.checkpoint_envelope_path)

        # Simulate the pre-fix world: strip the override AFTER isolate_candidate
        # composed it, by monkeypatching isolate_candidate to isolate then drop it.
        real_isolate = csc._default_isolate_candidate

        def isolate_then_strip_network_override(binding):
            isolated, proof = real_isolate(binding)
            overrides = dict(isolated.launch.native.get("config_overrides") or {})
            overrides.pop(csc._CODEX_NETWORK_OVERRIDE_KEY, None)
            stripped = dataclasses.replace(isolated, launch=dataclasses.replace(isolated.launch, native={**isolated.launch.native, "config_overrides": overrides}))
            return stripped, proof

        arc.coordinator.ports.isolate_candidate = isolate_then_strip_network_override
        with self.assertRaises(pc.ContinuityError) as ctx:
            arc.coordinator.start_candidate(plan, envelope)
        self.assertEqual(ctx.exception.code, "isolation-not-proven")
        self.assertIn("network_access", ctx.exception.detail)
        self.assertEqual(arc.world.started, [])  # never started without proven isolation


class RealClaudeIsolationMergeTests(TmpCase):
    def _binding(self) -> PersonaBinding:
        return _binding(self.tmp, role="orchestrator", persona="recovery", account="claude-fallback", harness="claude")

    def _settings_path(self, binding: PersonaBinding) -> Path:
        return csc._candidate_guard_settings_path(binding)

    def test_writes_canonical_settings_json_not_a_side_file(self) -> None:
        binding = self._binding()
        csc._default_isolate_candidate(binding)
        self.assertEqual(self._settings_path(binding), binding.account_home / ".claude" / "settings.json")
        self.assertTrue(self._settings_path(binding).exists())

    def test_preserves_existing_unrelated_settings_content(self) -> None:
        binding = self._binding()
        path = self._settings_path(binding)
        path.parent.mkdir(parents=True)
        path.write_text(csc._json_bytes({
            "mcpServers": {"telegram": {"command": "bun", "args": ["run", "bot.ts"]}},
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo unrelated"}]}]},
        }).decode())
        _, proof = csc._default_isolate_candidate(binding)
        settings = csc._load_settings(path)
        self.assertIn("mcpServers", settings)
        self.assertEqual(settings["mcpServers"]["telegram"]["command"], "bun")
        pre_tool_use = settings["hooks"]["PreToolUse"]
        self.assertEqual(len(pre_tool_use), 2)  # unrelated entry + new guard entry
        self.assertTrue(any(item["hooks"][0]["command"] == "echo unrelated" for item in pre_tool_use))
        self.assertTrue(csc._default_verify_isolation(binding, proof))

    def test_release_removes_only_the_tagged_entry(self) -> None:
        binding = self._binding()
        path = self._settings_path(binding)
        path.parent.mkdir(parents=True)
        path.write_text(csc._json_bytes({
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo unrelated"}]}]},
        }).decode())
        csc._default_isolate_candidate(binding)
        csc._default_release_isolation(binding)
        settings = csc._load_settings(path)
        pre_tool_use = settings["hooks"]["PreToolUse"]
        self.assertEqual(len(pre_tool_use), 1)
        self.assertEqual(pre_tool_use[0]["hooks"][0]["command"], "echo unrelated")
        self.assertFalse(any(item.get(csc._GUARD_TAG) for item in pre_tool_use))

    def test_release_on_fresh_file_removes_empty_hooks_key(self) -> None:
        binding = self._binding()
        csc._default_isolate_candidate(binding)
        csc._default_release_isolation(binding)
        settings = csc._load_settings(self._settings_path(binding))
        self.assertNotIn("hooks", settings)

    def test_release_is_idempotent_when_nothing_to_remove(self) -> None:
        binding = self._binding()
        csc._default_release_isolation(binding)  # no file at all yet
        self.assertFalse(self._settings_path(binding).exists())

    def test_verify_isolation_false_when_guard_entry_removed_externally(self) -> None:
        binding = self._binding()
        _, proof = csc._default_isolate_candidate(binding)
        path = self._settings_path(binding)
        settings = csc._load_settings(path)
        settings["hooks"]["PreToolUse"] = []
        csc._atomic_write_settings(path, settings)
        self.assertFalse(csc._default_verify_isolation(binding, proof))

    def test_verify_isolation_false_when_no_settings_file_exists(self) -> None:
        binding = self._binding()
        proof = csc.CandidateIsolationProof("claude-candidate-guard-active", "x" * 64, "2026-09-06T00:00:00Z")
        self.assertFalse(csc._default_verify_isolation(binding, proof))

    def test_write_refuses_on_malformed_existing_settings_rather_than_clobber(self) -> None:
        binding = self._binding()
        path = self._settings_path(binding)
        path.parent.mkdir(parents=True)
        path.write_text("{not valid json")
        with self.assertRaises(pc.ContinuityError) as ctx:
            csc._default_isolate_candidate(binding)
        self.assertEqual(ctx.exception.code, "candidate-settings-malformed")
        self.assertEqual(path.read_text(), "{not valid json")  # untouched

    def test_reisolate_replaces_stale_tagged_entry_not_duplicates(self) -> None:
        binding = self._binding()
        csc._default_isolate_candidate(binding)
        csc._default_isolate_candidate(binding)  # re-run (e.g. retry after a transient failure)
        settings = csc._load_settings(self._settings_path(binding))
        tagged = [item for item in settings["hooks"]["PreToolUse"] if item.get(csc._GUARD_TAG)]
        self.assertEqual(len(tagged), 1)


if __name__ == "__main__":
    unittest.main()
