"""Unit tests for tools/harness_adapter/ — PantheonHarnessAdapter bindings.

Tests the ORG-031 §i partition structure, per-harness key/filename translations,
and the honest seams on the codex binding (resume_session / inject_inbound).

Run from repo root:
    python3 -m pytest tools/test_harness_adapter.py -v

Coverage:
  1. Instantiation of both concrete bindings
  2. Interface completeness — all 7+2 abstract methods present on base class
  3. Partition 3 integrity — 5 not-abstracted surfaces NOT @abstractmethod on base
  4. Key binding translations — standing_prompt, mcp config, hooks config,
     exec_headless, auth_status, approval_defaults
  5. Typed explicit session resume
  6. Durable inbound delivery receipts
  7. Claude not-abstracted props return dicts (no raise); codex ones too
  8. Role/domain axis — role_domain accessible on both adapters after construction
  9. HARNESS_NAME class attribute on each concrete class
 10. PantheonHarnessAdapter cannot be instantiated directly (ABC enforcement)
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import inspect
import io
import json
import os
import tempfile
import tomllib  # stdlib ≥3.11 — the declared floor (T-2733: production harness_adapter needs it)
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

from tools.harness_adapter import (
    ClaudeHarnessAdapter,
    CodexHarnessAdapter,
    ExecutionRequest,
    HarnessEvent,
    InboundRequest,
    PantheonHarnessAdapter,
    SessionHandle,
)
from tools.harness_adapter.codex import (
    _prune_stale_project_entries,
    ensure_model_migration_prompts_hidden,
    ensure_lifecycle_hooks,
    ensure_project_trusted,
    ensure_update_check_disabled,
    ensure_worker_guard_hook,
    prepare_codex_launch,
    worker_guard_command,
    worker_guard_hook_status,
    gc_stale_project_trust_entries,
    gc_trust_entries_cli,
)
from tools.harness_adapter import codex as codex_adapter
from tools.harness_adapter.claude import (
    _parse_claude_mid_session_cap_reset_at,
    classify_claude_mid_session_cap,
)
from tools.harness_adapter.types import HarnessLaunchConfig, PostStartPaneSample, TmuxTarget
from tools.harness_adapter import claude as claude_adapter
from tools import mailbox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _claude_launch_config(native: dict) -> HarnessLaunchConfig:
    """Minimal HarnessLaunchConfig for direct build_interactive_launch tests."""
    return HarnessLaunchConfig(
        harness="claude",
        profile="interactive-subscription",
        executable=Path("/tmp/fake-claude-home/bin/claude"),
        model=None,
        mode="interactive",
        meter="subscription",
        quota_source="statusline",
        native=native,
        sources=("test",),
    )


def _post_start_sample(text: str, index: int) -> PostStartPaneSample:
    return PostStartPaneSample(
        capture_index=index,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        snippet_sha256=hashlib.sha256(text[:512].encode("utf-8")).hexdigest() if text else None,
        text=text,
    )


def _abstract_method_names(cls: type) -> set[str]:
    """Return the set of @abstractmethod names on *cls*."""
    return {
        name
        for name, val in inspect.getmembers(cls)
        if getattr(val, "__isabstractmethod__", False)
    }


# ---------------------------------------------------------------------------
# 1. ABC cannot be instantiated directly
# ---------------------------------------------------------------------------

class TestAbstractBase(unittest.TestCase):
    def test_base_cannot_be_instantiated(self) -> None:
        with self.assertRaises(TypeError):
            PantheonHarnessAdapter("orchestrator")  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# 2. Interface completeness — 7+2 abstract methods present on the base
# ---------------------------------------------------------------------------

class TestInterfaceCompleteness(unittest.TestCase):
    """All 7 generalized + 2 bridge surfaces must be @abstractmethod on the base."""

    EXPECTED_ABSTRACT_METHODS = {
        # Typed runtime lifecycle
        "capabilities",
        "build_exec",
        "build_resume",
        "parse_event",
        "finalize",
        "deliver_inbound",
        "spawn_error_event",
        "validate_launch_config",
        "describe_surfaces",
        "auth_descriptor",
        "build_interactive_launch",
        "quota_observer",
        # Partition 1 — 7 generalized
        "standing_prompt_filename",
        "mcp_server_config_path",
        "hooks_config_path",
        "spawn_subagent",
        "resume_session",
        "exec_headless",
        "auth_status",
        # Partition 2 — 2 bridge
        "inject_inbound",
        "approval_defaults",
    }

    def test_all_abstract_methods_present(self) -> None:
        abstract = _abstract_method_names(PantheonHarnessAdapter)
        missing = self.EXPECTED_ABSTRACT_METHODS - abstract
        self.assertEqual(
            missing,
            set(),
            f"Missing @abstractmethod on PantheonHarnessAdapter: {missing}",
        )

    def test_no_extra_abstract_methods(self) -> None:
        """The 5 not-abstracted surfaces must NOT be @abstractmethod."""
        not_abstracted = {
            "sandbox_config",
            "profiles_config",
            "app_server_config",
            "cloud_config",
            "doctor_config",
        }
        abstract = _abstract_method_names(PantheonHarnessAdapter)
        wrongly_abstract = not_abstracted & abstract
        self.assertEqual(
            wrongly_abstract,
            set(),
            f"These surfaces should NOT be @abstractmethod: {wrongly_abstract}",
        )


# ---------------------------------------------------------------------------
# 3. Partition 3 — not-abstracted surfaces raise on the base class
# ---------------------------------------------------------------------------

def _make_minimal_concrete() -> type:
    """Return a minimal concrete subclass implementing only the 9 abstract methods.

    Does NOT override any of the 5 not-abstracted @property surfaces, so those
    fall through to the base-class raise.  Used to test that the base class raises
    NotImplementedError for the not-abstracted partition.
    """

    class _Minimal(PantheonHarnessAdapter):
        HARNESS_NAME = "minimal"

        def standing_prompt_filename(self) -> str:
            return "MINIMAL.md"

        def capabilities(self):
            return None

        def build_exec(self, request):
            return None

        def build_resume(self, session, prompt=None):
            return None

        def parse_event(self, line):
            return None

        def finalize(self, events, *, returncode, stderr):
            return None

        def deliver_inbound(self, request):
            return None

        def spawn_error_event(self, exc):
            return None

        def validate_launch_config(self):
            return ()

        def describe_surfaces(self, cwd):
            return None

        def auth_descriptor(self):
            return None

        def build_interactive_launch(self, target, *, cwd):
            del cwd
            return None

        def quota_observer(self, state_dir):
            return None

        def mcp_server_config_path(self) -> Path:
            return Path("/tmp/minimal-config")

        def hooks_config_path(self) -> Path:
            return Path("/tmp/minimal-hooks")

        def spawn_subagent(self, role: str, config_overrides: dict[str, Any]) -> dict[str, Any]:
            return {}

        def resume_session(self, session_id: str) -> dict[str, Any]:
            return {}

        def exec_headless(self, prompt: str, **kwargs: Any) -> list[str]:
            return []

        def auth_status(self) -> dict[str, Any]:
            return {}

        def inject_inbound(self, message: str, **kwargs: Any) -> None:
            return None

        def approval_defaults(self) -> dict[str, Any]:
            return {}

    return _Minimal


class TestPartition3BaseRaises(unittest.TestCase):
    """Base class not-abstracted properties must raise NotImplementedError."""

    NOT_ABSTRACTED = [
        "sandbox_config",
        "profiles_config",
        "app_server_config",
        "cloud_config",
        "doctor_config",
    ]

    def setUp(self) -> None:
        # Use a minimal concrete that does NOT override the not-abstracted props,
        # so accessing them falls through to the base-class raise.
        MinimalConcrete = _make_minimal_concrete()
        self.minimal = MinimalConcrete("orchestrator")

    def test_base_not_abstracted_raise_on_base(self) -> None:
        """Accessing a not-abstracted surface on an unoverriding subclass must raise."""
        for name in self.NOT_ABSTRACTED:
            prop = PantheonHarnessAdapter.__dict__.get(name)
            self.assertIsNotNone(prop, f"property {name!r} missing from base class")
            self.assertIsInstance(
                prop, property, f"{name!r} should be a @property on the base"
            )
            with self.assertRaises(NotImplementedError, msg=f"base.{name} should raise"):
                getattr(self.minimal, name)


# ---------------------------------------------------------------------------
# 4. Instantiation + role_domain axis
# ---------------------------------------------------------------------------

class TestInstantiation(unittest.TestCase):
    def test_claude_instantiates(self) -> None:
        adapter = ClaudeHarnessAdapter("orchestrator")
        self.assertIsInstance(adapter, PantheonHarnessAdapter)

    def test_codex_instantiates(self) -> None:
        adapter = CodexHarnessAdapter("specialist")
        self.assertIsInstance(adapter, PantheonHarnessAdapter)

    def test_claude_role_domain_accessible(self) -> None:
        adapter = ClaudeHarnessAdapter("orchestrator")
        self.assertEqual(adapter.role_domain, "orchestrator")

    def test_codex_role_domain_accessible(self) -> None:
        adapter = CodexHarnessAdapter("recovery")
        self.assertEqual(adapter.role_domain, "recovery")

    def test_claude_harness_name(self) -> None:
        self.assertEqual(ClaudeHarnessAdapter.HARNESS_NAME, "claude")

    def test_codex_harness_name(self) -> None:
        self.assertEqual(CodexHarnessAdapter.HARNESS_NAME, "codex")


# ---------------------------------------------------------------------------
# 5. ClaudeHarnessAdapter — key/filename binding translations
# ---------------------------------------------------------------------------

class TestClaudeTranslations(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ClaudeHarnessAdapter(
            "orchestrator",
            claude_home=Path("/tmp/fake-claude-home"),
        )

    def test_standing_prompt_filename(self) -> None:
        self.assertEqual(self.adapter.standing_prompt_filename(), "CLAUDE.md")

    def test_mcp_server_config_path_is_current_user_registry(self) -> None:
        p = self.adapter.mcp_server_config_path()
        self.assertEqual(p.name, ".claude.json")

    def test_hooks_config_path_ends_with_settings_json(self) -> None:
        p = self.adapter.hooks_config_path()
        self.assertEqual(p.name, "settings.json")

    def test_exec_headless_returns_claude_p_prefix(self) -> None:
        args = self.adapter.exec_headless("hello world")
        self.assertEqual(args[0], "claude")
        self.assertIn("-p", args)
        self.assertIn("hello world", args)

    def test_exec_headless_returns_list_of_strings(self) -> None:
        args = self.adapter.exec_headless("prompt text")
        self.assertIsInstance(args, list)
        self.assertTrue(all(isinstance(a, str) for a in args))

    def test_resume_session_returns_dict_with_resume_arg(self) -> None:
        result = self.adapter.resume_session("abc123")
        self.assertIsInstance(result, dict)
        self.assertEqual(result["harness"], "claude")
        cli_args = result.get("cli_args", [])
        self.assertIn("--resume", cli_args)
        self.assertIn("abc123", cli_args)

    def test_auth_status_returns_dict_with_harness(self) -> None:
        result = self.adapter.auth_status()
        self.assertIsInstance(result, dict)
        self.assertEqual(result["harness"], "claude")
        self.assertIn("key_source", result)

    def test_legacy_interactive_launch_still_pins_bypass_permission_mode(self) -> None:
        command = self.adapter.build_interactive_launch(
            TmuxTarget(session="recovery", window="harness", pane="0"),
            cwd=Path("/tmp/launch"),
        )
        self.assertEqual(command.argv.count("--permission-mode"), 1)
        self.assertEqual(command.argv[-2:], ("--permission-mode", "bypassPermissions"))

    def test_interactive_launch_disables_file_mentions(self) -> None:
        # T-5843: an injected '@/path' must never load a file pre-model on a seat.
        command = self.adapter.build_interactive_launch(
            TmuxTarget(session="recovery", window="harness", pane="0"),
            cwd=Path("/tmp/launch"),
        )
        self.assertEqual(command.env.get("CLAUDE_CODE_DISABLE_FILE_MENTIONS"), "1")

    def test_claude_post_start_weekly_limit_is_subscription_cap_not_runtime_started(self) -> None:
        adapter = ClaudeHarnessAdapter(
            "orchestrator",
            claude_home=Path("/tmp/fake-claude-home"),
            launch=_claude_launch_config({}),
        )
        cap_text = (Path(__file__).resolve().parent / "testdata" / "orchestrator_cap_modal_pane.txt").read_text(encoding="utf-8")
        baseline = _post_start_sample("❯ \n", 0)
        assessment = adapter.assess_post_start_readiness(
            harness="claude",
            profile="interactive-subscription",
            baseline=baseline,
            samples=(_post_start_sample(cap_text, 1),),
        )
        payload = dataclasses.asdict(assessment)
        self.assertEqual(assessment.status, "subscription-cap")
        self.assertEqual(assessment.cause, "post-start-subscription-cap")
        self.assertEqual(assessment.pattern_id, "claude-rate-limit-options-modal")
        self.assertEqual(assessment.baseline_sha256, baseline.sha256)
        # T-4047: reset_at is a derived datetime, not raw text -- json.dumps
        # needs `default=str` to serialize it; the invariant under test
        # (no raw pane prose leaks into the assessment) still holds against
        # the str()-rendered payload.
        self.assertNotIn("You've hit your", json.dumps(payload, sort_keys=True, default=str))
        self.assertNotIn("What do you want to do?", json.dumps(payload, sort_keys=True, default=str))

    def test_claude_post_start_cap_assessment_parses_reset_at_and_cap_kind(self) -> None:
        """T-4047: the fixture's bare-time reset ("resets 2:50pm (UTC)" on a
        SESSION-limit sentinel) parses to an absolute UTC instant and a
        session-limit cap_kind, given a deterministic `now`."""
        adapter = ClaudeHarnessAdapter(
            "orchestrator",
            claude_home=Path("/tmp/fake-claude-home"),
            launch=_claude_launch_config({}),
        )
        cap_text = (Path(__file__).resolve().parent / "testdata" / "orchestrator_cap_modal_pane.txt").read_text(encoding="utf-8")
        fixed_now = datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc)
        assessment = adapter.assess_post_start_readiness(
            harness="claude",
            profile="interactive-subscription",
            baseline=_post_start_sample("❯ \n", 0),
            samples=(_post_start_sample(cap_text, 1),),
            now=fixed_now,
        )
        self.assertEqual(assessment.status, "subscription-cap")
        self.assertEqual(assessment.cap_kind, "session-limit")
        self.assertEqual(assessment.reset_at, datetime(2026, 9, 27, 14, 50, 0, tzinfo=timezone.utc))

    def test_claude_post_start_cap_assessment_unparseable_weekly_kind_has_no_reset_at(self) -> None:
        """T-4047: a weekly-limit sentinel with unparseable reset text yields
        cap_kind="weekly-limit" and reset_at=None -- the caller
        (_post_start_cap_cooldown_override) is what decides a weekly,
        unparseable cap should legitimately stay on the long next_weekly_reset
        default; this classifier's job is only to report what it saw."""
        adapter = ClaudeHarnessAdapter(
            "orchestrator",
            claude_home=Path("/tmp/fake-claude-home"),
            launch=_claude_launch_config({}),
        )
        # Plain text (no embedded ANSI) -- classify_claude_post_start_subscription_cap's
        # framing check is a simple substring test over the whole normalized
        # text, so real styling adds nothing this synthetic fixture needs;
        # unlike the mid-session classifier's stricter position-anchored
        # checks, this one has no footer/chrome requirement to satisfy.
        weekly_unparseable = (
            "  ⎿  You've hit your weekly limit · resets whenever the moon says so\n"
            "\n"
            "❯ /rate-limit-options\n"
            "\n"
            "What do you want to do?\n"
            "\n"
            "❯ 1. Stop and wait for limit to reset\n"
            "  2. Upgrade your plan\n"
            "\n"
            "Enter to confirm · Esc to cancel\n"
        )
        assessment = adapter.assess_post_start_readiness(
            harness="claude",
            profile="interactive-subscription",
            baseline=_post_start_sample("❯ \n", 0),
            samples=(_post_start_sample(weekly_unparseable, 1),),
            now=datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(assessment.status, "subscription-cap")
        self.assertEqual(assessment.cap_kind, "weekly-limit")
        self.assertIsNone(assessment.reset_at)

    def test_claude_post_start_cap_classifier_rejects_prompt_history_and_broad_limit_text(self) -> None:
        adapter = ClaudeHarnessAdapter(
            "orchestrator",
            claude_home=Path("/tmp/fake-claude-home"),
            launch=_claude_launch_config({}),
        )
        cap_text = (Path(__file__).resolve().parent / "testdata" / "orchestrator_cap_modal_pane.txt").read_text(encoding="utf-8")
        cases = (
            ("prompt-echo", "❯ \n", "User prompt: You've hit your weekly limit · resets 2:50pm (UTC)\n"),
            ("broad", "❯ \n", "weekly limit reached somewhere in docs\n"),
            ("old-provider-baseline", cap_text, cap_text),
            ("exact-sentence-without-modal", "❯ \n", "You've hit your session limit · resets 2:50pm (UTC)\n"),
        )
        for label, baseline_text, sample_text in cases:
            with self.subTest(label=label):
                assessment = adapter.assess_post_start_readiness(
                    harness="claude",
                    profile="interactive-subscription",
                    baseline=_post_start_sample(baseline_text, 0),
                    samples=(_post_start_sample(sample_text, 1),),
                )
                self.assertNotEqual(assessment.status, "subscription-cap")
                self.assertIsNone(assessment.pattern_id)

    def test_claude_authentication_loss_requires_two_identical_captures(self) -> None:
        adapter = ClaudeHarnessAdapter(
            "orchestrator", claude_home=Path("/tmp/fake-claude-home"),
            launch=_claude_launch_config({}),
        )
        baseline = _post_start_sample("❯ \n", 0)
        one = adapter.assess_post_start_readiness(
            harness="claude", profile="interactive-subscription", baseline=baseline,
            samples=(_post_start_sample("● Please run /login", 1),),
        )
        two = adapter.assess_post_start_readiness(
            harness="claude", profile="interactive-subscription", baseline=baseline,
            samples=(
                _post_start_sample("● Please run /login", 1),
                _post_start_sample("● Please run /login", 2),
            ),
        )
        self.assertEqual(one.status, "unknown")
        self.assertEqual(two.status, "authentication-loss")
        self.assertEqual(two.pattern_id, "login-required")

    def test_claude_interactive_completion_observer_uses_fresh_statusline_boundary_without_raw_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_home = root / "account"
            state_dir = root / "state" / "recovery" / "runtime-controller"
            transcript = account_home / ".claude" / "projects" / "project-a" / "session.jsonl"
            transcript.parent.mkdir(parents=True)
            statusline = state_dir / "quota" / "claude-statusline.json"
            statusline.parent.mkdir(parents=True)
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "old turn"}]},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            statusline.write_text(
                json.dumps(
                    {
                        "schema": "pantheon-claude-statusline-quota/v1",
                        "observed_at": "2026-08-14T00:00:00Z",
                        "session_id": "old",
                        "transcript_path": str(transcript),
                        "rate_limits": {"ok": True},
                    }
                ),
                encoding="utf-8",
            )
            adapter = ClaudeHarnessAdapter("orchestrator", claude_home=account_home)
            marker = "PANTHEON_PROVIDER_COMPLETION_fixture"
            probe = adapter.prepare_interactive_provider_completion(
                harness="claude",
                profile="interactive-subscription",
                persona="recovery",
                account_home=account_home,
                state_dir=state_dir,
                pane_baseline=_post_start_sample("ready\n❯ \n", 0),
                submitted_at=datetime(2026, 8, 14, 0, 0, 1, tzinfo=timezone.utc),
                nonce_marker=marker,
                nonce_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            )
            self.assertIsNotNone(probe)
            assert probe is not None
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "user", "message": {"role": "user", "content": marker}}) + "\n")
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {"role": "assistant", "content": [{"type": "text", "text": marker}]},
                        }
                    )
                    + "\n"
                )
            statusline.write_text(
                json.dumps(
                    {
                        "schema": "pantheon-claude-statusline-quota/v1",
                        "observed_at": "2026-08-14T00:00:02Z",
                        "session_id": "new",
                        "transcript_path": str(transcript),
                        "rate_limits": {"ok": True},
                    }
                ),
                encoding="utf-8",
            )
            assessment = adapter.assess_interactive_provider_completion(
                harness="claude",
                profile="interactive-subscription",
                probe=probe,
                samples=(_post_start_sample("ready changed\n❯ \n", 1),),
            )
        self.assertEqual(assessment.status, "provider-completed")
        payload = json.dumps(dataclasses.asdict(assessment), sort_keys=True)
        self.assertNotIn(marker, payload)
        self.assertNotIn("old turn", payload)
        self.assertNotIn("ready changed", payload)
        self.assertNotIn(str(transcript), payload)

    def test_claude_interactive_completion_classifies_existing_monthly_spend_modal(self) -> None:
        """T-6235: fallback-return can land directly on the blocking modal.

        The first provider-completion baseline therefore already contains the
        modal; requiring a post-baseline pane change misclassifies it as an
        ordinary completion timeout instead of a subscription cap.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_home = root / "account"
            (account_home / ".claude").mkdir(parents=True)
            state_dir = root / "state" / "specialist" / "runtime-controller"
            modal = (
                Path(__file__).resolve().parent / "testdata" / "orchestrator_spend_limit_modal_pane.txt"
            ).read_text(encoding="utf-8").replace("Sep 27", "Nov 8")
            adapter = ClaudeHarnessAdapter("orchestrator", claude_home=account_home)
            marker = "PANTHEON_PROVIDER_COMPLETION_t6235"
            probe = adapter.prepare_interactive_provider_completion(
                harness="claude",
                profile="interactive-subscription",
                persona="specialist",
                account_home=account_home,
                state_dir=state_dir,
                pane_baseline=_post_start_sample(modal, 0),
                submitted_at=datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc),
                nonce_marker=marker,
                nonce_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            )
            assert probe is not None
            assessment = adapter.assess_interactive_provider_completion(
                harness="claude",
                profile="interactive-subscription",
                probe=probe,
                samples=(_post_start_sample(modal, 1),),
            )

        self.assertEqual(assessment.status, "subscription-cap")
        self.assertEqual(assessment.cause, "provider-completion-subscription-cap")
        self.assertEqual(assessment.pattern_id, "claude-monthly-spend-limit-modal")
        self.assertEqual(assessment.cap_kind, "monthly-spend-limit")
        self.assertEqual(
            assessment.reset_at,
            datetime(2026, 11, 8, 17, 0, tzinfo=timezone.utc),
        )

    _HISTORY_SUBMITTED_AT = datetime(2026, 8, 14, 0, 0, 1, tzinfo=timezone.utc)

    def _history_probe(self, root: Path, marker: str = "PANTHEON_PROVIDER_COMPLETION_hist"):
        account_home = root / "account"
        (account_home / ".claude").mkdir(parents=True)
        adapter = ClaudeHarnessAdapter("orchestrator", claude_home=account_home)
        probe = adapter.prepare_interactive_provider_completion(
            harness="claude",
            profile="interactive-subscription",
            persona="recovery",
            account_home=account_home,
            state_dir=root / "state" / "recovery" / "runtime-controller",
            pane_baseline=_post_start_sample("ready\n❯ \n", 0),
            submitted_at=self._HISTORY_SUBMITTED_AT,
            nonce_marker=marker,
            nonce_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
        )
        assert probe is not None
        return adapter, probe, account_home / ".claude" / "history.jsonl"

    def _history_line(self, display: str, *, stamp_ms: int, pasted: bool = False) -> str:
        contents = {"1": {"id": "1", "type": "text", "contentHash": "261f3e63d6f0d48a"}} if pasted else {}
        return json.dumps({"display": display, "pastedContents": contents, "timestamp": stamp_ms}) + "\n"

    def test_claude_probe_submitted_reads_the_nonce_from_prompt_history(self) -> None:
        # T-4321: Claude Code appends each submitted prompt to history.jsonl.
        with tempfile.TemporaryDirectory() as tmp:
            adapter, probe, history = self._history_probe(Path(tmp))
            submit_ms = int(self._HISTORY_SUBMITTED_AT.timestamp() * 1000)
            history.write_text(self._history_line("an older prompt", stamp_ms=submit_ms - 5000), encoding="utf-8")
            self.assertIs(adapter.interactive_provider_completion_submitted(probe=probe), False)
            with history.open("a", encoding="utf-8") as handle:
                handle.write(self._history_line(probe.prompt, stamp_ms=submit_ms + 100))
            self.assertIs(adapter.interactive_provider_completion_submitted(probe=probe), True)

    def test_claude_probe_submitted_counts_a_collapsed_paste_after_the_submit(self) -> None:
        # A collapsed paste keeps its text in paste-cache, so the nonce isn't
        # in history. A pasted entry stamped at/after the submit still counts;
        # an older one doesn't.
        with tempfile.TemporaryDirectory() as tmp:
            adapter, probe, history = self._history_probe(Path(tmp))
            submit_ms = int(self._HISTORY_SUBMITTED_AT.timestamp() * 1000)
            history.write_text(
                self._history_line("[Pasted text #1 +1 lines]", stamp_ms=submit_ms - 1, pasted=True),
                encoding="utf-8",
            )
            self.assertIs(adapter.interactive_provider_completion_submitted(probe=probe), False)
            with history.open("a", encoding="utf-8") as handle:
                handle.write(self._history_line("[Pasted text #2 +1 lines]", stamp_ms=submit_ms, pasted=True))
            self.assertIs(adapter.interactive_provider_completion_submitted(probe=probe), True)

    def test_claude_probe_submitted_ignores_other_prompts_after_the_submit(self) -> None:
        # T-4321 r1 N3 (A7): history.jsonl is per ACCOUNT, so another session's
        # ordinary prompt can land after the submit. Only the nonce, or a paste,
        # counts; the compact and the spaced JSON forms both count as "no paste".
        with tempfile.TemporaryDirectory() as tmp:
            adapter, probe, history = self._history_probe(Path(tmp))
            submit_ms = int(self._HISTORY_SUBMITTED_AT.timestamp() * 1000)
            compact = json.dumps({"display": "unrelated", "pastedContents": {}, "timestamp": submit_ms + 50},
                                 separators=(",", ":"))
            history.write_text(compact + "\n" + self._history_line("unrelated too", stamp_ms=submit_ms + 60),
                               encoding="utf-8")
            self.assertIs(adapter.interactive_provider_completion_submitted(probe=probe), False)

    def _probe_transcript(self, account_home: Path, records: list[dict], *, name: str = "sess.jsonl") -> Path:
        project = account_home / ".claude" / "projects" / "-home-recovery-pantheon-config"
        project.mkdir(parents=True, exist_ok=True)
        path = project / name
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return path

    def _unflushed_history(self, history: Path) -> None:
        # T-4459: readable history that does NOT yet hold the probe (the write is buffered).
        submit_ms = int(self._HISTORY_SUBMITTED_AT.timestamp() * 1000)
        history.write_text(self._history_line("an older prompt", stamp_ms=submit_ms - 5000), encoding="utf-8")

    def test_claude_probe_taken_when_history_is_unflushed_but_the_transcript_has_it(self) -> None:
        # T-4459 (T-3026 attempt 4): Claude Code buffers history writes, so the entry
        # was missing while the probe turn was already running, and the controller
        # re-typed it. The transcript's user event is written at submit.
        with tempfile.TemporaryDirectory() as tmp:
            adapter, probe, history = self._history_probe(Path(tmp))
            self._unflushed_history(history)
            self.assertIs(adapter.interactive_provider_completion_submitted(probe=probe), False)
            self._probe_transcript(
                history.parent.parent,
                [{"type": "user", "message": {"role": "user", "content": probe.prompt}}],
            )
            self.assertIs(adapter.interactive_provider_completion_submitted(probe=probe), True)

    def test_claude_probe_transcript_fallback_needs_a_user_event(self) -> None:
        # Only the user event proves the prompt was taken; the nonce elsewhere doesn't.
        with tempfile.TemporaryDirectory() as tmp:
            adapter, probe, history = self._history_probe(Path(tmp))
            self._unflushed_history(history)
            self._probe_transcript(
                history.parent.parent,
                [{"type": "assistant", "message": {"role": "assistant",
                                                   "content": [{"type": "text", "text": probe.nonce_marker}]}}],
            )
            self.assertIs(adapter.interactive_provider_completion_submitted(probe=probe), False)

    def test_claude_probe_transcript_fallback_ignores_transcripts_older_than_the_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter, probe, history = self._history_probe(Path(tmp))
            self._unflushed_history(history)
            path = self._probe_transcript(
                history.parent.parent,
                [{"type": "user", "message": {"role": "user", "content": probe.prompt}}],
            )
            old = self._HISTORY_SUBMITTED_AT.timestamp() - 3600
            os.utime(path, (old, old))
            self.assertIs(adapter.interactive_provider_completion_submitted(probe=probe), False)

    def test_claude_probe_transcript_fallback_never_follows_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter, probe, history = self._history_probe(Path(tmp))
            self._unflushed_history(history)
            outside = Path(tmp) / "outside.jsonl"
            outside.write_text(
                json.dumps({"type": "user", "message": {"role": "user", "content": probe.prompt}}) + "\n",
                encoding="utf-8",
            )
            project = history.parent / "projects" / "-p"
            project.mkdir(parents=True)
            (project / "linked.jsonl").symlink_to(outside)
            self.assertIs(adapter.interactive_provider_completion_submitted(probe=probe), False)

    def test_probe_submitted_defaults_to_unknown_for_other_harnesses(self) -> None:
        # T-4321 r1 N1: the base hook says None, so a harness without a
        # prompt-history surface never has its probe re-typed.
        adapter = CodexHarnessAdapter("specialist")
        with tempfile.TemporaryDirectory() as tmp:
            _claude, probe, _history = self._history_probe(Path(tmp))
            self.assertIsNone(adapter.interactive_provider_completion_submitted(probe=probe))

    def test_claude_probe_submitted_is_unknown_without_a_readable_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter, probe, history = self._history_probe(Path(tmp))
            self.assertIsNone(adapter.interactive_provider_completion_submitted(probe=probe))
            history.write_text(self._history_line("an older prompt", stamp_ms=1), encoding="utf-8")
            history.chmod(0)
            try:
                self.assertIsNone(adapter.interactive_provider_completion_submitted(probe=probe))
            finally:
                history.chmod(0o600)

    def test_claude_interactive_completion_rejects_nonce_with_non_whitespace_prefix_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_home = root / "account"
            state_dir = root / "state" / "recovery" / "runtime-controller"
            transcript = account_home / ".claude" / "projects" / "project-a" / "session.jsonl"
            transcript.parent.mkdir(parents=True)
            statusline = state_dir / "quota" / "claude-statusline.json"
            statusline.parent.mkdir(parents=True)
            transcript.write_text("", encoding="utf-8")
            statusline.write_text(
                json.dumps(
                    {
                        "schema": "pantheon-claude-statusline-quota/v1",
                        "observed_at": "2026-08-14T00:00:00Z",
                        "session_id": "old",
                        "transcript_path": str(transcript),
                        "rate_limits": {"ok": True},
                    }
                ),
                encoding="utf-8",
            )
            adapter = ClaudeHarnessAdapter("orchestrator", claude_home=account_home)
            marker = "PANTHEON_PROVIDER_COMPLETION_fixture"
            probe = adapter.prepare_interactive_provider_completion(
                harness="claude",
                profile="interactive-subscription",
                persona="recovery",
                account_home=account_home,
                state_dir=state_dir,
                pane_baseline=_post_start_sample("ready\n❯ \n", 0),
                submitted_at=datetime(2026, 8, 14, 0, 0, 1, tzinfo=timezone.utc),
                nonce_marker=marker,
                nonce_sha256=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            )
            self.assertIsNotNone(probe)
            assert probe is not None
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": f"not exact: {marker} extra"}],
                            },
                        }
                    )
                    + "\n"
                )
            statusline.write_text(
                json.dumps(
                    {
                        "schema": "pantheon-claude-statusline-quota/v1",
                        "observed_at": "2026-08-14T00:00:02Z",
                        "session_id": "prefixed",
                        "transcript_path": str(transcript),
                        "rate_limits": {"ok": True},
                    }
                ),
                encoding="utf-8",
            )
            prefixed = adapter.assess_interactive_provider_completion(
                harness="claude",
                profile="interactive-subscription",
                probe=probe,
                samples=(_post_start_sample("ready changed\n❯ \n", 1),),
            )
            self.assertNotEqual(prefixed.status, "provider-completed")

            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": f" \n{marker}\t "}],
                            },
                        }
                    )
                    + "\n"
                )
            statusline.write_text(
                json.dumps(
                    {
                        "schema": "pantheon-claude-statusline-quota/v1",
                        "observed_at": "2026-08-14T00:00:03Z",
                        "session_id": "exact",
                        "transcript_path": str(transcript),
                        "rate_limits": {"ok": True},
                    }
                ),
                encoding="utf-8",
            )
            exact = adapter.assess_interactive_provider_completion(
                harness="claude",
                profile="interactive-subscription",
                probe=probe,
                samples=(_post_start_sample("ready changed again\n❯ \n", 2),),
            )
        self.assertEqual(exact.status, "provider-completed")

    def test_build_interactive_launch_emits_channels_flag_when_native_channels_set(
        self,
    ) -> None:
        adapter = ClaudeHarnessAdapter(
            "recovery",
            claude_home=Path("/tmp/fake-claude-home"),
            launch=_claude_launch_config(
                {"channels": "plugin:telegram@claude-plugins-official"}
            ),
        )
        command = adapter.build_interactive_launch(
            TmuxTarget(session="recovery", window="harness", pane="0"),
            cwd=Path("/tmp/launch"),
        )
        self.assertEqual(command.argv.count("--channels"), 1)
        idx = command.argv.index("--channels")
        self.assertEqual(
            command.argv[idx + 1], "plugin:telegram@claude-plugins-official"
        )

    def test_build_interactive_launch_omits_channels_flag_when_native_empty(
        self,
    ) -> None:
        adapter = ClaudeHarnessAdapter(
            "recovery",
            claude_home=Path("/tmp/fake-claude-home"),
            launch=_claude_launch_config({}),
        )
        command = adapter.build_interactive_launch(
            TmuxTarget(session="recovery", window="harness", pane="0"),
            cwd=Path("/tmp/launch"),
        )
        self.assertNotIn("--channels", command.argv)

    def test_build_interactive_launch_emits_effort_flag_when_native_effort_set(
        self,
    ) -> None:
        # T-3084: fleet policy (opus-4.8 @ xhigh) must survive the HC adapter --
        # specialist silently dropped to CLI-default effort at HC cutover because the
        # adapter never wired native.effort into argv. Mirrors the channels-flag
        # test shape above.
        adapter = ClaudeHarnessAdapter(
            "recovery",
            claude_home=Path("/tmp/fake-claude-home"),
            launch=_claude_launch_config({"effort": "xhigh"}),
        )
        command = adapter.build_interactive_launch(
            TmuxTarget(session="recovery", window="harness", pane="0"),
            cwd=Path("/tmp/launch"),
        )
        self.assertEqual(command.argv.count("--effort"), 1)
        idx = command.argv.index("--effort")
        self.assertEqual(command.argv[idx + 1], "xhigh")
        # Composition check: --permission-mode must stay the trailing pair --
        # test_legacy_interactive_launch_still_pins_bypass_permission_mode and
        # the T-2382 fixture both pin argv[-2:] to it.
        self.assertEqual(command.argv[-2:], ("--permission-mode", "bypassPermissions"))

    def test_build_interactive_launch_omits_effort_flag_when_native_empty(
        self,
    ) -> None:
        adapter = ClaudeHarnessAdapter(
            "recovery",
            claude_home=Path("/tmp/fake-claude-home"),
            launch=_claude_launch_config({}),
        )
        command = adapter.build_interactive_launch(
            TmuxTarget(session="recovery", window="harness", pane="0"),
            cwd=Path("/tmp/launch"),
        )
        self.assertNotIn("--effort", command.argv)

    def test_approval_defaults_uses_bypass_permissions(self) -> None:
        result = self.adapter.approval_defaults()
        self.assertIsInstance(result, dict)
        self.assertEqual(result["permission_mode"], "bypassPermissions")
        self.assertEqual(result["flag"], "--permission-mode bypassPermissions")
        self.assertEqual(result["tool_approval"], "bypass-permissions")

    def test_spawn_subagent_returns_dict(self) -> None:
        result = self.adapter.spawn_subagent("specialist", {"model": "claude-opus-4"})
        self.assertIsInstance(result, dict)
        self.assertEqual(result["harness"], "claude")
        self.assertIn("role", result)

    def test_legacy_inject_inbound_does_not_raise(self) -> None:
        # The compatibility shim may return no value; typed callers inspect the
        # DeliveryReceipt returned by deliver_inbound().
        try:
            self.adapter.inject_inbound("test message")
        except NotImplementedError:
            self.fail("ClaudeHarnessAdapter.inject_inbound should not raise")


# ---------------------------------------------------------------------------
# 5b. T-4037 — mid-session (no-baseline) subscription-cap classification
# ---------------------------------------------------------------------------

class TestClaudeMidSessionCap(unittest.TestCase):
    def setUp(self) -> None:
        self.session_cap_text = (
            Path(__file__).resolve().parent / "testdata" / "orchestrator_cap_modal_pane.txt"
        ).read_text(encoding="utf-8")
        self.spend_limit_text = (
            Path(__file__).resolve().parent / "testdata" / "orchestrator_spend_limit_modal_pane.txt"
        ).read_text(encoding="utf-8")

    def test_session_limit_modal_classified_without_baseline(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        modal = classify_claude_mid_session_cap(self.session_cap_text, now=now)
        self.assertIsNotNone(modal)
        self.assertEqual(modal.modal_kind, "session-limit")
        self.assertEqual(modal.reset_source, "parsed")
        # "resets 2:50pm (UTC)" observed at noon UTC -> later today.
        self.assertEqual(
            modal.reset_at,
            datetime(2026, 9, 27, 14, 50, 0, tzinfo=timezone.utc),
        )

    def test_spend_limit_modal_classified_with_dated_reset(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        modal = classify_claude_mid_session_cap(self.spend_limit_text, now=now)
        self.assertIsNotNone(modal)
        self.assertEqual(modal.modal_kind, "monthly-spend-limit")
        self.assertEqual(modal.reset_source, "parsed")
        self.assertEqual(
            modal.reset_at,
            datetime(2026, 9, 27, 17, 0, 0, tzinfo=timezone.utc),
        )

    def test_spend_limit_modal_with_background_task_status_tail_is_classified(self) -> None:
        """T-5744/T-5757: Claude may render one task-status line below an
        open modal. It is UI chrome, not conversational prose."""
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        text = self.spend_limit_text + "\nBackground task update waiting while this panel is open\n"
        modal = classify_claude_mid_session_cap(text, now=now)
        self.assertIsNotNone(modal)
        self.assertEqual(modal.modal_kind, "monthly-spend-limit")

    def test_spend_limit_modal_with_arbitrary_trailing_prose_is_not_classified(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        text = self.spend_limit_text + "\nThis prose quotes the modal above.\n"
        self.assertIsNone(classify_claude_mid_session_cap(text, now=now))

    def test_prompt_echoed_cap_text_is_not_classified(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        text = "User prompt: You've hit your weekly limit · resets 2:50pm (UTC)\n"
        self.assertIsNone(classify_claude_mid_session_cap(text, now=now))

    def test_broad_limit_mention_without_modal_framing_is_not_classified(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.assertIsNone(
            classify_claude_mid_session_cap("weekly limit reached somewhere in docs\n", now=now)
        )

    def test_partial_spend_limit_framing_without_options_is_not_classified(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        text = "You've hit your monthly spend limit, contact support.\n"
        self.assertIsNone(classify_claude_mid_session_cap(text, now=now))

    def test_empty_text_is_not_classified(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.assertIsNone(classify_claude_mid_session_cap("", now=now))

    # -- T-4037 REBOUND Finding 1: chrome, not framing-phrases alone --------

    def test_prose_quoting_session_variant_framing_without_chrome_is_not_classified(self) -> None:
        """Finding 1 test (a): prose that quotes the sentinel line AND every
        OLD framing phrase (`_has_claude_rate_limit_modal_framing`'s four
        substrings) for the session/weekly/opus variant, but never
        reproduces the live modal's numbered-option-list + footer chrome,
        must NOT classify."""
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        text = (
            "Reviewing the T-4037 false positive: a captured pane read\n"
            "  You've hit your session limit · resets 2:50pm (UTC)\n"
            "and the classifier also keys on /rate-limit-options plus the phrases\n"
            "\"what do you want to do?\", \"stop and wait for limit to reset\", and\n"
            "\"upgrade your plan\" -- none of which means a modal is actually open.\n"
        )
        self.assertIsNone(classify_claude_mid_session_cap(text, now=now))

    def test_prose_quoting_spend_variant_framing_without_chrome_is_not_classified(self) -> None:
        """Same as above, spend-limit variant: quotes the OLD three-substring
        framing set (pre-fix `_has_claude_spend_limit_modal_framing`) without
        the live modal's credit-balance / adjust-limit / footer chrome. Each
        phrase stays contiguous (not line-wrapped) so it genuinely satisfies
        the old substring check -- confirmed to classify (wrongly) pre-fix."""
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        text = (
            "The false positive: any prose saying \"you've hit your monthly spend limit\" and asking\n"
            "\"what do you want to do?\" and mentioning \"wait for limit to reset\" would\n"
            "previously classify -- even this sentence, quoted for illustration.\n"
        )
        self.assertIsNone(classify_claude_mid_session_cap(text, now=now))

    def test_indented_quote_of_modal_opening_without_footer_or_options_is_not_classified(self) -> None:
        """Finding 1 test (b): an indented/quoted block mimicking the
        modal's FIRST lines (sentinel + "What do you want to do?" opening)
        but stopping short of the live footer/options chrome -- both
        variants."""
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        session_text = (
            "    You've hit your session limit · resets 2:50pm (UTC)\n"
            "    What do you want to do?\n"
        )
        spend_text = (
            "    You've hit your monthly spend limit · your weekly limit"
            " resets Sep 27, 5pm (UTC)\n"
            "    What do you want to do?\n"
        )
        self.assertIsNone(classify_claude_mid_session_cap(session_text, now=now))
        self.assertIsNone(classify_claude_mid_session_cap(spend_text, now=now))

    def test_real_fixtures_still_classified_with_correct_modal_kind(self) -> None:
        """Finding 1 test (c): both real captures still detect, chrome and
        all -- redundant with (but explicit alongside) the pre-existing
        baseline tests above, which must also stay green under the
        chrome-tightened checks. The session fixture ends with a trailing
        blank line on disk (orchestrator_cap_modal_pane.txt); the spend fixture does
        not -- between the two, both "with" and "without a trailing blank
        line" are exercised for the round-2 footer-is-final-line check."""
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        session_modal = classify_claude_mid_session_cap(self.session_cap_text, now=now)
        self.assertIsNotNone(session_modal)
        self.assertEqual(session_modal.modal_kind, "session-limit")
        spend_modal = classify_claude_mid_session_cap(self.spend_limit_text, now=now)
        self.assertIsNotNone(spend_modal)
        self.assertEqual(spend_modal.modal_kind, "monthly-spend-limit")

    # -- T-4037 REBOUND round 2 Finding 1 + 2: footer-position + glyph -------

    def test_render_span_sha256_is_present_and_deterministic(self) -> None:
        """`render_span_sha256` (T-4037 REBOUND round 2 Finding 3's debounce
        input) must be a non-empty, deterministic digest of the tight render
        span -- identical across repeat calls on the same text, and different
        between the two structurally different real fixtures."""
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        first = classify_claude_mid_session_cap(self.session_cap_text, now=now)
        second = classify_claude_mid_session_cap(self.session_cap_text, now=now)
        self.assertTrue(first.render_span_sha256)
        self.assertEqual(first.render_span_sha256, second.render_span_sha256)
        spend = classify_claude_mid_session_cap(self.spend_limit_text, now=now)
        self.assertNotEqual(first.render_span_sha256, spend.render_span_sha256)

    def test_render_span_sha256_ignores_churn_above_the_tight_region(self) -> None:
        """The hash must be scoped to the modal's own tight render span, not
        the full pane -- unrelated scrollback ABOVE the modal (e.g. a ticking
        status line) changing between polls must not perturb it, or the
        round-2 Finding 3 debounce could never confirm a real, genuinely
        static, still-open modal."""
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        baseline = classify_claude_mid_session_cap(self.session_cap_text, now=now)
        churned_text = "an entirely different unrelated transcript line\n" + self.session_cap_text
        churned = classify_claude_mid_session_cap(churned_text, now=now)
        self.assertIsNotNone(churned)
        self.assertEqual(baseline.render_span_sha256, churned.render_span_sha256)

    def _repro_fenced_session_quote(self) -> str:
        """The round-2 reviewer's exact repro: a code-fenced, byte-exact
        quote of the session-limit modal, followed by explanatory prose."""
        return (
            "Here's the fixture content I'm reviewing for T-4037:\n"
            "\n"
            "```\n"
            "You've hit your session limit · resets 2:50pm (UTC)\n"
            "\n"
            "What do you want to do?\n"
            "\n"
            "1. Stop and wait for limit to reset\n"
            "2. Upgrade your plan\n"
            "\n"
            "Enter to confirm · Esc to cancel\n"
            "```\n"
            "\n"
            "This is the modal chrome the classifier checks for.\n"
        )

    def _repro_verbatim_spend_quote(self) -> str:
        """The round-2 reviewer's spend-limit variant: verbatim quote, no
        code fence, followed by explanatory prose."""
        return (
            "And the spend-limit variant has the same problem:\n"
            "You've hit your monthly spend limit · your weekly limit resets Sep 27, 5pm\n"
            "   (UTC)\n"
            "What do you want to do?                        Usage credit balance: $8.08\n"
            "Adjust monthly spend limit:        ← or → to adjust · Del to remove limit\n"
            "  Wait for limit to reset                         Resets Sep 27, 5pm (UTC)\n"
            "Enter to confirm · Esc to cancel\n"
            "\n"
            "Same fixture-quoting shape as the session variant above.\n"
        )

    def _repro_no_fence_session_quote(self) -> str:
        """Same session-limit quote as `_repro_fenced_session_quote`, but
        without the surrounding code fence."""
        return (
            "You've hit your session limit · resets 2:50pm (UTC)\n"
            "\n"
            "What do you want to do?\n"
            "\n"
            "1. Stop and wait for limit to reset\n"
            "2. Upgrade your plan\n"
            "\n"
            "Enter to confirm · Esc to cancel\n"
            "\n"
            "This is the modal chrome the classifier checks for.\n"
        )

    def test_reviewer_repro_fenced_session_quote_is_not_classified(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.assertIsNone(
            classify_claude_mid_session_cap(self._repro_fenced_session_quote(), now=now)
        )

    def test_reviewer_repro_verbatim_spend_quote_is_not_classified(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.assertIsNone(
            classify_claude_mid_session_cap(self._repro_verbatim_spend_quote(), now=now)
        )

    def test_reviewer_repro_no_fence_session_quote_is_not_classified(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.assertIsNone(
            classify_claude_mid_session_cap(self._repro_no_fence_session_quote(), now=now)
        )

    def test_reviewer_repros_followed_by_realistic_input_box_tail_are_not_classified(
        self,
    ) -> None:
        """Each of the three repros, followed by the Claude Code UI's own
        always-rendered input-box/prompt/status-line tail (what a REAL pane
        capture carries once the modal has actually closed) -- still None."""
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        realistic_tail = (
            "\n"
            "────────────────────\n"
            "> \n"
            "────────────────────\n"
            "  ? for shortcuts                          Context left until auto-compact: 42%\n"
        )
        for repro in (
            self._repro_fenced_session_quote(),
            self._repro_verbatim_spend_quote(),
            self._repro_no_fence_session_quote(),
        ):
            with self.subTest(repro=repro.splitlines()[0]):
                self.assertIsNone(
                    classify_claude_mid_session_cap(repro + realistic_tail, now=now)
                )

    def test_footer_as_last_content_without_glyph_adjacency_is_not_classified(self) -> None:
        """Finding 2: a quote where the modal's confirm/cancel footer IS the
        pane's absolute last content (passes the round-2 Finding 1 gate) but
        the option lines lack the "❯" selector glyph immediately before them
        -- must still not classify. Both variants."""
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        session_text = (
            "You've hit your session limit · resets 2:50pm (UTC)\n"
            "\n"
            "What do you want to do?\n"
            "\n"
            "1. Stop and wait for limit to reset\n"
            "2. Upgrade your plan\n"
            "\n"
            "Enter to confirm · Esc to cancel\n"
        )
        spend_text = (
            "You've hit your monthly spend limit · your weekly limit resets Sep 27, 5pm\n"
            "   (UTC)\n"
            "What do you want to do?                        Usage credit balance: $8.08\n"
            "Adjust monthly spend limit:        ← or → to adjust · Del to remove limit\n"
            "  Wait for limit to reset                         Resets Sep 27, 5pm (UTC)\n"
            "Enter to confirm · Esc to cancel\n"
        )
        self.assertIsNone(classify_claude_mid_session_cap(session_text, now=now))
        self.assertIsNone(classify_claude_mid_session_cap(spend_text, now=now))


class TestParseClaudeMidSessionCapResetAt(unittest.TestCase):
    def test_bare_form_same_day(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            _parse_claude_mid_session_cap_reset_at(
                "You've hit your session limit · resets 2:50pm (UTC)", now=now
            ),
            datetime(2026, 9, 27, 14, 50, 0, tzinfo=timezone.utc),
        )

    def test_bare_form_rolls_to_tomorrow_past_skew_grace(self) -> None:
        now = datetime(2026, 9, 27, 16, 0, 0, tzinfo=timezone.utc)  # well past 2:50pm
        self.assertEqual(
            _parse_claude_mid_session_cap_reset_at(
                "You've hit your session limit · resets 2:50pm (UTC)", now=now
            ),
            datetime(2026, 9, 28, 14, 50, 0, tzinfo=timezone.utc),
        )

    def test_bare_form_within_skew_grace_returns_none(self) -> None:
        now = datetime(2026, 9, 27, 15, 0, 0, tzinfo=timezone.utc)  # 10 min past 2:50pm
        self.assertIsNone(
            _parse_claude_mid_session_cap_reset_at(
                "You've hit your session limit · resets 2:50pm (UTC)", now=now
            )
        )

    def test_dated_form_parses_month_day_time(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            _parse_claude_mid_session_cap_reset_at("Resets Sep 27, 5pm (UTC)", now=now),
            datetime(2026, 9, 27, 17, 0, 0, tzinfo=timezone.utc),
        )

    def test_dated_form_feb_29_past_in_leap_year_returns_none_not_raises(self) -> None:
        """T-4173: a past "Feb 29" in a leap year rolls to next year, which
        has no Feb 29; the docstring's never-raises contract means None
        (unparsed), not a ValueError out of the cap classifier."""
        # T-5801: a reset only days past is now KEPT (not rolled), so the
        # rollover is exercised from far enough past (> half a year).
        now = datetime(2028, 12, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.assertIsNone(
            _parse_claude_mid_session_cap_reset_at("Resets Feb 29, 5pm (UTC)", now=now)
        )

    # -- T-5801: a modal still shown AFTER its reset is not a year away ------

    def test_dated_form_recently_passed_keeps_this_year(self) -> None:
        # Live 2026-11-01 mason capture: re-parsed as 2027-11-01 before T-5801,
        # so cap_modal_reaper read noop:pre-reset on a modal it exists to reap.
        now = datetime(2026, 11, 1, 17, 35, 0, tzinfo=timezone.utc)
        self.assertEqual(
            _parse_claude_mid_session_cap_reset_at("Resets Nov 1, 5pm (UTC)", now=now),
            datetime(2026, 11, 1, 17, 0, 0, tzinfo=timezone.utc),
        )

    def test_dated_form_dec_to_jan_rolls_forward(self) -> None:
        now = datetime(2026, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            _parse_claude_mid_session_cap_reset_at("Resets Jan 1, 5pm (UTC)", now=now),
            datetime(2027, 1, 1, 17, 0, 0, tzinfo=timezone.utc),
        )

    def test_dated_form_dec_reset_shown_in_jan_rolls_back(self) -> None:
        now = datetime(2027, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            _parse_claude_mid_session_cap_reset_at("Resets Dec 31, 5pm (UTC)", now=now),
            datetime(2026, 12, 31, 17, 0, 0, tzinfo=timezone.utc),
        )

    def test_unparseable_text_returns_none(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.assertIsNone(_parse_claude_mid_session_cap_reset_at("no reset text here", now=now))

    # -- T-4037 REBOUND Finding 2: day-name reset forms ---------------------

    def test_day_name_bare_form_resolves_to_end_of_day_next_occurrence(self) -> None:
        # 2026-09-28 is a Monday; "resets Sunday" with no time -> the next
        # Sunday, end-of-day, per the documented bare-day-name convention
        # (_resolve_dow_reset: no time given -> 23:59:59 UTC, conservative).
        now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            _parse_claude_mid_session_cap_reset_at(
                "You've hit your weekly limit · resets Sunday", now=now
            ),
            datetime(2026, 10, 4, 23, 59, 59, tzinfo=timezone.utc),
        )

    def test_day_name_abbreviated_with_time_matches_ho_stop_failure_shape(self) -> None:
        # Anchor mirrors tools/test_ho_stop_failure.py::test_day_of_week_prefix's
        # own real capture shape ("resets Mon 12:00am"): 2026-09-27 is a
        # Sunday; observed at noon -> next day, midnight.
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            _parse_claude_mid_session_cap_reset_at("resets Mon 12:00am", now=now),
            datetime(2026, 9, 28, 0, 0, 0, tzinfo=timezone.utc),
        )

    def test_day_name_same_weekday_already_passed_rolls_to_next_week(self) -> None:
        now = datetime(2026, 9, 27, 20, 0, 0, tzinfo=timezone.utc)  # Sunday 8pm
        self.assertEqual(
            _parse_claude_mid_session_cap_reset_at("resets Sun 3pm", now=now),
            datetime(2026, 10, 4, 15, 0, 0, tzinfo=timezone.utc),
        )

    def test_dated_regex_guard_prevents_day_name_swallowing_ambiguous_input(self) -> None:
        """Before the negative-lookahead guard, a day name followed by a
        comma-separated day+hour ("Mon 05, 12pm") satisfied the DATED
        regex's two-number-group shape, captured "Mon" as `month_name`, and
        `_resolve_dated_reset` silently returned None (no fallback to the
        day-name path) since "mon" isn't in `_MONTH_ABBR_TO_NUM`. The guard
        keeps the dated regex from matching at all when the token right after
        "resets " is a day name, so parsing now falls through to the
        day-name path instead of being swallowed."""
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)  # Sunday noon
        result = _parse_claude_mid_session_cap_reset_at("resets Mon 05, 12pm", now=now)
        self.assertIsNotNone(result)
        self.assertEqual(result, datetime(2026, 9, 28, 23, 59, 59, tzinfo=timezone.utc))

    def test_dated_form_still_parses_real_month_abbreviations(self) -> None:
        # Regression guard alongside the above: the negative lookahead must
        # never exclude a genuine month (disjoint token sets by construction).
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            _parse_claude_mid_session_cap_reset_at("Resets Sep 27, 5pm (UTC)", now=now),
            datetime(2026, 9, 27, 17, 0, 0, tzinfo=timezone.utc),
        )


# ---------------------------------------------------------------------------
# 6. ClaudeHarnessAdapter — not-abstracted props return dicts (no raise)
# ---------------------------------------------------------------------------

class TestFullUnlockDefaults(unittest.TestCase):
    """T-4929: every harness runs fully unlocked; the unix account is the boundary
    (the operator TG 11733/11741/11742; ORG-031 section (ii) as amended)."""

    def test_codex_defaults_are_full_unlock(self) -> None:
        adapter = CodexHarnessAdapter("codex")
        for cfg in (adapter.approval_defaults(), adapter.sandbox_config):
            self.assertEqual(cfg["sandbox_mode"], "danger-full-access")
            self.assertEqual(cfg["approval_policy"], "never")
            self.assertNotIn("sandbox_workspace_write", cfg)
        self.assertEqual(adapter.approval_defaults()["mcp_tool_approval"], "approve")
        self.assertIn("bypass-hook-trust", adapter.approval_defaults()["hook_trust"])

    def test_claude_default_is_still_default_mode_pending_followup(self) -> None:
        # Deliberately pins the CURRENT value: flipping Claude to bypassPermissions
        # changes every live Claude seat's launch mode and is split into its own
        # follow-up ticket T-4935. Update this pin there.
        adapter = ClaudeHarnessAdapter("orchestrator")
        self.assertEqual(adapter.approval_defaults()["permission_mode"], "bypassPermissions")


class TestClaudeNotAbstractedProps(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ClaudeHarnessAdapter("orchestrator")

    def test_sandbox_config_returns_dict(self) -> None:
        result = self.adapter.sandbox_config
        self.assertIsInstance(result, dict)

    def test_profiles_config_returns_dict(self) -> None:
        result = self.adapter.profiles_config
        self.assertIsInstance(result, dict)

    def test_app_server_config_reports_native_remote_control(self) -> None:
        result = self.adapter.app_server_config
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("supported"))
        self.assertIn("remote-control", result.get("surfaces", []))

    def test_cloud_config_returns_dict_with_supported_false(self) -> None:
        result = self.adapter.cloud_config
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("supported", True), "no claude-code analog → supported=False")

    def test_doctor_config_returns_dict(self) -> None:
        result = self.adapter.doctor_config
        self.assertIsInstance(result, dict)


