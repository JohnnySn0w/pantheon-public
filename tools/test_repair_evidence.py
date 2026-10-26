"""Tests for tools/repair_evidence.py -- RFC-004 §6.6, §6.15-18 (T-4707 BT1)."""
from __future__ import annotations

import re
import unittest

from tools import repair_evidence as re_


# Test-issued fake secrets (assembled so no scanner mistakes this file for a leak).
_SK = "sk-" + "ant-" + "TESTISSUED0123456789abcd"
_GH = "ghp_" + "TESTISSUED0123456789ABCDEFGHIJ"
_TG = "1234567890:" + "AATESTISSUED0123456789abcdefghijk"
_BEARER = "TESTBEARERvalue0123456789"
_JWT = "eyJ" + "hbGciOiJIUzI1" + ".eyJ" + "zdWIiOiJ0ZXN0" + ".abcDEF123456_-"
_PW = "hunter2TESTISSUED"
_PATHS = ["/home/other-acct/.claude/credentials.json", "/home/claude-fallback/.config/x"]
BENIGN = [
    "2026-10-08T00:00:01Z systemd[1]: pantheon-foo.service: Main process exited, code=exited, status=1/FAILURE",
    "error class: TimeoutError elapsed=30.2s unit_state=failed",
    "quota probe: banner not found in pane capture (3 attempts)",
]


def _fixture() -> str:
    sensitive = [
        f"Authorization: Bearer {_BEARER}",
        f"export ANTHROPIC_API_KEY={_SK}",
        f"git remote https://user:{_GH}@github.com/x/y.git",
        f"telegram {_TG}",
        f"jwt {_JWT}",
        f"password={_PW}",
        f"cat {_PATHS[0]}",
        f"open {_PATHS[1]}",
    ]
    lines = BENIGN[:1] + sensitive[:4] + BENIGN[1:2] + sensitive[4:] + BENIGN[2:]
    return "\n".join(lines) + "\n"


class RealStripTests(unittest.TestCase):  # §6.15
    def test_real_strip(self):
        res = re_.redact_evidence(_fixture())
        self.assertFalse(res.deferred)
        snap = res.snapshot
        for line in BENIGN:
            self.assertIn(line, snap)  # verbatim
        needles = [_SK, _GH, _TG, _BEARER, _JWT, _PW, "other-acct", "claude-fallback",
                   "credentials.json", "hbGciOiJIUzI1"]
        for n in needles:  # byte-substring
            self.assertNotIn(n.encode(), snap.encode(), n)
        for rx in (r"sk-[A-Za-z0-9_-]{12,}", r"ghp_[A-Za-z0-9]{20,}", r"\d{8,10}:[A-Za-z0-9_-]{30,}",
                   r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.", r"/home/(?!<redacted)"):  # regex
            self.assertIsNone(re.search(rx, snap), rx)

    def test_own_account_path_kept(self):
        red = re_.make_default_redactor("claude")
        out = red("a /home/claude/x.log b /home/other/y")
        self.assertIn("/home/claude/x.log", out)
        self.assertNotIn("/home/other", out)

    def test_weak_custom_redactor_still_stripped_of_known_shapes(self):
        res = re_.redact_evidence(f"k={_SK}\nok line\n", redactor=lambda s: s)
        self.assertNotIn(_SK, res.snapshot)
        self.assertIn("ok line", res.snapshot)

    def test_exotic_linebreaks_normalised(self):
        res = re_.redact_evidence("a b\x0bc\rd")
        self.assertEqual(res.snapshot.splitlines(), ["a", "b", "c", "d"])


class ByteCapTests(unittest.TestCase):  # §6.16
    def test_over_cap_deferred_and_no_snapshot(self):
        big = ("x" * 99 + "\n") * 400  # 40_000 bytes
        res = re_.redact_evidence(big)
        self.assertTrue(res.deferred)
        self.assertIsNone(res.snapshot)

    def test_cap_is_utf8_bytes_post_redaction(self):
        # 11_000 chars * 3 bytes = 33_000 bytes > cap although < cap in characters
        res = re_.redact_evidence("€" * 11_000)
        self.assertTrue(res.deferred)
        # redaction shrinks it back under the cap -> measured AFTER redaction
        res2 = re_.redact_evidence("pre\n" + "y" * 40_000, redactor=lambda s: s[:100])
        self.assertFalse(res2.deferred)

    def test_under_cap_snapshot_present(self):
        res = re_.redact_evidence("small\n")
        self.assertFalse(res.deferred)
        self.assertEqual(res.snapshot, "small\n")


class FailClosedTests(unittest.TestCase):  # §6.17
    def test_redactor_throws(self):
        def boom(_):
            raise RuntimeError("mid-emit")
        res = re_.redact_evidence("secret " + _SK, redactor=boom)
        self.assertTrue(res.deferred)
        self.assertIsNone(res.snapshot)

    def test_non_string_return(self):
        res = re_.redact_evidence("x", redactor=lambda s: None)
        self.assertTrue(res.deferred)
        self.assertIsNone(res.snapshot)


A = re_.Candidate("recovery", "claude", "claude-fallback")
B = re_.Candidate("consultant", "codex", "claude-codex")
C = re_.Candidate("orchestrator", "claude", "claude")


class RouteTests(unittest.TestCase):  # §6.6, §6.18
    def setUp(self):
        self.pages = []

    def _route(self, faulted, cands, live, **kw):
        return re_.resolve_route(faulted, cands, liveness=lambda c: c.name in live,
                                 page=lambda s, b: self.pages.append((s, b)), **kw)

    def test_prefers_other_harness(self):
        r = self._route("claude", [A, B, C], {"recovery", "consultant", "orchestrator"})
        self.assertEqual((r.kind, r.target), ("other_harness", B))

    def test_falls_to_any_live(self):
        r = self._route("claude", [A, B, C], {"recovery"})
        self.assertEqual((r.kind, r.target), ("any_live", A))

    def test_pages_when_none_live(self):
        r = self._route("claude", [A, B], set())
        self.assertEqual(r.kind, "page")
        self.assertIsNone(r.target)
        self.assertEqual(len(self.pages), 1)

    def test_deferred_refuses_cross_account_and_reroutes_same_account(self):
        r = self._route("claude", [A, B, C], {"recovery", "consultant", "orchestrator"}, evidence_deferred=True,
                        origin_account="claude-fallback", requested=B)
        self.assertEqual(r.kind, "same_account")
        self.assertEqual(r.target, A)
        self.assertIn("consultant", r.refused_cross_account)
        self.assertEqual(self.pages, [])

    def test_deferred_pages_when_no_same_account_live(self):
        r = self._route("claude", [A, B], {"consultant"}, evidence_deferred=True,
                        origin_account="claude-fallback", requested=B)
        self.assertEqual(r.kind, "page")
        self.assertEqual(len(self.pages), 1)


if __name__ == "__main__":
    unittest.main()
