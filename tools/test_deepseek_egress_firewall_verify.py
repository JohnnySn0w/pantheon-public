"""T-6192: root firewall verifier is deterministic and value-free."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import deepseek_egress_firewall_verify as verify
from tools.deepseek_egress_firewall import render_firewall
from tools.deepseek_egress_proxy import FirewallState
from tools.deepseek_egress_proxy import ProxyPolicy


V4_BLOCK = b"# P-DEEPSEEK-EGRESS-BEGIN\n-A ufw-before-output -m owner --uid-owner 2101 -j REJECT\n# P-DEEPSEEK-EGRESS-END\n"
V6_BLOCK = b"# P-DEEPSEEK-EGRESS-BEGIN\n-A ufw6-before-output -m owner --uid-owner 2101 -j REJECT\n# P-DEEPSEEK-EGRESS-END\n"


def _source(block: bytes, prefix: bytes) -> bytes:
    return b"*filter\n" + block + prefix + b" -o lo -j ACCEPT\n" + prefix + b" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT\nCOMMIT\n"


def _manifest() -> dict[str, str]:
    return {
        "schema": "pantheon-deepseek-egress-gate/v1",
        "policy_digest": "0" * 64,
        "required": "enforced",
        "v4_block_sha256": verify.sha256(V4_BLOCK),
        "v6_block_sha256": verify.sha256(V6_BLOCK),
        "firewall_health": "/run/pantheon-deepseek-egress/firewall.json",
        "client_auth": "proxy-held-dummy",
    }


def _active(block: bytes, prefix: bytes) -> bytes:
    rules = b"".join(line + b"\n" for line in block.splitlines() if line.startswith(b"-A "))
    return rules + prefix + b" -o lo -j ACCEPT\n" + prefix + b" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT\n"


def test_verify_requires_source_placement_and_active_v4_v6_rules():
    active4 = _active(V4_BLOCK, b"-A ufw-before-output")
    active6 = _active(V6_BLOCK, b"-A ufw6-before-output")
    state = verify.verify_firewall(
        _manifest(),
        _source(V4_BLOCK, b"-A ufw-before-output"),
        _source(V6_BLOCK, b"-A ufw6-before-output"),
        active4,
        active6,
        observed_at=100,
    )
    assert state == FirewallState.enforced("0" * 64, verify.sha256(V4_BLOCK), verify.sha256(V6_BLOCK), observed_at=100)


def test_verifier_and_its_tests_import_when_certificate_package_is_blocked():
    code = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'cryptography' or name.startswith('cryptography.'):
        raise ModuleNotFoundError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import tools.deepseek_egress_firewall_verify
import tools.test_deepseek_egress_firewall_verify
import tools.deepseek_egress_install
import tools.test_deepseek_egress_install
assert len(tools.deepseek_egress_firewall_verify.sha256(b'probe')) == 64
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("missing", ["source4", "source6", "active4", "active6"])
def test_missing_or_drifted_family_fails_closed(missing: str):
    values = {
        "source4": _source(V4_BLOCK, b"-A ufw-before-output"),
        "source6": _source(V6_BLOCK, b"-A ufw6-before-output"),
        "active4": _active(V4_BLOCK, b"-A ufw-before-output"),
        "active6": _active(V6_BLOCK, b"-A ufw6-before-output"),
    }
    values[missing] = b""
    with pytest.raises(verify.VerifyError):
        verify.verify_firewall(_manifest(), values["source4"], values["source6"], values["active4"], values["active6"], observed_at=100)


def test_duplicate_misplaced_wrong_digest_and_unrelated_rule_fail_closed():
    source4 = _source(V4_BLOCK, b"-A ufw-before-output")
    source6 = _source(V6_BLOCK, b"-A ufw6-before-output")
    active4 = _active(V4_BLOCK, b"-A ufw-before-output")
    active6 = _active(V6_BLOCK, b"-A ufw6-before-output")
    bad = _manifest()
    bad["v4_block_sha256"] = "f" * 64
    cases = [
        (bad, source4, source6, active4, active6),
        (_manifest(), source4 + V4_BLOCK, source6, active4, active6),
        (_manifest(), source4, source6, b"-A ufw-before-output -o lo -j ACCEPT\n" + active4, active6),
    ]
    for args in cases:
        with pytest.raises(verify.VerifyError):
            verify.verify_firewall(*args, observed_at=100)


def test_record_writer_is_atomic_value_free_and_mode_bounded(tmp_path: Path):
    state = FirewallState.enforced("0" * 64, "1" * 64, "2" * 64, observed_at=100)
    target = tmp_path / "firewall.json"
    verify.write_state(target, state)
    assert (target.stat().st_mode & 0o777) == 0o444
    assert json.loads(target.read_text()) == state.as_mapping()
    assert not list(tmp_path.glob(".*.tmp"))


def test_failed_reverify_replaces_prior_enforced_with_fresh_unknown(tmp_path: Path):
    target = tmp_path / "firewall.json"
    prior = FirewallState.enforced("0" * 64, "1" * 64, "2" * 64, observed_at=90)
    verify.write_state(target, prior)
    with pytest.raises(verify.VerifyError):
        verify.verify_and_write(
            target,
            _manifest(),
            _source(V4_BLOCK, b"-A ufw-before-output"),
            _source(V6_BLOCK, b"-A ufw6-before-output"),
            b"drifted",
            _active(V6_BLOCK, b"-A ufw6-before-output"),
            observed_at=100,
        )
    state = FirewallState.from_mapping(json.loads(target.read_text()))
    assert state.state == "unknown" and state.observed_at == 100


def test_foreign_allow_before_owned_block_is_refused():
    source4 = b"*filter\n-A ufw-before-output -j ACCEPT\n" + V4_BLOCK + b"-A ufw-before-output -o lo -j ACCEPT\n-A ufw-before-output -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT\nCOMMIT\n"
    with pytest.raises(verify.VerifyError):
        verify.verify_firewall(
            _manifest(), source4, _source(V6_BLOCK, b"-A ufw6-before-output"),
            _active(V4_BLOCK, b"-A ufw-before-output"), _active(V6_BLOCK, b"-A ufw6-before-output"), observed_at=100,
        )


def test_active_real_blocks_refuse_interleaved_owner_accept_for_each_family():
    policy = ProxyPolicy.from_mapping(
        {
            "schema": "pantheon-deepseek-egress/v1",
            "local_authority": "deepseek-proxy.invalid:8448",
            "upstream_hostname": "api.deepseek.com",
            "upstream_port": 443,
            "routes": [
                {"id": "chat", "method": "POST", "local_path": "/v1/chat/completions", "upstream_path": "/v1/chat/completions", "json": True},
                {"id": "balance", "method": "GET", "local_path": "/v1/user/balance", "upstream_path": "/user/balance", "json": False},
            ],
            "limits": {"request_body_bytes": 65536, "response_body_bytes": 1048576, "header_bytes": 16384},
            "timeouts": {"connect_seconds": 3, "read_seconds": 15, "total_seconds": 60},
            "accounts": {"seat": "deepseek-fallback", "proxy": "pantheon-deepseek-proxy"},
        }
    )
    rendered = render_firewall(policy, seat_uid=2101, proxy_uid=2102)
    block4, block6 = rendered["v4"].encode(), rendered["v6"].encode()
    manifest = {
        **_manifest(),
        "v4_block_sha256": verify.sha256(block4),
        "v6_block_sha256": verify.sha256(block6),
    }
    source4 = _source(block4, b"-A ufw-before-output")
    source6 = _source(block6, b"-A ufw6-before-output")
    active4 = _active(block4, b"-A ufw-before-output")
    active6 = _active(block6, b"-A ufw6-before-output")
    injections = (
        (
            active4.replace(
                b"\n",
                b"\n-A ufw-before-output -m owner --uid-owner 2101 -j ACCEPT\n",
                1,
            ),
            active6,
        ),
        (
            active4,
            active6.replace(
                b"\n",
                b"\n-A ufw6-before-output -m owner --uid-owner 2101 -j ACCEPT\n",
                1,
            ),
        ),
    )
    for bad4, bad6 in injections:
        with pytest.raises(verify.VerifyError):
            verify.verify_firewall(manifest, source4, source6, bad4, bad6, observed_at=100)


def test_unflagged_cli_refuses_without_running_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(verify.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    assert verify.main([]) == 64
    assert calls == []
