"""Tests for tools/scheduler_reconcile.py (T-2896, ORG-064 phase 1).

Hermetic throughout: every case builds its own tmp `schedules/` + tmp template dir
+ tmp host dir, and installed-account reads go through a fake `Ops` that returns
canned fixture data — NEVER the real `sudo -n -u <account>` subprocess, NEVER real
`/etc/systemd/system`. No test enables/starts/reloads anything; there is no apply
path in this module to accidentally exercise.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import scheduler_reconcile as recon
from tools import scheduler_spec as sspec
from tools import systemd_drift_sweep as dsweep


_TEMPLATE_SERVICE = (
    "[Unit]\n"
    "Description=a template unit\n"
    "OnFailure=pantheon-unit-failure-alert@%n.service\n"
    "\n"
    "[Service]\n"
    "Type=oneshot\n"
    "ExecStart=/usr/bin/python3 %h/pantheon-config/tools/x.py\n"
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class _FakeOps(dsweep.Ops):
    """Canned account reads — no subprocess, no real sudo."""

    def __init__(self, results: dict[str, tuple[str, dict[str, str] | None]]):
        self._results = results

    def read_installed_dir(self, account: str) -> tuple[str, dict[str, str] | None]:
        return self._results.get(account, ("no fixture registered", None))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.schedules = self.root / "schedules"
        self.schedules.mkdir()
        self.templates = self.root / "templates"
        self.templates.mkdir()
        self.host_dir = self.root / "etc-systemd-system"

    def write_template(self, name: str, content: str = _TEMPLATE_SERVICE) -> None:
        _write(self.templates / f"{name}.service", content)

    def write_global(self, content: str) -> None:
        _write(self.schedules / sspec.GLOBAL_FILE, content)

    def write_persona(self, persona: str, content: str) -> None:
        _write(self.schedules / f"{persona}.yaml", content)

    def load(self) -> sspec.Spec:
        return sspec.load_spec(self.schedules, template_dir=self.templates)


class TestRenderService(_Base):
    def test_template_payload_is_verbatim(self) -> None:
        self.write_template("pantheon-t")
        self.write_global(
            """
entries:
  - name: pantheon-t
    scope: host
    calendar: "*-*-* 03:00:00 UTC"
    unit: {template: pantheon-t}
    on_failure: standard
    owner: orchestrator
    serves: "x"
"""
        )
        spec = self.load()
        text = recon.render_service(spec.entries["pantheon-t"], template_dir=self.templates)
        self.assertEqual(text, _TEMPLATE_SERVICE)

    def test_inline_payload_synthesizes_on_failure_standard(self) -> None:
        self.write_global(
            """
entries:
  - name: pantheon-inline
    scope: host
    calendar: "*-*-* 03:00:00 UTC"
    unit:
      description: "inline job"
      exec_start: "/usr/bin/python3 /x.py"
    on_failure: standard
    owner: orchestrator
    serves: "x"
"""
        )
        spec = self.load()
        text = recon.render_service(spec.entries["pantheon-inline"], template_dir=self.templates)
        self.assertIn("OnFailure=pantheon-unit-failure-alert@%n.service", text)
        self.assertIn("ExecStart=/usr/bin/python3 /x.py", text)

    def test_inline_payload_on_failure_none_omits_wiring(self) -> None:
        self.write_global(
            """
entries:
  - name: pantheon-inline2
    scope: host
    calendar: "*-*-* 03:00:00 UTC"
    unit:
      description: "inline job"
      exec_start: "/usr/bin/python3 /x.py"
    on_failure: none
    owner: orchestrator
    serves: "x"
"""
        )
        spec = self.load()
        text = recon.render_service(spec.entries["pantheon-inline2"], template_dir=self.templates)
        self.assertNotIn("OnFailure=", text)


class TestRenderTimer(_Base):
    def test_calendar_and_persistent_render(self) -> None:
        self.write_template("pantheon-t")
        self.write_global(
            """
entries:
  - name: pantheon-t
    scope: host
    calendar: "*-*-* 03:00:00 UTC"
    unit: {template: pantheon-t}
    on_failure: none
    owner: orchestrator
    serves: "x"
"""
        )
        spec = self.load()
        entry = spec.entries["pantheon-t"]
        text = recon.render_timer(entry, "*-*-* 11:00:00 UTC", True)
        self.assertIn("OnCalendar=*-*-* 11:00:00 UTC", text)
        self.assertIn("Persistent=true", text)
        self.assertIn("Unit=pantheon-t.service", text)

    def test_persistent_false_omits_directive(self) -> None:
        self.write_template("pantheon-t")
        self.write_global(
            """
