"""T-5451 (T-5446 PR-5): the codex flip is unconditional fleet policy.

Intent: no seat stays wedged on a capped Claude because a per-role key was never typed.
Covers: quota allowlist retired, hand-set flip keys rejected, schema default ON,
comms exclusion replaced by receiver readiness (shared with verify-seat).
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import yaml

from tools import codex_telegram_bridge as ctb
from tools import persona_bringup
from tools import persona_installer as pim
from tools import runtime_controller as rc
from tools.harness_adapter.diagnostics import (
    flip_key_deprecations,
    flip_key_violations,
    resolve_controller_config,
)
from tools.harness_adapter.types import RuntimeControllerConfig
from tools.test_harness_runtime_profiles import _manifest
from tools.test_persona_installer import REPO_ROOT
from tools.test_runtime_controller import _stateless_binding, _telegram_comms

INSTANCE = REPO_ROOT / "instance.yaml"


class QuotaAllowlistRetiredTests(unittest.TestCase):
    def test_constants_and_loader_are_gone(self) -> None:
        self.assertFalse(hasattr(persona_bringup, "DEFAULT_QUOTA_CHECK_PERSONAS"))
        self.assertFalse(hasattr(persona_bringup, "quota_check_personas"))

    def test_instance_yaml_has_no_personas_key(self) -> None:
        raw = yaml.safe_load(INSTANCE.read_text(encoding="utf-8"))
        self.assertNotIn("personas", raw["persona_installer"]["quota_check"])

    def test_supported_gates_only_on_fallbacks(self) -> None:
        for persona, fallbacks, expect in (
            ("artisan", ("codex/x",), (True, "supported")),
            ("orchestrator", ("codex/x",), (True, "supported")),
            ("archivist", (), (False, "no-manifest-fallback")),
        ):
            binding = mock.Mock()
            binding.persona = persona
            binding.controller.policy.fallbacks = fallbacks
            self.assertEqual(persona_bringup.quota_check_supported(binding, manifest_path=INSTANCE), expect)

    def test_installer_manifest_rejects_personas_key(self) -> None:
        raw = yaml.safe_load(INSTANCE.read_text(encoding="utf-8"))
        raw["persona_installer"]["quota_check"]["personas"] = ["specialist"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instance.yaml"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            manifest = pim.Manifest.load(path)
            with self.assertRaisesRegex(pim.ManifestError, r"T-5446.*TG 12274"):
                pim.PersonaInstaller(REPO_ROOT).plan(manifest, "specialist", root=Path(tmp) / "r")


EXPECTED_EXPLICIT_TRUE = {
    ("head-engineering-orchestrator", "fallback_return"),
    ("specialist", "fallback_return"),
    ("head-system-orchestrator", "fallback_return"),
    ("engineering-orchestrator-1", "mid_session_cap_swap"),
    ("engineering-orchestrator-1", "fallback_return"),
}


class FlipKeysPhaseATests(unittest.TestCase):
    """T-5446 Phase A: default ON; `true` tolerated (deprecated no-op); `false` forbidden."""

    def test_defaults_are_on(self) -> None:
        self.assertTrue(RuntimeControllerConfig.__dataclass_fields__["mid_session_cap_swap_enabled"].default)
        self.assertTrue(RuntimeControllerConfig.__dataclass_fields__["fallback_return_enabled"].default)

    def test_resolved_controller_is_on_without_keys(self) -> None:
        controller = resolve_controller_config(_manifest(), persona="recovery", role="recovery")
        self.assertTrue(controller.mid_session_cap_swap_enabled)
        self.assertTrue(controller.fallback_return_enabled)

    def test_true_is_tolerated_silently_and_listed_as_deprecated(self) -> None:
        import warnings

        for key in ("mid_session_cap_swap", "fallback_return"):
            manifest = copy.deepcopy(_manifest())
            manifest["harness_runtime"]["roles"]["recovery"][key] = True
            with warnings.catch_warnings():
                warnings.simplefilter("error")  # the resolver must emit nothing (shell callers eval its stderr)
                controller = resolve_controller_config(manifest, persona="recovery", role="recovery")
            self.assertTrue(controller.mid_session_cap_swap_enabled)
            self.assertTrue(controller.fallback_return_enabled)
            self.assertEqual(flip_key_violations(manifest), [])
            self.assertEqual(flip_key_deprecations(manifest), [f"harness_runtime.roles.recovery.{key}"])

    def test_false_is_a_violation_but_never_crashes_or_disables_the_flip(self) -> None:
        for key in ("mid_session_cap_swap", "fallback_return"):
            manifest = copy.deepcopy(_manifest())
            manifest["harness_runtime"]["roles"]["recovery"][key] = False
            violations = flip_key_violations(manifest)
            self.assertEqual(len(violations), 1)
            self.assertRegex(violations[0], r"T-5446.*TG 12274/12283")
            controller = resolve_controller_config(manifest, persona="recovery", role="recovery")
            self.assertTrue(controller.mid_session_cap_swap_enabled)  # T-5458: fail-safe, flag stays on
            self.assertTrue(controller.fallback_return_enabled)

    def test_non_bool_is_ignored_at_runtime_but_a_strict_violation(self) -> None:
        for key in ("mid_session_cap_swap", "fallback_return"):
            manifest = copy.deepcopy(_manifest())
            manifest["harness_runtime"]["roles"]["recovery"][key] = "yes"
            controller = resolve_controller_config(manifest, persona="recovery", role="recovery")
            self.assertTrue(controller.mid_session_cap_swap_enabled)  # T-5458 fully closed
            self.assertTrue(controller.fallback_return_enabled)
            self.assertEqual(len(flip_key_violations(manifest)), 1)

    def test_manifest_load_rejects_false(self) -> None:
        raw = yaml.safe_load(INSTANCE.read_text(encoding="utf-8"))
        raw["harness_runtime"]["roles"]["head-system-orchestrator"]["fallback_return"] = False
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instance.yaml"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            with self.assertRaisesRegex(pim.ManifestError, r"T-5446.*TG 12274/12283"):
                pim.Manifest.load(path)

    def test_instance_yaml_has_exactly_the_expected_explicit_true_keys_and_no_false(self) -> None:
        raw = yaml.safe_load(INSTANCE.read_text(encoding="utf-8"))
        found = set()
        for role, body in raw["harness_runtime"]["roles"].items():
            for key in ("mid_session_cap_swap", "fallback_return"):
                if key in body:
                    self.assertIs(body[key], True, f"{role}.{key} must be an explicit true (never false)")
                    found.add((role, key))
        self.assertEqual(found, EXPECTED_EXPLICIT_TRUE)
        self.assertEqual(flip_key_violations(raw), [])


class RetiredFlipKeyControllerReportTests(unittest.TestCase):
    """T-5458: a re-added `false` is reported (evidence + cooldown-gated alert), never fatal."""

    def _manifest_with_false(self, tmp: Path) -> Path:
        raw = yaml.safe_load(INSTANCE.read_text(encoding="utf-8"))
        raw["harness_runtime"]["roles"]["head-system-orchestrator"]["fallback_return"] = False
        path = tmp / "instance.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        return path

    def test_reports_once_per_cooldown_and_only_for_the_seats_own_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._manifest_with_false(root)
            with mock.patch.object(rc, "_route_failure_alert") as alert, \
                 mock.patch.object(rc, "_append_controller_evidence") as ev:
                rc._report_retired_flip_keys("recovery", "head-system-orchestrator", path, state_root=root)
                rc._report_retired_flip_keys("recovery", "head-system-orchestrator", path, state_root=root)
                rc._report_retired_flip_keys("orchestrator", "head-engineering-orchestrator", path, state_root=root)
            self.assertEqual(alert.call_count, 1)
            events = [c[0][1] for c in ev.call_args_list]
            self.assertEqual(events.count("retired_flip_key_ignored"), 1)
            # orchestrator's role only carries a tolerated `true`: a deprecation note, never an alert
            self.assertEqual(events.count("deprecated_flip_key_noop"), 1)

    def test_true_key_records_one_deprecation_evidence_line_per_window_and_no_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(rc, "_route_failure_alert") as alert, \
                 mock.patch.object(rc, "_append_controller_evidence") as ev:
                rc._report_retired_flip_keys("recovery", "head-system-orchestrator", INSTANCE, state_root=root)
                rc._report_retired_flip_keys("recovery", "head-system-orchestrator", INSTANCE, state_root=root)
            alert.assert_not_called()
            self.assertEqual(ev.call_count, 1)
            self.assertEqual(ev.call_args[0][1], "deprecated_flip_key_noop")

    def test_never_raises_on_an_unreadable_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc._report_retired_flip_keys("recovery", "x", Path(tmp) / "missing.yaml", state_root=Path(tmp))


class ReceiverReadinessSharedTests(unittest.TestCase):
    """One shared readiness function (bridge module) for controller and verify-seat."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        manifest = pim.Manifest.load(INSTANCE)
        self.bridge = ctb.binding_from_manifest(manifest, "specialist", root=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _by_name(self):
        return {c.name: c for c in ctb.receiver_readiness(self.bridge, managed_marker=pim.MANAGED_MARKER)}

    def test_empty_account_is_not_ready(self) -> None:
        checks = self._by_name()
        self.assertEqual(set(checks), {"poller-unit-installed", "release-current-pinned", "codex-mcp-registered"})
        self.assertFalse(any(c.ok for c in checks.values()))

    def _make_ready(self) -> None:
        b = self.bridge
        unit = b.account_home / ".config" / "systemd" / "user" / b.codex_receiver_unit
        unit.parent.mkdir(parents=True)
        unit.write_text(f"# {pim.MANAGED_MARKER}\n[Unit]\n", encoding="utf-8")
        rel = b.release_root / "releases" / b.source.revision
        rel.mkdir(parents=True)
        b.current_release.symlink_to(rel)
        cfg = b.account_home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(ctb.expected_mcp_snippet(b), encoding="utf-8")

    def test_fully_installed_is_ready(self) -> None:
        self._make_ready()
        self.assertTrue(all(c.ok for c in self._by_name().values()))

    def test_stale_release_pin_is_not_ready(self) -> None:
        self._make_ready()
        self.bridge.current_release.unlink()
        stale = self.bridge.release_root / "releases" / "1266588b"
        stale.mkdir(parents=True)
        self.bridge.current_release.symlink_to(stale)
        checks = self._by_name()
        self.assertFalse(checks["release-current-pinned"].ok)
        self.assertTrue(checks["poller-unit-installed"].ok)


def _ready(ok: bool, why: str = "") -> tuple[bool, str]:
    return ok, why


class CommsReadinessSwapGateTests(unittest.TestCase):
    NOW = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)

    def _elig(self, root, claude, codex, **kw):
        return rc._mid_session_cap_swap_eligible(
            claude, candidates=[claude, codex], holds={}, now=self.NOW, state_root=root, **kw
        )

    def _pair(self, root):
        claude = _stateless_binding(
            root, harness="claude", profile="anthropic-code",
            communications=_telegram_comms(), mid_session_cap_swap=True,
        )
        codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
        return claude, codex

    def test_comms_seat_with_ready_receiver_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._pair(root)
            with mock.patch.object(rc, "_comms_receiver_ready", return_value=_ready(True)):
                self.assertTrue(self._elig(root, claude, codex))

    def test_comms_seat_not_ready_refuses_swap_and_alerts_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._pair(root)
            with mock.patch.object(rc, "_comms_receiver_ready", return_value=_ready(False, "release-current-pinned")), \
                 mock.patch.object(rc, "_route_failure_alert") as alert:
                self.assertFalse(self._elig(root, claude, codex))
                self.assertFalse(self._elig(root, claude, codex))
            self.assertEqual(alert.call_count, 1)  # cooldown-gated
            self.assertIn("release-current-pinned", alert.call_args[0][1])

    def test_not_ready_emits_evidence_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, codex = self._pair(root)
            with mock.patch.object(rc, "_comms_receiver_ready", return_value=_ready(False, "x")), \
                 mock.patch.object(rc, "_route_failure_alert"), \
                 mock.patch.object(rc, "_append_controller_evidence") as ev:
                self._elig(root, claude, codex)
            self.assertEqual(ev.call_args[0][1], "mid_session_swap_receiver_not_ready")

    def test_readiness_error_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, _ = self._pair(root)
            with mock.patch.object(ctb, "binding_from_manifest",
                                   side_effect=ctb.BridgeError("boom")):
                ok, why = rc._comms_receiver_ready(claude, manifest_path=INSTANCE)
            self.assertFalse(ok)
            self.assertIn("boom", why)

    def test_non_comms_seat_skips_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(root, harness="claude", profile="anthropic-code", mid_session_cap_swap=True)
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            with mock.patch.object(rc, "_comms_receiver_ready", side_effect=AssertionError("must not run")):
                self.assertTrue(self._elig(root, claude, codex))

    def test_no_fallback_is_a_no_op_before_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, _ = self._pair(root)
            with mock.patch.object(rc, "_comms_receiver_ready", side_effect=AssertionError("must not run")):
                self.assertFalse(rc._mid_session_cap_swap_eligible(
                    claude, candidates=[claude], holds={}, now=self.NOW, state_root=root))
                self.assertFalse(rc._mid_session_cap_swap_eligible(
                    claude, candidates=None, holds={}, now=self.NOW, state_root=root))


