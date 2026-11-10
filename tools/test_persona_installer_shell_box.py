"""T-5454: the persona installer wires shell-box containment into every Claude seat.

Intent: every Shell command, hook and MCP a Claude seat runs executes inside the
shell-box, so one runaway cannot exhaust RAM+swap and take down the fleet's user
managers (T-5222). The op sets ``env.CLAUDE_CODE_SHELL_PREFIX`` in the seat's own
``~/.claude/settings.json`` before the first Claude launch (T-5446).

Simulated backend / tmp roots / mocks only. NOTHING here touches a real
``~/.claude/settings.json``, a real user manager, a live seat, claude or codex.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tools import persona_installer as _pim
from tools import persona_installer_verify_seat as vs
from tools.host_control.envelope import EXECUTOR_KIND_CLASSES, OUT_OF_ENVELOPE_KINDS
from tools.persona_installer import (
    ExecutionContext,
    InstallOperation,
    LocalHostBackend,
    OperationFailed,
    SimulatedHostBackend,
)
from tools.test_persona_installer_periodics import PeriodicCase
from tools.test_persona_installer_services import legacy_ho_resumer_manifest

KIND = "seat-claude-settings-env"
OP_ID = "harness.claude-shell-box"
KEY = "CLAUDE_CODE_SHELL_PREFIX"


# ---------------------------------------------------------------------------
# plan shape
# ---------------------------------------------------------------------------
class PlanShapeTests(PeriodicCase):
    def test_claude_seat_gets_the_op_with_the_seat_home_shell_box_path(self) -> None:
        plan = self.plan("recovery")
        operation = self.op(plan, OP_ID)
        self.assertEqual(operation.kind, KIND)
        home = self.root / "projection" / "home" / "claude-fallback"
        self.assertEqual(operation.target, str(home / ".claude" / "settings.json"))
        self.assertEqual(operation.desired["env_key"], KEY)
        self.assertEqual(
            operation.desired["env_value"],
            str(home / "pantheon-config" / "tools" / "oom" / "pantheon-shell-box.sh"),
        )
        self.assertTrue(os.path.isabs(operation.desired["env_value"]))
        self.assertEqual(plan.provenance["shell_box_containment"], "applicable")

    def test_op_precedes_every_claude_launch_and_the_ho_resumer_decommission(self) -> None:
        self.set_manifest(legacy_ho_resumer_manifest())  # T-5633: the decommission is declared-only
        ids = self.ids(self.plan("recovery"))
        at = ids.index
        self.assertLess(at("host.user-manager"), at(OP_ID))
        self.assertLess(at(OP_ID), at("service.ho-resumer.decommission"))
        self.assertLess(at(OP_ID), at("harness.direct-fire"))
        self.assertLess(at(OP_ID), at("connective.unit-fire"))
        self.assertLess(at(OP_ID), at("service.runtime-controller.enable-observe"))

    def test_no_admitted_op_depends_on_it_and_it_is_not_an_activation_kind(self) -> None:
        plan = self.plan("recovery")
        for operation in plan.operations:
            if operation.kind not in EXECUTOR_KIND_CLASSES:
                continue
            for source in (operation.precondition, operation.desired):
                for dep in str(source.get("after", "")).split(","):
                    with self.subTest(op=operation.operation_id, dep=dep):
                        self.assertNotIn(dep.strip(), (OP_ID, KIND))
        # takes effect in the staged-ACTIVATE leg of a staged install (after the first
        # activation op, still before every launch op), not in the stage-prefix leg
        self.assertNotIn(KIND, _pim.ACTIVATION_OPERATION_KINDS)

    def test_kind_is_seat_local_residue_outside_the_envelope_and_partitions_cleanly(self) -> None:
        from tools.host_control.planner import SEAT_LOCAL_RESIDUE_LIMIT, partition_installer_plan

        self.assertIn(KIND, OUT_OF_ENVELOPE_KINDS)
        self.assertIn(KIND, _pim.SEAT_LOCAL_RESIDUE_KINDS)
        self.assertNotIn(KIND, EXECUTOR_KIND_CLASSES)
        broker, residue = partition_installer_plan(self.plan("recovery"))
        self.assertIn(OP_ID, {o.operation_id for o in residue})
        self.assertNotIn(OP_ID, {o.operation_id for o in broker.operations})
        # T-5470 (HO ruling 2026-10-26): routed launches have their own bound; B8 counts the rest
        # T-5961 (orchestrator ruling on B8): the fleet-usage periodic-timer ops are a separate bucket
        from tools.host_control.planner import residue_bucket_counts

        self.assertLessEqual(residue_bucket_counts(
            (o.kind, o.target) for o in residue if o.kind not in EXECUTOR_KIND_CLASSES)[0],
            SEAT_LOCAL_RESIDUE_LIMIT)

    def test_codex_only_seat_gets_no_op_and_a_not_applicable_plan_note(self) -> None:
        plan = self.plan("consultant")
        self.assertNotIn(OP_ID, self.ids(plan))
        self.assertNotIn(KIND, {o.kind for o in plan.operations})
        self.assertTrue(plan.provenance["shell_box_containment"].startswith("not-applicable"))

    def test_claude_primary_with_codex_fallback_counts_as_a_claude_seat(self) -> None:
        # mason shape: claude primary + codex fallback; the real manifest is the witness
        manifest = _pim.Manifest.load(Path(__file__).resolve().parent.parent / "instance.yaml")
        installer = _pim.PersonaInstaller(repo_root=Path(__file__).resolve().parent.parent)
        plan = installer.plan(manifest, "mason", root=self.root / "real")
        self.assertIn(OP_ID, self.ids(plan))
        for codex_only in ("watchman", "codex"):
            plan = installer.plan(manifest, codex_only, root=self.root / "real")
            self.assertNotIn(OP_ID, self.ids(plan))
            self.assertTrue(plan.provenance["shell_box_containment"].startswith("not-applicable"))

    def test_every_real_persona_stays_inside_the_residue_budget(self) -> None:
        from tools.host_control.planner import SEAT_LOCAL_RESIDUE_LIMIT, partition_installer_plan

        repo = Path(__file__).resolve().parent.parent
        manifest = _pim.Manifest.load(repo / "instance.yaml")
        installer = _pim.PersonaInstaller(repo_root=repo)
        personas = sorted({r["persona"] for r in manifest.roles.values()
                           if isinstance(r, dict) and r.get("persona")})
        for persona in personas:
            with self.subTest(persona=persona):
                plan = installer.plan(manifest, persona, root=self.root / "real")
                # T-5470 (HO ruling 2026-10-26): routed launches have their own bound; B8 counts the rest
                residue = partition_installer_plan(plan)[1]
                # T-5961 (orchestrator ruling on B8): the fleet-usage periodic-timer ops are a separate bucket
                from tools.host_control.planner import residue_bucket_counts

                self.assertLessEqual(residue_bucket_counts(
                    (o.kind, o.target) for o in residue if o.kind not in EXECUTOR_KIND_CLASSES)[0],
                    SEAT_LOCAL_RESIDUE_LIMIT)


# ---------------------------------------------------------------------------
# backend contract, run against BOTH backends
# ---------------------------------------------------------------------------
class SimDriver:
    TARGET = "/home/seat/.claude/settings.json"
    VALUE = "/home/seat/pantheon-config/tools/oom/pantheon-shell-box.sh"

    def setUp(self) -> None:
        self.backend = SimulatedHostBackend()
        self.target = self.TARGET
        self.value = self.VALUE
        self.op = make_op(self.target, self.value)

    def seed(self, text: str, mode: str = "0640") -> None:
        self.backend.seed_file(self.target, text, mode)

    def read(self) -> str | None:
        entry = self.backend.entries.get(self.target)
        if not entry:
            return None
        return base64.b64decode(entry["content_b64"]).decode("utf-8")

    def mode(self) -> str:
        return self.backend.entries[self.target]["mode"]

    def ctx(self) -> ExecutionContext:
        return ExecutionContext("all-in-one")


class LocalDriver:
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.backend = LocalHostBackend()
        self.target = str(self.tmp / ".claude" / "settings.json")
        script = self.tmp / "pantheon-config" / "tools" / "oom" / "pantheon-shell-box.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        self.value = str(script)
        self.op = make_op(self.target, self.value)

    def seed(self, text: str, mode: str = "0640") -> None:
        path = Path(self.target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(int(mode, 8))

    def read(self) -> str | None:
        path = Path(self.target)
        return path.read_text() if path.exists() else None

    def mode(self) -> str:
        return format(stat.S_IMODE(os.stat(self.target).st_mode), "04o")

    def ctx(self) -> ExecutionContext:
        return ExecutionContext("all-in-one", allowed_roots=(str(self.tmp),))


def make_op(target: str, value: str) -> InstallOperation:
    import pwd

    return InstallOperation(
        operation_id=OP_ID, layer=1, kind=KIND, target=target,
        precondition={"state": "must-pass"},
        desired={"env_key": KEY, "env_value": value},
        inverse=None, privilege="operator-required", owner=pwd.getpwuid(os.geteuid()).pw_name,
        provenance={"install_root": "/"},
    )


class ContractMixin:
    """Behaviour every backend must share."""

    def execute(self):
        return self.backend.execute(self.op, self.ctx())

    def test_absent_file_is_created_with_env_and_key_and_is_owned(self) -> None:
        self.assertTrue(self.backend.preflight(self.op).ok)
        mutation, evidence = self.execute()
        self.assertTrue(evidence.ok)
        self.assertEqual(json.loads(self.read()), {"env": {KEY: self.value}})
        self.assertTrue(mutation.changed)
        self.assertEqual(mutation.before["owned"], "true")
        self.assertEqual(self.mode(), "0600")

    def test_merge_preserves_other_keys_other_env_entries_and_order_and_mode(self) -> None:
        original = (
            '{\n  "zeta": 1,\n  "permissions": {"allow": ["Bash(ls)"]},\n'
            '  "env": {\n    "OTHER": "keep",\n    "AAA": "x"\n  },\n  "alpha": [1, 2]\n}\n'
        )
        self.seed(original, "0640")
        mutation, _ = self.execute()
        doc = json.loads(self.read())
        self.assertEqual(list(doc), ["zeta", "permissions", "env", "alpha"])
        self.assertEqual(list(doc["env"]), ["OTHER", "AAA", KEY])
        self.assertEqual(doc["env"]["OTHER"], "keep")
        self.assertEqual(doc["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(doc["alpha"], [1, 2])
        self.assertEqual(self.mode(), "0640")
        self.assertTrue(self.read().endswith("\n"))
        self.assertEqual(mutation.before["owned"], "true")

    def test_env_object_is_created_when_the_file_has_none(self) -> None:
        self.seed('{"model": "opus"}')
        self.execute()
        doc = json.loads(self.read())
        self.assertEqual(doc, {"model": "opus", "env": {KEY: self.value}})
        self.assertFalse(self.read().endswith("\n"))  # original had no trailing newline

    def test_identical_value_is_a_noop_byte_identical_and_not_owned(self) -> None:
        text = json.dumps({"env": {KEY: self.value}, "x": 1}, indent=4) + "\n"
        self.seed(text)
        self.assertEqual(self.backend.preflight(self.op).classification, "matching")
        mutation, evidence = self.execute()
        self.assertEqual(self.read(), text)
        self.assertFalse(mutation.changed)
        self.assertEqual(mutation.before["owned"], "false")
        self.assertTrue(evidence.ok)

    def test_conflicting_existing_value_is_refused_and_reported(self) -> None:
        text = json.dumps({"env": {KEY: "/somewhere/else.sh"}})
        self.seed(text)
        result = self.backend.preflight(self.op)
        self.assertFalse(result.ok)
        self.assertEqual(result.classification, "conflicting")
        self.assertIn("/somewhere/else.sh", result.detail)
        with self.assertRaises(OperationFailed):
            self.execute()
        self.assertEqual(self.read(), text)

    def test_unparseable_file_is_refused_and_untouched(self) -> None:
        self.seed("{not json")
        result = self.backend.preflight(self.op)
        self.assertFalse(result.ok)
        with self.assertRaises(OperationFailed):
            self.execute()
        self.assertEqual(self.read(), "{not json")

    def test_non_object_top_level_and_non_object_env_are_refused(self) -> None:
        for text in ("[]", '"s"', "3", "null", '{"env": []}', '{"env": "x"}'):
            with self.subTest(text=text):
                self.seed(text)
                self.assertFalse(self.backend.preflight(self.op).ok)
                with self.assertRaises(OperationFailed):
                    self.execute()
                self.assertEqual(self.read(), text)

    def test_duplicate_keys_are_refused_rather_than_silently_collapsed(self) -> None:
        text = '{"a": 1, "a": 2}'
        self.seed(text)
        self.assertFalse(self.backend.preflight(self.op).ok)
        self.assertEqual(self.read(), text)

    def test_rollback_deletes_only_the_key_and_leaves_env_in_place(self) -> None:
        self.seed('{"keep": true, "env": {"OTHER": "1"}}')
        mutation, _ = self.execute()
        self.assertEqual(self.backend.can_rollback(mutation).classification, "owned")
        self.backend.rollback_mutation(mutation, allowed_roots=self.rollback_roots())
        self.assertEqual(json.loads(self.read()), {"keep": True, "env": {"OTHER": "1"}})

    def test_rollback_keeps_an_emptied_env_object(self) -> None:
        mutation, _ = self.execute()  # file was absent, so env held only our key
        self.backend.rollback_mutation(mutation, allowed_roots=self.rollback_roots())
        self.assertEqual(json.loads(self.read()), {"env": {}})

    def test_rollback_of_a_non_owned_noop_leaves_the_key(self) -> None:
        text = json.dumps({"env": {KEY: self.value}})
        self.seed(text)
        mutation, _ = self.execute()
        self.assertEqual(self.backend.can_rollback(mutation).classification, "not-applied")
        self.backend.rollback_mutation(mutation, allowed_roots=self.rollback_roots())
        self.assertEqual(self.read(), text)

    def test_rollback_refuses_when_the_value_drifted_after_apply(self) -> None:
        mutation, _ = self.execute()
        self.seed(json.dumps({"env": {KEY: "/changed.sh"}}))
        check = self.backend.can_rollback(mutation)
        self.assertFalse(check.ok)
        self.assertEqual(check.classification, "drifted")

    def test_rollback_when_the_key_is_already_gone_is_not_applied(self) -> None:
        mutation, _ = self.execute()
        self.seed(json.dumps({"env": {}}))
        self.assertEqual(self.backend.can_rollback(mutation).classification, "not-applied")

    def test_verify_operation_reflects_the_key(self) -> None:
        self.assertFalse(self.backend.verify_operation(self.op).ok)
        self.execute()
        self.assertTrue(self.backend.verify_operation(self.op).ok)

    def test_preflight_is_read_only(self) -> None:
        self.seed('{"a": 1}')
        before = self.read()
        self.backend.preflight(self.op)
        self.assertEqual(self.read(), before)

    def rollback_roots(self):
        return ()


class SimulatedContractTests(ContractMixin, SimDriver, unittest.TestCase):
    pass


class LocalContractTests(ContractMixin, LocalDriver, unittest.TestCase):
    def rollback_roots(self):
        return (str(self.tmp),)

    def test_symlinked_settings_file_is_refused(self) -> None:
        real = self.tmp / "elsewhere.json"
        real.write_text("{}")
        Path(self.target).parent.mkdir(parents=True)
        os.symlink(real, self.target)
        self.assertFalse(self.backend.preflight(self.op).ok)
        with self.assertRaises(OperationFailed):
            self.execute()
        self.assertEqual(real.read_text(), "{}")

    def test_missing_prefix_target_is_refused_naming_the_path(self) -> None:
        os.unlink(self.value)
        result = self.backend.preflight(self.op)
        self.assertFalse(result.ok)
        self.assertIn(self.value, result.detail)
        with self.assertRaises(OperationFailed) as caught:
            self.execute()
        self.assertIn(self.value, str(caught.exception))
        self.assertIsNone(self.read())

    def test_non_executable_prefix_target_is_refused_naming_the_path(self) -> None:
        os.chmod(self.value, 0o644)
        result = self.backend.preflight(self.op)
        self.assertFalse(result.ok)
        self.assertIn(self.value, result.detail)

    def test_target_outside_allowed_roots_is_refused(self) -> None:
        ctx = ExecutionContext("all-in-one", allowed_roots=("/nonexistent-root",))
        with self.assertRaises(OperationFailed):
            self.backend.execute(self.op, ctx)
        self.assertIsNone(self.read())

    def test_no_temp_file_is_left_behind(self) -> None:
        self.seed("{}")
        self.execute()
        self.assertEqual(sorted(p.name for p in Path(self.target).parent.iterdir()), ["settings.json"])

    def test_atomic_write_preserves_the_original_owner_via_fchown(self) -> None:
        path = Path(self.target)
        path.parent.mkdir(parents=True)
        path.write_text("{}")
        real = os.lstat(path)
        fake = types.SimpleNamespace(
            st_mode=real.st_mode, st_uid=real.st_uid + 1, st_gid=real.st_gid + 1,
        )
        with mock.patch.object(_pim.os, "fchown") as fchown:
            _pim._settings_env_atomic_write(path, b'{"a": 1}', b"{}", fake)
        self.assertEqual(fchown.call_count, 1)
        self.assertEqual(fchown.call_args.args[1:], (fake.st_uid, fake.st_gid))
        self.assertEqual(path.read_bytes(), b'{"a": 1}')

    def test_concurrent_writer_between_read_and_rename_aborts_without_clobbering(self) -> None:
        path = Path(self.target)
        path.parent.mkdir(parents=True)
        path.write_text('{"mine": 0}')
        st = os.lstat(path)
        path.write_text('{"theirs": 1}')  # another tool wrote after we read
        with self.assertRaises(OperationFailed):
            _pim._settings_env_atomic_write(path, b'{"mine": 0, "env": {}}', b'{"mine": 0}', st)
        self.assertEqual(path.read_text(), '{"theirs": 1}')


# ---------------------------------------------------------------------------
# verify-seat containment section
# ---------------------------------------------------------------------------
class VerifySeatContainmentTests(PeriodicCase):
    def setUp(self) -> None:
        super().setUp()
        self.plan_ = self.plan("recovery")
        self.operation = self.op(self.plan_, OP_ID)
        self.settings = Path(self.operation.target)
        self.script = Path(self.operation.desired["env_value"])
        self.home = self.settings.parent.parent
        self.xdg = self.root / "xdg"
        self.fallback = self.home / ".local" / "state" / "pantheon" / "shell-box-fallback.log"
        self.good()

    def good(self) -> None:
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps({"env": {KEY: str(self.script)}}))
        self.script.parent.mkdir(parents=True, exist_ok=True)
        self.script.write_text("#!/bin/sh\n")
        self.script.chmod(0o755)
        (self.xdg / "systemd").mkdir(parents=True, exist_ok=True)
        (self.xdg / "systemd" / "private").write_text("")

    def run_checks(self, *, env=None, which=None):
        env = {"XDG_RUNTIME_DIR": str(self.xdg)} if env is None else env
        which = (lambda name: "/usr/bin/systemd-run") if which is None else which
        return vs.containment_checks(self.plan_, env=env, which=which)

    def by_name(self, checks):
        return {c["name"]: c for c in checks}

    def test_all_pass_when_wired(self) -> None:
        checks = self.run_checks()
        self.assertEqual({c["section"] for c in checks}, {"containment"})
        self.assertEqual(set(self.by_name(checks)),
                         {"settings-key", "prefix-executable", "user-manager", "fallback-log"})
        self.assertTrue(all(c["ok"] for c in checks), checks)

    def test_key_missing_file_or_env_or_key_fails(self) -> None:
        for content in (None, "{}", '{"env": {}}', "not json", "[]"):
            with self.subTest(content=content):
                if content is None:
                    self.settings.unlink()
                else:
                    self.settings.write_text(content)
                self.assertFalse(self.by_name(self.run_checks())["settings-key"]["ok"])
        self.good()

    def test_key_with_a_different_or_relative_value_fails(self) -> None:
        for value in ("/other/path.sh", "pantheon-shell-box.sh", ""):
            with self.subTest(value=value):
                self.settings.write_text(json.dumps({"env": {KEY: value}}))
                self.assertFalse(self.by_name(self.run_checks())["settings-key"]["ok"])

    def test_prefix_target_missing_or_not_executable_fails(self) -> None:
        self.script.chmod(0o644)
        self.assertFalse(self.by_name(self.run_checks())["prefix-executable"]["ok"])
        self.script.unlink()
        self.assertFalse(self.by_name(self.run_checks())["prefix-executable"]["ok"])

    def test_user_manager_each_requirement_is_checked(self) -> None:
        self.assertFalse(self.by_name(self.run_checks(env={}))["user-manager"]["ok"])
        self.assertFalse(self.by_name(self.run_checks(env={"XDG_RUNTIME_DIR": str(self.root / "none")}))["user-manager"]["ok"])
        self.assertFalse(self.by_name(self.run_checks(which=lambda name: None))["user-manager"]["ok"])

    def test_user_manager_fails_when_xdg_runtime_dir_is_unset(self) -> None:
        check = self.by_name(self.run_checks(env={}))["user-manager"]
        self.assertFalse(check["ok"])
        self.assertIn("XDG_RUNTIME_DIR unset", check["detail"])

    def test_user_manager_fails_when_the_private_socket_path_is_missing(self) -> None:
        (self.xdg / "systemd" / "private").unlink()
        check = self.by_name(self.run_checks())["user-manager"]
        self.assertFalse(check["ok"])
        self.assertIn("systemd/private absent", check["detail"])

    def test_user_manager_fails_when_systemd_run_is_not_found(self) -> None:
        check = self.by_name(self.run_checks(which=lambda name: None))["user-manager"]
        self.assertFalse(check["ok"])
        self.assertIn("systemd-run not on PATH", check["detail"])

    def test_check_does_not_read_the_host_environment_when_seams_are_injected(self) -> None:
        # T-5470: containment_checks binds ``which=shutil.which`` at def time, so
        # patching shutil.which never reached it (a dead patch). Inject a recording
        # ``which`` instead and prove it is the lookup the check consults; the
        # host-default lookup is swapped for one that fails loudly if it is used.
        asked: list[str] = []

        def injected_which(name: str) -> str:
            asked.append(name)
            return "/usr/bin/systemd-run"

        host_default = mock.patch.dict(
            vs.containment_checks.__kwdefaults__,
            {"which": mock.Mock(side_effect=AssertionError("read host PATH"))},
        )
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/nonexistent-host-xdg"}), host_default:
            self.assertTrue(self.by_name(self.run_checks(which=injected_which))["user-manager"]["ok"])
        self.assertEqual(asked, ["systemd-run"])

    def test_fallback_log_absent_or_empty_passes_and_non_empty_fails(self) -> None:
        self.assertTrue(self.by_name(self.run_checks())["fallback-log"]["ok"])
        self.fallback.parent.mkdir(parents=True)
        self.fallback.write_text("")
        self.assertTrue(self.by_name(self.run_checks())["fallback-log"]["ok"])
        self.fallback.write_text("shell-box: no user manager, running unboxed\n")
        self.assertFalse(self.by_name(self.run_checks())["fallback-log"]["ok"])

    def test_seat_with_no_containment_op_reports_not_applicable_and_passes(self) -> None:
        # T-5480: codex-only seats are no longer "not applicable" (they carry the codex hook op);
        # only a plan with NEITHER op is.
        import dataclasses

        plan = self.plan("consultant")
        stripped = dataclasses.replace(
            plan,
            operations=tuple(o for o in plan.operations
                             if o.kind not in (_pim.SETTINGS_ENV_KIND, _pim.CODEX_HOOK_KIND)),
            provenance={**plan.provenance,
                        "shell_box_containment": "not-applicable: no claude runtime candidate"},
        )
        checks = vs.containment_checks(stripped, env={}, which=lambda name: None)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["name"], "containment-applicable")
        self.assertTrue(checks[0]["ok"])
        self.assertIn("not-applicable", checks[0]["detail"])

    def test_section_is_registered_and_the_check_is_read_only(self) -> None:
        self.assertIn("containment", vs.SECTIONS)
        before = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        self.run_checks()
        after = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
