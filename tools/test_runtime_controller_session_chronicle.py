"""T-5016: runtime_controller's two session-end Chronicle hooks.

H1 runs in _start_runtime before any pane (re)start; H2 runs in controller_main
after the supervised runtime ends. Both are fail-open and bounded, and both are
no-ops under the suite default (tools/conftest.py turns the env gate off and
stubs the runner seam). These tests turn them back on with tmp_path stand-ins
and a fake runner; no real tmux, state dir or seat is touched."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from tools import runtime_controller as rc
from tools.test_runtime_controller import (
    _Adapter,
    _binding,
    _stateless_binding,
    codex_adapter_module,
)


@pytest.fixture
def hooks_on(monkeypatch):
    monkeypatch.setenv(rc.SESSION_CHRONICLE_HOOKS_ENV, "on")
    calls: list[tuple[list[str], float]] = []
    replies: list[dict] = []

    def runner(argv, *, timeout):
        calls.append((list(argv), timeout))
        return replies.pop(0) if replies else {"status": "ok", "results": [], "closed_pipe": False}

    monkeypatch.setattr(rc, "_SESSION_CHRONICLE_RUNNER", runner)
    return calls, replies


def _evidence(root: Path, persona: str = "recovery") -> list[dict]:
    path = rc._persona_state(persona, root) / "controller-evidence.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ------------------------------------------------------------------ the hook itself
def test_suite_default_is_off_and_stubbed():
    binding = _binding(Path("/nonexistent-t5016"))
    assert rc._session_chronicle_end(binding, cause="death") == {"status": "disabled"}
    assert rc._SESSION_CHRONICLE_RUNNER(["x"], timeout=1) == {"status": "stubbed"}


def test_hook_runs_the_duty_bounded_with_the_binding_identity(tmp_path, hooks_on):
    calls, _ = hooks_on
    binding = _binding(tmp_path, harness="claude")
    result = rc._session_chronicle_end(binding, cause="death", state_root=tmp_path)
    assert result["status"] == "ok"
    (argv, timeout), = calls
    assert timeout == rc.SESSION_CHRONICLE_TIMEOUT_SECONDS
    assert argv[1:3] == [str(rc.SESSION_CHRONICLE_TOOL), "session-end"]
    joined = " ".join(argv)
    for expected in ("--persona recovery", "--cause death", f"--account-home {tmp_path}", "--harness claude",
                     "--tmux-target pantheon-recovery:harness.0", f"--persona-state-dir {tmp_path}", "--stage"):
        assert expected in joined
    assert "--tmux-socket" not in argv
    (event,) = _evidence(tmp_path)
    assert event["event"] == "chronicle_session_end" and event["cause"] == "death"


def test_hook_passes_a_private_socket(tmp_path, hooks_on):
    calls, _ = hooks_on
    context = rc.TmuxRuntimeContext(socket="t5016", scope_unit="x.scope")
    rc._session_chronicle_end(_binding(tmp_path), cause="start", context=context, state_root=tmp_path)
    assert calls[0][0][-2:] == ["--tmux-socket", "t5016"]


@pytest.mark.parametrize("status", ["timeout", "runner-error"])
def test_timeout_or_runner_error_alerts_recovery_via_the_cooldown_gate(tmp_path, hooks_on, status):
    _, replies = hooks_on
    replies.append({"status": status})
    with mock.patch.object(rc, "_route_failure_alert") as route:
        first = rc._session_chronicle_end(_binding(tmp_path), cause="death", state_root=tmp_path)
        replies.append({"status": status})
        second = rc._session_chronicle_end(_binding(tmp_path), cause="death", state_root=tmp_path)
    assert first["alert"]["alert_routed"] is True
    assert second["alert"]["alert_suppressed"] == "cooldown"
    route.assert_called_once()
    persona, _msg, vocab = route.call_args.args
    assert (persona, vocab) == ("recovery", "chronicle-archive-failed")


def test_hook_never_raises(tmp_path, hooks_on, monkeypatch):
    monkeypatch.setattr(rc, "_SESSION_CHRONICLE_RUNNER", mock.Mock(side_effect=OSError("no python")))
    assert rc._session_chronicle_end(_binding(tmp_path), cause="death", state_root=tmp_path) == {
        "status": "hook-error"}
    monkeypatch.setattr(rc, "_SESSION_CHRONICLE_RUNNER", lambda argv, timeout: "not a dict")
    with mock.patch.object(rc, "_route_failure_alert"):
        assert rc._session_chronicle_end(_binding(tmp_path), cause="death", state_root=tmp_path)["status"] == "runner-error"


def test_real_runner_bounds_and_parses(tmp_path):
    fast = rc._session_chronicle_runner(["python3", "-c", "print('noise'); print('{\"status\": \"ok\"}')"], timeout=30)
    assert fast == {"status": "ok"}
    slow = rc._session_chronicle_runner(["python3", "-c", "import time; time.sleep(30)"], timeout=0.5)
    assert slow["status"] == "timeout"
    garbage = rc._session_chronicle_runner(["python3", "-c", "print('nope')"], timeout=30)
    assert garbage["status"] == "runner-error"


def test_chronicle_group_command_is_direct_when_live_process_has_group(monkeypatch):
    monkeypatch.setattr(rc.os, "geteuid", lambda: 1003)
    monkeypatch.setattr(rc.os, "getegid", lambda: 1004)
    monkeypatch.setattr(rc.os, "getgroups", lambda: [1003])
    monkeypatch.setattr(rc.grp, "getgrnam", lambda _name: mock.Mock(gr_gid=1003))
    monkeypatch.setattr(rc.pwd, "getpwuid", lambda _uid: mock.Mock(pw_name="seat", pw_gid=1004))
    monkeypatch.setattr(rc.os, "getgrouplist", lambda _name, _gid: [1003, 1004])
    assert rc._session_chronicle_group_command(["python3", "tool.py"]) == ["python3", "tool.py"]


def test_chronicle_group_command_wraps_configured_member_with_stale_live_groups(monkeypatch):
    monkeypatch.setattr(rc.os, "geteuid", lambda: 1003)
    monkeypatch.setattr(rc.os, "getegid", lambda: 1004)
    monkeypatch.setattr(rc.os, "getgroups", lambda: [1004])
    monkeypatch.setattr(rc.grp, "getgrnam", lambda _name: mock.Mock(gr_gid=1003))
    monkeypatch.setattr(rc.pwd, "getpwuid", lambda _uid: mock.Mock(pw_name="seat", pw_gid=1004))
    monkeypatch.setattr(rc.os, "getgrouplist", lambda _name, _gid: [1003, 1004])
    assert rc._session_chronicle_group_command(["python3", "tool with space.py"]) == [
        "/usr/bin/sg", "pantheon-shared", "-c", "python3 'tool with space.py'"
    ]


def test_chronicle_group_command_does_not_wrap_unconfigured_account(monkeypatch):
    monkeypatch.setattr(rc.os, "geteuid", lambda: 1003)
    monkeypatch.setattr(rc.os, "getegid", lambda: 1004)
    monkeypatch.setattr(rc.os, "getgroups", lambda: [1004])
    monkeypatch.setattr(rc.grp, "getgrnam", lambda _name: mock.Mock(gr_gid=1003))
    monkeypatch.setattr(rc.pwd, "getpwuid", lambda _uid: mock.Mock(pw_name="seat", pw_gid=1004))
    monkeypatch.setattr(rc.os, "getgrouplist", lambda _name, _gid: [1004])
    argv = ["python3", "tool.py"]
    assert rc._session_chronicle_group_command(argv) == argv


def test_rearm_only_after_a_closed_tap(tmp_path):
    binding = _binding(tmp_path)
    with mock.patch.object(rc, "_run_submit_helper") as helper:
        rc._session_chronicle_rearm(binding, {"closed_pipe": False})
        rc._session_chronicle_rearm(binding, None)
        rc._session_chronicle_rearm(binding, {"closed_pipe": True},
                                    context=rc.TmuxRuntimeContext(socket="s", scope_unit="x.scope"))
        helper.assert_not_called()
        rc._session_chronicle_rearm(binding, {"closed_pipe": True})
        (argv,), kwargs = helper.call_args
        assert argv == ["bash", str(rc.ARM_PANE_LOG_TOOL), "pantheon-recovery:harness.0", "recovery"]
        assert kwargs["timeout"] == rc.SESSION_CHRONICLE_REARM_TIMEOUT_SECONDS
    with mock.patch.object(rc, "_run_submit_helper", side_effect=subprocess.TimeoutExpired("arm", 20)):
        rc._session_chronicle_rearm(binding, {"closed_pipe": True})  # swallowed


# ------------------------------------------------------------------ H1 placement
def _start(binding, *, replace: bool, server_pid=123):
    order: list[str] = []
    completed = mock.Mock(returncode=0, stdout="", stderr="")

    def hook(b, *, cause, context=None, state_root=None, stage_if_dead=False):
        order.append(f"hook:{cause}")
        return {"status": "ok", "closed_pipe": True}

    def tmux(*args, **kw):
        order.append(f"tmux:{args[0]}")
        return completed

    with mock.patch.object(rc, "_target_known", return_value=True), mock.patch.object(
        rc, "_default_tmux_server_pid", return_value=server_pid
    ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
        rc, "_run_tmux", side_effect=tmux
    ), mock.patch.object(rc, "_session_chronicle_end", side_effect=hook), mock.patch.object(
        rc, "_session_chronicle_rearm", side_effect=lambda b, end, context=None: order.append(f"rearm:{end['closed_pipe']}")
    ), mock.patch.object(codex_adapter_module, "ensure_project_trusted"), mock.patch.object(
        codex_adapter_module, "ensure_update_check_disabled"
    ), mock.patch.object(codex_adapter_module, "ensure_model_migration_prompts_hidden"), mock.patch.object(
        codex_adapter_module, "ensure_worker_guard_hook"
    ):
        rc._start_runtime(binding, _Adapter(binding), replace=replace)
    return order


@pytest.mark.parametrize("replace", [True, False])
def test_h1_archives_before_the_respawn_and_rearms_after(tmp_path, replace):
    order = _start(_binding(tmp_path), replace=replace)
    assert order == ["hook:start", "tmux:respawn-pane", "rearm:True"]


def test_h1_is_byte_identical_tmux_when_hooks_are_off(tmp_path):
    binding = _binding(tmp_path)
    completed = mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch.object(rc, "_target_known", return_value=True), mock.patch.object(
        rc, "_default_tmux_server_pid", return_value=123
    ), mock.patch.object(rc, "_pid_in_scope", return_value=True), mock.patch.object(
        rc, "_run_tmux", return_value=completed
    ) as run, mock.patch.object(rc, "_run_submit_helper") as helper, mock.patch.object(
        codex_adapter_module, "ensure_project_trusted"
    ), mock.patch.object(codex_adapter_module, "ensure_update_check_disabled"), mock.patch.object(
        codex_adapter_module, "ensure_model_migration_prompts_hidden"
    ), mock.patch.object(codex_adapter_module, "ensure_worker_guard_hook"):
        rc._start_runtime(binding, _Adapter(binding), replace=False)
    assert [c.args[0] for c in run.call_args_list] == ["respawn-pane"]
    helper.assert_not_called()


# ------------------------------------------------------------------ H2 placement
class _Outcome:
    pass


@pytest.mark.parametrize(
    "outcome,cause",
    [
        (None, "death"),
        (rc.MidSessionCapSwapOutcome(modal_kind="x", cooldown_until=rc._utc_now(), cooldown_source="t"), "cap-swap"),
        (rc.FallbackReturnOutcome(target_ref="claude/x"), "fallback-return"),
        (rc.OperatorSwapOutcome(destination_ref="claude/x"), None),  # review M1: skipped
        # T-5622: an ORG-106 deep-sleep drain end is "sleep", not a death.
        (rc.SeatSleepOutcome(drain_outcome="drained"), "sleep"),
    ],
)
def test_h2_archives_after_the_supervised_session_ends(tmp_path, outcome, cause):
    binding = _stateless_binding(tmp_path, harness="claude", profile="anthropic-code")
    seen: list[tuple] = []

    def fake_start(b):
        return (rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                rc.RuntimeHandle(b.launch, b.controller.tmux, None, rc._utc_now()))

    def block(b):
        seen.append(("block",))
        return outcome

    def hook(b, *, cause, context=None, state_root=None, stage_if_dead=False):
        seen.append(("hook", cause, stage_if_dead))

    with mock.patch.object(rc, "_session_chronicle_end", side_effect=hook):
        result = rc.controller_main(
            "orchestrator", candidates=[binding], state_root=tmp_path, sleep=lambda s: None,
            start_runtime_fn=fake_start, block_until_death_fn=block,
            persist_record_fn=lambda *a, **k: "persisted",
        )
    assert result == 0
    expected = [("block",)] if cause is None else [("block",), ("hook", cause, True)]
    assert seen == expected
    assert rc.read_active_runtime("orchestrator", state_root=tmp_path) == {}  # bookkeeping unchanged


def test_h2_passes_stage_if_dead_to_the_duty(tmp_path, hooks_on):
    calls, _ = hooks_on
    rc._session_chronicle_end(_binding(tmp_path), cause="death", state_root=tmp_path, stage_if_dead=True)
    assert "--stage-if-dead" in calls[0][0]
    rc._session_chronicle_end(_binding(tmp_path), cause="start", state_root=tmp_path)
    assert "--stage-if-dead" not in calls[1][0]
    assert calls[1][0][calls[1][0].index("--cwd") + 1] == str(tmp_path)


# ------------------------------------------------------------------ M2: preflight + failed launch
def _start_with(binding, *, pid_in_scope=True, server_pid=123, returncode=0, replace=False):
    order: list[str] = []
    completed = mock.Mock(returncode=returncode, stdout="", stderr="respawn failed")

    def hook(b, *, cause, context=None, state_root=None, stage_if_dead=False):
        order.append(f"hook:{cause}")
        return {"status": "ok", "closed_pipe": True}

    def tmux(*args, **kw):
        order.append(f"tmux:{args[0]}")
        return completed

    error = None
    with mock.patch.object(rc, "_target_known", return_value=True), mock.patch.object(
        rc, "_default_tmux_server_pid", return_value=server_pid
    ), mock.patch.object(rc, "_pid_in_scope", return_value=pid_in_scope), mock.patch.object(
        rc, "_run_tmux", side_effect=tmux
    ), mock.patch.object(rc, "_session_chronicle_end", side_effect=hook), mock.patch.object(
        rc, "_session_chronicle_rearm",
        side_effect=lambda b, end, context=None: order.append(f"rearm:{bool(end and end.get('closed_pipe'))}"),
    ), mock.patch.object(codex_adapter_module, "ensure_project_trusted"), mock.patch.object(
        codex_adapter_module, "ensure_update_check_disabled"
    ), mock.patch.object(codex_adapter_module, "ensure_model_migration_prompts_hidden"), mock.patch.object(
        codex_adapter_module, "ensure_worker_guard_hook"
    ):
        try:
            rc._start_runtime(binding, _Adapter(binding), replace=replace)
        except RuntimeError as exc:
            error = exc
    return order, error


def test_m2_a_refused_preflight_never_touches_the_old_session(tmp_path):
    order, error = _start_with(_binding(tmp_path), pid_in_scope=False)
    assert error is not None and order == []
    order, error = _start_with(_binding(tmp_path), server_pid=None, replace=True)
    assert error is not None and order == []


def test_m2_a_failed_respawn_gives_the_old_session_its_tap_back(tmp_path):
    order, error = _start_with(_binding(tmp_path), returncode=1)
    assert error is not None
    assert order == ["hook:start", "tmux:respawn-pane", "rearm:True"]


def test_m2_no_server_reconciles_after_its_preflight(tmp_path):
    binding = _binding(tmp_path)
    with mock.patch.object(rc, "_default_tmux_server_pid", return_value=None), mock.patch.object(
        rc, "_session_chronicle_end", return_value={"status": "ok"}
    ) as hook, mock.patch.object(rc, "_prepare_runtime_scope"), mock.patch.object(
        rc, "_start_new_server_in_scope", return_value=mock.Mock(returncode=0, stdout="", stderr="")
    ), mock.patch.object(codex_adapter_module, "ensure_project_trusted"), mock.patch.object(
        codex_adapter_module, "ensure_update_check_disabled"
    ), mock.patch.object(codex_adapter_module, "ensure_model_migration_prompts_hidden"), mock.patch.object(
        codex_adapter_module, "ensure_worker_guard_hook"
    ):
        rc._start_runtime(binding, _Adapter(binding), replace=False)
    assert hook.call_args.kwargs["cause"] == "start"


def test_h2_failure_does_not_change_the_attempt_record(tmp_path, monkeypatch):
    monkeypatch.setenv(rc.SESSION_CHRONICLE_HOOKS_ENV, "on")
    monkeypatch.setattr(rc, "_SESSION_CHRONICLE_RUNNER", mock.Mock(side_effect=RuntimeError("boom")))
    binding = _stateless_binding(tmp_path, harness="claude", profile="anthropic-code")

    def fake_start(b):
        return (rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                rc.RuntimeHandle(b.launch, b.controller.tmux, None, rc._utc_now()))

    result = rc.controller_main(
        "orchestrator", candidates=[binding], state_root=tmp_path, sleep=lambda s: None,
        start_runtime_fn=fake_start, block_until_death_fn=lambda b: None,
        persist_record_fn=lambda *a, **k: "persisted",
    )
    assert result == 0
    attempt = rc.load_last_attempt("orchestrator", "claude--anthropic-code", state_root=tmp_path)
    assert attempt["failure_class"] == "process_death"


def test_session_chronicle_is_in_the_runtime_source_manifest():
    assert "tools/session_chronicle.py" in rc.RUNTIME_SOURCE_HASH_PATHS
    assert "tools/chronicle.py" in rc.RUNTIME_SOURCE_HASH_PATHS
    assert "tools/chronicle_export.py" in rc.RUNTIME_SOURCE_HASH_PATHS  # T-6118: imported at session end
    assert "tools/appserver_seats.py" in rc.RUNTIME_SOURCE_HASH_PATHS
    assert "tools/harness_adapter/integration.py" not in rc.RUNTIME_SOURCE_HASH_PATHS  # follow-up, not this PR


def test_t5622_sleep_is_an_accepted_chronicle_cause_and_not_a_death():
    from tools import session_chronicle
    assert "sleep" in session_chronicle.CAUSES
    assert rc._SESSION_END_CAUSE_BY_OUTCOME["SeatSleepOutcome"] == "sleep"
    # only a drain outcome relabels; a bare death (None) and unknown outcomes stay death
    assert rc._SESSION_END_CAUSE_BY_OUTCOME.get("NoneType", "death") == "death"


def test_t5622_sleep_end_is_not_counted_as_a_respawn_loop_death(tmp_path, monkeypatch):
    from tools import channel_liveness_monitor as clm
    recs = [
        {"event": "chronicle_session_end", "cause": "sleep", "recorded_at": "2026-11-04T00:00:00Z", "transition_id": "a"},
        {"event": "chronicle_session_end", "cause": "death", "recorded_at": "2026-11-04T00:00:01Z", "transition_id": "b"},
    ]
    monkeypatch.setattr(clm, "_respawn_evidence_records", lambda *a, **k: recs)
    monkeypatch.setattr(clm, "_runtime_state_path_for_agent", lambda *a, **k: None)
    now = clm._iso_epoch("2026-11-04T00:00:10Z")
    assert clm._session_deaths_in_window("orchestrator", now, 3600) == 1
