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


class RepairCandidateSetTests(unittest.TestCase):  # T-5999: Recovery in the generic-detector set (Option A)
    def setUp(self):
        self.pages = []

    def _route(self, faulted, **kw):
        return re_.resolve_route(faulted, re_.REPAIR_CANDIDATES, liveness=lambda c: True,
                                 page=lambda s, b: self.pages.append((s, b)), **kw)

    def test_set_is_watchman_then_recovery(self):
        self.assertEqual(re_.REPAIR_CANDIDATES, (re_.RECOVERY_CANDIDATE, re_.RECOVERY_CANDIDATE))
        self.assertEqual(re_.RECOVERY_CANDIDATE, re_.Candidate("recovery", "claude", "claude-fallback"))
        self.assertEqual(re_.RECOVERY_CANDIDATE, re_.Candidate("watchman", "codex", "codex-fallback"))

    def test_codex_faulted_routes_other_harness_to_recovery(self):  # RFC-004 s5 codex row, s6.6
        r = self._route("codex")
        self.assertEqual((r.kind, r.target), ("other_harness", re_.RECOVERY_CANDIDATE))

    def test_claude_faulted_still_routes_other_harness_to_watchman(self):
        r = self._route("claude")
        self.assertEqual((r.kind, r.target), ("other_harness", re_.RECOVERY_CANDIDATE))
        self.assertEqual(self.pages, [])

    def test_existing_bt2_bt3b_specs_widen_only_the_any_live_fallback(self):
        """T-5998 B(i) (replaces the T-5999 Option-A pin): the three specs keep candidates=(watchman,) and
        gain Recovery ONLY as the any_live fallback; the registry (REPAIR_CANDIDATES) specs get none."""
        for spec in _existing_specs():
            with self.subTest(spec=spec.detector):
                self.assertEqual(spec.candidates, (re_.RECOVERY_CANDIDATE,))
                self.assertEqual(spec.any_live_fallback, (re_.RECOVERY_CANDIDATE,))
                self.assertEqual(spec.harness, "claude")

    def test_fallback_is_any_live_only_never_same_account_or_requested(self):
        pages = []
        kw = dict(page=lambda s, b: pages.append((s, b)), any_live_fallback=(re_.RECOVERY_CANDIDATE,))
        # deferred under codex-fallback (generic): watchman is same-account; the fallback is never used
        r = re_.resolve_route("claude", (re_.RECOVERY_CANDIDATE,), liveness=lambda c: c.name == "watchman",
                              evidence_deferred=True, origin_account="codex-fallback", **kw)
        self.assertEqual((r.kind, r.target.name), ("same_account", "watchman"))
        r = re_.resolve_route("claude", (re_.RECOVERY_CANDIDATE,), liveness=lambda c: c.name == "recovery",
                              evidence_deferred=True, origin_account="codex-fallback", **kw)
        self.assertEqual(r.kind, "page")
        # requested + live wins unchanged; a not-live requested falls to other, then the fallback
        r = re_.resolve_route("claude", (re_.RECOVERY_CANDIDATE,), liveness=lambda c: True,
                              requested=re_.RECOVERY_CANDIDATE, **kw)
        self.assertEqual((r.kind, r.target.name), ("requested", "watchman"))
        r = re_.resolve_route("claude", (re_.RECOVERY_CANDIDATE,), liveness=lambda c: c.name == "recovery",
                              requested=re_.RECOVERY_CANDIDATE, **kw)
        self.assertEqual((r.kind, r.target.name), ("any_live", "recovery"))

    def test_fallback_defaults_empty_registry_specs_unchanged(self):
        r = re_.resolve_route("claude", re_.REPAIR_CANDIDATES, liveness=lambda c: False, page=lambda s, b: None)
        self.assertEqual(r.kind, "page")


