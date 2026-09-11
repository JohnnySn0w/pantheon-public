"""Namespaced diagnostic envelope for harness-specific bring-up checks."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Mapping, Protocol

from tools.harness_adapter.base import PantheonHarnessAdapter
from tools.harness_adapter import integration as harness_integration
from tools.harness_adapter.types import (
    DiagnosticObservation,
    DiagnosticReport,
    FailoverPolicy,
    HarnessLaunchConfig,
    HarnessName,
    PostStartCommand,
    RuntimeControllerConfig,
    TmuxTarget,
)
from tools.harness_permission_posture import (
    PermissionPostureError,
    compile_codex_permission_native,
)
from tools.mailbox_writable_roots import MailboxWritableRootError, resolve_mailbox_writable_roots


class HarnessDiagnosticProbe(Protocol):
    def collect(self) -> DiagnosticReport: ...


class RuntimeProfileError(ValueError):
    """Manifest runtime profile validation failed before any harness spawn."""


_MODES = {"interactive", "batch"}
_METERS = {"subscription", "sdk-credit", "api", "enterprise"}
_QUOTA_SOURCES = {"app-server", "statusline", "provider-api", "unavailable"}
_METER_QUOTA_PAIRS = {
    ("claude", "subscription", "statusline"),
    ("claude", "sdk-credit", "unavailable"),
    ("claude", "api", "provider-api"),
    ("claude", "enterprise", "provider-api"),
    ("codex", "subscription", "app-server"),
    ("codex", "api", "provider-api"),
    ("codex", "enterprise", "provider-api"),
}
_NATIVE_KEYS = {
    "claude": {"permission_mode", "channels", "effort"},
    "codex": {"sandbox", "config_overrides", "permission_posture", "execution_boundary"},
}


def normalize_harness(value: str) -> HarnessName:
    try:
        return harness_integration.normalize_harness(value)
    except ValueError as exc:
        raise RuntimeProfileError(f"unsupported harness {value!r}") from exc


def resolve_launch_config(
    manifest: Mapping[str, Any],
    *,
    role: str,
    account_home: Path,
    selected_harness: str,
    executable_override: Path | None = None,
    model_override: str | None = None,
    profile_override: str | None = None,
    bound_persona: str | None = None,
    bound_mailbox: Path | None = None,
    mailbox_hub: Path | None = None,
    root: Path = Path("/"),
) -> HarnessLaunchConfig:
    """Resolve the immutable runtime profile selected by role+harness.

    This helper is intentionally side-effect free: it resolves and validates the
    config shape, but executable existence stays a diagnostic check so tests can
    exercise "known missing" paths without invoking a provider.
    """
    harness = normalize_harness(selected_harness)
    runtime = manifest.get("harness_runtime")
    if runtime is None:
        launch = _legacy_launch_config(
            harness,
            account_home=account_home,
            executable_override=executable_override,
            model_override=model_override,
        )
        _validate_fallbacks(manifest, role=role, launch=launch)
        return launch
    if not isinstance(runtime, Mapping):
        raise RuntimeProfileError("harness_runtime must be a mapping")
    if runtime.get("schema") != "pantheon-harness-runtime/v1":
        raise RuntimeProfileError("harness_runtime.schema must be pantheon-harness-runtime/v1")

    harnesses = _mapping(runtime, "harnesses")
    harness_block = _mapping(harnesses, harness)
    default_profile = _required_str(harness_block, "default_profile")
    profile_name = profile_override or _role_harness(runtime, role, harness).get("profile")
    if profile_name is None:
        profile_name = default_profile
    if not isinstance(profile_name, str) or not profile_name:
        raise RuntimeProfileError(f"{role}.{harness}.profile must be a non-empty string")

    profiles = _mapping(harness_block, "profiles")
    profile = _mapping(profiles, profile_name)
    executable = _resolve_executable(
        _required_str(profile, "executable"),
        account_home=account_home,
    )
    sources = [f"harness_runtime.harnesses.{harness}.profiles.{profile_name}"]
    role_block = _role_harness(runtime, role, harness)
    if harness != "codex":
        forbidden_posture_keys = {
            key for key in ("permission_posture", "execution_boundary")
            if key in role_block
        }
        if forbidden_posture_keys:
            raise RuntimeProfileError(
                f"{role}.{harness} contains Codex-only permission posture keys: "
                + ", ".join(sorted(forbidden_posture_keys))
            )
    if role_block:
        sources.append(f"harness_runtime.roles.{role}.{harness}")
    if executable_override is not None:
        if not executable_override.is_absolute():
            raise RuntimeProfileError("executable override must be absolute")
        executable = executable_override
        sources.append("override.executable")

    mode = _literal(profile, "mode", _MODES)
    meter = _literal(profile, "meter", _METERS)
    quota_source = _literal(profile, "quota_source", _QUOTA_SOURCES)
    known_meter_pair = harness in _NATIVE_KEYS
    if known_meter_pair and (harness, meter, quota_source) not in _METER_QUOTA_PAIRS:
        raise RuntimeProfileError(
            f"{harness}/{profile_name} incompatible meter/quota_source "
            f"{meter}/{quota_source}"
        )
    if not profile_override and profile_name == default_profile and mode != "interactive":
        raise RuntimeProfileError(f"{harness} default_profile must be interactive")
    if harness == "claude" and not profile_override and profile_name == "batch-sdk":
        raise RuntimeProfileError("claude batch-sdk profile must be selected explicitly")

    model = role_block.get("model", profile.get("model"))
    if model is not None and not isinstance(model, str):
        raise RuntimeProfileError(f"{role}.{harness}.model must be string or null")
    if model_override is not None:
        model = model_override
        sources.append("override.model")

    native = role_block.get("native", {})
    if native is None:
        native = {}
    if not isinstance(native, Mapping):
        raise RuntimeProfileError(f"{role}.{harness}.native must be a mapping")
    if harness == "codex":
        try:
            native = compile_codex_permission_native(
                native,
                role_block=role_block,
                role=role,
            )
        except PermissionPostureError as exc:
            raise RuntimeProfileError(str(exc)) from exc
    _validate_native_keys(harness, native)
    writable_roots: tuple[Path, ...] = ()
    if bound_persona is not None or bound_mailbox is not None or mailbox_hub is not None:
        if bound_persona is None or bound_mailbox is None or mailbox_hub is None:
            raise RuntimeProfileError("mailbox writable root resolution requires persona, mailbox, and hub")
        try:
            writable_roots = tuple(
                item.path
                for item in resolve_mailbox_writable_roots(
                    manifest,
                    role=role,
                    harness=harness,
                    bound_persona=bound_persona,
                    bound_mailbox=bound_mailbox,
                    mailbox_hub=mailbox_hub,
                    root=root,
                )
            )
        except MailboxWritableRootError as exc:
            raise RuntimeProfileError(str(exc)) from exc

    launch = HarnessLaunchConfig(
        harness=harness,
        profile=profile_name,
        executable=executable,
        model=model,
        mode=mode,  # type: ignore[arg-type]
        meter=meter,  # type: ignore[arg-type]
        quota_source=quota_source,  # type: ignore[arg-type]
        native=dict(native),
        sources=tuple(sources),
        writable_roots=writable_roots,
    )
    try:
        harness_integration.integration_for(harness).validate_launch_config(launch)
    except ValueError as exc:
        raise RuntimeProfileError(str(exc)) from exc
    _validate_fallbacks(manifest, role=role, launch=launch)
    return launch


def resolve_controller_config(
    manifest: Mapping[str, Any],
    *,
    persona: str,
    role: str,
) -> RuntimeControllerConfig:
    runtime = manifest.get("harness_runtime")
    fallbacks: tuple[str, ...] = ()
    role_tmux: Mapping[str, str] = {}
    post_start: tuple[PostStartCommand, ...] = ()
    if isinstance(runtime, Mapping):
        controller = runtime.get("controller")
        controller = controller if isinstance(controller, Mapping) else {}
        tmux_raw = controller.get("tmux")
        tmux_raw = tmux_raw if isinstance(tmux_raw, Mapping) else {}
        quota_raw = controller.get("quota")
        quota_raw = quota_raw if isinstance(quota_raw, Mapping) else {}
        roles = runtime.get("roles")
        role_raw = roles.get(role) if isinstance(roles, Mapping) else None
        role_tmux = _validate_role_tmux_override(role_raw, role=role)
        post_start = _validate_post_start_commands(role_raw, role=role)
        if isinstance(role_raw, Mapping):
            # T-2635: was `()` (empty TUPLE) on both the default and the
            # None-branch below -- an empty tuple unconditionally fails the
            # very next `isinstance(raw_fallbacks, list)` check, so ANY role
            # with no explicit `fallbacks:` key (or an explicit `fallbacks:
            # null`) raised here. Mirrors the sibling `_validate_fallbacks()`
            # below, which already uses the correct `[]` default. Discovered
            # chasing T-2633 (Specialist's specialist role has no fallbacks key by
            # design; recovery's recovery role happens to declare one,
            # which is why this never fired before).
            raw_fallbacks = role_raw.get("fallbacks", [])
            if raw_fallbacks is None:
                raw_fallbacks = []
            if not isinstance(raw_fallbacks, list) or not all(
                isinstance(item, str) for item in raw_fallbacks
            ):
                raise RuntimeProfileError(f"harness_runtime.roles.{role}.fallbacks must be strings")
            fallbacks = tuple(raw_fallbacks)
    else:
        tmux_raw = {}
        quota_raw = {}

    session_template = str(role_tmux.get("session", tmux_raw.get("session", "pantheon-{persona}")))
    window = str(role_tmux.get("window", tmux_raw.get("window", "harness")))
    pane = str(role_tmux.get("pane", tmux_raw.get("pane", "0")))
    _validate_stable_tmux_template(session_template)
    _validate_stable_tmux_template(window)
    _validate_stable_tmux_template(pane)
    threshold = quota_raw.get("weekly_remaining_percent", 5)
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise RuntimeProfileError("weekly_remaining_percent must be numeric")
    return RuntimeControllerConfig(
        tmux=TmuxTarget(
            session=session_template.format(persona=persona),
            window=window.format(persona=persona),
            pane=pane.format(persona=persona),
        ),
        policy=FailoverPolicy(
            weekly_remaining_percent=float(threshold),
            fallbacks=fallbacks,
        ),
        post_start=post_start,
    )


class BasicHarnessDiagnosticProbe:
    """Cheap, non-provider-paid checks shared by both bindings.

    The check IDs are namespaced by harness and remain independently meaningful;
    consumers must not compare checks by position.
    """

    def __init__(
        self,
        adapter: PantheonHarnessAdapter,
        *,
        cwd: Path,
        execution_context: str,
    ) -> None:
        self.adapter = adapter
        self.cwd = cwd
        self.execution_context = execution_context

    def collect(self) -> DiagnosticReport:
        harness = self.adapter.HARNESS_NAME
        observations = [
            self._binary_observation(harness),
            self._cwd_observation(harness),
            self._config_observation(harness),
            self._standing_prompt_observation(harness),
            self._auth_observation(harness),
        ]
        observations.extend(self.adapter.validate_launch_config())
        surfaces = self.adapter.describe_surfaces(self.cwd)
        return DiagnosticReport(
            harness=harness,  # type: ignore[arg-type]
            execution_context=self.execution_context,
            observations=tuple(observations),
            launch=self.adapter.launch,
            surfaces=surfaces,
        )

    def _binary_observation(self, harness: str) -> DiagnosticObservation:
        executable = self.adapter.executable
        if executable.is_absolute():
            resolved = str(executable)
            ok = executable.is_file() and os.access(executable, os.X_OK)
        else:
            resolved = shutil.which(str(executable)) or ""
            ok = bool(resolved)
        return DiagnosticObservation(
            check_id=f"{harness}.executable",
            status="pass" if ok else "fail",
            summary="configured executable is usable" if ok else "configured executable is unavailable",
            authoritative=True,
            evidence={
                "configured_path": str(executable),
                "path": resolved,
                "execution_context": self.execution_context,
                "raw_exit": "not-run",
            },
        )

    def _cwd_observation(self, harness: str) -> DiagnosticObservation:
        usable = self.cwd.is_dir() and os.access(self.cwd, os.R_OK | os.X_OK)
        return DiagnosticObservation(
            check_id=f"{harness}.cwd",
            status="pass" if usable else "fail",
            summary="working directory is readable" if usable else "working directory unavailable",
            authoritative=True,
            evidence={"cwd": str(self.cwd), "execution_context": self.execution_context},
        )

    def _config_observation(self, harness: str) -> DiagnosticObservation:
        path = self.adapter.mcp_server_config_path()
        exists = path.is_file()
        readable = exists and os.access(path, os.R_OK)
        return DiagnosticObservation(
            check_id=f"{harness}.config",
            status="pass" if readable else "warn",
            summary="native config readable" if readable else "native config absent or unreadable",
            authoritative=True,
            evidence={
                "path": str(path),
                "exists": str(exists).lower(),
                "execution_context": self.execution_context,
            },
        )

    def _standing_prompt_observation(self, harness: str) -> DiagnosticObservation:
        name = self.adapter.standing_prompt_filename()
        found = _find_upward(self.cwd, name)
        return DiagnosticObservation(
            check_id=f"{harness}.standing_prompt",
            status="pass" if found else "warn",
            summary="standing prompt discovered" if found else "standing prompt not discovered",
            authoritative=True,
            evidence={
                "filename": name,
                "path": str(found or ""),
                "execution_context": self.execution_context,
            },
        )

    def _auth_observation(self, harness: str) -> DiagnosticObservation:
        descriptor = self.adapter.auth_descriptor()
        readable_paths = [
            path
            for path in descriptor.material_paths
            if path.is_file() and os.access(path, os.R_OK)
        ]
        return DiagnosticObservation(
            check_id=f"{harness}.auth_material",
            status="unavailable",
            summary="auth material described but live auth not probed",
            authoritative=False,
            evidence={
                "paths": os.pathsep.join(str(path) for path in descriptor.material_paths),
                "descriptor_only": "true",
                "observed_state": descriptor.observed_state,
                "readable_paths": os.pathsep.join(str(path) for path in readable_paths),
                "execution_context": self.execution_context,
            },
        )


def _find_upward(cwd: Path, filename: str) -> Path | None:
    current = cwd.resolve()
    while True:
        candidate = current / filename
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def _legacy_launch_config(
    harness: HarnessName,
    *,
    account_home: Path,
    executable_override: Path | None,
    model_override: str | None,
) -> HarnessLaunchConfig:
    if executable_override is not None:
        if not executable_override.is_absolute():
            raise RuntimeProfileError("executable override must be absolute")
        executable = executable_override
        sources = ("legacy.default", "override.executable")
    elif harness == "claude":
        executable = account_home / ".npm-global" / "bin" / "claude"
        sources = ("legacy.default",)
    else:
        executable = Path("/usr/bin/codex")
        sources = ("legacy.default",)
    return HarnessLaunchConfig(
        harness=harness,
        profile=(
            "interactive-subscription"
            if harness == "claude"
            else "interactive-chatgpt"
        ),
        executable=executable,
        model=model_override,
        mode="interactive",
        meter="subscription",
        quota_source="statusline" if harness == "claude" else "app-server",
        native={},
        sources=sources + (("override.model",) if model_override else ()),
    )


def _mapping(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise RuntimeProfileError(f"{key} must be a mapping")
    return value


def _required_str(parent: Mapping[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeProfileError(f"{key} must be a non-empty string")
    return value


def _literal(parent: Mapping[str, Any], key: str, allowed: set[str]) -> str:
    value = _required_str(parent, key)
    if value not in allowed:
        raise RuntimeProfileError(f"{key} has unsupported value {value!r}")
    return value


def _role_harness(
    runtime: Mapping[str, Any],
    role: str,
    harness: HarnessName,
) -> Mapping[str, Any]:
    roles = runtime.get("roles")
    if not isinstance(roles, Mapping):
        return {}
    role_block = roles.get(role)
    if not isinstance(role_block, Mapping):
        return {}
    harness_block = role_block.get(harness)
    if harness_block is None:
        return {}
    if not isinstance(harness_block, Mapping):
        raise RuntimeProfileError(f"harness_runtime.roles.{role}.{harness} must be a mapping")
    return harness_block


def _resolve_executable(template: str, *, account_home: Path) -> Path:
    if "{" in template or "}" in template:
        if template.count("{account_home}") != 1 or template.replace("{account_home}", "") == template:
            raise RuntimeProfileError("executable templates support only {account_home}")
        template = template.replace("{account_home}", str(account_home))
    path = Path(template)
    if not path.is_absolute():
        raise RuntimeProfileError("runtime executable must resolve to an absolute path")
    return path


def _validate_native_keys(harness: HarnessName, native: Mapping[str, Any]) -> None:
    allowed = _NATIVE_KEYS.get(harness, set())
    unknown = sorted(set(native) - allowed)
    if unknown:
        raise RuntimeProfileError(
            f"{harness} native keys are not recognized: {', '.join(unknown)}"
        )
    if harness == "claude" and "permission_mode" in native:
        permission_mode = native["permission_mode"]
        if not isinstance(permission_mode, str) or not permission_mode.strip():
            raise RuntimeProfileError(
                "claude native permission_mode must be a non-empty string"
            )
    if harness == "claude" and "channels" in native:
        channels = native["channels"]
        if not isinstance(channels, str) or not channels.strip():
            raise RuntimeProfileError(
                "claude native channels must be a non-empty string"
            )
    if harness == "claude" and "effort" in native:
        effort = native["effort"]
        if not isinstance(effort, str) or not effort.strip():
            raise RuntimeProfileError(
                "claude native.effort must be a non-empty string"
            )


def _validate_fallbacks(
    manifest: Mapping[str, Any],
    *,
    role: str,
    launch: HarnessLaunchConfig,
) -> None:
    runtime = manifest.get("harness_runtime")
    if not isinstance(runtime, Mapping):
        return
    roles = runtime.get("roles")
    role_block = roles.get(role) if isinstance(roles, Mapping) else None
    if not isinstance(role_block, Mapping):
        return
    fallbacks = role_block.get("fallbacks", [])
    if fallbacks is None:
        return
    if not isinstance(fallbacks, list) or not all(isinstance(item, str) for item in fallbacks):
        raise RuntimeProfileError(f"harness_runtime.roles.{role}.fallbacks must be strings")
    harnesses = _mapping(runtime, "harnesses")
    seen: set[str] = set()
    for item in fallbacks:
        if item in seen:
            raise RuntimeProfileError(f"fallback {item} self-cycles in fallback order")
        seen.add(item)
        try:
            fallback_harness, fallback_profile = item.split("/", 1)
        except ValueError as exc:
            raise RuntimeProfileError(f"fallback {item!r} must be harness/profile") from exc
        normalized = normalize_harness(fallback_harness)
        profiles = _mapping(_mapping(harnesses, normalized), "profiles")
        if fallback_profile not in profiles:
            raise RuntimeProfileError(f"fallback profile {item!r} does not exist")


def _validate_role_tmux_override(
    role_raw: Mapping[str, Any] | None,
    *,
    role: str,
) -> Mapping[str, str]:
    """Validate and return the optional per-role tmux template override.

    Absent a `tmux` key on the role (the common case today), this returns an empty
    mapping and `resolve_controller_config` falls through to the global
    `controller.tmux` template for every key — the resolved TmuxTarget stays
    byte-identical to today's output. Present keys are restricted to the same
    session/window/pane shape as the global template and are still run through
    `_validate_stable_tmux_template` by the caller after merging.
    """
    if not isinstance(role_raw, Mapping):
        return {}
    tmux_override = role_raw.get("tmux")
    if tmux_override is None:
        return {}
    if not isinstance(tmux_override, Mapping):
        raise RuntimeProfileError(f"harness_runtime.roles.{role}.tmux must be a mapping")
    allowed = {"session", "window", "pane"}
    unknown = sorted(set(tmux_override) - allowed)
    if unknown:
        raise RuntimeProfileError(
            f"harness_runtime.roles.{role}.tmux keys are not recognized: {', '.join(unknown)}"
        )
    for key, value in tmux_override.items():
        if not isinstance(value, str) or not value:
            raise RuntimeProfileError(
                f"harness_runtime.roles.{role}.tmux.{key} must be a non-empty string"
            )
    return tmux_override


_POST_START_SUBSTITUTION_PROBE = {
    "persona": "probe",
    "tmux_target": "probe:0.0",
    "account_home": "/tmp/probe",
}


def _validate_post_start_commands(
    role_raw: Mapping[str, Any] | None,
    *,
    role: str,
) -> tuple[PostStartCommand, ...]:
    """Validate and return the optional per-role post-runtime-start command list.

    Absent a ``post_start`` key on the role (the common case today, and the
    only case before T-2606), this returns an empty tuple and
    ``resolve_controller_config``'s output carries no hooks -- the control
    plane's post-start step (``runtime_controller._run_post_start_commands``)
    becomes a no-op loop over zero entries, so bringup stays byte-identical
    to today. Present entries are shell-free argv lists that may reference
    ``{persona}``, ``{tmux_target}``, and ``{account_home}`` -- the actual
    substitution happens at invocation time against the live binding, not
    here. This function only validates the *shape* and the *token
    vocabulary* early (via a dummy-value probe format, mirroring
    ``_validate_stable_tmux_template``) so a malformed manifest fails at
    load time rather than mid-incident, the first time the hook fires.
    """
    if not isinstance(role_raw, Mapping):
        return ()
    raw = role_raw.get("post_start")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RuntimeProfileError(f"harness_runtime.roles.{role}.post_start must be a list")
    commands: list[PostStartCommand] = []
    for index, entry in enumerate(raw):
        prefix = f"harness_runtime.roles.{role}.post_start[{index}]"
        if not isinstance(entry, Mapping):
            raise RuntimeProfileError(f"{prefix} must be a mapping")
        allowed = {"command", "background"}
        unknown = sorted(set(entry) - allowed)
        if unknown:
            raise RuntimeProfileError(f"{prefix} keys are not recognized: {', '.join(unknown)}")
        argv = entry.get("command")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise RuntimeProfileError(
                f"{prefix}.command must be a non-empty list of non-empty strings"
            )
        for token in argv:
            try:
                token.format(**_POST_START_SUBSTITUTION_PROBE)
            except (KeyError, ValueError) as exc:
                raise RuntimeProfileError(
                    f"{prefix}.command supports only "
                    "{persona}, {tmux_target}, {account_home}"
                ) from exc
        background = entry.get("background", False)
        if not isinstance(background, bool):
            raise RuntimeProfileError(f"{prefix}.background must be a bool")
        commands.append(PostStartCommand(argv=tuple(argv), background=background))
    return tuple(commands)


def _validate_stable_tmux_template(value: str) -> None:
    if "{harness}" in value or "{model}" in value or "{profile}" in value:
        raise RuntimeProfileError("tmux target template must not vary by runtime")
    try:
        value.format(persona="probe")
    except (KeyError, ValueError) as exc:
        raise RuntimeProfileError("tmux target template supports only {persona}") from exc