entries:
  - name: pantheon-t
    scope: host
    calendar: "*-*-* 03:00:00 UTC"
    persistent: false
    unit: {template: pantheon-t}
    on_failure: none
    owner: orchestrator
    serves: "x"
"""
        )
        spec = self.load()
        text = recon.render_timer(spec.entries["pantheon-t"], "*-*-* 03:00:00 UTC", False)
        self.assertNotIn("Persistent=", text)


# A plausible HAND-AUTHORED `.timer` — the exact shape that bit T-2896 REBOUND
# round 1: this is what a real `tools/systemd/*.timer` file looks like (its own
# Description + a Documentation= line), NOT the rendered-text-as-installed shape
# the pre-fix fixtures used (which is why that suite missed the false-positive —
# comparing rendered-against-itself can never surface a synthesized-field mismatch).
_HAND_AUTHORED_TIMER = (
    "[Unit]\n"
    "Description=Pantheon host thing — daily fire (03:00 UTC)\n"
    "Documentation=file:///home/claude/pantheon-config/tickets/done/T-0000.md\n"
    "\n"
    "[Timer]\n"
    "OnCalendar=*-*-* 03:00:00 UTC\n"
    "Persistent=true\n"
    "Unit=pantheon-host-thing.service\n"
    "\n"
    "[Install]\n"
    "WantedBy=timers.target\n"
)


class TestClassify(unittest.TestCase):
    def test_match(self) -> None:
        src = recon.InstalledDir(label="x", reachable=True, files={"pantheon-t.service": "same\n"})
        status, detail, severity = recon._classify("same\n", "pantheon-t.service", src)
        self.assertEqual(status, recon.STATUS_MATCH)
        self.assertEqual(severity, "")

    def test_drift_substantive_has_detail_and_severity(self) -> None:
        src = recon.InstalledDir(
            label="x", reachable=True,
            files={"pantheon-t.service": "[Service]\nExecStart=/bin/old\n"},
        )
        status, detail, severity = recon._classify("[Service]\nExecStart=/bin/new\n", "pantheon-t.service", src)
        self.assertEqual(status, recon.STATUS_DRIFT)
        self.assertEqual(severity, "substantive")
        self.assertTrue(detail)

    def test_drift_cosmetic_description_documentation_only(self) -> None:
        """LOCKING TEST (T-2896 REBOUND round 1): a rendered `.timer` differing from
        a plausible hand-authored installed `.timer` ONLY in Description=/
        Documentation= — same OnCalendar/Persistent/Unit — must classify
        DRIFT-COSMETIC, never the substantive DRIFT bucket."""
        src = recon.InstalledDir(
            label="x", reachable=True, files={"pantheon-host-thing.timer": _HAND_AUTHORED_TIMER},
        )
        rendered = (
            "[Unit]\n"
            "Description=Pantheon schedule: pantheon-host-thing — a host chore\n"
            "\n"
            "[Timer]\n"
            "OnCalendar=*-*-* 03:00:00 UTC\n"
            "Persistent=true\n"
            "Unit=pantheon-host-thing.service\n"
            "\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )
        status, detail, severity = recon._classify(rendered, "pantheon-host-thing.timer", src)
        self.assertEqual(status, recon.STATUS_DRIFT_COSMETIC)
        self.assertEqual(severity, "cosmetic")
        self.assertNotEqual(status, recon.STATUS_DRIFT)

    def test_drift_substantive_when_calendar_also_differs(self) -> None:
        """Same Description mismatch as above, PLUS a real OnCalendar change — must
        NOT be swallowed into cosmetic just because Description also differs."""
        src = recon.InstalledDir(
            label="x", reachable=True, files={"pantheon-host-thing.timer": _HAND_AUTHORED_TIMER},
        )
        rendered = (
            "[Unit]\n"
            "Description=Pantheon schedule: pantheon-host-thing — a host chore\n"
            "\n"
            "[Timer]\n"
            "OnCalendar=*-*-* 05:00:00 UTC\n"
            "Persistent=true\n"
            "Unit=pantheon-host-thing.service\n"
            "\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )
        status, _, severity = recon._classify(rendered, "pantheon-host-thing.timer", src)
        self.assertEqual(status, recon.STATUS_DRIFT)
        self.assertEqual(severity, "substantive")

    def test_create_when_not_installed(self) -> None:
        src = recon.InstalledDir(label="x", reachable=True, files={})
        status, _, severity = recon._classify("anything", "pantheon-t.service", src)
        self.assertEqual(status, recon.STATUS_CREATE)
        self.assertEqual(severity, "")

    def test_unverified_when_unreachable(self) -> None:
        src = recon.InstalledDir(label="x", reachable=False, files=None, reason="denied")
        status, detail, severity = recon._classify("anything", "pantheon-t.service", src)
        self.assertEqual(status, recon.STATUS_UNVERIFIED)
        self.assertEqual(detail, "denied")
        self.assertEqual(severity, "")

    def test_unverified_when_listed_but_unreadable(self) -> None:
        src = recon.InstalledDir(label="x", reachable=True, files={"pantheon-t.service": None})
        status, _, severity = recon._classify("anything", "pantheon-t.service", src)
        self.assertEqual(status, recon.STATUS_UNVERIFIED)
        self.assertEqual(severity, "")


class TestBuildPlan(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.write_template("pantheon-host-thing")
        self.write_template("pantheon-fleet-thing")
        self.write_template("pantheon-specialist-thing")
        self.write_global(
            """