def _existing_specs():
    from types import SimpleNamespace
    from tools import org076_meta, org076_sample_freshness, quota_probe_detector
    return [quota_probe_detector.spec_for(SimpleNamespace(persona="specialist", account="claude-fallback")),
            org076_sample_freshness.spec_for("specialist", "claude-fallback"),
            org076_sample_freshness.spec_for("specialist", "claude-fallback", detector="some-detector"),
            *org076_meta.specs("claude-fallback")]


def _boom(_text):
    raise RuntimeError("redactor down")  # a raising redactor => evidence_deferred (fail closed)


_H, _W, _P = ("other_harness", "watchman"), ("any_live", "recovery"), ("page", None)


class RouteTableB1Tests(unittest.TestCase):
    """The T-5998 route table (see repair_evidence's module docstring), through the store's real ``_route``
    for every existing spec. Row key: (evidence, watchman live, subject) -> (BEFORE, AFTER if Recovery live,
    AFTER if Recovery not live). H = other_harness watchman, W = any_live recovery, P = page."""
    H, W, P = _H, _W, _P
    ROWS = {
        ("nd", True, "specialist"): (H, H, H), ("nd", True, "recovery"): (H, H, H), ("nd", True, "watchman"): (P, W, P),
        ("nd", False, "specialist"): (P, W, P), ("nd", False, "recovery"): (P, P, P), ("nd", False, "watchman"): (P, W, P),
        **{("dfr", hl, sub): (_P, _P, _P) for hl in (True, False) for sub in ("specialist", "recovery", "watchman")},
    }

    def _route(self, spec, deferred, watchman_live, recovery_live, subject):
        import dataclasses
        from tools import episode_store as es
        live = {n for n, on in (("watchman", watchman_live), ("recovery", recovery_live)) if on}
        pages = []
        ports = es.Ports(liveness=lambda c: c.name in live, page=lambda s, b: pages.append((s, b)))
        sp = dataclasses.replace(spec, redactor=_boom) if deferred else spec
        ep = {"delivery": {}, "evidence": "ok", "fault_id": f"some-kind:{subject}", "episode_id": "E-1"}
        r = es._route(ep, sp, ports)
        return (r["kind"], None if r["kind"] == "page" else r["target"]), len(pages)

    def test_every_row_before_and_after_for_every_existing_spec(self):
        import dataclasses
        for spec in _existing_specs():
            for (ev, hl, subject), (before, after_wl, after_wnl) in self.ROWS.items():
                for wl, want in ((True, after_wl), (False, after_wnl)):
                    with self.subTest(spec=spec.domain_key, ev=ev, watchman_live=hl, subject=subject, recovery_live=wl):
                        got, npages = self._route(spec, ev == "dfr", hl, wl, subject)
                        self.assertEqual(got, want)
                        self.assertEqual(npages, int(want[0] == "page"))
                        old, _ = self._route(dataclasses.replace(spec, any_live_fallback=()),
                                             ev == "dfr", hl, wl, subject)
                        self.assertEqual(old, before)

    def test_deferred_under_claude_fallback_pages_not_recovery(self):  # B(ii) stays declined
        for spec in _existing_specs():
            self.assertEqual(spec.account, "claude-fallback")
            got, npages = self._route(spec, True, False, True, "specialist")
            self.assertEqual((got, npages), (self.P, 1))

    def test_watchman_not_live_routes_any_live_recovery(self):  # B(i)
        for spec in _existing_specs():
            self.assertEqual(self._route(spec, False, False, True, "specialist")[0], self.W)


