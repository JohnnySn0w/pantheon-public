"""Codex-side Telegram parity invariant for comms seats (T-5450, PR-4 of T-5446).

Over the REAL instance.yaml:
  (a) every persona under persona_installer.codex_telegram_bridge.personas has a
      ``codex/...`` entry in harness_runtime.roles.<role>.fallbacks;
  (b) it has a codex standing-instructions entry (resolved by the installer's own
      ``standing_instruction_entries``), except the named KNOWN_GAPS_B;
  (c) reverse: a role with a codex fallback AND a Claude telegram channel has a
      bridge entry (else a swap leaves the seat comms-dark).

Archivist is data-only. Her codex fallback awaits an operator data-boundary decision
and is deliberately NOT in instance.yaml. Adding her later needs exactly these keys
and no code change (proved synthetically below):
  * persona_installer.codex_telegram_bridge.personas.archivist
  * harness_runtime.roles.head-memory-orchestrator.fallbacks: [codex/<profile>]
  * persona_installer.codex_standing_instructions.personas.archivist
    (or persona_installer.harness_standing_instructions.personas.archivist.codex)
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from tools import codex_telegram_bridge as bridge
from tools.persona_installer import (
    Manifest,
    _codex_bridge_required,
    _role_runtime_references,
    standing_instruction_entries,
)

MANIFEST = Path(__file__).resolve().parent.parent / "instance.yaml"

# persona -> reason. Their codex standing source is a self-authored pinned-rules
# file (hand-curated, self-owned); not ours to write. Remove when compliant.
KNOWN_GAPS_B: dict[str, str] = {}


def _bridge_personas(manifest: Manifest) -> set[str]:
    _src, personas = bridge.parse_bridge_block(manifest.installer["codex_telegram_bridge"])
    return set(personas)


def _has_codex_fallback(manifest: Manifest, persona: str) -> bool:
    refs = _role_runtime_references(manifest, manifest.binding(persona))
    return any(r.startswith("codex/") for r in refs)


def _has_codex_standing(manifest: Manifest, persona: str) -> bool:
    return bool(standing_instruction_entries(manifest.installer, persona).get("codex"))


def _claude_telegram(manifest: Manifest, role: str) -> bool:
    role_cfg = manifest.harness_runtime["roles"].get(role) or {}
    channels = ((role_cfg.get("claude") or {}).get("native") or {}).get("channels")
    return isinstance(channels, str) and "telegram" in channels


class CodexCommsParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = Manifest.load(MANIFEST)
        cls.bridge = _bridge_personas(cls.m)

    def test_bridge_personas_present(self):
        self.assertTrue({"orchestrator", "specialist", "recovery"} <= self.bridge)

    def test_a_bridge_persona_has_codex_fallback(self):
        for p in sorted(self.bridge):
            with self.subTest(persona=p):
                self.assertTrue(_has_codex_fallback(self.m, p), f"{p}: no codex/ fallback")

    def test_b_bridge_persona_has_codex_standing_or_named_gap(self):
        for p in sorted(self.bridge):
            with self.subTest(persona=p):
                ok = _has_codex_standing(self.m, p)
                if p in KNOWN_GAPS_B:
                    self.assertFalse(ok, f"{p} is now compliant: remove it from KNOWN_GAPS_B")
                else:
                    self.assertTrue(ok, f"{p}: no codex standing instructions (unnamed gap)")

    def test_known_gaps_only_name_bridge_personas(self):
        self.assertLessEqual(set(KNOWN_GAPS_B), self.bridge)

    def test_c_codex_fallback_plus_claude_telegram_requires_bridge(self):
        checked = 0
        for role, raw in self.m.roles.items():
            persona = raw.get("persona") if isinstance(raw, dict) else None
            if not persona:
                continue
            if _has_codex_fallback(self.m, persona) and _claude_telegram(self.m, role):
                checked += 1
                with self.subTest(persona=persona):
                    self.assertIn(persona, self.bridge, f"{persona}: comms-dark on a codex swap")
        self.assertGreaterEqual(checked, 3)


class ArchivistIsDataOnly(unittest.TestCase):
    def _with_archivist(self, tmp: Path) -> Path:
        raw = copy.deepcopy(yaml.safe_load(MANIFEST.read_text()))
        pi = raw["persona_installer"]
        orchestrator = pi["codex_telegram_bridge"]["personas"]["orchestrator"]
        pi["codex_telegram_bridge"]["personas"]["archivist"] = {**orchestrator, "account": "exocortex"}
        raw["harness_runtime"]["roles"]["head-memory-orchestrator"].setdefault(
            "fallbacks", []
        ).append("codex/interactive-chatgpt")
        pi["codex_standing_instructions"]["personas"]["archivist"] = copy.deepcopy(
            pi["codex_standing_instructions"]["personas"]["orchestrator"]
        )
        path = tmp / "instance.yaml"
        path.write_text(yaml.safe_dump(raw))
        return path

    def test_data_only_addition_satisfies_every_check(self):
        with tempfile.TemporaryDirectory() as t:
            m = Manifest.load(self._with_archivist(Path(t)))
            self.assertIn("archivist", _bridge_personas(m))
            self.assertTrue(_has_codex_fallback(m, "archivist"))
            self.assertTrue(_has_codex_standing(m, "archivist"))
            self.assertTrue(
                _codex_bridge_required(m, m.binding("archivist"), selected_harness="claude")
            )

    def test_real_manifest_has_no_archivist_codex_entry(self):
        m = Manifest.load(MANIFEST)
        self.assertNotIn("archivist", _bridge_personas(m))
        self.assertFalse(_has_codex_fallback(m, "archivist"))

    def test_command_sheet_routes_archivist_to_generic_path(self):
        # command_sheet special-cases {"specialist","recovery"} to _legacy_command_sheet;
        # archivist must take the generic path (same as orchestrator). Reported, not fixed.
        calls = []
        orig = bridge._legacy_command_sheet
        bridge._legacy_command_sheet = lambda p, **k: calls.append(p) or {"legacy": True}
        try:
            with tempfile.TemporaryDirectory() as t:
                m_path = self._with_archivist(Path(t))
                for persona in ("specialist", "archivist"):  # specialist = positive control
                    try:
                        bridge.command_sheet(
                            persona,
                            expected_pantheon_revision="0" * 40,
                            manifest_path=m_path,
                            root=Path(t),
                        )
                    except Exception:
                        pass  # generic path may need host state; only the routing matters
        finally:
            bridge._legacy_command_sheet = orig
        self.assertEqual(calls, ["specialist"], "only specialist/recovery may take the legacy path")


if __name__ == "__main__":
    unittest.main()
