"""Tests for tools/scheduler_spec.py (T-2896, ORG-064 phase 1).

Hermetic: every case builds its own tmp `schedules/` dir (+ tmp template dir where a
`unit.template` reference is exercised) and passes them explicitly to `load_spec()`.
Nothing reads the real schedules/ dir except the two sanity checks at the bottom,
which prove the checked-in spec this ticket ships is itself valid.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import scheduler_spec as sspec


_MINIMAL_TEMPLATE_SERVICE = "[Unit]\nDescription=x\n\n[Service]\nExecStart=/bin/true\n"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.schedules = self.root / "schedules"
        self.schedules.mkdir()
        self.templates = self.root / "templates"
        self.templates.mkdir()

    def write_template(self, name: str) -> None:
        _write(self.templates / f"{name}.service", _MINIMAL_TEMPLATE_SERVICE)

    def write_global(self, content: str) -> None:
        _write(self.schedules / sspec.GLOBAL_FILE, content)

    def write_persona(self, persona: str, content: str) -> None:
        _write(self.schedules / f"{persona}.yaml", content)

    def load(self) -> sspec.Spec:
        return sspec.load_spec(self.schedules, template_dir=self.templates)


class TestValidSpecLoads(_Base):
    def test_host_and_all_persona_and_per_persona_load(self) -> None:
        self.write_template("pantheon-a")
        self.write_template("pantheon-b")
        self.write_template("pantheon-c")
        self.write_global(
            """
entries:
  - name: pantheon-a
    scope: host
    calendar: "*-*-* 03:30:00 UTC"
    unit: {template: pantheon-a}
    on_failure: none
    owner: orchestrator
    serves: "host chore"
  - name: pantheon-b
    scope: all-persona
    calendar: "*-*-* 09:00:00 UTC"
    unit: {template: pantheon-b}
    on_failure: standard
    owner: recovery
    serves: "fleet watchdog"
    system: "Harness Manager — persona seats: install/launch/supervise"
"""
        )
        self.write_persona(
            "specialist",
            """
entries:
  - name: pantheon-c
    scope: per-persona
    calendar: "*-*-* 08:00:00 UTC"
    unit: {template: pantheon-c}
    on_failure: standard
    owner: specialist
    serves: "specialist cadence"
    system: "Watch / research pipeline"
""",
        )
        spec = self.load()
        self.assertEqual(set(spec.entries), {"pantheon-a", "pantheon-b", "pantheon-c"})
        self.assertEqual(spec.entries["pantheon-c"].persona, "specialist")
        self.assertEqual(spec.entries["pantheon-a"].scope, sspec.SCOPE_HOST)
        # pantheon-a has no `system:` -> exactly one warning for it.
        self.assertEqual(len(spec.warnings), 1)
        self.assertIn("pantheon-a", spec.warnings[0])

    def test_inline_unit_payload(self) -> None:
        self.write_global(
            """
entries:
  - name: pantheon-inline
    scope: host
    calendar: "*-*-* 04:00:00 UTC"
    unit:
      description: "an inline job"
      exec_start: "/usr/bin/python3 /home/claude/pantheon-config/tools/x.py"
      working_directory: "%h/pantheon-config"
      environment: ["PATH=/usr/bin:/bin"]
      type: oneshot
      timeout_start_sec: 5min
    on_failure: none
    owner: orchestrator
    serves: "proves inline payload"
"""
        )
        spec = self.load()
        payload = spec.entries["pantheon-inline"].unit
        self.assertFalse(payload.is_template)
        self.assertEqual(payload.exec_start, "/usr/bin/python3 /home/claude/pantheon-config/tools/x.py")
        self.assertEqual(payload.environment, ("PATH=/usr/bin:/bin",))


class TestEntryValidationErrors(_Base):
    def test_missing_required_field(self) -> None:
        self.write_global(
            """
entries:
  - name: pantheon-x
    scope: host
    unit: {template: pantheon-x}
    on_failure: none
    owner: orchestrator
    serves: "no calendar"
"""
        )
        with self.assertRaises(sspec.SpecError) as ctx:
            self.load()
        self.assertIn("calendar", str(ctx.exception))

    def test_bad_scope(self) -> None:
        self.write_template("pantheon-x")
        self.write_global(
            """
entries:
  - name: pantheon-x
    scope: planet
    calendar: "*-*-* 03:30:00 UTC"
    unit: {template: pantheon-x}
    on_failure: none
    owner: orchestrator
    serves: "bad scope"
