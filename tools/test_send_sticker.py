"""Tests for tools/send_sticker.py (T-3319 Part B).

Small helper -- adversarial coverage of the failure modes only. The real
network call is not exercised (subprocess is mocked); the file_id resolution
+ token-scrub + error-clarity paths are.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.send_sticker as ss


class TokenLoad(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_ctx.name)
        self.addCleanup(self._tmp_ctx.cleanup)

    def test_reads_TELEGRAM_BOT_TOKEN_line(self):
        f = self.tmp / ".env"
        f.write_text("OTHER=x\nTELEGRAM_BOT_TOKEN=abc123\nMORE=y\n")
        self.assertEqual(ss._load_token(f), "abc123")

    def test_missing_line_raises_named_error(self):
        f = self.tmp / ".env"
        f.write_text("OTHER=x\n")
        with self.assertRaises(RuntimeError) as ctx:
            ss._load_token(f)
        self.assertIn("TELEGRAM_BOT_TOKEN=", str(ctx.exception))
        self.assertIn(str(f), str(ctx.exception))

    def test_missing_file_raises_named_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            ss._load_token(self.tmp / "nope.env")
        self.assertIn("could not read", str(ctx.exception))


class ManifestResolve(unittest.TestCase):
    def _manifest(self):
        return {
            "orchestrator": {"stickers": [
                {"reaction": "approved", "file_id": "CAACAg_APPROVED_id"},
                {"reaction": "wedged", "file_id": "CAACAg_WEDGED_id"},
                {"reaction": "unminted", "file_id": ""},  # committed but no file_id
            ]},
            "specialist": {"stickers": [{"reaction": "approved", "file_id": "specialist_id"}]},
        }

    def test_hit(self):
        self.assertEqual(ss._resolve_file_id(self._manifest(), "orchestrator", "approved"),
                          "CAACAg_APPROVED_id")

    def test_missing_persona_lists_available(self):
        with self.assertRaises(RuntimeError) as ctx:
            ss._resolve_file_id(self._manifest(), "nobody", "approved")
        self.assertIn("nobody", str(ctx.exception))
        self.assertIn("orchestrator", str(ctx.exception))
        self.assertIn("specialist", str(ctx.exception))

    def test_missing_reaction_lists_available(self):
        with self.assertRaises(RuntimeError) as ctx:
            ss._resolve_file_id(self._manifest(), "orchestrator", "nonesuch")
        self.assertIn("nonesuch", str(ctx.exception))
        self.assertIn("approved", str(ctx.exception))
        self.assertIn("wedged", str(ctx.exception))
        # 'unminted' has an empty file_id -- should NOT be advertised as available.
        self.assertNotIn("unminted", str(ctx.exception))

    def test_empty_file_id_treated_as_missing(self):
        # A manifest entry with reaction present but file_id blank should raise
        # (not return an empty string that would then break the curl call).
        with self.assertRaises(RuntimeError):
            ss._resolve_file_id(self._manifest(), "orchestrator", "unminted")


class SendStickerHappy(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_ctx.name)
        self.addCleanup(self._tmp_ctx.cleanup)
        self.manifest = self.tmp / "manifest.json"
        self.manifest.write_text(json.dumps({
            "orchestrator": {"stickers": [{"reaction": "approved", "file_id": "FID_APPROVED"}]},
        }))
        self.token = self.tmp / ".env"
        self.token.write_text("TELEGRAM_BOT_TOKEN=SECRET_TOKEN\n")

    def test_sends_expected_curl(self):
        captured = {}
        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": True, "result": {"message_id": 42}}), stderr="")
        with mock.patch.object(ss.subprocess, "run", side_effect=fake_run):
            resp = ss.send_sticker("orchestrator", "approved", "424242424",
                                    token_file=self.token, manifest_path=self.manifest)
        self.assertTrue(resp.get("ok"))
        self.assertEqual(resp["result"]["message_id"], 42)
        self.assertIn("FID_APPROVED", " ".join(captured["argv"]))
        self.assertIn("424242424", " ".join(captured["argv"]))
        self.assertIn("sendSticker", " ".join(captured["argv"]))
        # Token lives only in the URL, not as a separate argv token.
        self.assertNotIn("SECRET_TOKEN", [a for a in captured["argv"] if a == "SECRET_TOKEN"])

    def test_api_error_returns_dict_not_raises(self):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": False, "description": "chat not found"}), stderr="")
        with mock.patch.object(ss.subprocess, "run", side_effect=fake_run):
            resp = ss.send_sticker("orchestrator", "approved", "0",
                                    token_file=self.token, manifest_path=self.manifest)
        self.assertFalse(resp.get("ok"))
        self.assertIn("chat not found", resp.get("description", ""))

    def test_transport_timeout_raises_token_free_error(self):
        # T-1024 review: str(TimeoutExpired) embeds the argv, whose URL carries the token.
        def fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 30)
        with mock.patch.object(ss.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as cm:
                ss.send_sticker("orchestrator", "approved", "1", token_file=self.token, manifest_path=self.manifest)
        self.assertNotIn("SECRET_TOKEN", str(cm.exception))
        self.assertIn("TimeoutExpired", str(cm.exception))

    def test_non_json_response_wrapped_not_raises(self):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="<html>500</html>", stderr="curl: timeout")
        with mock.patch.object(ss.subprocess, "run", side_effect=fake_run):
            resp = ss.send_sticker("orchestrator", "approved", "0",
                                    token_file=self.token, manifest_path=self.manifest)
        self.assertFalse(resp.get("ok"))
        self.assertIn("_raw", resp)


class MainCliAndScrub(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_ctx.name)
        self.addCleanup(self._tmp_ctx.cleanup)
        self.manifest = self.tmp / "manifest.json"
        self.manifest.write_text(json.dumps({
            "orchestrator": {"stickers": [{"reaction": "approved", "file_id": "FID_APPROVED"}]},
        }))
        self.token = self.tmp / ".env"
        self.token.write_text("TELEGRAM_BOT_TOKEN=SECRET_TOKEN_XYZ\n")

    def _run(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = ss.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_ok_exit_zero(self):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": True, "result": {"message_id": 1}}), stderr="")
        with mock.patch.object(ss.subprocess, "run", side_effect=fake_run):
            rc, out, err = self._run("orchestrator", "approved", "123",
                                     "--token-file", str(self.token), "--manifest", str(self.manifest))
        self.assertEqual(rc, 0)
        self.assertIn("message_id", out)

    def test_api_false_exit_nonzero(self):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": False, "description": "no"}), stderr="")
        with mock.patch.object(ss.subprocess, "run", side_effect=fake_run):
            rc, out, err = self._run("orchestrator", "approved", "123",
                                     "--token-file", str(self.token), "--manifest", str(self.manifest))
        self.assertEqual(rc, 1)

    def test_missing_persona_exits_with_stderr_hint(self):
        rc, out, err = self._run("nobody", "approved", "123",
                                 "--token-file", str(self.token), "--manifest", str(self.manifest))
        self.assertEqual(rc, 1)
        self.assertIn("nobody", err)
        self.assertIn("orchestrator", err)

    def test_token_scrubbed_if_it_leaks_into_response(self):
        # Simulate a worst-case curl error whose stderr includes the URL
        # (and therefore the token). The scrub in main() should redact it.
        def fake_run(argv, **kwargs):
            # non-json stdout so the response comes back with _raw/_err populated
            return subprocess.CompletedProcess(
                argv, 22, stdout="oops",
                stderr=f"curl: (7) failed to connect to https://api.telegram.org/botSECRET_TOKEN_XYZ/sendSticker",
            )
        with mock.patch.object(ss.subprocess, "run", side_effect=fake_run):
            rc, out, err = self._run("orchestrator", "approved", "123",
                                     "--token-file", str(self.token), "--manifest", str(self.manifest))
        self.assertEqual(rc, 1)
        self.assertNotIn("SECRET_TOKEN_XYZ", out)
        self.assertIn("<TOKEN-REDACTED>", out)


if __name__ == "__main__":
    unittest.main()