class ManifestPathForwardingTests(unittest.TestCase):
    def test_block_loop_forwards_manifest_path_to_the_eligibility_gate(self) -> None:
        """T-5451: the manifest_path the loop receives reaches the readiness check
        (only the default path was covered before)."""
        sentinel = Path("/nonexistent/instance-under-test.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(
                root, harness="claude", profile="anthropic-code",
                communications=_telegram_comms(), mid_session_cap_swap=True,
            )
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            t = {"now": datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)}
            ticks = {"n": 0}

            def alive() -> bool:
                ticks["n"] += 1
                return ticks["n"] <= 2

            def sleep(seconds: float) -> None:
                t["now"] += timedelta(seconds=seconds)

            observation = SimpleNamespace(modal_present=True)
            with mock.patch.object(rc, "_observe_and_publish_mid_session_cap", return_value=observation), \
                 mock.patch.object(rc, "_mid_session_cap_swap_eligible", return_value=False) as eligible:
                rc.block_until_tmux_death(
                    claude, alive_check=alive, sleep=sleep, poll_interval_seconds=60.0,
                    capture_pane_fn=lambda: "", cap_check_interval_seconds=0.0,
                    now=lambda: t["now"], state_root=root, candidates=[claude, codex], holds={},
                    manifest_path=sentinel,
                )
            self.assertTrue(eligible.called)
            self.assertEqual(eligible.call_args.kwargs["manifest_path"], sentinel)

    def test_eligibility_forwards_manifest_path_to_readiness(self) -> None:
        sentinel = Path("/nonexistent/instance-under-test.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = _stateless_binding(
                root, harness="claude", profile="anthropic-code",
                communications=_telegram_comms(), mid_session_cap_swap=True,
            )
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            with mock.patch.object(rc, "_comms_receiver_ready", return_value=(True, "")) as ready:
                rc._mid_session_cap_swap_eligible(
                    claude, candidates=[claude, codex], holds={},
                    now=datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc), state_root=root,
                    manifest_path=sentinel,
                )
            self.assertEqual(ready.call_args.kwargs["manifest_path"], sentinel)


