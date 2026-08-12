"""Tests for tools.bringup_acceptance.composites + .runner (T-2556 phase 15 / T-2577).

The e2e-runner capstone: wires every landed catalog module + a persona's
discovered optional plug-ins into one run, resolves the mailbox-wake >=1-of
composite constituent, and emits the closure matrix. See runner.py's module
docstring for the wiring contract and composites.py's for the composite-map
provenance (ledger-pinned).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from tools import runtime_controller as rc
from tools.bringup_acceptance import composites, hook_emissions, harness_runtime, registry
from tools.bringup_acceptance.composites import (
    CORE_COMPOSITES,
    WAKE_TRIGGER_MODULE_ID,
    resolve_wake_trigger_row,
)
from tools.bringup_acceptance.evidence import RunPaths
from tools.bringup_acceptance.forced_cap_return import ForcedCapReturnModule
from tools.bringup_acceptance.forced_cap_return import COMPOSITE_ID as FORCED_CAP_RETURN_COMPOSITE_ID
from tools.bringup_acceptance.forced_cap_return import MODULE_ID as FORCED_CAP_RETURN_MODULE_ID
from tools.bringup_acceptance.harness_fallback_swap import HarnessFallbackSwapModule
from tools.bringup_acceptance.harness_fallback_swap import COMPOSITE_ID as SWAP_COMPOSITE_ID
from tools.bringup_acceptance.harness_fallback_swap import MODULE_ID as SWAP_MODULE_ID
from tools.bringup_acceptance.harness_runtime import HarnessRuntimeModule
from tools.bringup_acceptance.harness_runtime import COMPOSITE_ID as HARNESS_RUNTIME_COMPOSITE_ID
from tools.bringup_acceptance.harness_runtime import MODULE_ID as HARNESS_RUNTIME_MODULE_ID
from tools.bringup_acceptance.mailbox_roundtrip import MailboxRoundtripModule
from tools.bringup_acceptance.mailbox_roundtrip import MODULE_ID as MAILBOX_MODULE_ID
from tools.bringup_acceptance.urgent_mail_preempt import COMPOSITE_ID as URGENT_MAIL_PREEMPT_COMPOSITE_ID
from tools.bringup_acceptance.urgent_mail_preempt import MODULE_ID as URGENT_MAIL_PREEMPT_MODULE_ID
from tools.bringup_acceptance.producer_fires import (
    TIMER_CRON_ID,
    WATCH_PRODUCER_ID,
    FireHarness,
    FireOutcome,
    TimerCronModule,
    UnitFailureAlertModule,
    UnitPlan,
    WatchProducerModule,
)
from tools.bringup_acceptance.registry import discover_optional_modules
from tools.bringup_acceptance.runner import run_end_to_end, run_module_lifecycle
from tools.bringup_acceptance.telegram_outbound import MODULE_ID as TELEGRAM_OUTBOUND_MODULE_ID
from tools.bringup_acceptance.telegram_outbound import SendRequest, SendResult, TelegramOutboundModule
from tools.bringup_acceptance.types import (
    AcceptanceModule,
    HardStop,
    InventoryPathEntry,
    MatrixRow,
    ModuleDeclaration,
    ModuleEvidence,
    RestoreEvidence,
    RunContext,
    TargetInventory,
    Tier,
)
from tools.harness_adapter.types import DiagnosticObservation, DiagnosticReport


PERSONA = "bringup-e2e-fresh"
ACCOUNT = "bringup-e2e-account"


# ---------------------------------------------------------------------------
# Shared fixtures -- one disposable target covering every catalog module_id,
# plus each module's own established fixture-construction recipe (fakes/
# mocks), lifted verbatim from that module's own test suite.
# ---------------------------------------------------------------------------


def _entry(module_id: str, path: str, *, tier: str = "core", status: str = "applies") -> InventoryPathEntry:
    return InventoryPathEntry(path=Path(path), tier=tier, status=status, module_id=module_id)


def _full_catalog_target() -> TargetInventory:
    """Every catalog module_id represented -- mailbox-wake.timer installed
    (current T-1015 reality), .path absent (sibling-satisfied, not a gap).
    Hook events and the alert leg are left with NO entries at all, so their
    modules self-declare absent (optional-tier) without any subprocess/hook
    invocation in this smoke."""
    entries = [
        _entry(HARNESS_RUNTIME_MODULE_ID, f"pantheon-{PERSONA}:harness.0"),
        _entry(SWAP_MODULE_ID, f"fallback:{PERSONA}:codex/interactive-chatgpt"),
        _entry(MAILBOX_MODULE_ID, f"/hub/{PERSONA}"),
        _entry("telegram-inbound", f"/home/{ACCOUNT}/.claude/channels/telegram/.env"),
        _entry(TELEGRAM_OUTBOUND_MODULE_ID, f"/home/{ACCOUNT}/.claude/channels/telegram/.env"),
        _entry(TIMER_CRON_ID, f"pantheon-persona-consume@{PERSONA}.timer"),
        _entry(TIMER_CRON_ID, f"pantheon-context-fill-monitor@{PERSONA}.timer"),
        _entry(TIMER_CRON_ID, f"pantheon-activity-monitor@{PERSONA}.timer"),
        _entry(TIMER_CRON_ID, f"pantheon-inbox-consumer@{PERSONA}.timer"),
        _entry(TIMER_CRON_ID, f"pantheon-mailbox-wake@{PERSONA}.timer"),
    ]
    return TargetInventory(persona=PERSONA, entries=tuple(entries))


def _run_context(root: Path, *, run_id: str = "20260805T150000Z") -> RunContext:
    return RunContext(
        run_id=run_id,
        persona=PERSONA,
        account=ACCOUNT,
        harness="claude",
        profile="interactive-subscription",
        hub_path=root / "hub",
        state_path=root / "state",
        evidence_root=root / "evidence",
        baseline_ref="deadbeef" * 5,
    )


def _make_fake_fire_harness() -> FireHarness:
    """In-memory harness: install() always writes the nonce marker, teardown()
    always removes it -- every catalog producer nonce-fires. Mirrors
    test_bringup_acceptance_producer_fires.py's own `_make_fake_harness`
    shape (tracked-acceptable duplication -- see this ticket's DECISIONS)."""

    def _install(plan: UnitPlan) -> None:
        plan.marker_path.parent.mkdir(parents=True, exist_ok=True)
        plan.marker_path.write_text(plan.nonce)

    def _await_fire(plan: UnitPlan) -> FireOutcome:
        if plan.marker_path.is_file() and plan.marker_path.read_text() == plan.nonce:
            return FireOutcome(fired=True, detail={"observed_marker": True})
        return FireOutcome(fired=False, detail={"observed_marker": False})

    def _teardown(plan: UnitPlan) -> None:
        if plan.marker_path.exists():
            plan.marker_path.unlink()

    return FireHarness(install=_install, await_fire=_await_fire, teardown=_teardown)


def _ok_send(*, chat_id: str) -> Any:
    def _send(request: SendRequest) -> SendResult:
        return SendResult(
            ok=True, message_id="e2e-1001", chat_id=chat_id,
            reply_to_message_id=request.reply_to_message_id, error_kind=None, raw={"ok": True},
        )

    return _send


def _diagnose_report() -> DiagnosticReport:
    return DiagnosticReport(
        harness="claude", execution_context="test:e2e",
        observations=(
            DiagnosticObservation(
                check_id="claude.executable", status="pass", summary="fixture", authoritative=True, evidence={},
            ),
            DiagnosticObservation(
                check_id="claude.auth_material", status="unavailable", summary="fixture",
                authoritative=False, evidence={},
            ),
        ),
    )


def _fixture_source_provenance(**kwargs: object) -> rc.RuntimeSourceProvenance:
    """Shared cousin of the THREE existing per-module test helpers of the same
    name (harness_runtime/harness_fallback_swap/forced_cap_return) -- all
    patch the identical `rc._controller_source_provenance` target, so one
    fixture + one patch context covers all three modules in this run."""
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
        rc, "_controller_source_provenance", side_effect=lambda **kwargs: _fixture_source_provenance(**kwargs),
    )


