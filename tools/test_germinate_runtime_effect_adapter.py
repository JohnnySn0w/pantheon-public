from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from tools import runtime_controller as rc
from tools.germinate import runtime_effect_adapter as rea
from tools.germinate.planner_sandbox.wal import HeldDir, StructuralPrefix, WalStore, WalStoreError
from tools.harness_adapter.types import (
    CommandSpec,
    FailoverPolicy,
    HarnessLaunchConfig,
    RuntimeControllerConfig,
    TmuxTarget,
)
from tools.persona_bringup import PersonaBinding


FIXED_TIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class _Adapter:
    def build_interactive_launch(self, target, *, cwd):
        return CommandSpec(argv=("/bin/true",), stdin=None, cwd=cwd, env={})


def _binding(root: Path, *, persona: str = "recovery", role: str = "recovery") -> PersonaBinding:
    return PersonaBinding(
        role=role,
        persona=persona,
        account="codex",
        configured_harness="codex",
        selected_harness="codex",
        account_home=root / "home",
        pinned_rules=root / "AGENTS.md",
        mailbox=root / "mailbox",
        launch_cwd=root,
        launch=HarnessLaunchConfig(
            harness="codex",
            profile="interactive",
            executable=Path("/usr/bin/codex"),
            model="gpt-5",
            mode="interactive",
            meter="subscription",
            quota_source="unavailable",
            native={},
            sources=(),
        ),
        controller=RuntimeControllerConfig(
            tmux=TmuxTarget("pantheon-recovery", "harness", "0"),
            policy=FailoverPolicy(weekly_remaining_percent=0.0, fallbacks=()),
        ),
    )


def _wal_store(root: Path) -> WalStore:
    run_dir = root / "wal"
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
    store = WalStore.from_fd(fd, run_key="run", expected_uid=os.getuid())
    os.close(fd)
    store.initialize_manifest(created_at="2026-01-02T03:04:05Z", updated_at="2026-01-02T03:04:05Z")
    return store


def _runtime_path(root: Path, binding: PersonaBinding) -> Path:
    return root / binding.persona / "runtime-controller" / "runtime.json"


def _write_runtime(
    root: Path,
    binding: PersonaBinding,
    *,
    session_id: str | None = "session-1",
    started_at: datetime = FIXED_TIME,
    overrides: dict[str, object] | None = None,
    raw: bytes | None = None,
) -> bytes:
    path = _runtime_path(root, binding)
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is None:
        payload = rc._runtime_payload(
            binding,
            session_id=session_id,
            started_at=started_at,
            runtime_scope_unit=rc.TMUX_RUNTIME_SCOPE_UNIT,
        )
        if overrides:
            payload.update(overrides)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    return raw


def _probe(binding: PersonaBinding, root: Path, **kwargs) -> rea.RuntimeControllerProbe:
    defaults = {
        "pane_exists_fn": lambda target, *, context=None: True,
        "pane_pid_fn": lambda target, *, context=None: 4242,
        "pid_alive_fn": lambda pid: True,
        "server_pid_fn": lambda *, context=None: 1234,
        "pid_in_scope_fn": lambda pid, scope: True,
        "capture_pane_fn": lambda target, *, context=None: "ready pane",
        "readiness_fn": lambda text: True,
        "now": lambda: FIXED_TIME,
    }
    defaults.update(kwargs)
    return rea.RuntimeControllerProbe(binding, state_root=root, **defaults)


def _identity(binding: PersonaBinding, root: Path) -> rea.RuntimeEffectIdentity:
    return rea.expected_runtime_identity(binding, state_root=root)


def _apply_live(store: WalStore, binding: PersonaBinding, root: Path) -> rea.RuntimeEffectResult:
    _write_runtime(root, binding)
    return rea.apply_ensure_runtime_effect(
        store,
        binding,
        _Adapter(),
        state_root=root,
        ensure_runtime_fn=lambda binding, adapter, *, state_root=None, context=None: rc.RuntimeHandle(
            binding.launch, binding.controller.tmux, "session-1", FIXED_TIME, pid=4242
        ),
        probe=_probe(binding, root),
        now=lambda: FIXED_TIME,
    )


