"""Tests for tools.bringup_acceptance.inventory (T-2559 phase 2).

Every test is fixture-based: raw systemctl/settings.json/mailbox/telegram
observations are injected directly as already-fetched data (mirroring the
T-2382/T-2558 observation-injection convention) -- nothing here shells out to
a live systemctl or reads a real account's settings.json.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.bringup_acceptance.evidence import RunPaths, read_inventory
from tools.bringup_acceptance.inventory import (
    BindingFacts,
    TelegramObservation,
    UnitRecord,
    build_inventory,
    capture_parity_oracles,
    discover_harness_runtime,
    discover_hooks,
    discover_mailbox,
    discover_systemd,
    discover_telegram,
    is_templated_for_persona,
    parse_list_units_output,
    write_discovery_result,
)
from tools.bringup_acceptance.types import InventoryPathEntry, TargetInventory


def _binding(**overrides) -> BindingFacts:
    base = dict(
        persona="recovery",
        account="claude-fallback",
        harness="claude",
        profile="interactive-subscription",
        tmux_target="pantheon-recovery:harness.0",
        fallbacks=("codex/interactive-chatgpt",),
    )
    base.update(overrides)
    return BindingFacts(**base)


class ParseListUnitsOutputTest(unittest.TestCase):
    def test_parses_plain_no_legend_columns(self) -> None:
        text = (
            "pantheon-inbox-consumer@recovery.timer loaded active waiting Inbox consumer timer\n"
            "pantheon-apt-update.timer loaded active waiting Apt update timer\n"
        )
        records = parse_list_units_output(text)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].unit, "pantheon-inbox-consumer@recovery.timer")
        self.assertEqual(records[0].active, "active")
        self.assertEqual(records[0].sub, "waiting")
        self.assertIsNone(records[0].on_failure)

    def test_blank_lines_are_skipped(self) -> None:
        text = "\n\npantheon-keep-going@recovery.timer loaded active waiting Keep-going timer\n\n"
        records = parse_list_units_output(text)
        self.assertEqual(len(records), 1)

    def test_on_failure_can_be_supplied_per_record(self) -> None:
        records = parse_list_units_output(
            "pantheon-apt-update.timer loaded active waiting Apt update timer\n",
            on_failure={"pantheon-apt-update.timer": "pantheon-unit-failure-alert@%n.service"},
        )
        self.assertEqual(
            records[0].on_failure, "pantheon-unit-failure-alert@%n.service"
        )


class IsTemplatedForPersonaTest(unittest.TestCase):
    def test_matches_the_exact_persona_instance(self) -> None:
        self.assertTrue(is_templated_for_persona("pantheon-inbox-consumer@recovery.timer", "recovery"))
        self.assertTrue(is_templated_for_persona("pantheon-keep-going@recovery.service", "recovery"))

    def test_rejects_other_instances_and_non_templated_units(self) -> None:
        self.assertFalse(is_templated_for_persona("pantheon-inbox-consumer@specialist.timer", "recovery"))
        self.assertFalse(is_templated_for_persona("pantheon-apt-update.timer", "recovery"))


class DiscoverHarnessRuntimeTest(unittest.TestCase):
    def test_runtime_and_fallback_entries_when_fallbacks_configured(self) -> None:
        entries = discover_harness_runtime(_binding())
        by_module = {e.module_id: e for e in entries}
        self.assertEqual(by_module["harness-runtime"].status, "applies")
        self.assertEqual(by_module["harness-runtime"].tier, "core")
        self.assertEqual(by_module["harness-fallback-swap"].status, "applies")

    def test_fallback_entry_is_absent_with_no_fallbacks_configured(self) -> None:
        entries = discover_harness_runtime(_binding(fallbacks=()))
        by_module = {e.module_id: e for e in entries}
        self.assertEqual(by_module["harness-fallback-swap"].status, "absent")

    def test_no_sampling_every_configured_fallback_is_its_own_entry(self) -> None:
        entries = discover_harness_runtime(
            _binding(fallbacks=("codex/interactive-chatgpt", "claude/batch-sdk"))
        )
        fallback_entries = [e for e in entries if e.module_id == "harness-fallback-swap"]
        self.assertEqual(len(fallback_entries), 2)
        self.assertTrue(all(e.status == "applies" for e in fallback_entries))
        self.assertNotEqual(fallback_entries[0].path, fallback_entries[1].path)


class DiscoverSystemdTest(unittest.TestCase):
    def test_persona_templated_timer_units_are_in_scope(self) -> None:
        units = (
            UnitRecord(unit="pantheon-inbox-consumer@recovery.timer", active="active", sub="waiting"),
            UnitRecord(unit="pantheon-persona-consume@recovery.timer", active="active", sub="waiting"),
        )
        entries, out_of_scope = discover_systemd(units, persona="recovery")
        self.assertEqual(len(entries), 2)
        self.assertEqual(out_of_scope, ())
        self.assertTrue(all(e.tier == "core" for e in entries))
        self.assertTrue(all(e.module_id == "timer-cron" for e in entries))

    def test_no_sampling_multiple_timers_are_distinct_entries_under_one_module_id(self) -> None:
        units = tuple(
            UnitRecord(unit=f"pantheon-inbox-consumer@recovery.timer", active="active", sub="waiting")
            for _ in range(1)
        ) + (
            UnitRecord(unit="pantheon-persona-consume@recovery.timer", active="active", sub="waiting"),
            UnitRecord(unit="pantheon-context-fill-monitor@recovery.timer", active="active", sub="waiting"),
        )
        entries, _out_of_scope = discover_systemd(units, persona="recovery")
        self.assertEqual(len(entries), 3)
        self.assertEqual({e.module_id for e in entries}, {"timer-cron"})
        # Each one is its own row -- distinct paths, not collapsed.
        self.assertEqual(len({e.path for e in entries}), 3)

    def test_mailbox_wake_path_unit_maps_to_watch_producer_not_orphaned(self) -> None:
        units = (UnitRecord(unit="pantheon-mailbox-wake@recovery.path", active="active", sub="waiting"),)
        entries, out_of_scope = discover_systemd(units, persona="recovery")
        self.assertEqual(out_of_scope, ())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].module_id, "watch-producer")
        self.assertEqual(entries[0].tier, "core")

    def test_service_sibling_of_a_path_unit_is_also_watch_producer(self) -> None:
        units = (
            UnitRecord(unit="pantheon-mailbox-wake@recovery.path", active="active", sub="waiting"),
            UnitRecord(unit="pantheon-mailbox-wake@recovery.service", active="inactive", sub="dead"),
        )
        entries, _out_of_scope = discover_systemd(units, persona="recovery")
        self.assertEqual({e.module_id for e in entries}, {"watch-producer"})
        self.assertEqual(len(entries), 2)

    def test_keep_going_unit_gets_its_own_module_id_regardless_of_kind(self) -> None:
        units = (
            UnitRecord(unit="pantheon-keep-going@recovery.timer", active="active", sub="waiting"),
            UnitRecord(unit="pantheon-keep-going@recovery.service", active="inactive", sub="dead"),
        )
        entries, out_of_scope = discover_systemd(units, persona="recovery")
        self.assertTrue(all(e.module_id == "keep-going-kicker" for e in entries))
        self.assertEqual(len(entries), 2)
        self.assertEqual(out_of_scope, ())

    def test_host_hygiene_unit_is_excluded_and_named_not_a_gap(self) -> None:
        units = (UnitRecord(unit="pantheon-apt-update.timer", active="active", sub="waiting"),)
        entries, out_of_scope = discover_systemd(units, persona="recovery")
        self.assertEqual(entries, ())
        self.assertEqual(len(out_of_scope), 1)
        self.assertEqual(out_of_scope[0].unit, "pantheon-apt-update.timer")
        self.assertIn("host-hygiene", out_of_scope[0].reason)

    def test_host_hygiene_unit_with_alert_into_mailbox_is_named_and_captures_the_alert_leg(
        self,
    ) -> None:
        units = (
            UnitRecord(
                unit="pantheon-worktree-sweeper.timer",
                active="active",
                sub="waiting",
                on_failure="pantheon-unit-failure-alert@%n.service",
            ),
        )
        entries, out_of_scope = discover_systemd(units, persona="recovery")
        # The host unit itself is still named out-of-scope (not a persona-instance).
        self.assertEqual(len(out_of_scope), 1)
        self.assertEqual(out_of_scope[0].unit, "pantheon-worktree-sweeper.timer")
        # But its alert-into-mailbox leg IS captured as an in-scope entry.
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].module_id, "unit-failure-alert-into-mailbox")
        self.assertEqual(entries[0].tier, "optional")
        self.assertEqual(entries[0].status, "applies")

    def test_other_persona_instance_is_out_of_scope(self) -> None:
        units = (UnitRecord(unit="pantheon-inbox-consumer@specialist.timer", active="active", sub="waiting"),)
        entries, out_of_scope = discover_systemd(units, persona="recovery")
        self.assertEqual(entries, ())
        self.assertEqual(len(out_of_scope), 1)


class DiscoverHooksTest(unittest.TestCase):
    def test_no_settings_json_is_absent_for_every_event(self) -> None:
        entries = discover_hooks(None, "claude-fallback")
        self.assertEqual(len(entries), 5)
        self.assertTrue(all(e.status == "absent" for e in entries))
        self.assertTrue(all(e.tier == "optional" for e in entries))

    def test_registered_session_start_hook_applies(self) -> None:
        settings = """
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command",
          "command": "~/.claude/hooks/ensure-hooks-active.sh"}]}]}}
        """
        entries = discover_hooks(settings, "claude")
        by_module = {e.module_id: e for e in entries}
        self.assertEqual(by_module["hook-session-start"].status, "applies")
        self.assertEqual(by_module["hook-pre-tool-use"].status, "absent")

    def test_bare_string_and_top_level_command_shapes_are_recognized(self) -> None:
        settings = """
        {"hooks": {"Stop": ["some-bare-command.sh"],
                    "StopFailure": [{"command": "notify.sh"}]}}
        """
        entries = discover_hooks(settings, "claude")
        by_module = {e.module_id: e for e in entries}
        self.assertEqual(by_module["hook-stop"].status, "applies")
        self.assertEqual(by_module["hook-stop-failure"].status, "applies")

    def test_unparseable_settings_folds_to_absent_not_a_crash(self) -> None:
        entries = discover_hooks("not json", "claude")
        self.assertTrue(all(e.status == "absent" for e in entries))

    def test_no_sampling_multiple_commands_under_one_event_are_distinct_entries(self) -> None:
        settings = """
        {"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "~/.claude/hooks/ensure-hooks-active.sh"}]},
            {"hooks": [{"type": "command", "command": "~/.claude/hooks/handoff-auto-read.sh"}]}
        ]}}
        """
        entries = discover_hooks(settings, "recovery")
        session_start = [e for e in entries if e.module_id == "hook-session-start"]
        self.assertEqual(len(session_start), 2)
        self.assertTrue(all(e.status == "applies" for e in session_start))
        self.assertNotEqual(session_start[0].path, session_start[1].path)


class DiscoverMailboxTest(unittest.TestCase):
    def test_present_and_absent(self) -> None:
        present = discover_mailbox("recovery", Path("/home/the-ancestor/mailbox"), True)
        absent = discover_mailbox("recovery", Path("/home/the-ancestor/mailbox"), False)
        self.assertEqual(present.status, "applies")
        self.assertEqual(present.module_id, "mailbox-roundtrip")
        self.assertEqual(present.tier, "core")
        self.assertEqual(absent.status, "absent")


class DiscoverTelegramTest(unittest.TestCase):
    def test_bridge_present_yields_inbound_and_outbound_entries(self) -> None:
        entries, finding = discover_telegram(
            "claude", TelegramObservation(bridge_env_present=True, dedicated_test_chat=None)
        )
        module_ids = {e.module_id for e in entries}
        self.assertEqual(module_ids, {"telegram-inbound", "telegram-outbound"})
        self.assertTrue(all(e.status == "applies" for e in entries))
        self.assertIsNone(finding.dedicated_test_chat_configured)
        self.assertIsNone(finding.dedicated_test_chat_id)

    def test_bridge_absent_yields_absent_entries(self) -> None:
        entries, _finding = discover_telegram(
            "claude", TelegramObservation(bridge_env_present=False, dedicated_test_chat=False)
        )
        self.assertTrue(all(e.status == "absent" for e in entries))

    def test_q3_finding_carries_the_caller_supplied_answer_verbatim(self) -> None:
        _entries, finding_true = discover_telegram(
            "claude", TelegramObservation(bridge_env_present=True, dedicated_test_chat=True)
        )
        _entries, finding_false = discover_telegram(
            "claude", TelegramObservation(bridge_env_present=True, dedicated_test_chat=False)
        )
        _entries, finding_unknown = discover_telegram(
            "claude", TelegramObservation(bridge_env_present=True, dedicated_test_chat=None)
        )
        self.assertIs(finding_true.dedicated_test_chat_configured, True)
        self.assertIs(finding_false.dedicated_test_chat_configured, False)
        self.assertIsNone(finding_unknown.dedicated_test_chat_configured)
        # Never guessed: the unknown case must say so, not silently read as False.
        self.assertIn("unknown", finding_unknown.note.lower())

    def test_dedicated_test_chat_id_carries_verbatim_and_stays_none_until_confirmed(self) -> None:
        _entries, finding_with_id = discover_telegram(
            "claude",
            TelegramObservation(
                bridge_env_present=True, dedicated_test_chat=True, dedicated_test_chat_id="-5356823915"
            ),
        )
        _entries, finding_no_id = discover_telegram(
            "claude", TelegramObservation(bridge_env_present=True, dedicated_test_chat=None)
        )
        self.assertEqual(finding_with_id.dedicated_test_chat_id, "-5356823915")
        self.assertIsNone(finding_no_id.dedicated_test_chat_id)


class CaptureParityOraclesTest(unittest.TestCase):
    def test_every_catalogued_module_id_has_a_sourced_oracle(self) -> None:
        module_ids = [
            "harness-runtime",
            "harness-fallback-swap",
            "timer-cron",
            "watch-producer",
            "keep-going-kicker",
            "unit-failure-alert-into-mailbox",
            "mailbox-roundtrip",
            "telegram-inbound",
            "telegram-outbound",
            "hook-session-start",
            "hook-pre-tool-use",
            "hook-stop",
            "hook-stop-failure",
            "hook-subagent-stop",
        ]
        oracles = capture_parity_oracles(module_ids)
        self.assertEqual(set(oracles), set(module_ids))
        for module_id, oracle in oracles.items():
            self.assertTrue(oracle.source, f"{module_id} oracle has no source pointer")
            self.assertTrue(oracle.trigger)
            self.assertNotEqual(oracle.source, "none -- module_id has no PARITY_ORACLES entry yet")

    def test_catalog_is_open_an_uncatalogued_id_does_not_raise(self) -> None:
        # orch attaches new ids over time (forced-cap-return, telegram-failure-
        # modes, <core-id>-live suffixes, ...) -- discovery must tolerate one
        # that has no curated oracle yet rather than crashing.
        oracles = capture_parity_oracles(["forced-cap-return", "mailbox-roundtrip"])
        self.assertEqual(set(oracles), {"forced-cap-return", "mailbox-roundtrip"})
        self.assertEqual(oracles["forced-cap-return"].trigger, "not yet catalogued")
        self.assertNotEqual(oracles["mailbox-roundtrip"].trigger, "not yet catalogued")


class BuildInventoryTest(unittest.TestCase):
    def test_combines_every_source_into_one_target_inventory(self) -> None:
        result = build_inventory(
            binding=_binding(),
            units=(
                UnitRecord(unit="pantheon-inbox-consumer@recovery.timer", active="active", sub="waiting"),
                UnitRecord(unit="pantheon-apt-update.timer", active="active", sub="waiting"),
            ),
            settings_json_text=None,
            mailbox_hub=Path("/home/the-ancestor/mailbox"),
            mailbox_present=True,
            telegram=TelegramObservation(bridge_env_present=True, dedicated_test_chat=None),
        )
        self.assertIsInstance(result.inventory, TargetInventory)
        self.assertEqual(result.inventory.persona, "recovery")
        for entry in result.inventory.entries:
            self.assertIsInstance(entry, InventoryPathEntry)
        module_ids = {e.module_id for e in result.inventory.entries}
        self.assertIn("harness-runtime", module_ids)
        self.assertIn("timer-cron", module_ids)
        self.assertIn("mailbox-roundtrip", module_ids)
        self.assertIn("telegram-inbound", module_ids)
        self.assertIn("hook-session-start", module_ids)
        # apt-update was excluded from entries and named out-of-scope instead.
        self.assertNotIn("pantheon-apt-update.timer", [str(e.path) for e in result.inventory.entries])
        self.assertEqual(len(result.out_of_scope), 1)
        self.assertEqual(result.out_of_scope[0].unit, "pantheon-apt-update.timer")
        # Every produced module_id has a captured oracle.
        self.assertEqual(set(result.parity_oracles), module_ids)


class WriteDiscoveryResultTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.paths = RunPaths(root=self.root, run_id="run-1")

    def test_inventory_json_conforms_to_the_landed_schema(self) -> None:
        result = build_inventory(
            binding=_binding(),
            units=(),
            settings_json_text=None,
            mailbox_hub=Path("/home/the-ancestor/mailbox"),
            mailbox_present=True,
            telegram=TelegramObservation(bridge_env_present=False, dedicated_test_chat=None),
        )
        write_discovery_result(self.paths, result)
        observed = read_inventory(self.paths)
        self.assertEqual(observed["schema"], "pantheon-bringup-acceptance-inventory/v1")
        self.assertEqual(observed["persona"], "recovery")
        for entry in observed["entries"]:
            self.assertEqual(set(entry), {"path", "tier", "status", "module_id"})

    def test_discovery_extras_land_in_the_sibling_file(self) -> None:
        result = build_inventory(
            binding=_binding(),
            units=(
                UnitRecord(
                    unit="pantheon-worktree-sweeper.timer",
                    active="active",
                    sub="waiting",
                    on_failure="pantheon-unit-failure-alert@%n.service",
                ),
            ),
            settings_json_text=None,
            mailbox_hub=Path("/home/the-ancestor/mailbox"),
            mailbox_present=True,
            telegram=TelegramObservation(
                bridge_env_present=True, dedicated_test_chat=True, dedicated_test_chat_id="-5356823915"
            ),
        )
        write_discovery_result(self.paths, result)
        sibling = self.paths.run_dir / "inventory_discovery.json"
        self.assertTrue(sibling.is_file())
        import json

        payload = json.loads(sibling.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["out_of_scope"]), 1)
        self.assertIn("mailbox-roundtrip", payload["parity_oracles"])
        self.assertIs(payload["telegram_finding"]["dedicated_test_chat_configured"], True)
        self.assertEqual(payload["telegram_finding"]["dedicated_test_chat_id"], "-5356823915")


if __name__ == "__main__":
    unittest.main()
