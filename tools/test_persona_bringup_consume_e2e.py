"""T-4888: drive ``persona_bringup.main(["consume", ...])`` through the interactive-wake branch
with the REAL public ``wake_persistent_runtime`` wrapper and the REAL ``_wake_persistent_runtime_locked``
worker. Only IO edges are faked (binding load / diagnostics, tmux + systemd runtime controller calls,
pane probes); all state lives under ``tmp_path``.

Why: #894 added ``gate_on_usability``/``probe_deps`` to the worker only. ``main()`` passes
``gate_on_usability=True`` to the wrapper, so every persona-consume fire raised TypeError in
production, while every existing test mocked ``wake_persistent_runtime`` itself and stayed green.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from tools import persona_bringup, runtime_controller


def _binding(root: Path):
    mailbox = root / "mailbox"
    (mailbox / "new").mkdir(parents=True)
    policy = SimpleNamespace(weekly_remaining_percent=10, fallbacks=())
    return SimpleNamespace(
        persona="recovery",
        mailbox=mailbox,
        account_home=root / "home",
        launch=SimpleNamespace(mode="interactive", harness="claude", profile="interactive", model=None),
        controller=SimpleNamespace(policy=policy, tmux=None),
    )


class _Rig:
    def __init__(self, tmp_path: Path):
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.binding = _binding(tmp_path)
        snapshot = SimpleNamespace(freshness="fresh", windows=())
        self.adapter = mock.Mock()
        self.adapter.quota_observer.return_value.read.return_value = snapshot
        self.handle = SimpleNamespace(tmux="%fake-pane", session_id="s1", profile=self.binding.launch)
        self.selection = SimpleNamespace(
            binding=self.binding, adapter=self.adapter, handle=self.handle, receipt={"selected": "fake"})
        self.fallback = None
        self.on_swap = None  # called with planned_swap kwargs while patches are live
        self.fakes: dict[str, mock.Mock] = {}

    def run(self, extra=()):
        report = SimpleNamespace(observations=[SimpleNamespace(status="ok")])
        out = io.StringIO()
        printed: list = []  # stdout JSON edge: binding is a stand-in, not serializable
        rc = runtime_controller
        edges = [
            mock.patch.object(persona_bringup, "load_binding", return_value=self.binding),
            mock.patch.object(persona_bringup, "select_recorded_runtime_binding", return_value=self.binding),
            mock.patch.object(persona_bringup, "diagnose_persona", return_value=report),
            mock.patch.object(persona_bringup, "build_adapter", return_value=self.adapter),
            mock.patch.object(persona_bringup, "first_fallback_binding", return_value=self.fallback),
            mock.patch.object(persona_bringup, "configured_runtime_bindings", return_value=[]),
            mock.patch.object(rc, "ensure_viable_runtime", return_value=self.selection),
            mock.patch.object(rc, "ensure_runtime", return_value=self.handle),
            mock.patch.object(rc, "planned_swap", side_effect=self.on_swap),
            mock.patch.object(rc, "assert_drain_allowed"),
            mock.patch.object(rc, "runtime_target_text", return_value="%fake-pane"),
            mock.patch.object(rc, "submit_to_runtime"),
            mock.patch.object(persona_bringup, "_print_json", side_effect=printed.append),
            mock.patch("tools.mailbox_wake.pane_is_idle", return_value=True),
            mock.patch("tools.mailbox_wake.session_key", return_value="sess"),
            *extra,
        ]
        with contextlib.ExitStack() as st:
            mocks = [st.enter_context(p) for p in edges]
            self.fakes = {"planned_swap": mocks[8], "submit": mocks[11]}
            st.enter_context(contextlib.redirect_stdout(out))
            code = persona_bringup.main([
                "consume", "--persona", "recovery", "--state-dir", str(self.state),
                "--cwd", str(self.state), "--manifest", str(self.state / "no-manifest.yaml"),
                "--mailbox-hub", str(self.state / "hub"), "--reply-hub", str(self.state / "hub"),
            ])
        return code, printed


def test_consume_interactive_wake_runs_real_wrapper_and_worker(tmp_path):
    rig = _Rig(tmp_path)
    code, out = rig.run()
    assert code == 0, out
    consume = out[-1]["consume"]
    assert consume["mode"] == "interactive-wake"
    assert consume["mailbox_claimed"] is False


def test_gate_on_usability_reaches_worker_and_probe_deps_reach_gate(tmp_path):
    """Seed a cooperative planned-quota transition so the worker builds and passes the usability gate
    to the (faked) controller edge; prove the gate is live, i.e. gate_on_usability=True survived the
    wrapper."""
    rig = _Rig(tmp_path)
    target = SimpleNamespace(persona="recovery", launch=SimpleNamespace(harness="codex", profile="fb"))
    decision = SimpleNamespace(chosen=object(), per_candidate={"codex/fb": "usable"})
    select = mock.Mock(return_value=(decision, {}))
    rig.fallback = target
    gates: list = []

    def on_swap(*args, **kwargs):
        gate = kwargs.get("pre_replace_gate")
        if gate is not None:
            gates.append(gate(target))

    rig.on_swap = on_swap
    with mock.patch.object(runtime_controller, "transition_state",
                           return_value={"phase": "preparing", "trigger": "planned-quota"}):
        code, out = rig.run(extra=[
            mock.patch.object(persona_bringup.subscription_usability, "select_usable_fallback", select),
            mock.patch.object(persona_bringup.subscription_usability, "runtime_ref", return_value="codex/fb"),
        ])
    assert code == 0, out
    assert gates, "planned_swap never received a pre_replace_gate (gate_on_usability dropped)"
    assert gates[0] == (True, {"codex/fb": "usable"})
    assert select.call_args.kwargs["force"] is True
