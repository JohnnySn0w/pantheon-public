"""Tests for tools/systemd_drift_sweep.py — the cross-account drift sweep (T-2444).

Hermetic: a tempdir plays the repo unit tree, and a FakeOps plays the cross-account
sudo reader. Nothing here shells out or touches a real account, so the three traps the
module exists to honour are asserted as LOGIC, on every seat and on a CI runner:

  (a) a could-not-ask account must never read as clean-empty,
  (b) reachable-but-zero-tracked-units is UNVERIFIED, not CLEAN,
  (c) drift is a CONTENT difference (cp's, not symlinks).

Plus the severity split that keeps a doc-path reshuffle from paging (correction #2) and
the sudo-to-self carve-out (trap a's mechanism).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools import systemd_drift_sweep as sweep


class FakeOps:
    """Plays Ops.read_installed_dir from a canned {account: (reason, files|None)} map.
    files is None to model an unreachable account; {} to model asked-found-nothing."""

    def __init__(self, per_account: dict):
        self._map = per_account

    def read_installed_dir(self, account: str):
        return self._map.get(account, ("unknown account", None))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "units"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _repo_unit(self, name: str, text: str) -> None:
        (self.repo / name).write_text(text, encoding="utf-8")

    def _sweep(self, account: str, files, reason=""):
        ops = FakeOps({account: (reason, files)})
        return sweep.sweep_account(account, ops=ops, unit_dir=self.repo)


class TestPerAccountStatus(_Base):
    def test_matching_installed_is_clean(self):
        body = "[Service]\nExecStart=/bin/true\n"
        self._repo_unit("a.service", body)
        r = self._sweep("codex", {"a.service": body})
        self.assertEqual(r["status"], sweep.CLEAN)
        self.assertEqual(r["compared"], 1)
        self.assertEqual(r["substantive"], [])

    def test_content_difference_is_drift(self):
        # trap (c): a cp drifts by CONTENT. Repo ahead of the installed copy.
        self._repo_unit("b.timer", "[Timer]\nOnBootSec=2min\nOnActiveSec=5min\n")
        r = self._sweep("codex", {"b.timer": "[Timer]\nOnBootSec=2min\n"})
        self.assertEqual(r["status"], sweep.DRIFT)
        self.assertEqual([d["unit"] for d in r["substantive"]], ["b.timer"])

    def test_documentation_only_drift_is_cosmetic_and_clean_status(self):
        # correction #2: a Documentation= reshuffle is real drift but must NOT flip the
        # account to DRIFT (which alerts) — it is reported as cosmetic.
        self._repo_unit("c.service",
                        "[Unit]\nDocumentation=file:///new\n[Service]\nExecStart=/bin/true\n")
        r = self._sweep("claude-research",
                        {"c.service": "[Unit]\nDocumentation=file:///old\n[Service]\nExecStart=/bin/true\n"})
        self.assertEqual(r["status"], sweep.CLEAN)
        self.assertEqual([d["unit"] for d in r["cosmetic"]], ["c.service"])
        self.assertEqual(r["substantive"], [])

    def test_unreachable_account_is_never_clean(self):
        # trap (a): a denied sudo leaves empty stdout — Ops returns files=None, and the
        # sweep must surface UNREACHABLE, never a clean empty seat.
        self._repo_unit("d.service", "[Service]\nExecStart=/bin/true\n")
        r = self._sweep("exocortex", None, reason="query failed rc=1")
        self.assertEqual(r["status"], sweep.UNREACHABLE)
        self.assertEqual(r["compared"], 0)
        self.assertIn("query failed", r["reason"])

    def test_reachable_but_no_tracked_units_is_unverified_not_clean(self):
        # trap (b): an account we CAN read but that has none of our tracked units is
        # NO_UNITS, not CLEAN — else a wiped seat reads green.
        self._repo_unit("e.service", "[Service]\nExecStart=/bin/true\n")
        r = self._sweep("the-ancestor", {})  # read fine, zero tracked units
        self.assertEqual(r["status"], sweep.NO_UNITS)
        self.assertEqual(r["compared"], 0)

    def test_persona_is_resolved(self):
        self._repo_unit("f.service", "[Service]\nExecStart=/bin/true\n")
        r = self._sweep("claude-research", {"f.service": "[Service]\nExecStart=/bin/true\n"})
        self.assertEqual(r["persona"], "specialist")


class TestAlerting(_Base):
    def test_only_drift_and_unreachable_alert(self):
        self._repo_unit("real.service", "[Service]\nExecStart=/bin/true serve\n")
        self._repo_unit("doc.service",
                        "[Unit]\nDocumentation=file:///new\n[Service]\nExecStart=/bin/true\n")
        ops = FakeOps({
            "codex": ("", {"real.service": "[Service]\nExecStart=/bin/true\n",         # substantive
                           "doc.service": "[Unit]\nDocumentation=file:///new\n[Service]\nExecStart=/bin/true\n"}),
            "exocortex": ("sudo denied", None),                                          # unreachable
            "the-ancestor": ("", {}),                                                    # no units
            "suborch1": ("", {"real.service": "[Service]\nExecStart=/bin/true serve\n",  # clean
                              "doc.service": "[Unit]\nDocumentation=file:///new\n[Service]\nExecStart=/bin/true\n"}),
        })
        results = sweep.sweep_fleet(
            ["codex", "exocortex", "the-ancestor", "suborch1"], ops=ops, unit_dir=self.repo,
            manifest_path=None)  # drift-only; the expected-units half is tested below (T-4232)
        by = {r["account"]: r["status"] for r in results}
        self.assertEqual(by["codex"], sweep.DRIFT)
        self.assertEqual(by["exocortex"], sweep.UNREACHABLE)
        self.assertEqual(by["the-ancestor"], sweep.NO_UNITS)
        self.assertEqual(by["suborch1"], sweep.CLEAN)

        alerting = [r for r in results if r["status"] in sweep.ALERTING]
        self.assertEqual({r["account"] for r in alerting}, {"codex", "exocortex"})
        text = sweep._alert_text(alerting)
        self.assertIn("codex", text)
        self.assertIn("exocortex", text)
        self.assertNotIn("the-ancestor", text)   # NO_UNITS reported, not alerted
        self.assertNotIn("suborch1", text)        # CLEAN


class TestOpsReader(unittest.TestCase):
    """Ops.read_installed_dir mechanics — sudo-to-self carve-out and rc handling."""

    def _run_with_fake_subprocess(self, account, returncode, stdout, stderr=""):
        import subprocess as _sp

        class _R:
            pass

        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            r = _R()
            r.returncode = returncode
            r.stdout = stdout
            r.stderr = stderr
            return r

        orig = _sp.run
        _sp.run = fake_run
        try:
            reason, files = sweep.Ops().read_installed_dir(account)
        finally:
            _sp.run = orig
        return captured["argv"], reason, files

    def test_self_account_skips_sudo(self):
        me = sweep._me()
        argv, _, _ = self._run_with_fake_subprocess(me, 0, "")
        self.assertEqual(argv[0], "bash",
                         "sudo-to-self is a grant nobody made (trap a) — self must not be wrapped")

    def test_other_account_uses_sudo(self):
        me = sweep._me()
        other = "claude" if me != "claude" else "codex"
        argv, _, _ = self._run_with_fake_subprocess(other, 0, "")
        self.assertEqual(argv[:3], ["sudo", "-n", "-u"])
        self.assertEqual(argv[3], other)

    def test_nonzero_rc_with_empty_stdout_is_unverified_not_empty(self):
        # THE trap: a denied sudo returns rc!=0 and EMPTY stdout, byte-identical to a
        # clean empty read. Only the rc separates them.
        me = sweep._me()
        other = "claude" if me != "claude" else "codex"
        _, reason, files = self._run_with_fake_subprocess(other, 1, "", stderr="not permitted")
        self.assertIsNone(files, "non-zero rc must be UNVERIFIED (files=None), never clean-empty")
        self.assertIn("rc=1", reason)

    def test_nodir_is_reachable_empty_not_unreachable(self):
        me = sweep._me()
        other = "claude" if me != "claude" else "codex"
        _, reason, files = self._run_with_fake_subprocess(other, 0, sweep._NODIR)
        self.assertEqual(files, {}, "NODIR means asked-and-found-nothing, not could-not-ask")


class TestStreamParse(unittest.TestCase):
    def test_roundtrips_content_with_newlines_equals_and_hash(self):
        content_a = "[Service]\nExecStart=/bin/true --flag=1\n# a comment\nEnvironment=X=Y=Z\n"
        content_b = "[Timer]\nOnCalendar=*-*-* 04:00:00\n"
        stream = (f"{sweep._REC}a.service\0{content_a}"
                  f"{sweep._REC}b.timer\0{content_b}")
        files = sweep._parse_installed_stream(stream)
        self.assertEqual(files, {"a.service": content_a, "b.timer": content_b})

    def test_empty_stream_is_empty_dict(self):
        self.assertEqual(sweep._parse_installed_stream(""), {})


# ---------------------------------------------------------------------------
# T-4232 — expected-units manifest + MISSING_EXPECTED
# ---------------------------------------------------------------------------

_TIMER = "[Timer]\nOnCalendar=hourly\n[Install]\nWantedBy=timers.target\n"
_SVC = "[Service]\nType=oneshot\nExecStart=/bin/true\n"


class StateOps:
    """Plays Ops.read_installed_state: {account: (reason, files|None, wants|None)}."""

    def __init__(self, per_account: dict):
        self._map = per_account

    def read_installed_state(self, account: str):
        return self._map.get(account, ("unknown account", None, None))

    def read_installed_dir(self, account: str):
        reason, files, _ = self.read_installed_state(account)
        return reason, files


class _ManifestBase(_Base):
    def setUp(self) -> None:
        super().setUp()
        self._repo_unit("x.timer", _TIMER)
        self._repo_unit("x.service", _SVC)
        self._repo_unit("m@.timer", _TIMER)
        self._repo_unit("m@.service", _SVC)
        self.manifest = Path(self._tmp.name) / "expected-units.yaml"

    def _write_manifest(self, text: str) -> Path:
        self.manifest.write_text(text, encoding="utf-8")
        return self.manifest

    def _load(self, text: str):
        return sweep.load_expected_units(self._write_manifest(text), unit_dir=self.repo)


_GOOD = """
recovery:
  - {unit: x.timer, enabled: true}
  - {unit: x.service, enabled: false}
  - {unit: m@.service, enabled: false}
  - {unit: m@orchestrator.timer, enabled: true}