def _build_catalog_modules(*, fire_harness: FireHarness, root: Path) -> list[AcceptanceModule]:
    return [
        *hook_emissions.build_hook_emissions_modules(),
        MailboxRoundtripModule(),
        TimerCronModule(harness=fire_harness),
        WatchProducerModule(harness=fire_harness),
        UnitFailureAlertModule(
            harness=fire_harness, state_path=root / "alert-state.json", backup_root=root / "alert-backups",
        ),
        HarnessRuntimeModule(),
        HarnessFallbackSwapModule(),
        ForcedCapReturnModule(),
        TelegramOutboundModule(
            send=_ok_send(chat_id="e2e-disposable-chat"), chat_id="e2e-disposable-chat",
            bot_pid_path=root / "no-such-bot.pid",
        ),
    ]


# --------------------------------------------------------- composites module


class CoreCompositesMapTest(unittest.TestCase):
    def test_six_ratified_composites_present(self) -> None:
        self.assertEqual(
            set(CORE_COMPOSITES),
            {
                HARNESS_RUNTIME_COMPOSITE_ID, SWAP_COMPOSITE_ID, FORCED_CAP_RETURN_COMPOSITE_ID,
                URGENT_MAIL_PREEMPT_COMPOSITE_ID,
                "mailbox-fully-functional", "telegram-fully-functional",
            },
        )

    def test_self_pair_composites_require_the_landed_module_plus_its_live_sibling(self) -> None:
        self.assertEqual(
            CORE_COMPOSITES[HARNESS_RUNTIME_COMPOSITE_ID], (HARNESS_RUNTIME_MODULE_ID, "harness-runtime-live"),
        )
        self.assertEqual(
            CORE_COMPOSITES[SWAP_COMPOSITE_ID], (SWAP_MODULE_ID, "harness-fallback-swap-live"),
        )
        self.assertEqual(
            CORE_COMPOSITES[FORCED_CAP_RETURN_COMPOSITE_ID],
            (FORCED_CAP_RETURN_MODULE_ID, "forced-cap-return-live"),
        )
        self.assertEqual(
            CORE_COMPOSITES[URGENT_MAIL_PREEMPT_COMPOSITE_ID],
            (URGENT_MAIL_PREEMPT_MODULE_ID, "urgent-mail-preempt-live"),
        )

    def test_mailbox_fully_functional_requires_substrate_plus_live_plus_wake_trigger(self) -> None:
        self.assertEqual(
            CORE_COMPOSITES["mailbox-fully-functional"],
            (MAILBOX_MODULE_ID, "harness-runtime-live", WAKE_TRIGGER_MODULE_ID),
        )

    def test_telegram_fully_functional_requires_all_four_legs(self) -> None:
        self.assertEqual(
            CORE_COMPOSITES["telegram-fully-functional"],
            ("telegram-inbound", TELEGRAM_OUTBOUND_MODULE_ID, "telegram-user-visible", "telegram-failure-modes"),
        )


