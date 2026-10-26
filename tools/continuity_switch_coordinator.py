#!/usr/bin/env python3
"""External ContinuitySwitchCoordinator (T-3177).

Wraps the T-2382/T-2953 stateless runtime controller with the ORG-057
transactional harness-switch lifecycle named by
``rfc/RFC-001-persona-continuity-lifecycle.md`` (state 5, "Harness switch"): read-only
plan, compatibility/authority preflight, verified-checkpoint requirement,
non-authoritative candidate start, continuity verification, single-receiver
commit, exact receipt, and drift-refusing rollback.

This module is deliberately EXTERNAL: it does not modify
``tools/runtime_controller.py`` (T-2953, preserved as-is) or
``tools/persona_continuity.py`` (T-3176, preserved as-is). Both are consumed
as libraries. The runtime controller remains stateless and OS/manifest-derived
-- this coordinator adds no state to it. The switch/v1 receipt this module
emits is evidence about a transaction, never live identity or a
recovery-token authority: ``resume_after_crash`` always re-derives its keep-
vs-restore decision from FRESH live facts, never from a receipt alone (SOP
Authority rule 10: "a receipt or cache cannot overrule contradictory live
evidence").

Candidate isolation is structural, not prompt-based: Codex candidates launch
with ``writable_roots=()`` (an existing, OS-enforced sandbox boundary --
``tools/harness_adapter/codex.py`` already turns this into ``--add-dir``
sandbox args); Claude candidates are gated by the sibling
``tools/hooks/continuity_candidate_guard.py`` PreToolUse hook, modeled on the
same mechanism shape as ``tools/hooks/worker-guard.py``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from tools.persona_continuity import (
    SWITCH_SCHEMA_ID,
    ActorContext,
    CandidateVerificationEvidence,
    CheckpointEnvelope,
    ContinuityError,
    ContinuityVerificationReport,
    HarnessSwitchReceipt,
    RuntimeObservation,
    VERIFICATION_ASSERTION_IDS,
    build_verification_report,
    load_checkpoint_envelope,
    load_continuity_bundle,
    validate_switch_receipt_file,
)
from tools.persona_bringup import PersonaBinding, load_binding, configured_runtime_bindings
from tools import notify_policy
from tools import runtime_controller
from tools import continuity_response_capture as response_capture

# T-3460 (filed 2026-09-06, informs T-3177): these are runtime_controller's
# only single-receiver-presence-proof and tmux-liveness primitives and are
# currently underscore-prefixed (no public wrapper exists). DEC-5 (T-3177
# plan, HO bubble-up #2 ACCEPTED 2026-09-06): import them as-is rather than
# duplicate genuinely non-trivial tmux/dbus/process-membership logic; T-3460
# tracks promoting them to public names -- implementer note: T-3460's ask
# named the first two only; `pane_exists` was a code-turn discovery (the
# coordinator's own general liveness check, distinct from receiver-mediation,
# needs the same primitive) and should be folded into T-3460's scope. Do not
# edit runtime_controller.py here (T-3177 DEC-1 -- controller preserved as-is).
from tools.runtime_controller import (  # noqa: E402  (grouped with the T-3460 comment above)
    prove_managed_receiver_present as _receiver_present,
    prove_predecessor_receiver_absent as _receiver_absent,
    pane_exists as _pane_live,
)

SWITCH_PLAN_SCHEMA_ID = "pantheon-continuity-switch-plan/v1"
TRANSITION_SCHEMA_ID = "pantheon-continuity-switch-transition/v1"
MAX_JSON_BYTES = 256 * 1024

SwitchPhase = Literal[
    "planned",
    "preflighted",
    "checkpoint-verified",
    "candidate-started",
    "continuity-verified",
    "committed",
    "rolled-back",
]

IsolationMechanism = Literal["codex-writable-roots-empty", "claude-candidate-guard-active"]

CANDIDATE_MARKER_ENV = "PANTHEON_CONTINUITY_CANDIDATE"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _plain(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def _new_id(prefix: str) -> str:
    return f"{prefix}-{os.urandom(8).hex()}"


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CoordinatorConfig:
    persona_id: str
    organism_id: str
    runtime_manifest_path: Path       # instance.yaml -- persona_bringup's binding manifest
    bundle_path: Path                 # persona-continuity-bundle/v1 descriptor
    descriptor_root: Path             # root the bundle's refs resolve under
    repo_root: Path
    state_root: Path                  # runtime_controller state root
    checkpoint_root: Path             # persona_continuity checkpoint root
    switch_state_root: Path           # this module's own transition + receipt root
    actor: ActorContext
    transaction_ceiling_minutes: int = 20


@dataclass(frozen=True)
class SwitchPlan:
    schema: str
    plan_id: str
    persona_id: str
    organism_id: str
    old_binding_ref: str
    candidate_binding_ref: str
    manifest_sha256: str
    requested_at: str
    plan_sha256: str


@dataclass(frozen=True)
class PreflightResult:
    status: Literal["compatible", "incompatible"]
    old_sole_receiver_proof: bool
    capability_compatible: bool
    authority_compatible: bool
    manifest_revision: str
    controller_revision: str
    issues: tuple[str, ...]


@dataclass(frozen=True)
class CandidateIsolationProof:
    mechanism: IsolationMechanism
    evidence_sha256: str
    verified_at: str


@dataclass(frozen=True)
class CommunicationResult:
    correlation_id: str
    ok: bool
    evidence_sha256: str
    structured_payload: Mapping[str, Any]


@dataclass(frozen=True)
class SwitchTransition:
    schema: str
    transition_id: str
    persona_id: str
    phase: SwitchPhase
    plan_sha256: str
    checkpoint_sha256: str | None
    verification_sha256: str | None
    receipt_path: str | None
    manifest_sha256: str | None
    old_binding_ref: str
    candidate_binding_ref: str
    updated_at: str
    cleanup_issues: tuple[str, ...] = ()
    rollback_error: str | None = None


def _binding_ref(binding: PersonaBinding) -> str:
    return f"{binding.launch.harness}:{binding.account}:{binding.launch.profile}"


# --------------------------------------------------------------------------
# Ports -- injectable seams. Defaults wrap the real runtime_controller /
# persona_bringup / notify_policy / harness_adapter surfaces so production
# code never touches tmux/subprocess/systemd directly outside this bundle;
# tests substitute fakes so the state-machine logic (drift refusals, the
# four-fact conjunction, single-receiver enforcement) is exercised without a
# live tmux/systemd/git environment.
# --------------------------------------------------------------------------


@dataclass
class CoordinatorPorts:
    list_bindings: Callable[[], tuple[PersonaBinding, ...]]
    start_runtime: Callable[[PersonaBinding], "runtime_controller.RuntimeHandle"]
    observe_binding: Callable[[PersonaBinding], RuntimeObservation]
    communicate: Callable[["runtime_controller.RuntimeHandle", str], CommunicationResult]
    is_live: Callable[[PersonaBinding], bool]
    receiver_required: Callable[[PersonaBinding], bool]
    transfer_receiver: Callable[..., "runtime_controller.RuntimeHandle"]
    isolate_candidate: Callable[[PersonaBinding], tuple[PersonaBinding, CandidateIsolationProof]]
    verify_isolation: Callable[[PersonaBinding, CandidateIsolationProof], bool]
    release_isolation: Callable[[PersonaBinding], None]
    stop_runtime: Callable[[PersonaBinding], None]
    enter_maintenance: Callable[..., str]
    exit_maintenance: Callable[[str], bool]
    git_revision: Callable[[Path], str]
    receiver_present: Callable[[PersonaBinding], bool]
    receiver_absent: Callable[[PersonaBinding], bool]
    now: Callable[[], datetime] = _now


def _default_git_revision(repo_root: Path) -> str:
    """The reviewed-source-revision evidence for switch receipts.

    Deliberately lighter than runtime_controller's own
    `_controller_source_provenance` (which also enforces a clean worktree and
    hashes a fixed source-path list for its own launch-time integrity check)
    -- that is a different, heavier obligation this coordinator doesn't need
    to duplicate for its own evidence field. This is scope-limited to "what
    HEAD was checked out," a known, named simplification (see DECISIONS)."""

    result = subprocess.run(
        ("git", "-C", str(repo_root), "rev-parse", "HEAD"),
        check=False, capture_output=True, text=True, shell=False,
    )
    if result.returncode != 0:
        raise ContinuityError("controller-revision-unavailable", result.stderr.strip())
    revision = result.stdout.strip()
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ContinuityError("controller-revision-invalid", revision)
    return revision