# ---------------------------------------------------------------------------
# 7. CodexHarnessAdapter — key/filename binding translations
# ---------------------------------------------------------------------------

class TestCodexTranslations(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = CodexHarnessAdapter(
            "specialist",
            codex_home=Path("/tmp/fake-codex-home"),
        )

    def test_standing_prompt_filename(self) -> None:
        self.assertEqual(self.adapter.standing_prompt_filename(), "AGENTS.md")

    def test_mcp_server_config_path_ends_with_config_toml(self) -> None:
        p = self.adapter.mcp_server_config_path()
        self.assertEqual(p.name, "config.toml")

    def test_hooks_config_path_ends_with_hooks_json(self) -> None:
        p = self.adapter.hooks_config_path()
        self.assertEqual(p.name, "hooks.json")

    def test_exec_headless_returns_codex_exec_prefix(self) -> None:
        args = self.adapter.exec_headless("hello world")
        self.assertEqual(args[0], "codex")
        self.assertIn("exec", args)
        self.assertIn("hello world", args)

    def test_exec_headless_returns_list_of_strings(self) -> None:
        args = self.adapter.exec_headless("some prompt")
        self.assertIsInstance(args, list)
        self.assertTrue(all(isinstance(a, str) for a in args))

    def test_auth_status_returns_oauth_shape(self) -> None:
        result = self.adapter.auth_status()
        self.assertIsInstance(result, dict)
        self.assertEqual(result["harness"], "codex")
        self.assertEqual(result.get("key_source"), "oauth")
        # Must document the OAuth→image_gen capability delta
        combined = str(result)
        self.assertIn("oauth", combined.lower())

    def test_approval_defaults_has_approval_policy_never(self) -> None:
        result = self.adapter.approval_defaults()
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("approval_policy"), "never")

    def test_approval_defaults_has_sandbox_mode(self) -> None:
        result = self.adapter.approval_defaults()
        self.assertIn("sandbox_mode", result)
        self.assertEqual(result["sandbox_mode"], "danger-full-access")

    def test_approval_defaults_has_no_workspace_write_table(self) -> None:
        # danger-full-access has no workspace-write sub-config (T-4929).
        self.assertNotIn("sandbox_workspace_write", self.adapter.approval_defaults())
        self.assertNotIn("sandbox_workspace_write", self.adapter.sandbox_config)

    def test_spawn_subagent_returns_dict(self) -> None:
        result = self.adapter.spawn_subagent("specialist", {})
        self.assertIsInstance(result, dict)
        self.assertEqual(result["harness"], "codex")

    def test_build_interactive_launch_appends_hook_trust_bypass_flag(self) -> None:
        # T-2597 wedge #6: --dangerously-bypass-hook-trust is CLI-only (not a
        # config.toml key), so it must be appended on every interactive
        # launch rather than pre-seeded.
        command = self.adapter.build_interactive_launch(
            TmuxTarget(session="pantheon-test", window="harness", pane="0"),
            cwd=Path("/tmp/launch"),
        )
        self.assertIn("--dangerously-bypass-hook-trust", command.argv)