class ResolveWakeTriggerRowTest(unittest.TestCase):
    def _detail(self, *, installed: bool, fired: bool | None) -> Mapping[str, Any]:
        return {"producers": {"mailbox-wake-timer": {"wake_trigger": {"installed": installed, "fired": fired}}}}

    def _empty_detail(self) -> Mapping[str, Any]:
        return {"producers": {"mailbox-wake-path": {"wake_trigger": {"installed": False, "fired": None}}}}

    def test_installed_and_fired_is_a_proven_core_row(self) -> None:
        row = resolve_wake_trigger_row(
            timer_detail=self._detail(installed=True, fired=True), watch_detail=self._empty_detail(),
        )
        self.assertEqual(row.module_id, WAKE_TRIGGER_MODULE_ID)
        self.assertEqual(row.tier, "core")
        self.assertEqual(row.status, "proven")

    def test_neither_installed_is_a_gap_row_naming_the_verdict(self) -> None:
        row = resolve_wake_trigger_row(
            timer_detail=self._empty_detail(),
            watch_detail={"producers": {"mailbox-wake-path": {"wake_trigger": {"installed": False, "fired": None}}}},
        )
        self.assertEqual(row.status, "gap")
        self.assertIn("neither", row.summary)


# -------------------------------------------------------------- run_module_lifecycle