"""
        )
        with self.assertRaises(sspec.SpecError) as ctx:
            self.load()
        self.assertIn("scope", str(ctx.exception))

    def test_bad_on_failure(self) -> None:
        self.write_template("pantheon-x")
        self.write_global(
            """
entries:
  - name: pantheon-x
    scope: host
    calendar: "*-*-* 03:30:00 UTC"
    unit: {template: pantheon-x}
    on_failure: sometimes
    owner: orchestrator
    serves: "bad on_failure"
"""
        )
        with self.assertRaises(sspec.SpecError):
            self.load()

    def test_unit_missing_template_file(self) -> None:
        self.write_global(
            """
entries:
  - name: pantheon-x
    scope: host
    calendar: "*-*-* 03:30:00 UTC"
    unit: {template: pantheon-nonexistent}
    on_failure: none
    owner: orchestrator
    serves: "missing template"
"""
        )
        with self.assertRaises(sspec.SpecError) as ctx:
            self.load()
        self.assertIn("pantheon-nonexistent", str(ctx.exception))

    def test_unit_template_exclusive(self) -> None:
        self.write_template("pantheon-x")
        self.write_global(
            """
entries:
  - name: pantheon-x
    scope: host
    calendar: "*-*-* 03:30:00 UTC"
    unit: {template: pantheon-x, exec_start: "/bin/true"}
    on_failure: none
    owner: orchestrator
    serves: "template + inline"
"""
        )
        with self.assertRaises(sspec.SpecError):
            self.load()

    def test_inline_unit_missing_required(self) -> None:
        self.write_global(
            """
entries:
  - name: pantheon-x
    scope: host
    calendar: "*-*-* 03:30:00 UTC"
    unit:
      description: "no exec_start"
    on_failure: none
    owner: orchestrator
    serves: "incomplete inline"
"""
        )
        with self.assertRaises(sspec.SpecError):
            self.load()

    def test_duplicate_name_across_files(self) -> None:
        self.write_template("pantheon-dup")
        self.write_global(
            """
entries:
  - name: pantheon-dup
    scope: all-persona
    calendar: "*-*-* 03:30:00 UTC"
    unit: {template: pantheon-dup}
    on_failure: none
    owner: orchestrator
    serves: "first"
"""
        )
        self.write_persona(
            "specialist",
            """
entries:
  - name: pantheon-dup
    scope: per-persona
    calendar: "*-*-* 03:30:00 UTC"
    unit: {template: pantheon-dup}
    on_failure: none
    owner: specialist
    serves: "second, same name"
""",
        )
        with self.assertRaises(sspec.SpecError) as ctx:
            self.load()
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_per_persona_scope_forbidden_in_global(self) -> None:
        self.write_template("pantheon-x")
        self.write_global(
            """
entries:
  - name: pantheon-x
    scope: per-persona
    calendar: "*-*-* 03:30:00 UTC"
    unit: {template: pantheon-x}
    on_failure: none
    owner: orchestrator
    serves: "wrong file for per-persona"
"""
        )
        with self.assertRaises(sspec.SpecError) as ctx:
            self.load()
        self.assertIn("global.yaml", str(ctx.exception))

    def test_host_scope_forbidden_in_persona_file(self) -> None:
        self.write_template("pantheon-x")
        self.write_persona(
            "specialist",
            """
entries:
  - name: pantheon-x
    scope: host
    calendar: "*-*-* 03:30:00 UTC"
    unit: {template: pantheon-x}
    on_failure: none
    owner: specialist
    serves: "wrong file for host"
""",
        )
        with self.assertRaises(sspec.SpecError):
            self.load()

    def test_explicit_persona_field_forbidden(self) -> None:
        self.write_template("pantheon-x")
        self.write_persona(
            "specialist",
            """
entries:
  - name: pantheon-x
    scope: per-persona
    persona: specialist
    calendar: "*-*-* 03:30:00 UTC"
    unit: {template: pantheon-x}
    on_failure: none
    owner: specialist
    serves: "sets persona explicitly"
""",
        )
        with self.assertRaises(sspec.SpecError) as ctx:
            self.load()
        self.assertIn("persona", str(ctx.exception))

    def test_unknown_persona_filename(self) -> None:
        self.write_persona(
            "not-a-real-persona",
            "entries: []\n",
        )
        with self.assertRaises(sspec.SpecError) as ctx:
            self.load()
        self.assertIn("not-a-real-persona", str(ctx.exception))

    def test_unknown_entry_field_rejected(self) -> None:
        self.write_template("pantheon-x")
        self.write_global(
            """