# ---------------------------------------------------------------------------
# 8. CodexHarnessAdapter — honest seams (resume_session + inject_inbound)
# ---------------------------------------------------------------------------

class TestCodexLifecycleSeams(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = CodexHarnessAdapter("specialist")

    def test_resume_session_returns_current_exec_resume_descriptor(self) -> None:
        result = self.adapter.resume_session("some-session-id")
        self.assertEqual(
            result["cli_args"],
            ["exec", "resume", "--json", "some-session-id"],
        )

    def test_build_resume_uses_stdin_and_explicit_session(self) -> None:
        spec = self.adapter.build_resume(
            SessionHandle(
                session_id="abc",
                name=None,
                cwd=Path("/tmp"),
                resumable=True,
            ),
            "continue",
        )
        self.assertIn("resume", spec.argv)
        self.assertIn("abc", spec.argv)
        self.assertNotIn("continue", spec.argv)
        self.assertEqual(spec.stdin, "continue")

    def test_delivery_without_mailbox_is_explicit_rejection(self) -> None:
        receipt = self.adapter.deliver_inbound(
            InboundRequest(
                message="hello",
                correlation_id="c-1",
                source="test",
                reply_route={},
            )
        )
        self.assertEqual(receipt.state, "rejected")

    def test_delivery_to_mailbox_is_durably_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "recovery"
            mailbox.provision(inbox)
            adapter = CodexHarnessAdapter(
                "recovery",
                mailbox_path=inbox,
            )
            receipt = adapter.deliver_inbound(
                InboundRequest(
                    message="hello",
                    correlation_id="c-2",
                    source="test",
                    reply_route={},
                )
            )
            self.assertEqual(receipt.state, "durably_queued")
            self.assertTrue(receipt.evidence.get("message_id"))


# ---------------------------------------------------------------------------
# 9. CodexHarnessAdapter — not-abstracted props return dicts
# ---------------------------------------------------------------------------

class TestCodexNotAbstractedProps(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = CodexHarnessAdapter("specialist")

    def test_sandbox_config_returns_dict(self) -> None:
        result = self.adapter.sandbox_config
        self.assertIsInstance(result, dict)

    def test_profiles_config_returns_dict(self) -> None:
        result = self.adapter.profiles_config
        self.assertIsInstance(result, dict)

    def test_app_server_config_returns_dict(self) -> None:
        result = self.adapter.app_server_config
        self.assertIsInstance(result, dict)

    def test_cloud_config_returns_dict(self) -> None:
        result = self.adapter.cloud_config
        self.assertIsInstance(result, dict)

    def test_doctor_config_returns_dict(self) -> None:
        result = self.adapter.doctor_config
        self.assertIsInstance(result, dict)


# ---------------------------------------------------------------------------
# 10. Cross-binding: standing_prompt_filename differs between harnesses
# ---------------------------------------------------------------------------

class TestCrossHarnessPartition(unittest.TestCase):
    def test_standing_prompt_filenames_differ(self) -> None:
        claude = ClaudeHarnessAdapter("orchestrator")
        codex = CodexHarnessAdapter("specialist")
        self.assertNotEqual(
            claude.standing_prompt_filename(),
            codex.standing_prompt_filename(),
            "CLAUDE.md vs AGENTS.md must differ",
        )

    def test_mcp_config_paths_differ(self) -> None:
        claude = ClaudeHarnessAdapter("orchestrator")
        codex = CodexHarnessAdapter("specialist")
        self.assertNotEqual(
            claude.mcp_server_config_path().name,
            codex.mcp_server_config_path().name,
            "settings.json vs config.toml must differ",
        )

    def test_exec_headless_cli_name_differs(self) -> None:
        claude = ClaudeHarnessAdapter("orchestrator")
        codex = CodexHarnessAdapter("specialist")
        self.assertNotEqual(
            claude.exec_headless("p")[0],
            codex.exec_headless("p")[0],
            "claude vs codex binary name must differ",
        )


# --------------------------------------------------------------------------- #
# 11. ensure_project_trusted (T-2594) — the TRUST-modal-safety config lever. #
# --------------------------------------------------------------------------- #


class EnsureProjectTrustedTest(unittest.TestCase):
    def test_absent_config_creates_file_with_trusted_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "nonexistent" / ".codex"
            cwd = Path("/home/claude-fallback/pantheon-config")
            modified = ensure_project_trusted(codex_home, cwd)
            self.assertTrue(modified)
            config_path = codex_home / "config.toml"
            self.assertTrue(config_path.exists())
            parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["projects"][str(cwd)]["trust_level"], "trusted")

    def test_unrelated_existing_content_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            config_path = codex_home / "config.toml"
            config_path.write_text(
                'approval_policy = "never"\nsandbox_mode = "workspace-write"\n',
                encoding="utf-8",
            )
            cwd = Path("/home/claude-fallback/pantheon-config")
            modified = ensure_project_trusted(codex_home, cwd)
            self.assertTrue(modified)
            text = config_path.read_text(encoding="utf-8")
            self.assertIn('approval_policy = "never"', text)
            self.assertIn('sandbox_mode = "workspace-write"', text)
            parsed = tomllib.loads(text)
            self.assertEqual(parsed["approval_policy"], "never")
            self.assertEqual(parsed["projects"][str(cwd)]["trust_level"], "trusted")

    def test_already_trusted_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            cwd = Path("/home/claude-fallback/pantheon-config")
            self.assertTrue(ensure_project_trusted(codex_home, cwd))
            config_path = codex_home / "config.toml"
            before = config_path.read_bytes()
            modified_again = ensure_project_trusted(codex_home, cwd)
            self.assertFalse(modified_again)
            self.assertEqual(config_path.read_bytes(), before)

    def test_existing_non_trusted_value_is_left_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            cwd = Path("/home/claude-fallback/pantheon-config")
            config_path = codex_home / "config.toml"
            config_path.write_text(
                f'[projects."{cwd}"]\ntrust_level = "untrusted"\n', encoding="utf-8",
            )
            before = config_path.read_bytes()
            modified = ensure_project_trusted(codex_home, cwd)
            self.assertFalse(modified)
            self.assertEqual(config_path.read_bytes(), before)

    def test_malformed_existing_toml_raises_instead_of_blind_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            config_path = codex_home / "config.toml"
            config_path.write_text("this is not [valid toml\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ensure_project_trusted(codex_home, Path("/home/claude-fallback/pantheon-config"))

    def test_path_needing_toml_escaping_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            cwd = Path(tmp) / 'weird"quote' / "pantheon-config"
            modified = ensure_project_trusted(codex_home, cwd)
            self.assertTrue(modified)
            config_path = codex_home / "config.toml"
            parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["projects"][str(cwd)]["trust_level"], "trusted")


# --------------------------------------------------------------------------- #
# 11b. gc_stale_project_trust_entries (T-2600) — prunes dead [projects.*]     #
#      entries so config.toml stops growing unboundedly across bench runs.   #
# --------------------------------------------------------------------------- #


class PruneStaleProjectEntriesTest(unittest.TestCase):
    """Tests against the pure, I/O-free core: text in, (text, count) out."""

    def test_mixed_live_and_dead_preserves_everything_else_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_a = Path(tmp) / "live_a"
            live_b = Path(tmp) / "live_b"
            live_a.mkdir()
            live_b.mkdir()
            dead_a = Path(tmp) / "dead_a"  # never created -- doesn't exist
            dead_b = Path(tmp) / "dead_b"

            original = (
                'approval_policy = "never"\n'
                'sandbox_mode = "workspace-write"\n'
                'personality = "pragmatic"\n'
                "\n"
                "check_for_update_on_startup = false\n"
                "[sandbox_workspace_write]\n"
                "network_access = true\n"
                "\n"
                "[tui.model_availability_nux]\n"
                '"gpt-5.5" = 4\n'
                "\n"
                f'[projects."{live_a}"]\n'
                'trust_level = "trusted"\n'
                "\n"
                f'[projects."{dead_a}"]\n'
                'trust_level = "trusted"\n'
                "\n"
                f'[projects."{live_b}"]\n'
                'trust_level = "trusted"\n'
                "\n"
                f'[projects."{dead_b}"]\n'
                'trust_level = "trusted"\n'
            )
            expected = (
                'approval_policy = "never"\n'
                'sandbox_mode = "workspace-write"\n'
                'personality = "pragmatic"\n'
                "\n"
                "check_for_update_on_startup = false\n"
                "[sandbox_workspace_write]\n"
                "network_access = true\n"
                "\n"
                "[tui.model_availability_nux]\n"
                '"gpt-5.5" = 4\n'
                "\n"
                f'[projects."{live_a}"]\n'
                'trust_level = "trusted"\n'
                "\n"
                f'[projects."{live_b}"]\n'
                'trust_level = "trusted"\n'
            )
            new_text, pruned = _prune_stale_project_entries(original)
            self.assertEqual(pruned, 2)
            self.assertEqual(new_text, expected)

    def test_existing_path_is_never_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "live"
            live.mkdir()
            text = f'[projects."{live}"]\ntrust_level = "trusted"\n'
            new_text, pruned = _prune_stale_project_entries(text)
            self.assertEqual(pruned, 0)
            self.assertEqual(new_text, text)

    def test_no_dead_entries_is_idempotent_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "live"
            live.mkdir()
            text = (
                'approval_policy = "never"\n\n'
                f'[projects."{live}"]\n'
                'trust_level = "trusted"\n'
            )
            new_text, pruned = _prune_stale_project_entries(text)
            self.assertEqual(pruned, 0)
            self.assertEqual(new_text, text)

    def test_malformed_toml_raises_instead_of_guessing(self) -> None:
        with self.assertRaises(RuntimeError):
            _prune_stale_project_entries("this is not [valid toml\n")

    def test_empty_config_is_a_no_op(self) -> None:
        new_text, pruned = _prune_stale_project_entries("")
        self.assertEqual(pruned, 0)
        self.assertEqual(new_text, "")

    def test_no_projects_table_at_all_is_a_no_op(self) -> None:
        text = 'approval_policy = "never"\n'
        new_text, pruned = _prune_stale_project_entries(text)
        self.assertEqual(pruned, 0)
        self.assertEqual(new_text, text)

    def test_dead_entry_with_extra_hand_added_keys_is_fully_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "live"
            live.mkdir()
            dead = Path(tmp) / "dead"
            text = (
                f'[projects."{dead}"]\n'
                'trust_level = "trusted"\n'
                'nickname = "scratch"\n'
                "\n"
                f'[projects."{live}"]\n'
                'trust_level = "trusted"\n'
            )
            # The removed (dead) block owns only ITS OWN preceding blank line
            # (there isn't one -- dead is first in the fixture); the blank
            # line between dead and live belongs to live's own leading
            # newline (per ensure_project_trusted's `f'\n[projects....'`
            # append shape) and survives untouched.
            expected = f'\n[projects."{live}"]\ntrust_level = "trusted"\n'
            new_text, pruned = _prune_stale_project_entries(text)
            self.assertEqual(pruned, 1)
            self.assertEqual(new_text, expected)

    def test_adjacent_dead_entries_boundary_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "live"
            live.mkdir()
            dead_a = Path(tmp) / "dead_a"
            dead_b = Path(tmp) / "dead_b"
            text = (
                f'[projects."{live}"]\n'
                'trust_level = "trusted"\n'
                "\n"
                f'[projects."{dead_a}"]\n'
                'trust_level = "trusted"\n'
                "\n"
                f'[projects."{dead_b}"]\n'
                'trust_level = "trusted"\n'
            )
            expected = f'[projects."{live}"]\ntrust_level = "trusted"\n'
            new_text, pruned = _prune_stale_project_entries(text)
            self.assertEqual(pruned, 2)
            self.assertEqual(new_text, expected)


class FakeCodexHooks:
    """Fake ``hooks/list`` modelled on codex-cli 0.147.0 as captured live for T-4395 (throwaway
    CODEX_HOMEs, real app-server): key = "<realpath(CODEX_HOME)>/config.toml:pre_tool_use:<g>:<h>";
    currentHash covers the hook definition (the group's matcher and EVERY hook key, e.g.
    statusMessage) but not the path, comments or position; trustStatus is "untrusted" with no
    stored hash, "trusted" when the stored trusted_hash equals currentHash, "modified" when it
    differs; `enabled = false` in the state entry disables it. `real_home` overrides what the
    non-scratch home reports; each scratch read records the definition it was handed."""

    def __init__(self, real_home: Path | None = None):
        self.real_home = real_home
        self.calls: list[tuple[Path, float | None]] = []
        self.scratch_definitions: list[dict] = []
        self.scratch_result = "live"  # or None / "raise"
        self.real_result = "live"  # or None / "raise" / a hash-salt string forcing a mismatch
        self.on_real_read = None  # optional side effect before a real-home read

    @staticmethod
    def definition_hash(command: str, timeout, matcher, extra: dict | None = None, salt: str = "") -> str:
        hook = {"type": "command", "command": command, "timeout": timeout, **(extra or {})}
        return FakeCodexHooks._hash(hook, matcher, salt)

    @staticmethod
    def _hash(hook: dict, matcher, salt: str = "") -> str:
        body = json.dumps([matcher, sorted(hook.items()), salt])
        return "sha256:" + hashlib.sha256(body.encode()).hexdigest()

    def _is_real(self, codex_home) -> bool:
        return self.real_home is not None and os.path.realpath(codex_home) == os.path.realpath(self.real_home)

    def __call__(self, executable, *, codex_home, cwd=None, timeout=15.0):
        self.calls.append((codex_home, timeout))
        real = self._is_real(codex_home)
        if real and self.on_real_read:
            self.on_real_read()
        mode = self.real_result if real else self.scratch_result
        if mode is None:
            return None
        if mode == "raise":
            raise RuntimeError("app-server wedged")
        salt = "" if mode == "live" else mode
        config = Path(os.path.realpath(codex_home)) / "config.toml"
        parsed = tomllib.loads((Path(codex_home) / "config.toml").read_text()) if (Path(codex_home) / "config.toml").exists() else {}
        hooks = parsed.get("hooks") or {}
        if not real:
            for event in ("PreToolUse", "SessionStart", "Stop"):
                for group in hooks.get(event) or []:
                    self.scratch_definitions.append(group)
        out = []
        event_names = {
            "PreToolUse": ("pre_tool_use", "preToolUse"),
            "SessionStart": ("session_start", "sessionStart"),
            "Stop": ("stop", "stop"),
        }
        for event, (key_event, wire_event) in event_names.items():
            for g, group in enumerate(hooks.get(event) or []):
                for h, hook in enumerate(group.get("hooks") or []):
                    key = f"{config}:{key_event}:{g}:{h}"
                    current = self._hash(hook, group.get("matcher"), salt)
                    state = (hooks.get("state") or {}).get(key) or {}
                    stored = state.get("trusted_hash")
                    trust = "untrusted" if not stored else ("trusted" if stored == current else "modified")
                    out.append({"key": key, "eventName": wire_event, "handlerType": "command",
                                "matcher": group.get("matcher"), "command": hook["command"],
                                "timeoutSec": hook.get("timeout"), "sourcePath": str(config),
                                "source": "user", "enabled": state.get("enabled", True), "isManaged": False,
                                "currentHash": current, "trustStatus": trust})
        return out


class LifecycleSourceResolutionTest(unittest.TestCase):
    """T-4990: lifecycle orientation always comes from the bound persona."""

    def test_real_tree_all_eight_codex_roles_resolve_persona_owned_sources(self) -> None:
        import yaml

        repo = Path(__file__).resolve().parents[1]
        manifest = yaml.safe_load((repo / "instance.yaml").read_text(encoding="utf-8"))
        default_harness = manifest["instance"]["harness_default"]
        runtime_roles = manifest["harness_runtime"]["roles"]
        bindings = {
            role: binding
            for role, binding in manifest["roles"].items()
            if binding.get("harness", default_harness) == "codex"
            or any(
                str(item).split("/", 1)[0] == "codex"
                for item in runtime_roles.get(role, {}).get("fallbacks", [])
            )
        }
        expected = {
            "codex": "codex.md",
            "orchestrator": "orchestrator-codex.md",
            "specialist": "specialist-codex.md",
            "recovery": "recovery-codex.md",
            "archivist": "archivist-codex.md",
            "mason": "mason.md",
            "artisan": "artisan.md",
            "watchman": "watchman.md",
        }
        self.assertEqual({binding["persona"] for binding in bindings.values()}, set(expected))
        self.assertEqual(len(bindings), 8)

        with tempfile.TemporaryDirectory() as tmp:
            for role, binding in bindings.items():
                with self.subTest(role=role, persona=binding["persona"]):
                    home = Path(tmp) / binding["account"]
                    home.mkdir()
                    (home / "pantheon-config").symlink_to(repo, target_is_directory=True)
                    source = codex_adapter.lifecycle_source_path(home, binding["persona"])
                    self.assertIsNotNone(source)
                    self.assertTrue(source.is_file())
                    self.assertEqual(
                        source.resolve(),
                        (repo / "pinned-rules" / expected[binding["persona"]]).resolve(),
                    )

        consultant_source = (repo / "pinned-rules" / "codex.md").resolve()
        for persona in ("artisan", "mason", "watchman"):
            self.assertNotEqual(
                (repo / "pinned-rules" / expected[persona]).resolve(),
                consultant_source,
            )


class EnsureLifecycleHooksTest(unittest.TestCase):
    """T-4990: every Codex runtime gets trusted orientation lifecycle hooks."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "seat"
        hook_dir = self.home / "pantheon-config" / "harness" / "codex"
        hook_dir.mkdir(parents=True)
        (hook_dir / "run_codex_session_start_hook.py").write_text("# start\n")
        (hook_dir / "run_codex_stop_hook.py").write_text("# stop\n")
        rules = self.home / "pantheon-config" / "pinned-rules" / "specialist-codex.md"
        rules.parent.mkdir(parents=True)
        rules.write_text("# Specialist\n")
        self.source = rules
        self.codex_home = self.home / ".codex"
        self.config = self.codex_home / "config.toml"
        self.codex = FakeCodexHooks(real_home=self.codex_home)
        pkg = Path(self._tmp.name) / "npm" / "@openai" / "codex"
        (pkg / "bin").mkdir(parents=True)
        (pkg / "bin" / "codex.js").write_text("// launcher\n")
        (pkg / "package.json").write_text('{"version": "0.147.0"}\n')
        self.codex_bin = Path(self._tmp.name) / "codex"
        self.codex_bin.symlink_to(pkg / "bin" / "codex.js")
        self.start = (
            f"/usr/bin/python3 {hook_dir}/run_codex_session_start_hook.py "
            f"--source {self.source}"
        )
        self.stop = f"/usr/bin/python3 {hook_dir}/run_codex_stop_hook.py"

    def _ensure(self, read=None, **kwargs):
        return ensure_lifecycle_hooks(
            self.codex_home, self.home, source_path=self.source,
            executable=self.codex_bin, read_hooks=read or self.codex,
            **kwargs,
        )

    def _parsed(self):
        return tomllib.loads(self.config.read_text())

    def test_empty_config_gets_enabled_trusted_hooks_from_account_clone(self) -> None:
        self.assertTrue(self._ensure())
        parsed = self._parsed()
        self.assertIs(parsed["features"]["hooks"], True)
        [session] = parsed["hooks"]["SessionStart"]
        [start_hook] = session["hooks"]
        self.assertEqual(session["matcher"], "startup|resume|clear|compact")
        self.assertEqual(
            {k: start_hook[k] for k in ("type", "command", "statusMessage", "additionalContextLimit", "timeout")},
            {"type": "command", "command": self.start, "statusMessage": "Loading Codex orientation",
             "additionalContextLimit": 1200, "timeout": 5},
        )
        [stop_group] = parsed["hooks"]["Stop"]
        [stop_hook] = stop_group["hooks"]
        self.assertEqual(stop_hook, {"type": "command", "command": self.stop, "timeout": 5})
        state = parsed["hooks"]["state"]
        self.assertEqual(set(state), {
            f"{self.config}:session_start:0:0", f"{self.config}:stop:0:0",
        })
        self.assertTrue(all(v["trusted_hash"].startswith("sha256:") for v in state.values()))

    def test_current_config_is_idempotent_and_verified_marker_skips_app_server(self) -> None:
        self.assertTrue(self._ensure())
        before = self.config.read_bytes()
        self.codex.calls = []
        self.assertFalse(self._ensure())
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.codex.calls, [])

    def test_stale_known_hooks_are_normalized_without_duplicates(self) -> None:
        self.codex_home.mkdir()
        self.config.write_text(
            "[features]\nhooks = false\n\n"
            "[[hooks.SessionStart]]\nmatcher = \"startup\"\n\n"
            "[[hooks.SessionStart.hooks]]\ntype = \"command\"\n"
            f"command = \"{self.start}\"\ntimeout = 60\n\n"
            "[[hooks.Stop]]\n\n[[hooks.Stop.hooks]]\ntype = \"command\"\n"
            f"command = \"{self.stop}\"\ntimeout = 60\n"
        )
        self.assertTrue(self._ensure())
        parsed = self._parsed()
        self.assertIs(parsed["features"]["hooks"], True)
        self.assertEqual(len(parsed["hooks"]["SessionStart"]), 1)
        self.assertEqual(len(parsed["hooks"]["Stop"]), 1)
        self.assertEqual(parsed["hooks"]["SessionStart"][0]["matcher"], "startup|resume|clear|compact")
        self.assertEqual(parsed["hooks"]["SessionStart"][0]["hooks"][0]["timeout"], 5)
        self.assertEqual(parsed["hooks"]["Stop"][0]["hooks"][0]["timeout"], 5)

    def test_consultant_legacy_paths_are_accepted_and_trusted_not_rewritten(self) -> None:
        legacy_dir = self.home / "codex-telegram"
        legacy_dir.mkdir()
        (legacy_dir / "run_codex_session_start_hook.py").write_text("# legacy start\n")
        (legacy_dir / "run_codex_stop_hook.py").write_text("# legacy stop\n")
        legacy_start = f"/usr/bin/python3 {self.home}/codex-telegram/run_codex_session_start_hook.py"
        legacy_stop = f"/usr/bin/python3 {self.home}/codex-telegram/run_codex_stop_hook.py"
        self.codex_home.mkdir()
        self.config.write_text(
            "[features]\nhooks = true\n\n"
            "[[hooks.SessionStart]]\nmatcher = \"startup|resume|clear|compact\"\n\n"
            "[[hooks.SessionStart.hooks]]\ntype = \"command\"\n"
            f"command = \"{legacy_start}\"\nstatusMessage = \"Loading Codex orientation\"\n"
            "additionalContextLimit = 1200\ntimeout = 5\n\n"
            "[[hooks.Stop]]\n\n[[hooks.Stop.hooks]]\ntype = \"command\"\n"
            f"command = \"{legacy_stop}\"\ntimeout = 5\n"
        )
        self.assertTrue(self._ensure(persona="codex"))
        text = self.config.read_text()
        self.assertIn(legacy_start, text)
        self.assertIn(legacy_stop, text)
        self.assertNotIn(self.start, text)
        self.assertEqual(len(self._parsed()["hooks"]["SessionStart"]), 1)

    def test_non_consultant_legacy_paths_are_migrated_to_the_account_clone(self) -> None:
        legacy_start = f"/usr/bin/python3 {self.home}/codex-telegram/run_codex_session_start_hook.py"
        legacy_stop = f"/usr/bin/python3 {self.home}/codex-telegram/run_codex_stop_hook.py"
        self.codex_home.mkdir()
        self.config.write_text(
            "[features]\nhooks = true\n\n"
            "[[hooks.SessionStart]]\nmatcher = \"startup|resume|clear|compact\"\n\n"
            "[[hooks.SessionStart.hooks]]\ntype = \"command\"\n"
            f"command = \"{legacy_start}\"\nstatusMessage = \"Loading Codex orientation\"\n"
            "additionalContextLimit = 1200\ntimeout = 5\n\n"
            "[[hooks.Stop]]\n\n[[hooks.Stop.hooks]]\ntype = \"command\"\n"
            f"command = \"{legacy_stop}\"\ntimeout = 5\n"
        )
        self.assertTrue(self._ensure(persona="specialist"))
        text = self.config.read_text()
        self.assertIn(self.start, text)
        self.assertIn(self.stop, text)
        self.assertNotIn(legacy_start, text)
        self.assertNotIn(legacy_stop, text)

    def test_missing_entrypoint_skips_without_creating_config(self) -> None:
        (self.home / "pantheon-config" / "harness" / "codex" / "run_codex_stop_hook.py").unlink()
        self.assertFalse(self._ensure())
        self.assertFalse(self.config.exists())
        self.assertEqual(self.codex.calls, [])

    def test_duplicate_known_session_hooks_are_refused_without_mutation(self) -> None:
        self.codex_home.mkdir()
        group = (
            "[[hooks.SessionStart]]\nmatcher = \"startup|resume|clear|compact\"\n\n"
            "[[hooks.SessionStart.hooks]]\ntype = \"command\"\n"
            f"command = \"{self.start}\"\nstatusMessage = \"Loading Codex orientation\"\n"
            "additionalContextLimit = 1200\ntimeout = 5\n\n"
        )
        original = "[features]\nhooks = true\n\n" + group + group
        self.config.write_text(original)
        with self.assertRaisesRegex(RuntimeError, "refusing ambiguity before mutation"):
            self._ensure()
        self.assertEqual(self.config.read_text(), original)
        self.assertEqual(self.codex.calls, [])

    def test_persona_selects_its_own_pinned_source(self) -> None:
        self.assertTrue(ensure_lifecycle_hooks(
            self.codex_home, self.home, persona="specialist", executable=self.codex_bin,
            read_hooks=self.codex,
        ))
        self.assertIn(f"--source {self.source}", self.config.read_text())

    def test_missing_compat_source_uses_same_persona_canonical_rules(self) -> None:
        self.source.unlink()
        canonical = self.home / "pantheon-config" / "pinned-rules" / "artisan.md"
        canonical.write_text("# Artisan\n")
        self.assertTrue(ensure_lifecycle_hooks(
            self.codex_home, self.home, persona="artisan", executable=self.codex_bin,
            read_hooks=self.codex,
        ))
        self.assertIn(f"--source {canonical}", self.config.read_text())

    def test_missing_persona_source_refuses_before_config_or_trust_mutation(self) -> None:
        self.source.unlink()
        consultant = self.home / "pantheon-config" / "pinned-rules" / "codex.md"
        consultant.write_text("# Consultant only\n")
        self.assertFalse(ensure_lifecycle_hooks(
            self.codex_home, self.home, persona="artisan", executable=self.codex_bin,
            read_hooks=self.codex,
        ))
        self.assertFalse(self.config.exists())
        self.assertEqual(self.codex.calls, [])

    def test_disabled_hook_state_is_reenabled_and_reverified(self) -> None:
        self.assertTrue(self._ensure())
        text = self.config.read_text()
        stop_key = f"{self.config}:stop:0:0"
        parsed = tomllib.loads(text)
        disabled = codex_adapter._set_hook_state_value(text, parsed, stop_key, "enabled", False)
        self.assertIsNotNone(disabled)
        self.config.write_text(disabled)
        self.assertTrue(self._ensure())
        state = self._parsed()["hooks"]["state"]
        self.assertIs(state[stop_key]["enabled"], True)
        hooks = self.codex(self.codex_bin, codex_home=self.codex_home, cwd=self.home)
        stop = next(h for h in hooks if h["eventName"] == "stop")
        self.assertEqual((stop["enabled"], stop["trustStatus"]), (True, "trusted"))

    def test_failed_post_write_trust_verification_restores_original(self) -> None:
        self.codex_home.mkdir()
        self.config.write_text('model = "x"\n')

        def scratch_only(executable, *, codex_home, cwd=None, timeout=15.0):
            if os.path.realpath(codex_home) == os.path.realpath(self.codex_home):
                return None
            return self.codex(executable, codex_home=codex_home, cwd=cwd, timeout=timeout)

        self.assertFalse(self._ensure(read=scratch_only))
        self.assertEqual(self.config.read_text(), 'model = "x"\n')

    def test_prepare_launch_wires_lifecycle_with_binding_persona(self) -> None:
        binding = unittest.mock.Mock()
        binding.account_home = self.home
        binding.persona = "specialist"
        binding.launch.executable = str(self.codex_bin)
        with unittest.mock.patch.object(codex_adapter, "ensure_project_trusted"), \
                unittest.mock.patch.object(codex_adapter, "ensure_update_check_disabled"), \
                unittest.mock.patch.object(codex_adapter, "ensure_model_migration_prompts_hidden"), \
                unittest.mock.patch.object(codex_adapter, "ensure_worker_guard_hook"), \
                unittest.mock.patch.object(codex_adapter, "ensure_lifecycle_hooks") as lifecycle:
            prepare_codex_launch(binding, cwd=self.home)
        lifecycle.assert_called_once_with(
            self.codex_home, self.home, executable=self.codex_bin, persona="specialist",
        )


class LifecycleWrongSourceRegressionTest(unittest.TestCase):
    """T-4990 REBOUND #2: lifecycle identity cannot coexist or escape by spelling."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(__file__).resolve().parents[1]
        pkg = Path(self._tmp.name) / "npm" / "@openai" / "codex"
        (pkg / "bin").mkdir(parents=True)
        (pkg / "bin" / "codex.js").write_text("// launcher\n")
        (pkg / "package.json").write_text('{"version": "0.147.0"}\n')
        self.codex_bin = Path(self._tmp.name) / "codex"
        self.codex_bin.symlink_to(pkg / "bin" / "codex.js")

    def _seat(self, persona: str, *, seat_name: str | None = None):
        home = Path(self._tmp.name) / f"seat-{seat_name or persona}"
        home.mkdir()
        (home / "pantheon-config").symlink_to(self.repo, target_is_directory=True)
        codex_home = home / ".codex"
        codex_home.mkdir()
        source = codex_adapter.lifecycle_source_path(home, persona)
        self.assertIsNotNone(source)
        self.assertTrue(source.is_file())
        canonical_start, canonical_stop = codex_adapter.lifecycle_hook_commands(home, source)
        wrong_source = home / "pantheon-config" / "pinned-rules" / "codex.md"
        self.assertTrue(wrong_source.is_file())
        repo_start, _legacy_start = codex_adapter._lifecycle_entrypoints(home, "SessionStart")
        wrong_start = f"/usr/bin/python3 {repo_start} --source {wrong_source}"
        fake = FakeCodexHooks(real_home=codex_home)
        return home, codex_home, source, canonical_start, canonical_stop, wrong_start, fake

    @staticmethod
    def _config(starts: list[str], stop: str) -> str:
        groups = []
        for start in starts:
            groups.append(
                "[[hooks.SessionStart]]\nmatcher = \"startup|resume|clear|compact\"\n\n"
                "[[hooks.SessionStart.hooks]]\ntype = \"command\"\n"
                f"command = {json.dumps(start)}\n"
                "statusMessage = \"Loading Codex orientation\"\n"
                "additionalContextLimit = 1200\ntimeout = 5\n\n"
            )
        return (
            "[features]\nhooks = true\n\n" + "".join(groups)
            + "[[hooks.Stop]]\n\n[[hooks.Stop.hooks]]\ntype = \"command\"\n"
            + f"command = {json.dumps(stop)}\ntimeout = 5\n"
        )

    @staticmethod
    def _parsed_commands(config: Path, event: str) -> list[str]:
        parsed = tomllib.loads(config.read_text())
        return [
            hook["command"]
            for group in parsed["hooks"][event]
            for hook in group["hooks"]
        ]

    def test_wrong_source_migrates_for_watchman_artisan_mason(self) -> None:
        for persona in ("watchman", "artisan", "mason"):
            with self.subTest(persona=persona):
                (home, codex_home, _source, canonical_start, canonical_stop,
                 wrong_start, fake) = self._seat(persona)
                config = codex_home / "config.toml"
                config.write_text(self._config([wrong_start], canonical_stop))

                self.assertTrue(ensure_lifecycle_hooks(
                    codex_home, home, executable=self.codex_bin, persona=persona,
                    read_hooks=fake,
                ))

                parsed = tomllib.loads(config.read_text())
                [session] = parsed["hooks"]["SessionStart"]
                [start_hook] = session["hooks"]
                self.assertEqual(start_hook["command"], canonical_start)
                self.assertNotIn(wrong_start, config.read_text())
                hooks = fake(self.codex_bin, codex_home=codex_home, cwd=home)
                family = [h for h in hooks if h["eventName"] == "sessionStart"]
                self.assertEqual(len(family), 1)
                self.assertEqual(
                    (family[0]["command"], family[0]["trustStatus"], family[0]["enabled"]),
                    (canonical_start, "trusted", True),
                )

    def test_coexisting_canonical_and_wrong_source_refuses_before_mutation(self) -> None:
        (home, codex_home, _source, canonical_start, canonical_stop,
         wrong_start, fake) = self._seat("watchman")
        config = codex_home / "config.toml"
        original = self._config([canonical_start, wrong_start], canonical_stop)
        config.write_text(original)

        with self.assertRaisesRegex(RuntimeError, "refusing ambiguity before mutation"):
            ensure_lifecycle_hooks(
                codex_home, home, executable=self.codex_bin, persona="watchman",
                read_hooks=fake,
            )
        self.assertEqual(config.read_text(), original)
        self.assertEqual(fake.calls, [])

    def test_malformed_or_extra_argument_family_candidate_refuses_before_mutation(self) -> None:
        for case in ("missing-source", "malformed-source", "extra-argument", "quoted-malformed"):
            with self.subTest(case=case):
                (home, codex_home, source, _canonical_start, canonical_stop,
                 _wrong_start, fake) = self._seat(
                     "watchman", seat_name=f"watchman-{len(list(Path(self._tmp.name).glob('seat-*')))}",
                 )
                # The synthetic home still uses the real Watchman source/entrypoint;
                # persona is explicit because the directory suffix is test isolation only.
                source = codex_adapter.lifecycle_source_path(home, "watchman")
                repo_start, _legacy = codex_adapter._lifecycle_entrypoints(home, "SessionStart")
                commands = {
                    "missing-source": f"/usr/bin/python3 {repo_start}",
                    "malformed-source": f"/usr/bin/python3 {repo_start} --source \"",
                    "extra-argument": (
                        f"/usr/bin/python3 {repo_start} --source {source} --extra nope"
                    ),
                    "quoted-malformed": f'/usr/bin/python3 "{repo_start}" --source "',
                }
                command = commands[case]
                config = codex_home / "config.toml"
                original = self._config([command], canonical_stop)
                config.write_text(original)

                with self.assertRaisesRegex(RuntimeError, "refusing before mutation"):
                    ensure_lifecycle_hooks(
                        codex_home, home, executable=self.codex_bin, persona="watchman",
                        read_hooks=fake,
                    )
                self.assertEqual(config.read_text(), original)
                self.assertEqual(fake.calls, [])

    def test_unmatched_opening_quote_family_evidence_refuses_without_mutation(self) -> None:
        for label, quote in (("single", "'"), ("double", '"')):
            with self.subTest(quote=label):
                (home, codex_home, source, _canonical_start, canonical_stop,
                 _wrong_start, fake) = self._seat(
                     "watchman", seat_name=f"unmatched-{label}",
                 )
                repo_start, _legacy = codex_adapter._lifecycle_entrypoints(
                    home, "SessionStart",
                )
                malformed = f"/usr/bin/python3 {quote}{repo_start} --source {source}"
                config = codex_home / "config.toml"
                original = self._config([malformed], canonical_stop)
                config.write_text(original)

                with self.assertRaisesRegex(RuntimeError, "refusing before mutation"):
                    ensure_lifecycle_hooks(
                        codex_home, home, executable=self.codex_bin, persona="watchman",
                        read_hooks=fake,
                    )
                self.assertEqual(config.read_text(), original)
                self.assertEqual(fake.calls, [])

    def test_unmatched_opening_quote_coexistence_refuses_ambiguity(self) -> None:
        for label, quote in (("single", "'"), ("double", '"')):
            with self.subTest(quote=label):
                (home, codex_home, source, canonical_start, canonical_stop,
                 _wrong_start, fake) = self._seat(
                     "watchman", seat_name=f"coexist-{label}",
                 )
                repo_start, _legacy = codex_adapter._lifecycle_entrypoints(
                    home, "SessionStart",
                )
                malformed = f"/usr/bin/python3 {quote}{repo_start} --source {source}"
                config = codex_home / "config.toml"
                original = self._config([canonical_start, malformed], canonical_stop)
                config.write_text(original)

                with self.assertRaisesRegex(RuntimeError, "refusing ambiguity before mutation"):
                    ensure_lifecycle_hooks(
                        codex_home, home, executable=self.codex_bin, persona="watchman",
                        read_hooks=fake,
                    )
                self.assertEqual(config.read_text(), original)
                self.assertEqual(fake.calls, [])

    def test_balanced_entrypoint_quotes_migrate_and_strict_boundaries_stay_unrelated(self) -> None:
        for label, quote in (("single", "'"), ("double", '"')):
            with self.subTest(kind="balanced", quote=label):
                (home, codex_home, source, canonical_start, canonical_stop,
                 _wrong_start, fake) = self._seat(
                     "watchman", seat_name=f"balanced-{label}",
                 )
                repo_start, _legacy = codex_adapter._lifecycle_entrypoints(
                    home, "SessionStart",
                )
                balanced = (
                    f"/usr/bin/python3 {quote}{repo_start}{quote} --source {source}"
                )
                config = codex_home / "config.toml"
                config.write_text(self._config([balanced], canonical_stop))

                self.assertTrue(ensure_lifecycle_hooks(
                    codex_home, home, executable=self.codex_bin, persona="watchman",
                    read_hooks=fake,
                ))
                starts = self._parsed_commands(config, "SessionStart")
                self.assertEqual(starts, [canonical_start])

        for label, path_transform in (
            ("prefix", lambda path: path.with_name(f"prefix-{path.name}")),
            ("suffix", lambda path: Path(f"{path}.backup")),
        ):
            with self.subTest(kind="boundary", case=label):
                (home, codex_home, source, canonical_start, canonical_stop,
                 _wrong_start, fake) = self._seat(
                     "watchman", seat_name=f"boundary-{label}",
                 )
                repo_start, _legacy = codex_adapter._lifecycle_entrypoints(
                    home, "SessionStart",
                )
                unrelated = (
                    f"/usr/bin/python3 {path_transform(repo_start)} --source {source}"
                )
                config = codex_home / "config.toml"
                config.write_text(self._config([unrelated], canonical_stop))

                self.assertTrue(ensure_lifecycle_hooks(
                    codex_home, home, executable=self.codex_bin, persona="watchman",
                    read_hooks=fake,
                ))
                self.assertEqual(
                    self._parsed_commands(config, "SessionStart"),
                    [unrelated, canonical_start],
                )

    def test_extra_argument_stop_family_candidate_refuses_before_mutation(self) -> None:
        (home, codex_home, _source, canonical_start, canonical_stop,
         _wrong_start, fake) = self._seat("watchman")
        config = codex_home / "config.toml"
        original = self._config([canonical_start], f"{canonical_stop} --extra nope")
        config.write_text(original)

        with self.assertRaisesRegex(RuntimeError, "refusing before mutation"):
            ensure_lifecycle_hooks(
                codex_home, home, executable=self.codex_bin, persona="watchman",
                read_hooks=fake,
            )
        self.assertEqual(config.read_text(), original)
        self.assertEqual(fake.calls, [])


class EnsureWorkerGuardHookTest(unittest.TestCase):
    """T-4166: every codex runtime gets the worker-guard PreToolUse hook wired + trusted.
    T-4395 / T-4397 A: the block is `python3 -I`, timeout 30, and a stale block is upgraded
    with its new trusted_hash in ONE write, verified, else restored."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "seat"
        (self.home / "pantheon-config" / "tools" / "hooks").mkdir(parents=True)
        (self.home / "pantheon-config" / "tools" / "hooks" / "worker-guard.py").write_text("# guard\n")
        (self.home / "pantheon-config" / "tools" / "hooks" / "worker-guard-v2.py").write_text("# guard v2\n")
        self.codex_home = self.home / ".codex"
        self.config = self.codex_home / "config.toml"
        self.codex = FakeCodexHooks(real_home=self.codex_home)
        self.cmd = worker_guard_command(self.home)
        self.legacy_cmd = f"/usr/bin/python3 {self.home}/pantheon-config/tools/hooks/worker-guard.py"
        self.new_hash = FakeCodexHooks.definition_hash(self.cmd, 30, "Bash|apply_patch")
        self.old_hash = FakeCodexHooks.definition_hash(self.legacy_cmd, 10, "Bash|apply_patch")
        # A hermetic fake codex install (npm layout: bin/codex.js + package.json): the T-4428
        # verified-marker keys on its identity, so tests never depend on the host's codex.
        self.codex_pkg = Path(self._tmp.name) / "npm" / "@openai" / "codex"
        (self.codex_pkg / "bin").mkdir(parents=True)
        (self.codex_pkg / "bin" / "codex.js").write_text("// launcher\n")
        (self.codex_pkg / "package.json").write_text('{"version": "0.147.0"}\n')
        self.codex_bin = Path(self._tmp.name) / "codex"
        self.codex_bin.symlink_to(self.codex_pkg / "bin" / "codex.js")
        self.writes: list[str] = []
        real_write = codex_adapter._write_config_preserving_mode

        def spy(path, text):
            self.writes.append(text)
            return real_write(path, text)

        patcher = unittest.mock.patch.object(codex_adapter, "_write_config_preserving_mode", spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ensure(self, read=None, **kw):
        self.stderr = io.StringIO()
        with contextlib.redirect_stderr(self.stderr):
            return ensure_worker_guard_hook(self.codex_home, self.home, executable=self.codex_bin,
                                            read_hooks=read or self.codex, **kw)

    def _real_calls(self):
        return [c for c, _t in self.codex.calls if Path(c) == self.codex_home]

    def _guard_state(self):
        parsed = tomllib.loads(self.config.read_text())
        [group] = [g for g in parsed["hooks"]["PreToolUse"]
                   if any(h["command"] == self.cmd for h in g["hooks"])]
        self.assertEqual(group["matcher"], "Bash|apply_patch")
        return parsed["hooks"].get("state") or {}

    def _legacy_config(self, *, state_extra: str = "", prefix: str = 'model = "x"\n', suffix: str = "",
                       matcher: str | None = "Bash|apply_patch", hook_extra: str = ""):
        """A seat as T-4160/T-4166 left it: the pre-T-4395 block (codex-config da8c863) + its trust
        (the hash codex reports for exactly this definition)."""
        self.codex_home.mkdir(exist_ok=True)
        matcher_line = f'matcher = "{matcher}"\n' if matcher is not None else ""
        extra = tomllib.loads(hook_extra) if hook_extra else {}
        old_hash = FakeCodexHooks.definition_hash(self.legacy_cmd, 10, matcher, extra)
        text = (f'{prefix}\n# PreToolUse worker-guard (T-4144 / T-4166): comment\n'
                f'[[hooks.PreToolUse]]\n{matcher_line}\n[[hooks.PreToolUse.hooks]]\n'
                f'type = "command"\ncommand = "{self.legacy_cmd}"\n{hook_extra}timeout = 10\n\n'
                f'[hooks.state."{self.config}:pre_tool_use:0:0"]\n{state_extra}'
                f'trusted_hash = "{old_hash}"\n{suffix}')
        self.config.write_text(text)
        self.config.chmod(0o640)
        return text

    # --- rendering (T-4395 / T-4397 A) ---

    def test_rendered_block_is_isolated_interpreter_with_timeout_30(self) -> None:
        block = tomllib.loads(codex_adapter._WORKER_GUARD_BLOCK.format(command=f'"{self.cmd}"'))
        [group] = block["hooks"]["PreToolUse"]
        [hook] = group["hooks"]
        self.assertEqual(hook["timeout"], 30)
        self.assertEqual(group["matcher"], "Bash|apply_patch")
        self.assertTrue(self.cmd.startswith("/usr/bin/python3 -I "), self.cmd)
        # codex_hook_trust_check recognises the guard by this suffix; -I must not break it.
        self.assertTrue(self.cmd.endswith("/pantheon-config/tools/hooks/worker-guard-v2.py"))

    # --- fresh seat (T-4166, now one write) ---

    def test_fresh_seat_gets_the_guard_wired_and_trusted_in_one_write_0600(self) -> None:
        self.assertTrue(self._ensure())
        state = self._guard_state()
        self.assertEqual(state[f"{self.config}:pre_tool_use:0:0"], {"trusted_hash": self.new_hash})
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self._real_calls(), [], "the hash comes from a scratch home, not the real one")

    def test_second_run_is_a_noop_one_bounded_real_read_no_write(self) -> None:
        # T-4395 r1: a current block is confirmed with codex itself (so a STALE hash is caught
        # and repaired), costing one bounded hooks/list on the real home and nothing else.
        self._ensure()
        before, self.codex.calls, self.writes = self.config.read_bytes(), [], []
        self.assertFalse(self._ensure())
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.codex.calls, [(self.codex_home, codex_adapter._HOOKS_LIST_TIMEOUT_S)])
        self.assertEqual(self.writes, [])

    def test_existing_content_and_mode_are_preserved(self) -> None:
        self.codex_home.mkdir()
        original = 'model = "x"\n\n[projects."/tmp"]\ntrust_level = "trusted"\n'
        self.config.write_text(original)
        self.config.chmod(0o664)
        self._ensure()
        self.assertTrue(self.config.read_text().startswith(original))
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o664)
        self.assertEqual(tomllib.loads(self.config.read_text())["model"], "x")

    def test_symlinked_config_stays_a_symlink(self) -> None:
        # The sibling seeders' write_text writes THROUGH a symlink; the atomic replace must too.
        self.codex_home.mkdir()
        real = self.home / "shared-config.toml"
        real.write_text('model = "x"\n')
        self.config.symlink_to(real)
        self._ensure()
        self.assertTrue(self.config.is_symlink())
        self.assertIn(self.cmd, real.read_text())

    def test_guard_after_other_pretooluse_groups_gets_its_own_key(self) -> None:
        self.codex_home.mkdir()
        self.config.write_text('[[hooks.PreToolUse]]\nmatcher = "x"\n\n[[hooks.PreToolUse.hooks]]\n'
                               'type = "command"\ncommand = "/bin/true"\n')
        self._ensure()
        self.assertEqual(self._guard_state()[f"{self.config}:pre_tool_use:1:0"], {"trusted_hash": self.new_hash})

    def test_unparseable_toml_refuses_and_leaves_the_file_alone(self) -> None:
        self.codex_home.mkdir()
        self.config.write_text("[broken\n")
        with self.assertRaises(RuntimeError):
            self._ensure()
        self.assertEqual(self.config.read_text(), "[broken\n")

    def test_app_server_failure_still_wires_the_guard(self) -> None:
        # Untrusted still RUNS under --dangerously-bypass-hook-trust; parity flags it.
        self.assertTrue(self._ensure(read=lambda *a, **k: None))
        self.assertEqual(self._guard_state(), {})
        self.assertIn("UNTRUSTED", self.stderr.getvalue())

    def test_current_block_with_a_hashless_state_entry_is_trusted_in_place_not_duplicated(self) -> None:
        self.codex_home.mkdir()
        self.config.write_text(
            f'[[hooks.PreToolUse]]\nmatcher = "Bash|apply_patch"\n\n[[hooks.PreToolUse.hooks]]\n'
            f'type = "command"\ncommand = "{self.cmd}"\ntimeout = 30\n\n'
            f'[hooks.state."{self.config}:pre_tool_use:0:0"]\nenabled = true\n')
        self.assertTrue(self._ensure())
        self.assertEqual(self._guard_state()[f"{self.config}:pre_tool_use:0:0"],
                         {"enabled": True, "trusted_hash": self.new_hash})
        self.assertEqual(self.config.read_text().count("[hooks.state."), 1)
        self.assertEqual(len(self.writes), 1)

    def test_current_block_without_trust_is_trusted_from_the_real_home(self) -> None:
        self.codex_home.mkdir()
        self.config.write_text(
            f'[[hooks.PreToolUse]]\nmatcher = "Bash|apply_patch"\n\n[[hooks.PreToolUse.hooks]]\n'
            f'type = "command"\ncommand = "{self.cmd}"\ntimeout = 30\n')
        self.assertTrue(self._ensure())
        self.assertEqual(self._guard_state()[f"{self.config}:pre_tool_use:0:0"], {"trusted_hash": self.new_hash})

    def test_seat_without_the_guard_script_is_left_untouched(self) -> None:
        (self.home / "pantheon-config" / "tools" / "hooks" / "worker-guard-v2.py").unlink()
        self.assertFalse(self._ensure())
        self.assertFalse(self.config.exists())
        self.assertEqual(self.codex.calls, [])

    # --- the T-4395 upgrade: stale block + stale trusted_hash ---

    def test_legacy_block_is_upgraded_in_place_with_its_new_hash_in_one_write(self) -> None:
        original = self._legacy_config(suffix='\n[[hooks.Stop]]\n\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "/bin/true"\n')
        self.assertEqual(self.codex(Path("/usr/bin/codex"), codex_home=self.codex_home)[0]["trustStatus"], "trusted")
        self.codex.calls = []
        self.assertTrue(self._ensure())
        key = f"{self.config}:pre_tool_use:0:0"
        # exactly ONE write, and it already carries both the new block and the new hash
        self.assertEqual(len(self.writes), 1)
        first = tomllib.loads(self.writes[0])
        hook = first["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual((hook["command"], hook["timeout"]), (self.cmd, 30))
        self.assertEqual(first["hooks"]["state"][key], {"trusted_hash": self.new_hash})
        # same key, stale hash gone, only the three values changed, mode kept
        text = self.config.read_text()
        self.assertNotIn(self.old_hash, text)
        self.assertEqual(text, original.replace(f'"{self.legacy_cmd}"', f'"{self.cmd}"')
                         .replace("timeout = 10", "timeout = 30").replace(self.old_hash, self.new_hash))
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        [real] = [h for h in self.codex(Path("/usr/bin/codex"), codex_home=self.codex_home) if h["key"] == key]
        self.assertEqual((real["trustStatus"], real["currentHash"]), ("trusted", self.new_hash))
        self.assertIn("UPGRADED", self.stderr.getvalue())
        self.assertIn("verified", self.stderr.getvalue())

    def test_upgrade_is_idempotent(self) -> None:
        self._legacy_config()
        self.assertTrue(self._ensure())
        before, self.codex.calls, self.writes = self.config.read_bytes(), [], []
        self.assertFalse(self._ensure())
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.writes, [])
        self.assertEqual(self._real_calls(), [self.codex_home])
        self.assertNotIn("RESTORED", self.stderr.getvalue())

    def test_upgrade_keeps_sibling_state_keys(self) -> None:
        self._legacy_config(state_extra="enabled = true\n")
        self.assertTrue(self._ensure())
        self.assertEqual(self._guard_state()[f"{self.config}:pre_tool_use:0:0"],
                         {"enabled": True, "trusted_hash": self.new_hash})

    def test_upgrade_of_a_current_command_with_a_stale_timeout(self) -> None:
        self._legacy_config()
        self.config.write_text(self.config.read_text().replace(f'"{self.legacy_cmd}"', f'"{self.cmd}"'))
        self.assertTrue(self._ensure())
        self.assertEqual(self._guard_state()[f"{self.config}:pre_tool_use:0:0"], {"trusted_hash": self.new_hash})
        self.assertIn("timeout = 30", self.config.read_text())

    def test_precompute_failure_leaves_the_legacy_guard_untouched(self) -> None:
        for mode in (None, "raise"):
            with self.subTest(scratch=mode):
                original = self._legacy_config()
                self.codex.scratch_result, self.writes = mode, []
                self.assertFalse(self._ensure())
                self.assertEqual(self.config.read_text(), original)
                self.assertEqual(self.writes, [])
                self.assertEqual(self._real_calls(), [])
                self.assertIn("SKIPPED", self.stderr.getvalue())

    def test_unverifiable_upgrade_restores_the_original_bytes(self) -> None:
        # None = hooks/list failed or TIMED OUT (the reader is bounded); "raise" = it blew up;
        # a salt = codex reports a different hash, so the new block would read "modified".
        for mode in (None, "raise", "other-codex-version"):
            with self.subTest(real=mode):
                original = self._legacy_config()
                self.codex.real_result, self.writes = mode, []
                self.assertFalse(self._ensure())
                self.assertEqual(self.config.read_text(), original)
                self.assertEqual(len(self.writes), 2)  # the upgrade, then the restore
                self.assertEqual(self.writes[1], original)
                self.assertIn("RESTORED", self.stderr.getvalue())

    def test_every_app_server_read_is_bounded(self) -> None:
        self._legacy_config()
        self._ensure()
        self.assertTrue(self.codex.calls)
        for _home, timeout in self.codex.calls:
            self.assertEqual(timeout, codex_adapter._HOOKS_LIST_TIMEOUT_S)
        self.assertLessEqual(codex_adapter._HOOKS_LIST_TIMEOUT_S, 15.0)

    def test_state_in_an_unrecognised_shape_refuses(self) -> None:
        self.codex_home.mkdir()
        original = (f'[[hooks.PreToolUse]]\nmatcher = "Bash|apply_patch"\n\n[[hooks.PreToolUse.hooks]]\n'
                    f'type = "command"\ncommand = "{self.legacy_cmd}"\ntimeout = 10\n\n'
                    f'[hooks.state]\n"{self.config}:pre_tool_use:0:0" = {{ trusted_hash = "{self.old_hash}" }}\n')
        self.config.write_text(original)
        self.assertFalse(self._ensure())
        self.assertEqual(self.config.read_text(), original)
        self.assertIn("REFUSED", self.stderr.getvalue())

    def test_an_edit_that_parses_but_is_not_minimal_is_never_written(self) -> None:
        # The line edit lands inside a multi-line string (a decoy `timeout =` line, then a line that
        # looks like a table header): it parses, but not to "only command/timeout/hash changed".
        self.codex_home.mkdir()
        original = (f'[[hooks.PreToolUse]]\nmatcher = "Bash|apply_patch"\n\n[[hooks.PreToolUse.hooks]]\n'
                    f'type = "command"\ncommand = "{self.legacy_cmd}"\nstatusMessage = """\ntimeout = 99\n[x\n"""\n'
                    f'timeout = 10\n\n[hooks.state."{self.config}:pre_tool_use:0:0"]\n'
                    f'trusted_hash = "{self.old_hash}"\n')
        self.config.write_text(original)
        self.assertFalse(self._ensure())
        self.assertEqual(self.writes, [], "a non-minimal edit must be refused BEFORE any write")
        self.assertEqual(self.config.read_text(), original)
        self.assertIn("REFUSED", self.stderr.getvalue())

    # --- T-4395 r1: the definition codex hashes is the one written ---

    def _assert_trusted_at_first_verify(self):
        log = self.stderr.getvalue()
        self.assertIn("verified", log)
        self.assertNotIn("RESTORED", log)
        self.assertEqual(len(self.writes), 1)

    def test_precompute_input_equals_the_written_definition(self) -> None:
        # A future drift between what is hashed and what is written must fail HERE, not open a
        # "modified" window on a live home.
        for label, kwargs in (("canonical", {}), ("matcher", {"matcher": "Bash"}),
                              ("extra-key", {"hook_extra": 'statusMessage = "guarding"\n'})):
            with self.subTest(case=label):
                self.codex.scratch_definitions, self.writes = [], []
                self._legacy_config(**kwargs)
                self.assertTrue(self._ensure())
                [hashed] = self.codex.scratch_definitions
                [written] = tomllib.loads(self.writes[0])["hooks"]["PreToolUse"]
                self.assertEqual(hashed, written)
                self._assert_trusted_at_first_verify()

    def test_narrow_legacy_matcher_is_normalized_and_trusted_at_first_verify(self) -> None:
        self._legacy_config(matcher="Bash")
        self.assertTrue(self._ensure())
        self._assert_trusted_at_first_verify()
        [group] = tomllib.loads(self.config.read_text())["hooks"]["PreToolUse"]
        self.assertEqual(group["matcher"], "Bash|apply_patch")
        self.assertTrue(self._status()["ok"])

    def test_missing_matcher_is_inserted(self) -> None:
        self._legacy_config(matcher=None)
        self.assertTrue(self._ensure())
        self._assert_trusted_at_first_verify()
        self.assertTrue(self._status()["ok"])

    def test_extra_hook_keys_are_kept_and_hashed(self) -> None:
        self._legacy_config(hook_extra='statusMessage = "guarding"\n')
        self.assertTrue(self._ensure())
        self._assert_trusted_at_first_verify()
        [group] = tomllib.loads(self.config.read_text())["hooks"]["PreToolUse"]
        self.assertEqual(group["hooks"][0]["statusMessage"], "guarding")
        self.assertTrue(self._status()["ok"])

    def test_matcher_change_is_refused_when_the_group_holds_other_hooks(self) -> None:
        # Normalizing the group matcher would silently widen the sibling hook too.
        original = self._legacy_config(
            matcher="Bash", hook_extra="", suffix="")
        original = original.replace(
            "timeout = 10\n", 'timeout = 10\n\n[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = "/bin/true"\n', 1)
        self.config.write_text(original)
        self.assertFalse(self._ensure())
        self.assertEqual((self.config.read_text(), self.writes), (original, []))
        self.assertIn("REFUSED", self.stderr.getvalue())

    def test_symlinked_codex_home_uses_the_realpath_key(self) -> None:
        real = self.home / "real-codex"
        real.mkdir()
        self.codex_home.symlink_to(real)
        self._legacy_config()
        real_key = f"{real / 'config.toml'}:pre_tool_use:0:0"
        text = self.config.read_text().replace(f'"{self.config}:pre_tool_use:0:0"', f'"{real_key}"')
        self.config.write_text(text)
        self.assertEqual(self.codex(Path("/usr/bin/codex"), codex_home=self.codex_home)[0]["trustStatus"], "trusted")
        self.codex.calls, self.writes = [], []
        self.assertTrue(self._ensure())
        self._assert_trusted_at_first_verify()
        state = tomllib.loads(self.config.read_text())["hooks"]["state"]
        self.assertEqual(state, {real_key: {"trusted_hash": self.new_hash}})
        status = self._status()
        self.assertEqual((status["key"], status["ok"]), (real_key, True))

    def test_symlinked_codex_home_fresh_seat_is_trusted_under_the_realpath_key(self) -> None:
        real = self.home / "real-codex"
        real.mkdir()
        self.codex_home.symlink_to(real)
        self.assertTrue(self._ensure())
        self.assertIn(f"{real / 'config.toml'}:pre_tool_use:0:0", self._guard_state())
        self.assertTrue(self._status()["ok"])

    def test_stale_hash_on_a_current_block_is_repaired_in_one_write(self) -> None:
        self._ensure()
        self.config.write_text(self.config.read_text().replace(self.new_hash, "sha256:" + "00" * 32))
        self.assertEqual(self._status()["trust_status"], "modified")
        self.writes = []
        self.assertTrue(self._ensure())
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self._guard_state()[f"{self.config}:pre_tool_use:0:0"], {"trusted_hash": self.new_hash})
        self.assertIn("REPAIRED stale", self.stderr.getvalue())
        self.assertTrue(self._status()["ok"])

    def test_restore_is_compare_and_swap(self) -> None:
        # The verify read fails AND someone rewrote config.toml after our write: leave theirs.
        self._legacy_config()
        concurrent = 'model = "someone-else"\n'
        self.codex.real_result = None
        self.codex.on_real_read = lambda: self.config.write_text(concurrent)
        self.assertTrue(self._ensure())
        self.assertEqual(self.config.read_text(), concurrent)
        self.assertEqual(len(self.writes), 1, "no restore write over a concurrent edit")
        self.assertIn("NOT RESTORED", self.stderr.getvalue())

    # --- T-4395 r2 nits ---

    def _current_untrusted(self) -> str:
        self.codex_home.mkdir(exist_ok=True)
        text = (f'[[hooks.PreToolUse]]\nmatcher = "Bash|apply_patch"\n\n[[hooks.PreToolUse.hooks]]\n'
                f'type = "command"\ncommand = "{self.cmd}"\ntimeout = 30\n')
        self.config.write_text(text)
        return text

    def test_retrust_is_compare_and_swap_against_the_bytes_read(self) -> None:
        self._current_untrusted()
        changed = self.config.read_text() + '\n[projects."/tmp"]\ntrust_level = "trusted"\n'
        self.codex.on_real_read = lambda: self.config.write_text(changed)
        self.assertFalse(self._ensure())
        self.assertEqual(self.writes, [])
        self.assertEqual(self.config.read_text(), changed)
        self.assertIn("NOT WRITTEN", self.stderr.getvalue())

    def test_retrust_requires_codex_to_report_our_command_at_the_key(self) -> None:
        original = self._current_untrusted()
        swapped = original.replace(self.cmd, "/bin/true")
        self.codex.on_real_read = lambda: self.config.write_text(swapped)
        self.assertFalse(self._ensure())
        self.assertEqual(self.writes, [])
        self.assertIn("re-trust REFUSED", self.stderr.getvalue())

    def test_unverifiable_current_guard_is_logged(self) -> None:
        for label, read in (("timeout", lambda *a, **k: None), ("no-hook", lambda *a, **k: [])):
            with self.subTest(case=label):
                self._current_untrusted()
                self.assertFalse(self._ensure(read=read))
                self.assertIn("[worker-guard-hook]", self.stderr.getvalue())
                self.assertIn("unverifiable", self.stderr.getvalue())

    def test_upgrade_of_a_disabled_guard_says_so(self) -> None:
        self._legacy_config(state_extra="enabled = false\n")
        self.assertTrue(self._ensure())
        log = self.stderr.getvalue()
        self.assertIn("verified", log)
        self.assertIn("DISABLED", log)
        self.assertFalse(self._status()["ok"])

    def test_status_requires_the_guard_enabled(self) -> None:
        self._ensure()
        self.config.write_text(self.config.read_text().rstrip("\n") + "\nenabled = false\n")
        status = self._status()
        self.assertEqual((status["trust_status"], status["enabled"], status["ok"]), ("trusted", False, False))

    # --- Recovery's ask (T-4395 r1): ON DISK vs LIVE SESSION ---

    def _fake_proc(self, procs: dict[int, tuple[list[str], dict | None, int]], btime: int = 1000) -> Path:
        root = Path(self._tmp.name) / "proc"
        root.mkdir()
        (root / "stat").write_text(f"cpu  1 2 3\nbtime {btime}\nprocesses 9\n")
        for pid, (argv, env, start_ticks) in procs.items():
            d = root / str(pid)
            d.mkdir()
            (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
            if env is not None:
                (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
            fields = ["S"] + ["0"] * 18 + [str(start_ticks)] + ["0"] * 5
            (d / "stat").write_text(f"{pid} (node codex) " + " ".join(fields) + "\n")
        return root

    def test_live_sessions_split_before_and_after_the_config_write(self) -> None:
        self._ensure()
        tick = os.sysconf("SC_CLK_TCK")
        os.utime(self.config, (1050, 1050))
        proc = self._fake_proc({
            10: (["node", "/usr/lib/node_modules/@openai/codex/bin/codex.js"], {"CODEX_HOME": str(self.codex_home)}, 10 * tick),
            11: (["/usr/bin/codex", "resume"], {"CODEX_HOME": str(self.codex_home)}, 100 * tick),
            12: (["/usr/bin/codex"], {"CODEX_HOME": "/elsewhere/.codex"}, 10 * tick),
            13: (["python3", "x.py"], {"CODEX_HOME": str(self.codex_home)}, 10 * tick),
            14: (["codex"], {"HOME": str(self.home)}, 20 * tick),
            15: (["codex"], None, 10 * tick),
        })
        live = worker_guard_hook_status(self.codex_home, self.home, executable=Path("/usr/bin/codex"),
                                        read_hooks=self.codex, proc_root=proc)["live_sessions"]
        self.assertTrue(live["measurable"])
        self.assertEqual(live["config_mtime"], 1050)
        self.assertEqual({s["pid"]: s["started_before_config_mtime"] for s in live["sessions"]},
                         {10: True, 11: False, 14: True})
        self.assertEqual(live["unreadable"], 1)

    def test_live_sessions_unmeasurable_without_proc_and_never_gate_ok(self) -> None:
        self._ensure()
        status = worker_guard_hook_status(self.codex_home, self.home, executable=Path("/usr/bin/codex"),
                                          read_hooks=self.codex, proc_root=Path(self._tmp.name) / "no-proc")
        self.assertFalse(status["live_sessions"]["measurable"])
        self.assertTrue(status["ok"], "ok is the ON-DISK verdict only")

    def test_two_stale_guards_are_ambiguous_and_left_alone(self) -> None:
        original = self._legacy_config()
        original += (f'\n[[hooks.PreToolUse]]\nmatcher = "Bash|apply_patch"\n\n[[hooks.PreToolUse.hooks]]\n'
                     f'type = "command"\ncommand = "{self.legacy_cmd}"\ntimeout = 10\n')
        self.config.write_text(original)
        self.assertFalse(self._ensure())
        self.assertEqual(self.config.read_text(), original)

    # --- the rollout verdict (T-4395 item 4) ---

    # --- T-4428: the verified-marker skips the launch read, never a repair ---

    def _marker(self) -> Path:
        return self.codex_home / codex_adapter._VERIFIED_MARKER_NAME

    def _verified(self) -> None:
        """A current + trusted home whose last read recorded the marker; calls/writes reset."""
        self._ensure()  # fresh wire (no real read, no marker)
        self._ensure()  # the real read says trusted -> marker
        self.assertTrue(self._marker().is_file())
        self.codex.calls, self.writes = [], []

    def test_marker_hit_skips_the_hooks_list_read(self) -> None:
        self._verified()
        before = self.config.read_bytes()
        self.assertFalse(self._ensure())
        self.assertEqual(self.codex.calls, [], "a marker hit makes no hooks/list call")
        self.assertEqual(self.writes, [])
        self.assertEqual(self.config.read_bytes(), before)

    def test_trust_marker_false_always_reads(self) -> None:
        self._verified()
        self.assertFalse(self._ensure(trust_marker=False))
        self.assertEqual(self._real_calls(), [self.codex_home])

    def test_config_byte_change_re_reads(self) -> None:
        self._verified()
        self.config.write_text(self.config.read_text() + "# a comment, same TOML\n")
        self.assertFalse(self._ensure())
        self.assertEqual(self._real_calls(), [self.codex_home])
        self.codex.calls = []
        self.assertFalse(self._ensure())
        self.assertEqual(self.codex.calls, [], "the re-read re-recorded the marker for the new bytes")

    def test_codex_binary_change_re_reads(self) -> None:
        launcher = self.codex_pkg / "bin" / "codex.js"
        other = Path(self._tmp.name) / "other-codex"
        other.write_text("// launcher\n")
        changes = {
            "mtime": lambda: os.utime(launcher, ns=(1, 1)),
            "size": lambda: launcher.write_text("// launcher, bigger\n"),
            "path": lambda: (self.codex_bin.unlink(), self.codex_bin.symlink_to(other)),
            "version": lambda: (self.codex_pkg / "package.json").write_text('{"version": "0.148.0"}\n'),
        }
        for label, change in changes.items():
            with self.subTest(change=label):
                self._marker().unlink(missing_ok=True)
                self.codex_bin.unlink()
                self.codex_bin.symlink_to(launcher)
                self._ensure()
                self._ensure()
                self.codex.calls = []
                self.assertFalse(self._ensure())
                self.assertEqual(self.codex.calls, [])
                change()
                self.assertFalse(self._ensure())
                self.assertEqual(self._real_calls(), [self.codex_home])

    def test_missing_malformed_or_wrong_key_marker_re_reads(self) -> None:
        def wrong(field, value):
            def go():
                body = json.loads(self._marker().read_text())
                body[field] = value
                self._marker().write_text(json.dumps(body))
            return go
        cases = {
            "missing": lambda: self._marker().unlink(),
            "malformed": lambda: self._marker().write_text("{not json"),
            "not-a-dict": lambda: self._marker().write_text("[]"),
            "wrong-key": wrong("key", "/elsewhere/config.toml:pre_tool_use:0:0"),
            "wrong-config-hash": wrong("config_sha256", "00" * 32),
            "wrong-version": wrong("version", 0),
            "unreadable": lambda: (self._marker().unlink(), self._marker().mkdir()),
        }
        for label, corrupt in cases.items():
            with self.subTest(case=label):
                if self._marker().is_dir():
                    self._marker().rmdir()
                self._verified()
                corrupt()
                self.assertFalse(self._ensure())
                self.assertEqual(self._real_calls(), [self.codex_home])

    def test_stale_hash_is_still_repaired_after_a_codex_upgrade(self) -> None:
        # A codex upgrade changes currentHash for the unchanged definition: the stored hash now
        # reads "modified". The binary identity moved, so the marker misses and the repair runs.
        self._verified()
        self.codex.real_result = "codex-0.148"  # the upgraded codex hashes differently
        upgraded = FakeCodexHooks.definition_hash(self.cmd, 30, "Bash|apply_patch", salt="codex-0.148")
        (self.codex_pkg / "package.json").write_text('{"version": "0.148.0"}\n')
        (self.codex_pkg / "bin" / "codex.js").write_text("// launcher 0.148\n")
        self.assertTrue(self._ensure())
        self.assertEqual(self._real_calls(), [self.codex_home])
        self.assertEqual(len(self.writes), 1)
        self.assertIn("REPAIRED stale", self.stderr.getvalue())
        self.assertEqual(self._guard_state()[f"{self.config}:pre_tool_use:0:0"], {"trusted_hash": upgraded})
        # the repair wrote new bytes: the next launch reads (trusted) and re-records, then skips
        self.codex.calls = []
        self.assertFalse(self._ensure())
        self.assertEqual(self._real_calls(), [self.codex_home])
        self.codex.calls = []
        self.assertFalse(self._ensure())
        self.assertEqual(self.codex.calls, [])

    def test_codex_upgrade_during_the_read_records_no_marker(self) -> None:
        # r2 (review round 1's probe): the old binary judges "trusted" while an upgrade lands
        # mid-read. Recording that verdict under the NEW identity would skip the repair the
        # upgrade needs; the body is taken before the read, so nothing is recorded.
        self._ensure()  # fresh wire
        self.codex.on_real_read = lambda: (self.codex_pkg / "package.json").write_text(
            '{"version": "0.148.0"}\n')
        self.assertFalse(self._ensure())
        self.codex.on_real_read = None
        self.assertFalse(self._marker().exists())
        self.codex.real_result = "codex-0.148"  # the upgraded codex hashes differently
        self.codex.calls, self.writes = [], []
        self.assertTrue(self._ensure())  # the stale-hash repair runs, as without the marker
        self.assertEqual(self._real_calls(), [self.codex_home])
        self.assertIn("REPAIRED stale", self.stderr.getvalue())

    def test_no_marker_unless_the_verdict_is_current_and_trusted(self) -> None:
        cases = {
            "untrusted (re-trusted by a write)": lambda: self._current_untrusted(),
            "modified (repaired by a write)": lambda: (self._ensure(), self.config.write_text(
                self.config.read_text().replace(self.new_hash, "sha256:" + "00" * 32))),
            "read failed": lambda: (self._ensure(), setattr(self.codex, "real_result", None)),
            "read raised": lambda: (self._ensure(), setattr(self.codex, "real_result", "raise")),
            "legacy block upgraded": lambda: self._legacy_config(),
            "fresh seat": lambda: None,
        }
        for label, setup in cases.items():
            with self.subTest(case=label):
                self.config.unlink(missing_ok=True)
                self._marker().unlink(missing_ok=True)
                self.codex.real_result = "live"
                setup()
                self._ensure()
                self.assertFalse(self._marker().exists())

    def test_no_marker_when_config_changes_during_the_read(self) -> None:
        self._ensure()
        self.codex.on_real_read = lambda: self.config.write_text(self.config.read_text() + "# edit\n")
        self.assertFalse(self._ensure())
        self.assertFalse(self._marker().exists())

    def test_marker_is_0600_and_leaves_no_temp_file(self) -> None:
        self._verified()
        self.assertEqual(self._marker().stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.codex_home.iterdir()),
                         sorted(["config.toml", codex_adapter._VERIFIED_MARKER_NAME]))

    def test_status_never_uses_the_marker(self) -> None:
        self._verified()
        self._status()
        self.assertEqual(self._real_calls(), [self.codex_home])

    def _status(self, read=None):
        return worker_guard_hook_status(self.codex_home, self.home, executable=Path("/usr/bin/codex"),
                                        read_hooks=read or self.codex)

    def test_status_before_and_after_the_upgrade(self) -> None:
        self._legacy_config()
        before = self._status()
        self.assertEqual((before["block_current"], before["legacy_present"], before["ok"]), (False, True, False))
        self._ensure()
        after = self._status()
        self.assertEqual(after["key"], f"{self.config}:pre_tool_use:0:0")
        self.assertEqual((after["block_current"], after["legacy_present"], after["trust_status"],
                          after["hash_matches"], after["measurable"], after["ok"]),
                         (True, False, "trusted", True, True, True))
        self.assertEqual(after["stored_hash"], self.new_hash)

    def test_status_flags_a_modified_guard(self) -> None:
        self._ensure()
        self.codex.real_result = "other-codex-version"
        status = self._status()
        self.assertEqual((status["trust_status"], status["hash_matches"], status["ok"]), ("modified", False, False))

    def test_status_is_unmeasurable_when_the_app_server_read_fails(self) -> None:
        self._ensure()
        self.codex.real_result = None
        status = self._status()
        self.assertEqual((status["measurable"], status["ok"], status["trust_status"]), (False, False, None))


class EnsureWorkerGuardV2Test(unittest.TestCase):
    """T-6123: the canonical guard is worker-guard-v2.py (python3 -I, T-5695); exact v1 -I is the
    operator ROLLBACK (kept on the launch path), and a second guard is never appended."""

    KEY0 = "pre_tool_use:0:0"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "seat"
        hooks = self.home / "pantheon-config" / "tools" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "worker-guard.py").write_text("# guard\n")
        (hooks / "worker-guard-v2.py").write_text("# guard v2\n")
        self.codex_home = self.home / ".codex"
        self.config = self.codex_home / "config.toml"
        self.codex = FakeCodexHooks(real_home=self.codex_home)
        self.v2 = f"/usr/bin/python3 -I {self.home}/pantheon-config/tools/hooks/worker-guard-v2.py"
        self.v1 = f"/usr/bin/python3 -I {self.home}/pantheon-config/tools/hooks/worker-guard.py"
        self.plain_v1 = self.v1.replace(" -I", "")
        self.plain_v2 = self.v2.replace(" -I", "")
        self.shell_box = f"/usr/bin/python3 -I {self.home}/pantheon-config/tools/oom/pantheon-codex-shell-box-hook.py"
        pkg = Path(self._tmp.name) / "npm" / "@openai" / "codex"
        (pkg / "bin").mkdir(parents=True)
        (pkg / "bin" / "codex.js").write_text("// launcher\n")
        (pkg / "package.json").write_text('{"version": "0.147.0"}\n')
        self.codex_bin = Path(self._tmp.name) / "codex"
        self.codex_bin.symlink_to(pkg / "bin" / "codex.js")
        self.writes: list[str] = []
        real_write = codex_adapter._write_config_preserving_mode

        def spy(path, text):
            self.writes.append(text)
            return real_write(path, text)

        patcher = unittest.mock.patch.object(codex_adapter, "_write_config_preserving_mode", spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ensure(self, **kw):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            out = ensure_worker_guard_hook(self.codex_home, self.home, executable=self.codex_bin,
                                           read_hooks=self.codex, **kw)
        self.stderr = err
        return out

    def _status(self):
        return worker_guard_hook_status(self.codex_home, self.home, executable=Path("/usr/bin/codex"),
                                        read_hooks=self.codex)

    def _group(self, command, timeout=30, matcher="Bash|apply_patch"):
        return (f'[[hooks.PreToolUse]]\nmatcher = "{matcher}"\n\n[[hooks.PreToolUse.hooks]]\n'
                f'type = "command"\ncommand = "{command}"\ntimeout = {timeout}\n\n')

    def _state(self, g, h, digest):
        return f'[hooks.state."{self.config}:pre_tool_use:{g}:{h}"]\ntrusted_hash = "{digest}"\n\n'

    def _h(self, command, timeout=30, matcher="Bash|apply_patch"):
        return FakeCodexHooks.definition_hash(command, timeout, matcher)

    def _write(self, text):
        self.codex_home.mkdir(exist_ok=True)
        self.config.write_text(text)
        self.config.chmod(0o640)
        return text

    def _guards(self):
        parsed = tomllib.loads(self.config.read_text())
        return [h["command"] for g in parsed["hooks"]["PreToolUse"] for h in g["hooks"]
                if h["command"].endswith("/pantheon-config/tools/hooks/worker-guard.py")
                or h["command"].endswith("/pantheon-config/tools/hooks/worker-guard-v2.py")]

    def _consultant(self, guard_cmd=None, trusted=True):
        guard_cmd = guard_cmd or self.v2
        sb = self._h(self.shell_box, 10, "Bash")
        return self._write(
            'model = "x"\n\n'
            + self._group(guard_cmd) + self._group(self.shell_box, 10, "Bash")
            + '[[hooks.SessionStart]]\n\n[[hooks.SessionStart.hooks]]\ntype = "command"\ncommand = "/bin/tg"\n\n'
            + '[[hooks.Stop]]\n\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "/bin/tg"\n\n'
            + (self._state(0, 0, self._h(guard_cmd)) if trusted else "")
            + self._state(1, 0, sb))

    def test_canonical_command_is_v2_and_rollback_is_v1(self) -> None:
        self.assertEqual(worker_guard_command(self.home), self.v2)
        self.assertEqual(codex_adapter.rollback_worker_guard_command(self.home), self.v1)
        self.assertEqual(set(codex_adapter._legacy_worker_guard_commands(self.home)),
                         {self.plain_v1, self.plain_v2})

    def test_consultant_shape_writes_and_appends_nothing(self) -> None:
        original = self._consultant()
        self.assertFalse(self._ensure())
        self.assertEqual(self.config.read_text(), original)
        self.assertEqual(self.writes, [])
        parsed = tomllib.loads(original)
        self.assertEqual(len(parsed["hooks"]["PreToolUse"]), 2)
        self.assertEqual(self._guards(), [self.v2])

    def test_consultant_shape_untrusted_guard_only_gains_its_hash(self) -> None:
        self._consultant(trusted=False)
        self.assertTrue(self._ensure())
        self.assertEqual(self._guards(), [self.v2])
        state = tomllib.loads(self.config.read_text())["hooks"]["state"]
        self.assertEqual(state[f"{self.config}:{self.KEY0}"], {"trusted_hash": self._h(self.v2)})

    def test_fresh_seat_gets_exactly_one_v2_guard_trusted_in_one_write(self) -> None:
        self.assertTrue(self._ensure())
        self.assertEqual(self._guards(), [self.v2])
        state = tomllib.loads(self.config.read_text())["hooks"]["state"]
        self.assertEqual(state[f"{self.config}:{self.KEY0}"], {"trusted_hash": self._h(self.v2)})
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_rollback_v1_trusted_is_left_alone_on_launch(self) -> None:
        original = self._write(self._group(self.v1) + self._state(0, 0, self._h(self.v1)))
        self.assertFalse(self._ensure())
        self.assertEqual(self.config.read_text(), original)
        self.assertEqual(self._guards(), [self.v1])

    def test_rollback_v1_untrusted_gets_only_its_hash_and_stays_v1(self) -> None:
        self._write(self._group(self.v1))
        self.assertTrue(self._ensure())
        self.assertEqual(self._guards(), [self.v1])
        state = tomllib.loads(self.config.read_text())["hooks"]["state"]
        self.assertEqual(state[f"{self.config}:{self.KEY0}"], {"trusted_hash": self._h(self.v1)})

    def test_upgrade_rollback_rewrites_v1_to_v2_in_place_and_retrusts(self) -> None:
        self._write(self._group(self.v1) + self._group(self.shell_box, 10, "Bash")
                    + self._state(0, 0, self._h(self.v1)))
        self.assertTrue(self._ensure(upgrade_rollback=True))
        self.assertEqual(self._guards(), [self.v2])
        parsed = tomllib.loads(self.config.read_text())
        self.assertEqual(parsed["hooks"]["state"][f"{self.config}:{self.KEY0}"],
                         {"trusted_hash": self._h(self.v2)})
        self.assertEqual(len(parsed["hooks"]["PreToolUse"]), 2)
        self.assertEqual(len(self.writes), 1)

    def test_stale_forms_are_upgraded_in_place_to_v2(self) -> None:
        for stale, timeout in ((self.plain_v1, 10), (self.plain_v2, 30), (self.v1, 10), (self.v2, 10)):
            with self.subTest(stale=stale, timeout=timeout):
                self.writes.clear()
                self._write(self._group(stale, timeout) + self._state(0, 0, self._h(stale, timeout)))
                self.assertTrue(self._ensure())
                self.assertEqual(self._guards(), [self.v2])
                self.assertEqual(len(self.writes), 1)
                parsed = tomllib.loads(self.config.read_text())
                self.assertEqual(parsed["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"], 30)
                self.assertEqual(parsed["hooks"]["state"][f"{self.config}:{self.KEY0}"],
                                 {"trusted_hash": self._h(self.v2)})

    def test_v1_alongside_v2_never_gets_a_third_guard(self) -> None:
        original = self._write(self._group(self.v2) + self._group(self.v1)
                               + self._state(0, 0, self._h(self.v2)) + self._state(1, 0, self._h(self.v1)))
        self._ensure()
        self.assertEqual(self.config.read_text(), original)
        self.assertEqual(len(self._guards()), 2)

    def test_edited_guard_command_is_refused_not_duplicated(self) -> None:
        for edited in (self.v2 + " --x", self.v2.replace("/usr/bin/python3", "/opt/py/python3"),
                       self.v1 + " --x"):
            with self.subTest(edited=edited):
                original = self._write(self._group(edited))
                self.assertFalse(self._ensure())
                self.assertEqual(self.config.read_text(), original)
                self.assertIn("unrecognised worker-guard entry", self.stderr.getvalue())
                self.assertEqual(self.writes, [])

    def test_quoted_and_shell_wrapped_guard_commands_are_refused_not_duplicated(self) -> None:
        script = f"{self.home}/pantheon-config/tools/hooks/worker-guard"
        for edited in (f"/usr/bin/python3 '{script}-v2.py'",
                       f'/usr/bin/python3 "{script}.py"',
                       f"sh -c '{script}.py;true'"):
            with self.subTest(edited=edited):
                toml_cmd = edited.replace("\\", "\\\\").replace('"', '\\"')
                original = self._write(self._group(toml_cmd))
                self.assertFalse(self._ensure())
                self.assertEqual(self.config.read_text(), original)
                self.assertIn("unrecognised worker-guard entry", self.stderr.getvalue())
                self.assertEqual(self.writes, [])
                from tools.harness_adapter import codex
                self.assertEqual(len(codex._guard_like_positions(tomllib.loads(original))), 1)

    def test_guard_script_regex_boundaries(self) -> None:
        from tools.harness_adapter import codex
        base = "/h/pantheon-config/tools/hooks/"
        for cmd in (base + "worker-guard-shadow.py", base + "worker-guard-v2.pyc",
                    base + "worker-guard.py.bak"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(codex._GUARD_SCRIPT_RE.search("/usr/bin/python3 " + cmd))
        for cmd in (base + "worker-guard.py", "'" + base + "worker-guard-v2.py'",
                    base + "worker-guard.py;true", base + "worker-guard.py --x"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(codex._GUARD_SCRIPT_RE.search(cmd))

    def test_status_v2_generation(self) -> None:
        self._consultant()
        status = self._status()
        self.assertEqual((status["generation"], status["ok"], status["legacy_present"]), ("v2", True, False))

    def test_status_rollback_generation_is_ok_when_trusted_and_alone(self) -> None:
        self._write(self._group(self.v1) + self._state(0, 0, self._h(self.v1)))
        status = self._status()
        self.assertEqual((status["generation"], status["ok"], status["legacy_present"]),
                         ("rollback-v1", True, False))
        self._write(self._group(self.v1))
        self.assertFalse(self._status()["ok"])

    def test_status_v1_plus_v2_is_not_ok_and_legacy_present(self) -> None:
        self._write(self._group(self.v2) + self._group(self.v1)
                    + self._state(0, 0, self._h(self.v2)) + self._state(1, 0, self._h(self.v1)))
        status = self._status()
        self.assertEqual((status["generation"], status["ok"], status["legacy_present"]), ("v2", False, True))

    def test_status_no_guard(self) -> None:
        self.assertEqual((self._status()["generation"], self._status()["ok"]), (None, False))


class GcStaleProjectTrustEntriesTest(unittest.TestCase):
    """Thin tests for the I/O-performing outer function + the manual CLI."""

    def test_writes_pruned_config_and_returns_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            live = Path(tmp) / "live"
            live.mkdir()
            dead = Path(tmp) / "dead"
            config_path = codex_home / "config.toml"
            config_path.write_text(
                f'[projects."{live}"]\ntrust_level = "trusted"\n\n'
                f'[projects."{dead}"]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )
            pruned = gc_stale_project_trust_entries(codex_home)
            self.assertEqual(pruned, 1)
            parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(list(parsed["projects"]), [str(live)])

    def test_absent_config_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "nonexistent"
            self.assertEqual(gc_stale_project_trust_entries(codex_home), 0)

    def test_nothing_stale_does_not_rewrite_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            live = Path(tmp) / "live"
            live.mkdir()
            config_path = codex_home / "config.toml"
            config_path.write_text(
                f'[projects."{live}"]\ntrust_level = "trusted"\n', encoding="utf-8"
            )
            before_mtime = config_path.stat().st_mtime_ns
            pruned = gc_stale_project_trust_entries(codex_home)
            self.assertEqual(pruned, 0)
            self.assertEqual(config_path.stat().st_mtime_ns, before_mtime)

    def test_cli_prunes_and_reports_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            live = Path(tmp) / "live"
            live.mkdir()
            dead = Path(tmp) / "dead"
            config_path = codex_home / "config.toml"
            config_path.write_text(
                f'[projects."{live}"]\ntrust_level = "trusted"\n\n'
                f'[projects."{dead}"]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = gc_trust_entries_cli(["--codex-home", str(codex_home)])
            self.assertEqual(rc, 0)
            self.assertIn("pruned 1", buf.getvalue())
            parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(list(parsed["projects"]), [str(live)])

    def test_cli_dry_run_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            dead = Path(tmp) / "dead"
            config_path = codex_home / "config.toml"
            config_path.write_text(
                f'[projects."{dead}"]\ntrust_level = "trusted"\n', encoding="utf-8"
            )
            before = config_path.read_bytes()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = gc_trust_entries_cli(["--codex-home", str(codex_home), "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertIn("would prune 1", buf.getvalue())
            self.assertEqual(config_path.read_bytes(), before)


# --------------------------------------------------------------------------- #
# 12. ensure_update_check_disabled (T-2597 wedge #2/#3) — the update-picker/  #
#     banner-safety config lever.                                            #
# --------------------------------------------------------------------------- #


class EnsureUpdateCheckDisabledTest(unittest.TestCase):
    def test_absent_config_creates_file_with_flag_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "nonexistent" / ".codex"
            modified = ensure_update_check_disabled(codex_home)
            self.assertTrue(modified)
            config_path = codex_home / "config.toml"
            parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["check_for_update_on_startup"], False)
            self.assertIs(parsed["features"]["apps"], False)

    def test_root_level_key_lands_before_existing_table_not_inside_it(self) -> None:
        # The load-bearing TOML-scoping correctness case: a bare append (like
        # ensure_project_trusted's own style) would silently nest the key
        # inside whatever table precedes end-of-file. It must land at root
        # scope regardless of what tables already exist.
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            config_path = codex_home / "config.toml"
            cwd = Path("/tmp/some-project")
            config_path.write_text(
                f'[projects."{cwd}"]\ntrust_level = "trusted"\n', encoding="utf-8",
            )
            modified = ensure_update_check_disabled(codex_home)
            self.assertTrue(modified)
            text = config_path.read_text(encoding="utf-8")
            parsed = tomllib.loads(text)
            self.assertEqual(parsed["check_for_update_on_startup"], False)
            self.assertEqual(parsed["projects"][str(cwd)]["trust_level"], "trusted")
            # Textual check: the key must appear BEFORE the table header line.
            key_index = text.index("check_for_update_on_startup")
            table_index = text.index("[projects.")
            self.assertLess(key_index, table_index)

    def test_already_set_true_is_left_untouched(self) -> None:
        # Respects an operator's explicit prior choice, even a non-default one.
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            config_path = codex_home / "config.toml"
            config_path.write_text(
                "check_for_update_on_startup = true\n\n[features]\napps = true\n",
                encoding="utf-8",
            )
            before = config_path.read_bytes()
            modified = ensure_update_check_disabled(codex_home)
            self.assertFalse(modified)
            self.assertEqual(config_path.read_bytes(), before)

    def test_already_set_false_is_idempotent_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            self.assertTrue(ensure_update_check_disabled(codex_home))
            config_path = codex_home / "config.toml"
            before = config_path.read_bytes()
            modified_again = ensure_update_check_disabled(codex_home)
            self.assertFalse(modified_again)
            self.assertEqual(config_path.read_bytes(), before)

    def test_unrelated_existing_content_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            config_path = codex_home / "config.toml"
            config_path.write_text('approval_policy = "never"\n', encoding="utf-8")
            modified = ensure_update_check_disabled(codex_home)
            self.assertTrue(modified)
            parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["approval_policy"], "never")
            self.assertEqual(parsed["check_for_update_on_startup"], False)

    def test_malformed_existing_toml_raises_instead_of_blind_insert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            config_path = codex_home / "config.toml"
            config_path.write_text("this is not [valid toml\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ensure_update_check_disabled(codex_home)

    # T-6257: codex_apps (the 0.147.0 ``apps`` feature) is not started on seats.
    def test_apps_seed_joins_existing_features_table_without_duplicate_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            config_path = codex_home / "config.toml"
            config_path.write_text(
                "check_for_update_on_startup = false\n\n[features]\nhooks = true\n\n"
                '[projects."/x"]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )
            self.assertTrue(ensure_update_check_disabled(codex_home))
            text = config_path.read_text(encoding="utf-8")
            parsed = tomllib.loads(text)
            self.assertEqual(text.count("[features]"), 1)
            self.assertIs(parsed["features"]["hooks"], True)
            self.assertIs(parsed["features"]["apps"], False)
            self.assertEqual(parsed["projects"]["/x"]["trust_level"], "trusted")

    def test_explicit_apps_true_is_preserved_while_update_guard_is_seeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            config_path = codex_home / "config.toml"
            config_path.write_text("[features]\napps = true\n", encoding="utf-8")
            self.assertTrue(ensure_update_check_disabled(codex_home))
            parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertIs(parsed["features"]["apps"], True)
            self.assertIs(parsed["check_for_update_on_startup"], False)

    def test_inline_features_table_skips_apps_seed_instead_of_breaking_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            config_path = codex_home / "config.toml"
            config_path.write_text(
                "check_for_update_on_startup = false\nfeatures = { hooks = true }\n",
                encoding="utf-8",
            )
            before = config_path.read_bytes()
            self.assertFalse(ensure_update_check_disabled(codex_home))
            self.assertEqual(config_path.read_bytes(), before)


# --------------------------------------------------------------------------- #
# 13. ensure_model_migration_prompts_hidden (T-2597 wedge #4) — the model-    #
#     migration-modal-safety config lever.                                   #
# --------------------------------------------------------------------------- #


class EnsureModelMigrationPromptsHiddenTest(unittest.TestCase):
    def _write_cache(self, codex_home: Path, models: list[dict]) -> Path:
        codex_home.mkdir(parents=True, exist_ok=True)
        cache_path = codex_home / "models_cache.json"
        cache_path.write_text(
            json.dumps({"fetched_at": "2026-08-05T19:06:56Z", "models": models}),
            encoding="utf-8",
        )
        return cache_path

    def test_no_cache_file_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "nonexistent"
            self.assertFalse(ensure_model_migration_prompts_hidden(codex_home))
            self.assertFalse((codex_home / "config.toml").exists())

    def test_malformed_cache_json_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            codex_home.mkdir(parents=True, exist_ok=True)
            (codex_home / "models_cache.json").write_text("not json{", encoding="utf-8")
            self.assertFalse(ensure_model_migration_prompts_hidden(codex_home))

    def test_no_active_upgrades_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            self._write_cache(
                codex_home, [{"slug": "gpt-5.6-sol", "upgrade": None}],
            )
            self.assertFalse(ensure_model_migration_prompts_hidden(codex_home))

    def test_active_upgrade_for_unknown_slug_is_a_no_op(self) -> None:
        # The documented residual gap: an active upgrade whose slug is in
        # NEITHER known dict (no hideable key, no known [notice.
        # model_migrations] mapping) cannot be suppressed -- covered instead
        # by tools.codex_modal's keystroke fallback (T-2599). gpt-5.4 is NOT
        # this case anymore (T-2674 covers it via _KNOWN_MODEL_MIGRATIONS) --
        # use a synthetic slug that will never be in either dict.
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            self._write_cache(
                codex_home,
                [{"slug": "gpt-9.9-nonexistent", "upgrade": {"model": "gpt-9.9-sol"}}],
            )
            self.assertFalse(ensure_model_migration_prompts_hidden(codex_home))
            self.assertFalse((codex_home / "config.toml").exists())

    def test_active_upgrade_for_known_slug_seeds_notices_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            self._write_cache(
                codex_home,
                [{"slug": "gpt-5.1", "upgrade": {"model": "gpt-5.6-sol"}}],
            )
            modified = ensure_model_migration_prompts_hidden(codex_home)
            self.assertTrue(modified)
            parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(parsed["notices"]["hide_gpt5_1_migration_prompt"], True)

    def test_hyphenated_slug_key_is_written_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            self._write_cache(
                codex_home,
                [{"slug": "gpt-5.1-codex-max", "upgrade": {"model": "gpt-5.6-sol"}}],
            )
            modified = ensure_model_migration_prompts_hidden(codex_home)
            self.assertTrue(modified)
            parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(
                parsed["notices"]["hide_gpt-5.1-codex-max_migration_prompt"], True
            )

    def test_idempotent_second_call_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            self._write_cache(
                codex_home,
                [{"slug": "gpt-5.1", "upgrade": {"model": "gpt-5.6-sol"}}],
            )
            self.assertTrue(ensure_model_migration_prompts_hidden(codex_home))
            config_path = codex_home / "config.toml"
            before = config_path.read_bytes()
            modified_again = ensure_model_migration_prompts_hidden(codex_home)
            self.assertFalse(modified_again)
            self.assertEqual(config_path.read_bytes(), before)

    def test_existing_notices_table_gets_new_key_inserted_not_duplicated(self) -> None:
        # Exercises the "[notices] already exists" insertion path -- this is
        # this function's OWN second-run behavior when a second, different
        # migration goes active later, not just a hypothetical.
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            config_path = codex_home / "config.toml"
            config_path.write_text(
                '[notices]\nhide_full_access_warning = true\n\n'
                '[tui.model_availability_nux]\n"gpt-5.5" = 4\n',
                encoding="utf-8",
            )
            self._write_cache(
                codex_home,
                [{"slug": "gpt-5.1", "upgrade": {"model": "gpt-5.6-sol"}}],
            )
            modified = ensure_model_migration_prompts_hidden(codex_home)
            self.assertTrue(modified)
            text = config_path.read_text(encoding="utf-8")
            self.assertEqual(text.count("[notices]"), 1)
            parsed = tomllib.loads(text)
            self.assertEqual(parsed["notices"]["hide_full_access_warning"], True)
            self.assertEqual(parsed["notices"]["hide_gpt5_1_migration_prompt"], True)
            self.assertEqual(parsed["tui"]["model_availability_nux"]["gpt-5.5"], 4)

    def test_existing_explicit_value_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            config_path = codex_home / "config.toml"
            config_path.write_text(
                "[notices]\nhide_gpt5_1_migration_prompt = false\n", encoding="utf-8",
            )
            self._write_cache(
                codex_home,
                [{"slug": "gpt-5.1", "upgrade": {"model": "gpt-5.6-sol"}}],
            )
            before = config_path.read_bytes()
            modified = ensure_model_migration_prompts_hidden(codex_home)
            self.assertFalse(modified)
            self.assertEqual(config_path.read_bytes(), before)

    def test_malformed_config_toml_raises_instead_of_blind_insert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            self._write_cache(
                codex_home,
                [{"slug": "gpt-5.1", "upgrade": {"model": "gpt-5.6-sol"}}],
            )
            (codex_home / "config.toml").write_text("this is not [valid toml\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ensure_model_migration_prompts_hidden(codex_home)

    # ----------------------------------------------------------------- #
    # T-2674: [notice.model_migrations] pre-seeding -- covers the two    #
    # currently-active migrations with NO hide-key in the pinned binary  #
    # (gpt-5.4, gpt-5.4-mini), by pre-seeding the SAME acknowledgment    #
    # entry codex itself writes to config.toml on manual dismissal.      #
    # ----------------------------------------------------------------- #

    def test_active_upgrade_for_known_migration_slug_seeds_notice_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            self._write_cache(
                codex_home,
                [{"slug": "gpt-5.4", "upgrade": {"model": "gpt-5.6-terra"}}],
            )
            modified = ensure_model_migration_prompts_hidden(codex_home)
            self.assertTrue(modified)
            text = (codex_home / "config.toml").read_text(encoding="utf-8")
            # The migration-slug key ("gpt-5.4") contains a literal "." and
            # so is NOT a valid bare TOML key -- must be emitted quoted, or
            # TOML would parse it as dotted-key table nesting instead of one
            # flat key (same load-bearing concern as the hide-key table's
            # gpt-5.1-codex-max precedent).
            self.assertIn('"gpt-5.4" = "gpt-5.6-terra"', text)
            parsed = tomllib.loads(text)
            self.assertEqual(parsed["notice"]["model_migrations"]["gpt-5.4"], "gpt-5.6-terra")

    def test_active_upgrade_for_known_migration_mini_slug_seeds_notice_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            self._write_cache(
                codex_home,
                [{"slug": "gpt-5.4-mini", "upgrade": {"model": "gpt-5.6-luna"}}],
            )
            modified = ensure_model_migration_prompts_hidden(codex_home)
            self.assertTrue(modified)
            parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(
                parsed["notice"]["model_migrations"]["gpt-5.4-mini"], "gpt-5.6-luna"
            )

    def test_both_hideable_and_migration_only_slugs_seed_both_tables_in_one_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            self._write_cache(
                codex_home,
                [
                    {"slug": "gpt-5.1", "upgrade": {"model": "gpt-5.6-sol"}},
                    {"slug": "gpt-5.4", "upgrade": {"model": "gpt-5.6-terra"}},
                ],
            )
            modified = ensure_model_migration_prompts_hidden(codex_home)
            self.assertTrue(modified)
            parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(parsed["notices"]["hide_gpt5_1_migration_prompt"], True)
            self.assertEqual(parsed["notice"]["model_migrations"]["gpt-5.4"], "gpt-5.6-terra")

    def test_migration_idempotent_second_call_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            self._write_cache(
                codex_home,
                [{"slug": "gpt-5.4", "upgrade": {"model": "gpt-5.6-terra"}}],
            )
            self.assertTrue(ensure_model_migration_prompts_hidden(codex_home))
            config_path = codex_home / "config.toml"
            before = config_path.read_bytes()
            modified_again = ensure_model_migration_prompts_hidden(codex_home)
            self.assertFalse(modified_again)
            self.assertEqual(config_path.read_bytes(), before)

    def test_existing_model_migrations_table_gets_new_key_inserted_not_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            config_path = codex_home / "config.toml"
            config_path.write_text(
                '[notice.model_migrations]\n"gpt-5.1" = "gpt-5.6-sol"\n\n'
                '[tui.model_availability_nux]\n"gpt-5.5" = 4\n',
                encoding="utf-8",
            )
            self._write_cache(
                codex_home,
                [{"slug": "gpt-5.4-mini", "upgrade": {"model": "gpt-5.6-luna"}}],
            )
            modified = ensure_model_migration_prompts_hidden(codex_home)
            self.assertTrue(modified)
            text = config_path.read_text(encoding="utf-8")
            self.assertEqual(text.count("[notice.model_migrations]"), 1)
            parsed = tomllib.loads(text)
            self.assertEqual(parsed["notice"]["model_migrations"]["gpt-5.1"], "gpt-5.6-sol")
            self.assertEqual(
                parsed["notice"]["model_migrations"]["gpt-5.4-mini"], "gpt-5.6-luna"
            )
            self.assertEqual(parsed["tui"]["model_availability_nux"]["gpt-5.5"], 4)

    def test_existing_model_migration_explicit_value_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            config_path = codex_home / "config.toml"
            config_path.write_text(
                '[notice.model_migrations]\n"gpt-5.4" = "some-other-target"\n',
                encoding="utf-8",
            )
            self._write_cache(
                codex_home,
                [{"slug": "gpt-5.4", "upgrade": {"model": "gpt-5.6-terra"}}],
            )
            before = config_path.read_bytes()
            modified = ensure_model_migration_prompts_hidden(codex_home)
            self.assertFalse(modified)
            self.assertEqual(config_path.read_bytes(), before)


class ClaudeCompletionTranscriptPending(unittest.TestCase):
    """T-4151: a statusline transcript that is well-formed and confined but not
    usable YET (absent, or over the byte cap) keeps the probe WAITING. Only a
    transcript that can never become valid is terminal operational-readiness.
    Before this, all of them were terminal: specialist 2026-09-28T13:34Z was torn
    down on its first sample."""

    MARKER = "PANTHEON_PROVIDER_COMPLETION_t4151"

    def _assess(self, transcript_value, *, setup=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_home = root / "account"
            projects = account_home / ".claude" / "projects"
            (projects / "p").mkdir(parents=True)
            state_dir = root / "state" / "specialist" / "runtime-controller"
            statusline = state_dir / "quota" / "claude-statusline.json"
            statusline.parent.mkdir(parents=True)
            adapter = ClaudeHarnessAdapter("orchestrator", claude_home=account_home)
            probe = adapter.prepare_interactive_provider_completion(
                harness="claude",
                profile="interactive-subscription",
                persona="specialist",
                account_home=account_home,
                state_dir=state_dir,
                pane_baseline=_post_start_sample("ready\n❯ \n", 0),
                submitted_at=datetime(2026, 9, 28, 13, 34, 5, tzinfo=timezone.utc),
                nonce_marker=self.MARKER,
                nonce_sha256=hashlib.sha256(self.MARKER.encode("utf-8")).hexdigest(),
            )
            assert probe is not None
            value = transcript_value(root, projects) if callable(transcript_value) else transcript_value
            if setup is not None:
                setup(root, projects)
            statusline.write_text(json.dumps({
                "schema": "pantheon-claude-statusline-quota/v1",
                "observed_at": "2026-09-28T13:34:10Z",
                "session_id": "fresh",
                "transcript_path": value,
                "rate_limits": {"ok": True},
            }), encoding="utf-8")
            return adapter.assess_interactive_provider_completion(
                harness="claude",
                profile="interactive-subscription",
                probe=probe,
                samples=(_post_start_sample("ready changed\n❯ \n", 1),),
            )

    def test_not_yet_created_transcript_keeps_waiting(self):
        a = self._assess(lambda root, projects: str(projects / "p" / "fresh.jsonl"))
        self.assertEqual((a.status, a.cause), ("unknown", "provider-completion-waiting"))

    def test_missing_project_dir_keeps_waiting(self):
        a = self._assess(lambda root, projects: str(projects / "new-slug" / "fresh.jsonl"))
        self.assertEqual((a.status, a.cause), ("unknown", "provider-completion-waiting"))

    def test_over_cap_transcript_keeps_waiting_without_reading_it(self):
        def big(root, projects):
            path = projects / "p" / "old-long-session.jsonl"
            with path.open("wb") as fh:  # sparse: size over the cap, nothing to read
                fh.truncate(2 * 1024 * 1024 + 1)
        a = self._assess(lambda root, projects: str(projects / "p" / "old-long-session.jsonl"),
                         setup=big)
        self.assertEqual((a.status, a.cause), ("unknown", "provider-completion-waiting"))

    def test_never_valid_transcripts_stay_terminal(self):
        def outside(root, projects):
            (root / "elsewhere.jsonl").write_text("", encoding="utf-8")

        def symlink(root, projects):
            (root / "target.jsonl").write_text("", encoding="utf-8")
            (projects / "p" / "link.jsonl").symlink_to(root / "target.jsonl")

        def directory(root, projects):
            (projects / "p" / "dir.jsonl").mkdir()

        cases = {
            "relative": ("p/fresh.jsonl", None),
            "escape": (lambda root, projects: str(root / "elsewhere.jsonl"), outside),
            "symlink-out": (lambda root, projects: str(projects / "p" / "link.jsonl"), symlink),
            "directory": (lambda root, projects: str(projects / "p" / "dir.jsonl"), directory),
        }
        for name, (value, setup) in cases.items():
            with self.subTest(name):
                a = self._assess(value, setup=setup)
                self.assertEqual((a.status, a.cause),
                                 ("operational-readiness", "provider-completion-transcript-invalid"))


class ClaudeCompletionFreshSessionProfile(unittest.TestCase):
    """T-4452: a fresh session's transcript is read from offset 0, so the probe
    also reads Claude Code's boot attachments. T-3026 attempt 3 (recovery,
    2026-09-30 15:50Z) was torn down with provider-completion-transcript-malformed:
    line 15, the type=attachment/instructions entry carrying CLAUDE.md and
    memory, was 70,631 bytes, over the old 64 KiB per-line cap. The whole
    transcript was 166,083 bytes, close to the old 256 KiB delta cap.

    The fixture keeps that line-size profile and Claude Code's key order. The
    top-level "type" comes AFTER "attachment" and "message", so a prefix peek
    would read a nested "type" first. That is why an over-cap line is skipped
    whole, not peeked at."""

    MARKER = "PANTHEON_PROVIDER_COMPLETION_t4452"
    # (top-level type, attachment type, bytes incl. newline) of 581c4d40.jsonl
    # lines 0-20, as measured: everything written before the probe reply.
    BOOT_PROFILE = (
        ("mode", None, 83), ("permission-mode", None, 105), ("atis-latch", None, 83),
        ("file-history-snapshot", None, 236), ("attachment", "hook_success", 1517),
        ("attachment", "hook_success", 26643), ("attachment", "hook_success", 6870),
        ("user", None, 721), ("attachment", "environment", 1509), ("attachment", "model", 833),
        ("attachment", "deferred_tools_delta", 4470), ("attachment", "agent_listing_delta", 6040),
        ("attachment", "mcp_instructions_delta", 7847), ("attachment", "skill_listing", 23912),
        ("attachment", "total_tokens_reminder", 601), ("attachment", "instructions", 70631),
        ("attachment", "session_context", 2952), ("attachment", "date", 575),
        ("attachment", "credential_org", 505), ("attachment", "remote_session_change", 1308),
        ("attachment", "prompt_snapshot", 7682),
    )

    @staticmethod
    def _line(top_type, attachment_type, size):
        """One JSONL line of exactly `size` bytes (newline included) in Claude
        Code's key order."""
        if attachment_type is None:
            obj = {"type": top_type, "pad": ""}
        else:
            obj = {"parentUuid": None, "isSidechain": False,
                   "attachment": {"type": attachment_type, "content": ""},
                   "type": top_type, "uuid": "u"}
        base = len(json.dumps(obj)) + 1
        assert size >= base, (top_type, size)
        if attachment_type is None:
            obj["pad"] = "x" * (size - base)
        else:
            obj["attachment"]["content"] = "x" * (size - base)
        line = json.dumps(obj) + "\n"
        assert len(line) == size
        return line

    @classmethod
    def _assistant(cls, text, *, pad=0):
        return json.dumps({
            "parentUuid": "p", "isSidechain": False,
            "message": {"role": "assistant",
                        "content": [{"type": "thinking", "thinking": "t" * pad},
                                    {"type": "text", "text": text}]},
            "type": "assistant", "uuid": "a",
        }) + "\n"

    def _assess(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_home = root / "account"
            project = account_home / ".claude" / "projects" / "-home-claude-fallback-pantheon-config"
            project.mkdir(parents=True)
            old = account_home / ".claude" / "projects" / "old.jsonl"
            old.write_text(self._assistant("old turn"), encoding="utf-8")
            state_dir = root / "state" / "recovery" / "runtime-controller"
            statusline = state_dir / "quota" / "claude-statusline.json"
            statusline.parent.mkdir(parents=True)
            statusline.write_text(json.dumps({
                "schema": "pantheon-claude-statusline-quota/v1",
                "observed_at": "2026-09-30T15:50:30Z",
                "session_id": "old",
                "transcript_path": str(old),
                "rate_limits": {"ok": True},
            }), encoding="utf-8")
            adapter = ClaudeHarnessAdapter("orchestrator", claude_home=account_home)
            probe = adapter.prepare_interactive_provider_completion(
                harness="claude",
                profile="interactive-subscription",
                persona="recovery",
                account_home=account_home,
                state_dir=state_dir,
                pane_baseline=_post_start_sample("ready\n❯ \n", 0),
                submitted_at=datetime(2026, 9, 30, 15, 50, 50, tzinfo=timezone.utc),
                nonce_marker=self.MARKER,
                nonce_sha256=hashlib.sha256(self.MARKER.encode("utf-8")).hexdigest(),
            )
            assert probe is not None
            fresh = project / "581c4d40.jsonl"
            fresh.write_text(body, encoding="utf-8")
            statusline.write_text(json.dumps({
                "schema": "pantheon-claude-statusline-quota/v1",
                "observed_at": "2026-09-30T15:50:58Z",
                "session_id": "581c4d40",
                "transcript_path": str(fresh),
                "rate_limits": {"ok": True},
            }), encoding="utf-8")
            return adapter.assess_interactive_provider_completion(
                harness="claude",
                profile="interactive-subscription",
                probe=probe,
                samples=(_post_start_sample("ready changed\n❯ \n", 1),),
            )

    def _boot(self):
        return "".join(self._line(*row) for row in self.BOOT_PROFILE)

    def test_attempt3_boot_profile_is_the_measured_one(self):
        boot = self._boot()
        self.assertEqual(len(boot.encode("utf-8")), 165123)
        self.assertEqual(max(len(line) + 1 for line in boot.splitlines()), 70631)

    def test_attempt3_boot_then_reply_completes(self):
        a = self._assess(self._boot() + self._assistant(self.MARKER))
        self.assertEqual((a.status, a.cause), ("provider-completed", None))
        self.assertEqual(a.assistant_event_count, 1)

    def test_attempt3_boot_before_reply_keeps_waiting(self):
        a = self._assess(self._boot())
        self.assertEqual((a.status, a.cause), ("unknown", "provider-completion-waiting"))

    def _resized(self, **sizes):
        return "".join(self._line(top, kind, sizes.get(kind, size))
                       for top, kind, size in self.BOOT_PROFILE)

    def test_larger_boot_payloads_still_complete(self):
        # Orchestrator's instructions attachment is larger than recovery's. The last case
        # puts a fresh session's whole delta just under the 2 MiB file cap.
        cases = {
            "instructions 70 KB": {"instructions": 70_000},
            "instructions 150 KB": {"instructions": 150_000},
            "instructions 400 KB": {"instructions": 400_000},
            "delta near the cap": {"instructions": 1_000_000, "skill_listing": 900_000},
        }
        for name, sizes in cases.items():
            with self.subTest(name):
                body = self._resized(**sizes) + self._assistant(self.MARKER)
                if name == "delta near the cap":
                    self.assertGreater(len(body), 2 * 1024 * 1024 - 150_000)
                    self.assertLessEqual(len(body), claude_adapter._MAX_TRANSCRIPT_BYTES)
                a = self._assess(body)
                self.assertEqual((a.status, a.cause), ("provider-completed", None))

    def test_an_over_cap_line_is_skipped_not_malformed(self):
        # The cap is on the line without its newline.
        huge = self._line("attachment", "instructions", claude_adapter._MAX_JSONL_LINE_BYTES + 2)
        a = self._assess(huge + self._assistant(self.MARKER))
        self.assertEqual((a.status, a.cause), ("provider-completed", None))

    def test_a_large_reply_line_under_the_cap_is_parsed(self):
        # An xhigh-effort reply can carry a big thinking block. A 200 KB reply
        # line is under the cap, so it is parsed and its nonce counts.
        reply = self._assistant(self.MARKER, pad=200_000)
        self.assertLess(len(reply), claude_adapter._MAX_JSONL_LINE_BYTES)
        a = self._assess(self._boot() + reply)
        self.assertEqual((a.status, a.cause), ("provider-completed", None))

    def test_a_nonce_inside_an_over_cap_line_is_never_accepted(self):
        # Skipping can only MISS a reply, never pass one: an assistant line over
        # the cap is not parsed, so its nonce doesn't count.
        huge = self._assistant(self.MARKER, pad=claude_adapter._MAX_JSONL_LINE_BYTES)
        self.assertGreater(len(huge), claude_adapter._MAX_JSONL_LINE_BYTES)
        a = self._assess(self._boot() + huge)
        self.assertEqual((a.status, a.cause), ("unknown", "provider-completion-waiting"))
        self.assertEqual(a.assistant_event_count, 0)

    def test_an_undecodable_line_is_still_malformed(self):
        a = self._assess(self._boot() + "{not json\n" + self._assistant(self.MARKER))
        self.assertEqual((a.status, a.cause),
                         ("operational-readiness", "provider-completion-transcript-malformed"))


if __name__ == "__main__":
    unittest.main()