class _StubModule:
    """Minimal AcceptanceModule stand-in for run_module_lifecycle's own unit tests."""

    def __init__(self, *, module_id: str, tier: Tier, applies: bool) -> None:
        self.id = module_id
        self.tier = tier
        self._applies = applies
        self.prove_called = False
        self.restore_called = False

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        return ModuleDeclaration(
            module_id=self.id, tier=self.tier, applies=self._applies, reason="stub reason",
        )

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        self.prove_called = True
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=Path("/dev/null"), evidence_paths=(), detail={},
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        self.restore_called = True
        return RestoreEvidence(module_id=self.id, restored=True, detail={})

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status, summary="stub", evidence_refs=(),
        )


class RunModuleLifecycleTest(unittest.TestCase):
    def test_non_applying_core_module_is_a_gap_not_declared_absent(self) -> None:
        module = _StubModule(module_id="stub-core", tier="core", applies=False)
        row, evidence = run_module_lifecycle(module, target=TargetInventory(persona=PERSONA, entries=()), ctx=None)
        self.assertEqual(row.status, "gap")
        self.assertIsNone(evidence)
        self.assertFalse(module.prove_called)

    def test_non_applying_optional_module_is_declared_absent(self) -> None:
        module = _StubModule(module_id="stub-optional", tier="optional", applies=False)
        row, evidence = run_module_lifecycle(module, target=TargetInventory(persona=PERSONA, entries=()), ctx=None)
        self.assertEqual(row.status, "declared_absent")
        self.assertIsNone(evidence)

    def test_applying_module_runs_the_full_lifecycle(self) -> None:
        module = _StubModule(module_id="stub-core", tier="core", applies=True)
        row, evidence = run_module_lifecycle(module, target=TargetInventory(persona=PERSONA, entries=()), ctx=None)
        self.assertEqual(row.status, "proven")
        self.assertIsNotNone(evidence)
        self.assertTrue(module.prove_called)
        self.assertTrue(module.restore_called)

    def test_restore_runs_even_if_prove_raises(self) -> None:
        module = _StubModule(module_id="stub-core", tier="core", applies=True)

        def _raising_prove(ctx: RunContext) -> ModuleEvidence:
            module.prove_called = True
            raise HardStop("synthetic prove failure")

        module.prove = _raising_prove  # type: ignore[assignment]
        with self.assertRaises(HardStop):
            run_module_lifecycle(module, target=TargetInventory(persona=PERSONA, entries=()), ctx=None)
        self.assertTrue(module.restore_called)


# --------------------------------------------------------------------- run_end_to_end


