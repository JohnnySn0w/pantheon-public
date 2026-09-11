"""Fixed transitional Codex permission posture helpers.

This is deliberately a tiny pure module: no project imports, no grants DSL, no
defaults. It exists so runtime parsing, installer planning, and bridge rendering
can share the same fixed literals without importing each other.
"""

from __future__ import annotations

from typing import Any, Mapping


CODEX_PERMISSION_POSTURE_KEY = "permission_posture"
CODEX_EXECUTION_BOUNDARY_KEY = "execution_boundary"
CODEX_NATIVE_PERMISSION_POSTURE_KEY = "permission_posture"
CODEX_NATIVE_EXECUTION_BOUNDARY_KEY = "execution_boundary"
CODEX_FULL_AUTO_PERMISSION_POSTURE = "full-auto"
CODEX_FULL_AUTO_APPROVAL_POLICY = "never"
CODEX_PERMISSION_POSTURES = frozenset({CODEX_FULL_AUTO_PERMISSION_POSTURE})
CODEX_EXECUTION_BOUNDARIES = frozenset({"workspace", "unrestricted"})
CODEX_EXECUTION_BOUNDARY_SANDBOX = {
    "workspace": "workspace-write",
    "unrestricted": "danger-full-access",
}


class PermissionPostureError(ValueError):
    """The fixed transitional Codex posture shape is invalid."""


def codex_permission_intent_from_role_block(
    role_block: Mapping[str, Any],
    *,
    role: str,
) -> Mapping[str, str]:
    posture = role_block.get(CODEX_PERMISSION_POSTURE_KEY)
    boundary = role_block.get(CODEX_EXECUTION_BOUNDARY_KEY)
    if posture is None and boundary is None:
        return {}
    if posture is None:
        raise PermissionPostureError(
            f"harness_runtime.roles.{role}.codex.execution_boundary requires "
            "permission_posture"
        )
    if boundary is None:
        raise PermissionPostureError(
            f"harness_runtime.roles.{role}.codex.permission_posture requires "
            "execution_boundary"
        )
    if posture not in CODEX_PERMISSION_POSTURES:
        raise PermissionPostureError(
            f"harness_runtime.roles.{role}.codex.permission_posture has unsupported "
            f"value {posture!r}"
        )
    if boundary not in CODEX_EXECUTION_BOUNDARIES:
        raise PermissionPostureError(
            f"harness_runtime.roles.{role}.codex.execution_boundary has unsupported "
            f"value {boundary!r}"
        )
    return {
        CODEX_NATIVE_PERMISSION_POSTURE_KEY: str(posture),
        CODEX_NATIVE_EXECUTION_BOUNDARY_KEY: str(boundary),
    }


def validate_no_codex_permission_dual_authority(
    native: Mapping[str, Any],
    *,
    role: str,
    has_transitional_intent: bool,
) -> None:
    if not has_transitional_intent:
        return
    if "sandbox" in native:
        raise PermissionPostureError(
            f"harness_runtime.roles.{role}.codex cannot combine execution_boundary "
            "with native.sandbox"
        )
    overrides = native.get("config_overrides")
    if isinstance(overrides, Mapping) and "approval_policy" in overrides:
        raise PermissionPostureError(
            f"harness_runtime.roles.{role}.codex cannot combine permission_posture "
            "with native.config_overrides.approval_policy"
        )


def compile_codex_permission_native(
    native: Mapping[str, Any],
    *,
    role_block: Mapping[str, Any],
    role: str,
) -> Mapping[str, Any]:
    raw_internal_keys = {
        key for key in (
            CODEX_NATIVE_PERMISSION_POSTURE_KEY,
            CODEX_NATIVE_EXECUTION_BOUNDARY_KEY,
        )
        if key in native
    }
    if raw_internal_keys:
        raise PermissionPostureError(
            f"harness_runtime.roles.{role}.codex native contains reserved "
            "permission posture keys: "
            + ", ".join(sorted(raw_internal_keys))
        )
    intent = codex_permission_intent_from_role_block(role_block, role=role)
    validate_no_codex_permission_dual_authority(
        native,
        role=role,
        has_transitional_intent=bool(intent),
    )
    if not intent:
        return dict(native)
    return {**dict(native), **intent}


def codex_permission_native_sandbox(native: Mapping[str, Any]) -> str | None:
    boundary = native.get(CODEX_NATIVE_EXECUTION_BOUNDARY_KEY)
    if boundary is None:
        return None
    if boundary not in CODEX_EXECUTION_BOUNDARY_SANDBOX:
        raise PermissionPostureError(
            f"codex execution_boundary has unsupported value {boundary!r}"
        )
    return CODEX_EXECUTION_BOUNDARY_SANDBOX[str(boundary)]


def codex_permission_native_overrides(native: Mapping[str, Any]) -> Mapping[str, Any]:
    validate_codex_permission_native(native, role="adapter")
    posture = native.get(CODEX_NATIVE_PERMISSION_POSTURE_KEY)
    if posture is None:
        return {}
    return {"approval_policy": CODEX_FULL_AUTO_APPROVAL_POLICY}


def requires_unattended_telegram(role_codex_block: Mapping[str, Any]) -> bool:
    intent = codex_permission_intent_from_role_block(role_codex_block, role="role")
    return intent.get(CODEX_NATIVE_PERMISSION_POSTURE_KEY) == CODEX_FULL_AUTO_PERMISSION_POSTURE


def validate_codex_permission_native(native: Mapping[str, Any], *, role: str) -> None:
    posture = native.get(CODEX_NATIVE_PERMISSION_POSTURE_KEY)
    boundary = native.get(CODEX_NATIVE_EXECUTION_BOUNDARY_KEY)
    if posture is None and boundary is None:
        return
    if posture is None:
        raise PermissionPostureError(f"{role}.codex native execution_boundary requires permission_posture")
    if boundary is None:
        raise PermissionPostureError(f"{role}.codex native permission_posture requires execution_boundary")
    if posture != CODEX_FULL_AUTO_PERMISSION_POSTURE:
        raise PermissionPostureError(f"{role}.codex native permission_posture has unsupported value {posture!r}")
    if boundary not in CODEX_EXECUTION_BOUNDARY_SANDBOX:
        raise PermissionPostureError(f"{role}.codex native execution_boundary has unsupported value {boundary!r}")
    validate_no_codex_permission_dual_authority(
        native,
        role=role,
        has_transitional_intent=True,
    )


def validate_harness_runtime_permission_postures(harness_runtime: Mapping[str, Any]) -> None:
    roles = harness_runtime.get("roles")
    if not isinstance(roles, Mapping):
        return
    for role, role_raw in roles.items():
        if not isinstance(role_raw, Mapping):
            continue
        for harness, block in role_raw.items():
            if not isinstance(block, Mapping):
                continue
            has_keys = (
                CODEX_PERMISSION_POSTURE_KEY in block
                or CODEX_EXECUTION_BOUNDARY_KEY in block
            )
            if harness != "codex":
                if has_keys:
                    raise PermissionPostureError(
                        f"harness_runtime.roles.{role}.{harness} contains Codex-only "
                        "permission posture keys"
                    )
                continue
            native = block.get("native", {})
            if native is None:
                native = {}
            if not isinstance(native, Mapping):
                continue
            compile_codex_permission_native(native, role_block=block, role=str(role))
