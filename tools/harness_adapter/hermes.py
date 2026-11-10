"""Hermes Agent harness binding (T-5005, T-4924 H1; plan §3.1).

A third model family (DeepSeek, in the Hermes Agent harness) on a system
recovery seat, so that a single vendor's cap cannot take recovery dark.

Registered in H2 (T-5011 PR-B): ``integration.HermesIntegration`` is
registered next to claude and codex, together with the
``runtime_controller._in_runtime_receiver_harness`` fix (candidates are
filtered on ``communications_managed``, so hermes never makes the T-4184
codex->claude receiver handoff ambiguous). No manifest names hermes until H4,
so no seat runs it.

Bindings are H0-captured (T-5005) against Hermes v0.22.0, commit e06df9c2,
with all model traffic sent to a local stub endpoint; the event fixtures under
tools/fixtures/harness_adapter/hermes/ are those captures, redacted. A binding
still marked [H0] is one H0 did not settle.

Byte safety (pantheon-config CLAUDE.md, plan §1): the standing prompt is the
native lowercase ``.hermes.md`` only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Mapping

from tools.harness_adapter._common import (
    deliver_to_mailbox,
    finalize_events,
    parse_json_object,
)
from tools.harness_adapter.base import PantheonHarnessAdapter
from tools.harness_adapter.hermes_render import (
    HERMES_HOME_DIRNAME,
    IDENTITY_FILENAME,
    LAUNCH_ENV_UNSET,
    RUNTIME_CONFIG_FILENAME,
    SEAT_DIRNAME,
    STANDING_FILENAME,
    HermesPrepareResult,
    HermesRenderError,
    HermesRenderInputs,
    HermesRenderTarget,
    prepare_hermes_launch,
)
# HermesIntegration lives in integration.py (registered there, T-5011 PR-B);
# re-exported here for H1 importers.
from tools.harness_adapter.integration import HermesIntegration
from tools.harness_adapter.provider_quota import (
    DEEPSEEK_BALANCE_BASE_URL,
    DeepSeekBalanceObserver,
    read_deepseek_balance,
)
from tools.harness_adapter.quota import BalanceThresholds
from tools.harness_adapter.types import (
    AuthDescriptor,
    CommandSpec,
    DeliveryReceipt,
    DiagnosticObservation,
    ExecutionError,
    ExecutionRequest,
    ExecutionResult,
    HarnessCapabilities,
    HarnessEvent,
    HarnessLaunchConfig,
    HarnessSurfaceDescriptors,
    InboundRequest,
    McpDescriptor,
    MemoryDescriptor,
    PostStartAssessment,
    PostStartPaneSample,
    ProviderBillingEvidence,
    RoleDescriptor,
    SessionHandle,
    StandingInstructionsDescriptor,
    TmuxTarget,
    Usage,
)


#: Allowed ``native`` keys on a hermes runtime profile (plan §3.1, plus
#: ``base_url`` for the first-run-wizard-skipping model block, plan §3.1 Surface).
#: T-5011 PR-D: no ``toolsets``: the CLI tool set is locked in the shared
#: defaults (platform_toolsets.cli), so no profile can widen or narrow it.
HERMES_NATIVE_KEYS = frozenset(
    {"provider", "base_url", "approvals", "terminal_backend", "busy_input_mode", "balance"}
)
#: The classic UI (H0 probe e): it renders classifiable idle/busy states.
#: ``--tui`` was not probed.
INTERACTIVE_UI: Literal["classic", "tui"] = "classic"
#: The quiet flag on ``hermes chat`` (H0: exists; stream-json implies quiet).
_QUIET_FLAG = "-Q"
#: Hermes exits 130 on SIGINT/SIGTERM with ``result.error == "Interrupted"``
#: (H0 probe d); ``_common.finalize_events`` only treats a negative return code
#: as interrupted.
_SIGINT_EXIT = 130
_INTERRUPTED_ERROR = "Interrupted"
_ENV_FILENAME = ".env"
_EXEC_HOME_DIRNAME = ".hermes-exec"
#: Every launch runs through ``env -u`` so an INHERITED managed-scope repoint
#: variable never reaches Hermes (H0 Specialist must 3). It does not stop a seat
#: setting HERMES_MANAGED_DIR in its own ``$HERMES_HOME/.env``, which Hermes
#: loads into os.environ before resolving the managed scope; that path is
#: guarded by ``hermes_render.check_env_file_controls`` (the H2/H3 must).
_ENV_BINARY = "/usr/bin/env"

#: Real DeepSeek model ids at the pin. ``deepseek-chat`` is an alias Hermes
#: normalizes to ``deepseek-flash`` (with a non-JSON stdout warning), so it is
#: never a default and a profile naming an alias fails validation.
DEEPSEEK_MODEL_IDS = ("deepseek-flash", "deepseek-v4-pro")
DEEPSEEK_MODEL_ALIASES = {"deepseek-chat": "deepseek-flash"}

# Interactive pane markers (classic UI, H0 probe e; tui/classic2 captures).
# Constants only: the submit_verify hermes profile that uses them is H2.
# Submission note: a send-keys text followed by an immediate Enter is swallowed
# into the paste as a newline; text, then a ~1s pause, then Enter submits.
#: Busy: the prompt-line hint while a turn runs.
PANE_BUSY_HINT = "msg=interrupt · /queue · /bg · /steer"
#: Busy: the running-turn timer glyph in the status line (U+23F1). The idle
#: status line shows a different glyph, U+23F2.
PANE_BUSY_STATUS_GLYPH = "⏱"
#: Idle: the input line starts with this and no busy hint is shown.
PANE_IDLE_PROMPT = "❯ "
#: The interactive 402 banner ("⚡ Out of credits").
PANE_BALANCE_BANNER = "Out of credits"

_CAP_BOTTOM_LINES = 20
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# The interactive 402 render (H0, tui/classic2/06_402.txt): the "Out of credits"
# banner, plus DeepSeek's "Insufficient Balance" body in the same region.
_BALANCE_CAP_RE = re.compile(r"out\s+of\s+credits|insufficient\s+balance", re.IGNORECASE)
# result.error at the pin reads "HTTP <code>: <msg>" (H0 probe d). Explicit
# status forms only, so prose numbers ("500 files") never read as an HTTP
# status (r1 LOW-4).
_STATUS_RE = re.compile(
    r"\b(?:http(?:\s+status)?|status(?:\s+code)?|error\s+code)\s*[:=]?\s*([45]\d\d)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HermesBalanceCap:
    cap_kind: str
    render_span_sha256: str
    reset_at: None = None  # a prepaid balance has no reset
    reset_source: None = None


def _bottom_region(text: str) -> str:
    normalized = _ANSI_RE.sub("", text).replace("\r", "").rstrip()
    return "\n".join(normalized.splitlines()[-_CAP_BOTTOM_LINES:])


def classify_hermes_insufficient_balance(text: str) -> HermesBalanceCap | None:
    """Classify a pane sample as an exhausted-prepaid-balance render, or None."""
    if not text:
        return None
    region = _bottom_region(text)
    if _BALANCE_CAP_RE.search(region) is None:
        return None
    return HermesBalanceCap(
        cap_kind="prepaid-balance",
        render_span_sha256=hashlib.sha256(region.encode("utf-8")).hexdigest(),
    )


def classify_hermes_mid_session_cap(text: str, *, now: datetime | None = None) -> HermesBalanceCap | None:
    """The mid-session cap classifier (T-5011 PR-B, plan §3.2 gap 4) that the
    runtime controller reaches through ``HermesIntegration``: the exhausted
    prepaid-balance render. A balance has no reset (``reset_at`` None)."""
    del now
    return classify_hermes_insufficient_balance(text)


def _status_code(raw: Mapping[str, Any]) -> int | None:
    error = raw.get("error")
    candidates: list[Any] = []
    if isinstance(error, Mapping):
        candidates.extend(error.get(key) for key in ("status", "status_code", "http_status", "code"))
    candidates.extend(raw.get(key) for key in ("status", "status_code", "http_status"))
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 100 <= value <= 599:
            return value
        if isinstance(value, str) and value.isdigit() and 100 <= int(value) <= 599:
            return int(value)
    return None


def _error_message(raw: Mapping[str, Any]) -> str | None:
    error = raw.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
        return str(dict(error))
    if isinstance(error, str) and error:
        return error
    message = raw.get("message")
    return message if isinstance(message, str) and message else None


def hermes_failure_category(raw: Mapping[str, Any], message: str | None) -> tuple[str, bool]:
    """Plan §3.1 finalize row: 402->quota (not retryable), 429->rate_limit,
    401/403->auth, 5xx->provider. At the pin the status rides in the
    ``result.error`` string, "HTTP <code>: <msg>" (H0 probe d)."""
    status = _status_code(raw)
    lower = (message or "").lower()
    if status is None:
        match = _STATUS_RE.search(lower)
        status = int(match.group(1)) if match else None
    if status == 402 or "insufficient balance" in lower:
        return "quota", False
    if status == 429:
        return "rate_limit", True
    if status in {401, 403}:
        return "auth", False
    if status is not None and 500 <= status <= 599:
        return "provider", True
    return "provider", False


def _hermes_usage(value: Any) -> Usage | None:
    """The ``result`` event's ``tokens`` block (H0 probe c):
    ``{input, output, total, cache_read, cache_write}``. No reasoning count."""
    if not isinstance(value, Mapping):
        return None

    def number(key: str) -> int | None:
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool):
            return item
        return None

    usage = Usage(
        input_tokens=number("input"),
        cached_input_tokens=number("cache_read"),
        output_tokens=number("output"),
    )
    if usage == Usage():
        return None
    return usage


def balance_thresholds_from_native(raw: Any) -> BalanceThresholds:
    """Parse ``native.balance`` into thresholds; raises ValueError when malformed."""
    if not isinstance(raw, Mapping):
        raise ValueError("native.balance must be a mapping")
    unknown = sorted(set(raw) - {"currency", "low_water", "hard_floor", "burn_alarm_24h"})
    if unknown:
        raise ValueError(f"native.balance has unknown keys: {', '.join(unknown)}")
    currency = raw.get("currency")
    if currency not in {"CNY", "USD"}:
        raise ValueError("native.balance.currency must be CNY or USD")

    def amount(key: str, *, required: bool) -> Decimal | None:
        value = raw.get(key)
        if value is None and not required:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError(f"native.balance.{key} must be a decimal string")
        try:
            parsed = Decimal(str(value))
        except InvalidOperation:
            raise ValueError(f"native.balance.{key} must be a decimal string") from None
        if not parsed.is_finite() or parsed < 0:
            raise ValueError(f"native.balance.{key} must be finite and non-negative")
        return parsed

    low_water = amount("low_water", required=True)
    hard_floor = amount("hard_floor", required=True)
    assert low_water is not None and hard_floor is not None
    if hard_floor > low_water:
        raise ValueError("native.balance.hard_floor must not exceed low_water")
    return BalanceThresholds(
        currency=currency,
        low_water=low_water,
        hard_floor=hard_floor,
        burn_alarm_24h=amount("burn_alarm_24h", required=False),
    )


class HermesHarnessAdapter(PantheonHarnessAdapter):
    """Harness adapter for the Hermes Agent CLI (Nous Research).

    Args:
        role_domain: ORG-027 instance role-key for the target agent.
        account_home: the seat account's home (``~``). ``HERMES_HOME`` is
            ``<account_home>/.hermes``. Defaults to ``Path.home()``.
        mailbox_path: the seat's own inbox (receive-only, plan D-35).
        launch: the immutable runtime profile.
        exec_home: a separate ``HERMES_HOME`` for headless exec (plan D-33);
            defaults to ``<account_home>/.hermes-exec``.
    """

    HARNESS_NAME: str = "hermes"

    def __init__(
        self,
        role_domain: str,
        account_home: Path | None = None,
        mailbox_path: Path | None = None,
        launch: HarnessLaunchConfig | None = None,
        exec_home: Path | None = None,
    ) -> None:
        super().__init__(role_domain, launch=launch)
        self._account_home = account_home if account_home is not None else Path.home()
        self._mailbox_path = mailbox_path
        self._exec_home = exec_home if exec_home is not None else self._account_home / _EXEC_HOME_DIRNAME
        self._target = HermesRenderTarget()

    @property
    def hermes_home(self) -> Path:
        return self._account_home / HERMES_HOME_DIRNAME

    @property
    def seat_dir(self) -> Path:
        return self._account_home / SEAT_DIRNAME

    # -------------------------------------------------------------------------
    # Typed lifecycle contract
    # -------------------------------------------------------------------------

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            harness="hermes",
            session_resume_by_id=True,
            session_resume_last=True,
            structured_progress=True,
            structured_output_schema=False,  # v0.22.0 `chat --help` has no output-schema flag
            local_generated_memory=True,  # exists; disabled by the shared defaults
            native_stop_continuation=False,
            inbound_mechanisms=("maildir", "poll-and-inject"),
            hook_events={},  # no verified Hermes hook analog (plan §3.2 gap 7)
        )

    def _launch_prefix(self) -> list[str]:
        """``/usr/bin/env -u HERMES_MANAGED_DIR -- <hermes>``: a managed-scope
        repoint variable INHERITED from the caller's environment never reaches
        Hermes (``CommandSpec.env`` can only add variables). This does not cover
        a value the seat writes into ``$HERMES_HOME/.env`` (or ``.op.env``, or
        the install-root ``.env``): Hermes loads those into os.environ with
        override before it resolves the managed scope. That path is the
        ``hermes_render.check_env_file_controls`` drift check (H2/H3 must)."""
        prefix = [_ENV_BINARY]
        for name in LAUNCH_ENV_UNSET:
            prefix.extend(["-u", name])
        prefix.extend(["--", str(self.executable)])
        return prefix

    def _provider_model_args(self, model: str | None) -> list[str]:
        # `--provider` and `-m/--model` exist at the pin (H0 probe c); the
        # provider id comes from the profile.
        args: list[str] = []
        provider = self.launch.native.get("provider") if self.launch else None
        if isinstance(provider, str) and provider:
            args.extend(["--provider", provider])
        if model:
            args.extend(["-m", model])
        return args

    def build_exec(self, request: ExecutionRequest) -> CommandSpec:
        # `--usage-file` does not exist in v0.22.0 (H0 probe c; artisan ruling
        # D-H1-7 dropped it): usage comes from the `result` event's `tokens`.
        model = request.model or (self.launch.model if self.launch else None)
        argv = [
            *self._launch_prefix(),
            "chat",
            "--query-file",
            "-",
            "--format",
            "stream-json",
            _QUIET_FLAG,
            *self._provider_model_args(model),
        ]
        return CommandSpec(
            argv=tuple(argv),
            stdin=request.prompt,
            cwd=request.cwd,
            env={"HERMES_HOME": str(self._exec_home)},
        )

    def build_resume(
        self,
        session: SessionHandle,
        prompt: str | None = None,
    ) -> CommandSpec:
        cwd = session.cwd if session.cwd is not None else self.seat_dir
        model = self.launch.model if self.launch else None
        argv = [
            *self._launch_prefix(),
            "chat",
            "--resume",
            session.session_id,
            *(["--query-file", "-"] if prompt is not None else []),
            "--format",
            "stream-json",
            _QUIET_FLAG,
            *self._provider_model_args(model),
        ]
        return CommandSpec(
            argv=tuple(argv),
            stdin=prompt,
            cwd=cwd,
            env={"HERMES_HOME": str(self._exec_home)},
        )

    def parse_event(self, line: str) -> HarnessEvent | None:
        """One stream-json stdout line (H0 probe c shapes):

        * ``system`` (subtype ``init``): model, session_id
        * ``tool_use``: name, input
        * ``tool_result``: name, output (a JSON string), duration_ms, is_error
        * ``text``: text
        * ``result``: session_id, exit_code, text, tokens{...}, duration_ms[, error]

        A non-JSON stdout line is skipped (returns None): Hermes prints plain
        notices on stdout among the events, e.g. the model-alias warning
        "Normalized model 'deepseek-chat' to 'deepseek-flash' for deepseek."
        """
        raw, parse_error = parse_json_object(line)
        if parse_error or raw is None:
            return None
        native_type = raw.get("type")
        raw_session = raw.get("session_id")
        session_id = raw_session if isinstance(raw_session, str) and raw_session else None
        message: str | None = None
        usage: Usage | None = None
        kind = "progress"
        event_raw: Mapping[str, Any] = raw

        if native_type == "system":
            kind = "session_started"
        elif native_type == "text":
            text = raw.get("text")
            message = text if isinstance(text, str) else None
        elif native_type == "tool_use":
            kind = "tool_started"
            name = raw.get("name") or raw.get("id")
            message = str(name) if name is not None else None
        elif native_type == "tool_result":
            # No call id at the pin: the result names the tool.
            kind = "tool_completed"
            name = raw.get("name")
            message = str(name) if name is not None else None
        elif native_type == "result":
            usage = _hermes_usage(raw.get("tokens"))
            exit_code = raw.get("exit_code")
            text = raw.get("text")
            if exit_code == 0 and not isinstance(exit_code, bool) and not raw.get("error"):
                kind = "turn_completed"
                message = text if isinstance(text, str) and text else None
            else:
                kind = "turn_failed"
                message = _error_message(raw) or f"hermes turn failed (exit_code={exit_code!r})"
                category, retryable = hermes_failure_category(raw, message)
                if exit_code == _SIGINT_EXIT or raw.get("error") == _INTERRUPTED_ERROR:
                    category, retryable = "interrupted", True
                event_raw = {**raw, "category": category, "retryable": retryable}
        elif native_type == "error":
            # Fail-closed defence: Hermes at the pin (e06df9c2) never emits an
            # "error" event; hermes_cli/stream_json.py emits only system, text,
            # tool_use, tool_result and result (errors ride in result.error).
            # Kept so a future emitter is classified, not dropped (T-5011 PR-D).
            kind = "error"
            message = _error_message(raw) or "hermes error event"
            category, retryable = hermes_failure_category(raw, message)
            event_raw = {**raw, "category": category, "retryable": retryable}

        return HarnessEvent(
            harness="hermes",
            kind=kind,  # type: ignore[arg-type]
            session_id=session_id,
            message=message,
            usage=usage,
            raw=event_raw,
        )

    def finalize(
        self,
        events: Iterable[HarnessEvent],
        *,
        returncode: int,
        stderr: str,
    ) -> ExecutionResult:
        captured = tuple(events)
        result = finalize_events(
            "hermes",
            captured,
            returncode=returncode,
            stderr=stderr,
            provider_billing=self._billing(captured),
        )
        interrupted_event = any(
            event.kind == "turn_failed" and event.raw.get("category") == "interrupted" for event in captured
        )
        if interrupted_event and returncode != _SIGINT_EXIT:
            # e.g. an outer `timeout` SIGTERM: Hermes reports exit_code 130,
            # "Interrupted", while the wrapper exits 124 (H0 probe d, 503 case).
            result = dataclasses.replace(result, status="interrupted")
        elif returncode == _SIGINT_EXIT:
            result = dataclasses.replace(
                result,
                status="interrupted",
                error=ExecutionError(
                    category="interrupted",
                    message=f"hermes interrupted (exit {_SIGINT_EXIT})",
                    retryable=True,
                ),
            )
        elif result.final_message is None and result.status == "completed":
            last_text = next(
                (
                    event.message
                    for event in reversed(captured)
                    if event.raw.get("type") == "text" and event.message
                ),
                None,
            )
            if last_text is not None:
                result = dataclasses.replace(result, final_message=last_text)
        return result

    def _billing(self, events: tuple[HarnessEvent, ...]) -> ProviderBillingEvidence:
        usage = next((event.usage for event in reversed(events) if event.usage is not None), None)
        provider = self.launch.native.get("provider") if self.launch else None
        return ProviderBillingEvidence(
            provider="hermes",
            api_key_source="env-file",
            total_cost_usd=None,
            input_tokens=usage.input_tokens if usage else None,
            cached_input_tokens=usage.cached_input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            source_events=(f"upstream-provider:{provider}",) if isinstance(provider, str) and provider else (),
        )

    def deliver_inbound(self, request: InboundRequest) -> DeliveryReceipt:
        return deliver_to_mailbox(
            "hermes",
            request,
            default_inbox=self._mailbox_path,
            default_sender="harness-adapter",
        )

    def spawn_error_event(self, exc: OSError) -> HarnessEvent:
        return HarnessEvent(
            harness="hermes",
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
                    check_id="hermes.runtime_profile",
                    status="warn",
                    summary="legacy adapter has no immutable runtime profile",
                    authoritative=True,
                    evidence={"profile": "", "legacy": "true"},
                ),
            )
        native = self.launch.native
        problems: list[str] = []
        unknown = sorted(set(native) - HERMES_NATIVE_KEYS)
        if unknown:
            problems.append("unknown-native-keys")
        provider = native.get("provider")
        if not isinstance(provider, str) or not provider or provider == "auto":
            problems.append("provider-missing")
        if not self.launch.model:
            problems.append("model-missing")
        elif self.launch.model in DEEPSEEK_MODEL_ALIASES:
            problems.append("model-alias")  # Hermes rewrites it; name the real id
        if "balance" in native:
            try:
                balance_thresholds_from_native(native["balance"])
            except ValueError:
                problems.append("balance-invalid")
        status = "fail" if problems else "pass"
        return (
            DiagnosticObservation(
                check_id="hermes.runtime_profile",
                status=status,
                summary=(
                    "hermes runtime profile is valid"
                    if status == "pass"
                    else "hermes runtime profile is invalid: " + ",".join(problems)
                ),
                authoritative=True,
                evidence={
                    "harness": self.launch.harness,
                    "profile": self.launch.profile,
                    "mode": self.launch.mode,
                    "meter": self.launch.meter,
                    "quota_source": self.launch.quota_source,
                    "unknown_native_keys": ",".join(unknown),
                    "problems": ",".join(problems),
                    "native_namespace": "hermes",
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
                load_order=("cwd-upward-to-git-root", "hermes-home-soul"),
                # H0 probe a: a found .hermes.md is the ONE context kind Hermes
                # loads (lowercase first; nonce test). With none found, another
                # kind or a file higher up would load, so nothing is claimed.
                authoritative=discovered is not None,
                evidence={
                    "harness": "hermes",
                    "cwd": str(cwd),
                    "identity": str(self.hermes_home / IDENTITY_FILENAME),
                },
            ),
            auth=self.auth_descriptor(),
            memory=MemoryDescriptor(
                available=True,
                enabled=False,
                readable=None,
                writable=None,
                evidence={"harness": "hermes", "disabled_by": "shared-defaults", "claim": "descriptor-only"},
            ),
            mcp=McpDescriptor(
                config_paths=(self.mcp_server_config_path(),),
                configured_servers=(),
                scope=("seat",),
                authoritative=False,
                evidence={"harness": "hermes", "native_format": "yaml", "mcp_servers": "none"},
            ),
            roles=RoleDescriptor(
                config_paths=(self.hermes_home / RUNTIME_CONFIG_FILENAME,),
                configured_roles=(),
                delegation_modes=(),
                authoritative=False,
                evidence={"harness": "hermes", "dispatch": "none"},
            ),
        )

    def auth_descriptor(self) -> AuthDescriptor:
        return AuthDescriptor(
            mechanisms=("api-key",),
            material_paths=(self.hermes_home / _ENV_FILENAME,),
            observed_state="unverified",
            authoritative=False,
            evidence={"harness": "hermes", "descriptor_only": "true", "live_probe": "not-run"},
        )

    def build_interactive_launch(self, target: TmuxTarget, *, cwd: Path) -> CommandSpec:
        # `hermes --provider <p> -m <model>`: the classic UI (H0 probe e).
        model = self.launch.model if self.launch else None
        argv = self._launch_prefix()
        if INTERACTIVE_UI == "tui":
            argv.append("--tui")
        argv.extend(self._provider_model_args(model))
        return CommandSpec(
            argv=tuple(argv),
            stdin=None,
            cwd=cwd,
            env={
                "HERMES_HOME": str(self.hermes_home),
                "PANTHEON_TMUX_SESSION": target.session,
                "PANTHEON_TMUX_WINDOW": target.window,
                "PANTHEON_TMUX_PANE": target.pane,
            },
        )

    def prepare_launch(self, binding: Any, *, cwd: Path) -> HermesPrepareResult:  # type: ignore[override]
        """Plan §2.2 step 2. Fails closed without ``binding.hermes_render``
        (the H1 stand-in for the H2/H4 manifest wiring)."""
        inputs = getattr(binding, "hermes_render", None)
        if not isinstance(inputs, HermesRenderInputs):
            raise HermesRenderError("render-inputs-missing", "binding carries no hermes_render inputs")
        if self.launch is None:
            raise HermesRenderError("no-launch-profile", "a runtime profile is required")
        if inputs.account_home != self._account_home:
            raise HermesRenderError("account-home-mismatch", str(inputs.account_home))
        if Path(cwd) != self.seat_dir:
            raise HermesRenderError("cwd-not-seat", str(cwd))
        return prepare_hermes_launch(inputs, self.launch, target=self._target)

    def assess_post_start_readiness(
        self,
        *,
        harness: str,
        profile: str,
        baseline: PostStartPaneSample,
        samples: Iterable[PostStartPaneSample],
        now: datetime | None = None,
    ) -> PostStartAssessment:
        """An exhausted-balance render that appeared after launch is a cap
        (baseline-diffed, the T-4862 codex shape); otherwise generic."""
        sample_tuple = tuple(samples)
        baseline_hit = classify_hermes_insufficient_balance(baseline.text)
        for sample in sample_tuple:
            if sample.sha256 == baseline.sha256:
                continue
            hit = classify_hermes_insufficient_balance(sample.text)
            if hit is None or (
                baseline_hit is not None and hit.render_span_sha256 == baseline_hit.render_span_sha256
            ):
                continue
            return PostStartAssessment(
                schema="pantheon-harness-post-start-assessment/v1",
                harness=harness,
                profile=profile,
                status="subscription-cap",
                cause="hermes-insufficient-balance",
                pattern_id="hermes-insufficient-balance",
                baseline_sha256=baseline.sha256,
                capture_count=len(sample_tuple),
                changed_capture_indices=tuple(
                    item.capture_index for item in sample_tuple if item.sha256 != baseline.sha256
                ),
                cap_kind=hit.cap_kind,
                reset_at=None,  # a prepaid balance has no reset
            )
        return super().assess_post_start_readiness(
            harness=harness, profile=profile, baseline=baseline, samples=sample_tuple, now=now
        )

    def classify_mid_session_cap(self, text: str, *, now: datetime | None = None) -> HermesBalanceCap | None:
        """Plan §3.2 gap 4 (the controller reaches the same function through
        ``HermesIntegration.classify_mid_session_cap``)."""
        return classify_hermes_mid_session_cap(text, now=now)

    def quota_observer(self, state_dir: Path) -> DeepSeekBalanceObserver:
        del state_dir
        profile = self.launch.profile if self.launch else "legacy"
        thresholds: BalanceThresholds | None = None
        if self.launch is not None and "balance" in self.launch.native:
            try:
                thresholds = balance_thresholds_from_native(self.launch.native["balance"])
            except ValueError:
                thresholds = None  # the observer then reports balance_band=unknown
        env_file = self.hermes_home / _ENV_FILENAME
        base_url = self.launch.native.get("base_url") if self.launch is not None else None
        key_holding_proxy = (
            isinstance(base_url, str)
            and base_url.startswith("https://deepseek-proxy.invalid:")
            and base_url.endswith("/v1")
        )
        balance_base_url = base_url if key_holding_proxy else DEEPSEEK_BALANCE_BASE_URL
        return DeepSeekBalanceObserver(
            lambda: read_deepseek_balance(
                env_file,
                base_url=balance_base_url,
                key_holding_proxy=key_holding_proxy,
            ),
            profile=f"hermes/{profile}",
            thresholds=thresholds,
        )

    # -------------------------------------------------------------------------
    # Partition 1 — Generalized surfaces
    # -------------------------------------------------------------------------

    def standing_prompt_filename(self) -> str:
        """Return the native lowercase ``.hermes.md``: top-precedence in Hermes's
        project-context lookup and trigger-free (plan §1, D-2). H0 probe a
        verified it: the nonce loaded, it won over the other context names, and
        it was found by walk-up from a subdirectory."""
        return STANDING_FILENAME

    def mcp_server_config_path(self) -> Path:
        return self.hermes_home / RUNTIME_CONFIG_FILENAME  # `mcp_servers: {}`

    def hooks_config_path(self) -> Path:
        # v0.22.0 `chat --help`: shell hooks are "declared in config.yaml".
        return self.hermes_home / RUNTIME_CONFIG_FILENAME

    def spawn_subagent(self, role: str, config_overrides: dict[str, Any]) -> dict[str, Any]:
        return {
            "harness": "hermes",
            "role": role,
            "enabled": False,
            "reason": "the recovery seat dispatches nothing (plan §5, P5)",
            "config_overrides": dict(config_overrides),
        }

    def resume_session(self, session_id: str) -> dict[str, Any]:
        return {"harness": "hermes", "cli_args": ["chat", "--resume", session_id]}

    def exec_headless(self, prompt: str, **kwargs: Any) -> list[str]:
        """The legacy argv form of ``build_exec``. The prompt is NEVER in argv:
        the caller supplies it on stdin (``--query-file -``), as ``build_exec``'s
        CommandSpec does."""
        del kwargs
        return list(self.build_exec(ExecutionRequest(prompt=prompt, cwd=self.seat_dir)).argv)

    def auth_status(self) -> dict[str, Any]:
        return {
            "harness": "hermes",
            "key_source": "env-file",
            "auth_file": str(self.hermes_home / _ENV_FILENAME),
            "observed_state": self.auth_descriptor().observed_state,
            "descriptor_only": True,
        }

    # -------------------------------------------------------------------------
    # Partition 2 — Bridge surfaces
    # -------------------------------------------------------------------------

    def inject_inbound(self, message: str, **kwargs: Any) -> None:
        """Durable maildir delivery (the codex shape). The tmux send-keys wake
        through a ``submit_verify`` hermes profile is H2."""
        request = InboundRequest(
            message=message,
            correlation_id=str(kwargs.pop("correlation_id", "legacy-inbound")),
            source=str(kwargs.pop("source", "legacy")),
            reply_route={str(key): str(value) for key, value in kwargs.items()},
        )
        self.deliver_inbound(request)
        return None

    def approval_defaults(self) -> dict[str, Any]:
        """``approvals.mode: off`` plus deny globs; Hermes's hardline blocklist
        still applies. ``smart`` is rejected because it can escalate to a manual
        prompt, which wedges an unattended seat (plan §3.1, D-17)."""
        return {
            "approvals": {"mode": "off", "deny": "see harness/hermes/config.defaults.yaml"},
            "busy_input_mode": "queue",
            "boundary": "unix-account",
        }

    # -------------------------------------------------------------------------
    # Partition 3 — NOT abstracted
    # -------------------------------------------------------------------------

    @property
    def sandbox_config(self) -> dict[str, Any]:
        return {"terminal_backend": "local", "boundary": "unix-account + egress policy (plan §6)"}

    @property
    def profiles_config(self) -> dict[str, Any]:
        return {"pantheon_profiles": "one per provider (plan D-5)", "hermes_profiles": "unused"}

    @property
    def app_server_config(self) -> dict[str, Any]:
        # There is no gateway on/off config key at the pin: "off" is operational
        # (the launcher never runs `hermes gateway run|install` or the web
        # dashboard; H0 Specialist must 2).
        return {
            "gateway": "off",
            "gateway_off_by": "launcher-never-starts-it",
            "cron": "off (ticks only inside the gateway or the desktop dashboard)",
            "global_stop": "hermes pause",
        }

    @property
    def cloud_config(self) -> dict[str, Any]:
        return {"applicable": False}

    @property
    def doctor_config(self) -> dict[str, Any]:
        return {
            "commands": (
                (*self._launch_prefix(), "--version"),
                ("python3", "tools/persona_orientation/render.py", "--check", "--harness", "hermes"),
            ),
            "note": (
                "render --check --harness hermes checks the SOP skills (T-5011 PR-C registry; "
                "pass --hermes-skills-root for the pinned dir); "
                "--version prints 'vunknown' from a tarball install, so the pin is the "
                "commit + tarball sha256 in harness/hermes/config.defaults.yaml"
            ),
        }


def _find_upward(cwd: Path, filename: str) -> Path | None:
    current = cwd.resolve()
    while True:
        candidate = current / filename
        if candidate.is_file():
            return candidate
        if (current / ".git").exists() or current.parent == current:
            return None
        current = current.parent


__all__ = [
    "DEEPSEEK_MODEL_ALIASES",
    "DEEPSEEK_MODEL_IDS",
    "HERMES_NATIVE_KEYS",
    "PANE_BALANCE_BANNER",
    "PANE_BUSY_HINT",
    "PANE_BUSY_STATUS_GLYPH",
    "PANE_IDLE_PROMPT",
    "HermesBalanceCap",
    "HermesHarnessAdapter",
    "HermesIntegration",
    "balance_thresholds_from_native",
    "classify_hermes_insufficient_balance",
    "classify_hermes_mid_session_cap",
    "hermes_failure_category",
]
