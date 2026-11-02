"""T-5282: a Claude-only Telegram seat with no bridge binding or cross-harness
fallback needs no receiver handoff, so the consume path's read-only proof is
not-applicable. A seat that CAN hand off (Telegram channel + cross-harness fallback)
but lost its bridge entry must still fail closed. Archivist joined the bridged set in
T-5744."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import persona_bringup, runtime_controller as rc
from tools.harness_adapter import integration as harness_integration

MANIFEST = Path(__file__).resolve().parents[1] / "instance.yaml"
TELEGRAM = "plugin:telegram@claude-plugins-official"


def _binding(*, communications=None, channels=TELEGRAM, fallbacks=(), harness="claude", persona="seatx"):
    native = {"channels": channels} if channels else {}
    return SimpleNamespace(
        persona=persona,
        communications=communications,
        launch=SimpleNamespace(harness=harness, profile="p", native=native),
        controller=SimpleNamespace(policy=SimpleNamespace(fallbacks=tuple(fallbacks))),
    )


@pytest.fixture
def bridge(monkeypatch):
    """The bridge subprocess edge: raises exactly as binding_from_manifest does."""
    calls = []

    def fake(binding, *args, **kwargs):
        calls.append(args)
        raise RuntimeError("codex telegram bridge has no persona binding for 'seatx'")

    monkeypatch.setattr(harness_integration, "_telegram_subprocess", fake)
    return calls


def test_claude_only_telegram_seat_without_bridge_is_not_applicable(bridge):
    result = rc._read_only_receiver_present(_binding(), backend=None)
    assert result["ok"] is True
    assert result["state"] == "not-applicable"
    assert bridge == [], "the bridge must not be consulted for a seat that can never hand off"


def test_mailbox_only_seat_is_not_applicable_even_with_codex_fallback(bridge):
    seat = _binding(channels=None, fallbacks=("codex/interactive-chatgpt",))
    result = rc._read_only_receiver_present(seat, backend=None)
    assert result["state"] == "not-applicable"
    assert bridge == []


def test_telegram_seat_with_codex_fallback_and_missing_bridge_fails_closed(bridge):
    seat = _binding(fallbacks=("codex/interactive-chatgpt",))
    with pytest.raises(RuntimeError, match="no persona binding"):
        rc._read_only_receiver_present(seat, backend=None)
    assert bridge, "the bridge proof must still run for a seat that can hand off"


def test_bridged_seat_takes_the_unchanged_proof_path(monkeypatch):
    seat = _binding(communications={"kind": "telegram"}, fallbacks=("codex/interactive-chatgpt",))
    seen = []

    def fake(binding, *args, **kwargs):
        seen.append(args)
        return {"state": "present", "evidence": {"k": "v"}}

    monkeypatch.setattr(harness_integration, "_telegram_subprocess", fake)
    result = rc._read_only_receiver_present(seat, backend=None)
    assert result == {"ok": True, "state": "present", "evidence": {"k": "v"}}
    assert seen and "--expected" in seen[0]

    def absent(binding, *args, **kwargs):
        return {"state": "absent", "evidence": {}}

    monkeypatch.setattr(harness_integration, "_telegram_subprocess", absent)
    assert rc._read_only_receiver_present(seat, backend=None)["ok"] is False


def test_same_harness_fallback_does_not_count_as_a_handoff(bridge):
    seat = _binding(fallbacks=("claude/other-profile",))
    assert rc._read_only_receiver_present(seat, backend=None)["state"] == "not-applicable"


def test_real_manifest_classification():
    def load(persona):
        return persona_bringup.load_binding(MANIFEST, persona)

    assert rc._receiver_proof_not_applicable(load("artisan")) is True
    assert rc._receiver_proof_not_applicable(load("mason")) is True
    for bridged in ("orchestrator", "specialist", "recovery", "archivist"):
        assert rc._receiver_proof_not_applicable(load(bridged)) is False, bridged
