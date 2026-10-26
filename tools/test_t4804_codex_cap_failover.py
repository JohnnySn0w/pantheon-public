"""T-4804 / T-4879: codex usage-limit auto-failover, auto-return, swap notice, ranking.

Hermetic: tmp state roots, injected clock/terminate/notifier fakes. No live tmux,
Telegram, ~/.local/state or /var/lib.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools import runtime_controller as rc
from tools import subscription_usability as su
from tools.harness_adapter.codex import classify_codex_usage_limit
from tools.test_runtime_controller import _stateless_binding

BANNER = "You've hit your usage limit. Upgrade to Pro or try again at Oct 14th 4:13 AM."
NO_TS_BANNER = "You've hit your usage limit."
T0 = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)


def _clock(start: datetime):
    state = {"t": start}
    return (lambda: state["t"]), (lambda s: state.__setitem__("t", state["t"] + timedelta(seconds=s)))


class CodexCapSwapTests(unittest.TestCase):
    """Clause (1): a codex-capped seat swaps to claude with a cooldown until the reset."""

    def _run(self, banner: str):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(
                root, harness="codex", profile="interactive-chatgpt", mid_session_cap_swap=True,
            )
            now, sleep = _clock(T0)
            dead = {"v": False}
            ticks = {"n": 0}

            def alive() -> bool:
                ticks["n"] += 1
                if ticks["n"] > 40:
                    raise AssertionError("loop never swapped")
                return not dead["v"]

            def terminate(_b, **_k) -> None:
                dead["v"] = True

            outcome = rc.block_until_tmux_death(
                codex, alive_check=alive, sleep=sleep, poll_interval_seconds=60.0,
                capture_pane_fn=lambda: "work\n" + banner + "\n> ",
                cap_check_interval_seconds=60.0, now=now, state_root=root,
                candidates=[claude, codex], holds={}, terminate_runtime_fn=terminate,
            )
            return outcome, ticks["n"]

    def test_parsed_reset_becomes_the_cooldown(self) -> None:
        outcome, _ = self._run(BANNER)
        expected = classify_codex_usage_limit(BANNER, now=T0)
        self.assertIsInstance(outcome, rc.MidSessionCapSwapOutcome)
        self.assertEqual(outcome.cooldown_until, expected.reset_at)
        self.assertEqual(outcome.cooldown_source, expected.reset_source)

    def test_unparseable_reset_uses_the_existing_conservative_default(self) -> None:
        outcome, _ = self._run(NO_TS_BANNER)
        self.assertIsInstance(outcome, rc.MidSessionCapSwapOutcome)
        self.assertEqual(outcome.cooldown_source, "conservative-default")
        elapsed = (outcome.cooldown_until - T0).total_seconds()
        self.assertGreaterEqual(elapsed, rc.MID_SESSION_CAP_CONSERVATIVE_DEFAULT_SECONDS)
        self.assertLess(elapsed, rc.MID_SESSION_CAP_CONSERVATIVE_DEFAULT_SECONDS + 600)


class ReturnAfterResetTests(unittest.TestCase):
    """Clause (2): after the reset the seat reselects / fails back to codex."""

    def _rig(self, root: Path):
        codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
        claude = _stateless_binding(
            root, harness="claude", profile="anthropic-code", fallback_return=True,
        )
        reset = T0 + timedelta(hours=6)
        rc.record_last_attempt(
            "recovery", codex, outcome="failed", failure_class="subscription_cap",
            trigger="test", state_root=root, now=T0, cooldown_until_override=reset,
        )
        return codex, claude, reset

    def test_reselects_codex_only_after_the_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex, claude, reset = self._rig(root)
            before, _ = rc._select_viable_runtime(
                "recovery", [codex, claude], {}, reset - timedelta(minutes=1), state_root=root)
            after, _ = rc._select_viable_runtime(
                "recovery", [codex, claude], {}, reset + timedelta(minutes=1), state_root=root)
            self.assertIs(before, claude)
            self.assertIs(after, codex)

    def test_fallback_return_targets_codex_only_after_the_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex, claude, reset = self._rig(root)
            kw = dict(candidates=[codex, claude], holds={}, state_root=root)
            self.assertIsNone(rc._fallback_return_eligible(
                claude, now=reset - timedelta(minutes=1), **kw))
            self.assertIs(rc._fallback_return_eligible(
                claude, now=reset + timedelta(minutes=1), **kw), codex)


class SwapNoticeTests(unittest.TestCase):
    """Clause (3): exactly one notice per swap outcome, never per poll, never raising."""

    def _start(self, binding):
        return (rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                rc.RuntimeHandle(binding.launch, binding.controller.tmux, None, rc._utc_now()))

    def _main(self, root: Path, outcome, *, alert=None):
        codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
        claude = _stateless_binding(root, harness="claude", profile="anthropic-code")
        alert = alert or mock.Mock()
        with mock.patch.object(rc, "_route_failure_alert", alert):
            result = rc.controller_main(
                "recovery", candidates=[codex, claude], state_root=root, now=lambda: T0,
                sleep=lambda s: None, start_runtime_fn=self._start,
                block_until_death_fn=lambda b: outcome,
            )
        return result, alert

    def test_cap_swap_notifies_once_with_runtime_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reset = T0 + timedelta(hours=6)
            result, alert = self._main(Path(tmp), rc.MidSessionCapSwapOutcome(
                modal_kind="usage-limit", cooldown_until=reset, cooldown_source="parsed"))
            self.assertEqual(result, 0)
            self.assertEqual(alert.call_count, 1)
            agent, msg, _vocab = alert.call_args.args
            self.assertEqual(agent, "recovery")
            self.assertIn("codex/interactive-chatgpt", msg)
            self.assertIn(rc._iso(reset), msg)

    def test_fallback_return_notifies_once_with_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _result, alert = self._main(
                Path(tmp), rc.FallbackReturnOutcome(target_ref="claude/anthropic-code"))
            self.assertEqual(alert.call_count, 1)
            self.assertIn("claude/anthropic-code", alert.call_args.args[1])

    def test_ordinary_death_does_not_notify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _result, alert = self._main(Path(tmp), None)
            alert.assert_not_called()

    def test_failing_notifier_never_breaks_the_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, alert = self._main(
                Path(tmp),
                rc.MidSessionCapSwapOutcome(
                    modal_kind="usage-limit", cooldown_until=T0 + timedelta(hours=1),
                    cooldown_source="parsed"),
                alert=mock.Mock(side_effect=RuntimeError("telegram down")),
            )
            self.assertEqual(result, 0)
            self.assertEqual(alert.call_count, 1)


    def test_start_up_cap_notifies_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")

            def start(binding):
                raise rc.RuntimeOperationalFailure(
                    "capped", failure_class="subscription-cap", cause="cap",
                    binding_runtime=rc._binding_runtime_ref(binding))

            alert = mock.Mock()
            with mock.patch.object(rc, "_route_failure_alert", alert):
                result = rc.controller_main(
                    "recovery", candidates=[codex, claude], state_root=root, now=lambda: T0,
                    sleep=lambda s: None, start_runtime_fn=start,
                    block_until_death_fn=lambda b: None)
            self.assertEqual(result, 1)
            self.assertEqual(alert.call_count, 1)
            self.assertIn("start-up", alert.call_args.args[1])

    def test_notice_runs_after_state_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            order: list[str] = []
            real_clear = rc.clear_mid_session_cap_state

            def clear(*a, **k):
                order.append("clear")
                return real_clear(*a, **k)

            alert = mock.Mock(side_effect=lambda *a: order.append("notify"))
            with mock.patch.object(rc, "clear_mid_session_cap_state", clear):
                self._main(Path(tmp), rc.MidSessionCapSwapOutcome(
                    modal_kind="usage-limit", cooldown_until=T0 + timedelta(hours=1),
                    cooldown_source="parsed"), alert=alert)
            self.assertEqual(order, ["clear", "notify"])

    def test_hung_notifier_is_bounded(self) -> None:
        import threading
        gate = threading.Event()
        self.addCleanup(gate.set)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(rc, "_route_failure_alert", lambda *a: gate.wait(30)):
                started = time.monotonic()
                rc._notify_runtime_swap("mason", "codex/x", "cap-swap", "k", "m",
                                        state_root=Path(tmp), timeout_seconds=0.2)
                self.assertLess(time.monotonic() - started, 5)


class RealAlertPathTests(unittest.TestCase):
    """Through the REAL inbox_consumer.route_failure_alert (throttle + backoff intact);
    only the lowest-level sends are mocked and every state path is redirected to tmp."""

    def _swap_then_return(self, persona: str):
        from tools import inbox_consumer as ic

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pane = mock.Mock(return_value=(True, "ok"))
            info = mock.Mock()
            to_orchestrator = mock.Mock(return_value=True)
            with mock.patch.object(ic, "ROUTED_AUDIT_STATE_PATH", root / "audit.json"), \
                 mock.patch.object(ic, "RECOVERY_SELF_BACKOFF_STATE_PATH", root / "backoff.json"), \
                 mock.patch.object(ic, "_send_to_recovery_pane_diagnosed", pane), \
                 mock.patch.object(ic, "_send_telegram_info", info), \
                 mock.patch.object(ic, "_send_telegram_alert", info), \
                 mock.patch.object(ic, "_persona_is_muted", return_value=False), \
                 mock.patch.object(ic, "_deliver_recovery_self_alert_to_orchestrator", to_orchestrator), \
                 mock.patch.object(ic, "_log_recovery_routing"):
                rc._notify_runtime_swap(
                    persona, "codex/interactive-chatgpt", "cap-swap", "2026-10-14T04:13:00Z",
                    "runtime-controller: capped", state_root=root)
                rc._notify_runtime_swap(
                    persona, "codex/interactive-chatgpt", "fallback-return", "2026-10-14T04:20:00Z",
                    "runtime-controller: returning", state_root=root)
            return info, to_orchestrator

    def test_swap_then_return_both_ping_operator(self) -> None:
        info, _ = self._swap_then_return("mason")
        self.assertEqual(info.call_count, 2)
        self.assertIn("capped", info.call_args_list[0].args[0])
        self.assertIn("returning", info.call_args_list[1].args[0])

    def test_recovery_self_path_delivers_swap_and_return(self) -> None:
        _info, to_orchestrator = self._swap_then_return("recovery")
        self.assertEqual(to_orchestrator.call_count, 2)


class ReselectionRankingTests(unittest.TestCase):
    """T-4879: after a cap, reselection ranks usable > near-cap > unknown (not manifest order)."""

    def _rig(self, root: Path, verdicts: dict[str, str], *, cap_episode: bool = True):
        primary = _stateless_binding(root, harness="claude", profile="primary")
        a = _stateless_binding(root, harness="codex", profile="a")
        b = _stateless_binding(root, harness="codex", profile="b")
        if cap_episode:
            rc.record_last_attempt(
                "recovery", primary, outcome="failed", failure_class="subscription_cap",
                trigger="test", state_root=root, now=T0,
                cooldown_until_override=T0 + timedelta(hours=6))
        calls: list[str] = []

        def probe(binding, hint):
            ref = su.runtime_ref(binding)
            calls.append(ref)
            return su.UsabilityProbe(ref, verdicts[ref], "t", T0, T0, None, {})

        def pick():
            sel, _ = rc._select_viable_runtime(
                "recovery", [primary, a, b], {}, T0 + timedelta(minutes=5),
                state_root=root, usability_probe=probe)
            return sel

        return a, b, calls, pick

    def _case(self, a_verdict: str, b_verdict: str, *, cap_episode: bool = True):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, calls, pick = self._rig(
                Path(tmp),
                {"claude/primary": "capped", "codex/a": a_verdict, "codex/b": b_verdict},
                cap_episode=cap_episode)
            chosen = pick()
            return ("a" if chosen is a else "b" if chosen is b else None), calls

    def test_usable_beats_earlier_near_cap(self) -> None:
        self.assertEqual(self._case("near-cap", "usable")[0], "b")

    def test_near_cap_beats_earlier_unknown(self) -> None:
        self.assertEqual(self._case("unknown", "near-cap")[0], "b")

    def test_manifest_order_breaks_ties(self) -> None:
        self.assertEqual(self._case("usable", "usable")[0], "a")

    def test_capped_never_chosen_and_unknown_still_allowed(self) -> None:
        self.assertEqual(self._case("capped", "unknown")[0], "b")

    def test_all_capped_keeps_manifest_order(self) -> None:
        self.assertEqual(self._case("capped", "capped")[0], "a")

    def test_no_cap_episode_keeps_manifest_order_and_never_probes_viable_ones(self) -> None:
        chosen, calls = self._case("unknown", "usable", cap_episode=False)
        self.assertEqual(chosen, None)  # primary (no cooldown) is first viable
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
