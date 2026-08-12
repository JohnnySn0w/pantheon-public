"""Tests for tools.bringup_acceptance.codex_hermetic_home (T-2598).

Pure filesystem + stdlib tier -- never shells to a real codex binary. The
gated live-check (tools/t2598_live_codex_login_check.py) is the separate,
opt-in leg that actually proves a seeded home clears codex's login screen;
this tier only proves the FIXTURE is well-formed (the thing the live-check
depends on being right before it ever spends a real codex invocation).
"""

from __future__ import annotations

import base64
import json
import stat
import tempfile
import tomllib  # stdlib ≥3.11 — the declared floor (T-2733: production harness_adapter needs it)
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.bringup_acceptance import codex_hermetic_home as chh


def _b64url_decode_json(segment: str) -> dict:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))


class TestBuildSyntheticIdToken(unittest.TestCase):
    def test_three_dot_separated_parts(self) -> None:
        token = chh.build_synthetic_id_token()
        self.assertEqual(len(token.split(".")), 3)

    def test_header_and_payload_decode_to_json(self) -> None:
        header_b64, payload_b64, _sig = chh.build_synthetic_id_token().split(".")
        header = _b64url_decode_json(header_b64)
        payload = _b64url_decode_json(payload_b64)
        self.assertEqual(header, {"alg": "RS256", "typ": "JWT"})
        self.assertIn("sub", payload)
        self.assertIn("email", payload)
        self.assertIn("exp", payload)

    def test_account_id_propagates_into_claim(self) -> None:
        _header_b64, payload_b64, _sig = chh.build_synthetic_id_token(
            account_id="acct-test-123"
        ).split(".")
        payload = _b64url_decode_json(payload_b64)
        self.assertEqual(
            payload["https://api.openai.com/auth"]["chatgpt_account_id"],
            "acct-test-123",
        )


class TestBuildSyntheticAuthJson(unittest.TestCase):
    def test_top_level_keys(self) -> None:
        auth = chh.build_synthetic_auth_json()
        self.assertEqual(
            set(auth.keys()),
            {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"},
        )
        self.assertEqual(auth["auth_mode"], "chatgpt")
        self.assertIsNone(auth["OPENAI_API_KEY"])

    def test_tokens_shape(self) -> None:
        auth = chh.build_synthetic_auth_json(account_id="acct-xyz")
        tokens = auth["tokens"]
        self.assertEqual(
            set(tokens.keys()),
            {"id_token", "access_token", "refresh_token", "account_id"},
        )
        self.assertEqual(tokens["account_id"], "acct-xyz")
        self.assertEqual(len(tokens["id_token"].split(".")), 3)

    def test_last_refresh_uses_injected_clock(self) -> None:
        fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        auth = chh.build_synthetic_auth_json(now=lambda: fixed)
        self.assertEqual(auth["last_refresh"], "2026-01-01T00:00:00Z")


class TestSeedHermeticCodexHome(unittest.TestCase):
    def test_writes_auth_json_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            trusted_cwd = Path(tmp) / "work"
            chh.seed_hermetic_codex_home(codex_home, trusted_cwd)

            auth_path = codex_home / "auth.json"
            self.assertTrue(auth_path.exists())
            mode = stat.S_IMODE(auth_path.stat().st_mode)
            self.assertEqual(mode, 0o600)

            auth = json.loads(auth_path.read_text(encoding="utf-8"))
            self.assertEqual(auth["auth_mode"], "chatgpt")

    def test_writes_trust_entry_for_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            trusted_cwd = Path(tmp) / "work"
            chh.seed_hermetic_codex_home(codex_home, trusted_cwd)

            config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
            parsed = tomllib.loads(config_text)
            projects = parsed["projects"]
            self.assertIn(str(trusted_cwd), projects)
            self.assertEqual(projects[str(trusted_cwd)]["trust_level"], "trusted")

    def test_account_id_flows_through_to_auth_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            trusted_cwd = Path(tmp) / "work"
            chh.seed_hermetic_codex_home(
                codex_home, trusted_cwd, account_id="acct-flow-through"
            )
            auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(auth["tokens"]["account_id"], "acct-flow-through")

    def test_reseeding_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            trusted_cwd = Path(tmp) / "work"
            chh.seed_hermetic_codex_home(codex_home, trusted_cwd)
            chh.seed_hermetic_codex_home(codex_home, trusted_cwd)

            auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(auth["auth_mode"], "chatgpt")
            config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
            parsed = tomllib.loads(config_text)
            self.assertEqual(
                parsed["projects"][str(trusted_cwd)]["trust_level"], "trusted"
            )


if __name__ == "__main__":
    unittest.main()
