"""Tests for tools.bringup_acceptance.harness_runtime (T-2556 phase 7 / T-2569).

Four legs, each independently exercised:

* ``_diagnose_verdict`` -- pure filter logic over a hand-built
  ``DiagnosticReport`` (authoritative-only exit-0, auth_material
  contract-regression guard). A separate real end-to-end test runs the
  actual ``persona_bringup.diagnose_persona`` against the real ``recovery``
  binding (mirrors test_bringup_acceptance_mailbox_roundtrip.py's own
  "real transport against a disposable tree" convention) -- structural
  assertions only (environment-dependent pass/fail is not asserted).
* readiness-gate leg -- REAL isolated ``tmux -S <private socket>`` (no
  systemd scope, no default-server interaction at all), positive +
  negative control, against the REAL exported
  ``tools.submit_verify.ready_for_submission``.
* ensure_runtime-chain leg -- the REAL public
  ``runtime_controller.ensure_runtime`` with systemd/tmux-touching
  internals mocked, reusing test_runtime_controller.py's own seam.
* kick_lands_executes leg -- asserts the deferred-to-T-2570 evidence
  shape (no live attempt is made off-prod; see harness_runtime.py's
  module docstring for the two structural findings that drove this).

No live production systemd/tmux server is ever touched by this suite.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import codex_telegram_bridge as bridge
from tools import persona_bringup
from tools import runtime_controller as rc
from tools import t2611_recovery_persona_apply as persona_apply
from tools.bringup_acceptance import harness_runtime
from tools.bringup_acceptance.harness_runtime import (
    COMPOSITE_ID,
    DEFERRED_TO_TICKET,
    MODULE_ID,
    HarnessRuntimeModule,
    _diagnose_verdict,
)
from tools.bringup_acceptance.types import (
    HardStop,
    InventoryPathEntry,
    RunContext,
    TargetInventory,
)
from tools.harness_adapter.types import DeliveryReceipt, DiagnosticObservation, DiagnosticReport
from tools.harness_adapter.types import ReceiverStateProof
from tools.persona_installer import Manifest, PersonaInstaller, SimulatedHostBackend


def _fixture_source_provenance(**kwargs: object) -> rc.RuntimeSourceProvenance:
    """Mirrors test_runtime_controller.py's own ``_fixture_source_provenance``/
    ``_provenance_patch`` helpers -- the real ``_controller_source_provenance``
    requires a clean git worktree + a full-repo checkout, neither of which an
    acceptance-module test should depend on."""
    source_hashes = {relative: "a" * 64 for relative in rc.RUNTIME_SOURCE_HASH_PATHS}
    return rc.RuntimeSourceProvenance(
        revision="1" * 40,
        source_hashes=source_hashes,
        source_manifest_sha256=rc._source_manifest_hash(source_hashes),
        runtime_start_boundary_at=kwargs["runtime_start_boundary_at"],
        runtime_start_observed_at=kwargs["runtime_start_observed_at"],
    )


def _provenance_patch():
    return mock.patch.object(
        rc, "_controller_source_provenance",
        side_effect=lambda **kwargs: _fixture_source_provenance(**kwargs),
    )


def _run_context(root: Path) -> RunContext:
    return RunContext(
        run_id="20260805T120000Z",
        persona="recovery",
        account="claude-fallback",
        harness="claude",
        profile="interactive",
        hub_path=root / "hub",
        state_path=root / "state",
        evidence_root=root / "evidence",
        baseline_ref="deadbeef" * 5,
    )


def _orchestrator_context(root: Path) -> RunContext:
    return RunContext(
        run_id="20260814T120000Z",
        persona="orchestrator",
        account="claude",
        harness="claude",
        profile="interactive-subscription",
        hub_path=root / "hub",
        state_path=root / "state",
        evidence_root=root / "evidence",
        baseline_ref="deadbeef" * 5,
    )


def _observation(
    check_id: str, *, status: str = "pass", authoritative: bool = True
) -> DiagnosticObservation:
    return DiagnosticObservation(
        check_id=check_id, status=status, summary="fixture",
        authoritative=authoritative, evidence={},
    )


def _report(*observations: DiagnosticObservation) -> DiagnosticReport:
    return DiagnosticReport(
        harness="claude", execution_context="test:fixture", observations=tuple(observations),
    )


# --------------------------------------------------------------------- declare


class DeclareTest(unittest.TestCase):
    def test_applies_when_inventory_marks_harness_runtime_present(self) -> None:
        module = HarnessRuntimeModule()
        inventory = TargetInventory(
            persona="recovery",
            entries=(
                InventoryPathEntry(
                    path=Path("pantheon-recovery:harness.0"), tier="core", status="applies",
                    module_id=MODULE_ID,
                ),
            ),
        )
        decl = module.declare(inventory)
        self.assertTrue(decl.applies)
        self.assertEqual(decl.tier, "core")
        self.assertIn(COMPOSITE_ID, decl.contributes_to)

    def test_does_not_apply_when_no_matching_entries(self) -> None:
        module = HarnessRuntimeModule()
        inventory = TargetInventory(persona="recovery", entries=())
        decl = module.declare(inventory)
        self.assertFalse(decl.applies)


# ------------------------------------------------------------- diagnose verdict


class DiagnoseVerdictTest(unittest.TestCase):
    def test_exit_zero_when_only_non_authoritative_check_fails(self) -> None:
        report = _report(
            _observation("claude.executable", status="pass"),
            _observation(
                "claude.auth_material", status="unavailable", authoritative=False
            ),
        )
        verdict = _diagnose_verdict(report)
        self.assertEqual(verdict["exit_code"], 0)
        self.assertEqual(verdict["authoritative_failures"], [])

    def test_exit_one_when_an_authoritative_check_fails(self) -> None:
        report = _report(
            _observation("claude.executable", status="fail"),
            _observation(
                "claude.auth_material", status="unavailable", authoritative=False
            ),
        )
        verdict = _diagnose_verdict(report)
        self.assertEqual(verdict["exit_code"], 1)
        self.assertEqual(verdict["authoritative_failures"], ["claude.executable"])

    def test_auth_material_unavailable_never_counts_as_a_failure(self) -> None:
        # binding contract: auth_material=unavailable is non-authoritative --
        # it must never fail the daemon, even though its own status string
        # is not "pass".
        report = _report(
            _observation(
                "claude.auth_material", status="unavailable", authoritative=False
            ),
        )
        verdict = _diagnose_verdict(report)
        self.assertEqual(verdict["exit_code"], 0)

    def test_hard_stops_if_auth_material_missing_entirely(self) -> None:
        report = _report(_observation("claude.executable", status="pass"))
        with self.assertRaises(HardStop):
            _diagnose_verdict(report)

    def test_hard_stops_if_auth_material_flips_authoritative(self) -> None:
        report = _report(
            _observation("claude.auth_material", status="unavailable", authoritative=True)
        )
        with self.assertRaises(HardStop):
            _diagnose_verdict(report)


class ThothCodexBootstrapTest(unittest.TestCase):
    def test_orchestrator_controller_bootstrap_preserves_mailbox_wake_and_pane_logging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proof = harness_runtime.run_codex_controller_bootstrap_leg(
                _orchestrator_context(root),
                manifest_path=Path("instance.yaml"),
                mailbox_hub=root / "mail",
                codex_executable=Path("/usr/bin/codex"),
            )
        self.assertEqual(proof["status"], "configured")
        self.assertEqual(proof["mode"], "configured-executable")
        self.assertEqual(proof["mailbox_wake"], "preserved-from-manifest")
        self.assertEqual(proof["pane_logging"], "preserved-from-post-start")
        self.assertEqual(proof["restart_route"], "runtime-controller")

    def test_orchestrator_runtime_returns_codex_ready_after_bootstrap_from_clean_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = Path("instance.yaml")
            manifest = Manifest.load(manifest_path)
            plan = PersonaInstaller(Path(__file__).resolve().parent.parent).plan(
                manifest,
                "orchestrator",
                harness_override="claude",
                root=root / "projection",
            )
            operation_ids = [operation.operation_id for operation in plan.operations]
            self.assertIn("codex-standing-instructions.install", operation_ids)
            self.assertIn("codex-telegram.unit-install", operation_ids)
            self.assertIn("codex-telegram.disabled-first", operation_ids)

            primary = persona_bringup.load_binding(
                manifest_path,
                "orchestrator",
                mailbox_hub=root / "mail",
            )
            fallback = persona_bringup.first_fallback_binding(manifest_path, primary)
            self.assertIsNotNone(fallback)
            assert fallback is not None
            self.assertEqual(primary.launch.harness, "claude")
            self.assertEqual(fallback.launch.harness, "codex")

            state_root = root / "personas"

            with mock.patch.object(rc, "pane_exists", return_value=False), mock.patch.object(
                rc, "_start_runtime", return_value=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False)
            ), mock.patch.object(rc, "_verify_started_runtime"), mock.patch.object(
                rc, "_verify_runtime_input_ready"
            ), mock.patch.object(rc, "_run_post_start_commands"), _provenance_patch():
                current_handle = rc.ensure_runtime(
                    primary,
                    adapter=mock.Mock(),
                    state_root=state_root,
                )

            class ReceiverBackend:
                def __init__(self) -> None:
                    self.events: list[str] = []

                def execute(self, action: str, binding) -> dict:
                    self.events.append(action)
                    return {"ok": True, "action": action, "persona": binding.persona}

                def prove_receiver_state(self, binding, expected: str) -> ReceiverStateProof:
                    harness = binding.launch.harness
                    self.events.append(f"proof:{harness}:{expected}")
                    return ReceiverStateProof(expected, {"expected": expected, "harness": harness})

            backend = ReceiverBackend()

            def replace_runtime(binding, *, state_root: Path | None, context=None, on_started=None):
                del context
                if on_started is not None:
                    on_started(
                        rc._RuntimeStartEvent(
                            started=rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, False),
                            boundary=rc._utc_now(),
                        )
                    )
                rc._atomic_json(
                    Path(state_root) / binding.persona / "runtime-controller" / "runtime.json",
                    rc._runtime_payload(binding, session_id="replacement", started_at=rc._utc_now()),
                )
                return rc.RuntimeHandle(binding.launch, binding.controller.tmux, "replacement", rc._utc_now())

            with mock.patch.object(rc, "pane_exists", return_value=True), mock.patch.object(
                rc, "_replace_runtime", side_effect=replace_runtime
            ), mock.patch.object(rc, "_prompt_successor"):
                with self.assertRaises(rc.RuntimeTransitionPending):
                    rc._operator_target_swap_resolved(
                        primary,
                        fallback,
                        trigger="operator",
                        correlation_id="t2953-test-orchestrator-to-codex",
                        state_root=state_root,
                        backend=backend,
                        current_handle=current_handle,
                    )
            events = backend.events
            transition = rc.transition_state("orchestrator", state_root=state_root)
            self.assertTrue(transition["runtime_swapped"])
            self.assertTrue(transition["receiver_transferred"])
            self.assertEqual(transition["phase"], "awaiting-successor-read")
            self.assertEqual(transition["receiver_post_receipt"]["operations"], [
                "probe-claude-absent",
                "start-codex-poller",
                "probe-codex-present",
                "health-codex-no-409",
            ])
            old_absent_positions = [
                index for index, action in enumerate(events) if action == "probe-claude-absent"
            ]
            self.assertTrue(old_absent_positions)
            self.assertIn("stop-dormant-claude-receiver", events)
            self.assertLess(old_absent_positions[-1], events.index("start-codex-poller"))
            self.assertLess(events.index("start-codex-poller"), events.index("probe-codex-present"))

            # T-2966: rc.recorded_active_runtime()/ActiveRuntimeIdentity dissolved
            # (self-referential controller state, R31->R38). Read the same
            # runtime.json the old helper wrapped, directly -- this preserves the
            # assertion (codex became the recorded active runtime) without the
            # dissolved dataclass.
            runtime_state = rc._read_json(rc._persona_state("orchestrator", state_root) / "runtime.json")
            self.assertEqual(runtime_state.get("schema"), "pantheon-runtime-state/v1")
            self.assertEqual(runtime_state.get("harness"), "codex")
            self.assertEqual(runtime_state.get("profile"), "interactive-chatgpt")

            proof = harness_runtime.run_codex_controller_bootstrap_leg(
                _orchestrator_context(root),
                manifest_path=manifest_path,
                mailbox_hub=root / "mail",
                codex_executable=Path("/usr/bin/codex"),
            )
        self.assertEqual(proof["status"], "configured")
        self.assertEqual(proof["mode"], "configured-executable")
        self.assertEqual(proof["primary"], "claude/interactive-subscription")
        self.assertEqual(proof["fallback"], "codex/interactive-chatgpt")
        self.assertEqual(proof["tmux_target"], "claude-telegram-bot:harness.0")

    def test_orchestrator_clean_bootstrap_falls_back_to_codex_and_hands_off_single_receiver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = Path("instance.yaml")
            manifest = Manifest.load(manifest_path)
            plan = PersonaInstaller(Path(__file__).resolve().parent.parent).plan(
                manifest,
                "orchestrator",
                harness_override="claude",
                root=root / "projection",
            )
            operation_ids = [operation.operation_id for operation in plan.operations]
            self.assertIn("codex-standing-instructions.install", operation_ids)
            self.assertIn("codex-telegram.disabled-first", operation_ids)
            stage_backend = SimulatedHostBackend()
            stage_code, stage_summary = persona_apply.stage_disabled(
                manifest_path,
                "orchestrator",
                "claude",
                Path(__file__).resolve().parent.parent,
                "r5-integration",
                backend=stage_backend,
                root=root / "projection",
            )
            self.assertEqual(stage_code, 0, stage_summary)
            self.assertIn("codex-standing-instructions.install", stage_backend.events)
            self.assertNotIn("connective.unit-fire", stage_backend.events)
            activate_code, activate_summary = persona_apply.activate_staged(
                manifest_path,
                "orchestrator",
                "claude",
                Path(__file__).resolve().parent.parent,
                "r5-integration",
                backend=stage_backend,
                root=root / "projection",
            )
            self.assertEqual(activate_code, 0, activate_summary)
            self.assertIn("connective.unit-fire", stage_backend.events)
            sheet = bridge.command_sheet("orchestrator", expected_pantheon_revision="4" * 40)
            sheet_ids = [step["step_id"] for step in sheet["steps"]]
            self.assertLess(
                sheet_ids.index("08-stage-disabled-instructions-and-bridge"),
                sheet_ids.index("10-codex-instruction-discovery"),
            )
            self.assertLess(
                sheet_ids.index("10-codex-instruction-discovery"),
                sheet_ids.index("14-stage1-cutover-execute"),
            )
            self.assertNotIn("11-start-controller-consume-viability", sheet_ids)

            primary = persona_bringup.load_binding(manifest_path, "orchestrator", mailbox_hub=root / "mail")
            candidates = persona_bringup.configured_runtime_bindings(manifest_path, primary)
            fallback = candidates[1]
            self.assertEqual(
                [persona_bringup.runtime_reference(item) for item in candidates],
                ["claude/interactive-subscription", "codex/interactive-chatgpt"],
            )
            from tools import mailbox

            mailbox.provision(primary.mailbox)
            mailbox.send(primary.mailbox, {"body": "wake"}, "specialist", subject="viability")
            state_root = root / "personas"
            events: list[str] = []

            class Adapter:
                def __init__(self, binding) -> None:
                    self.binding = binding

                def quota_observer(self, state_dir: Path):
                    del state_dir
                    return mock.Mock(
                        read=lambda: mock.Mock(
                            profile=f"{self.binding.launch.harness}/{self.binding.launch.profile}",
                            windows=(),
                            freshness="unavailable",
                            evidence={"authoritative_limit": "false"},
                        )
                    )

            adapters = {item.launch.harness: Adapter(item) for item in candidates}

            class ReceiverBackend:
                def execute(self, action: str, binding) -> dict[str, object]:
                    events.append(f"{binding.launch.harness}:{action}")
                    return {"ok": True, "action": action}

                def prove_receiver_state(self, binding, expected: str) -> ReceiverStateProof:
                    events.append(f"{binding.launch.harness}:proof-{expected}")
                    return ReceiverStateProof(expected, {"expected": expected, "harness": binding.launch.harness})

            def start_runtime(binding, adapter, *, replace=False, context=None):
                del adapter, replace, context
                events.append(f"{binding.launch.harness}:start")
                return rc.RuntimeStart(rc.TMUX_RUNTIME_SCOPE_UNIT, True)

            def verify_input(binding, started, *, state_root=None, adapter=None, context=None):
                del state_root, adapter, context
                if binding.launch.harness == "claude":
                    raise rc.RuntimeOperationalFailure(
                        "input not ready",
                        failure_class="operational-readiness",
                        cause="runtime_input_not_ready",
                        binding_runtime=persona_bringup.runtime_reference(binding),
                        runtime_start=started,
                        created_scope=started.created_scope,
                        pane_may_be_mutated=True,
                        readiness_evidence={"event": "runtime_input_not_ready"},
                    )

            submitted: dict[str, object] = {}

            def submit(handle, request, **kwargs):
                submitted["handle"] = handle
                submitted["request"] = request
                submitted["kwargs"] = kwargs
                return DeliveryReceipt(
                    harness="codex",
                    correlation_id=kwargs["correlation_id"],
                    state="submitted",
                    mechanism="tmux-send-keys",
                    retryable=False,
                    evidence={"profile": handle.profile.profile},
                )

            with mock.patch.object(persona_bringup, "build_adapter", side_effect=lambda binding, *, harness_home: adapters[binding.launch.harness]), mock.patch.object(
                rc, "pane_exists", return_value=False
            ), mock.patch.object(rc, "_start_runtime", side_effect=start_runtime), mock.patch.object(
                rc, "_verify_started_runtime"
            ), mock.patch.object(rc, "_verify_runtime_input_ready", side_effect=verify_input), mock.patch.object(
                rc, "_run_post_start_commands"
            ), mock.patch.object(rc, "_stop_created_scope", side_effect=lambda scope: events.append(f"stop:{scope}")), mock.patch.object(
                rc, "submit_to_runtime", side_effect=submit
            ), mock.patch("tools.mailbox_wake.session_key", return_value="session-key"), mock.patch(
                "tools.mailbox_wake.pane_is_idle", return_value=True
            ), _provenance_patch():
                result = persona_bringup.wake_persistent_runtime(
                    primary,
                    adapter=adapters["claude"],
                    fallback_binding=fallback,
                    primary_binding=primary,
                    runtime_candidates=candidates,
                    receiver_backend=ReceiverBackend(),
                    state_dir=state_root,
                    cwd=root,
                    now=1.0,
                )

            self.assertEqual(result["action"], "wake")
            self.assertEqual(result["harness"], "codex")
            self.assertEqual(result["profile"], "interactive-chatgpt")
            self.assertEqual(result["runtime_viability"].selected_runtime, "codex/interactive-chatgpt")
            self.assertEqual(submitted["handle"].profile.harness, "codex")
            self.assertIn("Run it bare", submitted["request"].prompt)
            self.assertIn(str(primary.mailbox), submitted["request"].prompt)
            state = rc.initial_viability_state("orchestrator", state_root=state_root)
            self.assertEqual(state["attempts"][0]["failure_class"], "operational-readiness")
            self.assertLess(events.index(f"stop:{rc.TMUX_RUNTIME_SCOPE_UNIT}"), events.index("claude:proof-absent"))
            self.assertLess(events.index("claude:proof-absent"), events.index("codex:start"))
            self.assertLess(events.index("codex:proof-absent"), events.index("codex:start-codex-poller"))
            self.assertLess(events.index("codex:start-codex-poller"), events.index("codex:probe-codex-present"))

    def test_hard_stops_on_unexpected_auth_material_status(self) -> None:
        report = _report(
            _observation("claude.auth_material", status="warn", authoritative=False)
        )
        with self.assertRaises(HardStop):
            _diagnose_verdict(report)


def _disposable_persona_binding(root: Path) -> "persona_bringup.PersonaBinding":
    """A fully-controlled PersonaBinding -- every path under ``root`` (not a
    real account's real home), so the real diagnose_persona() call below
    never depends on which seat runs this suite (unlike routing through
    load_binding's real ``pwd.getpwnam``-resolved account_home, which hit a
    real cross-account PermissionError on a sandboxed seat)."""
    from tools.harness_adapter.types import FailoverPolicy, HarnessLaunchConfig, RuntimeControllerConfig, TmuxTarget

    account_home = root / "account-home"
    mailbox = root / "hub" / "recovery"
    (mailbox / "new").mkdir(parents=True)
    pinned_rules = persona_bringup.REPO_ROOT / "pinned-rules" / "recovery.md"
    return persona_bringup.PersonaBinding(
        role="recovery",
        persona="recovery",
        account="claude-fallback",
        configured_harness="claude",
        selected_harness="claude",
        account_home=account_home,
        pinned_rules=pinned_rules,
        mailbox=mailbox,
        launch_cwd=root,
        launch=HarnessLaunchConfig(
            harness="claude", profile="interactive", executable=Path("/bin/echo"),
            model=None, mode="interactive", meter="subscription", quota_source="statusline",
            native={}, sources=("test",),
        ),
        controller=RuntimeControllerConfig(
            tmux=TmuxTarget("pantheon-recovery", "harness", "0"), policy=FailoverPolicy(5.0, ()),
        ),
    )


class DiagnoseLegRealBindingSmokeTest(unittest.TestCase):
    """Structural-only: real diagnose_persona() against a fully-disposable
    binding must not crash and must carry the expected shape. Pass/fail of
    individual checks is environment-dependent and is deliberately NOT
    asserted here."""

    def test_real_diagnose_persona_produces_a_well_shaped_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = _disposable_persona_binding(root)
            report = persona_bringup.diagnose_persona(
                binding, harness_home=binding.account_home, cwd=persona_bringup.REPO_ROOT,
                execution_context="test:disposable",
            )
            detail = harness_runtime._diagnose_verdict(report)
            self.assertIn(detail["exit_code"], (0, 1))
            self.assertTrue(detail["auth_material_check_ids"])
            self.assertIsInstance(detail["authoritative_failures"], list)


# ------------------------------------------------------------- readiness gate


class ReadinessGateLegTest(unittest.TestCase):
    def test_positive_and_negative_control_against_real_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            module = HarnessRuntimeModule()
            detail = module._readiness_gate_leg(ctx, evidence_root=root / "evidence")
            self.assertTrue(detail["positive_control"]["ready"])
            self.assertFalse(detail["negative_control"]["ready"])
            self.assertIsNone(module._socket_path)  # torn down, not left set as live

    def test_restore_is_clean_after_readiness_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            module = HarnessRuntimeModule()
            module._readiness_gate_leg(ctx, evidence_root=root / "evidence")
            restore_evidence = module.restore(ctx)
            self.assertTrue(restore_evidence.restored)


# --------------------------------------------------------- ensure_runtime chain


class EnsureRuntimeChainLegTest(unittest.TestCase):
    def test_chain_runs_in_order_and_publishes_runtime_json(self) -> None:
        # _ensure_runtime_chain_leg does its OWN internal mock.patch.object of
        # rc's systemd/tmux-touching privates (that IS the leg -- see its
        # docstring); this test must not ALSO patch the same targets from the
        # outside, or ensure_runtime's calls land on the outer mock while the
        # inner one (whose side_effects build call_order) never sees them.
        # Only _controller_source_provenance (read-only, unmocked by the leg
        # itself) needs a test-side fixture, since the real one requires a
        # clean git worktree.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            with _provenance_patch():
                detail = harness_runtime._ensure_runtime_chain_leg(
                    ctx, state_root=root / "chain-state"
                )
            self.assertEqual(
                detail["call_order"],
                ["start_runtime", "verify_started_runtime", "verify_runtime_input_ready"],
            )
            self.assertEqual(detail["launch_cwd"], str(ctx.state_path))
            self.assertTrue(detail["handle_profile_matches"])
            self.assertTrue(detail["handle_tmux_matches"])
            state_path = root / "chain-state" / ctx.persona / "runtime-controller" / "runtime.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["schema"], "pantheon-runtime-state/v1")
            self.assertEqual(state["persona"], ctx.persona)
            self.assertEqual(state["harness"], ctx.harness)
            self.assertEqual(state["profile"], ctx.profile)
            self.assertEqual(
                state["runtime_scope_unit"], f"bringup-acceptance-{ctx.run_id}.scope"
            )


# ----------------------------------------------------------------------- prove


class ProveTest(unittest.TestCase):
    def test_prove_records_all_four_legs_and_names_the_deferred_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            (root / "hub" / "recovery" / "new").mkdir(parents=True)
            module = HarnessRuntimeModule()
            fixture_report = _report(
                _observation("claude.executable", status="pass"),
                _observation(
                    "claude.auth_material", status="unavailable", authoritative=False
                ),
            )
            # ensure_runtime_chain's own leg does its OWN internal patching of
            # rc's systemd/tmux privates (see EnsureRuntimeChainLegTest) --
            # only _controller_source_provenance (left real by that leg) needs
            # a fixture here, same reason as that test.
            with mock.patch.object(
                harness_runtime, "_resolve_mailbox_hub", return_value=root / "hub"
            ), mock.patch.object(
                # Isolates prove()'s LEG-WIRING from diagnose_persona's own real
                # ground-truth behavior (separately covered by
                # DiagnoseLegRealBindingSmokeTest + DiagnoseVerdictTest) --
                # also sidesteps the same real-executable permission wall.
                persona_bringup, "diagnose_persona", return_value=fixture_report,
            ), _provenance_patch():
                evidence = module.prove(ctx)

            self.assertIn(evidence.status, ("proven", "gap"))
            legs = evidence.detail["legs"]
            self.assertEqual(
                set(legs), {"diagnose", "readiness_gate", "ensure_runtime_chain", "kick_lands_executes"}
            )
            self.assertEqual(legs["kick_lands_executes"]["deferred_to"], DEFERRED_TO_TICKET)

            restore_evidence = module.restore(ctx)
            self.assertTrue(restore_evidence.restored)

            row = module.report(evidence)
            self.assertIn(DEFERRED_TO_TICKET, row.summary)
            self.assertIn(COMPOSITE_ID, row.summary)


if __name__ == "__main__":
    unittest.main()
