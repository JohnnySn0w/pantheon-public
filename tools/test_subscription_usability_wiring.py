"""T-4862: wiring of live usability gating into quota_check, planned_swap, return, status.

Hermetic: tmp state roots, injected fakes. No tmux, codex, claude or network.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import mailbox, persona_bringup, runtime_controller as rc
from tools import subscription_usability as su
from tools.harness_adapter.codex import classify_codex_usage_limit
from tools.test_runtime_controller import (
    REPO_ROOT, _Adapter, _binding, _quota_probe_result, _quota_snapshot,
)
from tools.test_subscription_usability import snap

NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


class QuotaRig:
    def __init__(self, tc: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory(prefix="su-w-")
        tc.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.primary = _binding(self.root, harness="claude", profile="primary", fallbacks=("codex/a", "codex/b"))
        self.a = _binding(self.root, harness="codex", profile="a")
        self.b = _binding(self.root, harness="codex", profile="b")
        mailbox.provision(self.primary.mailbox)
        self.results: dict[str, object] = {}
        self.calls: list[str] = []

    def deps(self):
        def codex(binding):
            self.calls.append(su.runtime_ref(binding))
            value = self.results[su.runtime_ref(binding)]
            if isinstance(value, BaseException):
                raise value
            return value

        return su.ProbeDeps(codex_snapshot=codex)

    def check(self, now=NOW):
        probe = _quota_probe_result(self.primary, self.state)
        primary_snapshot = _quota_snapshot(
            self.primary, self.root,
            evidence={"authoritative_limit": "false", "provider": "claude", "path": probe.statusline_path,
                      "sha256": str(probe.statusline_sha256), "session_id": probe.session_id},
        )
        return persona_bringup.quota_check(
            self.primary, adapter=_Adapter(self.primary, primary_snapshot),
            fallback_bindings=(self.a, self.b), primary_binding=self.primary,
            state_dir=self.state, cwd=self.root, now=now,
            claude_probe_runner=lambda **_: probe, manifest_path=REPO_ROOT / "instance.yaml",
            probe_deps=self.deps(),
        )


class QuotaCheckGatingTests(unittest.TestCase):
    def test_all_capped_no_swap_primary_untouched_alert_once(self):
        r = QuotaRig(self)
        r.results.update({"codex/a": snap(r.a, remaining=0.0), "codex/b": snap(r.b, authoritative=True)})
        before = rc.transition_state("recovery", state_root=r.state)
        with mock.patch.object(rc, "begin_planned_transition") as begin, \
                mock.patch.object(rc, "_route_failure_alert") as alert:
            result = r.check()
            r.check(now=NOW + timedelta(minutes=1))
        begin.assert_not_called()
        self.assertEqual(result.decision, "no-viable-target")
        self.assertFalse(result.acted)
        self.assertEqual(alert.call_count, 1)
        self.assertEqual(rc.transition_state("recovery", state_root=r.state), before)
        self.assertFalse(rc._transition_path("recovery", r.state).exists())
        self.assertIn("planned_quota_no_viable_target", rc._evidence_log_path("recovery", r.state).read_text())

    def test_first_capped_second_usable_selected(self):
        r = QuotaRig(self)
        r.results.update({"codex/a": snap(r.a, remaining=0.0), "codex/b": snap(r.b)})
        result = r.check()
        self.assertEqual(result.decision, "planned-transition-started")
        self.assertEqual(rc.transition_state("recovery", state_root=r.state)["incoming_profile"], "b")

    def test_unknown_probe_does_not_swap(self):
        r = QuotaRig(self)
        r.results.update({"codex/a": TimeoutError("t"), "codex/b": None})
        with mock.patch.object(rc, "_route_failure_alert"):
            result = r.check()
        self.assertEqual(result.decision, "no-viable-target")
        self.assertFalse(rc._transition_path("recovery", r.state).exists())

    def test_near_cap_only_candidate_is_selected(self):
        r = QuotaRig(self)
        r.results.update({"codex/a": snap(r.a, remaining=0.0), "codex/b": snap(r.b, remaining=2.0)})
        self.assertEqual(r.check().decision, "planned-transition-started")


class PreReplaceGateTests(unittest.TestCase):
    def _setup(self):
        tmp = tempfile.TemporaryDirectory(prefix="su-g-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cur_b = _binding(root, harness="claude", profile="primary")
        target = _binding(root, harness="codex", profile="fallback")
        handoff = root / "HANDOFF.md"
        handoff.write_text("# old\n", encoding="utf-8")
        current = rc.RuntimeHandle(cur_b.launch, cur_b.controller.tmux, "s1", rc._utc_now())
        return root, target, handoff, current

    def _drive(self, root, target, handoff, current, gate, trigger="planned-quota"):
        with mock.patch.object(rc.chronicle, "archive_required", return_value=root / "a.md"), \
                mock.patch.object(rc, "_replace_runtime") as replace, \
                mock.patch.object(rc, "_prompt_successor"), mock.patch.object(rc, "submit_to_runtime"), \
                mock.patch.object(rc, "_route_failure_alert") as alert:
            kwargs = dict(message_id="m1", drain_id="d1", handoff_path=handoff, state_root=root,
                          pre_replace_gate=gate, trigger=trigger)
            with self.assertRaises(rc.RuntimeTransitionPending):
                rc.planned_swap(current, target, **kwargs)
            handoff.write_text("# fresh handoff\n", encoding="utf-8")
            try:
                rc.planned_swap(current, target, **kwargs)
            except rc.RuntimeTransitionPending as exc:
                return replace, alert, exc
        return replace, alert, None

    def test_failed_gate_abandons_without_replace_and_alerts(self):
        root, target, handoff, current = self._setup()
        calls = []

        def gate(binding):
            calls.append(binding.launch.profile)
            return False, {"codex/fallback": "capped"}

        replace, alert, exc = self._drive(root, target, handoff, current, gate)
        replace.assert_not_called()
        self.assertEqual(calls, ["fallback"])
        self.assertIsInstance(exc, rc.RuntimeTransitionAbandoned)
        state = rc.transition_state("recovery", state_root=root)
        self.assertEqual(state["phase"], "abandoned")
        self.assertFalse(state.get("runtime_swapped"))
        self.assertEqual(alert.call_count, 1)
        text = rc._evidence_log_path("recovery", root).read_text()
        self.assertIn("planned_swap_abandoned", text)
        stamp = rc._persona_state("recovery", root) / su.ABANDON_ALERT_STAMP
        self.assertTrue(stamp.exists())

    def test_passing_gate_replaces(self):
        root, target, handoff, current = self._setup()
        replace, _alert, _exc = self._drive(root, target, handoff, current, lambda b: (True, {}))
        replace.assert_called_once()

    def test_abandoned_phase_is_inactive_for_every_reader(self):
        root, target, handoff, current = self._setup()
        self._drive(root, target, handoff, current, lambda b: (False, {}))
        # a fresh planned transition may begin over an abandoned one
        res = rc.begin_planned_transition(
            "recovery", outgoing_harness="claude", outgoing_profile="primary", incoming_harness="codex",
            incoming_profile="fallback", message_id=None, primary_resets_at=None, quota_evidence=None,
            state_root=root,
        )
        self.assertEqual(res.status, "started")
        # drain guard treats it as no-transition (re-arm leaves guard)
        rc._atomic_json(rc._transition_path("recovery", root), {"phase": "abandoned"})
        rc.assert_drain_allowed("recovery", "d2", state_root=root)
        primary = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",))
        self.assertIsNone(persona_bringup.primary_recovery_evidence(primary, state_dir=root))
        self.assertIs(
            persona_bringup.select_recorded_runtime_binding(REPO_ROOT / "instance.yaml", primary, state_dir=root),
            primary,
        )

    def test_quota_reset_abandon_restores_fallback_record_and_return_path(self):
        tmp = tempfile.TemporaryDirectory(prefix="su-b1-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        primary = _binding(root, harness="codex", profile="primary", fallbacks=("claude/fallback",))
        fallback = _binding(root, harness="claude", profile="fallback")
        handoff = root / "HANDOFF.md"
        handoff.write_text("# old\n", encoding="utf-8")
        completed = {
            "schema": "pantheon-runtime-transition/v1", "transition_id": "t0", "phase": "completed",
            "fallback_active": True, "runtime_swapped": True, "trigger": "planned-quota",
            "outgoing_harness": "codex", "outgoing_profile": "primary",
            "incoming_harness": "claude", "incoming_profile": "fallback",
            "primary_resets_at": (NOW + timedelta(days=3)).isoformat().replace("+00:00", "Z"),
        }
        rc._atomic_json(rc._transition_path("recovery", root), completed)
        current = rc.RuntimeHandle(fallback.launch, fallback.controller.tmux, "s1", rc._utc_now())
        replace, _alert, exc = self._drive(root, primary, handoff, current, lambda b: (False, {"x": "unknown"}),
                                           trigger="quota-reset")
        replace.assert_not_called()
        self.assertIsInstance(exc, rc.RuntimeTransitionAbandoned)
        state = rc.transition_state("recovery", state_root=root)
        for key in ("phase", "fallback_active", "trigger", "primary_resets_at", "incoming_harness",
                    "incoming_profile", "outgoing_harness", "outgoing_profile", "transition_id"):
            self.assertEqual(state[key], completed[key], key)
        self.assertEqual(state["last_abandon_reason"], "pre-replace-usability-gate")
        self.assertNotIn("prior_fallback_record", state)
        # the seat is still recognisably on the fallback
        with mock.patch.object(persona_bringup, "load_runtime_reference", return_value=fallback):
            self.assertIs(persona_bringup.select_recorded_runtime_binding(
                REPO_ROOT / "instance.yaml", primary, state_dir=root), fallback)
        # return path reachable again: evidence once the (early-reset) probe is usable
        deps = su.ProbeDeps(codex_snapshot=lambda b: snap(b, remaining=80.0))
        ev = persona_bringup.primary_recovery_evidence(primary, state_dir=root, now=NOW, probe_deps=deps)
        self.assertIsNotNone(ev)
        # and quota_check still reports fallback-active-noop
        cl = _binding(root, harness="claude", profile="primary", fallbacks=("codex/fallback",))
        res = persona_bringup.quota_check(
            cl, adapter=_Adapter(cl), fallback_bindings=(), primary_binding=cl, state_dir=root, cwd=root,
            now=NOW, manifest_path=REPO_ROOT / "instance.yaml")
        self.assertEqual(res.decision, "fallback-active-noop")


class ReturnTests(unittest.TestCase):
    def _rig(self, resets_at):
        tmp = tempfile.TemporaryDirectory(prefix="su-r-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        primary = _binding(root, harness="codex", profile="primary", fallbacks=("claude/fallback",))
        rc._atomic_json(rc._transition_path("recovery", root), {
            "schema": "pantheon-runtime-transition/v1", "phase": "completed", "fallback_active": True,
            "trigger": "forced-cap", "primary_resets_at": resets_at.isoformat().replace("+00:00", "Z"),
        })
        return root, primary

    def test_early_reset_probe_usable_returns_before_announced_reset(self):
        root, primary = self._rig(NOW + timedelta(days=3))
        deps = su.ProbeDeps(codex_snapshot=lambda b: snap(b, remaining=80.0))
        ev = persona_bringup.primary_recovery_evidence(primary, state_dir=root, now=NOW, probe_deps=deps)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["usability_verdict"], "usable")

    def test_reset_passed_but_probe_capped_no_return_and_reprobe_set(self):
        root, primary = self._rig(NOW - timedelta(hours=1))
        deps = su.ProbeDeps(codex_snapshot=lambda b: snap(b, remaining=0.0))
        self.assertIsNone(persona_bringup.primary_recovery_evidence(primary, state_dir=root, now=NOW, probe_deps=deps))
        saved = su.load_usability("recovery", primary, state_root=root)
        self.assertEqual(saved.verdict, "capped")
        self.assertIsNotNone(saved.reprobe_at)

    def test_near_cap_primary_does_not_return(self):
        root, primary = self._rig(NOW - timedelta(hours=1))
        deps = su.ProbeDeps(codex_snapshot=lambda b: snap(b, remaining=2.0))
        self.assertIsNone(persona_bringup.primary_recovery_evidence(primary, state_dir=root, now=NOW, probe_deps=deps))


class CodexBannerTests(unittest.TestCase):
    BANNER = "You've hit your usage limit. Upgrade to Pro or try again at Oct 14th 4:13 AM."

    def test_parses_ordinals_and_no_year(self):
        now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        hit = classify_codex_usage_limit(self.BANNER, now=now, tz=timezone.utc)
        self.assertEqual(hit.reset_at, datetime(2026, 10, 14, 4, 13, tzinfo=timezone.utc))
        self.assertEqual(hit.reset_source, "parsed")

    def test_ordinals_and_year_variants(self):
        now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        for text, want in (
            ("You've hit your usage limit. try again at Oct 22nd 4:13 AM", datetime(2026, 10, 22, 4, 13)),
            ("You've hit your usage limit. try again at Oct 23rd, 2027 4:13 PM", datetime(2027, 10, 23, 16, 13)),
            ("You've hit your usage limit. try again at Nov 3rd 12:00 AM", datetime(2026, 11, 3, 0, 0)),
            ("You've hit your usage limit. try again at Nov 21st 12:30 PM", datetime(2026, 11, 21, 12, 30)),
            ("You've hit your usage limit. try again at Nov 21 11:59 PM", datetime(2026, 11, 21, 23, 59)),
        ):
            hit = classify_codex_usage_limit(text, now=now, tz=timezone.utc)
            self.assertEqual(hit.reset_at, want.replace(tzinfo=timezone.utc), text)

    def test_year_rollover_when_date_already_passed(self):
        now = datetime(2026, 12, 30, 12, 0, tzinfo=timezone.utc)
        hit = classify_codex_usage_limit("You've hit your usage limit. try again at Jan 2nd 4:13 AM", now=now, tz=timezone.utc)
        self.assertEqual(hit.reset_at.year, 2027)

    def test_recently_passed_reset_keeps_this_year(self):
        # T-5801: a banner still shown days after its reset is past, not a year away.
        now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        hit = classify_codex_usage_limit("You've hit your usage limit. try again at Nov 4th 8:02 AM", now=now, tz=timezone.utc)
        self.assertEqual(hit.reset_at, datetime(2026, 11, 4, 8, 2, tzinfo=timezone.utc))

    def test_dec_reset_shown_in_jan_rolls_back(self):
        now = datetime(2027, 1, 2, 12, 0, tzinfo=timezone.utc)
        hit = classify_codex_usage_limit("You've hit your usage limit. try again at Dec 31st 4:13 AM", now=now, tz=timezone.utc)
        self.assertEqual(hit.reset_at, datetime(2026, 12, 31, 4, 13, tzinfo=timezone.utc))

    def test_banner_without_parseable_time_still_classifies_as_cap(self):
        hit = classify_codex_usage_limit("You've hit your usage limit.", now=NOW)
        self.assertIsNotNone(hit)
        self.assertIsNone(hit.reset_at)

    def test_garbage_and_non_match_return_none(self):
        for text in ("", "hello", "try again at Oct 14th 4:13 AM"):
            self.assertIsNone(classify_codex_usage_limit(text, now=NOW))
        hit = classify_codex_usage_limit("You've hit your usage limit. try again at Zzz 99th 99:99 AM", now=NOW)
        self.assertIsNotNone(hit)
        self.assertIsNone(hit.reset_at)  # garbage time is dropped, never raised

    def test_quoted_prose_far_from_bottom_is_not_a_cap(self):
        text = self.BANNER + "\n" + "\n".join(f"line {i}" for i in range(40))
        self.assertIsNone(classify_codex_usage_limit(text, now=NOW))

    def test_post_start_banner_is_subscription_cap(self):
        from tools.harness_adapter.codex import CodexHarnessAdapter  # noqa: F401
        from tools.harness_adapter.types import PostStartPaneSample

        r = SimpleNamespace()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        binding = _binding(Path(tmp.name), harness="codex", profile="a")
        adapter = persona_bringup.build_adapter(binding, harness_home=Path(tmp.name))
        import hashlib

        def sample(i, text):
            return PostStartPaneSample(i, hashlib.sha256(text.encode()).hexdigest(), None, text)

        base = sample(0, "> ")
        cap = sample(1, "x\n" + self.BANNER + "\n> ")
        out = adapter.assess_post_start_readiness(
            harness="codex", profile="a", baseline=base, samples=[cap],
            now=datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(out.status, "subscription-cap")
        self.assertEqual(out.cap_kind, "usage-limit")
        del r


class MidSessionCodexTests(unittest.TestCase):
    BANNER = CodexBannerTests.BANNER

    def test_entry_recorded_once_across_repeated_polls(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        binding = _binding(root, harness="codex", profile="a")
        pane = "work\n" + self.BANNER + "\n> "
        now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        confirmed = []
        for i in range(5):
            obs = rc._observe_and_publish_mid_session_cap(
                binding, capture_pane_fn=lambda: pane, now=now + timedelta(seconds=60 * i), state_root=root,
            )
            confirmed.append(obs.modal_present)
        self.assertEqual(confirmed, [False, True, True, True, True])  # 2-sample debounce
        att = rc.load_last_attempt("recovery", "codex--a", state_root=root)
        self.assertEqual(att["failure_class"], "subscription_cap")
        self.assertEqual(att["consecutive_failures"], 1)
        self.assertIsNotNone(att["cooldown_until"])

    def test_claude_entry_also_recorded_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        binding = _binding(root, harness="claude", profile="primary")
        modal = SimpleNamespace(modal_kind="weekly-limit", reset_at=None, reset_source=None,
                                render_span_sha256="h1")
        now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(rc, "classify_claude_mid_session_cap", return_value=modal):
            for i in range(4):
                rc._observe_and_publish_mid_session_cap(
                    binding, capture_pane_fn=lambda: "x", now=now + timedelta(seconds=60 * i), state_root=root,
                )
        att = rc.load_last_attempt("recovery", "claude--primary", state_root=root)
        self.assertEqual(att["consecutive_failures"], 1)


class ActiveRuntimeCacheAndStatusTests(unittest.TestCase):
    def test_replace_runtime_writes_active_runtime_cache(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        target = _binding(root, harness="codex", profile="a")
        from tools.test_runtime_controller import _replace_and_record  # noqa: F401

        with mock.patch.object(rc, "_start_runtime", return_value=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)), \
                mock.patch.object(rc, "_verify_started_runtime"), mock.patch.object(rc, "_verify_runtime_input_ready"), \
                mock.patch.object(rc, "_target_known", return_value=True), \
                mock.patch.object(rc, "_controller_source_provenance", return_value=None), \
                mock.patch.object(rc, "_runtime_payload", return_value={"x": 1}), \
                mock.patch.object(rc, "_pane_process_pid", return_value=4242), \
                mock.patch("tools.persona_bringup.build_adapter", return_value=SimpleNamespace()):
            rc._replace_runtime(target, state_root=root)
        cache = rc.read_active_runtime("recovery", state_root=root)
        self.assertEqual(cache["runtime"], "codex/a")
        self.assertEqual(cache["pid"], 4242)

    def test_status_mismatch_and_degrade(self):
        from tools import harness as H

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cl = _binding(root, harness="claude", profile="primary", fallbacks=("codex/a",))
        cx = _binding(root, harness="codex", profile="a")
        rc._atomic_json(rc._active_runtime_path("recovery", state_root=root / "personas"),
                        {"runtime": "claude/primary", "tmux_session": "s", "pid": 1})
        args = SimpleNamespace(persona="recovery")

        def run(**kw):
            buf = io.StringIO()
            with redirect_stdout(buf):
                H._do_status(args, root=root, bindings=[cl, cx], **kw)
            return buf.getvalue()

        out = run(pane_probe=lambda bindings: ("codex/a", 77))
        self.assertIn("live pane: codex/a", out)
        self.assertIn("MISMATCH", out)
        ok = run(pane_probe=lambda bindings: ("claude/primary", 77))
        self.assertNotIn("MISMATCH", ok)

        def boom(bindings):
            raise PermissionError("no /proc")

        bad = run(pane_probe=boom)
        self.assertIn("live pane: unavailable", bad)

    def test_status_prints_usability_verdict(self):
        from tools import harness as H

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cx = _binding(root, harness="codex", profile="a")
        su.write_usability("recovery", cx, su.UsabilityProbe("codex/a", "capped", "codex-app-server", NOW,
                                                         NOW + timedelta(minutes=30), None, {}), state_root=root / "personas")
        buf = io.StringIO()
        with redirect_stdout(buf):
            H._do_status(SimpleNamespace(persona="recovery"), root=root, bindings=None,
                         pane_probe=lambda b: None)
        self.assertIn("usability", buf.getvalue())
        self.assertIn("capped", buf.getvalue())


class SelectViableProbeTests(unittest.TestCase):
    def _rig(self, failure_class="subscription_cap"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        a = _binding(root, harness="codex", profile="a")
        rc.record_last_attempt("recovery", a, outcome="failed", failure_class=failure_class,
                               state_root=root, now=NOW - timedelta(hours=1),
                               cooldown_until_override=NOW + timedelta(days=2))
        return root, a

    def _probe(self, verdict, calls):
        def fn(binding, hint):
            calls.append(hint)
            return su.UsabilityProbe(su.runtime_ref(binding), verdict, "t", NOW, NOW, None, {})
        return fn

    def test_default_none_keeps_cooldown_skip(self):
        root, a = self._rig()
        sel, skipped = rc._select_viable_runtime("recovery", [a], {}, NOW, state_root=root)
        self.assertIsNone(sel)
        self.assertIn("cooldown-active", skipped[0]["reason"])

    def test_usable_probe_supersedes_cap_cooldown(self):
        root, a = self._rig()
        calls = []
        sel, _ = rc._select_viable_runtime("recovery", [a], {}, NOW, state_root=root,
                                           usability_probe=self._probe("usable", calls))
        self.assertIs(sel, a)
        self.assertEqual(len(calls), 1)
        att = rc.load_last_attempt("recovery", "codex--a", state_root=root)
        self.assertIsNone(att["cooldown_until"])
        self.assertEqual(att["provenance"]["trigger"], "early-reset-probe")

    def test_capped_or_raising_probe_keeps_cooldown(self):
        root, a = self._rig()
        sel, _ = rc._select_viable_runtime("recovery", [a], {}, NOW, state_root=root,
                                           usability_probe=self._probe("capped", []))
        self.assertIsNone(sel)

        def boom(binding, hint):
            raise RuntimeError("x")

        sel, _ = rc._select_viable_runtime("recovery", [a], {}, NOW, state_root=root, usability_probe=boom)
        self.assertIsNone(sel)

    def test_non_cap_cooldown_is_never_probed(self):
        root, a = self._rig(failure_class="process_death")
        calls = []
        sel, _ = rc._select_viable_runtime("recovery", [a], {}, NOW, state_root=root,
                                           usability_probe=self._probe("usable", calls))
        self.assertIsNone(sel)
        self.assertEqual(calls, [])

    def test_no_cooldown_candidate_is_not_probed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        a = _binding(Path(tmp.name), harness="codex", profile="a")
        calls = []
        sel, _ = rc._select_viable_runtime("recovery", [a], {}, NOW, state_root=Path(tmp.name),
                                           usability_probe=self._probe("capped", calls))
        self.assertIs(sel, a)
        self.assertEqual(calls, [])


class MidSessionEligibilityR1Tests(unittest.TestCase):
    def _go(self, verdict):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cur = _binding(root, harness="claude", profile="primary", fallbacks=("codex/a",),
                       mid_session_cap_swap=True)
        a = _binding(root, harness="codex", profile="a")

        def pr(binding):
            return su.UsabilityProbe(su.runtime_ref(binding), verdict, "t", NOW,
                                     NOW + timedelta(minutes=30), None, {})

        with mock.patch.object(su, "probe_usability", side_effect=lambda b, **kw: pr(b)):
            return rc._mid_session_cap_swap_eligible(
                cur, candidates=[cur, a], holds={}, now=NOW, state_root=root,
                usability_probe=lambda b, h: pr(b),
            )

    def test_capped_fallback_not_eligible(self):
        self.assertFalse(self._go("capped"))

    def test_unknown_fallback_attempted_when_primary_confirmed_capped(self):
        self.assertTrue(self._go("unknown"))

    def test_usable_fallback_eligible(self):
        self.assertTrue(self._go("usable"))



class SwapPathsWriteActiveRuntimeCacheTests(unittest.TestCase):
    """The cache is read by `harness status` and Recovery's credential-expiry monitor (T-4791)."""

    FIELDS = ("runtime", "tmux_session", "pid")

    def _patches(self):
        return [
            mock.patch.object(rc, "_start_runtime", return_value=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)),
            mock.patch.object(rc, "_verify_started_runtime"),
            mock.patch.object(rc, "_verify_runtime_input_ready"),
            mock.patch.object(rc, "_target_known", return_value=True),
            mock.patch.object(rc, "_controller_source_provenance", return_value=None),
            mock.patch.object(rc, "_runtime_payload", return_value={"x": 1}),
            mock.patch.object(rc, "_pane_process_pid", return_value=5151),
            mock.patch.object(rc, "_run_integration_handoff"),
            mock.patch.object(rc, "_prompt_successor"),
            mock.patch.object(rc, "submit_to_runtime"),
            mock.patch("tools.persona_bringup.build_adapter", return_value=SimpleNamespace()),
        ]

    def _assert_cache(self, root, target):
        cache = rc.read_active_runtime("recovery", state_root=root)
        self.assertEqual(cache["schema"], "pantheon-persona-active-runtime/v1")
        self.assertEqual(cache["runtime"], "codex/fallback")
        self.assertEqual(cache["tmux_session"], target.controller.tmux.session)
        self.assertEqual(cache["pid"], 5151)

    def _enter(self):
        import contextlib

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for p in self._patches():
            stack.enter_context(p)

    def test_planned_swap_receiver_handoff_path_writes_cache(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cur_b = _binding(root, harness="claude", profile="primary")
        target = _binding(root, harness="codex", profile="fallback")
        handoff = root / "HANDOFF.md"
        handoff.write_text("# old\n", encoding="utf-8")
        current = rc.RuntimeHandle(cur_b.launch, cur_b.controller.tmux, "s1", rc._utc_now())
        self._enter()
        (root / "a.md").write_text("archived", encoding="utf-8")
        with mock.patch.object(rc.chronicle, "archive_required", return_value=root / "a.md"):
            kwargs = dict(message_id="m1", drain_id="d1", handoff_path=handoff, state_root=root)
            with self.assertRaises(rc.RuntimeTransitionPending):
                rc.planned_swap(current, target, **kwargs)
            handoff.write_text("# fresh\n", encoding="utf-8")
            with self.assertRaises(rc.RuntimeTransitionPending):
                rc.planned_swap(current, target, **kwargs)
        self.assertEqual(rc.transition_state("recovery", state_root=root)["phase"], "awaiting-successor-read")
        self._assert_cache(root, target)

    def test_forced_swap_writes_cache(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cur_b = _binding(root, harness="claude", profile="primary")
        target = _binding(root, harness="codex", profile="fallback")
        current = rc.RuntimeHandle(cur_b.launch, cur_b.controller.tmux, "s1", rc._utc_now())
        evidence = root / "m1.md"
        evidence.write_text("facts", encoding="utf-8")
        error = rc.ProviderQuotaExhausted("weekly cap", profile="primary", provider_error="cap")
        self._enter()
        with mock.patch.object(rc, "_existing_chronicle", return_value=evidence):
            with self.assertRaises(rc.RuntimeTransitionPending):
                rc.forced_swap(current, target, error=error, message_id="m1",
                               correlation_id="c1", drain_id="d1", state_root=root)
        self._assert_cache(root, target)


class WakeWrapperSignatureTests(unittest.TestCase):
    """The public wake_persistent_runtime wrapper must accept every keyword its
    locked worker accepts. #894 added gate_on_usability/probe_deps to the worker
    only; main() passes gate_on_usability=True to the wrapper, so every
    persona-consume fire raised TypeError fleet-wide (2026-10-12 21:55Z)."""

    def test_wrapper_accepts_every_worker_keyword(self):
        import inspect
        wrapper = inspect.signature(persona_bringup.wake_persistent_runtime).parameters
        worker = inspect.signature(persona_bringup._wake_persistent_runtime_locked).parameters
        missing = [n for n, p in worker.items()
                   if p.kind is inspect.Parameter.KEYWORD_ONLY and n not in wrapper]
        self.assertEqual(missing, [])

    def test_wrapper_forwards_gate_and_probe_deps(self):
        deps = object()
        with mock.patch.object(persona_bringup.runtime_controller, "persona_operation_lock") as lock, \
                mock.patch.object(persona_bringup, "_wake_persistent_runtime_locked", return_value={"ok": 1}) as worker:
            lock.return_value.__enter__ = lambda s: None
            lock.return_value.__exit__ = lambda s, *a: False
            out = persona_bringup.wake_persistent_runtime(
                SimpleNamespace(persona="recovery"), adapter=None, state_dir=Path("/x"), cwd=Path("/y"),
                gate_on_usability=True, probe_deps=deps)
        self.assertEqual(out, {"ok": 1})
        self.assertIs(worker.call_args.kwargs["gate_on_usability"], True)
        self.assertIs(worker.call_args.kwargs["probe_deps"], deps)


if __name__ == "__main__":
    unittest.main()
