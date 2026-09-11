"""ClaudeHarnessAdapter — concrete binding for the claude-code CLI harness.

Implements PantheonHarnessAdapter for claude-code (the primary Pantheon harness:
Orchestrator/HO, Specialist, Recovery).  All 7 generalized + 2 bridge surfaces are implemented
with real key/filename translations.

Not-abstracted surfaces (sandbox_config, profiles_config, app_server_config,
cloud_config, doctor_config) remain provider-specific descriptions. They are
not forced into Codex-shaped policy.

References:
    adr/ORG-031-harness-abstraction-and-daemon-onboarding.md §Decision (i)
    knowledge/episodic/2026-06-04-codex-harness-abstraction-synthesis/
      notes.md §2 (capability map)
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.harness_adapter._common import (
    deliver_to_mailbox,
    finalize_events,
    parse_json_object,
)
from tools.harness_adapter.base import PantheonHarnessAdapter
from tools.harness_adapter.types import (
    AuthDescriptor,
    CommandSpec,
    DeliveryReceipt,
    DiagnosticObservation,
    ExecutionRequest,
    ExecutionResult,
    HarnessLaunchConfig,
    HarnessCapabilities,
    HarnessEvent,
    HarnessSurfaceDescriptors,
    HookCapability,
    InboundRequest,
    McpDescriptor,
    MemoryDescriptor,
    ProviderBillingEvidence,
    PostStartAssessment,
    PostStartPaneSample,
    ProviderCompletionAssessment,
    ProviderCompletionProbe,
    RoleDescriptor,
    SessionHandle,
    StandingInstructionsDescriptor,
    TmuxTarget,
    Usage,
)
from tools.claude_statusline_capture import SCHEMA as STATUSLINE_SCHEMA
from tools.claude_quota_probe import CAP_SENTINELS, READY_PROMPT_LINE
from tools.harness_adapter.quota import QuotaObserver
from tools.harness_adapter.provider_quota import (
    ClaudeAcceptanceQuotaOverlayObserver,
    ClaudeStatuslineQuotaObserver,
    UnavailableQuotaObserver,
    valid_acceptance_run_id,
)


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_COMPLETION_SCHEMA = "pantheon-harness-provider-completion/v1"
_MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
_MAX_TRANSCRIPT_DELTA_BYTES = 256 * 1024
_MAX_TRANSCRIPT_LINES = 400
_MAX_JSONL_LINE_BYTES = 64 * 1024
_MAX_STATUSLINE_BYTES = 1024 * 1024


def _normalize_provider_pane(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\r", "")


def _claude_cap_sentinel(text: str) -> str | None:
    normalized = _normalize_provider_pane(text)
    for pattern in CAP_SENTINELS:
        for line in normalized.splitlines():
            match = pattern.match(line.strip())
            if match:
                return match.group(1)
    return None


def _has_claude_rate_limit_modal_framing(text: str) -> bool:
    normalized = _normalize_provider_pane(text).lower()
    required = (
        "/rate-limit-options",
        "what do you want to do?",
        "stop and wait for limit to reset",
        "upgrade your plan",
    )
    return all(item in normalized for item in required)


def classify_claude_post_start_subscription_cap(
    *,
    baseline_text: str,
    sample_text: str,
) -> tuple[str | None, str | None]:
    """Return a narrow provider cap classification for changed post-start UI."""
    if _normalize_provider_pane(baseline_text) == _normalize_provider_pane(sample_text):
        return None, None
    if _claude_cap_sentinel(baseline_text):
        return None, None
    sentinel = _claude_cap_sentinel(sample_text)
    if not sentinel:
        return None, None
    if not _has_claude_rate_limit_modal_framing(sample_text):
        return None, None
    return "claude-rate-limit-options-modal", "post-start-subscription-cap"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str | None:
    try:
        raw, _st = _read_regular_file_no_follow(path, max_bytes=_MAX_STATUSLINE_BYTES)
        return hashlib.sha256(raw).hexdigest()
    except OSError:
        return None


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _read_statusline(path: Path) -> Mapping[str, Any] | None:
    try:
        raw, _st = _read_regular_file_no_follow(path, max_bytes=_MAX_STATUSLINE_BYTES)
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, Mapping) or payload.get("schema") != STATUSLINE_SCHEMA:
        return None
    return payload


def _read_regular_file_no_follow(path: Path, *, max_bytes: int) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            raise OSError("provider evidence file is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = st.st_size
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), st
    finally:
        os.close(fd)


def _confined_transcript(account_home: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    raw = Path(value)
    if not raw.is_absolute():
        return None
    try:
        projects = (account_home / ".claude" / "projects").resolve()
        resolved = raw.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(projects):
        return None
    try:
        _raw, st = _read_regular_file_no_follow(resolved, max_bytes=_MAX_TRANSCRIPT_BYTES)
    except OSError:
        return None
    return resolved


def _transcript_boundary(account_home: Path, observation: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if observation is None:
        return None
    path = _confined_transcript(account_home, observation.get("transcript_path"))
    if path is None:
        return None
    try:
        _raw, st = _read_regular_file_no_follow(path, max_bytes=_MAX_TRANSCRIPT_BYTES)
    except OSError:
        return None
    relative = str(path.relative_to((account_home / ".claude" / "projects").resolve()))
    return {
        "path": path,
        "offset": st.st_size,
        "inode": st.st_ino,
        "device": st.st_dev,
        "relative_sha256": _sha256_text(relative),
    }


def _assistant_delta_has_nonce(path: Path, *, start: int, marker: str) -> tuple[bool, int, int, int]:
    try:
        raw_all, st = _read_regular_file_no_follow(path, max_bytes=_MAX_TRANSCRIPT_BYTES)
    except OSError:
        return False, start, 0, 1
    if start < 0 or st.st_size < start or st.st_size > _MAX_TRANSCRIPT_BYTES:
        return False, start, 0, 1
    delta_size = st.st_size - start
    if delta_size > _MAX_TRANSCRIPT_DELTA_BYTES:
        return False, st.st_size, 0, 1
    raw = raw_all[start:]
    if raw and not raw.endswith(b"\n"):
        raw = raw.rsplit(b"\n", 1)[0] if b"\n" in raw else b""
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) > _MAX_TRANSCRIPT_LINES:
        return False, st.st_size, 0, 1
    assistant_events = 0
    malformed = 0
    for line in lines:
        if len(line) > _MAX_JSONL_LINE_BYTES:
            malformed += 1
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed += 1
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role is not None and role != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        assistant_events += 1
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip() == marker:
                return True, st.st_size, assistant_events, malformed
    return False, st.st_size, assistant_events, malformed


class ClaudeHarnessAdapter(PantheonHarnessAdapter):
    """Harness adapter for claude-code (Orchestrator / Specialist / Recovery instances).

    Args:
        role_domain: ORG-027 instance role-key (e.g. "orchestrator").
        claude_home: path to the claude user home; defaults to Path.home().
                     Override in tests to avoid reading real home-dir config.
    """

    HARNESS_NAME: str = "claude"

    def __init__(
        self,
        role_domain: str,
        claude_home: Path | None = None,
        mailbox_path: Path | None = None,
        launch: HarnessLaunchConfig | None = None,
    ) -> None:
        super().__init__(role_domain, launch=launch)
        self._claude_home = claude_home if claude_home is not None else Path.home()
        self._mailbox_path = mailbox_path

    # -------------------------------------------------------------------------
    # Typed lifecycle contract
    # -------------------------------------------------------------------------

    def capabilities(self) -> HarnessCapabilities:
        hook_names = (
            "PreToolUse",
            "PermissionRequest",
            "PostToolUse",
            "PostToolUseFailure",
            "Notification",
            "UserPromptSubmit",
            "SessionStart",
            "SessionEnd",
            "Stop",
            "SubagentStart",
            "SubagentStop",
            "PreCompact",
            "PostCompact",
            "TeammateIdle",
            "TaskCompleted",
            "ConfigChange",
            "WorktreeCreate",
            "WorktreeRemove",
            "InstructionsLoaded",
        )
        hooks = {
            name: HookCapability(
                event=name,
                matchers=name
                not in {
                    "UserPromptSubmit",
                    "SessionStart",
                    "SessionEnd",
                    "Stop",
                    "SubagentStart",
                    "SubagentStop",
                    "PreCompact",
                    "PostCompact",
                },
                can_block=name
                in {
                    "PreToolUse",
                    "PermissionRequest",
                    "UserPromptSubmit",
                    "Stop",
                    "SubagentStop",
                    "TaskCompleted",
                    "ConfigChange",
                    "WorktreeCreate",
                },
                can_continue=name in {"Stop", "SubagentStop"},
                handler_types=("command", "prompt", "agent"),
            )
            for name in hook_names
        }
        return HarnessCapabilities(
            harness="claude",
            session_resume_by_id=True,
            session_resume_last=True,
            structured_progress=True,
            structured_output_schema=True,
            local_generated_memory=True,
            native_stop_continuation=True,
            inbound_mechanisms=("channel", "maildir"),
            hook_events=hooks,
        )

    def build_exec(self, request: ExecutionRequest) -> CommandSpec:
        argv = [
            str(self.executable),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        model = request.model or (self.launch.model if self.launch else None)
        if model:
            argv.extend(["--model", model])
        if request.output_schema:
            argv.extend(["--json-schema", str(request.output_schema)])
        return CommandSpec(
            argv=tuple(argv),
            stdin=request.prompt,
            cwd=request.cwd,
        )

    def build_resume(
        self,
        session: SessionHandle,
        prompt: str | None = None,
    ) -> CommandSpec:
        cwd = session.cwd if session.cwd is not None else Path.cwd()
        return CommandSpec(
            argv=(
                str(self.executable),
                "-p",
                "--resume",
                session.session_id,
                "--output-format",
                "stream-json",
                "--verbose",
            ),
            stdin=prompt,
            cwd=cwd,
        )

    def parse_event(self, line: str) -> HarnessEvent | None:
        raw, parse_error = parse_json_object(line)
        if parse_error:
            return HarnessEvent(
                harness="claude",
                kind="error",
                session_id=None,
                message=parse_error,
                usage=None,
                raw={"line": line.rstrip("\n"), "category": "provider"},
            )
        if raw is None:
            return None

        native_type = raw.get("type")
        session_id = raw.get("session_id")
        session_id = session_id if isinstance(session_id, str) else None
        usage_raw = raw.get("usage")
        usage = _claude_usage(usage_raw, raw.get("total_cost_usd"))
        message: str | None = None
        kind = "progress"

        if native_type == "system" and raw.get("subtype") == "init":
            kind = "session_started"
        elif native_type == "assistant":
            content = raw.get("message")
            if isinstance(content, dict):
                message = _claude_text(content.get("content"))
            kind = "progress"
        elif native_type in {"tool_progress", "tool_use"}:
            kind = "tool_started"
        elif native_type == "tool_use_summary":
            kind = "tool_completed"
            summary = raw.get("summary")
            message = summary if isinstance(summary, str) else None
        elif native_type == "rate_limit_event":
            kind = "retry_scheduled"
            message = str(raw.get("message") or "rate limit event")
        elif native_type == "result":
            result = raw.get("result")
            message = result if isinstance(result, str) else None
            is_error = bool(raw.get("is_error")) or raw.get("subtype") not in {
                None,
                "success",
            }
            kind = "turn_failed" if is_error else "turn_completed"
        return HarnessEvent(
            harness="claude",
            kind=kind,
            session_id=session_id,
            message=message,
            usage=usage,
            raw=raw,
        )

    def finalize(
        self,
        events: Iterable[HarnessEvent],
        *,
        returncode: int,
        stderr: str,
    ) -> ExecutionResult:
        captured = tuple(events)
        return finalize_events(
            "claude",
            captured,
            returncode=returncode,
            stderr=stderr,
            provider_billing=_claude_provider_billing(captured),
        )

    def deliver_inbound(self, request: InboundRequest) -> DeliveryReceipt:
        return deliver_to_mailbox(
            "claude",
            request,
            default_inbox=self._mailbox_path,
            default_sender="harness-adapter",
        )

    def spawn_error_event(self, exc: OSError) -> HarnessEvent:
        return HarnessEvent(
            harness="claude",
            kind="error",
            session_id=None,
            message=f"{type(exc).__name__}: {exc}",
            usage=None,
            raw={"category": "config", "retryable": False},
        )

    def validate_launch_config(self) -> tuple[DiagnosticObservation, ...]:
        if self.launch is None:
            return (
                DiagnosticObservation(
                    check_id="claude.runtime_profile",
                    status="warn",
                    summary="legacy adapter has no immutable runtime profile",
                    authoritative=True,
                    evidence={"profile": "", "legacy": "true"},
                ),
            )
        allowed_native = {"permission_mode", "channels", "effort"}
        unknown = sorted(set(self.launch.native) - allowed_native)
        status = "fail" if unknown else "pass"
        return (
            DiagnosticObservation(
                check_id="claude.runtime_profile",
                status=status,
                summary=(
                    "claude runtime profile is valid"
                    if status == "pass"
                    else "claude runtime profile contains unknown native keys"
                ),
                authoritative=True,
                evidence={
                    "harness": self.launch.harness,
                    "profile": self.launch.profile,
                    "mode": self.launch.mode,
                    "meter": self.launch.meter,
                    "quota_source": self.launch.quota_source,
                    "unknown_native_keys": ",".join(unknown),
                    "native_namespace": "claude",
                },
            ),
        )

    def describe_surfaces(self, cwd: Path) -> HarnessSurfaceDescriptors:
        standing = self.standing_prompt_filename()
        discovered = _find_upward(cwd, standing)
        return HarnessSurfaceDescriptors(
            standing=StandingInstructionsDescriptor(
                filename=standing,
                discovered=(discovered,) if discovered else (),
                load_order=("cwd-upward", "claude-home"),
                authoritative=True,
                evidence={"harness": "claude", "cwd": str(cwd)},
            ),
            auth=self.auth_descriptor(),
            memory=MemoryDescriptor(
                available=True,
                enabled=False,
                readable=None,
                writable=None,
                evidence={
                    "harness": "claude",
                    "enabled_by_adapter": "false",
                    "claim": "descriptor-only",
                },
            ),
            mcp=McpDescriptor(
                config_paths=(self.mcp_server_config_path(), cwd / ".mcp.json"),
                configured_servers=(),
                scope=("user", "project"),
                authoritative=False,
                evidence={"harness": "claude", "native_format": "json"},
            ),
            roles=RoleDescriptor(
                config_paths=(self._claude_home / ".claude" / "settings.json",),
                configured_roles=(),
                delegation_modes=("agent-tool",),
                authoritative=False,
                evidence={"harness": "claude", "schema": "claude-native"},
            ),
        )

    def auth_descriptor(self) -> AuthDescriptor:
        return AuthDescriptor(
            mechanisms=("oauth", "api-key"),
            material_paths=(
                self._claude_home / ".claude" / ".credentials.json",
                self._claude_home / ".claude" / "settings.json",
            ),
            observed_state="unverified",
            authoritative=False,
            evidence={
                "harness": "claude",
                "descriptor_only": "true",
                "live_probe": "not-run",
            },
        )

    def build_interactive_launch(self, target: TmuxTarget, *, cwd: Path) -> CommandSpec:
        argv = [str(self.executable)]
        channels = (
            self.launch.native.get("channels")
            if self.launch and isinstance(self.launch.native, dict)
            else None
        )
        if isinstance(channels, str) and channels:
            argv.extend(["--channels", channels])
        model = self.launch.model if self.launch else None
        if model:
            argv.extend(["--model", model])
        effort = (
            self.launch.native.get("effort")
            if self.launch and isinstance(self.launch.native, dict)
            else None
        )
        if isinstance(effort, str) and effort:
            argv.extend(["--effort", effort])
        permission_mode = (
            self.launch.native.get("permission_mode")
            if self.launch and isinstance(self.launch.native, dict)
            else None
        )
        if permission_mode is None:
            permission_mode = "default"
        if isinstance(permission_mode, str):
            argv.extend(["--permission-mode", permission_mode])
        return CommandSpec(
            argv=tuple(argv),
            stdin=None,
            cwd=cwd,
            env={
                "PANTHEON_TMUX_SESSION": target.session,
                "PANTHEON_TMUX_WINDOW": target.window,
                "PANTHEON_TMUX_PANE": target.pane,
            },
        )

    def quota_observer(self, state_dir: Path) -> QuotaObserver:
        profile = self.launch.profile if self.launch else "legacy"
        run_id = os.environ.get("PANTHEON_ACCEPTANCE_RUN_ID")
        overlay = os.environ.get("PANTHEON_ACCEPTANCE_QUOTA_OVERLAY")
        if run_id is None and overlay is None:
            return ClaudeStatuslineQuotaObserver(
                state_dir / "quota" / "claude-statusline.json",
                profile=f"claude/{profile}",
            )
        if not _valid_acceptance_overlay_config(run_id, overlay):
            return UnavailableQuotaObserver(
                profile=f"claude/{profile}",
                reason="invalid-acceptance-overlay-config",
                evidence={
                    "provider": "claude",
                    "source": "acceptance-overlay",
                    "fixture": "true",
                    "run_id_present": str(run_id is not None).lower(),
                    "overlay_present": str(overlay is not None).lower(),
                },
            )
        assert run_id is not None
        assert overlay is not None
        canonical_path = state_dir / "quota" / "claude-statusline.json"
        return ClaudeAcceptanceQuotaOverlayObserver(
            Path(overlay),
            canonical_path=canonical_path,
            profile=f"claude/{profile}",
            run_id=run_id,
            state_dir=state_dir,
        )

    def assess_post_start_readiness(
        self,
        *,
        harness: str,
        profile: str,
        baseline: PostStartPaneSample,
        samples: Iterable[PostStartPaneSample],
    ) -> PostStartAssessment:
        sample_tuple = tuple(samples)
        changed = tuple(
            sample.capture_index
            for sample in sample_tuple
            if sample.sha256 != baseline.sha256
        )
        for sample in sample_tuple:
            if sample.sha256 == baseline.sha256:
                continue
            pattern_id, cause = classify_claude_post_start_subscription_cap(
                baseline_text=baseline.text,
                sample_text=sample.text,
            )
            if pattern_id is not None:
                return PostStartAssessment(
                    schema="pantheon-harness-post-start-assessment/v1",
                    harness=harness,
                    profile=profile,
                    status="subscription-cap",
                    cause=cause,
                    pattern_id=pattern_id,
                    baseline_sha256=baseline.sha256,
                    capture_count=len(sample_tuple),
                    changed_capture_indices=changed,
                )
        ready = any(
            sample.sha256 != baseline.sha256
            and READY_PROMPT_LINE.search(_normalize_provider_pane(sample.text))
            for sample in sample_tuple
        )
        return PostStartAssessment(
            schema="pantheon-harness-post-start-assessment/v1",
            harness=harness,
            profile=profile,
            status="ready" if ready else "unknown",
            cause=None,
            pattern_id=None,
            baseline_sha256=baseline.sha256,
            capture_count=len(sample_tuple),
            changed_capture_indices=changed,
        )

    def prepare_interactive_provider_completion(
        self,
        *,
        harness: str,
        profile: str,
        persona: str,
        account_home: Path,
        state_dir: Path,
        pane_baseline: PostStartPaneSample,
        submitted_at: datetime,
        nonce_marker: str,
        nonce_sha256: str,
    ) -> ProviderCompletionProbe | None:
        del persona
        if harness != "claude":
            return None
        statusline_path = state_dir / "quota" / "claude-statusline.json"
        baseline_sha = _sha256_file(statusline_path)
        baseline_statusline = _read_statusline(statusline_path)
        baseline_transcript = _transcript_boundary(account_home, baseline_statusline)
        prompt = (
            "Provider completion probe. Reply with exactly this marker and no extra text: "
            f"{nonce_marker}"
        )
        return ProviderCompletionProbe(
            prompt=prompt,
            nonce_marker=nonce_marker,
            nonce_sha256=nonce_sha256,
            prompt_sha256=_sha256_text(prompt),
            provider_state={
                "account_home": account_home,
                "statusline_path": statusline_path,
                "baseline_statusline_sha256": baseline_sha,
                "baseline_transcript": baseline_transcript,
                "baseline_pane_text": pane_baseline.text,
                "baseline_pane_sha256": pane_baseline.sha256,
                "submitted_at": submitted_at,
                "harness": harness,
                "profile": profile,
            },
        )

    def assess_interactive_provider_completion(
        self,
        *,
        harness: str,
        profile: str,
        probe: ProviderCompletionProbe | None,
        samples: Iterable[PostStartPaneSample],
    ) -> ProviderCompletionAssessment:
        sample_tuple = tuple(samples)
        changed = tuple(
            sample.capture_index
            for sample in sample_tuple
            if probe is not None
            and sample.sha256 != probe.provider_state.get("baseline_pane_sha256")
        )
        if probe is None or harness != "claude":
            return ProviderCompletionAssessment(
                schema=_COMPLETION_SCHEMA,
                harness=harness,
                profile=profile,
                status="not-applicable",
                cause=None,
                pattern_id=None,
                nonce_sha256=None,
                prompt_sha256=None,
                statusline_baseline_sha256=None,
                statusline_observed_sha256=None,
                capture_count=len(sample_tuple),
                changed_capture_indices=(),
            )
        baseline_text = str(probe.provider_state.get("baseline_pane_text") or "")
        for sample in sample_tuple:
            pattern_id, cause = classify_claude_post_start_subscription_cap(
                baseline_text=baseline_text,
                sample_text=sample.text,
            )
            if pattern_id is not None:
                return ProviderCompletionAssessment(
                    schema=_COMPLETION_SCHEMA,
                    harness=harness,
                    profile=profile,
                    status="subscription-cap",
                    cause="provider-completion-subscription-cap",
                    pattern_id=pattern_id,
                    nonce_sha256=probe.nonce_sha256,
                    prompt_sha256=probe.prompt_sha256,
                    statusline_baseline_sha256=probe.provider_state.get("baseline_statusline_sha256"),
                    statusline_observed_sha256=_sha256_file(probe.provider_state["statusline_path"]),
                    capture_count=len(sample_tuple),
                    changed_capture_indices=changed,
                )
        statusline_path = probe.provider_state["statusline_path"]
        current_sha = _sha256_file(statusline_path)
        baseline_sha = probe.provider_state.get("baseline_statusline_sha256")
        statusline = _read_statusline(statusline_path)
        submitted_at = probe.provider_state["submitted_at"]
        if (
            current_sha is None
            or current_sha == baseline_sha
            or statusline is None
            or _parse_utc(statusline.get("observed_at")) is None
            or _parse_utc(statusline.get("observed_at")) < submitted_at
            or not statusline.get("session_id")
            or not statusline.get("transcript_path")
        ):
            return ProviderCompletionAssessment(
                schema=_COMPLETION_SCHEMA,
                harness=harness,
                profile=profile,
                status="unknown",
                cause="provider-completion-waiting",
                pattern_id=None,
                nonce_sha256=probe.nonce_sha256,
                prompt_sha256=probe.prompt_sha256,
                statusline_baseline_sha256=baseline_sha,
                statusline_observed_sha256=current_sha,
                capture_count=len(sample_tuple),
                changed_capture_indices=changed,
            )
        account_home = probe.provider_state["account_home"]
        transcript = _confined_transcript(account_home, statusline.get("transcript_path"))
        if transcript is None:
            cause = "provider-completion-transcript-invalid"
            start = None
            rel_hash = None
        else:
            baseline = probe.provider_state.get("baseline_transcript")
            rel_hash = _sha256_text(str(transcript.relative_to((account_home / ".claude" / "projects").resolve())))
            start = 0
            if isinstance(baseline, Mapping) and baseline.get("path") == transcript:
                try:
                    st = transcript.stat(follow_symlinks=False)
                except OSError:
                    st = None
                if (
                    st is None
                    or st.st_ino != baseline.get("inode")
                    or st.st_dev != baseline.get("device")
                    or st.st_size < int(baseline.get("offset", 0))
                ):
                    cause = "provider-completion-transcript-replaced"
                else:
                    start = int(baseline["offset"])
                    cause = None
            else:
                cause = None
        if transcript is None or cause is not None:
            return ProviderCompletionAssessment(
                schema=_COMPLETION_SCHEMA,
                harness=harness,
                profile=profile,
                status="operational-readiness",
                cause=cause,
                pattern_id=None,
                nonce_sha256=probe.nonce_sha256,
                prompt_sha256=probe.prompt_sha256,
                statusline_baseline_sha256=baseline_sha,
                statusline_observed_sha256=current_sha,
                capture_count=len(sample_tuple),
                changed_capture_indices=changed,
                transcript_relative_path_sha256=rel_hash,
                transcript_start_offset=start,
            )
        seen, end, assistant_count, malformed = _assistant_delta_has_nonce(
            transcript,
            start=start,
            marker=probe.nonce_marker,
        )
        if malformed:
            status = "operational-readiness"
            cause = "provider-completion-transcript-malformed"
        elif seen:
            status = "provider-completed"
            cause = None
        else:
            status = "unknown"
            cause = "provider-completion-waiting"
        return ProviderCompletionAssessment(
            schema=_COMPLETION_SCHEMA,
            harness=harness,
            profile=profile,
            status=status,
            cause=cause,
            pattern_id="claude-statusline-transcript-nonce" if seen else None,
            nonce_sha256=probe.nonce_sha256,
            prompt_sha256=probe.prompt_sha256,
            statusline_baseline_sha256=baseline_sha,
            statusline_observed_sha256=current_sha,
            capture_count=len(sample_tuple),
            changed_capture_indices=changed,
            transcript_relative_path_sha256=rel_hash,
            transcript_start_offset=start,
            transcript_end_offset=end,
            assistant_event_count=assistant_count,
        )

    # -------------------------------------------------------------------------
    # Partition 1 — Generalized surfaces
    # -------------------------------------------------------------------------

    def standing_prompt_filename(self) -> str:
        """Return "CLAUDE.md" — the claude-code standing-prompt filename."""
        return "CLAUDE.md"

    def mcp_server_config_path(self) -> Path:
        """Return ~/.claude.json — user/local MCP registrations live here."""
        return self._claude_home / ".claude.json"

    def hooks_config_path(self) -> Path:
        """Return ~/.claude/settings.json — hooks live in the same settings file."""
        return self._claude_home / ".claude" / "settings.json"

    def spawn_subagent(self, role: str, config_overrides: dict[str, Any]) -> dict[str, Any]:
        """Return a claude-code Agent-tool spawn descriptor for the given role.

        Shape (skeleton stub; not wired to real subprocess):
            {"harness": "claude", "role": role, "isolation": "none",
             "config_overrides": config_overrides}

        Runtime wiring uses the Agent tool with isolation and prompt parameters;
        see the team dispatch pattern in feedback_implementer_dispatch_isolation_worktree_pattern.
        """
        return {
            "harness": "claude",
            "role": role,
            "isolation": "none",
            "config_overrides": config_overrides,
        }

    def resume_session(self, session_id: str) -> dict[str, Any]:
        """Return a claude --resume <session_id> descriptor.

        Shape: {"harness": "claude", "cli_args": ["--resume", session_id]}

        The session_id maps to a JSONL session file under
        ~/.claude/projects/<cwd-hash>/<session_id>.jsonl.
        The ho_resumer.py tool drives this surface at runtime.
        """
        return {
            "harness": "claude",
            "cli_args": ["--resume", session_id],
            "session_id": session_id,
        }

    def exec_headless(self, prompt: str, **kwargs: Any) -> list[str]:
        """Return ["claude", "-p", prompt] — the claude headless execution args.

        Args:
            prompt: the prompt string to pass to ``claude -p``.
            **kwargs: reserved for future harness-specific flags (model, etc.).

        Returns:
            CLI args list suitable for subprocess.run().
        """
        return [str(self.executable), "-p", prompt]

    def auth_status(self) -> dict[str, Any]:
        """Return auth state dict for claude-code.

        Shape:
            {"harness": "claude",
             "key_source": "ANTHROPIC_API_KEY",
             "settings_path": "<claude_home>/.claude/settings.json"}
        """
        return {
            "harness": "claude",
            "key_source": "ANTHROPIC_API_KEY",
            "settings_path": str(self.mcp_server_config_path()),
            "observed_state": self.auth_descriptor().observed_state,
            "descriptor_only": True,
        }

    # -------------------------------------------------------------------------
    # Partition 2 — Bridge surfaces
    # -------------------------------------------------------------------------

    def inject_inbound(self, message: str, **kwargs: Any) -> None:
        """Compatibility shim over durable maildir delivery.

        BRIDGE SURFACE — mechanism: server pushes via the ``claude/channel`` MCP
        endpoint into the active session.  This capability is claude-code-specific
        and does NOT generalize to other harnesses (codex does not implement it;
        live-verified T-0714).

        ``deliver_inbound`` returns the receipt new callers should inspect.
        Queueing proves durability only; a separate Claude wake/channel bridge
        advances the receipt to submitted or acknowledged.
        """
        request = InboundRequest(
            message=message,
            correlation_id=str(kwargs.pop("correlation_id", "legacy-inbound")),
            source=str(kwargs.pop("source", "legacy")),
            reply_route={str(key): str(value) for key, value in kwargs.items()},
        )
        self.deliver_inbound(request)
        return None

    def approval_defaults(self) -> dict[str, Any]:
        """Return claude-code unattended-mode defaults.

        Pantheon keeps Claude Code in its explicit ``default`` permission mode
        and grants unattended actions through the account's scoped
        ``permissions.allow`` entries.  Bypass/dontAsk modes are security-posture
        changes and are not adapter defaults.

        Shape:
            {"permission_mode": "default",
             "flag": "--permission-mode default",
             "tool_approval": "scoped-permissions.allow"}
        """
        return {
            "permission_mode": "default",
            "flag": "--permission-mode default",
            "tool_approval": "scoped-permissions.allow",
        }

    # -------------------------------------------------------------------------
    # Partition 3 — NOT abstracted (documented per-harness attributes)
    # -------------------------------------------------------------------------

    @property
    def sandbox_config(self) -> dict[str, Any]:
        """Describe Claude Code's provider-specific sandbox and permission modes."""
        return {
            "harness": "claude",
            "mechanism": "native sandbox settings plus permission modes",
            "description": (
                "Claude Code has native sandbox configuration and permission modes. "
                "Pantheon's Unix-account boundary remains an additional org-tier layer."
            ),
        }

    @property
    def profiles_config(self) -> dict[str, Any]:
        """claude-code profiles are per-instance settings.json files (org-tier).

        No ``-p <profile>`` flag analog.  Each Pantheon agent (Orchestrator/Specialist/Recovery) has
        its own home-dir settings.json; profile separation is achieved via Unix-user
        separation.
        """
        return {
            "harness": "claude",
            "mechanism": "per-instance settings.json via Unix user separation",
            "description": (
                "No -p <profile> flag. Each agent has its own home-dir settings.json; "
                "profile separation is achieved via distinct Unix accounts."
            ),
        }

    @property
    def app_server_config(self) -> dict[str, Any]:
        """Describe Claude Code remote-control without equating it to Codex app-server."""
        return {
            "harness": "claude",
            "supported": True,
            "surfaces": ["remote-control"],
            "description": "Remote-control is native; its wire and policy are Claude-specific.",
        }

    @property
    def cloud_config(self) -> dict[str, Any]:
        """claude-code has no cloud-task ingest analog.

        Returns {"supported": False, "reason": "no claude-code analog"} so consumers
        can distinguish "no analog" from "configured-but-empty".
        """
        return {
            "harness": "claude",
            "supported": False,
            "reason": "no claude-code analog",
        }

    @property
    def doctor_config(self) -> dict[str, Any]:
        """Describe Claude's native diagnostics without claiming Codex equivalence."""
        return {
            "harness": "claude",
            "mechanism": "/doctor plus --safe-mode/config checks",
            "tools": ["/doctor", "--safe-mode"],
            "description": (
                "Native checks remain namespaced Claude diagnostics; Pantheon also "
                "uses JSONL/session and process checks."
            ),
        }