class SubjectExclusionTests(unittest.TestCase):  # T-5999 Recovery condition: never route to the episode's subject
    def setUp(self):
        self.pages = []

    def _route(self, faulted, cands, live, **kw):
        return re_.resolve_route(faulted, cands, liveness=lambda c: c.name in live,
                                 page=lambda s, b: self.pages.append((s, b)), **kw)

    def test_subject_of_parses_kind_persona_and_never_raises(self):
        self.assertEqual(re_.subject_of("mail-stale:recovery"), "recovery")
        self.assertEqual(re_.subject_of("sample-freshness-silent:mail-x:specialist"), "specialist")
        for bad in ("cycle-rate-low", ":recovery", "x:", "x:Bad Persona", "x:../etc", None, 7, b"x:recovery", "x:" + "a" * 70):
            with self.subTest(bad=bad):
                self.assertIsNone(re_.subject_of(bad))

    def test_recovery_subject_deferred_on_claude_fallback_pages_not_recovery(self):  # the generic B(ii) case
        r = self._route("codex", re_.REPAIR_CANDIDATES, {"recovery", "watchman"}, evidence_deferred=True,
                        origin_account="claude-fallback", episode_subject=re_.subject_of("x:recovery"))
        self.assertEqual((r.kind, r.target, r.excluded_subject), ("page", None, ("recovery",)))
        self.assertEqual(len(self.pages), 1)
        self.assertIn("excluded_subject=['recovery']", self.pages[0][1])

    def test_recovery_subject_with_watchman_live_routes_watchman(self):
        for faulted in ("codex", "claude"):
            with self.subTest(faulted=faulted):
                r = self._route(faulted, re_.REPAIR_CANDIDATES, {"recovery", "watchman"}, episode_subject="recovery")
                self.assertEqual(r.target, re_.RECOVERY_CANDIDATE)
                self.assertEqual(r.excluded_subject, ("recovery",))

    def test_watchman_subject_never_routes_to_watchman(self):
        for faulted, live, want in (("claude", {"recovery", "watchman"}, re_.RECOVERY_CANDIDATE),
                                    ("codex", {"recovery", "watchman"}, re_.RECOVERY_CANDIDATE),
                                    ("claude", {"watchman"}, None)):
            with self.subTest(faulted=faulted, live=live):
                r = self._route(faulted, re_.REPAIR_CANDIDATES, live, episode_subject="watchman")
                self.assertEqual(r.target, want)
                self.assertNotEqual(getattr(r.target, "name", None), "watchman")
        r = self._route("claude", (re_.RECOVERY_CANDIDATE,), {"watchman"}, episode_subject="watchman",
                        requested=re_.RECOVERY_CANDIDATE)
        self.assertEqual(r.kind, "page", "a requested subject is excluded too")

    def test_no_subject_is_the_old_behaviour(self):
        r = self._route("claude", re_.REPAIR_CANDIDATES, {"recovery", "watchman"}, episode_subject=None)
        self.assertEqual((r.kind, r.target, r.excluded_subject), ("other_harness", re_.RECOVERY_CANDIDATE, ()))

    def test_existing_specs_routing_unchanged_by_the_exclusion(self):
        """The three watchman-only specs: watchman is never their subject, so every route kind is as before."""
        from types import SimpleNamespace
        from tools import org076_meta, org076_sample_freshness, quota_probe_detector
        cases = [(quota_probe_detector.spec_for(SimpleNamespace(persona=p, account="claude")),
                  quota_probe_detector.fault_id(p)) for p in ("recovery", "specialist", "orchestrator")]
        cases += [(org076_sample_freshness.spec_for(p, "claude-fallback"), org076_sample_freshness.fault_silent(p))
                  for p in ("recovery", "specialist")]
        cases += [(s, fid) for s in org076_meta.specs("claude-fallback")
                  for fid in ("cycle-rate-low", "heartbeat-write-failing", "ingest-backlog")]
        for spec, fid in cases:
            for live in ({"watchman"}, set()):
                for deferred in (False, True):
                    with self.subTest(spec=spec.domain_key, fid=fid, live=live, deferred=deferred):
                        kw = dict(evidence_deferred=deferred, origin_account=spec.account)
                        old = self._route(spec.harness, spec.candidates, live, **kw)
                        new = self._route(spec.harness, spec.candidates, live, episode_subject=re_.subject_of(fid), **kw)
                        self.assertEqual((new.kind, new.target, new.refused_cross_account),
                                         (old.kind, old.target, old.refused_cross_account))
                        self.assertEqual(new.excluded_subject, ())
                        self.assertEqual(spec.candidates, (re_.RECOVERY_CANDIDATE,))


if __name__ == "__main__":
    unittest.main()
