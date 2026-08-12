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
import inspect
import io
import json
import tempfile
import tomllib  # stdlib ≥3.11 — the declared floor (T-2733: production harness_adapter needs it)
import unittest
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
    ensure_project_trusted,
    ensure_update_check_disabled,
    gc_stale_project_trust_entries,
    gc_trust_entries_cli,
)
from tools.harness_adapter.types import HarnessLaunchConfig, TmuxTarget
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

        def build_interactive_launch(self, target):
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

    def test_legacy_interactive_launch_still_pins_default_permission_mode(self) -> None:
        command = self.adapter.build_interactive_launch(
            TmuxTarget(session="recovery", window="harness", pane="0")
        )
        self.assertEqual(command.argv.count("--permission-mode"), 1)
        self.assertEqual(command.argv[-2:], ("--permission-mode", "default"))

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
            TmuxTarget(session="recovery", window="harness", pane="0")
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
            TmuxTarget(session="recovery", window="harness", pane="0")
        )
        self.assertNotIn("--channels", command.argv)

    def test_approval_defaults_uses_explicit_default_and_scoped_allowlist(self) -> None:
        result = self.adapter.approval_defaults()
        self.assertIsInstance(result, dict)
        self.assertEqual(result["permission_mode"], "default")
        self.assertEqual(result["flag"], "--permission-mode default")
        self.assertEqual(result["tool_approval"], "scoped-permissions.allow")
        self.assertNotIn("bypass", str(result).lower())

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
# 6. ClaudeHarnessAdapter — not-abstracted props return dicts (no raise)
# ---------------------------------------------------------------------------

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
        self.assertEqual(result["sandbox_mode"], "workspace-write")

    def test_approval_defaults_has_network_access(self) -> None:
        result = self.adapter.approval_defaults()
        nested = result.get("sandbox_workspace_write", {})
        self.assertTrue(
            nested.get("network_access"),
            "ORG-031 §ii requires network_access=true",
        )

    def test_spawn_subagent_returns_dict(self) -> None:
        result = self.adapter.spawn_subagent("specialist", {})
        self.assertIsInstance(result, dict)
        self.assertEqual(result["harness"], "codex")

    def test_build_interactive_launch_appends_hook_trust_bypass_flag(self) -> None:
        # T-2597 wedge #6: --dangerously-bypass-hook-trust is CLI-only (not a
        # config.toml key), so it must be appended on every interactive
        # launch rather than pre-seeded.
        command = self.adapter.build_interactive_launch(
            TmuxTarget(session="pantheon-test", window="harness", pane="0")
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
            config_path.write_text("check_for_update_on_startup = true\n", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