def _valid_acceptance_overlay_config(run_id: str | None, overlay: str | None) -> bool:
    if not valid_acceptance_run_id(run_id):
        return False
    if not isinstance(overlay, str) or not overlay.strip():
        return False
    return True


def _claude_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts) or None


def _claude_usage(value: Any, total_cost: Any) -> Usage | None:
    if not isinstance(value, dict) and not isinstance(total_cost, (int, float)):
        return None
    value = value if isinstance(value, dict) else {}
    return Usage(
        input_tokens=_optional_int(value.get("input_tokens")),
        cached_input_tokens=_optional_int(
            value.get("cache_read_input_tokens")
            or value.get("cached_input_tokens")
        ),
        output_tokens=_optional_int(value.get("output_tokens")),
        cost_usd=float(total_cost) if isinstance(total_cost, (int, float)) else None,
    )


def _claude_provider_billing(
    events: tuple[HarnessEvent, ...],
) -> ProviderBillingEvidence | None:
    api_key_source: str | None = None
    result_usage: Usage | None = None
    total_cost_usd: float | None = None
    source_events: list[str] = []

    for event in events:
        if (
            event.raw.get("type") == "system"
            and event.raw.get("subtype") == "init"
        ):
            api_key_source = _claude_init_api_key_source(event.raw)
            if api_key_source is not None:
                source_events.append("system/init")
            break

    for event in reversed(events):
        if event.raw.get("type") != "result":
            continue
        result_usage = event.usage
        total_cost_usd = _optional_float(event.raw.get("total_cost_usd"))
        source_events.append("result")
        break

    if api_key_source is None and result_usage is None and total_cost_usd is None:
        return None
    return ProviderBillingEvidence(
        provider="claude",
        api_key_source=api_key_source,
        total_cost_usd=total_cost_usd,
        input_tokens=result_usage.input_tokens if result_usage else None,
        cached_input_tokens=(
            result_usage.cached_input_tokens if result_usage else None
        ),
        output_tokens=result_usage.output_tokens if result_usage else None,
        source_events=tuple(source_events),
    )


def _claude_init_api_key_source(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("apiKeySource")
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _find_upward(cwd: Path, filename: str) -> Path | None:
    current = cwd.resolve()
    while True:
        candidate = current / filename
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent
