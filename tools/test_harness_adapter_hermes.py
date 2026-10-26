"""T-5005 (T-4924 H1): conformance tests for the inert hermes harness binding.

Event and pane fixtures are the H0 captures against Hermes v0.22.0
(e06df9c2), redacted (tools/fixtures/harness_adapter/hermes/PROVENANCE.json).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.harness_adapter import integration as harness_integration
from tools.harness_adapter.base import PantheonHarnessAdapter
from tools.harness_adapter.claude import ClaudeHarnessAdapter
from tools.harness_adapter.codex import CodexHarnessAdapter
from tools.harness_adapter.hermes import (
    DEEPSEEK_MODEL_ALIASES,
    DEEPSEEK_MODEL_IDS,
    PANE_BALANCE_BANNER,
    PANE_BUSY_HINT,
    PANE_BUSY_STATUS_GLYPH,
    PANE_IDLE_PROMPT,
    HermesHarnessAdapter,
    HermesIntegration,
    balance_thresholds_from_native,
    classify_hermes_insufficient_balance,
    hermes_failure_category,
)
from tools.harness_adapter.hermes_render import HermesRenderError
from tools.harness_adapter.provider_quota import DeepSeekBalanceObserver
from tools.harness_adapter.types import (
    ExecutionRequest,
    HarnessLaunchConfig,
    InboundRequest,
    PostStartPaneSample,
    SessionHandle,
    TmuxTarget,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "harness_adapter" / "hermes"
EXE = "/opt/pantheon/hermes-agent/pin/bin/hermes"
ENV_PREFIX = ("/usr/bin/env", "-u", "HERMES_MANAGED_DIR", "--")


def _launch(**native_overrides) -> HarnessLaunchConfig:
    native = {"provider": "deepseek", "busy_input_mode": "queue"}
    native.update(native_overrides)
    return HarnessLaunchConfig(
        harness="hermes",
        profile="deepseek-direct",
        executable=Path(EXE),
        model="deepseek-v4-pro",
        mode="interactive",
        meter="api",
        quota_source="provider-api",
        native=native,
        sources=("test",),
    )


def _adapter(tmp_path: Path, **native) -> HermesHarnessAdapter:
    return HermesHarnessAdapter(
        "system-orchestrator",
        account_home=tmp_path,
        mailbox_path=tmp_path / "inbox",
        launch=_launch(**native),
    )


def _run_fixture(adapter: HermesHarnessAdapter, name: str, *, returncode: int):
    events = [
        event
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if (event := adapter.parse_event(line)) is not None
    ]
    return events, adapter.finalize(events, returncode=returncode, stderr="")


# --- interface ---------------------------------------------------------------


def test_instantiates_with_every_abstract_method(tmp_path):
    adapter = _adapter(tmp_path)
    assert isinstance(adapter, PantheonHarnessAdapter)
    assert adapter.HARNESS_NAME == "hermes"
    assert not getattr(HermesHarnessAdapter, "__abstractmethods__", frozenset())


def test_standing_prompt_is_native_lowercase_name(tmp_path):
    adapter = _adapter(tmp_path)
    assert adapter.standing_prompt_filename() == ".hermes.md"
    upper = "HER" + "MES" + ".md"  # built at runtime: the committed bytes never hold it
    assert adapter.standing_prompt_filename() != upper


def test_build_exec_is_shell_free_with_prompt_on_stdin(tmp_path):
    adapter = _adapter(tmp_path)
    spec = adapter.build_exec(ExecutionRequest(prompt="do the thing", cwd=tmp_path / "seat"))
    assert spec.stdin == "do the thing"
    assert "do the thing" not in spec.argv
    assert spec.argv[:10] == (
        *ENV_PREFIX, EXE, "chat", "--query-file", "-", "--format", "stream-json",
    )
    assert ("--provider", "deepseek") == spec.argv[spec.argv.index("--provider"):spec.argv.index("--provider") + 2]
    assert ("-m", "deepseek-v4-pro") == spec.argv[spec.argv.index("-m"):spec.argv.index("-m") + 2]
    assert "--usage-file" not in spec.argv  # does not exist in v0.22.0 (H0; artisan ruling D-H1-7)
    assert all(isinstance(arg, str) for arg in spec.argv)


def test_exec_and_interactive_use_separate_hermes_homes(tmp_path):
    adapter = _adapter(tmp_path)
    exec_spec = adapter.build_exec(ExecutionRequest(prompt="x", cwd=tmp_path))
    live = adapter.build_interactive_launch(TmuxTarget("pantheon-q", "0", "0"), cwd=tmp_path / "seat")
    assert live.env["HERMES_HOME"] == str(tmp_path / ".hermes")
    assert exec_spec.env["HERMES_HOME"] != live.env["HERMES_HOME"]
    assert live.env["PANTHEON_TMUX_SESSION"] == "pantheon-q"
    assert live.stdin is None
    # Classic UI: `hermes --provider <p> -m <model>` behind the env -u prefix (H0 probe e).
    assert live.argv == (*ENV_PREFIX, EXE, "--provider", "deepseek", "-m", "deepseek-v4-pro")


def test_build_resume_names_session(tmp_path):
    adapter = _adapter(tmp_path)
    spec = adapter.build_resume(SessionHandle("abc", None, tmp_path, True), prompt="next")
    assert spec.argv[:8] == (*ENV_PREFIX, EXE, "chat", "--resume", "abc")
    assert spec.argv[8:10] == ("--query-file", "-") and spec.stdin == "next"
    assert "--query-file" not in adapter.build_resume(SessionHandle("abc", None, tmp_path, True)).argv


# --- launch config -----------------------------------------------------------


def test_valid_profile_passes(tmp_path):
    adapter = _adapter(tmp_path, balance={"currency": "USD", "low_water": "10", "hard_floor": "1"})
    (obs,) = adapter.validate_launch_config()
    assert obs.status == "pass", obs.summary


@pytest.mark.parametrize(
    "native,problem",
    [
        ({"sandbox": "x"}, "unknown-native-keys"),
        ({"provider": ""}, "provider-missing"),
        ({"provider": "auto"}, "provider-missing"),
        ({"balance": {"currency": "EUR", "low_water": "1", "hard_floor": "1"}}, "balance-invalid"),
        ({"balance": {"currency": "USD", "low_water": "1", "hard_floor": "5"}}, "balance-invalid"),
    ],
)
def test_invalid_profile_fails(tmp_path, native, problem):
    (obs,) = _adapter(tmp_path, **native).validate_launch_config()
    assert obs.status == "fail"
    assert problem in obs.evidence["problems"]


def test_balance_thresholds_parse_as_decimal():
    t = balance_thresholds_from_native({"currency": "CNY", "low_water": "50.5", "hard_floor": "7"})
    assert t.low_water == Decimal("50.5") and t.hard_floor == Decimal("7") and t.burn_alarm_24h is None


# --- events (H0-captured fixtures) -------------------------------------------


def test_provenance_sidecar_labels_every_fixture():
    sidecar = json.loads((FIXTURES / "PROVENANCE.json").read_text(encoding="utf-8"))
    provenance = sidecar["fixtures"]
    on_disk = {path.name for path in FIXTURES.iterdir() if path.name != "PROVENANCE.json"}
    assert on_disk == set(provenance)
    captured = "h0-captured (hermes v0.22.0 e06df9c2, local stub endpoint)"
    stub_body = "h0-captured, stub-derived error body"
    for name in ("success.jsonl", "tools.jsonl", "interrupted.jsonl"):
        assert provenance[name]["provenance"] == captured, name
    for name in ("balance_402.jsonl", "rate_limit_429.jsonl", "auth_401.jsonl"):
        assert provenance[name]["provenance"] == stub_body, name
    assert all(entry["provenance"].startswith("h0-captured") for entry in provenance.values())


def test_fixtures_are_redacted_for_the_public_mirror():
    upper = ("HER" + "MES" + ".md").encode("utf-8")
    for path in FIXTURES.iterdir():
        data = path.read_bytes()
        assert upper not in data, path.name
        assert b"swarm" not in data, path.name  # the probe host's name
        assert b"hermes-probe" not in data, path.name  # the probe account
    assert b'"1|host0"' in (FIXTURES / "tools.jsonl").read_bytes().replace(b'\\"', b'"')


def test_success_fixture_skips_the_non_json_stdout_line(tmp_path):
    lines = (FIXTURES / "success.jsonl").read_text(encoding="utf-8").splitlines()
    notice = [line for line in lines if not line.startswith("{")]
    assert notice == ["⚠️  Normalized model 'deepseek-chat' to 'deepseek-flash' for deepseek."]
    adapter = _adapter(tmp_path)
    assert adapter.parse_event(notice[0]) is None
    events, result = _run_fixture(adapter, "success.jsonl", returncode=0)
    assert [e.kind for e in events] == ["session_started", "progress", "turn_completed"]
    assert result.status == "completed" and result.error is None
    assert result.final_message == "stub reply ok"
    assert result.session is not None and result.session.session_id == "20261014_191854_6193e1"
    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.cached_input_tokens) == (10, 3, 0)
    assert result.provider_billing.provider == "hermes"
    assert result.provider_billing.input_tokens == 10
    assert result.provider_billing.source_events == ("upstream-provider:deepseek",)


def test_tools_fixture_shapes(tmp_path):
    events, result = _run_fixture(_adapter(tmp_path), "tools.jsonl", returncode=0)
    assert [e.kind for e in events] == [
        "session_started", "tool_started", "tool_completed", "progress", "turn_completed",
    ]
    tool_use, tool_result = events[1], events[2]
    assert tool_use.message == "read_file" and tool_use.raw["input"] == {"path": "/etc/hostname"}
    assert tool_result.message == "read_file"
    assert tool_result.raw["is_error"] is False and tool_result.raw["duration_ms"] == 144
    assert json.loads(tool_result.raw["output"])["content"] == "1|host0"
    assert result.status == "completed"
    assert result.final_message == "stub reply ok"  # the result event's text, not the raw text event


@pytest.mark.parametrize(
    "name,category,retryable,error",
    [
        ("balance_402.jsonl", "quota", False, "HTTP 402: Insufficient Balance"),
        ("rate_limit_429.jsonl", "rate_limit", True, "HTTP 429: Rate limit reached for requests"),
        ("auth_401.jsonl", "auth", False, "HTTP 401: Authentication Fails, Your api key is invalid"),
    ],
)
def test_failure_fixtures(tmp_path, name, category, retryable, error):
    events, result = _run_fixture(_adapter(tmp_path), name, returncode=1)
    assert events[-1].kind == "turn_failed" and events[-1].raw["error"] == error
    assert result.status == "failed"
    assert result.error.category == category
    assert result.error.retryable is retryable
    assert result.error.message == error


@pytest.mark.parametrize("returncode", [130, 124])
def test_interrupted_fixture(tmp_path, returncode):
    """The 503 probe: Hermes retried until an outer `timeout` SIGTERM, then
    reported exit_code 130 / "Interrupted"; the wrapper itself exited 124."""
    events, result = _run_fixture(_adapter(tmp_path), "interrupted.jsonl", returncode=returncode)
    assert events[-1].raw["exit_code"] == 130 and events[-1].raw["error"] == "Interrupted"
    assert result.status == "interrupted"
    assert result.error.category == "interrupted" and result.error.retryable is True


def test_interrupted_error_without_exit_130_is_interrupted(tmp_path):
    event = _adapter(tmp_path).parse_event(
        json.dumps({"type": "result", "session_id": "s", "exit_code": 1, "text": "", "error": "Interrupted"})
    )
    assert event.kind == "turn_failed" and event.raw["category"] == "interrupted"


def test_exit_130_without_events_is_interrupted(tmp_path):
    result = _adapter(tmp_path).finalize([], returncode=130, stderr="")
    assert result.status == "interrupted"


@pytest.mark.parametrize(
    "error,expected",
    [
        # result.error at the pin: "HTTP <code>: <msg>" (H0 probe d).
        ("HTTP 402: Insufficient Balance", ("quota", False)),
        ("HTTP 429: Rate limit reached for requests", ("rate_limit", True)),
        ("HTTP 401: Authentication Fails, Your api key is invalid", ("auth", False)),
        ("HTTP 403: Forbidden", ("auth", False)),
        # 5xx: no captured body (the 503 probe ended Interrupted); the form is the captured one.
        ("HTTP 500: Internal Server Error", ("provider", True)),
        ("HTTP 503: Service Unavailable", ("provider", True)),
    ],
)
def test_result_error_classification(tmp_path, error, expected):
    raw = {"type": "result", "session_id": "s", "exit_code": 1, "text": "", "error": error}
    assert hermes_failure_category(raw, error) == expected
    event = _adapter(tmp_path).parse_event(json.dumps(raw))
    assert event.kind == "turn_failed" and event.message == error
    assert (event.raw["category"], event.raw["retryable"]) == expected


@pytest.mark.parametrize(
    "raw,message,expected",
    [
        ({"error": {"status": 503}}, "busy", ("provider", True)),
        ({}, "HTTP 402 from upstream", ("quota", False)),
        ({}, "Insufficient Balance", ("quota", False)),
        ({"error": {"status_code": "403"}}, None, ("auth", False)),
        ({}, "something odd", ("provider", False)),
        # r1 LOW-4: only explicit status forms count; prose numbers do not.
        ({}, "processed 500 files before stopping", ("provider", False)),
        ({}, "wrote 429 lines, 401 skipped", ("provider", False)),
        ({}, "HTTP 503 Service Unavailable", ("provider", True)),
        ({}, "status: 429", ("rate_limit", True)),
        ({}, "Error code: 401 - invalid key", ("auth", False)),
        ({}, "status code 502", ("provider", True)),
    ],
)
def test_failure_category_mapping(raw, message, expected):
    assert hermes_failure_category(raw, message) == expected


def test_exit_code_zero_with_error_is_a_failure(tmp_path):
    event = _adapter(tmp_path).parse_event(
        json.dumps({"type": "result", "session_id": "s", "exit_code": 0, "text": "", "error": "HTTP 402: x"})
    )
    assert event.kind == "turn_failed" and event.raw["category"] == "quota"


def test_exec_headless_keeps_prompt_off_argv(tmp_path):
    adapter = _adapter(tmp_path)
    argv = adapter.exec_headless("secret incident text")
    assert "secret incident text" not in argv
    assert argv == list(adapter.build_exec(ExecutionRequest(prompt="x", cwd=tmp_path)).argv)
    assert argv[6:8] == ["--query-file", "-"]


@pytest.mark.parametrize(
    "line",
    [
        "{not json",
        "⚠️  Normalized model 'deepseek-chat' to 'deepseek-flash' for deepseek.",
        "[1, 2]",  # JSON, but not an object
        "   ",
    ],
)
def test_non_json_stdout_line_is_skipped(tmp_path, line):
    assert _adapter(tmp_path).parse_event(line) is None


# --- launch env, model ids, pane markers (H0) --------------------------------


def test_every_launch_drops_the_managed_dir_env(tmp_path):
    adapter = _adapter(tmp_path)
    specs = (
        adapter.build_exec(ExecutionRequest(prompt="x", cwd=tmp_path)),
        adapter.build_resume(SessionHandle("abc", None, tmp_path, True), prompt="y"),
        adapter.build_interactive_launch(TmuxTarget("pantheon-q", "0", "0"), cwd=tmp_path / "seat"),
    )
    for spec in specs:
        assert spec.argv[:5] == (*ENV_PREFIX, EXE)
        assert "HERMES_MANAGED_DIR" not in spec.env


def test_env_prefix_really_unsets_the_managed_dir(tmp_path):
    """Run the built prefix: an inherited HERMES_MANAGED_DIR never reaches the child."""
    launch = HarnessLaunchConfig(
        harness="hermes", profile="p", executable=Path("/usr/bin/printenv"), model=None,
        mode="interactive", meter="api", quota_source="provider-api", native={}, sources=("test",),
    )
    adapter = HermesHarnessAdapter("system-orchestrator", account_home=tmp_path, launch=launch)
    spec = adapter.build_interactive_launch(TmuxTarget("s", "0", "0"), cwd=tmp_path)
    env = {**os.environ, **spec.env, "HERMES_MANAGED_DIR": str(tmp_path / "evil")}
    unset = subprocess.run([*spec.argv, "HERMES_MANAGED_DIR"], env=env, capture_output=True, text=True)
    assert unset.returncode == 1 and unset.stdout == ""
    control = subprocess.run(["/usr/bin/printenv", "HERMES_MANAGED_DIR"], env=env, capture_output=True, text=True)
    assert control.stdout.strip() == str(tmp_path / "evil")


def test_model_ids_and_alias(tmp_path):
    assert DEEPSEEK_MODEL_IDS == ("deepseek-flash", "deepseek-v4-pro")
    assert DEEPSEEK_MODEL_ALIASES == {"deepseek-chat": "deepseek-flash"}
    assert not set(DEEPSEEK_MODEL_IDS) & set(DEEPSEEK_MODEL_ALIASES)
    alias = HermesHarnessAdapter(
        "system-orchestrator", account_home=tmp_path,
        launch=dataclasses.replace(_launch(), model="deepseek-chat"),
    )
    (obs,) = alias.validate_launch_config()
    assert obs.status == "fail" and "model-alias" in obs.evidence["problems"]


def test_pane_markers_match_the_captured_renders():
    idle = (FIXTURES / "pane_idle.txt").read_text(encoding="utf-8")
    busy = (FIXTURES / "pane_busy.txt").read_text(encoding="utf-8")
    assert PANE_BUSY_HINT in busy and PANE_BUSY_STATUS_GLYPH in busy
    assert PANE_BUSY_HINT not in idle and PANE_BUSY_STATUS_GLYPH not in idle
    assert any(line.startswith(PANE_IDLE_PROMPT) for line in idle.splitlines())
    assert not any(line.startswith(PANE_IDLE_PROMPT) for line in busy.splitlines()[-4:])


# --- delivery, surfaces, auth ------------------------------------------------


def test_deliver_inbound_without_inbox_is_rejected(tmp_path):
    adapter = HermesHarnessAdapter("system-orchestrator", account_home=tmp_path, launch=_launch())
    receipt = adapter.deliver_inbound(InboundRequest("hi", "c1", "test", {}))
    assert receipt.harness == "hermes" and receipt.state == "rejected"


def test_describe_surfaces_finds_standing_and_stops_at_git_root(tmp_path):
    adapter = _adapter(tmp_path)
    seat = tmp_path / "seat"
    seat.mkdir()
    (seat / ".hermes.md").write_text("rules\n", encoding="utf-8")
    surfaces = adapter.describe_surfaces(seat)
    assert surfaces.standing.discovered == (seat / ".hermes.md",)
    assert surfaces.standing.authoritative is True  # H0 probe a: the found file is what loads
    assert surfaces.memory.enabled is False
    assert surfaces.mcp.configured_servers == ()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    assert adapter.describe_surfaces(repo / "sub").standing.discovered == ()
    assert adapter.describe_surfaces(repo / "sub").standing.authoritative is False


def test_auth_is_descriptor_only_env_file(tmp_path):
    adapter = _adapter(tmp_path)
    assert adapter.auth_descriptor().mechanisms == ("api-key",)
    assert adapter.auth_descriptor().material_paths == (tmp_path / ".hermes" / ".env",)
    assert adapter.auth_status()["descriptor_only"] is True


def test_partition3_properties_are_bound(tmp_path):
    adapter = _adapter(tmp_path)
    assert adapter.app_server_config["gateway"] == "off"
    assert adapter.sandbox_config["terminal_backend"] == "local"
    assert adapter.approval_defaults()["approvals"]["mode"] == "off"


def test_doctor_version_command_uses_the_launch_prefix(tmp_path):
    commands = _adapter(tmp_path).doctor_config["commands"]
    assert commands[0] == (*ENV_PREFIX, EXE, "--version")


# --- quota observer ----------------------------------------------------------


def test_quota_observer_is_deepseek_balance_with_manifest_thresholds(tmp_path):
    adapter = _adapter(tmp_path, balance={"currency": "USD", "low_water": "10", "hard_floor": "1"})
    observer = adapter.quota_observer(tmp_path)
    assert isinstance(observer, DeepSeekBalanceObserver)
    assert observer.thresholds.low_water == Decimal("10")
    assert observer.profile == "hermes/deepseek-direct"


def test_quota_observer_without_key_reports_unavailable(tmp_path):
    adapter = _adapter(tmp_path, balance={"currency": "USD", "low_water": "10", "hard_floor": "1"})
    snapshot = adapter.quota_observer(tmp_path).read()  # no .env: no network attempted
    assert snapshot.freshness == "unavailable"
    assert snapshot.evidence["reason"] == "read-failed"
    assert snapshot.balance is None


# --- post-start readiness ----------------------------------------------------


def _sample(index: int, text: str) -> PostStartPaneSample:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return PostStartPaneSample(capture_index=index, sha256=digest, snippet_sha256=None, text=text)


def test_captured_out_of_credits_render_is_a_cap(tmp_path):
    adapter = _adapter(tmp_path)
    idle = (FIXTURES / "pane_idle.txt").read_text(encoding="utf-8")
    capped = (FIXTURES / "pane_balance_402.txt").read_text(encoding="utf-8")
    assert PANE_BALANCE_BANNER in capped and PANE_BALANCE_BANNER not in idle
    assert classify_hermes_insufficient_balance(idle) is None
    assert classify_hermes_insufficient_balance((FIXTURES / "pane_busy.txt").read_text(encoding="utf-8")) is None
    result = adapter.assess_post_start_readiness(
        harness="hermes", profile="deepseek-direct", baseline=_sample(0, idle),
        samples=[_sample(1, capped)],
    )
    assert result.status == "subscription-cap"
    assert result.pattern_id == "hermes-insufficient-balance"
    assert result.cap_kind == "prepaid-balance" and result.reset_at is None


def test_out_of_credits_banner_alone_is_a_cap():
    assert classify_hermes_insufficient_balance(" ─ ⚡ Out of credits ─\n\n❯ ") is not None


def test_new_insufficient_balance_render_is_a_cap(tmp_path):
    adapter = _adapter(tmp_path)
    baseline = _sample(0, "hermes ready\n> ")
    result = adapter.assess_post_start_readiness(
        harness="hermes", profile="deepseek-direct", baseline=baseline,
        samples=[_sample(1, "hermes ready\nError 402: Insufficient Balance\n> ")],
    )
    assert result.status == "subscription-cap"
    assert result.cap_kind == "prepaid-balance" and result.reset_at is None


def test_balance_render_already_in_baseline_is_not_a_cap(tmp_path):
    adapter = _adapter(tmp_path)
    text = "Error 402: Insufficient Balance\n> "
    result = adapter.assess_post_start_readiness(
        harness="hermes", profile="deepseek-direct", baseline=_sample(0, text),
        samples=[_sample(1, text + " ")],
    )
    assert result.status == "unknown"
    assert classify_hermes_insufficient_balance("all good") is None


# --- prepare_launch hook -----------------------------------------------------


def test_base_prepare_launch_is_a_noop_for_claude_and_codex(tmp_path):
    for adapter in (
        ClaudeHarnessAdapter("orchestrator", claude_home=tmp_path),
        CodexHarnessAdapter("orchestrator", codex_home=tmp_path),
    ):
        assert adapter.prepare_launch(SimpleNamespace(), cwd=tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_hermes_prepare_launch_fails_closed_without_render_inputs(tmp_path):
    with pytest.raises(HermesRenderError) as info:
        _adapter(tmp_path).prepare_launch(SimpleNamespace(), cwd=tmp_path / "seat")
    assert info.value.reason == "render-inputs-missing"


# --- integration (registered only inside the context manager) ----------------


def test_hermes_is_registered_and_the_receiver_handoff_still_resolves_to_claude():
    """T-5011 PR-B (replaces the H1 D-H1-1 not-registered guard): hermes is
    registered, and because it carries no Telegram receiver
    (communications_managed False) the T-4184 in-runtime receiver harness is
    still claude, so the codex->claude receiver handoff is not a no-op."""
    from tools import runtime_controller

    assert "hermes" in harness_integration.configured_harness_names()
    assert harness_integration.normalize_harness("hermes") == "hermes"
    assert runtime_controller._in_runtime_receiver_harness() == "claude"


def test_hermes_integration_under_registered_context(tmp_path):
    with harness_integration.registered_integration(HermesIntegration()):
        assert harness_integration.normalize_harness("hermes") == "hermes"
        integration = harness_integration.integration_for("Hermes")
        binding = SimpleNamespace(role="system-orchestrator", mailbox=tmp_path / "inbox", launch=_launch())
        adapter = integration.build_adapter(binding, harness_home=tmp_path)
        assert isinstance(adapter, HermesHarnessAdapter)
        contract = integration.successor_evidence_contract("p", "t1", tmp_path / "ev.json")
        assert contract.expected == "mailbox:runtime-transfer:t1:ev.json"
        assert not contract.expected.startswith("hook:")
        assert integration.receiver_direction(_launch(), _launch()) is None
        assert integration.prove_receiver_state(binding, "absent").state == "not-applicable"
        assert integration.run_receiver_handoff(binding, "d", "pre", reason="r").ok
        assert integration.communications_managed is False
    # T-5011 PR-B: registered at import, so the context manager restores it.
    assert "hermes" in harness_integration.configured_harness_names()
