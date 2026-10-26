from __future__ import annotations

import dataclasses
import concurrent.futures
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import yaml

from tools import persona_continuity as pc


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "tools" / "persona_continuity.py"


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class ContinuityFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.state = root / "state"
        self.descriptors = root / "descriptors"
        self.checkpoints = root / "checkpoints"
        for path in (self.repo, self.state, self.descriptors):
            path.mkdir(mode=0o700, parents=True)
        self.sources: dict[str, Path] = {}
        self.refs: dict[str, dict[str, object]] = {}
        specifications = [
            ("role_contract_ref", "role", "repo:role.json"),
            ("identity_canon_ref", "identity", "repo:identity.json"),
            ("goal", "goal", "repo:goal.json"),
            ("capability_profile_ref", "capability", "repo:capability.json"),
            ("authority_profile_ref", "authority", "repo:authority.json"),
            ("memory", "memory", "state:memory.json"),
            ("runtime_binding_ref", "runtime", "state:runtime.json"),
            ("persona_primitive_ref", "identity", "repo:persona.json"),
        ]
        for label, kind, uri in specifications:
            scheme, rel = uri.split(":", 1)
            path = (self.repo if scheme == "repo" else self.state) / rel
            raw = json_bytes({"kind": kind, "label": label})
            path.write_bytes(raw)
            os.chmod(path, 0o644)
            self.sources[uri] = path
            self.refs[label] = {
                "kind": kind,
                "uri": uri,
                "expected_revision": "r1",
                "expected_sha256": hashlib.sha256(raw).hexdigest(),
                "authority_class": "canonical",
                "required": True,
                "purpose_limits": ["hydrate-runtime", "checkpoint-boundary", "verify-candidate"],
            }
        self.manifest_data: dict[str, object] = {
            "schema": pc.BUNDLE_SCHEMA_ID,
            "persona_id": "consultant",
            "organism_id": "pantheon",
            "role_contract_ref": self.refs["role_contract_ref"],
            "identity_canon_ref": self.refs["identity_canon_ref"],
            "values_and_goals_refs": [self.refs["goal"]],
            "capability_profile_ref": self.refs["capability_profile_ref"],
            "authority_profile_ref": self.refs["authority_profile_ref"],
            "memory_root_refs": [self.refs["memory"]],
            "commitment_query": {"kind": "commitment", "persona_id": "consultant", "states": ["active", "waiting"]},
            "runtime_binding_ref": self.refs["runtime_binding_ref"],
            "provenance": {
                "authored_by": "consultant",
                "adopted_by": "operator",
                "source_revision": "rev-a",
                "migrations": [],
                "persona_primitive_ref": self.refs["persona_primitive_ref"],
            },
        }
        self.manifest = self.descriptors / "bundle.yaml"
        self.policy = self.descriptors / "policy.json"
        self.write_manifest()
        self.write_policy()

    def write_manifest(self, data: dict[str, object] | None = None, *, raw: str | None = None) -> None:
        if raw is None:
            raw = yaml.safe_dump(self.manifest_data if data is None else data, sort_keys=False)
        self.manifest.write_text(raw)
        os.chmod(self.manifest, 0o644)

    def policy_data(self) -> dict[str, object]:
        return {
            "schema": pc.SOURCE_POLICY_SCHEMA_ID,
            "source_locator": "test-policy",
            "catalog_revision": "p1",
            "actors": [{
                "actor_id": "consultant",
                "authority_refs": ["authority:canonical"],
                "allowed_purposes": ["hydrate-runtime", "checkpoint-boundary", "verify-candidate"],
            }],
            "entries": [
                {
                    "uri": uri,
                    "source_revision": "r1",
                    "authority_class": "canonical",
                    "privacy": "private" if "memory" in uri else "internal",
                    "redacted": "memory" in uri,
                    "allowed_purposes": ["hydrate-runtime", "checkpoint-boundary", "verify-candidate"],
                    "expected_uid": os.getuid(),
                    "max_mode": "0644",
                }
                for uri in self.sources
            ],
        }

    def write_policy(self, data: dict[str, object] | None = None, *, raw: bytes | None = None) -> None:
        self.policy.write_bytes(raw if raw is not None else json_bytes(self.policy_data() if data is None else data))
        os.chmod(self.policy, 0o644)

    def load(self) -> tuple[pc.ContinuityBundleManifest, pc.SourcePolicyCatalog]:
        return (
            pc.load_continuity_bundle(self.manifest, descriptor_root=self.descriptors),
            pc.load_source_policy_catalog(self.policy),
        )

    def resolve(self, *, actor_authority: tuple[str, ...] = ("authority:canonical",)) -> pc.ResolvedContinuityView:
        manifest, policy = self.load()
        store = pc.ContinuitySourceStore(repo_root=self.repo, state_root=self.state, source_policy=policy)
        actor = pc.ActorContext("consultant", actor_authority, (pc.ContinuityPurpose.HYDRATE_RUNTIME,))
        return pc.ContinuityResolver(store, checkpoint_root=self.checkpoints).resolve(
            manifest,
            purpose="hydrate-runtime",
            actor=actor,
            as_of=datetime(2026, 9, 6, tzinfo=timezone.utc),
        )

    def observation(self, **changes: object) -> pc.RuntimeObservation:
        data: dict[str, object] = {
            "runtime_id": "runtime-1",
            "harness_id": "codex",
            "account_ref": "account:consultant",
            "responsive": True,
            "self_identification": "Consultant",
            "role_identification": "orchestrator",
            "authority_limits": ("authority:canonical",),
            "current_work_refs": ("ticket:T-3176",),
            "receiver_evidence_sha256": "a" * 64,
            "external_actions_disabled": True,
            "observed_at": "2026-09-06T00:00:00Z",
        }
        data.update(changes)
        return pc.RuntimeObservation(**data)  # type: ignore[arg-type]

    def content(self) -> pc.CheckpointContent:
        return pc.CheckpointContent(("ticket:T-3176",), ("promise:T-3176",), (), (), "action:T-3176", ("decision:T-3176",), (), ("state:memory-candidate/T-3176",))

    def boundary(self, **changes: object) -> pc.CheckpointBoundary:
        data: dict[str, object] = {
            "boundary_id": "boundary-1",
            "author": "consultant",
            "correlation_id": "corr-1",
            "created_at": "2026-09-06T00:00:00Z",
            "supersedes_receipt_path": None,
            "supersedes_payload_sha256": None,
        }
        data.update(changes)
        return pc.CheckpointBoundary(**data)  # type: ignore[arg-type]

    def checkpoint(self) -> tuple[pc.ResolvedContinuityView, pc.CheckpointReceipt]:
        view = self.resolve()
        _, policy = self.load()
        actor = policy.actor_context("consultant", pc.ContinuityPurpose.HYDRATE_RUNTIME)
        resolver = pc.ContinuityResolver(None, checkpoint_root=self.checkpoints, source_policy=policy)
        receipt = resolver.checkpoint(
            view,
            boundary_id="boundary-1",
            runtime_observation=self.observation(),
            content=self.content(),
            boundary=self.boundary(),
            producer_revision="test-revision",
            actor=actor,
            purpose="hydrate-runtime",
        )
        return view, receipt

    def candidate(self, envelope: pc.CheckpointEnvelope, **changes: object) -> pc.CandidateVerificationEvidence:
        data: dict[str, object] = {
            "runtime_id": "candidate-1",
            "persona_id": envelope.persona_id,
            "organism_id": envelope.organism_id,
            "role_id": "repo:role.json",
            "role_authority_refs": ("authority:canonical",),
            "current_commitment_refs": tuple(sorted((*envelope.content.active_ticket_refs, *envelope.content.promise_refs, *envelope.content.wait_refs, *envelope.content.escalation_refs))),
            "expected_next_action": envelope.content.expected_next_action,
            "authority_limits": envelope.runtime_observation.authority_limits,
            "unavailable_capabilities": (),
            "capability_evidence_complete": True,
            "checkpoint_sha256": envelope.payload_sha256,
            "descriptor_sha256": envelope.descriptor_sha256,
            "source_revisions": tuple(pc.CandidateSourceRevision(item.uri, item.source_revision, item.source_sha256) for item in envelope.source_refs),
            "communication_correlation_id": envelope.boundary.correlation_id,
            "communication_evidence_sha256": "b" * 64,
            "communication_ok": True,
            "external_actions_disabled": True,
            "old_embodiment_sole_authority": True,
            "behavioral_variance": (),
            "variance_evidence_sha256": "c" * 64,
            "observed_at": "2026-09-06T00:01:00Z",
        }
        data.update(changes)
        return pc.CandidateVerificationEvidence(**data)  # type: ignore[arg-type]


class PersonaContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fx = ContinuityFixture(Path(self.temp.name))

    def assert_code(self, code: str, callback: object) -> None:
        with self.assertRaises(pc.ContinuityError) as raised:
            callback()  # type: ignore[operator]
        self.assertEqual(raised.exception.code, code)

    def test_generated_continuity_schema_bytes_match_checked_in_files(self) -> None:
        for name, raw in pc.generate_continuity_schemas().items():
            self.assertEqual((REPO_ROOT / "schemas" / name).read_bytes(), raw)
            self.assertTrue(raw.endswith(b"\n"))
            self.assertFalse(raw.endswith(b"\n\n"))

    def test_bundle_requires_all_org057_logical_fields(self) -> None:
        for field in list(self.fx.manifest_data):
            if field == "schema":
                continue
            data = dict(self.fx.manifest_data)
            del data[field]
            self.fx.write_manifest(data)
            with self.subTest(field=field):
                self.assert_code("missing-field", self.fx.load)

    def test_bundle_refuses_missing_duplicate_unknown_fields(self) -> None:
        data = dict(self.fx.manifest_data)
        data["unknown"] = True
        self.fx.write_manifest(data)
        self.assert_code("unknown-field", self.fx.load)
        raw = yaml.safe_dump(self.fx.manifest_data, sort_keys=False) + "persona_id: other\n"
        self.fx.write_manifest(raw=raw)
        self.assert_code("duplicate-field", self.fx.load)

    def test_bundle_refuses_unknown_ref_kind(self) -> None:
        self.fx.refs["role_contract_ref"]["kind"] = "oracle"
        self.fx.write_manifest()
        self.assert_code("invalid-enum", self.fx.load)

    def test_bundle_refuses_inline_secret(self) -> None:
        self.fx.refs["role_contract_ref"]["secret_value"] = "not-inline"
        self.fx.write_manifest()
        self.assert_code("inline-authority-forbidden", self.fx.load)

    def test_bundle_refuses_inline_memory_payload(self) -> None:
        self.fx.refs["memory"]["payload"] = "memory bytes"
        self.fx.write_manifest()
        self.assert_code("inline-authority-forbidden", self.fx.load)

    def test_bundle_refuses_path_traversal_symlink_owner_or_mode_fault(self) -> None:
        self.fx.refs["role_contract_ref"]["uri"] = "repo:../escape"
        self.fx.write_manifest()
        self.assert_code("path-traversal", self.fx.load)
        self.fx = ContinuityFixture(Path(self.temp.name) / "second")
        role = self.fx.sources["repo:role.json"]
        target = role.with_suffix(".target")
        role.rename(target)
        role.symlink_to(target)
        self.assert_code("symlink-forbidden", self.fx.resolve)
        role.unlink()
        target.rename(role)
        os.chmod(role, 0o666)
        self.assert_code("unsafe-mode", self.fx.resolve)

    def test_bundle_refuses_path_traversal_and_symlink(self) -> None:
        self.fx.refs["role_contract_ref"]["uri"] = "repo:../escape"
        self.fx.write_manifest()
        self.assert_code("path-traversal", self.fx.load)

        separate = ContinuityFixture(Path(self.temp.name) / "symlink-root")
        actual = separate.descriptors.with_name("actual-descriptors")
        separate.descriptors.rename(actual)
        separate.descriptors.symlink_to(actual, target_is_directory=True)
        self.assert_code("symlink-root-forbidden", separate.load)

    def test_bundle_refuses_duplicate_logical_field(self) -> None:
        self.fx.refs["identity_canon_ref"]["uri"] = self.fx.refs["role_contract_ref"]["uri"]
        self.fx.write_manifest()
        self.assert_code("duplicate-logical-ref", self.fx.load)

    def test_resolver_reads_descriptor_bytes_then_real_source_bytes_and_binds_digest(self) -> None:
        view = self.fx.resolve()
        self.assertEqual(len(view.source_evidence), 8)
        for evidence in view.source_evidence:
            self.assertEqual(evidence.source_sha256, hashlib.sha256(self.fx.sources[evidence.uri].read_bytes()).hexdigest())
        self.assertRegex(view.view_sha256, r"^[0-9a-f]{64}$")

    def test_resolver_refuses_dangling_required_ref(self) -> None:
        self.fx.sources["repo:identity.json"].unlink()
        self.assert_code("source-unavailable", self.fx.resolve)

    def test_resolver_records_optional_ref_omission(self) -> None:
        self.fx.refs["memory"]["required"] = False
        self.fx.write_manifest()
        self.fx.sources["state:memory.json"].unlink()
        view = self.fx.resolve()
        self.assertEqual([(item.uri, item.reason) for item in view.omissions], [("state:memory.json", "source-unavailable")])

    def test_resolver_refuses_revision_or_digest_drift(self) -> None:
        self.fx.refs["role_contract_ref"]["expected_revision"] = "wrong"
        self.fx.write_manifest()
        self.assert_code("revision-drift", self.fx.resolve)
        self.fx.refs["role_contract_ref"]["expected_revision"] = "r1"
        self.fx.refs["role_contract_ref"]["expected_sha256"] = "0" * 64
        self.fx.write_manifest()
        self.assert_code("digest-drift", self.fx.resolve)

    def test_direct_api_normalizes_required_refs_and_runtime_reference_fields(self) -> None:
        manifest, policy = self.fx.load()
        unpinned = dataclasses.replace(
            manifest,
            role_contract_ref=dataclasses.replace(manifest.role_contract_ref, expected_revision=None, expected_sha256=None),
        )
        store = pc.ContinuitySourceStore(repo_root=self.fx.repo, state_root=self.fx.state, source_policy=policy)
        resolver = pc.ContinuityResolver(store, checkpoint_root=self.fx.checkpoints)
        actor = policy.actor_context("consultant", pc.ContinuityPurpose.HYDRATE_RUNTIME)
        self.assert_code(
            "required-ref-unpinned",
            lambda: resolver.resolve(unpinned, purpose="hydrate-runtime", actor=actor, as_of=datetime.now(timezone.utc)),
        )

        view = self.fx.resolve()
        checkpoint_resolver = pc.ContinuityResolver(None, checkpoint_root=self.fx.checkpoints, source_policy=policy)
        self.assert_code(
            "invalid-reference",
            lambda: checkpoint_resolver.checkpoint(
                view,
                boundary_id="boundary-1",
                runtime_observation=self.fx.observation(authority_limits=("raw authority prose",)),
                content=self.fx.content(),
                boundary=self.fx.boundary(),
                producer_revision="r",
                actor=actor,
                purpose="hydrate-runtime",
            ),
        )

    def test_bundle_accepts_canonical_empty_optional_ref_collections(self) -> None:
        self.fx.manifest_data["values_and_goals_refs"] = []
        self.fx.manifest_data["memory_root_refs"] = []
        self.fx.write_manifest()
        view = self.fx.resolve()
        self.assertFalse(any(item.kind in {"goal", "memory"} for item in view.source_evidence))

    def test_required_ref_requires_revision_and_digest_pins(self) -> None:
        for field in ("expected_revision", "expected_sha256"):
            with self.subTest(field=field):
                self.fx.refs["role_contract_ref"][field] = None
                self.fx.write_manifest()
                self.assert_code("required-ref-unpinned", self.fx.load)
                self.fx = ContinuityFixture(Path(self.temp.name) / field)

    def test_resolver_refuses_actor_or_purpose_reuse(self) -> None:
        self.assert_code("actor-context-conflict", lambda: self.fx.resolve(actor_authority=("authority:other",)))
        manifest, policy = self.fx.load()
        store = pc.ContinuitySourceStore(repo_root=self.fx.repo, state_root=self.fx.state, source_policy=policy)
        actor = pc.ActorContext("consultant", ("authority:canonical",), (pc.ContinuityPurpose.OPERATOR_AUDIT,))
        self.assert_code("actor-purpose-refused", lambda: pc.ContinuityResolver(store, checkpoint_root=self.fx.checkpoints).resolve(manifest, purpose="operator-audit", actor=actor, as_of=datetime.now(timezone.utc)))

    def test_resolver_preserves_source_authority_and_redaction(self) -> None:
        view = self.fx.resolve()
        memory = next(item for item in view.source_evidence if item.kind == "memory")
        self.assertEqual(memory.authority_class, "canonical")
        self.assertEqual(memory.privacy, "private")
        self.assertTrue(memory.redacted)

    def test_source_policy_descriptor_self_assertion_cannot_authorize(self) -> None:
        self.fx.refs["role_contract_ref"]["authority_class"] = "invented"
        self.fx.write_manifest()
        self.assert_code("authority-conflict", self.fx.resolve)

    def test_source_policy_omission_and_conflict_fail_closed(self) -> None:
        data = self.fx.policy_data()
        data["entries"] = data["entries"][1:]  # type: ignore[index]
        self.fx.write_policy(data)
        self.assert_code("source-policy-missing", self.fx.resolve)
        data = self.fx.policy_data()
        data["entries"].append(dict(data["entries"][0]))  # type: ignore[union-attr,index]
        self.fx.write_policy(data)
        self.assert_code("source-policy-ambiguous", self.fx.load)

    def test_native_or_latent_memory_cannot_satisfy_required_ref(self) -> None:
        self.fx.refs["identity_canon_ref"]["uri"] = "native-memory:identity"
        self.fx.write_manifest()
        self.assert_code("invalid-uri", self.fx.load)

    def test_t3029_persona_role_contract_is_consumed_not_redefined(self) -> None:
        data = dict(self.fx.manifest_data)
        data["functional_role_class"] = "second-authority"
        self.fx.write_manifest(data)
        self.assert_code("unknown-field", self.fx.load)

    def test_checkpoint_requires_verified_runtime_observation(self) -> None:
        view = self.fx.resolve()
        _, policy = self.fx.load()
        actor = policy.actor_context("consultant", pc.ContinuityPurpose.HYDRATE_RUNTIME)
        resolver = pc.ContinuityResolver(None, checkpoint_root=self.fx.checkpoints, source_policy=policy)
        self.assert_code("runtime-not-ready", lambda: resolver.checkpoint(view, boundary_id="boundary-1", runtime_observation=self.fx.observation(self_identification=""), content=self.fx.content(), boundary=self.fx.boundary(), producer_revision="r", actor=actor, purpose="hydrate-runtime"))

    def test_checkpoint_exclusive_create_0600_no_follow_fsync_and_readback(self) -> None:
        _, receipt = self.fx.checkpoint()
        path = Path(receipt.path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        envelope = pc.load_checkpoint_envelope(path, checkpoint_root=self.fx.checkpoints)
        self.assertEqual(envelope.payload_sha256, receipt.payload_sha256)

    def test_checkpoint_exclusive_create_mode_and_readback(self) -> None:
        _, receipt = self.fx.checkpoint()
        path = Path(receipt.path)
        self.assertEqual(stat.S_IMODE(path.stat(follow_symlinks=False).st_mode), 0o600)
        self.assertEqual(
            pc.load_checkpoint_envelope(path, checkpoint_root=self.fx.checkpoints).payload_sha256,
            receipt.payload_sha256,
        )

    def test_checkpoint_verify_readback_from_separate_process(self) -> None:
        _, receipt = self.fx.checkpoint()
        receipt_path = self.fx.root / "receipt.json"
        receipt_path.write_bytes(json_bytes(pc._plain(receipt)))
        os.chmod(receipt_path, 0o600)
        envelope = pc.load_checkpoint_envelope(Path(receipt.path), checkpoint_root=self.fx.checkpoints)
        candidate_path = self.fx.root / "candidate.json"
        candidate_path.write_bytes(json_bytes(pc._plain(self.fx.candidate(envelope))))
        os.chmod(candidate_path, 0o600)
        proc = subprocess.run([sys.executable, str(CLI), "verify", "--receipt", str(receipt_path), "--manifest", str(self.fx.manifest), "--descriptor-root", str(self.fx.descriptors), "--checkpoint-root", str(self.fx.checkpoints), "--repo-root", str(self.fx.repo), "--state-root", str(self.fx.state), "--source-policy", str(self.fx.policy), "--candidate-evidence", str(candidate_path), "--actor-id", "consultant", "--purpose", "hydrate-runtime", "--json"], text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "ready")

    def test_checkpoint_is_immutable_and_supersession_linked(self) -> None:
        _, receipt = self.fx.checkpoint()
        self.assert_code("checkpoint-exists", self.fx.checkpoint)
        view = self.fx.resolve()
        second = pc.write_checkpoint(view, content=self.fx.content(), boundary=self.fx.boundary(boundary_id="boundary-2", supersedes_receipt_path=receipt.path, supersedes_payload_sha256=receipt.payload_sha256), runtime_observation=self.fx.observation(), checkpoint_root=self.fx.checkpoints, producer_revision="r2")
        envelope = pc.load_checkpoint_envelope(Path(second.path), checkpoint_root=self.fx.checkpoints)
        self.assertEqual(envelope.prior_checkpoint_sha256, receipt.payload_sha256)

    def test_checkpoint_refuses_hidden_session_state_field(self) -> None:
        data = pc._plain(self.fx.content())
        data["session_state"] = "opaque"  # type: ignore[index]
        self.assert_code("unknown-field", lambda: pc._content_from_obj(data))

    def test_checkpoint_refuses_unclassified_commitment(self) -> None:
        data = pc._plain(self.fx.content())
        data["promise_refs"] = [{"payload": "inline"}]  # type: ignore[index]
        self.assert_code("invalid-string", lambda: pc._content_from_obj(data))
        data = pc._plain(self.fx.content())
        data["expected_next_action"] = "continue with private prose"  # type: ignore[index]
        self.assert_code("invalid-reference", lambda: pc._content_from_obj(data))

    def test_checkpoint_refuses_partial_race_and_digest_drift(self) -> None:
        _, receipt = self.fx.checkpoint()
        path = Path(receipt.path)
        os.chmod(path, 0o600)
        raw = json.loads(path.read_text())
        raw["producer_revision"] = "tampered"
        path.write_bytes(json_bytes(raw))
        os.chmod(path, 0o600)
        self.assert_code("checkpoint-digest-drift", lambda: pc.load_checkpoint_envelope(path, checkpoint_root=self.fx.checkpoints))

        partial = ContinuityFixture(Path(self.temp.name) / "partial")
        _, partial_receipt = partial.checkpoint()
        partial_path = Path(partial_receipt.path)
        partial_path.write_bytes(b'{"schema":')
        os.chmod(partial_path, 0o600)
        self.assert_code("invalid-json", lambda: pc.load_checkpoint_envelope(partial_path, checkpoint_root=partial.checkpoints))

        racing = ContinuityFixture(Path(self.temp.name) / "race")

        def attempt() -> str:
            try:
                racing.checkpoint()
                return "created"
            except pc.ContinuityError as exc:
                return exc.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(results, ["created", "checkpoint-exists"])

    def test_verification_requires_exact_fixed_ten_assertions(self) -> None:
        _, receipt = self.fx.checkpoint()
        envelope = pc.load_checkpoint_envelope(Path(receipt.path), checkpoint_root=self.fx.checkpoints)
        report = pc.build_verification_report(envelope=envelope, candidate_evidence=self.fx.candidate(envelope))
        self.assertEqual(tuple(item.assertion_id for item in report.assertions), pc.VERIFICATION_ASSERTION_IDS)
        report_path = self.fx.root / "report.json"
        report_path.write_bytes(json_bytes(pc._plain(report)))
        os.chmod(report_path, 0o600)
        self.assertEqual(pc.load_verification_report(report_path).report_sha256, report.report_sha256)
        bad = pc._plain(report)
        bad["assertions"] = bad["assertions"][:-1]  # type: ignore[index]
        bad["report_sha256"] = hashlib.sha256(json_bytes({key: value for key, value in bad.items() if key != "report_sha256"})).hexdigest()  # type: ignore[union-attr,index]
        report_path.write_bytes(json_bytes(bad))
        self.assert_code("verification-assertions", lambda: pc.load_verification_report(report_path))

    def test_verification_refuses_context_drift_and_degrades_missing_candidate_proof(self) -> None:
        _, receipt = self.fx.checkpoint()
        envelope = pc.load_checkpoint_envelope(Path(receipt.path), checkpoint_root=self.fx.checkpoints)
        candidate = self.fx.candidate(envelope)
        manifest, policy = self.fx.load()
        store = pc.ContinuitySourceStore(repo_root=self.fx.repo, state_root=self.fx.state, source_policy=policy)
        actor = policy.actor_context("consultant", pc.ContinuityPurpose.HYDRATE_RUNTIME)
        resolver = pc.ContinuityResolver(store, checkpoint_root=self.fx.checkpoints)

        for field, value in (
            ("persona_id", "other"),
            ("role_id", "repo:other-role.json"),
            ("current_commitment_refs", ()),
            ("authority_limits", ("authority:other",)),
            ("checkpoint_sha256", "0" * 64),
            ("descriptor_sha256", "0" * 64),
            ("communication_ok", False),
            ("external_actions_disabled", False),
            ("old_embodiment_sole_authority", False),
            ("variance_evidence_sha256", None),
        ):
            with self.subTest(field=field):
                result = resolver.verify_checkpoint(
                    receipt,
                    manifest,
                    candidate_evidence=dataclasses.replace(candidate, **{field: value}),
                    actor=actor,
                    purpose="hydrate-runtime",
                )
                self.assertEqual(result.status, "degraded")
                self.assertEqual(sum(not item.passed for item in result.report.assertions), 1)  # type: ignore[union-attr]

        self.fx.manifest_data["provenance"]["source_revision"] = "changed"  # type: ignore[index]
        self.fx.write_manifest()
        changed_manifest, _ = self.fx.load()
        changed = resolver.verify_checkpoint(receipt, changed_manifest, candidate_evidence=candidate, actor=actor, purpose="hydrate-runtime")
        self.assertEqual((changed.status, changed.code), ("refused", "descriptor-drift"))

    def test_verification_role_is_bound_to_governed_role_ref_not_runtime_claim(self) -> None:
        view = self.fx.resolve()
        _, policy = self.fx.load()
        actor = policy.actor_context("consultant", pc.ContinuityPurpose.HYDRATE_RUNTIME)
        resolver = pc.ContinuityResolver(None, checkpoint_root=self.fx.checkpoints, source_policy=policy)
        receipt = resolver.checkpoint(
            view,
            boundary_id="boundary-1",
            runtime_observation=self.fx.observation(role_identification="fabricated-role"),
            content=self.fx.content(),
            boundary=self.fx.boundary(),
            producer_revision="r",
            actor=actor,
            purpose="hydrate-runtime",
        )
        envelope = pc.load_checkpoint_envelope(Path(receipt.path), checkpoint_root=self.fx.checkpoints)
        report = pc.build_verification_report(envelope=envelope, candidate_evidence=self.fx.candidate(envelope))
        role = next(item for item in report.assertions if item.assertion_id == "role-authority-boundary")
        self.assertTrue(role.passed)
        wrong = pc.build_verification_report(
            envelope=envelope,
            candidate_evidence=self.fx.candidate(envelope, role_id="repo:fabricated-role.json"),
        )
        self.assertFalse(next(item for item in wrong.assertions if item.assertion_id == "role-authority-boundary").passed)

    def test_checkpoint_refuses_cross_actor_view_replay_and_boundary_author(self) -> None:
        view = self.fx.resolve()
        _, policy = self.fx.load()
        actor = policy.actor_context("consultant", pc.ContinuityPurpose.HYDRATE_RUNTIME)
        resolver = pc.ContinuityResolver(None, checkpoint_root=self.fx.checkpoints, source_policy=policy)
        verify_actor = policy.actor_context("consultant", pc.ContinuityPurpose.VERIFY_CANDIDATE)
        self.assert_code(
            "view-replay-refused",
            lambda: resolver.checkpoint(view, boundary_id="boundary-1", runtime_observation=self.fx.observation(), content=self.fx.content(), boundary=self.fx.boundary(), producer_revision="r", actor=verify_actor, purpose="verify-candidate"),
        )
        self.assert_code(
            "checkpoint-actor-mismatch",
            lambda: resolver.checkpoint(view, boundary_id="boundary-1", runtime_observation=self.fx.observation(), content=self.fx.content(), boundary=self.fx.boundary(author="other"), producer_revision="r", actor=actor, purpose="hydrate-runtime"),
        )
        drifted_policy = dataclasses.replace(policy, catalog_revision="p2")
        drifted_resolver = pc.ContinuityResolver(None, checkpoint_root=self.fx.checkpoints, source_policy=drifted_policy)
        self.assert_code(
            "view-replay-refused",
            lambda: drifted_resolver.checkpoint(view, boundary_id="boundary-1", runtime_observation=self.fx.observation(), content=self.fx.content(), boundary=self.fx.boundary(), producer_revision="r", actor=actor, purpose="hydrate-runtime"),
        )

    def test_legacy_session_id_is_runtime_evidence_not_continuity(self) -> None:
        path = self.fx.state / "legacy.json"
        path.write_bytes(json_bytes({"persona_id": "consultant", "session_id": "s-1", "harness": "old"}))
        os.chmod(path, 0o600)
        evidence = pc.classify_legacy_session_state(path, persona_id="consultant", state_root=self.fx.state)
        self.assertEqual(evidence.classification, pc.SourceClassification.RUNTIME_EVIDENCE)
        self.assertTrue(evidence.cache_only)
        self.assertFalse(evidence.can_satisfy_required_ref)
        self.assertEqual(evidence.session_id_sha256, hashlib.sha256(b"s-1").hexdigest())
        self.assertNotIn("s-1", json.dumps(pc._plain(evidence)))

    def test_legacy_migration_is_additive_idempotent_and_cache_only(self) -> None:
        path = self.fx.state / "legacy.json"
        path.write_bytes(json_bytes({"persona_id": "consultant", "session_id": "s-1"}))
        os.chmod(path, 0o600)
        output = self.fx.root / "migration"
        first = pc.migrate_legacy_session_state(path, persona_id="consultant", state_root=self.fx.state, output_root=output)
        second = pc.migrate_legacy_session_state(path, persona_id="consultant", state_root=self.fx.state, output_root=output)
        self.assertEqual((first.status, second.status), ("created", "idempotent"))
        migrated = json.loads(Path(first.output_path).read_text())
        self.assertFalse(migrated["can_satisfy_required_ref"])
        self.assertNotIn("s-1", Path(first.output_path).read_text())
        self.assertEqual(Path(first.output_path).stem, first.source_sha256)

    def test_legacy_migration_concurrent_publish_is_atomic_and_idempotent(self) -> None:
        path = self.fx.state / "legacy.json"
        path.write_bytes(json_bytes({"persona_id": "consultant", "session_id": "private-session"}))
        os.chmod(path, 0o600)
        output = self.fx.root / "migration-race"

        def migrate() -> pc.LegacyMigrationReceipt:
            return pc.migrate_legacy_session_state(path, persona_id="consultant", state_root=self.fx.state, output_root=output)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            receipts = list(executor.map(lambda _: migrate(), range(2)))
        self.assertCountEqual([item.status for item in receipts], ["created", "idempotent"])
        published = Path(receipts[0].output_path)
        self.assertNotIn("private-session", published.read_text())
        self.assertFalse(any(published.parent.glob(f".{receipts[0].source_sha256}.*")))

    def test_blank_responsive_model_is_not_ready(self) -> None:
        result = pc.assess_hydration_readiness(self.fx.resolve(), self.fx.observation(self_identification="", role_identification=""))
        self.assertEqual(result.status, "degraded")
        self.assertIn("runtime-not-ready", result.issues)

    def test_missing_required_identity_ref_is_unavailable(self) -> None:
        self.fx.sources["repo:identity.json"].unlink()
        self.assert_code("source-unavailable", self.fx.resolve)

    def test_authority_mismatch_is_degraded_before_smoke(self) -> None:
        result = pc.assess_hydration_readiness(self.fx.resolve(), self.fx.observation(authority_limits=("authority:other",)))
        self.assertEqual(result.status, "degraded")
        self.assertIn("authority-mismatch", result.issues)

    def test_hydration_reports_behavioral_variance_separately(self) -> None:
        data = pc._plain(self.fx.resolve())
        data["behavioral_variance"] = ["tone-shift"]  # type: ignore[index]
        data.pop("view_sha256")  # type: ignore[union-attr]
        data["view_sha256"] = hashlib.sha256(json_bytes(data)).hexdigest()  # type: ignore[index]
        view = pc._resolved_view_from_obj(data)
        result = pc.assess_hydration_readiness(view, self.fx.observation())
        self.assertEqual(result.status, "ready")
        self.assertEqual(view.behavioral_variance, ("tone-shift",))

    def test_cli_resolve_checkpoint_verify_are_real_subprocess_bounded_json(self) -> None:
        view_path = self.fx.root / "view.json"
        resolve = subprocess.run([sys.executable, str(CLI), "resolve", "--manifest", str(self.fx.manifest), "--descriptor-root", str(self.fx.descriptors), "--repo-root", str(self.fx.repo), "--state-root", str(self.fx.state), "--source-policy", str(self.fx.policy), "--checkpoint-root", str(self.fx.checkpoints), "--actor-id", "consultant", "--purpose", "hydrate-runtime", "--output", str(view_path), "--json"], text=True, capture_output=True, check=False)
        self.assertEqual(resolve.returncode, 0, resolve.stderr)
        self.assertLessEqual(len(resolve.stdout.encode()), pc.MAX_CLI_OUTPUT_BYTES)
        self.assertEqual(json.loads(resolve.stdout)["schema"], pc.VIEW_SCHEMA_ID)
        content = self.fx.root / "content.json"
        boundary = self.fx.root / "boundary.json"
        observation = self.fx.root / "observation.json"
        content.write_bytes(json_bytes(pc._plain(self.fx.content())))
        boundary.write_bytes(json_bytes(pc._plain(self.fx.boundary())))
        observation.write_bytes(json_bytes(pc._plain(self.fx.observation())))
        for path in (content, boundary, observation):
            os.chmod(path, 0o600)
        checkpoint = subprocess.run([sys.executable, str(CLI), "checkpoint", "--view", str(view_path), "--content", str(content), "--boundary", str(boundary), "--runtime-observation", str(observation), "--checkpoint-root", str(self.fx.checkpoints), "--source-policy", str(self.fx.policy), "--actor-id", "consultant", "--purpose", "hydrate-runtime", "--producer-revision", "test", "--json"], text=True, capture_output=True, check=False)
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)
        receipt = json.loads(checkpoint.stdout)
        receipt_path = self.fx.root / "receipt.json"
        receipt_path.write_bytes(json_bytes(receipt))
        os.chmod(receipt_path, 0o600)
        envelope = pc.load_checkpoint_envelope(Path(receipt["path"]), checkpoint_root=self.fx.checkpoints)
        candidate_path = self.fx.root / "candidate.json"
        candidate_path.write_bytes(json_bytes(pc._plain(self.fx.candidate(envelope))))
        os.chmod(candidate_path, 0o600)
        verify = subprocess.run([sys.executable, str(CLI), "verify", "--receipt", str(receipt_path), "--manifest", str(self.fx.manifest), "--descriptor-root", str(self.fx.descriptors), "--checkpoint-root", str(self.fx.checkpoints), "--repo-root", str(self.fx.repo), "--state-root", str(self.fx.state), "--source-policy", str(self.fx.policy), "--candidate-evidence", str(candidate_path), "--actor-id", "consultant", "--purpose", "hydrate-runtime", "--json"], text=True, capture_output=True, check=False)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertEqual(json.loads(verify.stdout)["status"], "ready")

    def test_cli_refusal_is_bounded_and_does_not_mutate_output(self) -> None:
        before = {str(path): path.read_bytes() for path in self.fx.root.rglob("*") if path.is_file()}
        proc = subprocess.run([sys.executable, str(CLI), "switch-apply", "--json"], text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 78)
        self.assertLessEqual(len(proc.stdout.encode()), pc.MAX_CLI_OUTPUT_BYTES)
        self.assertEqual(json.loads(proc.stdout)["status"], "refused")
        after = {str(path): path.read_bytes() for path in self.fx.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_cli_refuses_oversized_input_and_bounds_stderr_without_output_mutation(self) -> None:
        output = self.fx.root / "view.json"
        output.write_bytes(b"prior\n")
        os.chmod(output, 0o600)
        self.fx.manifest.write_bytes(b"x" * (pc.MAX_BUNDLE_BYTES + 1))
        os.chmod(self.fx.manifest, 0o600)
        proc = subprocess.run(
            [sys.executable, str(CLI), "resolve", "--manifest", str(self.fx.manifest), "--descriptor-root", str(self.fx.descriptors), "--repo-root", str(self.fx.repo), "--state-root", str(self.fx.state), "--source-policy", str(self.fx.policy), "--checkpoint-root", str(self.fx.checkpoints), "--actor-id", "consultant", "--purpose", "hydrate-runtime", "--output", str(output), "--json"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertLessEqual(len(proc.stdout.encode()), pc.MAX_CLI_OUTPUT_BYTES)
        self.assertLessEqual(len(proc.stderr.encode()), 4097)
        self.assertEqual(output.read_bytes(), b"prior\n")

    def _write_inventory_envelopes(self) -> Path:
        _, receipt = self.fx.checkpoint()
        envelope = pc.load_checkpoint_envelope(Path(receipt.path), checkpoint_root=self.fx.checkpoints)
        report = pc.build_verification_report(envelope=envelope, candidate_evidence=self.fx.candidate(envelope))
        report_path = self.fx.checkpoints / "verification.json"
        report_path.write_bytes(json_bytes(pc._plain(report)))
        os.chmod(report_path, 0o600)
        return report_path

    def test_snapshot_export_inventory_includes_continuity_envelopes(self) -> None:
        self._write_inventory_envelopes()
        inventory = pc.build_snapshot_export_inventory(self.fx.checkpoints)
        self.assertEqual({entry.schema for entry in inventory.entries}, {pc.CHECKPOINT_SCHEMA_ID, pc.VERIFICATION_SCHEMA_ID})
        output = self.fx.root / "history" / "continuity-inventory.json"
        pc.write_snapshot_export_inventory(inventory, output)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_snapshot_export_inventory_refuses_symlink_traversal_owner_mode_faults(self) -> None:
        report = self._write_inventory_envelopes()
        link = self.fx.checkpoints / "escape.json"
        link.symlink_to(report)
        self.assert_code("inventory-symlink", lambda: pc.build_snapshot_export_inventory(self.fx.checkpoints))
        link.unlink()
        os.chmod(report, 0o666)
        self.assert_code("unsafe-mode", lambda: pc.build_snapshot_export_inventory(self.fx.checkpoints))
        self.assert_code("owner-mismatch", lambda: pc.build_snapshot_export_inventory(self.fx.checkpoints, expected_uid=os.getuid() + 1))

        separate = ContinuityFixture(Path(self.temp.name) / "inventory-root")
        separate.checkpoints.mkdir(mode=0o700)
        actual = separate.checkpoints.with_name("actual-checkpoints")
        separate.checkpoints.rename(actual)
        separate.checkpoints.symlink_to(actual, target_is_directory=True)
        self.assert_code("symlink-root-forbidden", lambda: pc.build_snapshot_export_inventory(separate.checkpoints))

    def test_roots_refuse_group_mutation_and_checkpoint_symlink_ancestor(self) -> None:
        os.chmod(self.fx.repo, 0o770)
        self.assert_code("unsafe-mode", self.fx.resolve)
        os.chmod(self.fx.repo, 0o700)
        os.chmod(self.fx.descriptors, 0o770)
        self.assert_code("unsafe-mode", self.fx.load)
        os.chmod(self.fx.descriptors, 0o700)

        view = self.fx.resolve()
        _, policy = self.fx.load()
        actor = policy.actor_context("consultant", pc.ContinuityPurpose.HYDRATE_RUNTIME)
        real = self.fx.root / "real-checkpoint-parent"
        real.mkdir(mode=0o700)
        link = self.fx.root / "checkpoint-parent-link"
        link.symlink_to(real, target_is_directory=True)
        resolver = pc.ContinuityResolver(None, checkpoint_root=link / "checkpoints", source_policy=policy)
        self.assert_code(
            "symlink-ancestor-forbidden",
            lambda: resolver.checkpoint(view, boundary_id="boundary-1", runtime_observation=self.fx.observation(), content=self.fx.content(), boundary=self.fx.boundary(), producer_revision="r", actor=actor, purpose="hydrate-runtime"),
        )

    def test_snapshot_inventory_transaction_preserves_prior_on_failure(self) -> None:
        self._write_inventory_envelopes()
        inventory = pc.build_snapshot_export_inventory(self.fx.checkpoints)
        output = self.fx.root / "history" / "continuity-inventory.json"
        output.parent.mkdir()
        os.chmod(output.parent, 0o700)
        output.write_bytes(b"prior\n")
        os.chmod(output, 0o644)
        self.assert_code("inventory-output-unsafe", lambda: pc.write_snapshot_export_inventory(inventory, output))
        self.assertEqual(output.read_bytes(), b"prior\n")

    def test_side_effecting_switch_commands_are_typed_refusals_or_read_only_until_t3177(self) -> None:
        for command in ("switch-apply", "switch-rollback"):
            proc = subprocess.run([sys.executable, str(CLI), command, "--json"], text=True, capture_output=True, check=False)
            self.assertEqual(proc.returncode, 78)
            body = json.loads(proc.stdout)
            self.assertEqual(body["delegated_to"], "T-3177")
            self.assertFalse(body["side_effects"])

    def test_switch_receipt_schema_and_validator_match_registered_contract(self) -> None:
        base: dict[str, object] = {
            "schema": pc.SWITCH_SCHEMA_ID,
            "receipt_id": "receipt-1",
            "persona_id": "consultant",
            "approved_plan_sha256": "1" * 64,
            "actor_id": "consultant",
            "reviewed_source_revision": "r1",
            "old_runtime_observation": pc._plain(self.fx.observation()),
            "candidate_runtime_observation": pc._plain(self.fx.observation(runtime_id="candidate-1")),
            "checkpoint_sha256": "2" * 64,
            "verification_sha256": "3" * 64,
            "compatibility_result": "compatible",
            "old_receiver_isolation_sha256": "4" * 64,
            "candidate_receiver_isolation_sha256": "5" * 64,
            "outcome": "committed",
            "manifest_revision": "r1",
            "controller_revision": "r1",
            "prior_receipt_sha256": None,
        }
        body = dict(base, receipt_sha256=hashlib.sha256(json_bytes(base)).hexdigest())
        path = self.fx.root / "switch-receipt.json"
        path.write_bytes(json_bytes(body))
        os.chmod(path, 0o600)
        self.assertEqual(pc.validate_switch_receipt_file(path).receipt_id, "receipt-1")
        proc = subprocess.run([sys.executable, str(CLI), "switch-plan", "--receipt", str(path), "--json"], text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["receipt_sha256"], body["receipt_sha256"])


class PublicationErrorTranslationTests(unittest.TestCase):
    """T-3459 fault-injection tests: _publish_immutable_file's error-translation
    completeness gap surfaced by T-3176's terminal reviewer. Every publication-
    boundary OSError must translate to ContinuityError("publication-failed",
    ...) so the CLI's typed-refusal contract holds; cleanup failures must NOT
    replace the primary typed error.

    Guard-scope for Amendment J: these tests bind to the OSError-translation
    branch added in _publish_immutable_file. The mutation gate should target
    the new `except OSError as exc:` and `except ContinuityError:` clauses +
    the finally-block broadened cleanup catches (git diff HEAD~1 for the
    canonical scope).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="t3459-")
        self.dir = Path(self._tmp) / "publish"

    def tearDown(self) -> None:
        subprocess.run(["chmod", "-R", "u+rwX", self._tmp], check=False)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _publish(self) -> bool:
        return pc._publish_immutable_file(
            self.dir,
            "target.json",
            b'{"k":"v"}\n',
            max_bytes=1024,
            exists_code="already-exists",
            conflict_code="publication-conflict",
        )

    def _assert_typed(self, err_cls: type, expected_code: str, callable_):
        with self.assertRaises(err_cls) as cm:
            callable_()
        self.assertEqual(cm.exception.code, expected_code)
        return cm.exception

    # --- happy path baseline ---------------------------------------------
    def test_baseline_publish_returns_true(self) -> None:
        self.assertTrue(self._publish())
        self.assertEqual((self.dir / "target.json").read_bytes(), b'{"k":"v"}\n')

    # --- fault-injection: each OS-boundary raises OSError → ContinuityError ---
    def test_temp_open_oserror_translates(self) -> None:
        real_open = os.open
        first = {"seen": False}

        def bad_open(*args, **kwargs):
            # Second os.open call is the O_CREAT|O_EXCL temp; first is the
            # directory-fd open. Fail on the temp create.
            if not first["seen"] and "dir_fd" in kwargs and (kwargs.get("mode") == 0o600 or (len(args) >= 3 and args[2] == 0o600)):
                first["seen"] = True
                raise PermissionError(errno.EACCES, "denied")
            return real_open(*args, **kwargs)

        with mock.patch("os.open", side_effect=bad_open):
            exc = self._assert_typed(pc.ContinuityError, "publication-failed", self._publish)
        self.assertEqual(exc.status, "unavailable")
        self.assertIn("EACCES", exc.detail)
        self.assertIsInstance(exc.__cause__, PermissionError)

    def test_write_oserror_translates(self) -> None:
        real_write = os.write

        def bad_write(fd, data):
            raise OSError(errno.ENOSPC, "disk full")

        with mock.patch("os.write", side_effect=bad_write):
            exc = self._assert_typed(pc.ContinuityError, "publication-failed", self._publish)
        self.assertEqual(exc.status, "unavailable")
        self.assertIn("ENOSPC", exc.detail)

    def test_fsync_oserror_translates(self) -> None:
        real_fsync = os.fsync
        counts = {"n": 0}

        def bad_fsync(fd):
            counts["n"] += 1
            if counts["n"] == 1:  # first fsync = temp file fsync
                raise OSError(errno.EIO, "I/O error")
            return real_fsync(fd)

        with mock.patch("os.fsync", side_effect=bad_fsync):
            exc = self._assert_typed(pc.ContinuityError, "publication-failed", self._publish)
        self.assertIn("EIO", exc.detail)

    def test_link_oserror_non_fileexists_translates(self) -> None:
        real_link = os.link

        def bad_link(*args, **kwargs):
            raise OSError(errno.EXDEV, "cross-device link")

        with mock.patch("os.link", side_effect=bad_link):
            exc = self._assert_typed(pc.ContinuityError, "publication-failed", self._publish)
        self.assertIn("EXDEV", exc.detail)

    def test_final_open_readback_oserror_translates(self) -> None:
        real_open = os.open

        def bad_open(*args, **kwargs):
            # Fire on the second-to-last os.open call (the final RDONLY
            # readback of the published file). Distinguish it by O_RDONLY
            # + dir_fd + no O_CREAT.
            flags = args[1] if len(args) > 1 else kwargs.get("flags", 0)
            is_rdonly_readback = (
                (flags & os.O_RDONLY == 0)  # O_RDONLY == 0, so this checks not-writable
                and "dir_fd" in kwargs
                and not (flags & os.O_CREAT)
                and args[0] == "target.json"
            )
            if is_rdonly_readback:
                raise OSError(errno.EIO, "readback I/O")
            return real_open(*args, **kwargs)

        with mock.patch("os.open", side_effect=bad_open):
            exc = self._assert_typed(pc.ContinuityError, "publication-failed", self._publish)
        self.assertIn("EIO", exc.detail)

    def test_directory_fsync_oserror_translates(self) -> None:
        real_fsync = os.fsync
        counts = {"n": 0}

        def bad_fsync(fd):
            counts["n"] += 1
            if counts["n"] == 2:  # second fsync = the directory-fd fsync
                raise OSError(errno.EIO, "dir fsync")
            return real_fsync(fd)

        with mock.patch("os.fsync", side_effect=bad_fsync):
            exc = self._assert_typed(pc.ContinuityError, "publication-failed", self._publish)
        self.assertIn("EIO", exc.detail)

    # --- cleanup-failure invariant: primary error wins -------------------
    def test_cleanup_failure_does_not_replace_primary_error(self) -> None:
        """When the primary error is OSError (publication-failed) AND the
        finally-block cleanup ALSO raises OSError, the caller sees the
        primary publication-failed ContinuityError — not the cleanup error.
        """
        real_write = os.write
        real_unlink = os.unlink

        def bad_write(fd, data):
            raise OSError(errno.ENOSPC, "disk full")

        def bad_unlink(*args, **kwargs):
            # Fail every unlink (cleanup) — must not surface
            raise PermissionError(errno.EPERM, "cleanup denied")

        with mock.patch("os.write", side_effect=bad_write), \
             mock.patch("os.unlink", side_effect=bad_unlink):
            exc = self._assert_typed(pc.ContinuityError, "publication-failed", self._publish)
        # Primary error is publication-failed (from write), NOT anything about the cleanup unlink
        self.assertIn("ENOSPC", exc.detail)
        self.assertIsInstance(exc.__cause__, OSError)
        self.assertEqual(exc.__cause__.errno, errno.ENOSPC)

    def test_cleanup_failure_preserves_typed_continuity_error(self) -> None:
        """When the primary error is already a ContinuityError (e.g.
        readback-drift) AND cleanup fails, the caller sees the primary
        ContinuityError unchanged — not the cleanup error.
        """
        # Corrupt the readback by returning wrong bytes.
        real_read = pc._read_regular_fd

        def bad_read(fd, *, max_bytes):
            return b"WRONG"  # drift → publication-readback-drift

        def bad_unlink(*args, **kwargs):
            raise PermissionError(errno.EPERM, "cleanup denied")

        with mock.patch.object(pc, "_read_regular_fd", side_effect=bad_read), \
             mock.patch("os.unlink", side_effect=bad_unlink):
            exc = self._assert_typed(pc.ContinuityError, "publication-readback-drift", self._publish)
        # Primary code is the original readback-drift, unchanged by cleanup failure
        self.assertEqual(exc.code, "publication-readback-drift")

    # --- unchanged-destination guarantee: cleanup unlinks the final file ---
    # (Amendment J §J6(i) closure — REBOUND 1/3 remediation. Reviewer picked
    # lines 1469 [except ContinuityError:] and 1490 [os.unlink in OSError
    # cleanup] as real test-binding gaps for the "unchanged-destination"
    # guarantee named verbatim in T-3176 terminal-reviewer annotation
    # 2c87c2deec + T-3459 ticket ask. Probes below observe the on-disk
    # side-effect the cleanup branches produce.)

    def test_post_publish_continuity_error_removes_final_file(self) -> None:
        """After a post-publish ContinuityError (readback-drift on the FINAL
        file, not the temp file), the just-linked destination file MUST be
        unlinked — otherwise a caller receiving a typed refusal would still
        find drifted content at the canonical path. Binds line 1469's
        `except ContinuityError:` cleanup branch.
        """
        calls = {"n": 0}

        def flaky_read(fd, *, max_bytes):
            calls["n"] += 1
            if calls["n"] == 1:
                return b'{"k":"v"}\n'  # temp-file readback: OK, proceed to link
            return b"WRONG"  # final-file readback: drift → post-publish ContinuityError

        with mock.patch.object(pc, "_read_regular_fd", side_effect=flaky_read):
            with self.assertRaises(pc.ContinuityError) as cm:
                self._publish()
        self.assertEqual(cm.exception.code, "publication-readback-drift")
        self.assertFalse(
            (self.dir / "target.json").exists(),
            "final file must be unlinked after a post-publish ContinuityError",
        )

    def test_post_publish_oserror_removes_final_file(self) -> None:
        """After a post-publish OSError (directory-fsync fails AFTER link
        succeeded), the just-linked destination file MUST be unlinked. Binds
        line 1490's `os.unlink(filename, ...)` in the `except OSError as exc:`
        cleanup branch.
        """
        real_fsync = os.fsync
        counts = {"n": 0}

        def bad_fsync(fd):
            counts["n"] += 1
            if counts["n"] == 2:  # directory-fd fsync, the 2nd fsync call, AFTER publish
                raise OSError(errno.EIO, "dir fsync")
            return real_fsync(fd)

        with mock.patch("os.fsync", side_effect=bad_fsync):
            with self.assertRaises(pc.ContinuityError) as cm:
                self._publish()
        self.assertEqual(cm.exception.code, "publication-failed")
        self.assertFalse(
            (self.dir / "target.json").exists(),
            "final file must be unlinked after a post-publish OSError translation",
        )

    # --- existing conflict/existence path still works --------------------
    def test_existing_file_conflict_code_still_raised(self) -> None:
        """Regression guard: the existing FileExistsError → conflict/exists
        translation is preserved (not swallowed by the broadened OSError
        catch). Same bytes → False return (conflict-with-identical); different
        bytes → publication-conflict ContinuityError.
        """
        self.assertTrue(self._publish())  # baseline
        # Same bytes, conflict_code path → False return
        self.assertFalse(self._publish())
        # Different bytes → publication-conflict raised
        def _diff():
            return pc._publish_immutable_file(
                self.dir, "target.json", b'{"k":"different"}\n',
                max_bytes=1024, exists_code="already-exists",
                conflict_code="publication-conflict",
            )
        exc = self._assert_typed(pc.ContinuityError, "publication-conflict", _diff)


if __name__ == "__main__":
    unittest.main()
