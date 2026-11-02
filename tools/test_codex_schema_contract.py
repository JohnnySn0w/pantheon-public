"""ORG-103 L5 (T-5305): the codex pin + schema contract gate.

CI runs these on every PR. A pin bump in harness/codex/appserver_seats.json fails here
unless a regenerated snapshot for the new version is committed and passes the contract.

Run: python3 -m unittest tools.test_codex_schema_contract
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import codex_schema_contract as csc  # noqa: E402

PINNED = sorted({b["codex_version"] for b in csc.load_bindings().values()})


def _snapshot(v=None):
    return csc.load_snapshot(v or PINNED[0])


def _union_drop(doc, union, method):
    entries = doc["definitions"][union]["oneOf"]
    doc["definitions"][union]["oneOf"] = [
        e for e in entries if (e.get("properties", {}).get("method", {}).get("enum") or [None])[0] != method]


class PinGateTests(unittest.TestCase):
    def test_every_pinned_version_has_a_passing_snapshot(self):
        results = csc.check_pins(csc.load_bindings())
        self.assertEqual(set(results), set(PINNED))
        for v, fails in results.items():
            self.assertEqual(fails, [], f"codex {v} contract failures:\n" + "\n".join(fails))

    def test_a_bump_without_a_snapshot_fails(self):
        b = copy.deepcopy(csc.load_bindings())
        next(iter(b.values()))["codex_version"] = "0.999.0"
        fails = csc.check_pins(b)["0.999.0"]
        self.assertTrue(fails and "no usable snapshot" in fails[0])

    def test_a_snapshot_filed_under_the_wrong_version_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / f"codex-{PINNED[0]}"
            shutil.copytree(csc.snapshot_dir(PINNED[0]), d)
            (d / "VERSION").write_text("0.1.0\n")
            fails = csc.check_pins(csc.load_bindings(), root=Path(tmp))[PINNED[0]]
        self.assertTrue(fails and "VERSION" in fails[0])

    def test_bindings_carry_the_fields_the_seat_path_needs(self):
        for name, b in csc.load_bindings().items():
            for key in ("account", "codex_version", "codex_bin", "cwd", "model", "sandbox",
                        "approval_policy", "network_access", "writable_roots"):
                self.assertIn(key, b, f"{name} binding lacks {key}")
            cwd = csc.resolve_binding_cwd(b, instance_root=Path("/srv/pantheon"))
            csc.cd.SeatPolicy(b["sandbox"], b["approval_policy"], b["model"], cwd)

    def test_instance_root_sentinel_resolves_to_the_active_checkout(self):
        self.assertEqual(
            csc.resolve_binding_cwd({"cwd": "@instance-root"}, instance_root=Path("/tmp/check")),
            "/tmp/check",
        )

    def test_other_relative_binding_cwd_is_refused(self):
        with self.assertRaises(ValueError):
            csc.resolve_binding_cwd({"cwd": "relative/path"}, instance_root=Path("/tmp/check"))

    def test_live_regeneration_matches_the_snapshot_when_the_pinned_codex_is_here(self):
        for name, b in csc.load_bindings().items():
            have = csc.codex_version(b["codex_bin"]) if Path(b["codex_bin"]).exists() else None
            if have != b["codex_version"]:
                self.skipTest(f"{b['codex_bin']} is {have!r}, not the pinned {b['codex_version']}")
            with tempfile.TemporaryDirectory() as tmp:
                dest = csc.regen(b["codex_bin"], out_root=Path(tmp))
                self.assertEqual((dest / csc.BUNDLE).read_bytes(),
                                 (csc.snapshot_dir(have) / csc.BUNDLE).read_bytes(),
                                 f"codex {have} regenerates a different schema than the snapshot")


class ContractCatchesDriftTests(unittest.TestCase):
    """Each mutation is a plausible upstream change; the contract must flag it."""

    def fails(self, mutate):
        doc = copy.deepcopy(_snapshot())
        mutate(doc)
        return csc.check(doc)

    def assertFlags(self, mutate, needle):
        fails = self.fails(mutate)
        self.assertTrue(any(needle in f for f in fails), f"{needle!r} not in {fails}")

    def test_baseline_passes(self):
        self.assertEqual(csc.check(copy.deepcopy(_snapshot())), [])

    def test_removed_client_method(self):
        self.assertFlags(lambda d: _union_drop(d, "ClientRequest", "turn/steer"), "'turn/steer' missing")

    def test_removed_presence_only_method(self):
        self.assertFlags(lambda d: _union_drop(d, "ClientRequest", "thread/start"), "'thread/start' missing")

    def test_removed_notification(self):
        self.assertFlags(lambda d: _union_drop(d, "ServerNotification", "turn/completed"),
                         "'turn/completed' missing")

    def test_param_the_client_sends_becomes_invalid(self):
        def m(d):
            d["definitions"]["v2"]["TurnSteerParams"]["required"].append("clientTurnToken")
        self.assertFlags(m, "turn/steer params")

    def test_sandbox_wire_name_changes(self):
        def m(d):
            sp = d["definitions"]["v2"]["SandboxPolicy"]
            for br in sp["oneOf"]:
                t = br["properties"]["type"]
                if t.get("enum") == ["dangerFullAccess"]:
                    t["enum"] = ["fullAccess"]
        self.assertFlags(m, "turn/start params")

    def test_result_field_the_client_reads_is_renamed(self):
        def m(d):
            props = d["definitions"]["v2"]["TurnSteerResponse"]["properties"]
            props["turn_id"] = props.pop("turnId")
        self.assertFlags(m, "turn/steer result: no field turnId")

    def test_turn_status_value_removed(self):
        def m(d):
            d["definitions"]["v2"]["TurnStatus"]["enum"].remove("interrupted")
        self.assertFlags(m, "TurnStatus: missing values ['interrupted']")

    def test_refusal_shape_no_longer_valid(self):
        def m(d):
            node = d["definitions"]["CommandExecutionApprovalDecision"]
            node["oneOf"] = [b for b in node["oneOf"] if b.get("enum") != ["decline"]]
        self.assertFlags(m, "item/commandExecution/requestApproval refusal")

    def test_new_approval_request_without_a_deny_answer(self):
        def m(d):
            entry = copy.deepcopy(d["definitions"]["ServerRequest"]["oneOf"][0])
            entry["properties"]["method"]["enum"] = ["item/network/requestApproval"]
            d["definitions"]["ServerRequest"]["oneOf"].append(entry)
        self.assertFlags(m, "'item/network/requestApproval' is new")

    def test_unauthorized_error_info_removed(self):
        def m(d):
            for br in d["definitions"]["v2"]["CodexErrorInfo"]["oneOf"]:
                if "enum" in br and "unauthorized" in br["enum"]:
                    br["enum"].remove("unauthorized")
        self.assertFlags(m, "'unauthorized' missing")

    def test_context_compaction_item_removed(self):
        def m(d):
            ti = d["definitions"]["v2"]["ThreadItem"]
            ti["oneOf"] = [b for b in ti["oneOf"]
                           if b["properties"]["type"].get("enum") != ["contextCompaction"]]
        self.assertFlags(m, "ThreadItem: missing types ['contextCompaction']")


class RegenAndInstalledTests(unittest.TestCase):
    def fake_run(self, version_out, schema_doc=None, rc=0):
        def run(argv, **kw):
            if argv[-1] == "--version":
                return subprocess.CompletedProcess(argv, 0, version_out, "")
            out = Path(argv[argv.index("--out") + 1])
            (out / csc.BUNDLE).write_text(json.dumps(schema_doc or {"definitions": {}}))
            return subprocess.CompletedProcess(argv, rc, "", "boom" if rc else "")
        return run

    def test_regen_files_the_snapshot_under_the_binarys_own_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = csc.regen("/x/codex", out_root=Path(tmp), run=self.fake_run("codex-cli 0.150.1\n"))
            self.assertEqual(dest.name, "codex-0.150.1")
            self.assertEqual((dest / "VERSION").read_text().strip(), "0.150.1")

    def test_regen_refuses_an_unreadable_version(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
            csc.regen("/x/codex", out_root=Path(tmp), run=self.fake_run("something else"))

    def test_regen_refuses_a_failed_generate(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
            csc.regen("/x/codex", out_root=Path(tmp), run=self.fake_run("codex-cli 0.150.1", rc=1))

    def test_installed_check(self):
        b = {"codex_bin": "/x/codex", "codex_version": "0.147.0"}
        self.assertIsNone(csc.check_installed(b, run=self.fake_run("codex-cli 0.147.0")))
        self.assertIn("pins '0.147.0'", csc.check_installed(b, run=self.fake_run("codex-cli 0.148.0")))


class SupervisorPinTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(csc.REPO / "harness" / "codex"))
        import appserver_supervise as sup
        self.sup = sup
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        self.args = sup.parse_args(["--rundir", str(d / "rd"), "--state-dir", str(d / "st"),
                                    "--codex-home", str(d / "h"), "--model", "m", "--poll", "0",
                                    "--account", "codex"])
        (d / "h").mkdir()
        (d / "h" / "config.toml").write_text(
            'check_for_update_on_startup = false\n[notice]\nhide_rate_limit_model_nudge = true\n'
            'hide_full_access_warning = true\n')

    def supervisor(self, version_out):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, version_out, "")
        s = self.sup.Supervisor(self.args, run=run, popen=lambda *a, **k: self.fail("started"))
        return s, calls

    def test_wrong_codex_refuses_before_anything_starts(self):
        s, _ = self.supervisor("codex-cli 0.148.0")
        self.assertEqual(s.run(), self.sup.EXIT_REFUSED)
        self.assertFalse(Path(self.args.rundir).exists())

    def test_pinned_codex_passes_the_check(self):
        s, calls = self.supervisor("codex-cli " + csc.load_bindings()["consultant"]["codex_version"])
        self.assertIsNone(s.check_pin())
        self.assertEqual(calls[0][-1], "--version")

    def test_unbound_account_is_not_checked(self):
        self.args.account = "nobody-bound"
        s, calls = self.supervisor("codex-cli 0.1.0")
        self.assertIsNone(s.check_pin())
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