def test_runtime_effect_intent_binds_observable_identity_before_start(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _wal_store(tmp_path)
    calls = []

    def provider(binding, adapter, *, state_root=None, context=None):
        calls.append("provider")
        identity = _identity(binding, state_root)
        intent, _digest = store.read_intent(identity.wal_op_id)
        assert intent.preimage.payload is not None
        assert intent.preimage.payload["operation"] == "tools.runtime_controller.ensure_runtime"
        assert intent.preimage.payload["wal_op_id"] == identity.wal_op_id
        assert intent.preimage.payload["persona"] == "recovery"
        assert intent.preimage.payload["role"] == "recovery"
        assert intent.preimage.payload["account"] == "codex"
        assert intent.preimage.payload["tmux_target"] == "pantheon-recovery:harness.0"
        assert intent.preimage.payload["runtime_scope_unit"] == rc.TMUX_RUNTIME_SCOPE_UNIT
        assert intent.preimage.payload["runtime_state_path_sha256"] == identity.runtime_state_path_sha256
        _write_runtime(state_root, binding)
        return rc.RuntimeHandle(binding.launch, binding.controller.tmux, "session-1", FIXED_TIME, pid=4242)

    result = rea.apply_ensure_runtime_effect(
        store,
        binding,
        _Adapter(),
        state_root=tmp_path,
        ensure_runtime_fn=provider,
        probe=_probe(binding, tmp_path),
        now=lambda: FIXED_TIME,
    )

    assert calls == ["provider"]
    assert result.decision is rea.RuntimeEffectDecision.COMPLETE
    assert result.intent_ref is not None
    assert result.receipt_ref is not None
    assert result.manifest_entry is not None


def test_runtime_effect_drives_ensure_runtime_public_path_and_records_completion(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _wal_store(tmp_path)
    calls = []

    def public_provider(binding, adapter, *, state_root=None, context=None):
        calls.append((binding.persona, state_root))
        _write_runtime(state_root, binding, session_id=None)
        return rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, FIXED_TIME, pid=4242)

    with mock.patch.object(rc, "ensure_runtime", side_effect=public_provider):
        result = rea.apply_ensure_runtime_effect(
            store,
            binding,
            _Adapter(),
            state_root=tmp_path,
            probe=_probe(binding, tmp_path),
            now=lambda: FIXED_TIME,
        )

    assert calls == [("recovery", tmp_path)]
    assert result.decision is rea.RuntimeEffectDecision.COMPLETE
    assert result.handle is not None
    assert result.handle.session_id is None
    assert store.read_manifest().receipts


def test_runtime_effect_real_probe_reads_exact_provider_bytes_and_identity(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    raw = _write_runtime(tmp_path, binding, session_id=None)
    observation = _probe(binding, tmp_path).observe_identity(_identity(binding, tmp_path))

    assert observation.status is rea.RuntimeEffectStatus.LIVE_MATCHING
    assert observation.runtime_state_bytes_sha256 == hashlib.sha256(raw).hexdigest()
    assert observation.observed_identity == rea.ObservedRuntimeIdentity(
        schema="pantheon-runtime-state/v1",
        persona="recovery",
        role="recovery",
        harness="codex",
        profile="interactive",
        executable="/usr/bin/codex",
        model="gpt-5",
        tmux_target="pantheon-recovery:harness.0",
        runtime_scope_unit=rc.TMUX_RUNTIME_SCOPE_UNIT,
    )
    assert observation.session_id is None
    assert observation.parsed_started_at == FIXED_TIME
    assert observation.pane_sha256 == hashlib.sha256(b"ready pane").hexdigest()


def test_runtime_effect_real_probe_classifies_absent_malformed_and_strict_started_at(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    expected = _identity(binding, tmp_path)

    absent = _probe(binding, tmp_path).observe_identity(expected)
    assert absent.status is rea.RuntimeEffectStatus.NOT_OBSERVED
    assert absent.runtime_state_bytes_sha256 is None
    assert absent.observed_identity is None

    cases: list[bytes | None] = [
        b"{",
        b"[]",
        _write_runtime(tmp_path, binding, overrides={"schema": "wrong"}),
        _write_runtime(tmp_path, binding, overrides={"persona": 42}),
        _write_runtime(tmp_path, binding, overrides={"session_id": 42}),
        _write_runtime(tmp_path, binding, overrides={"started_at": "not-a-date"}),
        _write_runtime(tmp_path, binding, overrides={"started_at": "2026-01-02T03:04:05"}),
    ]
    for raw in cases:
        if raw is not None:
            _runtime_path(tmp_path, binding).write_bytes(raw)
        observed = _probe(binding, tmp_path, now=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)).observe_identity(expected)
        assert observed.status is rea.RuntimeEffectStatus.UNKNOWN
        assert observed.runtime_state_bytes_sha256 is None
        assert observed.observed_identity is None
        assert observed.parsed_started_at is None

    _runtime_path(tmp_path, binding).unlink()
    _runtime_path(tmp_path, binding).symlink_to(tmp_path / "elsewhere")
    linked = _probe(binding, tmp_path).observe_identity(expected)
    assert linked.status is rea.RuntimeEffectStatus.UNKNOWN


def test_runtime_effect_reuses_only_matching_live_runtime_identity(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    expected = _identity(binding, tmp_path)
    _write_runtime(tmp_path, binding)

    assert _probe(binding, tmp_path).observe_identity(expected).status is rea.RuntimeEffectStatus.LIVE_MATCHING
    assert _probe(binding, tmp_path, pane_exists_fn=lambda target, *, context=None: False).observe_identity(expected).status is rea.RuntimeEffectStatus.DEAD_MATCHING
    _write_runtime(tmp_path, binding, overrides={"persona": "other"})
    assert _probe(binding, tmp_path).observe_identity(expected).status is rea.RuntimeEffectStatus.LIVE_MISMATCHED
    assert _probe(binding, tmp_path, pane_exists_fn=lambda target, *, context=None: False).observe_identity(expected).status is rea.RuntimeEffectStatus.DEAD_MISMATCHED
    _write_runtime(tmp_path, binding)
    assert _probe(binding, tmp_path, pane_pid_fn=lambda target, *, context=None: None).observe_identity(expected).status is rea.RuntimeEffectStatus.PANE_PID_MISMATCH
    assert _probe(binding, tmp_path, pid_alive_fn=lambda pid: False).observe_identity(expected).status is rea.RuntimeEffectStatus.PANE_PID_MISMATCH
    assert _probe(binding, tmp_path, server_pid_fn=lambda *, context=None: None).observe_identity(expected).status is rea.RuntimeEffectStatus.SERVER_SCOPE_MISMATCH
    assert _probe(binding, tmp_path, pid_in_scope_fn=lambda pid, scope: False).observe_identity(expected).status is rea.RuntimeEffectStatus.SERVER_SCOPE_MISMATCH


def test_runtime_effect_completion_requires_fresh_probe_not_cached_receipt(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _wal_store(tmp_path)
    completed = _apply_live(store, binding, tmp_path)
    assert completed.decision is rea.RuntimeEffectDecision.COMPLETE

    _runtime_path(tmp_path, binding).unlink()
    result = rea.reconcile_runtime_effect(store, binding, state_root=tmp_path, probe=_probe(binding, tmp_path), now=lambda: FIXED_TIME)
    assert result.decision is rea.RuntimeEffectDecision.REFUSE
    assert result.prefix is StructuralPrefix.MANIFEST_REFERENCED


def test_runtime_effect_kill_before_intent_retries_without_completion(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _wal_store(tmp_path)
    child = _run_child("before_intent", tmp_path)
    assert child.returncode == 73

    identity = _identity(binding, tmp_path)
    with pytest.raises(WalStoreError):
        store.read_intent(identity.wal_op_id)
    assert not store.read_manifest().receipts

    provider_called = []
    result = rea.apply_ensure_runtime_effect(
        store,
        binding,
        _Adapter(),
        state_root=tmp_path,
        ensure_runtime_fn=lambda binding, adapter, *, state_root=None, context=None: (
            provider_called.append(True),
            _write_runtime(state_root, binding),
            rc.RuntimeHandle(binding.launch, binding.controller.tmux, "session-1", FIXED_TIME, pid=4242),
        )[-1],
        probe=_probe(binding, tmp_path),
        now=lambda: FIXED_TIME,
    )
    assert provider_called == [True]
    assert result.decision is rea.RuntimeEffectDecision.COMPLETE


def test_runtime_effect_kill_after_intent_before_provider_retries_from_probe(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _wal_store(tmp_path)
    child = _run_child("after_intent_before_provider", tmp_path)
    assert child.returncode == 73

    result = rea.reconcile_runtime_effect(store, binding, state_root=tmp_path, probe=_probe(binding, tmp_path), now=lambda: FIXED_TIME)
    assert result.decision is rea.RuntimeEffectDecision.RETRY
    assert result.prefix is StructuralPrefix.INTENT_ONLY

    provider_called = []
    applied = rea.apply_ensure_runtime_effect(
        store,
        binding,
        _Adapter(),
        state_root=tmp_path,
        ensure_runtime_fn=lambda binding, adapter, *, state_root=None, context=None: (
            provider_called.append(True),
            _write_runtime(state_root, binding),
            rc.RuntimeHandle(binding.launch, binding.controller.tmux, "session-1", FIXED_TIME, pid=4242),
        )[-1],
        probe=_probe(binding, tmp_path),
        now=lambda: FIXED_TIME,
    )
    assert provider_called == [True]
    assert applied.decision is rea.RuntimeEffectDecision.COMPLETE


def test_runtime_effect_kill_after_provider_before_receipt_completes_only_from_current_probe(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _wal_store(tmp_path)
    child = _run_child("after_provider_before_receipt", tmp_path)
    assert child.returncode == 73

    result = rea.reconcile_runtime_effect(store, binding, state_root=tmp_path, probe=_probe(binding, tmp_path), now=lambda: FIXED_TIME)
    assert result.decision is rea.RuntimeEffectDecision.COMPLETE
    assert result.receipt_ref is not None

    stale_root = tmp_path / "stale"
    stale_store = _wal_store(stale_root)
    _run_child("after_provider_before_receipt", stale_root)
    _write_runtime(stale_root, binding, overrides={"persona": "other"})
    stale = rea.reconcile_runtime_effect(stale_store, binding, state_root=stale_root, probe=_probe(binding, stale_root), now=lambda: FIXED_TIME)
    assert stale.decision is rea.RuntimeEffectDecision.COMPENSATION_REQUIRED


def test_runtime_effect_kill_after_receipt_before_manifest_references_orphan(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _wal_store(tmp_path)
    child = _run_child("after_receipt_before_manifest", tmp_path)
    assert child.returncode == 73
    assert not store.read_manifest().receipts

    result = rea.reconcile_runtime_effect(store, binding, state_root=tmp_path, probe=_probe(binding, tmp_path), now=lambda: FIXED_TIME)
    assert result.decision is rea.RuntimeEffectDecision.COMPLETE
    assert store.read_manifest().receipts


def test_runtime_effect_kill_after_manifest_reference_is_idempotent_but_not_truthless(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _wal_store(tmp_path)
    child = _run_child("after_manifest_reference", tmp_path)
    assert child.returncode == 73
    assert store.read_manifest().receipts

    result = rea.reconcile_runtime_effect(store, binding, state_root=tmp_path, probe=_probe(binding, tmp_path), now=lambda: FIXED_TIME)
    assert result.decision is rea.RuntimeEffectDecision.REUSE

    _runtime_path(tmp_path, binding).unlink()
    stale = rea.reconcile_runtime_effect(store, binding, state_root=tmp_path, probe=_probe(binding, tmp_path), now=lambda: FIXED_TIME)
    assert stale.decision is rea.RuntimeEffectDecision.REFUSE


def test_runtime_effect_decision_table_covers_reuse_complete_retry_compensate_refuse(tmp_path: Path) -> None:
    seen = set()
    rows = [
        (StructuralPrefix.BEFORE_INTENT, rea.RuntimeEffectStatus.NOT_OBSERVED, rea.RuntimeEffectDecision.RETRY),
        (StructuralPrefix.BEFORE_INTENT, rea.RuntimeEffectStatus.LIVE_MATCHING, rea.RuntimeEffectDecision.REUSE),
        (StructuralPrefix.BEFORE_INTENT, rea.RuntimeEffectStatus.UNKNOWN, rea.RuntimeEffectDecision.REFUSE),
        (StructuralPrefix.INTENT_ONLY, rea.RuntimeEffectStatus.LIVE_MATCHING, rea.RuntimeEffectDecision.COMPLETE),
        (StructuralPrefix.INTENT_ONLY, rea.RuntimeEffectStatus.PANE_PID_MISMATCH, rea.RuntimeEffectDecision.COMPENSATION_REQUIRED),
        (StructuralPrefix.APPLIED_NO_RECEIPT, rea.RuntimeEffectStatus.DEAD_MATCHING, rea.RuntimeEffectDecision.REFUSE),
        (StructuralPrefix.ORPHAN_RECEIPT, rea.RuntimeEffectStatus.LIVE_MATCHING, rea.RuntimeEffectDecision.COMPLETE),
        (StructuralPrefix.MANIFEST_REFERENCED, rea.RuntimeEffectStatus.LIVE_MATCHING, rea.RuntimeEffectDecision.REUSE),
    ]
    for prefix, status, expected in rows:
        decision = rea._decision_for(prefix, status)
        seen.add(decision)
        assert decision is expected
    assert seen == set(rea.RuntimeEffectDecision)
    assert rea._apply_may_call_provider(StructuralPrefix.BEFORE_INTENT, rea.RuntimeEffectStatus.NOT_OBSERVED)
    assert rea._apply_may_call_provider(StructuralPrefix.INTENT_ONLY, rea.RuntimeEffectStatus.DEAD_MATCHING)
    assert not rea._apply_may_call_provider(StructuralPrefix.APPLIED_NO_RECEIPT, rea.RuntimeEffectStatus.NOT_OBSERVED)


def test_runtime_effect_corrupt_or_unknown_artifact_fails_closed_without_provider_call(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _wal_store(tmp_path)
    identity = _identity(binding, tmp_path)
    _write_runtime(tmp_path, binding)
    store.write_intent(
        op_id=identity.wal_op_id,
        effect_domain="external",
        preimage=rea._intent_preimage(identity),
        inverse=rea._unsupported_inverse(identity),
        postcondition_check={"schema": "runtime-effect-postcondition/v1"},
        created_at="2026-01-02T03:04:05Z",
    )
    (tmp_path / "wal" / f"intent-{identity.wal_op_id}.json").write_text("not canonical\n", encoding="utf-8")

    provider_called = []
    result = rea.apply_ensure_runtime_effect(
        store,
        binding,
        _Adapter(),
        state_root=tmp_path,
        ensure_runtime_fn=lambda binding, adapter, *, state_root=None, context=None: provider_called.append(True),
        probe=_probe(binding, tmp_path),
        now=lambda: FIXED_TIME,
    )
    assert result.decision is rea.RuntimeEffectDecision.REFUSE
    assert provider_called == []
    assert not store.read_manifest().receipts

    canonical_root = tmp_path / "canonical-wrong"
    canonical_binding = _binding(canonical_root)
    canonical_store = _wal_store(canonical_root)
    canonical_identity = _identity(canonical_binding, canonical_root)
    wrong_identity = replace(
        canonical_identity,
        persona="wrong-persona",
        runtime_state_path_sha256="0" * 64,
    )
    _write_runtime(canonical_root, canonical_binding)
    canonical_store.write_intent(
        op_id=canonical_identity.wal_op_id,
        effect_domain="external",
        preimage=rea._intent_preimage(wrong_identity),
        inverse=rea._unsupported_inverse(wrong_identity),
        postcondition_check={
            "schema": "runtime-effect-postcondition/v1",
            "identity_digest": rea.runtime_effect_identity_digest(wrong_identity),
            "required_status": rea.RuntimeEffectStatus.LIVE_MATCHING.value,
        },
        created_at="2026-01-02T03:04:05Z",
    )
    provider_called = []
    canonical_result = rea.apply_ensure_runtime_effect(
        canonical_store,
        canonical_binding,
        _Adapter(),
        state_root=canonical_root,
        ensure_runtime_fn=lambda binding, adapter, *, state_root=None, context=None: provider_called.append(True),
        probe=_probe(canonical_binding, canonical_root),
        now=lambda: FIXED_TIME,
    )
    assert canonical_result.decision is rea.RuntimeEffectDecision.REFUSE
    assert provider_called == []
    with pytest.raises(WalStoreError):
        canonical_store.read_receipt_by_ref(f"receipt-{canonical_identity.wal_op_id}.json")
    assert not canonical_store.read_manifest().receipts


def test_runtime_effect_sabotage_receipt_digest_and_probe_red_green(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    store = _wal_store(tmp_path)
    completed = _apply_live(store, binding, tmp_path)
    assert completed.receipt_ref is not None
    receipt_path = tmp_path / "wal" / completed.receipt_ref
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["intent_digest"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    result = rea.reconcile_runtime_effect(store, binding, state_root=tmp_path, probe=_probe(binding, tmp_path), now=lambda: FIXED_TIME)
    assert result.decision is rea.RuntimeEffectDecision.REFUSE


def test_runtime_effect_forbidden_scope_static_guard() -> None:
    # T-3841: this guard's ongoing value is the BYTE-CONTENT invariant — the
    # germinate PoC files must never gain a forbidden byte-sequence (see the
    # tuple below for the concrete list). A separate `git diff --name-only`
    # scope-cordon that fired on any other file changing lived here through
    # T-3340's PoC build phase (the operator 2026-09-01); it was dev-scaffolding
    # that stopped serving once T-3340 landed and started failing on every
    # unrelated PR. Removed here; byte-content assertions retained.
    paths = [
        Path("tools/germinate/runtime_effect_adapter.py"),
        Path("tools/test_germinate_runtime_effect_adapter.py"),
    ]
    needle = ("HERME" + "S.md").encode()
    forbidden = (
        b"daemon" + b"-reload",
        b"tele" + b"gram",
        b"Ar" + b"gus",
        b"H" + b"C",
        b"_wait_for_runtime" + b"_input_ready",
        b"_verify_runtime" + b"_input_ready",
    )
    for path in paths:
        data = path.read_bytes()
        assert needle not in data
        for item in forbidden:
            assert item not in data


def _run_child(boundary: str, root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    repo_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = repo_root if not env.get("PYTHONPATH") else f"{repo_root}:{env['PYTHONPATH']}"
    return subprocess.run(
        [sys.executable, __file__, "--runtime-effect-child", boundary, str(root)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def _child(boundary: str, root: Path) -> None:
    binding = _binding(root)
    store = _wal_store(root)
    identity = _identity(binding, root)
    if boundary == "before_intent":
        os._exit(73)
    intent_ref, intent_digest = rea._write_intent(store, identity, now=lambda: FIXED_TIME)
    if boundary == "after_intent_before_provider":
        os._exit(73)
    _write_runtime(root, binding)
    observation = _probe(binding, root).observe_identity(identity)
    classification = store.classify_prefix(op_id=identity.wal_op_id, probe=rea._ValidatingWalProbe(identity, observation))
    if boundary == "after_provider_before_receipt":
        os._exit(73)
    receipt_ref, _receipt_digest = store.write_receipt(
        op_id=identity.wal_op_id,
        intent_ref=intent_ref,
        intent_digest=intent_digest,
        postcondition_result=rea._postcondition_result(identity, observation),
        receipt_payload=rea._receipt_payload(identity, observation),
        committed_at="2026-01-02T03:04:05Z",
    )
    if boundary == "after_receipt_before_manifest":
        os._exit(73)
    assert classification.intent_ref == intent_ref
    store.commit_receipt_reference(
        op_id=identity.wal_op_id,
        receipt_ref=receipt_ref,
        committed_at="2026-01-02T03:04:05Z",
        updated_at="2026-01-02T03:04:05Z",
    )
    if boundary == "after_manifest_reference":
        os._exit(73)
    raise SystemExit(f"unknown boundary: {boundary}")


if __name__ == "__main__" and "--runtime-effect-child" in sys.argv:
    index = sys.argv.index("--runtime-effect-child")
    _child(sys.argv[index + 1], Path(sys.argv[index + 2]))
