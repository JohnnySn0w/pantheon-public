"""T-5282: the consume entry skips the whole cycle while the seat is in seat-maintenance
(notify_policy.in_maintenance). Maintenance is the hold mechanism for a seat with no
holds.json; without this guard an unblocked consume would rehydrate/launch or wake a
seat that is deliberately held. Fail-open on any fault (holds.json stays the
fail-closed structural hold)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from tools import persona_bringup, runtime_controller
from tools.test_persona_bringup_consume_e2e import _Rig


def _arm(tmp_path, monkeypatch, *, expires_delta):
    expires = (datetime.now(timezone.utc) + expires_delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = tmp_path / "seat_maintenance.json"
    path.write_text(json.dumps({
        "schema": "pantheon-seat-maintenance/v1",
        "maintenance": {"recovery": {"expires": expires, "by": "t", "reason": "T-5282 test"}},
    }))
    monkeypatch.setenv("PANTHEON_MAINTENANCE_REGISTRY", str(path))


def _run(rig):
    rc = runtime_controller
    mail = rig.binding.mailbox / "new" / "m1.json"
    mail.write_text(json.dumps({"id": "m1", "from": "x", "payload": {"subject": "s"}}))
    wake = mock.Mock(wraps=persona_bringup.wake_persistent_runtime)
    ensure_viable = mock.Mock(return_value=rig.selection)
    ensure_runtime = mock.Mock(return_value=rig.handle)
    start = mock.Mock()
    rehydrate = mock.Mock()
    code, out = rig.run(extra=[
        mock.patch.object(persona_bringup, "wake_persistent_runtime", wake),
        mock.patch.object(rc, "ensure_viable_runtime", ensure_viable),
        mock.patch.object(rc, "ensure_runtime", ensure_runtime),
        mock.patch.object(rc, "_start_runtime", start),
        mock.patch.object(rc, "_rehydrate_recorded_runtime_selection", rehydrate),
    ])
    return code, out, mail, {"wake": wake, "ensure_viable": ensure_viable, "start": start,
                              "rehydrate": rehydrate, "ensure_runtime": ensure_runtime}


def test_armed_seat_is_skipped_with_no_launch_no_wake_no_claim(tmp_path, monkeypatch):
    _arm(tmp_path, monkeypatch, expires_delta=timedelta(hours=1))
    rig = _Rig(tmp_path)
    code, out, mail, m = _run(rig)
    assert code == 0, out
    assert out[-1]["consume"]["skipped"] == "seat_maintenance"
    assert out[-1]["outcomes"] == []
    for name in ("wake", "ensure_viable", "start", "rehydrate", "ensure_runtime"):
        m[name].assert_not_called()
    rig.fakes["submit"].assert_not_called()
    assert mail.exists(), "mail must stay queued, unclaimed, for after the hold"


def test_expired_maintenance_proceeds_normally(tmp_path, monkeypatch):
    _arm(tmp_path, monkeypatch, expires_delta=timedelta(hours=-1))
    rig = _Rig(tmp_path)
    code, out, _mail, m = _run(rig)
    assert code == 0, out
    assert out[-1]["consume"]["mode"] == "interactive-wake"
    m["wake"].assert_called_once()


def test_no_maintenance_entry_proceeds_normally(tmp_path, monkeypatch):
    monkeypatch.setenv("PANTHEON_MAINTENANCE_REGISTRY", str(tmp_path / "absent.json"))
    rig = _Rig(tmp_path)
    code, out, _mail, m = _run(rig)
    assert code == 0, out
    m["wake"].assert_called_once()


def test_notify_policy_raising_fails_open(tmp_path, monkeypatch):
    rig = _Rig(tmp_path)
    with mock.patch("tools.notify_policy.in_maintenance", side_effect=RuntimeError("boom")):
        code, out, _mail, m = _run(rig)
    assert code == 0, out
    assert out[-1]["consume"].get("skipped") != "seat_maintenance"
    m["wake"].assert_called_once()


def test_skip_records_policy_skip_reason(tmp_path, monkeypatch):
    _arm(tmp_path, monkeypatch, expires_delta=timedelta(hours=1))
    rig = _Rig(tmp_path)
    code, _out, _mail, _m = _run(rig)
    assert code == 0
    assert persona_bringup.load_policy_skip(rig.state, "recovery")["reason"] == "seat_maintenance"


# --- T-2953: one-shot hand-move bootstrap bypass token --------------------------------
# The hand-move SOP requires the seat to be in maintenance (T-4377) AND needs exactly one
# consume fire to bootstrap the runtime (phase 4). The token lets that ONE fire through.

def _token(rig, *, persona="recovery", expires_delta=timedelta(minutes=5), body=None):
    expires = (datetime.now(timezone.utc) + expires_delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = persona_bringup.maintenance_bypass_path(rig.state, persona)
    path.write_text(json.dumps(body if body is not None else
                               {"persona": persona, "expires": expires, "by": "t2953-test"}))
    return path


def test_bypass_token_lets_one_fire_through_and_is_consumed(tmp_path, monkeypatch):
    _arm(tmp_path, monkeypatch, expires_delta=timedelta(hours=1))
    rig = _Rig(tmp_path)
    tok = _token(rig)
    code, out, _mail, m = _run(rig)
    assert code == 0, out
    assert out[-1]["consume"].get("skipped") != "seat_maintenance"
    m["wake"].assert_called_once()
    assert not tok.exists(), "the token is single-use"
    # the next fire, with maintenance still armed, is held again
    code, out, _mail, m = _run(rig)
    assert out[-1]["consume"]["skipped"] == "seat_maintenance"
    m["wake"].assert_not_called()


def test_expired_bypass_token_does_not_bypass_and_is_removed(tmp_path, monkeypatch):
    _arm(tmp_path, monkeypatch, expires_delta=timedelta(hours=1))
    rig = _Rig(tmp_path)
    tok = _token(rig, expires_delta=timedelta(minutes=-1))
    code, out, _mail, m = _run(rig)
    assert out[-1]["consume"]["skipped"] == "seat_maintenance"
    m["wake"].assert_not_called()
    assert not tok.exists()


def test_bypass_token_for_another_persona_does_not_bypass(tmp_path, monkeypatch):
    _arm(tmp_path, monkeypatch, expires_delta=timedelta(hours=1))
    rig = _Rig(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tok = _token(rig, body={"persona": "orchestrator", "expires": future})  # at RECOVERY's path
    code, out, _mail, m = _run(rig)
    assert out[-1]["consume"]["skipped"] == "seat_maintenance"
    m["wake"].assert_not_called()
    assert not tok.exists()


def test_far_future_bypass_token_is_refused(tmp_path, monkeypatch):
    # a token is a seconds-scale bootstrap aid, never a standing release of the hold
    _arm(tmp_path, monkeypatch, expires_delta=timedelta(hours=1))
    rig = _Rig(tmp_path)
    _token(rig, expires_delta=timedelta(hours=2))
    code, out, _mail, m = _run(rig)
    assert out[-1]["consume"]["skipped"] == "seat_maintenance"
    m["wake"].assert_not_called()


def test_malformed_bypass_token_does_not_bypass(tmp_path, monkeypatch):
    _arm(tmp_path, monkeypatch, expires_delta=timedelta(hours=1))
    rig = _Rig(tmp_path)
    persona_bringup.maintenance_bypass_path(rig.state, "recovery").write_text("{not json")
    code, out, _mail, m = _run(rig)
    assert out[-1]["consume"]["skipped"] == "seat_maintenance"
    m["wake"].assert_not_called()


def test_token_is_cleared_even_when_maintenance_has_lapsed(tmp_path, monkeypatch):
    # review finding 1: the seat script fails if its token survives the fire, so consume
    # must clear it on an unarmed fire too (maintenance expired mid-move, or failed open)
    monkeypatch.setenv("PANTHEON_MAINTENANCE_REGISTRY", str(tmp_path / "absent.json"))
    rig = _Rig(tmp_path)
    tok = _token(rig)
    code, out, _mail, m = _run(rig)
    assert code == 0, out
    m["wake"].assert_called_once()
    assert not tok.exists()


def test_non_utf8_token_does_not_crash_and_is_removed(tmp_path, monkeypatch):
    _arm(tmp_path, monkeypatch, expires_delta=timedelta(hours=1))
    rig = _Rig(tmp_path)
    tok = persona_bringup.maintenance_bypass_path(rig.state, "recovery")
    tok.write_bytes(b"\xff\xfe\x00garbage")
    code, out, _mail, m = _run(rig)
    assert code == 0, out
    assert out[-1]["consume"]["skipped"] == "seat_maintenance"
    assert not tok.exists()