def _default_receiver_required(binding: PersonaBinding) -> bool:
    return runtime_controller.receiver_handoff_required(binding)


def _default_transfer_receiver(**kwargs: Any) -> "runtime_controller.RuntimeHandle":
    return runtime_controller.transition_runtime_with_receiver_handoff(**kwargs)


def _default_enter_maintenance(persona: str, *, minutes: int, reason: str) -> str:
    return notify_policy.enter_maintenance(persona, minutes=minutes, by="continuity-switch-coordinator", reason=reason)


def _default_exit_maintenance(persona: str) -> bool:
    # T-5396: exit now refuses to rewrite a registry it cannot read (instead of wiping
    # every other seat's window). After a commit or rollback that must not abort the
    # transition; the window is bounded and expires on its own, so warn and go on.
    try:
        return notify_policy.exit_maintenance(persona)
    except notify_policy.RegistryUnreadable as exc:
        print(f"[continuity_switch] could not clear maintenance for {persona}: {exc}; "
              f"it expires on its own", file=sys.stderr)
        return False


_CODEX_NETWORK_OVERRIDE_KEY = "sandbox_workspace_write.network_access"


def _default_isolate_candidate(binding: PersonaBinding) -> tuple[PersonaBinding, CandidateIsolationProof]:
    """Compose the harness-specific structural isolation for `binding`.

    Codex: replace writable_roots with an empty tuple -- codex.py already
    translates this into `--add-dir` sandbox args (OS-enforced, not prompt
    discipline) -- AND force sandbox_workspace_write.network_access=false via
    the existing native.config_overrides escape hatch
    (harness_adapter/codex.py:_append_native_args already renders this as
    `--config key=value`). Filesystem isolation alone leaves the SOP's
    "external network or device action" structural-disable item unmet: a
    fleet-standing codex role config defaults network_access=true, and a
    candidate with the persona's own provisioned credentials could otherwise
    speak over the network during verification (T-3177 review finding #2).
    Merges into any existing config_overrides rather than clobbering it.
    Claude: point the candidate at a settings fragment that activates
    tools/hooks/continuity_candidate_guard.py before the pane is reachable;
    the marker env var is what the guard checks.
    """

    if binding.launch.harness == "codex":
        existing_overrides = binding.launch.native.get("config_overrides")
        overrides = dict(existing_overrides) if isinstance(existing_overrides, dict) else {}
        overrides[_CODEX_NETWORK_OVERRIDE_KEY] = False
        isolated_native = {**binding.launch.native, "config_overrides": overrides}
        isolated_launch = dataclasses.replace(binding.launch, writable_roots=(), native=isolated_native)
        isolated = dataclasses.replace(binding, launch=isolated_launch)
        evidence = _sha256(_json_bytes({
            "mechanism": "codex-writable-roots-empty", "writable_roots": [],
            _CODEX_NETWORK_OVERRIDE_KEY: False,
        }))
        proof = CandidateIsolationProof("codex-writable-roots-empty", evidence, _iso(_now()))
        return isolated, proof
    if binding.launch.harness == "claude":
        entry = _write_candidate_guard_settings(binding)
        evidence = _sha256(_json_bytes(entry))
        proof = CandidateIsolationProof("claude-candidate-guard-active", evidence, _iso(_now()))
        return binding, proof
    raise ContinuityError("unsupported-harness-for-isolation", binding.launch.harness)


# Reference_claude_settings_loaded_scope (verified 2.1.221): Claude Code loads
# ONLY canonical `~/.claude/settings.json` ("User settings") -- an
# arbitrarily-named settings file is never read, and `settings.local.json`
# under $HOME is not a loaded scope either. The candidate's OWN account's
# canonical settings.json is therefore the only file that can structurally
# activate this hook. That file may already carry real configuration (MCP
# servers, other hooks, permissions) -- this MUST merge, never clobber.
_GUARD_TAG = "_pantheon_continuity_candidate_guard"


