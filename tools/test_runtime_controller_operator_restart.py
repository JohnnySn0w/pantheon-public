"""T-5483: an operator `harness restart` is never recorded as a runtime failure.

The harness drops a durable marker before the kill; the controller consumes it at
its death observation (running phase) or start/readiness failure (start phase).
Durable state only: a FRESH controller_main call after the kill must still select
the manifest-first runtime.

Layout: harness root R == tmp_path; controller state_root == R/personas."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools import harness_state as hs
from tools import runtime_controller as rc
from tools.test_runtime_controller import _stateless_binding

T0 = datetime(2026, 10, 25, 22, 37, 0, tzinfo=timezone.utc)
CLAUDE = "claude/anthropic-code"
CODEX = "codex/interactive"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _marker(root: Path, *, ts: datetime, runtime: str = CLAUDE) -> None:
    hs.write_operator_restart_marker(
        "orchestrator", root=root, operator="orchestrator", runtime=runtime, tmux_session="s",
        pid=1, ts=_iso(ts), correlation_id="corr-1",
    )


def _marker_exists(root: Path) -> bool:
    return hs.operator_restart_path("orchestrator", root=root).exists()


def _run(root: Path, *, alive: float, selected_log=None, start_raises=None, failure_class=None):
    """One controller_main process. The runtime starts at T0 and ends at T0+alive."""
    claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
    codex = _stateless_binding(root, harness="codex", profile="interactive")
    clock = {"t": T0}

    def fake_start(b):
        if selected_log is not None:
            selected_log.append(rc._binding_runtime_ref(b))
        if failure_class is not None:
            clock["t"] = T0 + timedelta(seconds=alive)
            raise rc.RuntimeOperationalFailure(
                "start failed", failure_class=failure_class, cause="test",
                binding_runtime=rc._binding_runtime_ref(b),
            )
        return (
            rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
            rc.RuntimeHandle(b.launch, b.controller.tmux, None, clock["t"]),
        )

    def fake_block(b):
        clock["t"] = T0 + timedelta(seconds=alive)
        return None

    return rc.controller_main(
        "orchestrator", candidates=[claude, codex], state_root=root / "personas", now=lambda: clock["t"],
        sleep=lambda s: None, max_reselect_cycles=1,
        start_runtime_fn=fake_start, block_until_death_fn=fake_block,
        persist_record_fn=lambda *a, **k: "persisted",
    )


def _attempt(root: Path, slug="claude--anthropic-code"):
    return rc.load_last_attempt("orchestrator", slug, state_root=root / "personas")


def _events(root: Path):
    path = root / "personas" / "orchestrator" / "runtime-controller" / "controller-evidence.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_regression_restart_then_fresh_controller_selects_manifest_first(tmp_path):
    _marker(tmp_path, ts=T0 + timedelta(seconds=118))
    assert _run(tmp_path, alive=120) == 0  # killed ~2 min after start
    attempt = _attempt(tmp_path)
    assert attempt["outcome"] == "success"
    assert not attempt.get("cooldown_until")
    assert not _marker_exists(tmp_path)
    assert any(e.get("event") == "operator_restart_observed" for e in _events(tmp_path))
    picked: list[str] = []  # fresh controller process: durable state only
    _run(tmp_path, alive=400, selected_log=picked)
    assert picked == [CLAUDE]


def test_no_marker_early_death_still_process_death(tmp_path):
    _run(tmp_path, alive=120)
    attempt = _attempt(tmp_path)
    assert attempt["outcome"] == "failed" and attempt["failure_class"] == "process_death"
    picked: list[str] = []
    _run(tmp_path, alive=400, selected_log=picked)
    assert picked == [CODEX]  # unchanged behaviour: claude cooling down


def test_marker_older_than_max_age_is_ignored_and_cleaned(tmp_path):
    _marker(tmp_path, ts=T0 + timedelta(seconds=10))
    _run(tmp_path, alive=10 + 181)  # 181s old at observation
    attempt = _attempt(tmp_path)
    assert attempt["outcome"] == "failed" and attempt["failure_class"] == "process_death"
    assert not _marker_exists(tmp_path)


def test_marker_written_before_this_attempt_started_is_ignored(tmp_path):
    _marker(tmp_path, ts=T0 - timedelta(seconds=5))
    _run(tmp_path, alive=60)
    assert _attempt(tmp_path)["failure_class"] == "process_death"
    assert not _marker_exists(tmp_path)


def test_marker_for_other_runtime_ignored_and_cleaned(tmp_path):
    _marker(tmp_path, ts=T0 + timedelta(seconds=50), runtime=CODEX)
    _run(tmp_path, alive=60)
    assert _attempt(tmp_path)["failure_class"] == "process_death"
    assert not _marker_exists(tmp_path)


def test_marker_consumed_once(tmp_path):
    _marker(tmp_path, ts=T0 + timedelta(seconds=50))
    _run(tmp_path, alive=60)
    assert _attempt(tmp_path)["outcome"] == "success"
    _run(tmp_path, alive=60)  # second early death, no new marker
    assert _attempt(tmp_path)["failure_class"] == "process_death"


def test_marker_honoured_after_sustained_alive_too(tmp_path):
    _marker(tmp_path, ts=T0 + timedelta(seconds=3600))
    _run(tmp_path, alive=3601)
    assert _attempt(tmp_path)["outcome"] == "success"
    assert not _marker_exists(tmp_path)


def test_malformed_marker_ignored_and_removed(tmp_path):
    path = hs.operator_restart_path("orchestrator", root=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    _run(tmp_path, alive=60)
    assert _attempt(tmp_path)["failure_class"] == "process_death"
    assert not path.exists()


# ---- start / readiness phase -------------------------------------------------
def test_readiness_phase_failure_with_marker_is_success_not_cooldown(tmp_path):
    _marker(tmp_path, ts=T0 + timedelta(seconds=20))
    assert _run(tmp_path, alive=30, failure_class="operational-readiness") == 1
    attempt = _attempt(tmp_path)
    assert attempt["outcome"] == "success"
    assert not attempt.get("cooldown_until")
    assert not _marker_exists(tmp_path)
    assert any(
        e.get("event") == "operator_restart_observed" and e.get("phase") == "start"
        for e in _events(tmp_path)
    )


def test_readiness_phase_failure_without_marker_still_fails(tmp_path):
    assert _run(tmp_path, alive=30, failure_class="operational-readiness") == 1
    assert _attempt(tmp_path)["failure_class"] == "readiness_timeout"


def test_subscription_cap_is_never_masked_by_the_marker(tmp_path):
    _marker(tmp_path, ts=T0 + timedelta(seconds=20))
    assert _run(tmp_path, alive=30, failure_class="subscription-cap") == 1
    attempt = _attempt(tmp_path)
    assert attempt["outcome"] == "failed" and attempt["failure_class"] == "subscription_cap"


def test_marker_path_parity_between_harness_state_and_controller(tmp_path):
    assert rc._operator_restart_marker_path("orchestrator", state_root=tmp_path / "personas") == (
        hs.operator_restart_path("orchestrator", root=tmp_path)
    )


def test_unreadable_or_unremovable_marker_fails_open_with_evidence(tmp_path):
    path = hs.operator_restart_path("orchestrator", root=tmp_path)
    path.mkdir(parents=True)  # a directory: read and unlink both raise OSError
    _run(tmp_path, alive=60)  # must not raise
    assert _attempt(tmp_path)["failure_class"] == "process_death"
    names = {e.get("event") for e in _events(tmp_path)}
    assert "operator_restart_marker_unreadable" in names
    assert "operator_restart_marker_unlink_failed" in names