entries:
  - name: pantheon-x
    scope: host
    calendar: "*-*-* 03:30:00 UTC"
    unit: {template: pantheon-x}
    on_failure: none
    owner: orchestrator
    serves: "extra field"
    made_up_field: true
"""
        )
        with self.assertRaises(sspec.SpecError):
            self.load()


class TestExceptionValidation(_Base):
    def _base_global(self) -> None:
        self.write_template("pantheon-fleet")
        self.write_template("pantheon-solo")
        self.write_global(
            """
entries:
  - name: pantheon-fleet
    scope: all-persona
    calendar: "*-*-* 09:00:00 UTC"
    unit: {template: pantheon-fleet}
    on_failure: standard
    owner: recovery
    serves: "fleet default"
  - name: pantheon-solo
    scope: host
    calendar: "*-*-* 03:00:00 UTC"
    unit: {template: pantheon-solo}
    on_failure: none
    owner: orchestrator
    serves: "host-only"
"""
        )

    def test_valid_opt_out_and_time_override(self) -> None:
        self._base_global()
        self.write_persona(
            "specialist",
            """
exceptions:
  - entry: pantheon-fleet
    kind: opt-out
    reason: "specialist runs its own variant"
""",
        )
        self.write_persona(
            "recovery",
            """
exceptions:
  - entry: pantheon-fleet
    kind: time-override
    calendar: "*-*-* 11:00:00 UTC"
""",
        )
        spec = self.load()
        self.assertEqual(spec.exceptions["specialist"][0].kind, sspec.KIND_OPT_OUT)
        self.assertEqual(spec.exceptions["recovery"][0].kind, sspec.KIND_TIME_OVERRIDE)

    def test_opt_out_requires_reason(self) -> None:
        self._base_global()
        self.write_persona(
            "specialist",
            """
exceptions:
  - entry: pantheon-fleet
    kind: opt-out
""",
        )
        with self.assertRaises(sspec.SpecError) as ctx:
            self.load()
        self.assertIn("reason", str(ctx.exception))

    def test_opt_out_forbids_calendar(self) -> None:
        self._base_global()
        self.write_persona(
            "specialist",
            """
exceptions:
  - entry: pantheon-fleet
    kind: opt-out
    reason: "x"
    calendar: "*-*-* 12:00:00 UTC"
""",
        )
        with self.assertRaises(sspec.SpecError):
            self.load()

    def test_time_override_requires_calendar(self) -> None:
        self._base_global()
        self.write_persona(
            "specialist",
            """
exceptions:
  - entry: pantheon-fleet
    kind: time-override
""",
        )
        with self.assertRaises(sspec.SpecError) as ctx:
            self.load()
        self.assertIn("calendar", str(ctx.exception))

    def test_unknown_entry_reference(self) -> None:
        self._base_global()
        self.write_persona(
            "specialist",
            """
exceptions:
  - entry: pantheon-does-not-exist
    kind: opt-out
    reason: "x"
""",
        )
        with self.assertRaises(sspec.SpecError) as ctx:
            self.load()
        self.assertIn("unknown entry", str(ctx.exception))

    def test_exception_against_non_all_persona_entry_rejected(self) -> None:
        self._base_global()
        self.write_persona(
            "specialist",
            """
exceptions:
  - entry: pantheon-solo
    kind: opt-out
    reason: "x"
""",
        )
        with self.assertRaises(sspec.SpecError) as ctx:
            self.load()
        self.assertIn("scope=", str(ctx.exception))

    def test_exception_with_payload_key_rejected(self) -> None:
        self._base_global()
        self.write_persona(
            "specialist",
            """
exceptions:
  - entry: pantheon-fleet
    kind: opt-out
    reason: "x"
    unit: {template: pantheon-solo}
""",
        )
        with self.assertRaises(sspec.SpecError) as ctx:
            self.load()
        self.assertIn("WHEN/WHETHER", str(ctx.exception))

    def test_duplicate_exception_for_same_entry(self) -> None:
        self._base_global()
        self.write_persona(
            "specialist",
            """
exceptions:
  - entry: pantheon-fleet
    kind: opt-out
    reason: "first"
  - entry: pantheon-fleet
    kind: time-override
    calendar: "*-*-* 11:00:00 UTC"