class OptionalPluginWiringTest(unittest.TestCase):
    """Isolates the runner's OWN pass-through of registry.run_persona_optionals
    from any real persona's (e.g. Recovery's) environment-coupled modules."""

    def test_a_discovered_optional_module_lands_as_an_optional_row(self) -> None:
        fake_optional = _StubModule(module_id="fake-optional", tier="optional", applies=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            target = TargetInventory(persona=PERSONA, entries=())
            with mock.patch.object(registry, "discover_optional_modules", return_value=(fake_optional,)):
                closure_result = run_end_to_end(ctx, target, catalog_modules=())
        optional_rows = [row for row in closure_result.rows if row.module_id == "fake-optional"]
        self.assertEqual(len(optional_rows), 1)
        self.assertEqual(optional_rows[0].tier, "optional")
        self.assertEqual(optional_rows[0].status, "proven")


class EndToEndCoreGreenTest(unittest.TestCase):
    def test_core_rows_all_proven_composites_red_pending_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _run_context(root)
            target = _full_catalog_target()
            fire_harness = _make_fake_fire_harness()
            catalog_modules = _build_catalog_modules(fire_harness=fire_harness, root=root)

            # harness_runtime.prove()'s diagnose leg resolves a real
            # persona_bringup.load_binding() against the REAL default
            # manifest before ever reaching diagnose_persona -- PERSONA here
            # is a fresh/unbound fixture name (deliberately not a real bound
            # persona, for hermeticity), so `_diagnose_leg` itself is
            # replaced wholesale (NOT persona_bringup.load_binding globally
            # -- harness_fallback_swap/forced_cap_return call THAT with
            # their own self-contained real fixture manifests and need the
            # real function).
            with mock.patch.object(
                harness_runtime, "_diagnose_leg", return_value=harness_runtime._diagnose_verdict(_diagnose_report()),
            ), mock.patch.object(
                harness_runtime, "_resolve_mailbox_hub", return_value=root / "hub",
            ), _provenance_patch():
                closure_result = run_end_to_end(ctx, target, catalog_modules=catalog_modules)

            # -- every CORE-tier row is proven --
            core_rows = {row.module_id: row for row in closure_result.rows if row.tier == "core"}
            expected_core_ids = {
                HARNESS_RUNTIME_MODULE_ID, SWAP_MODULE_ID, FORCED_CAP_RETURN_MODULE_ID, MAILBOX_MODULE_ID,
                TELEGRAM_OUTBOUND_MODULE_ID, TIMER_CRON_ID, WATCH_PRODUCER_ID, WAKE_TRIGGER_MODULE_ID,
            }
            self.assertEqual(set(core_rows), expected_core_ids)
            for module_id, row in core_rows.items():
                self.assertEqual(row.status, "proven", f"{module_id}: expected proven, got {row.status}")

            # -- optional-tier rows with nothing wired: declared_absent, no execution --
            optional_rows = {row.module_id: row for row in closure_result.rows if row.tier == "optional"}
            for module_id in (
                "hook-session-start", "hook-pre-tool-use", "hook-stop", "hook-stop-failure",
                "hook-subagent-stop", "unit-failure-alert-into-mailbox",
            ):
                self.assertIn(module_id, optional_rows)
                self.assertEqual(optional_rows[module_id].status, "declared_absent")

            # -- every gap is a composite-missing-constituent gap, never a per-module one --
            for gap in closure_result.gaps:
                self.assertIn("composite capability missing constituent", gap)

            # -- all 5 composites present and RED, naming only not-yet-landed legs --
            composite_by_id = {row.capability_id: row for row in closure_result.composite_rows}
            self.assertEqual(set(composite_by_id), set(CORE_COMPOSITES))
            for capability_id, row in composite_by_id.items():
                self.assertEqual(row.status, "RED", f"{capability_id}: expected RED-pending-live")
            self.assertEqual(
                composite_by_id[HARNESS_RUNTIME_COMPOSITE_ID].missing_module_ids, ("harness-runtime-live",),
            )
            self.assertEqual(composite_by_id[SWAP_COMPOSITE_ID].missing_module_ids, ("harness-fallback-swap-live",))
            self.assertEqual(
                composite_by_id[FORCED_CAP_RETURN_COMPOSITE_ID].missing_module_ids, ("forced-cap-return-live",),
            )
            self.assertEqual(
                composite_by_id["mailbox-fully-functional"].missing_module_ids, ("harness-runtime-live",),
            )
            self.assertEqual(
                composite_by_id["telegram-fully-functional"].missing_module_ids,
                ("telegram-inbound", "telegram-user-visible", "telegram-failure-modes"),
            )

            # -- aggregate status is honestly RED (composites gate it, not a failure) --
            self.assertEqual(closure_result.status, "RED")

            # -- durably persisted per the fixed T-2558 evidence-tree layout --
            paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
            self.assertTrue(paths.closure_json.is_file())
            self.assertTrue(paths.closure_md.is_file())
            self.assertTrue(paths.inventory_json.is_file())
            self.assertTrue(paths.run_json.is_file())


if __name__ == "__main__":
    unittest.main()
