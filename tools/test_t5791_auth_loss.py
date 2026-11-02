"""T-5791 r1 REBOUND coverage: Claude authentication-loss classification,
mid-session corroboration, controller recording, consume skip, post-start
raise, and the clear-cooldown rollback. No real tmux; every pane is a string."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import harness_state as hs  # noqa: E402
from tools import runtime_controller as rc  # noqa: E402
from tools.harness_adapter.claude import (  # noqa: E402
    AUTH_LOSS_ACTIVE_REGION_LINES,
    classify_claude_authentication_loss,
)
from tools.test_runtime_controller import _stateless_binding  # noqa: E402

BANNER = "● Please run /login · API Error: 401 Invalid authentication credentials"
PROMPT_CHROME = "─" * 40 + "\n❯ \n" + "─" * 40 + "\n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"


def _pane(*banners: str) -> str:
    body = "".join(f"> wake {i}\n{b}\n" for i, b in enumerate(banners))
    return "earlier transcript line\n" + body + PROMPT_CHROME


class ClassifierTests(unittest.TestCase):
    def test_real_banner_classifies_as_http_401_with_count(self) -> None:
        self.assertEqual(classify_claude_authentication_loss(_pane(BANNER)), ("http-401", 1))
        self.assertEqual(classify_claude_authentication_loss(_pane(BANNER, BANNER)), ("http-401", 2))

    def test_login_required_banner_variants(self) -> None:
        for line in ("Please run /login", "Not logged in - Please run /login", "OAuth token revoked · Please run /login"):
            with self.subTest(line=line):
                self.assertEqual(classify_claude_authentication_loss(_pane(f"  ⎿  {line}"))[0], "login-required")

    def test_account_disabled_banner(self) -> None:
        got = classify_claude_authentication_loss(_pane("  ⎿  API Error: 400 This organization has been disabled."))
        self.assertEqual(got[0], "account-disabled")

    def test_reviewer_false_positives_do_not_match(self) -> None:
        for text in (
            "discussing: the docs say 'Please run /login' when expired",
            "Error 401 invalid authentication in test output",
            "the account was disabled by admin\n organization settings",
            "● The SOP says to run /login when the pane shows a 401",
            "  `API Error: 401 Invalid authentication credentials` is the banner to look for",
        ):
            with self.subTest(text=text):
                self.assertIsNone(classify_claude_authentication_loss(text + "\n" + PROMPT_CHROME))

    def test_cat_output_without_claude_glyph_never_matches(self) -> None:
        """T-5803: a file printed twice by a tool (bare, '>' or '│' prefixed)
        must not classify, so it can never reach count=2."""
        for prefix in ("", "> ", "│ ", "  │ ", ">> "):
            for banner in ("Please run /login", "API Error: 401 Invalid authentication credentials"):
                with self.subTest(prefix=prefix, banner=banner):
                    cat = f"● Bash(cat notes.txt)\n{prefix}{banner}\n{prefix}{banner}\n"
                    self.assertIsNone(classify_claude_authentication_loss(cat + PROMPT_CHROME))

    def test_banner_in_scrollback_outside_active_region_is_ignored(self) -> None:
        scrolled = _pane(BANNER).replace(PROMPT_CHROME, "") + "".join(
            f"● later work line {i}\n" for i in range(AUTH_LOSS_ACTIVE_REGION_LINES)
        ) + PROMPT_CHROME
        self.assertIsNone(classify_claude_authentication_loss(scrolled))


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class BlockUntilTmuxDeathAuthLossTests(unittest.TestCase):
    def _run(self, panes: Callable[[int], str], *, polls: int = 6) -> tuple[Any, list[Any]]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code", mid_session_cap_swap=True)
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            clock = _Clock(datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc))
            state = {"alive": True, "polls": 0, "captures": 0}
            terminated: list[Any] = []

            def alive() -> bool:
                state["polls"] += 1
                return state["alive"] and state["polls"] <= polls

            def capture() -> str:
                state["captures"] += 1
                return panes(state["captures"])

            def terminate(binding, **_kw) -> None:
                terminated.append(binding)
                state["alive"] = False

            result = rc.block_until_tmux_death(
                claude, alive_check=alive, sleep=clock.sleep, poll_interval_seconds=5.0,
                capture_pane_fn=capture, cap_check_interval_seconds=0.0, now=clock.now,
                state_root=root, candidates=[claude, codex], holds={}, terminate_runtime_fn=terminate,
            )
            return result, terminated

    def test_static_banner_pane_never_escalates(self) -> None:
        result, terminated = self._run(lambda _n: _pane(BANNER))
        self.assertIsNone(result)
        self.assertEqual(terminated, [])

    def test_quoted_discussion_never_escalates(self) -> None:
        talk = "● the docs say 'Please run /login' when expired\n● Error 401 invalid authentication in test output\n"
        result, terminated = self._run(lambda n: talk * n + PROMPT_CHROME)
        self.assertIsNone(result)
        self.assertEqual(terminated, [])

    def test_fresh_second_failure_swaps_with_outcome(self) -> None:
        result, terminated = self._run(lambda n: _pane(*([BANNER] * min(n, 2))))
        self.assertIsInstance(result, rc.AuthenticationLossSwapOutcome)
        self.assertEqual(result.pattern_id, "http-401")
        self.assertEqual(len(terminated), 1)


class ControllerRecordingTests(unittest.TestCase):
    def test_controller_main_records_untimed_marker_and_notifies_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code")

            def start(binding):
                return rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False), rc.RuntimeHandle(
                    binding.launch, binding.controller.tmux, None, rc._utc_now()
                )

            with mock.patch.object(rc, "_notify_runtime_swap") as notify:
                code = rc.controller_main(
                    claude.persona, candidates=[claude], state_root=root, sleep=lambda _s: None,
                    start_runtime_fn=start,
                    block_until_death_fn=lambda _b: rc.AuthenticationLossSwapOutcome(pattern_id="http-401"),
                )
            self.assertEqual(code, 0)
            attempt = rc.load_last_attempt(claude.persona, "claude--anthropic-code", state_root=root)
            self.assertEqual((attempt["outcome"], attempt["failure_class"]), ("failed", "authentication_loss"))
            self.assertIsNone(attempt["cooldown_until"])
            notify.assert_called_once()
            self.assertEqual(notify.call_args.args[1:3], ("claude/anthropic-code", "authentication-loss-swap"))
            self.assertEqual(
                rc._controller_cooldown_skip(claude, state_root=root, now=rc._utc_now() + timedelta(days=400)),
                ("controller-authentication-loss", None),
            )

    def test_post_start_authentication_loss_raises_classified_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binding = _stateless_binding(Path(tmp), harness="claude", profile="anthropic-code")
            assessment = rc.PostStartAssessment(
                schema="pantheon-harness-post-start-assessment/v1", harness="claude",
                profile="anthropic-code", status="authentication-loss",
                cause="claude-authentication-loss", pattern_id="http-401",
                baseline_sha256="0" * 64, capture_count=2, changed_capture_indices=(1, 2),
            )
            with self.assertRaises(rc.RuntimeOperationalFailure) as caught:
                rc._raise_for_post_start_assessment(
                    binding, rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True), assessment,
                )
            self.assertEqual(caught.exception.failure_class, "authentication-loss")
            self.assertEqual(rc._classify_death(caught.exception), "authentication_loss")


class RollbackTests(unittest.TestCase):
    def test_clear_cooldown_restores_claude_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hs_root = Path(tmp)
            personas = hs_root / "personas"
            claude = _stateless_binding(personas, harness="claude", profile="anthropic-code")
            codex = _stateless_binding(personas, harness="codex", profile="interactive-chatgpt")
            now = rc._utc_now()
            rc.record_last_attempt(
                "orchestrator", claude, outcome="failed", failure_class="authentication_loss",
                state_root=personas, now=now,
            )
            blocked, _ = rc._select_viable_runtime("orchestrator", [claude, codex], {}, now, state_root=personas)
            self.assertIs(blocked, codex)

            doc, had = hs.clear_cooldown(
                "orchestrator", "claude/anthropic-code", operator="recovery", now_ts=rc._iso(now), root=hs_root,
            )
            self.assertTrue(had)
            self.assertIsNone(doc["failure_class"])
            hs.write_last_attempt("orchestrator", "claude/anthropic-code", doc, root=hs_root)

            restored, skipped = rc._select_viable_runtime("orchestrator", [claude, codex], {}, now, state_root=personas)
            self.assertIs(restored, claude)
            self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main()