entries:
  - name: pantheon-host-thing
    scope: host
    calendar: "*-*-* 03:00:00 UTC"
    unit: {template: pantheon-host-thing}
    on_failure: none
    owner: orchestrator
    serves: "host chore"
  - name: pantheon-fleet-thing
    scope: all-persona
    calendar: "*-*-* 09:00:00 UTC"
    unit: {template: pantheon-fleet-thing}
    on_failure: standard
    owner: recovery
    serves: "fleet watchdog"
"""
        )
        self.write_persona(
            "specialist",
            """
entries:
  - name: pantheon-specialist-thing
    scope: per-persona
    calendar: "*-*-* 08:00:00 UTC"
    unit: {template: pantheon-specialist-thing}
    on_failure: standard
    owner: specialist
    serves: "specialist cadence"
""",
        )
        self.spec = self.load()

    def test_host_match_and_persona_create_and_unverified(self) -> None:
        # Host dir "installed" already matches the rendered host unit.
        rendered_host_service, rendered_host_timer = recon.render_pair(
            self.spec.entries["pantheon-host-thing"], "*-*-* 03:00:00 UTC", True, self.templates,
        )
        _write(self.host_dir / "pantheon-host-thing.service", rendered_host_service)
        _write(self.host_dir / "pantheon-host-thing.timer", rendered_host_timer)

        # orchestrator's own account (claude) reachable but empty -> CREATE for fleet-thing.
        # specialist's account reachable but empty -> CREATE for both fleet-thing + specialist-thing.
        # recovery's account unreadable -> UNVERIFIED.
        fake = _FakeOps({
            "claude": ("", {}),
            "claude-research": ("", {}),
            "claude-fallback": ("denied: no NOPASSWD grant", None),
            "codex": ("", {}),
            "the-ancestor": ("", {}),
            "exocortex": ("", {}),
            "suborch1": ("", {}),
            "suborch2": ("", {}),
        })
        result = recon.build_plan(self.spec, ops=fake, template_dir=self.templates, host_dir=self.host_dir)
        by_key = {(i.target, i.unit, i.suffix): i.status for i in result.items}

        self.assertEqual(by_key[("host", "pantheon-host-thing", "service")], recon.STATUS_MATCH)
        self.assertEqual(by_key[("host", "pantheon-host-thing", "timer")], recon.STATUS_MATCH)
        self.assertEqual(by_key[("orchestrator@claude", "pantheon-fleet-thing", "service")], recon.STATUS_CREATE)
        self.assertEqual(by_key[("specialist@claude-research", "pantheon-specialist-thing", "service")], recon.STATUS_CREATE)
        self.assertEqual(by_key[("recovery@claude-fallback", "pantheon-fleet-thing", "service")], recon.STATUS_UNVERIFIED)
        self.assertIn("claude-fallback:~/.config/systemd/user", result.unreachable)

    def test_hand_authored_timer_description_only_is_cosmetic_not_substantive(self) -> None:
        """End-to-end LOCKING TEST (T-2896 REBOUND round 1) — the exact real-box
        shape: `.service` is the verbatim template (MATCH), `.timer` is a plausible
        hand-authored install (own Description=, no Documentation=) whose
        OnCalendar/Persistent/Unit are otherwise identical to the render. The
        substantive DRIFT count must be zero; the mismatch must land in
        DRIFT-COSMETIC."""
        rendered_service, _ = recon.render_pair(
            self.spec.entries["pantheon-host-thing"], "*-*-* 03:00:00 UTC", True, self.templates,
        )
        _write(self.host_dir / "pantheon-host-thing.service", rendered_service)
        _write(
            self.host_dir / "pantheon-host-thing.timer",
            "[Unit]\n"
            "Description=Pantheon host thing — daily fire (03:00 UTC)\n"
            "\n"
            "[Timer]\n"
            "OnCalendar=*-*-* 03:00:00 UTC\n"
            "Persistent=true\n"
            "Unit=pantheon-host-thing.service\n"
            "\n"
            "[Install]\n"
            "WantedBy=timers.target\n",
        )
        fake = _FakeOps({a: ("", {}) for a in [
            "claude", "claude-research", "claude-fallback", "codex", "the-ancestor",
            "exocortex", "suborch1", "suborch2",
        ]})
        result = recon.build_plan(self.spec, ops=fake, template_dir=self.templates, host_dir=self.host_dir)
        counts = result.counts()

        self.assertEqual(counts.get(recon.STATUS_DRIFT, 0), 0)
        self.assertEqual(counts.get(recon.STATUS_DRIFT_COSMETIC, 0), 1)
        cosmetic = [i for i in result.items if i.status == recon.STATUS_DRIFT_COSMETIC]
        self.assertEqual([(i.target, i.unit, i.suffix) for i in cosmetic],
                          [("host", "pantheon-host-thing", "timer")])
        self.assertEqual(cosmetic[0].severity, "cosmetic")
        service_item = next(i for i in result.items
                             if (i.target, i.unit, i.suffix) == ("host", "pantheon-host-thing", "service"))
        self.assertEqual(service_item.status, recon.STATUS_MATCH)

    def test_orphan_reported_never_touched(self) -> None:
        _write(self.host_dir / "pantheon-mystery.service", "[Unit]\nDescription=nobody claims this\n")
        fake = _FakeOps({a: ("", {}) for a in [
            "claude", "claude-research", "claude-fallback", "codex", "the-ancestor",
            "exocortex", "suborch1", "suborch2",
        ]})
        result = recon.build_plan(self.spec, ops=fake, template_dir=self.templates, host_dir=self.host_dir)
        orphans = [i for i in result.items if i.status == recon.STATUS_ORPHAN]
        self.assertEqual([(o.target, o.unit) for o in orphans], [("host", "pantheon-mystery")])
        # Orphan detection must never mutate the fixture directory (report, never touch).
        self.assertTrue((self.host_dir / "pantheon-mystery.service").exists())

    def test_account_filter_limits_targets(self) -> None:
        fake = _FakeOps({a: ("", {}) for a in [
            "claude", "claude-research", "claude-fallback", "codex", "the-ancestor",
            "exocortex", "suborch1", "suborch2",
        ]})
        result = recon.build_plan(
            self.spec, accounts=["specialist"], ops=fake, template_dir=self.templates, host_dir=self.host_dir,
        )
        targets_hit = {i.target for i in result.items}
        self.assertEqual(targets_hit, {"specialist@claude-research"})

    def test_spec_warnings_pass_through(self) -> None:
        fake = _FakeOps({a: ("", {}) for a in [
            "claude", "claude-research", "claude-fallback", "codex", "the-ancestor",
            "exocortex", "suborch1", "suborch2",
        ]})
        result = recon.build_plan(self.spec, ops=fake, template_dir=self.templates, host_dir=self.host_dir)
        # None of the fixture entries set `system:` -> one warning per entry.
        self.assertEqual(len(result.warnings), 3)


class TestCLI(_Base):
    def test_plan_json_smoke(self) -> None:
        # Hermetic: build_plan is mocked out here (fully exercised, with fixture
        # Ops/host_dir, in TestBuildPlan above) — this test only proves argv wiring
        # (spec loads, --json path, exit code), never spawns a real `sudo -n -u`
        # subprocess or reads the real /etc/systemd/system.
        self.write_template("pantheon-t")
        self.write_global(
            """
entries:
  - name: pantheon-t
    scope: host
    calendar: "*-*-* 03:00:00 UTC"
    unit: {template: pantheon-t}
    on_failure: none
    owner: orchestrator
    serves: "x"
"""
        )
        fake_result = recon.PlanResult(items=[], warnings=[], unreachable=[])
        with mock.patch.object(recon, "build_plan", return_value=fake_result) as m:
            rc = recon.main([
                "plan", "--json",
                "--schedules-dir", str(self.schedules),
                "--template-dir", str(self.templates),
            ])
        self.assertEqual(rc, 0)
        m.assert_called_once()

    def test_bad_spec_dir_returns_2(self) -> None:
        rc = recon.main(["plan", "--schedules-dir", str(self.root / "does-not-exist")])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
