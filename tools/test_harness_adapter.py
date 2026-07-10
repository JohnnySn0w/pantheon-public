"""Unit tests for tools/harness_adapter/ — PantheonHarnessAdapter skeleton.

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
  5. Session seam honesty — CodexHarnessAdapter.resume_session raises with T-0822
  6. Inbound bridge honesty — CodexHarnessAdapter.inject_inbound raises with T-0714
  7. Claude not-abstracted props return dicts (no raise); codex ones too
  8. Role/domain axis — role_domain accessible on both adapters after construction
  9. HARNESS_NAME class attribute on each concrete class
 10. PantheonHarnessAdapter cannot be instantiated directly (ABC enforcement)
"""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from tools.harness_adapter import (
    ClaudeHarnessAdapter,
    CodexHarnessAdapter,
    PantheonHarnessAdapter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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

    def test_mcp_server_config_path_ends_with_settings_json(self) -> None:
        p = self.adapter.mcp_server_config_path()
        self.assertEqual(p.name, "settings.json")

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

    def test_approval_defaults_has_bypass(self) -> None:
        result = self.adapter.approval_defaults()
        self.assertIsInstance(result, dict)
        # Must encode the bypass-permissions mechanism
        combined = str(result)
        self.assertIn("bypass-permissions", combined)

    def test_spawn_subagent_returns_dict(self) -> None:
        result = self.adapter.spawn_subagent("specialist", {"model": "claude-opus-4"})
        self.assertIsInstance(result, dict)
        self.assertEqual(result["harness"], "claude")
        self.assertIn("role", result)

    def test_inject_inbound_is_stub(self) -> None:
        # claude inject_inbound is a documented stub at skeleton tier; it must
        # not raise (the bridge mechanism exists, just not wired in the skeleton).
        # It may return None silently.
        try:
            self.adapter.inject_inbound("test message")
        except NotImplementedError:
            self.fail("ClaudeHarnessAdapter.inject_inbound should not raise at skeleton tier")


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

    def test_app_server_config_returns_dict_with_supported_false(self) -> None:
        result = self.adapter.app_server_config
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("supported", True), "no claude-code analog → supported=False")

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


# ---------------------------------------------------------------------------
# 8. CodexHarnessAdapter — honest seams (resume_session + inject_inbound)
# ---------------------------------------------------------------------------

class TestCodexHonestSeams(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = CodexHarnessAdapter("specialist")

    def test_resume_session_raises_not_implemented(self) -> None:
        """Must raise NotImplementedError — deferred per T-0822."""
        with self.assertRaises(NotImplementedError) as ctx:
            self.adapter.resume_session("some-session-id")
        msg = str(ctx.exception)
        self.assertIn("T-0822", msg, "error message must cite T-0822")

    def test_resume_session_error_mentions_sqlite(self) -> None:
        with self.assertRaises(NotImplementedError) as ctx:
            self.adapter.resume_session("abc")
        self.assertIn("SQLite", str(ctx.exception))

    def test_inject_inbound_raises_not_implemented(self) -> None:
        """Must raise NotImplementedError — codex inbound is poll-and-inject (T-0714)."""
        with self.assertRaises(NotImplementedError) as ctx:
            self.adapter.inject_inbound("hello from telegram")
        msg = str(ctx.exception)
        self.assertIn("T-0714", msg, "error message must cite T-0714")

    def test_inject_inbound_error_mentions_poll_and_inject(self) -> None:
        with self.assertRaises(NotImplementedError) as ctx:
            self.adapter.inject_inbound("msg")
        self.assertIn("poll-and-inject", str(ctx.exception))


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


if __name__ == "__main__":
    unittest.main()