def _candidate_guard_settings_path(binding: PersonaBinding) -> Path:
    return binding.account_home / ".claude" / "settings.json"


def _guard_hook_entry(binding: PersonaBinding) -> dict[str, object]:
    guard_script = str((Path(__file__).resolve().parent / "hooks" / "continuity_candidate_guard.py"))
    return {
        "matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [{"type": "command", "command": f"{CANDIDATE_MARKER_ENV}=1 python3 {guard_script}"}],
        _GUARD_TAG: True,
    }


def _load_settings(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContinuityError("candidate-settings-malformed", str(path)) from exc
    if not isinstance(parsed, dict):
        raise ContinuityError("candidate-settings-malformed", str(path))
    return parsed


def _atomic_write_settings(path: Path, settings: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_json_bytes(settings))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_candidate_guard_settings(binding: PersonaBinding) -> dict[str, object]:
    """Merge the candidate guard's PreToolUse hook into the account's REAL
    canonical settings.json, preserving every existing key. Tags its own
    entry with `_GUARD_TAG` so `_default_release_isolation` removes exactly
    what it added and nothing else."""

    path = _candidate_guard_settings_path(binding)
    settings = _load_settings(path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ContinuityError("candidate-settings-malformed", str(path))
    pre_tool_use = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre_tool_use, list):
        raise ContinuityError("candidate-settings-malformed", str(path))
    pre_tool_use[:] = [item for item in pre_tool_use if not (isinstance(item, dict) and item.get(_GUARD_TAG))]
    entry = _guard_hook_entry(binding)
    pre_tool_use.append(entry)
    _atomic_write_settings(path, settings)
    return entry


def _codex_network_access_disabled(binding: PersonaBinding) -> bool:
    overrides = binding.launch.native.get("config_overrides")
    return isinstance(overrides, dict) and overrides.get(_CODEX_NETWORK_OVERRIDE_KEY) is False


def _isolation_failure_reason(binding: PersonaBinding, proof: CandidateIsolationProof) -> str:
    """Diagnostic-only: does not change the verify_isolation port's bool
    contract, just makes the ContinuityError this coordinator raises legible
    about WHICH structural gate failed, instead of the bare mechanism name."""

    if proof.mechanism == "codex-writable-roots-empty":
        if binding.launch.writable_roots != ():
            return f"{proof.mechanism}: writable_roots not empty"
        if not _codex_network_access_disabled(binding):
            return f"{proof.mechanism}: {_CODEX_NETWORK_OVERRIDE_KEY} not disabled"
        return proof.mechanism
    if proof.mechanism == "claude-candidate-guard-active":
        return f"{proof.mechanism}: candidate guard entry not confirmed active"
    return proof.mechanism


def _default_verify_isolation(binding: PersonaBinding, proof: CandidateIsolationProof) -> bool:
    if proof.mechanism == "codex-writable-roots-empty":
        return binding.launch.writable_roots == () and _codex_network_access_disabled(binding)
    if proof.mechanism == "claude-candidate-guard-active":
        path = _candidate_guard_settings_path(binding)
        try:
            settings = _load_settings(path)
        except ContinuityError:
            return False
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            return False
        pre_tool_use = hooks.get("PreToolUse")
        if not isinstance(pre_tool_use, list):
            return False
        for item in pre_tool_use:
            if isinstance(item, dict) and item.get(_GUARD_TAG) and _sha256(_json_bytes(item)) == proof.evidence_sha256:
                return True
        return False
    return False


def _default_release_isolation(binding: PersonaBinding) -> None:
    """Remove exactly the tagged guard entry this coordinator added --
    NEVER the whole settings.json (which may carry unrelated real
    configuration this module has no authority over)."""

    if binding.launch.harness != "claude":
        return
    path = _candidate_guard_settings_path(binding)
    try:
        settings = _load_settings(path)
    except ContinuityError:
        return  # can't safely edit a malformed file; leave it alone
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    pre_tool_use = hooks.get("PreToolUse")
    if not isinstance(pre_tool_use, list):
        return
    remaining = [item for item in pre_tool_use if not (isinstance(item, dict) and item.get(_GUARD_TAG))]
    if len(remaining) == len(pre_tool_use):
        return  # nothing to remove -- idempotent no-op
    if remaining:
        hooks["PreToolUse"] = remaining
    else:
        del hooks["PreToolUse"]
    if not hooks:
        del settings["hooks"]
    _atomic_write_settings(path, settings)


def _default_stop_runtime(binding: PersonaBinding) -> None:
    """Best-effort candidate teardown for rollback.

    No existing runtime_controller primitive fits directly:
    `compensate_failed_initial_candidate` is scoped to the bootstrap-viability
    failure path and requires a `RuntimeOperationalFailure` this transaction
    doesn't have; forcing that shape would be a worse fit than a direct,
    minimal kill. Flagged in the T-3177 report as a reuse gap rather than
    silently forced -- a candidate for the same T-3460-style promotion
    treatment if a future arc wants one shared teardown primitive.
    """

    subprocess.run(
        ("tmux", "kill-session", "-t", binding.controller.tmux.session),
        check=False, capture_output=True, text=True, shell=False,
    )


def _default_observe_binding(binding: PersonaBinding, *, external_actions_disabled: bool,
                              manifest_path: Path) -> RuntimeObservation:
    """Live-fact probe. Presence is tmux+pid membership (the same authority
    runtime_controller documents for its own active-runtime cache: never a
    receipt or cache, always the OS observation)."""

    live = runtime_controller.pane_exists(binding.controller.tmux)  # noqa: SLF001 -- see T-3460 header note
    return RuntimeObservation(
        runtime_id=_binding_ref(binding),
        harness_id=binding.launch.harness,
        account_ref=binding.account,
        responsive=live,
        self_identification=None,
        role_identification=None,
        authority_limits=(),
        current_work_refs=(),
        receiver_evidence_sha256=None,
        external_actions_disabled=external_actions_disabled,
        observed_at=_iso(_now()),
    )


def default_ports(config: CoordinatorConfig) -> CoordinatorPorts:
    def start_runtime(binding: PersonaBinding) -> "runtime_controller.RuntimeHandle":
        from tools.harness_adapter import base as harness_base

        adapter = harness_base.adapter_for(binding)  # type: ignore[attr-defined]
        # T-4179: opt out of ensure_runtime's prior-codex-poller stop. The
        # coordinator owns the receiver handoff (commit_transfer ->
        # transition_runtime_with_receiver_handoff); stopping the old seat's
        # poller at candidate START would go comms-dark before the candidate is
        # proven, and rollback (ensure_runtime reuse of the live old pane) would
        # never restart it.
        return runtime_controller.ensure_runtime(
            binding, adapter, state_root=config.state_root, stop_prior_receiver=False
        )

    def communicate(handle: "runtime_controller.RuntimeHandle", prompt: str) -> CommunicationResult:
        # T-3461: replaces the T-3177 scaffold (DEC-8/DEC-9) that hardcoded
        # structured_payload={} -- submit_to_runtime is still fire-and-forget
        # by design, so the actual submit+poll+capture+parse composition now
        # lives in continuity_response_capture.default_communicate (kept
        # external to this module for the same code-size reason T-3177 kept
        # runtime_controller external to this one; see T-3461 DECISIONS).
        outcome = response_capture.default_communicate(
            handle, prompt, repo_root=config.repo_root, state_root=config.state_root,
        )
        return CommunicationResult(
            correlation_id=outcome.correlation_id,
            ok=outcome.ok,
            evidence_sha256=outcome.evidence_sha256,
            structured_payload=outcome.structured_payload,
        )

    def list_bindings() -> tuple[PersonaBinding, ...]:
        old_binding = load_binding(config.runtime_manifest_path, config.persona_id)
        return (old_binding, *configured_runtime_bindings(config.runtime_manifest_path, old_binding))

    return CoordinatorPorts(
        list_bindings=list_bindings,
        start_runtime=start_runtime,
        observe_binding=lambda binding: _default_observe_binding(
            binding, external_actions_disabled=False, manifest_path=config.runtime_manifest_path,
        ),
        communicate=communicate,
        is_live=lambda binding: bool(_pane_live(binding.controller.tmux)),
        receiver_required=_default_receiver_required,
        transfer_receiver=_default_transfer_receiver,
        isolate_candidate=_default_isolate_candidate,
        verify_isolation=_default_verify_isolation,
        release_isolation=_default_release_isolation,
        stop_runtime=_default_stop_runtime,
        enter_maintenance=_default_enter_maintenance,
        exit_maintenance=_default_exit_maintenance,
        git_revision=lambda repo_root: _default_git_revision(repo_root),
        receiver_present=lambda binding: bool(_receiver_present(binding)),
        receiver_absent=lambda binding: bool(_receiver_absent(binding, manifest_path=config.runtime_manifest_path)),
    )


# --------------------------------------------------------------------------
# Durable transition persistence -- plain atomic-replace (NOT the immutable
# append-only discipline persona_continuity uses for checkpoints/receipts:
# a transition record legitimately advances phase-by-phase, so "current
# state" is mutable-by-design; only the terminal receipt is immutable).
# --------------------------------------------------------------------------


def _transition_path(root: Path, persona: str, transition_id: str) -> Path:
    return root / persona / "transitions" / f"{transition_id}.json"


def _write_transition(root: Path, transition: SwitchTransition) -> None:
    path = _transition_path(root, transition.persona_id, transition.transition_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _json_bytes(_plain(transition))
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".transition.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_transition(root: Path, persona: str, transition_id: str) -> SwitchTransition:
    path = _transition_path(root, persona, transition_id)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ContinuityError("transition-unavailable", str(path)) from exc
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContinuityError("transition-malformed", str(path)) from exc
    if obj.get("schema") != TRANSITION_SCHEMA_ID:
        raise ContinuityError("transition-invalid", str(path))
    return SwitchTransition(
        schema=TRANSITION_SCHEMA_ID,
        transition_id=obj["transition_id"],
        persona_id=obj["persona_id"],
        phase=obj["phase"],
        plan_sha256=obj["plan_sha256"],
        checkpoint_sha256=obj.get("checkpoint_sha256"),
        verification_sha256=obj.get("verification_sha256"),
        receipt_path=obj.get("receipt_path"),
        manifest_sha256=obj.get("manifest_sha256"),
        old_binding_ref=obj["old_binding_ref"],
        candidate_binding_ref=obj["candidate_binding_ref"],
        updated_at=obj["updated_at"],
        cleanup_issues=tuple(obj.get("cleanup_issues", ())),
        rollback_error=obj.get("rollback_error"),
    )


# --------------------------------------------------------------------------
# Durable, immutable receipt publication -- same discipline as T-3459's
# hardened `_publish_immutable_file` (dirfd-relative, 0600/0700, no-follow,
# exclusive create + atomic link, fsync + readback, typed OSError
# translation, cleanup never replaces the primary error) but reimplemented
# locally (DEC-2): these are ~30 lines scoped to this module's own directory
# convention, not worth crossing persona_continuity's private boundary for.
# --------------------------------------------------------------------------


def _publish_receipt(directory: Path, filename: str, raw: bytes) -> bool:
    if "/" in filename or filename in {"", ".", ".."}:
        raise ContinuityError("invalid-filename", filename)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    dir_fd = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    temp_name = f".{filename}.{os.urandom(8).hex()}.tmp"
    temp_fd: int | None = None
    published = False
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        temp_fd = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
        offset = 0
        while offset < len(raw):
            offset += os.write(temp_fd, raw[offset:])
        os.fsync(temp_fd)
        try:
            os.link(temp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        except FileExistsError as exc:
            existing_fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
            try:
                existing = os.read(existing_fd, MAX_JSON_BYTES + 1)
            finally:
                os.close(existing_fd)
            if existing != raw:
                raise ContinuityError("switch-receipt-conflict", str(directory / filename)) from exc
            return False
        published = True
        final_fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        try:
            if os.read(final_fd, MAX_JSON_BYTES + 1) != raw:
                raise ContinuityError("switch-receipt-readback-drift", filename)
        finally:
            os.close(final_fd)
        os.fsync(dir_fd)
        return True
    except ContinuityError:
        if published:
            try:
                os.unlink(filename, dir_fd=dir_fd)
            except OSError:
                pass
        raise
    except OSError as exc:
        if published:
            try:
                os.unlink(filename, dir_fd=dir_fd)
            except OSError:
                pass
        raise ContinuityError("switch-receipt-publication-failed", f"{directory / filename}: {exc}") from exc
    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        try:
            os.unlink(temp_name, dir_fd=dir_fd)
        except OSError:
            pass
        try:
            os.close(dir_fd)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Coordinator
# --------------------------------------------------------------------------


class ContinuitySwitchCoordinator:
    def __init__(self, config: CoordinatorConfig, ports: CoordinatorPorts | None = None) -> None:
        self.config = config
        self.ports = ports or default_ports(config)

    # -- state 1: plan --------------------------------------------------

    def plan(self, *, candidate_harness: str, candidate_account: str | None = None) -> SwitchPlan:
        manifest_bytes = self.config.runtime_manifest_path.read_bytes()
        manifest_sha256 = _sha256(manifest_bytes)
        try:
            bindings = self.ports.list_bindings()
        except ValueError as exc:
            raise ContinuityError("manifest-unresolvable", str(exc)) from exc
        old_binding, candidates = bindings[0], bindings[1:]
        candidate_binding = next(
            (item for item in candidates if item.launch.harness == candidate_harness
             and (candidate_account is None or item.account == candidate_account)),
            None,
        )
        if candidate_binding is None:
            raise ContinuityError("candidate-binding-unresolvable", candidate_harness)
        if _binding_ref(candidate_binding) == _binding_ref(old_binding):
            raise ContinuityError("candidate-equals-old", candidate_harness)
        base: dict[str, object] = {
            "schema": SWITCH_PLAN_SCHEMA_ID,
            "plan_id": _new_id("plan"),
            "persona_id": self.config.persona_id,
            "organism_id": self.config.organism_id,
            "old_binding_ref": _binding_ref(old_binding),
            "candidate_binding_ref": _binding_ref(candidate_binding),
            "manifest_sha256": manifest_sha256,
            "requested_at": _iso(self.ports.now()),
        }
        return SwitchPlan(**base, plan_sha256=_sha256(_json_bytes(base)))  # type: ignore[arg-type]

    def _resolve_bindings(self, plan: SwitchPlan) -> tuple[PersonaBinding, PersonaBinding]:
        by_ref = {_binding_ref(item): item for item in self.ports.list_bindings()}
        try:
            return by_ref[plan.old_binding_ref], by_ref[plan.candidate_binding_ref]
        except KeyError as exc:
            raise ContinuityError("plan-binding-unresolvable", str(exc)) from exc

    # -- state 2: preflight ----------------------------------------------

    def preflight(self, plan: SwitchPlan) -> PreflightResult:
        current_manifest_sha256 = _sha256(self.config.runtime_manifest_path.read_bytes())
        if current_manifest_sha256 != plan.manifest_sha256:
            raise ContinuityError("manifest-drift", plan.plan_id)
        old_binding, candidate_binding = self._resolve_bindings(plan)

        issues: list[str] = []
        capability_compatible = candidate_binding.launch.harness in {"codex", "claude"}
        if not capability_compatible:
            issues.append(f"unsupported-candidate-harness:{candidate_binding.launch.harness}")
        authority_compatible = old_binding.persona == candidate_binding.persona
        if not authority_compatible:
            issues.append("persona-authority-mismatch")

        old_alive = self.ports.is_live(old_binding)
        if not old_alive:
            issues.append("old-binding-not-live")
        old_sole_receiver_proof = old_alive and self._probe_sole_receiver(old_binding, expect_present=True)
        if old_alive and not old_sole_receiver_proof:
            issues.append("old-receiver-ambiguous")

        controller_revision = self.ports.git_revision(self.config.repo_root)
        status: Literal["compatible", "incompatible"] = (
            "compatible" if (capability_compatible and authority_compatible and old_sole_receiver_proof) else "incompatible"
        )
        result = PreflightResult(
            status=status,
            old_sole_receiver_proof=old_sole_receiver_proof,
            capability_compatible=capability_compatible,
            authority_compatible=authority_compatible,
            manifest_revision=plan.manifest_sha256,
            controller_revision=controller_revision,
            issues=tuple(issues),
        )
        if status == "incompatible":
            raise ContinuityError("preflight-incompatible", "; ".join(issues) or "unknown")
        self.ports.enter_maintenance(
            self.config.persona_id,
            minutes=self.config.transaction_ceiling_minutes,
            reason=f"continuity-switch:{plan.plan_id}",
        )
        self._persist_transition(plan, phase="preflighted")
        return result

    def _probe_sole_receiver(self, binding: PersonaBinding, *, expect_present: bool) -> bool:
        if not self.ports.receiver_required(binding):
            return True
        try:
            if expect_present:
                return bool(self.ports.receiver_present(binding))
            return bool(self.ports.receiver_absent(binding))
        except Exception:
            return False

    def _persist_transition(self, plan: SwitchPlan, *, phase: SwitchPhase, **overrides: Any) -> SwitchTransition:
        try:
            existing = _read_transition(self.config.switch_state_root, self.config.persona_id, plan.plan_id)
            base = dataclasses.asdict(existing)
        except ContinuityError:
            base = {
                "schema": TRANSITION_SCHEMA_ID,
                "transition_id": plan.plan_id,
                "persona_id": self.config.persona_id,
                "plan_sha256": plan.plan_sha256,
                "checkpoint_sha256": None,
                "verification_sha256": None,
                "receipt_path": None,
                "manifest_sha256": plan.manifest_sha256,
                "old_binding_ref": plan.old_binding_ref,
                "candidate_binding_ref": plan.candidate_binding_ref,
                "cleanup_issues": (),
                "rollback_error": None,
            }
        base.update(overrides)
        base["phase"] = phase
        base["updated_at"] = _iso(self.ports.now())
        transition = SwitchTransition(**base)
        _write_transition(self.config.switch_state_root, transition)
        return transition

    # -- state 3+4: require_checkpoint ------------------------------------

    def require_checkpoint(self, plan: SwitchPlan, preflight: PreflightResult, *,
                            checkpoint_envelope_path: Path) -> CheckpointEnvelope:
        if preflight.status != "compatible":
            raise ContinuityError("preflight-not-compatible", plan.plan_id)
        envelope = load_checkpoint_envelope(checkpoint_envelope_path, checkpoint_root=self.config.checkpoint_root)
        if envelope.persona_id != self.config.persona_id:
            raise ContinuityError("checkpoint-persona-mismatch", envelope.persona_id)
        bundle = load_continuity_bundle(self.config.bundle_path, descriptor_root=self.config.descriptor_root)
        if envelope.descriptor_sha256 != bundle.descriptor_sha256:
            raise ContinuityError("descriptor-drift", str(checkpoint_envelope_path))
        self._persist_transition(plan, phase="checkpoint-verified", checkpoint_sha256=envelope.payload_sha256)
        return envelope

    # -- state 5+6: start_candidate ----------------------------------------

    def start_candidate(self, plan: SwitchPlan, checkpoint: CheckpointEnvelope) -> tuple["runtime_controller.RuntimeHandle", CandidateIsolationProof]:
        _, candidate_binding = self._resolve_bindings(plan)
        isolated_binding, proof = self.ports.isolate_candidate(candidate_binding)
        if not self.ports.verify_isolation(isolated_binding, proof):
            raise ContinuityError("isolation-not-proven", _isolation_failure_reason(isolated_binding, proof))
        handle = self.ports.start_runtime(isolated_binding)
        if not self.ports.verify_isolation(isolated_binding, proof):
            raise ContinuityError("isolation-not-proven-post-start", _isolation_failure_reason(isolated_binding, proof))
        self._persist_transition(plan, phase="candidate-started")
        return handle, proof

    # -- state 7: verify_continuity -----------------------------------------

    def verify_continuity(self, plan: SwitchPlan, checkpoint: CheckpointEnvelope,
                           candidate_handle: "runtime_controller.RuntimeHandle",
                           isolation_proof: CandidateIsolationProof) -> ContinuityVerificationReport:
        old_binding, candidate_binding = self._resolve_bindings(plan)
        comm = self.ports.communicate(candidate_handle, "identify yourself: persona, role, next action")
        payload = comm.structured_payload
        old_sole_authority = self.ports.is_live(old_binding) and \
            self._probe_sole_receiver(old_binding, expect_present=True) and \
            self.ports.verify_isolation(candidate_binding, isolation_proof)
        # NOTE: every field below defaults to a value that CANNOT satisfy
        # build_verification_report's comparison (None / empty tuple / a
        # placeholder string) when the candidate's response omitted it --
        # never to the "expected" value. A candidate that doesn't self-report
        # must fail the assertion, not pass it by the coordinator's own
        # silent substitution (that would make "candidate identifies its own
        # commitments/role" trivially true regardless of what the candidate
        # actually said).
        #
        # T-3461: checkpoint_sha256/descriptor_sha256/source_revisions used
        # to be populated straight from `checkpoint` (the trusted envelope)
        # regardless of what the candidate reported -- the exact tautology
        # T-3177's reviewer named ("last-checkpoint-digest" and
        # "bundle-source-revisions" always passed since both sides were the
        # same envelope). They now come from the candidate's self-report
        # payload, with fail-closed defaults, so a real candidate has to
        # actually echo these back correctly. See T-3461 DECISIONS for why
        # source_revisions' fail-closed default is a non-empty sentinel
        # (persona_continuity's own schema validator rejects an empty list
        # as malformed evidence, not a legitimate empty self-report).
        evidence = CandidateVerificationEvidence(
            runtime_id=_binding_ref(candidate_binding),
            persona_id=checkpoint.persona_id,
            organism_id=checkpoint.organism_id,
            role_id=payload.get("role_id", "repo:unidentified.json"),
            role_authority_refs=tuple(payload.get("role_authority_refs", ())),
            current_commitment_refs=tuple(payload.get("current_commitment_refs", ())),
            expected_next_action=payload.get("expected_next_action", "unidentified"),
            authority_limits=tuple(payload.get("authority_limits", ())),
            unavailable_capabilities=tuple(payload.get("unavailable_capabilities", ())),
            capability_evidence_complete=bool(payload.get("capability_evidence_complete", False)),
            checkpoint_sha256=payload.get("checkpoint_sha256") or "0" * 64,
            descriptor_sha256=payload.get("descriptor_sha256") or "0" * 64,
            source_revisions=response_capture.candidate_source_revisions_from_payload(
                payload.get("source_revisions")
            ),
            communication_correlation_id=comm.correlation_id,
            communication_evidence_sha256=comm.evidence_sha256,
            communication_ok=comm.ok and comm.correlation_id == checkpoint.boundary.correlation_id,
            external_actions_disabled=self.ports.verify_isolation(candidate_binding, isolation_proof),
            old_embodiment_sole_authority=old_sole_authority,
            behavioral_variance=tuple(payload.get("behavioral_variance", ())),
            variance_evidence_sha256=payload.get("variance_evidence_sha256") or comm.evidence_sha256,
            observed_at=_iso(self.ports.now()),
        )
        report = build_verification_report(envelope=checkpoint, candidate_evidence=evidence)
        self._persist_transition(plan, phase="continuity-verified", verification_sha256=report.report_sha256)
        return report

    # -- state 8-12: commit_transfer -----------------------------------------

    def commit_transfer(self, plan: SwitchPlan, checkpoint: CheckpointEnvelope,
                         verification: ContinuityVerificationReport,
                         isolation_proof: CandidateIsolationProof) -> HarnessSwitchReceipt:
        if not all(item.passed for item in verification.assertions):
            failed = tuple(item.assertion_id for item in verification.assertions if not item.passed)
            raise ContinuityError("verification-not-ready", ",".join(failed))
        old_binding, candidate_binding = self._resolve_bindings(plan)

        if not self._stable_observation_sandwich(old_binding, expect_present=True):
            raise ContinuityError("unstable-observation", plan.old_binding_ref)
        if not self._stable_observation_sandwich(candidate_binding, expect_present=True):
            raise ContinuityError("unstable-observation", plan.candidate_binding_ref)

        old_live = self.ports.is_live(old_binding)
        candidate_isolated = self.ports.verify_isolation(candidate_binding, isolation_proof)
        if old_live and not candidate_isolated:
            # Both would hold action authority simultaneously -- split-brain,
            # refused per ORG-057 rule #5 (verification overlap may observe,
            # never create two action authorities).
            raise ContinuityError("split-brain-refused", plan.plan_id)

        old_obs = self.ports.observe_binding(old_binding)
        old_obs = dataclasses.replace(old_obs, external_actions_disabled=False,
                                       self_identification=old_obs.self_identification or "old-embodiment",
                                       role_identification=old_obs.role_identification or "incumbent",
                                       current_work_refs=old_obs.current_work_refs or ("state:continuity-switch",))
        candidate_obs = self.ports.observe_binding(candidate_binding)
        candidate_obs = dataclasses.replace(candidate_obs, external_actions_disabled=True,
                                             self_identification=candidate_obs.self_identification or "candidate-embodiment",
                                             role_identification=candidate_obs.role_identification or "candidate",
                                             current_work_refs=candidate_obs.current_work_refs or ("state:continuity-switch",))

        try:
            self.ports.transfer_receiver(
                current=old_binding, target=candidate_binding,
                replace=lambda item: self._atomic_commit(item, old_binding=old_binding, candidate_binding=candidate_binding),
                reason="continuity-switch",
            )
        except Exception as exc:
            self.rollback(plan, reason=f"commit-transfer-failed: {exc}", stage_reached="committed")
            raise

        receipt = self.emit_receipt(
            plan, outcome="committed", old_obs=old_obs, candidate_obs=candidate_obs,
            checkpoint=checkpoint, verification=verification, isolation_proof=isolation_proof,
        )
        self.ports.exit_maintenance(self.config.persona_id)
        self._persist_transition(plan, phase="committed", receipt_path=receipt.receipt_sha256)
        return receipt

    def _atomic_commit(self, item: PersonaBinding, *, old_binding: PersonaBinding,
                        candidate_binding: PersonaBinding) -> "runtime_controller.RuntimeHandle":
        """The `replace` callable passed to `transfer_receiver`
        (transition_runtime_with_receiver_handoff). That function calls this
        with `candidate_binding` on the forward path, and MAY call it again
        with `old_binding` on its own internal reverse-compensation path if
        something fails mid-transfer (before our own `commit_transfer`
        except-block's `rollback()` ever runs) -- so this must be correct in
        both directions, not just forward."""

        item_ref = _binding_ref(item)
        if item_ref == _binding_ref(candidate_binding):
            self.ports.stop_runtime(old_binding)
            self.ports.release_isolation(candidate_binding)
            return self.ports.start_runtime(candidate_binding)
        if item_ref == _binding_ref(old_binding):
            self.ports.stop_runtime(candidate_binding)
            return self.ports.start_runtime(old_binding)
        raise ContinuityError("atomic-commit-unknown-target", item_ref)

    def _stable_observation_sandwich(self, binding: PersonaBinding, *, expect_present: bool,
                                      probes: int = 2, interval_seconds: float = 0.0) -> bool:
        results = []
        for index in range(probes):
            results.append(self.ports.is_live(binding) == expect_present)
            if index < probes - 1 and interval_seconds:
                time.sleep(interval_seconds)
        return len(set(results)) == 1 and results[0]

    # -- state 6 (rollback) ----------------------------------------------

    def rollback(self, plan: SwitchPlan, *, reason: str, stage_reached: SwitchPhase) -> HarnessSwitchReceipt:
        old_binding, candidate_binding = self._resolve_bindings(plan)
        # Stop the candidate first -- rollback must prove its action surfaces
        # absent BEFORE restoring old, never merely release its isolation
        # marker (that's commit's job, for the now-authoritative binding).
        try:
            self.ports.stop_runtime(candidate_binding)
        except Exception:
            pass
        candidate_absent = not self.ports.is_live(candidate_binding)

        current_manifest_sha256 = _sha256(self.config.runtime_manifest_path.read_bytes())
        if current_manifest_sha256 != plan.manifest_sha256:
            raise ContinuityError("rollback-postimage-drift", plan.plan_id)

        try:
            self.ports.start_runtime(old_binding)
        except Exception as exc:
            self._persist_transition(plan, phase="rolled-back", rollback_error=str(exc))
            raise ContinuityError("rollback-restore-failed", str(exc)) from exc

        old_restored = self.ports.is_live(old_binding) and self._probe_sole_receiver(old_binding, expect_present=True)
        old_obs = self.ports.observe_binding(old_binding)
        old_obs = dataclasses.replace(old_obs, external_actions_disabled=False,
                                       self_identification=old_obs.self_identification or "old-embodiment",
                                       role_identification=old_obs.role_identification or "incumbent",
                                       current_work_refs=old_obs.current_work_refs or ("state:continuity-switch-rollback",))
        candidate_obs = self.ports.observe_binding(candidate_binding)
        candidate_obs = dataclasses.replace(candidate_obs, external_actions_disabled=True,
                                             self_identification=candidate_obs.self_identification or "candidate-embodiment",
                                             role_identification=candidate_obs.role_identification or "candidate",
                                             current_work_refs=candidate_obs.current_work_refs or ("state:continuity-switch-rollback",))

        receipt = self.emit_receipt(
            plan, outcome="rolled-back", old_obs=old_obs, candidate_obs=candidate_obs,
            checkpoint=None, verification=None, isolation_proof=None,
            note=f"reason={reason};stage={stage_reached};candidate_absent={candidate_absent};old_restored={old_restored}",
        )
        self.ports.exit_maintenance(self.config.persona_id)
        self._persist_transition(plan, phase="rolled-back", receipt_path=receipt.receipt_sha256)
        return receipt

    # -- receipt emission --------------------------------------------------

    def emit_receipt(self, plan: SwitchPlan, *, outcome: Literal["committed", "rolled-back"],
                      old_obs: RuntimeObservation, candidate_obs: RuntimeObservation,
                      checkpoint: CheckpointEnvelope | None,
                      verification: ContinuityVerificationReport | None,
                      isolation_proof: CandidateIsolationProof | None,
                      note: str = "") -> HarnessSwitchReceipt:
        prior_receipt_sha256 = self._latest_receipt_sha256(plan)
        base: dict[str, object] = {
            "schema": SWITCH_SCHEMA_ID,
            "receipt_id": _new_id("receipt"),
            "persona_id": self.config.persona_id,
            "approved_plan_sha256": plan.plan_sha256,
            "actor_id": self.config.actor.actor_id,
            "reviewed_source_revision": self.ports.git_revision(self.config.repo_root),
            "old_runtime_observation": _plain(old_obs),
            "candidate_runtime_observation": _plain(candidate_obs),
            "checkpoint_sha256": checkpoint.payload_sha256 if checkpoint else (self._current_checkpoint_sha256(plan) or "0" * 64),
            "verification_sha256": verification.report_sha256 if verification else "0" * 64,
            "compatibility_result": "compatible" if outcome == "committed" else "rolled-back",
            "old_receiver_isolation_sha256": _sha256(note.encode() or b"none"),
            "candidate_receiver_isolation_sha256": isolation_proof.evidence_sha256 if isolation_proof else "0" * 64,
            "outcome": outcome,
            "manifest_revision": plan.manifest_sha256,
            "controller_revision": self.ports.git_revision(self.config.repo_root),
            "prior_receipt_sha256": prior_receipt_sha256,
        }
        raw_body = _json_bytes(base)
        receipt_sha256 = _sha256(raw_body)
        body = dict(base, receipt_sha256=receipt_sha256)
        raw = _json_bytes(body)
        directory = self.config.switch_state_root / self.config.persona_id / "receipts"
        filename = f"{body['receipt_id']}.json"
        _publish_receipt(directory, filename, raw)
        return validate_switch_receipt_file(directory / filename)

    def _latest_receipt_sha256(self, plan: SwitchPlan) -> str | None:
        try:
            transition = _read_transition(self.config.switch_state_root, self.config.persona_id, plan.plan_id)
        except ContinuityError:
            return None
        return transition.receipt_path

    def _current_checkpoint_sha256(self, plan: SwitchPlan) -> str | None:
        try:
            transition = _read_transition(self.config.switch_state_root, self.config.persona_id, plan.plan_id)
        except ContinuityError:
            return None
        return transition.checkpoint_sha256

    # -- section 6: post-commit-crash recovery ------------------------------

    def resume_after_crash(self, transition_id: str) -> HarnessSwitchReceipt:
        try:
            transition = _read_transition(self.config.switch_state_root, self.config.persona_id, transition_id)
        except ContinuityError:
            plan = self._reconstruct_plan_for_resume(transition_id)
            return self.rollback(plan, reason="transition-record-unavailable", stage_reached="planned")

        plan = SwitchPlan(
            schema=SWITCH_PLAN_SCHEMA_ID, plan_id=transition.transition_id, persona_id=transition.persona_id,
            organism_id=self.config.organism_id, old_binding_ref=transition.old_binding_ref,
            candidate_binding_ref=transition.candidate_binding_ref,
            manifest_sha256=transition.manifest_sha256 or "", requested_at=transition.updated_at,
            plan_sha256=transition.plan_sha256,
        )

        if not transition.receipt_path:
            return self.rollback(plan, reason="no-receipt-recorded", stage_reached=transition.phase)

        receipt_path = self._find_receipt_by_sha(transition.receipt_path)
        if receipt_path is None:
            return self.rollback(plan, reason="receipt-file-missing", stage_reached=transition.phase)
        try:
            receipt = validate_switch_receipt_file(receipt_path)
        except ContinuityError:
            return self.rollback(plan, reason="receipt-invalid", stage_reached=transition.phase)
        if receipt.outcome != "committed":
            return self.rollback(plan, reason="receipt-not-committed", stage_reached=transition.phase)

        old_binding, candidate_binding = self._resolve_bindings(plan)

        fact_old_absent = not self.ports.is_live(old_binding) and self._probe_sole_receiver(old_binding, expect_present=False)
        fact_candidate_sole_healthy = self._stable_observation_sandwich(candidate_binding, expect_present=True) and \
            self._probe_sole_receiver(candidate_binding, expect_present=True)
        try:
            fact_manifest_exact = _sha256(self.config.runtime_manifest_path.read_bytes()) == receipt.manifest_revision
        except OSError:
            fact_manifest_exact = False
        fact_digests_exact = self._resume_digests_exact(transition, receipt)

        if fact_old_absent and fact_candidate_sole_healthy and fact_manifest_exact and fact_digests_exact:
            return receipt
        return self.rollback(
            plan,
            reason=(
                f"post-commit-crash-fact-check-failed:old_absent={fact_old_absent},"
                f"candidate_sole_healthy={fact_candidate_sole_healthy},"
                f"manifest_exact={fact_manifest_exact},digests_exact={fact_digests_exact}"
            ),
            stage_reached="committed",
        )

    def _resume_digests_exact(self, transition: SwitchTransition, receipt: HarnessSwitchReceipt) -> bool:
        if transition.checkpoint_sha256 != receipt.checkpoint_sha256:
            return False
        if transition.verification_sha256 is not None and transition.verification_sha256 != receipt.verification_sha256:
            return False
        return True

    def _find_receipt_by_sha(self, receipt_sha256: str) -> Path | None:
        directory = self.config.switch_state_root / self.config.persona_id / "receipts"
        try:
            entries = list(directory.iterdir())
        except OSError:
            return None
        for entry in entries:
            try:
                obj = json.loads(entry.read_bytes())
            except (OSError, json.JSONDecodeError):
                continue
            if obj.get("receipt_sha256") == receipt_sha256:
                return entry
        return None

    def _reconstruct_plan_for_resume(self, transition_id: str) -> SwitchPlan:
        old_binding = self.ports.list_bindings()[0]
        return SwitchPlan(
            schema=SWITCH_PLAN_SCHEMA_ID, plan_id=transition_id, persona_id=self.config.persona_id,
            organism_id=self.config.organism_id, old_binding_ref=_binding_ref(old_binding),
            candidate_binding_ref=_binding_ref(old_binding),
            manifest_sha256=_sha256(self.config.runtime_manifest_path.read_bytes()),
            requested_at=_iso(self.ports.now()), plan_sha256="0" * 64,
        )