class FalseFlipKeyCannotDisableTheSwapTests(unittest.TestCase):
    """REBOUND 2 finding 1: a `false` flip key (own role or any other role) must not make
    the comms receiver 'unresolvable' and so refuse the reactive swap."""

    NOW = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)

    def _bad_manifest(self, tmp: Path) -> Path:
        raw = yaml.safe_load(INSTANCE.read_text(encoding="utf-8"))
        roles = raw["harness_runtime"]["roles"]
        roles["specialist"]["fallback_return"] = False  # the comms seat's own role (specialist)
        roles["engineering-orchestrator-1"]["mid_session_cap_swap"] = "no"  # an unrelated role
        path = tmp / "instance.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        return path

    def _ready_bridge(self, tmp: Path, manifest_path: Path):
        manifest = pim.Manifest.load(manifest_path, strict_flip_keys=False)
        bridge = ctb.binding_from_manifest(manifest, "specialist", root=tmp / "root")
        unit = bridge.account_home / ".config" / "systemd" / "user" / bridge.codex_receiver_unit
        unit.parent.mkdir(parents=True)
        unit.write_text(f"# {pim.MANAGED_MARKER}\n[Unit]\n", encoding="utf-8")
        rel = bridge.release_root / "releases" / bridge.source.revision
        rel.mkdir(parents=True)
        bridge.current_release.symlink_to(rel)
        cfg = bridge.account_home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(ctb.expected_mcp_snippet(bridge), encoding="utf-8")
        return bridge

    def test_strict_load_still_rejects_but_lenient_load_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._bad_manifest(Path(tmp))
            with self.assertRaises(pim.ManifestError):
                pim.Manifest.load(path)
            pim.Manifest.load(path, strict_flip_keys=False)

    def test_ready_receiver_with_bad_flip_keys_is_ready_and_swap_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._bad_manifest(root)
            bridge = self._ready_bridge(root, path)
            claude = _stateless_binding(
                root, harness="claude", profile="anthropic-code",
                communications=_telegram_comms(), mid_session_cap_swap=True,
            )
            codex = _stateless_binding(root, harness="codex", profile="interactive-chatgpt")
            claude.persona = "specialist"
            with mock.patch.object(ctb, "binding_from_manifest", return_value=bridge):
                self.assertEqual(rc._comms_receiver_ready(claude, manifest_path=path), (True, ""))
                self.assertTrue(rc._mid_session_cap_swap_eligible(
                    claude, candidates=[claude, codex], holds={}, now=self.NOW, state_root=root,
                    manifest_path=path,
                ))

    def test_ignored_false_still_reports_evidence_and_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._bad_manifest(root)
            with mock.patch.object(rc, "_route_failure_alert") as alert, \
                 mock.patch.object(rc, "_append_controller_evidence") as ev:
                rc._report_retired_flip_keys("specialist", "specialist", path, state_root=root)
            self.assertEqual(alert.call_count, 1)
            self.assertEqual(ev.call_args[0][1], "retired_flip_key_ignored")

    # T-5461: installer-tier tools load strictly by design; everything else is runtime-path.
    STRICT_MANIFEST_LOADERS = frozenset({
        "tools/host_control/planner.py",
        "tools/persona_installer.py",
        "tools/persona_installer_apply.py",
        "tools/persona_installer_verify_seat.py",
        "tools/systemd_drift_lint.py",
        "tools/t2381_live_acceptance.py",
        "tools/t2611_recovery_persona_apply.py",
    })

    @staticmethod
    def _manifest_loader_calls(tree: "ast.AST") -> "list[ast.Call]":
        """Manifest.load / Manifest.from_bytes calls, through module or alias imports."""
        import ast

        aliases = {"Manifest"} | {
            a.asname for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
            for a in n.names if a.name == "Manifest" and a.asname
        }
        calls = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("load", "from_bytes")):
                continue
            owner = node.func.value
            name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", None)
            if name in aliases:
                calls.append(node)
        return calls

    def test_no_runtime_manifest_loader_is_strict(self) -> None:
        """Every Manifest.load/from_bytes outside the strict allowlist passes strict_flip_keys=False."""
        import ast

        seen_strict = set()
        for path in sorted((REPO_ROOT / "tools").rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if path.name.startswith(("test_", "_test")):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            for call in self._manifest_loader_calls(tree):
                strict = next((k.value for k in call.keywords if k.arg == "strict_flip_keys"), None)
                lenient = isinstance(strict, ast.Constant) and strict.value is False
                if rel in self.STRICT_MANIFEST_LOADERS:
                    seen_strict.add(rel)
                    continue
                self.assertTrue(lenient, f"{rel}:{call.lineno}: {ast.unparse(call)} must pass strict_flip_keys=False")
        self.assertEqual(seen_strict, self.STRICT_MANIFEST_LOADERS, "stale strict-allowlist entry")

    def test_manifest_loader_scan_sees_aliases_and_multiline_calls(self) -> None:
        import ast

        src = (
            "from persona_installer import Manifest as M\n"
            "import persona_installer as pi\n"
            "M.load(p)\n"
            "pi.Manifest.from_bytes(\n    p,\n    raw,\n    strict_flip_keys=False,\n)\n"
            "other.load(p)\n"
        )
        calls = self._manifest_loader_calls(ast.parse(src))
        self.assertEqual([c.lineno for c in calls], [3, 4])


class FlipPolicyReportTests(unittest.TestCase):
    def test_report_is_read_only_and_computes_trigger_from_supplied_usage(self) -> None:
        from tools import flip_policy_report as fpr

        with mock.patch.object(fpr, "_comms_readiness", return_value="ready"):
            rows = {r["persona"]: r for r in fpr.build_report(INSTANCE, usage={"orchestrator": 96.0, "artisan": 80.0})}
        self.assertIsNone(rows["archivist"]["fallback"])
        self.assertTrue(rows["orchestrator"]["planned_transition_on_next_tick"])
        self.assertFalse(rows["artisan"]["planned_transition_on_next_tick"])
        self.assertIsNone(rows["recovery"]["planned_transition_on_next_tick"])  # usage not supplied
        for persona in ("orchestrator", "artisan", "specialist", "recovery", "mason"):
            self.assertTrue(rows[persona]["quota_check_supported"])
            self.assertTrue(rows[persona]["mid_session_cap_swap"])
            self.assertTrue(rows[persona]["fallback_return"])


if __name__ == "__main__":
    unittest.main()