"""


class TestManifestLoad(_ManifestBase):
    def test_good_manifest_loads(self):
        m = self._load(_GOOD)
        self.assertEqual([e["unit"] for e in m["recovery"]],
                         ["x.timer", "x.service", "m@.service", "m@orchestrator.timer"])
        self.assertTrue(m["recovery"][0]["enabled"])

    def test_missing_file_fails_closed(self):
        with self.assertRaises(sweep.ManifestError):
            sweep.load_expected_units(Path(self._tmp.name) / "nope.yaml", unit_dir=self.repo)

    def test_bad_yaml_fails_closed(self):
        with self.assertRaises(sweep.ManifestError):
            self._load("recovery: [ {unit: x.timer, enabled: true\n")

    def test_empty_manifest_is_not_nothing_expected(self):
        with self.assertRaises(sweep.ManifestError):
            self._load("")

    def test_unknown_persona_rejected(self):
        with self.assertRaises(sweep.ManifestError) as cm:
            self._load("nosuchpersona:\n  - {unit: x.timer, enabled: true}\n")
        self.assertIn("nosuchpersona", str(cm.exception))

    def test_os_account_is_not_a_persona(self):
        # personas resolve via agent_role_map; an account name is not one.
        with self.assertRaises(sweep.ManifestError):
            self._load("claude-fallback:\n  - {unit: x.timer, enabled: true}\n")

    def test_unknown_unit_rejected(self):
        with self.assertRaises(sweep.ManifestError) as cm:
            self._load("recovery:\n  - {unit: gone.timer, enabled: true}\n")
        self.assertIn("gone.timer", str(cm.exception))

    def test_instance_without_template_rejected(self):
        with self.assertRaises(sweep.ManifestError):
            self._load("recovery:\n  - {unit: nope@orchestrator.timer, enabled: true}\n")

    def test_persona_account_outside_swept_fleet_rejected(self):
        # Review nit 2: an expectation on an account the sweep never iterates would never
        # be checked — fail closed (MANIFEST_UNKNOWN), not silently.
        orig = sweep.arm.fleet_accounts
        sweep.arm.fleet_accounts = lambda: tuple(a for a in orig() if a != "claude-fallback")
        try:
            with self.assertRaises(sweep.ManifestError) as cm:
                self._load("recovery:\n  - {unit: x.timer, enabled: true}\n")
            self.assertIn("not in the swept fleet", str(cm.exception))
            results = sweep.sweep_fleet([], ops=StateOps({}), unit_dir=self.repo,
                                        manifest_path=self.manifest)
            self.assertEqual([r["status"] for r in results], [sweep.MANIFEST_UNKNOWN])
        finally:
            sweep.arm.fleet_accounts = orig

    def test_bad_entry_shape_rejected(self):
        for bad in ("recovery:\n  - {unit: x.timer}\n",
                    "recovery:\n  - {unit: x.timer, enabled: yes-please}\n",
                    "recovery:\n  - {unit: x.timer, enabled: true, extra: 1}\n",
                    "recovery:\n  - {unit: x.timer, enabled: true}\n  - {unit: x.timer, enabled: true}\n",
                    "recovery: []\n",
                    "recovery:\n  - {unit: m@.timer, enabled: true}\n"):
            with self.subTest(bad=bad), self.assertRaises(sweep.ManifestError):
                self._load(bad)


class TestMissingExpected(_ManifestBase):
    def _run(self, files, wants, account="claude-fallback"):
        expected = self._load(_GOOD)
        ops = StateOps({account: ("", files, wants)})
        return sweep.sweep_account(account, ops=ops, unit_dir=self.repo, expected=expected)

    def _all_present(self):
        return {"x.timer": _TIMER, "x.service": _SVC, "m@.service": _SVC, "m@.timer": _TIMER}

    def test_present_and_enabled_is_clean(self):
        r = self._run(self._all_present(), {"x.timer", "m@orchestrator.timer"})
        self.assertEqual(r["status"], sweep.CLEAN)
        self.assertEqual(r["missing_expected"], [])

    def test_absent_unit_is_missing_expected(self):
        files = self._all_present()
        del files["x.timer"]
        r = self._run(files, {"x.timer", "m@orchestrator.timer"})
        self.assertEqual(r["status"], sweep.MISSING_EXPECTED)
        self.assertEqual([m["unit"] for m in r["missing_expected"]], ["x.timer"])
        self.assertTrue(r["missing_expected"][0]["detail"].startswith("absent"))

    def test_instance_absent_when_template_missing(self):
        files = self._all_present()
        del files["m@.timer"]
        r = self._run(files, {"x.timer", "m@orchestrator.timer"})
        self.assertEqual([m["unit"] for m in r["missing_expected"]], ["m@orchestrator.timer"])

    def test_disabled_unit_is_missing_expected(self):
        r = self._run(self._all_present(), {"m@orchestrator.timer"})  # x.timer not in any wants
        self.assertEqual(r["status"], sweep.MISSING_EXPECTED)
        self.assertEqual([m["unit"] for m in r["missing_expected"]], ["x.timer"])
        self.assertTrue(r["missing_expected"][0]["detail"].startswith("disabled"))

    def test_disabled_instance_is_missing_expected(self):
        r = self._run(self._all_present(), {"x.timer", "m@specialist.timer"})
        self.assertEqual([m["unit"] for m in r["missing_expected"]], ["m@orchestrator.timer"])

    def test_enabled_false_is_presence_only(self):
        # x.service is enabled:false and in no wants dir -> fine.
        r = self._run(self._all_present(), {"x.timer", "m@orchestrator.timer"})
        self.assertNotIn("x.service", [m["unit"] for m in r["missing_expected"]])

    def test_empty_seat_with_expectations_alerts_not_no_units(self):
        r = self._run({}, set())
        self.assertEqual(r["status"], sweep.MISSING_EXPECTED)
        self.assertEqual(len(r["missing_expected"]), 4)

    def test_unreachable_wins_over_missing(self):
        expected = self._load(_GOOD)
        ops = StateOps({"claude-fallback": ("sudo denied", None, None)})
        r = sweep.sweep_account("claude-fallback", ops=ops, unit_dir=self.repo,
                                expected=expected)
        self.assertEqual(r["status"], sweep.UNREACHABLE)
        self.assertEqual(r["missing_expected"], [])

    def test_persona_not_in_manifest_is_unaffected(self):
        expected = self._load(_GOOD)
        ops = StateOps({"codex": ("", {"x.timer": _TIMER}, set())})
        r = sweep.sweep_account("codex", ops=ops, unit_dir=self.repo, expected=expected)
        self.assertEqual(r["status"], sweep.CLEAN)

    def test_legacy_seam_without_wants_fails_closed(self):
        # A seam that only overrides read_installed_dir cannot report enablement, so an
        # enabled:true unit is flagged, never assumed enabled. And the real reader must
        # NOT be reached behind the fake (no subprocess).
        expected = self._load(_GOOD)
        ops = FakeOps({"claude-fallback": ("", self._all_present())})
        r = sweep.sweep_account("claude-fallback", ops=ops, unit_dir=self.repo,
                                expected=expected)
        self.assertEqual(r["status"], sweep.MISSING_EXPECTED)
        self.assertTrue(all("NOT verified enabled" in m["detail"]
                            for m in r["missing_expected"]))

    def test_legacy_subclass_seam_does_not_shell_out(self):
        test = self

        class Sub(sweep.Ops):
            def read_installed_dir(self, account):
                return "", test._all_present()

        import subprocess as _sp
        orig = _sp.run

        def boom(*a, **k):  # pragma: no cover - failure path
            raise AssertionError("real subprocess reached behind a fake seam")

        _sp.run = boom
        try:
            r = sweep.sweep_account("claude-fallback", ops=Sub(), unit_dir=self.repo,
                                    expected=self._load(_GOOD))
        finally:
            _sp.run = orig
        self.assertEqual(r["status"], sweep.MISSING_EXPECTED)


class TestRuntimeConditionalExpectation(_ManifestBase):
    def setUp(self) -> None:
        super().setUp()
        self._repo_unit("recovery-telegram-child-watchdog.timer", _TIMER)
        self.expected = self._load(
            "recovery:\n"
            "  - {unit: recovery-telegram-child-watchdog.timer, enabled: true}\n"
        )
        self.files = {"recovery-telegram-child-watchdog.timer": _TIMER}
        self.ops = StateOps({"claude-fallback": ("", self.files, set())})
        self.home = Path(self._tmp.name) / "home"

    def _context(self, runtime="codex/interactive-chatgpt", *, complete=False,
                 saved_enabled="enabled"):
        active = self.home / ".local/state/pantheon/personas/recovery/active-runtime.json"
        snapshot = (self.home /
                    ".config/codex-telegram/receipts/receiver-topology-snapshot.json")
        active.parent.mkdir(parents=True, exist_ok=True)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        active.write_text(json.dumps({"runtime": runtime}), encoding="utf-8")
        snapshot.write_text(json.dumps({
            "schema": "pantheon-receiver-topology-snapshot/v1",
            "persona": "recovery",
            "account": "claude-fallback",
            "complete": complete,
            "units": {"recovery-telegram-child-watchdog.timer": {
                "enabled": saved_enabled,
            }},
        }), encoding="utf-8")
        return sweep.RuntimeExpectations(home=self.home)

    def _run(self, context):
        return sweep.sweep_account(
            "claude-fallback", ops=self.ops, unit_dir=self.repo,
            expected=self.expected, runtime_expectations=context,
        )

    def test_codex_with_pending_snapshot_allows_disabled_watchdog(self):
        r = self._run(self._context())
        self.assertEqual(r["status"], sweep.CLEAN)
        self.assertEqual(r["missing_expected"], [])

    def test_claude_runtime_requires_watchdog_restored(self):
        r = self._run(self._context(runtime="claude/interactive-subscription"))
        self.assertEqual(r["status"], sweep.MISSING_EXPECTED)

    def test_completed_snapshot_requires_watchdog_restored(self):
        r = self._run(self._context(complete=True))
        self.assertEqual(r["status"], sweep.MISSING_EXPECTED)

    def test_snapshot_without_prior_enabled_state_fails_closed(self):
        r = self._run(self._context(saved_enabled="disabled"))
        self.assertEqual(r["status"], sweep.MISSING_EXPECTED)

    def test_missing_or_malformed_context_fails_closed(self):
        r = self._run(sweep.RuntimeExpectations(home=self.home))
        self.assertEqual(r["status"], sweep.MISSING_EXPECTED)


class TestExpectedAlertRouting(_ManifestBase):
    def test_missing_expected_alerts_and_text_names_units(self):
        self._write_manifest(_GOOD)
        ops = StateOps({
            "claude-fallback": ("", {"x.service": _SVC, "m@.service": _SVC,
                                     "m@.timer": _TIMER}, {"m@orchestrator.timer"}),
        })
        results = sweep.sweep_fleet(["claude-fallback"], ops=ops, unit_dir=self.repo,
                                    manifest_path=self.manifest)
        self.assertEqual(results[0]["status"], sweep.MISSING_EXPECTED)
        self.assertIn(sweep.MISSING_EXPECTED, sweep.ALERTING)
        text = sweep._alert_text([r for r in results if r["status"] in sweep.ALERTING])
        self.assertIn("MISSING_EXPECTED claude-fallback (recovery)", text)
        self.assertIn("x.timer [absent]", text)
        self.assertIn("1 account(s) need a look", text)

    def test_invalid_manifest_is_one_alerting_row(self):
        self._write_manifest("recovery:\n  - {unit: gone.timer, enabled: true}\n")
        body = "[Service]\nExecStart=/bin/true\n"
        self._repo_unit("a.service", body)
        ops = StateOps({"codex": ("", {"a.service": body}, set())})
        results = sweep.sweep_fleet(["codex"], ops=ops, unit_dir=self.repo,
                                    manifest_path=self.manifest)
        by = {r["account"]: r for r in results}
        self.assertEqual(by["codex"]["status"], sweep.CLEAN)  # drift half still ran
        row = by[sweep.MANIFEST_ROW_ACCOUNT]
        self.assertEqual(row["status"], sweep.MANIFEST_UNKNOWN)
        self.assertIn(sweep.MANIFEST_UNKNOWN, sweep.ALERTING)
        self.assertIn("gone.timer", sweep._alert_text([row]))
        # Review nit 3: the synthetic manifest row is not counted as an account.
        self.assertIn("0 account(s) + expected-units manifest invalid", sweep._alert_text([row]))

    def test_missing_manifest_file_is_alerting_row(self):
        results = sweep.sweep_fleet([], ops=StateOps({}), unit_dir=self.repo,
                                    manifest_path=Path(self._tmp.name) / "absent.yaml")
        self.assertEqual([r["status"] for r in results], [sweep.MANIFEST_UNKNOWN])

    def test_main_routes_via_existing_lane(self):
        # The new statuses reuse ROUTE_AGENT/route_failure_alert — no new route, no page.
        captured = {}
        import types, sys as _sys
        fake = types.ModuleType("inbox_consumer")

        def route_failure_alert(agent, text, vocab=None):
            captured.update(agent=agent, text=text, vocab=vocab)

        fake.route_failure_alert = route_failure_alert
        self._write_manifest(_GOOD)
        orig_mod = _sys.modules.get("inbox_consumer")
        orig_fleet = sweep.sweep_fleet
        _sys.modules["inbox_consumer"] = fake
        sweep.sweep_fleet = lambda accounts: orig_fleet(
            ["claude-fallback"], ops=StateOps({"claude-fallback": ("", {}, set())}),
            unit_dir=self.repo, manifest_path=self.manifest)
        try:
            self.assertEqual(sweep.main([]), 0)
        finally:
            sweep.sweep_fleet = orig_fleet
            if orig_mod is None:
                _sys.modules.pop("inbox_consumer", None)
            else:
                _sys.modules["inbox_consumer"] = orig_mod
        self.assertEqual(captured["agent"], sweep.ROUTE_AGENT)
        self.assertEqual(captured["vocab"], "systemd-drift-sweep")
        self.assertIn("MISSING_EXPECTED", captured["text"])


class TestWantsStream(unittest.TestCase):
    def test_w_records_never_become_unit_files(self):
        content = "[Timer]\nOnCalendar=hourly\n"
        stream = (f"{sweep._REC}a.timer\0{content}"
                  f"{sweep._WREC}timers.target.wants/a.timer\0"
                  f"{sweep._WREC}default.target.wants/b.service\0")
        files = sweep._parse_installed_stream(stream)
        self.assertEqual(files, {"a.timer": content})  # content not polluted by W
        self.assertEqual(sweep._parse_wants_stream(stream), {"a.timer", "b.service"})

    def test_w_only_stream(self):
        stream = f"{sweep._WREC}timers.target.wants/a.timer\0"
        self.assertEqual(sweep._parse_installed_stream(stream), {})
        self.assertEqual(sweep._parse_wants_stream(stream), {"a.timer"})


class TestReaderScriptAgainstRealDir(unittest.TestCase):
    """Runs the REAL reader script (local-account `bash -c` branch) against a fake $HOME:
    proves the shell text parses and frames records, not just the Python parser."""

    def setUp(self):
        import os
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        import os
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def _read(self):
        return sweep.Ops().read_installed_state(sweep._me())

    def test_unit_file_wants_symlink_and_subdir(self):
        d = self.home / ".config" / "systemd" / "user"
        (d / "timers.target.wants").mkdir(parents=True)
        (d / "a.service.d").mkdir()                       # drop-in subdir: not a unit
        (d / "a.service.d" / "x.conf").write_text("[Service]\n")
        body = "[Timer]\nOnCalendar=hourly\n# = and # survive\n"
        (d / "a.timer").write_text(body)
        (d / "a.service").write_text("[Service]\nExecStart=/bin/true\n")
        (d / "timers.target.wants" / "a.timer").symlink_to(d / "a.timer")
        (d / "timers.target.wants" / "m@orchestrator.timer").symlink_to(d / "missing@.timer")  # dangling
        reason, files, wants = self._read()
        self.assertEqual(reason, "")
        self.assertEqual(set(files), {"a.timer", "a.service"})
        self.assertEqual(files["a.timer"], body)
        self.assertEqual(wants, {"a.timer", "m@orchestrator.timer"})

    def test_no_wants_dir_gives_empty_wants(self):
        d = self.home / ".config" / "systemd" / "user"
        d.mkdir(parents=True)
        (d / "a.timer").write_text("[Timer]\n")
        reason, files, wants = self._read()
        self.assertEqual(set(files), {"a.timer"})
        self.assertEqual(wants, set())

    def test_nodir(self):
        reason, files, wants = self._read()
        self.assertEqual((files, wants), ({}, set()))

    def test_unreadable_unit_before_wants_dir_is_unverified(self):
        # Review nit 1: a mode-000 unit file followed (in glob order) by a *.wants dir must
        # not be masked by the trailing `continue` — the read is UNVERIFIED, not partial.
        import os
        if os.geteuid() == 0:
            self.skipTest("root reads mode-000 files")
        d = self.home / ".config" / "systemd" / "user"
        (d / "timers.target.wants").mkdir(parents=True)
        (d / "a.timer").write_text("[Timer]\n")
        (d / "timers.target.wants" / "a.timer").symlink_to(d / "a.timer")
        locked = d / "b.service"
        locked.write_text("[Service]\n")
        locked.chmod(0)
        try:
            reason, files, wants = self._read()
        finally:
            locked.chmod(0o644)
        self.assertIsNone(files, "a failed cat must surface as UNVERIFIED (files None)")
        self.assertIsNone(wants)
        self.assertIn("rc=", reason)

    def test_read_installed_dir_shape_unchanged(self):
        d = self.home / ".config" / "systemd" / "user"
        (d / "timers.target.wants").mkdir(parents=True)
        (d / "a.timer").write_text("[Timer]\n")
        (d / "timers.target.wants" / "a.timer").symlink_to(d / "a.timer")
        reason, files = sweep.Ops().read_installed_dir(sweep._me())
        self.assertEqual(files, {"a.timer": "[Timer]\n"})


class TestRepoManifestConsistency(unittest.TestCase):
    """D-6: the committed manifest must validate against the committed repo. Deleting a
    unit template (or a persona binding) without editing expected-units.yaml in the same
    change turns this red."""

    def test_committed_manifest_is_valid(self):
        m = sweep.load_expected_units()  # raises ManifestError on any defect
        self.assertIn("orchestrator", m)
        self.assertIn("recovery", m)

    def test_ho_units_on_orchestrator_seat(self):
        units = {e["unit"] for e in sweep.load_expected_units()["orchestrator"]}
        for u in ("pantheon-cascade-sweep.timer",
                  "pantheon-fleet-clone-currency.timer", "pantheon-echo-lint.timer",
                  "pantheon-ci-audit-tip-coverage.timer"):
            self.assertIn(u, units)
        # RETIRED per orchestrator ruling 2026-09-29 (T-4232 live dry-run).
        self.assertNotIn("pantheon-hermes-local-audit.timer", units)

    def test_retired_recovery_units_not_expected(self):
        units = {e["unit"] for e in sweep.load_expected_units()["recovery"]}
        self.assertNotIn("pantheon-ancestor-liveness.timer", units)  # T-2214
        self.assertNotIn("pantheon-activity-digest.timer", units)  # T-4289
        self.assertNotIn("pantheon-activity-digest.service", units)  # T-4289

    # Timers held presence-only on purpose, each with a TODO(recovery) in the manifest. Adding
    # one here is the explicit, reviewable step (anything else presence-only is a typo).
    _PRESENCE_ONLY_TIMERS: frozenset = frozenset()

    def test_every_timer_with_install_expected_enabled(self):
        # Timers are what make a seat's schedule run; one listed presence-only is a typo.
        for persona, entries in sweep.load_expected_units().items():
            for e in entries:
                if e["unit"].endswith(".timer") and not e["unit"].rpartition(".")[0].endswith("@"):
                    if (persona, e["unit"]) in self._PRESENCE_ONLY_TIMERS:
                        self.assertFalse(e["enabled"], f"{persona}: {e['unit']} (drop from allowlist)")
                        continue
                    self.assertTrue(e["enabled"], f"{persona}: {e['unit']}")


if __name__ == "__main__":
    unittest.main()