""",
        )
        with self.assertRaises(sspec.SpecError) as ctx:
            self.load()
        self.assertIn("duplicate", str(ctx.exception).lower())


class TestMerge(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.write_template("pantheon-fleet")
        self.write_template("pantheon-solo")
        self.write_template("pantheon-specialist-only")
        self.write_global(
            """
entries:
  - name: pantheon-fleet
    scope: all-persona
    calendar: "*-*-* 09:00:00 UTC"
    unit: {template: pantheon-fleet}
    on_failure: standard
    owner: recovery
    serves: "fleet default"
  - name: pantheon-solo
    scope: host
    calendar: "*-*-* 03:00:00 UTC"
    unit: {template: pantheon-solo}
    on_failure: none
    owner: orchestrator
    serves: "host-only"
"""
        )
        self.write_persona(
            "specialist",
            """
entries:
  - name: pantheon-specialist-only
    scope: per-persona
    calendar: "*-*-* 08:00:00 UTC"
    unit: {template: pantheon-specialist-only}
    on_failure: standard
    owner: specialist
    serves: "specialist cadence"

exceptions:
  - entry: pantheon-fleet
    kind: time-override
    calendar: "*-*-* 11:00:00 UTC"
""",
        )
        self.write_persona(
            "recovery",
            """
exceptions:
  - entry: pantheon-fleet
    kind: opt-out
    reason: "recovery runs a bespoke variant"
""",
        )
        self.spec = self.load()

    def test_default_persona_gets_all_persona_entry_unchanged(self) -> None:
        eff = sspec.effective_schedule(self.spec, "codex")
        names = {e.entry.name: e.calendar for e in eff}
        self.assertEqual(names, {"pantheon-fleet": "*-*-* 09:00:00 UTC"})

    def test_time_override_changes_only_calendar(self) -> None:
        eff = sspec.effective_schedule(self.spec, "specialist")
        by_name = {e.entry.name: e for e in eff}
        self.assertEqual(by_name["pantheon-fleet"].calendar, "*-*-* 11:00:00 UTC")
        self.assertIn("pantheon-specialist-only", by_name)

    def test_opt_out_excludes_entry_entirely(self) -> None:
        eff = sspec.effective_schedule(self.spec, "recovery")
        names = {e.entry.name for e in eff}
        self.assertNotIn("pantheon-fleet", names)

    def test_host_entries_excluded_from_every_persona(self) -> None:
        for persona in sspec.fleet_personas():
            eff = sspec.effective_schedule(self.spec, persona)
            self.assertNotIn("pantheon-solo", {e.entry.name for e in eff})
        self.assertEqual([e.name for e in sspec.host_entries(self.spec)], ["pantheon-solo"])

    def test_targets_spans_fleet_minus_opt_outs_with_overrides(self) -> None:
        entry = self.spec.entries["pantheon-fleet"]
        eff = sspec.targets(self.spec, entry)
        by_persona = {e.persona: e.calendar for e in eff}
        self.assertNotIn("recovery", by_persona)  # opted out
        self.assertEqual(by_persona["specialist"], "*-*-* 11:00:00 UTC")  # time-override
        self.assertEqual(by_persona["orchestrator"], "*-*-* 09:00:00 UTC")  # untouched default
        self.assertEqual(set(by_persona), set(sspec.fleet_personas()) - {"recovery"})

    def test_targets_for_per_persona_entry_is_single(self) -> None:
        entry = self.spec.entries["pantheon-specialist-only"]
        eff = sspec.targets(self.spec, entry)
        self.assertEqual([e.persona for e in eff], ["specialist"])

    def test_targets_for_host_entry_is_empty(self) -> None:
        entry = self.spec.entries["pantheon-solo"]
        self.assertEqual(sspec.targets(self.spec, entry), [])


class TestRealCheckedInSpec(unittest.TestCase):
    """Sanity: the spec this ticket actually ships loads clean."""

    def test_real_spec_loads(self) -> None:
        if not sspec.SCHEDULES_DIR.is_dir():
            self.skipTest("schedules/ not present in this checkout")
        spec = sspec.load_spec()
        self.assertIn("pantheon-vps-state-backup", spec.entries)
        self.assertIn("pantheon-watch", spec.entries)
        # every fleet persona gets a merge answer without raising
        for persona in sspec.fleet_personas():
            sspec.effective_schedule(spec, persona)


if __name__ == "__main__":
    unittest.main()
