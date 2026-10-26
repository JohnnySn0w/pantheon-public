"""Pure validator for the initial-viability invocation receipt (T-5572).

Intent (T-2652 / ORG-052 section 1 and 3): one closed module set per privileged
binary. The per-seat executor (tools/host_control/executor/engine.py) validates
the invocation receipt a disabled first fire leaves behind. That validator used
to live in tools.runtime_controller, whose import loads the mailbox client, the
harness adapter and the Codex Telegram actions. The validator closure is moved
here verbatim: the receipt schema constants, the descriptor-confined no-follow
reader, the payload checks, and the persona state-root resolution
(``DEFAULT_STATE_ROOT``, ``_state_root``, ``_persona_state``) and canonical-bytes
helpers it calls. Standard library only.

tools.runtime_controller re-exports every name here as the same object, so its
callers are unchanged. These definitions resolve each other through THIS
module's globals: patch them here, not on runtime_controller (pinned by
tools/test_host_control_import_closure.py).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_STATE_ROOT = Path.home() / ".local" / "state" / "pantheon" / "personas"
INITIAL_VIABILITY_INVOCATION_SCHEMA = "pantheon-runtime-initial-viability-invocation/v1"
INITIAL_VIABILITY_INVOCATION_MAX_BYTES = 256 * 1024
SYSTEMD_INVOCATION_ID_RE = re.compile(r"[0-9a-f]{32}")


def _state_root(state_root: Path | None = None) -> Path:
    """The personas dir. T-5028 (b): PANTHEON_PERSONA_STATE_DIR, else
    ``$PANTHEON_STATE_DIR/personas``, else DEFAULT_STATE_ROOT -- the same
    resolution as session_chronicle.persona_state_root, proactive_swap_init and
    the harness CLI (harness_state.state_root_from_env), so the holds file the
    operator writes is the one both planes read. The planes never refuse on a
    conflicting pair (PANTHEON_PERSONA_STATE_DIR wins); the CLI does."""
    if state_root is not None:
        return state_root
    configured = os.environ.get("PANTHEON_PERSONA_STATE_DIR")
    if configured:
        return Path(configured)
    state_dir = os.environ.get("PANTHEON_STATE_DIR")
    return Path(state_dir) / "personas" if state_dir else DEFAULT_STATE_ROOT


def _persona_state(persona: str, state_root: Path | None = None) -> Path:
    return _state_root(state_root) / persona / "runtime-controller"


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_systemd_invocation_id(value: str) -> str:
    if not SYSTEMD_INVOCATION_ID_RE.fullmatch(value):
        raise RuntimeError("systemd INVOCATION_ID is malformed")
    return value


def _initial_viability_invocation_dir(persona: str, state_root: Path | None) -> Path:
    return _persona_state(persona, state_root) / "initial-viability-invocations"


def _initial_viability_invocation_path(
    persona: str,
    invocation_id: str,
    state_root: Path | None,
) -> Path:
    return _initial_viability_invocation_dir(persona, state_root) / f"{_validate_systemd_invocation_id(invocation_id)}.json"


def _secure_directory_fd(path: Path, *, mode: int = 0o700, create: bool) -> int:
    if not path.is_absolute():
        raise RuntimeError("invocation receipt directory must be absolute")
    parts = path.parts
    if not parts or parts[0] != "/":
        raise RuntimeError("invocation receipt directory must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow_flags = flags | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open("/", flags)
    try:
        for part in parts[1:]:
            if part in {"", ".", ".."}:
                raise RuntimeError("invocation receipt directory contains unsafe component")
            try:
                next_fd = os.open(part, nofollow_flags, dir_fd=fd)
            except FileNotFoundError as exc:
                if not create:
                    raise RuntimeError("invocation receipt directory is missing") from exc
                os.mkdir(part, mode, dir_fd=fd)
                next_fd = os.open(part, nofollow_flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise RuntimeError("invocation receipt directory contains symlink or non-directory") from exc
                raise
            st = os.fstat(next_fd)
            if not stat.S_ISDIR(st.st_mode):
                os.close(next_fd)
                raise RuntimeError("invocation receipt directory component is not a directory")
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_invocation_payload_no_follow_at(parent_fd: int, name: str) -> tuple[dict[str, Any], str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise RuntimeError("invocation receipt is unavailable") from exc
    return _read_invocation_payload_fd(fd)


def _read_invocation_payload_fd(fd: int) -> tuple[dict[str, Any], str]:
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError("invocation receipt is not a regular file")
        if st.st_uid != os.geteuid():
            raise RuntimeError("invocation receipt owner mismatch")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise RuntimeError("invocation receipt mode mismatch")
        if st.st_size <= 0 or st.st_size > INITIAL_VIABILITY_INVOCATION_MAX_BYTES:
            raise RuntimeError("invocation receipt size is outside the bounded range")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(INITIAL_VIABILITY_INVOCATION_MAX_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > INITIAL_VIABILITY_INVOCATION_MAX_BYTES:
        raise RuntimeError("invocation receipt size is outside the bounded range")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invocation receipt JSON is malformed") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("invocation receipt payload is not an object")
    return payload, hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _validate_invocation_payload(
    payload: Mapping[str, Any],
    *,
    persona: str,
    invocation_id: str,
) -> Mapping[str, Any]:
    if payload.get("schema") != INITIAL_VIABILITY_INVOCATION_SCHEMA:
        raise RuntimeError("invocation receipt schema mismatch")
    if payload.get("persona") != persona:
        raise RuntimeError("invocation receipt persona mismatch")
    if payload.get("invocation_id") != _validate_systemd_invocation_id(invocation_id):
        raise RuntimeError("invocation receipt id mismatch")
    viability = payload.get("initial_viability")
    if not isinstance(viability, Mapping):
        raise RuntimeError("invocation receipt missing viability payload")
    if viability.get("schema") != "pantheon-runtime-initial-viability/v1":
        raise RuntimeError("invocation viability schema mismatch")
    if viability.get("persona") != persona:
        raise RuntimeError("invocation viability persona mismatch")
    if viability.get("status") != "selected":
        raise RuntimeError("invocation viability is not selected")
    return viability


def validate_initial_viability_invocation_receipt(
    persona: str,
    *,
    invocation_id: str,
    state_root: Path | None,
    allowed_runtime_refs: Sequence[str],
    require_receiver_receipt: bool = False,
    receiver_operations_by_runtime: Mapping[str, Sequence[str]] | None = None,
) -> Mapping[str, Any]:
    path = _initial_viability_invocation_path(persona, invocation_id, state_root)
    state_root_path = _state_root(state_root)
    if not state_root_path.is_absolute() or not path.is_absolute():
        raise RuntimeError("invocation receipt state root must be absolute")
    try:
        path.relative_to(state_root_path)
    except ValueError as exc:
        raise RuntimeError("invocation receipt path escapes state root") from exc
    parent_fd = _secure_directory_fd(path.parent, create=False)
    try:
        payload, payload_sha = _read_invocation_payload_no_follow_at(parent_fd, path.name)
    finally:
        os.close(parent_fd)
    return _validate_initial_viability_invocation_payload(
        persona,
        invocation_id=invocation_id,
        payload=payload,
        payload_sha=payload_sha,
        allowed_runtime_refs=allowed_runtime_refs,
        require_receiver_receipt=require_receiver_receipt,
        receiver_operations_by_runtime=receiver_operations_by_runtime,
    )


def _validate_initial_viability_invocation_payload(
    persona: str,
    *,
    invocation_id: str,
    payload: Mapping[str, Any],
    payload_sha: str,
    allowed_runtime_refs: Sequence[str],
    require_receiver_receipt: bool = False,
    receiver_operations_by_runtime: Mapping[str, Sequence[str]] | None = None,
) -> Mapping[str, Any]:
    viability = _validate_invocation_payload(
        payload,
        persona=persona,
        invocation_id=invocation_id,
    )
    allowed = tuple(allowed_runtime_refs)
    if not allowed or len(set(allowed)) != len(allowed):
        raise RuntimeError("allowed runtime refs must be nonempty and deduplicated")
    selected = viability.get("selected_runtime")
    if selected not in allowed:
        raise RuntimeError("selected runtime is not manifest-authorized")
    attempts = viability.get("attempts")
    if not isinstance(attempts, list):
        raise RuntimeError("invocation viability attempts missing")
    selected_attempts = [
        item
        for item in attempts
        if isinstance(item, Mapping)
        and item.get("runtime") == selected
        and item.get("status") == "selected"
    ]
    if len(selected_attempts) != 1:
        raise RuntimeError("selected invocation attempt missing or duplicated")
    selected_attempt = selected_attempts[0]
    readiness = selected_attempt.get("readiness")
    if not isinstance(readiness, Mapping):
        raise RuntimeError("selected readiness evidence missing")
    if not readiness.get("provider_completion_status"):
        raise RuntimeError("selected provider completion status missing")
    provenance = selected_attempt.get("source_provenance")
    required_provenance = {
        "revision",
        "source_manifest_sha256",
        "runtime_start_boundary_at",
        "runtime_start_observed_at",
    }
    if not isinstance(provenance, Mapping) or not required_provenance.issubset(provenance):
        raise RuntimeError("selected source provenance missing")
    allowed_failures = {"subscription-cap", "operational-launch", "operational-readiness"}
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            raise RuntimeError("invocation viability attempt is malformed")
        if attempt.get("status") == "failed" and attempt.get("failure_class") not in allowed_failures:
            raise RuntimeError("failed attempt has unknown failure class")
    receiver = selected_attempt.get("receiver")
    expected_receiver_ops = dict(receiver_operations_by_runtime or {})
    expected_ops = tuple(str(item) for item in expected_receiver_ops.get(str(selected), ()))
    if require_receiver_receipt or expected_ops:
        if not isinstance(receiver, Mapping) or receiver.get("ok") is not True:
            raise RuntimeError("selected receiver receipt missing")
        operations = receiver.get("operations")
        if not isinstance(operations, list):
            raise RuntimeError("selected receiver receipt operations missing")
        if expected_ops and tuple(str(item) for item in operations) != expected_ops:
            raise RuntimeError("selected receiver receipt operations mismatch")
    return {
        "schema": "pantheon-runtime-initial-viability-invocation-check/v1",
        "persona": persona,
        "invocation_id": invocation_id,
        "ok": True,
        "selected_runtime": str(selected),
        "selected_harness": str(viability.get("selected_harness", "")),
        "selected_profile": str(viability.get("selected_profile", "")),
        "provider_completion_status": str(readiness.get("provider_completion_status", "")),
        "invocation_receipt_sha256": payload_sha,
        "attempt_count": str(len(attempts)),
    }
