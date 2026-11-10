"""T-5480: the persona installer wires shell-box containment into every seat with a codex runtime.

Intent: every Bash command a codex runtime (codex-only seat, or a claude seat's codex fallback)
runs executes inside the shell-box, so a fallback seat cannot OOM the host (ORG-104, T-5222). The
op ``harness.codex-shell-box`` adds a PreToolUse hook block + its codex trust hash to the seat's
``~/.codex/config.toml`` in one atomic write.

Simulated backend / tmp roots / stubbed codex app-server only. NOTHING here touches a real
~/.codex, a live seat, or codex itself.
"""
from __future__ import annotations

import base64
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path

from tools import persona_installer as _pim
from tools.host_control.envelope import (
    EXECUTOR_KIND_CLASSES,
    MUST_RUN_BEFORE_LAUNCH_KINDS,
    OUT_OF_ENVELOPE_KINDS,
)
from tools.persona_installer import (
    ExecutionContext,
    InstallOperation,
    LocalHostBackend,
    OperationFailed,
    SimulatedHostBackend,
)
from tools.test_codex_shell_box import WORKER_GUARD, fake_read_hooks
from tools.test_persona_installer_periodics import PeriodicCase

KIND = "seat-codex-hook-config"
OP_ID = "harness.codex-shell-box"


REPO = Path(__file__).resolve().parent.parent


class PlanShapeTests(PeriodicCase):
    def real_plan(self, persona: str):
        manifest = _pim.Manifest.load(REPO / "instance.yaml")
        return _pim.PersonaInstaller(repo_root=REPO).plan(manifest, persona, root=self.root / "real")

    def test_claude_seat_with_codex_fallback_gets_the_op(self) -> None:
        plan = self.real_plan("mason")  # claude primary + codex fallback
        operation = self.op(plan, OP_ID)
        home = self.root / "real" / "home" / plan.account
        self.assertEqual(operation.kind, KIND)
        self.assertEqual(operation.target, str(home / ".codex" / "config.toml"))
        self.assertEqual(operation.desired["account_home"], str(home))
        self.assertEqual(
            operation.desired["hook_command"],
            f"/usr/bin/python3 -I {home}/pantheon-config/tools/oom/pantheon-codex-shell-box-hook.py",
        )
        self.assertEqual(operation.desired["codex_executable"], "/usr/bin/codex")
        self.assertEqual(plan.provenance["codex_shell_box_containment"], "applicable")

    def test_codex_only_seat_gets_the_op_and_no_claude_op(self) -> None:
        plan = self.real_plan("watchman")
        self.assertIn(OP_ID, self.ids(plan))
        self.assertNotIn("harness.claude-shell-box", self.ids(plan))

    def test_codex_plan_routing_is_unchanged_by_the_op(self) -> None:
        # HO ruling (T-5480 / T-5608): the op is plain seat-local residue; a codex plan's
        # launches stay broker-executed, so the first codex session after install is unboxed
        # until the runtime restarts (documented at the op's backend docstring).
        from tools.host_control.planner import launch_routed_operation_ids

        plan = self.real_plan("watchman")
        self.assertIn(OP_ID, self.ids(plan))
        self.assertEqual(launch_routed_operation_ids(plan), frozenset())

    def test_a_seat_without_a_codex_candidate_is_not_applicable(self) -> None:
        plan = self.plan("recovery")  # fixture manifest: claude only
        self.assertNotIn(OP_ID, self.ids(plan))
        self.assertTrue(plan.provenance["codex_shell_box_containment"].startswith("not-applicable"))

    def test_kind_registries(self) -> None:
        self.assertIn(KIND, OUT_OF_ENVELOPE_KINDS)
        self.assertIn(KIND, _pim.SEAT_LOCAL_RESIDUE_KINDS)
        self.assertNotIn(KIND, EXECUTOR_KIND_CLASSES)
        # T-5480 ruling: NOT must-run-before-launch (keeps T-5608 broker-launch routing)
        self.assertNotIn(KIND, MUST_RUN_BEFORE_LAUNCH_KINDS)
        self.assertNotIn(KIND, _pim.ACTIVATION_OPERATION_KINDS)

    def test_real_manifest_applicability_follows_the_codex_candidate(self) -> None:
        repo = Path(__file__).resolve().parent.parent
        manifest = _pim.Manifest.load(repo / "instance.yaml")
        installer = _pim.PersonaInstaller(repo_root=repo)
        personas = sorted({r["persona"] for r in manifest.roles.values()
                           if isinstance(r, dict) and r.get("persona")})
        for persona in personas:
            with self.subTest(persona=persona):
                plan = installer.plan(manifest, persona, root=self.root / "real")
                has_op = OP_ID in self.ids(plan)
                self.assertEqual(has_op, plan.provenance["codex_shell_box_containment"] == "applicable")
                if persona in ("codex", "watchman"):
                    self.assertTrue(has_op)  # codex-only seats were not-applicable before this change
                if not has_op:
                    self.assertTrue(plan.provenance["codex_shell_box_containment"].startswith("not-applicable"))


def make_op(target: str, account_home: str) -> InstallOperation:
    import pwd

    return InstallOperation(
        operation_id=OP_ID, layer=1, kind=KIND, target=target,
        precondition={"state": "must-pass"},
        desired={
            "hook_command": f"/usr/bin/python3 -I {account_home}/pantheon-config/tools/oom/pantheon-codex-shell-box-hook.py",
            "account_home": account_home,
            "codex_executable": "/nonexistent/codex",
        },
        inverse=None, privilege="operator-required", owner=pwd.getpwuid(os.geteuid()).pw_name,
        provenance={"install_root": "/"},
    )


class SimDriver:
    HOME = "/home/seat"

    def setUp(self) -> None:
        self.backend = SimulatedHostBackend()
        self.target = f"{self.HOME}/.codex/config.toml"
        self.op = make_op(self.target, self.HOME)

    def seed(self, text: str, mode: str = "0640") -> None:
        self.backend.seed_file(self.target, text, mode)

    def read(self):
        entry = self.backend.entries.get(self.target)
        return base64.b64decode(entry["content_b64"]).decode() if entry else None

    def mode(self) -> str:
        return self.backend.entries[self.target]["mode"]

    def ctx(self) -> ExecutionContext:
        return ExecutionContext("all-in-one")

    def rollback_roots(self):
        return ()


class LocalDriver:
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.backend = LocalHostBackend()
        self.backend.codex_hooks_reader = fake_read_hooks
        oom = self.tmp / "pantheon-config" / "tools" / "oom"
        oom.mkdir(parents=True)
        for name in ("pantheon-codex-shell-box-hook.py", "pantheon-shell-box.sh"):
            (oom / name).write_text("#!/bin/sh\n")
            (oom / name).chmod(0o755)
        self.target = str(self.tmp / ".codex" / "config.toml")
        self.op = make_op(self.target, str(self.tmp))

    def seed(self, text: str, mode: str = "0640") -> None:
        path = Path(self.target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(int(mode, 8))

    def read(self):
        path = Path(self.target)
        return path.read_text() if path.exists() else None

    def mode(self) -> str:
        return format(stat.S_IMODE(os.stat(self.target).st_mode), "04o")

    def ctx(self) -> ExecutionContext:
        return ExecutionContext("all-in-one", allowed_roots=(str(self.tmp),))

    def rollback_roots(self):
        return (str(self.tmp),)


class ContractMixin:
    def execute(self):
        return self.backend.execute(self.op, self.ctx())

    def hooks(self):
        return (tomllib.loads(self.read()).get("hooks") or {})

    def test_absent_file_is_created_with_block_and_trust(self) -> None:
        pre = self.backend.preflight(self.op)
        self.assertTrue(pre.ok)
        self.assertEqual(pre.classification, "absent")
        mutation, evidence = self.execute()
        self.assertTrue(evidence.ok)
        self.assertTrue(mutation.changed)
        self.assertEqual(mutation.before["owned"], "true")
        [group] = self.hooks()["PreToolUse"]
        self.assertEqual(group["matcher"], "Bash")
        self.assertEqual(len(self.hooks()["state"]), 1)
        self.assertEqual(self.mode(), "0600")
        self.assertTrue(self.backend.verify_operation(self.op).ok)

    def test_worker_guard_and_other_tables_survive_and_mode_is_kept(self) -> None:
        self.seed(WORKER_GUARD, "0640")
        self.execute()
        text = self.read()
        self.assertTrue(text.startswith(WORKER_GUARD.rstrip("\n")))
        self.assertEqual(self.hooks()["state"]["KEY0"], {"trusted_hash": "sha256:guard"})
        self.assertEqual(tomllib.loads(text)["projects"]["/h"], {"trust_level": "trusted"})
        self.assertEqual(self.mode(), "0640")

    def test_reapply_is_a_byte_identical_noop_and_not_owned(self) -> None:
        self.execute()
        wired = self.read()
        self.assertEqual(self.backend.preflight(self.op).classification, "matching")
        mutation, evidence = self.execute()
        self.assertEqual(self.read(), wired)
        self.assertFalse(mutation.changed)
        self.assertEqual(mutation.before["owned"], "false")
        self.assertTrue(evidence.ok)

    def test_edited_block_is_conflicting_and_untouched(self) -> None:
        self.execute()
        edited = self.read().replace('matcher = "Bash"', 'matcher = "Bash|apply_patch"')
        self.seed(edited)
        result = self.backend.preflight(self.op)
        self.assertFalse(result.ok)
        self.assertEqual(result.classification, "conflicting")
        with self.assertRaises(OperationFailed):
            self.execute()
        self.assertEqual(self.read(), edited)

    def test_unparseable_toml_is_refused_and_untouched(self) -> None:
        self.seed("= = nope")
        self.assertFalse(self.backend.preflight(self.op).ok)
        with self.assertRaises(OperationFailed):
            self.execute()
        self.assertEqual(self.read(), "= = nope")

    def test_rollback_removes_only_our_block_and_trust(self) -> None:
        self.seed(WORKER_GUARD)
        mutation, _ = self.execute()
        self.assertEqual(self.backend.can_rollback(mutation).classification, "owned")
        self.backend.rollback_mutation(mutation, allowed_roots=self.rollback_roots())
        self.assertEqual(tomllib.loads(self.read()), tomllib.loads(WORKER_GUARD))
        self.assertFalse(self.backend.verify_operation(self.op).ok)

    def test_rollback_of_a_fresh_file_leaves_an_empty_document(self) -> None:
        mutation, _ = self.execute()
        self.backend.rollback_mutation(mutation, allowed_roots=self.rollback_roots())
        self.assertEqual(tomllib.loads(self.read()), {})

    def test_rollback_of_a_non_owned_noop_leaves_the_block(self) -> None:
        self.execute()
        wired = self.read()
        mutation, _ = self.execute()
        self.assertEqual(self.backend.can_rollback(mutation).classification, "not-applied")
        self.backend.rollback_mutation(mutation, allowed_roots=self.rollback_roots())
        self.assertEqual(self.read(), wired)

    def test_rollback_refuses_when_the_block_drifted_after_apply(self) -> None:
        mutation, _ = self.execute()
        self.seed(self.read().replace("timeout = 10", "timeout = 11"))
        check = self.backend.can_rollback(mutation)
        self.assertFalse(check.ok)
        self.assertEqual(check.classification, "drifted")

    def test_rollback_when_already_gone_is_not_applied(self) -> None:
        mutation, _ = self.execute()
        self.seed("")
        self.assertEqual(self.backend.can_rollback(mutation).classification, "not-applied")

    def test_verify_before_apply_is_false(self) -> None:
        self.assertFalse(self.backend.verify_operation(self.op).ok)

    def test_preflight_is_read_only(self) -> None:
        self.seed('model = "x"\n')
        before = self.read()
        self.backend.preflight(self.op)
        self.assertEqual(self.read(), before)


class SimulatedContractTests(ContractMixin, SimDriver, unittest.TestCase):
    pass


class LocalContractTests(ContractMixin, LocalDriver, unittest.TestCase):
    def test_missing_hook_script_is_refused_naming_the_path(self) -> None:
        script = self.tmp / "pantheon-config/tools/oom/pantheon-codex-shell-box-hook.py"
        script.unlink()
        result = self.backend.preflight(self.op)
        self.assertFalse(result.ok)
        self.assertIn(str(script), result.detail)
        with self.assertRaises(OperationFailed):
            self.execute()
        self.assertIsNone(self.read())

    def test_non_executable_box_is_refused(self) -> None:
        (self.tmp / "pantheon-config/tools/oom/pantheon-shell-box.sh").chmod(0o644)
        self.assertFalse(self.backend.preflight(self.op).ok)

    def test_symlinked_config_is_refused(self) -> None:
        real = self.tmp / "elsewhere.toml"
        real.write_text("")
        Path(self.target).parent.mkdir(parents=True)
        os.symlink(real, self.target)
        self.assertFalse(self.backend.preflight(self.op).ok)
        with self.assertRaises(OperationFailed):
            self.execute()

    def test_unreadable_codex_hash_is_refused_and_untouched(self) -> None:
        self.backend.codex_hooks_reader = lambda *a, **k: None
        with self.assertRaises(OperationFailed):
            self.execute()
        self.assertIsNone(self.read())

    def test_target_outside_allowed_roots_is_refused(self) -> None:
        ctx = ExecutionContext("all-in-one", allowed_roots=("/nonexistent-root",))
        with self.assertRaises(OperationFailed):
            self.backend.execute(self.op, ctx)
        self.assertIsNone(self.read())

    def test_no_temp_file_is_left_behind(self) -> None:
        self.seed("")
        self.execute()
        self.assertEqual(sorted(p.name for p in Path(self.target).parent.iterdir()), ["config.toml"])


# ---------------------------------------------------------------------------
# verify-seat containment section, codex side
# ---------------------------------------------------------------------------
class VerifyCodexContainmentTests(PeriodicCase):
    """Real-manifest plans (mason: claude + codex fallback; watchman: codex only) under a tmp root."""

    def setUp(self) -> None:
        super().setUp()
        from tools import persona_installer_verify_seat as vs
        from tools.harness_adapter import codex_shell_box as csb

        self.vs, self.csb = vs, csb
        manifest = _pim.Manifest.load(REPO / "instance.yaml")
        self.installer = _pim.PersonaInstaller(repo_root=REPO)
        self.manifest = manifest
        self.xdg = self.root / "xdg"
        (self.xdg / "systemd").mkdir(parents=True)
        (self.xdg / "systemd" / "private").write_text("")

    def wired(self, persona: str):
        plan = self.installer.plan(self.manifest, persona, root=self.root / persona)
        op = self.op(plan, OP_ID)
        home = Path(op.desired["account_home"])
        for rel in (self.csb.HOOK_RELATIVE, self.csb.BOX_RELATIVE):
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            (home / rel).write_text("#!/bin/sh\n")
            (home / rel).chmod(0o755)
        config = Path(op.target)
        config.parent.mkdir(parents=True, exist_ok=True)
        _, text = self.csb.wire("", config.parent, home, executable=Path("/x"), read_hooks=fake_read_hooks)
        config.write_text(text)
        claude = [o for o in plan.operations if o.kind == _pim.SETTINGS_ENV_KIND]
        for c in claude:
            Path(c.target).parent.mkdir(parents=True, exist_ok=True)
            Path(c.target).write_text(__import__("json").dumps({"env": {c.desired["env_key"]: c.desired["env_value"]}}))
            Path(c.desired["env_value"]).parent.mkdir(parents=True, exist_ok=True)
            Path(c.desired["env_value"]).write_text("#!/bin/sh\n")
            Path(c.desired["env_value"]).chmod(0o755)
        return plan, op, config, home

    def checks(self, plan, **kw):
        out = self.vs.containment_checks(plan, env={"XDG_RUNTIME_DIR": str(self.xdg)},
                                         which=lambda n: "/usr/bin/systemd-run", **kw)
        return out, {c["name"]: c for c in out}

    def test_wired_codex_only_seat_passes(self) -> None:
        plan, *_ = self.wired("watchman")
        checks, by = self.checks(plan)
        self.assertTrue(all(c["ok"] for c in checks), checks)
        self.assertTrue({"codex-hook-block", "codex-hook-trusted", "codex-hook-targets",
                         "user-manager", "fallback-log"} <= set(by))
        self.assertNotIn("settings-key", by)

    def test_wired_claude_seat_with_codex_fallback_keeps_its_claude_checks(self) -> None:
        plan, *_ = self.wired("mason")
        checks, by = self.checks(plan)
        self.assertTrue(all(c["ok"] for c in checks), checks)
        self.assertTrue({"settings-key", "prefix-executable", "codex-hook-block"} <= set(by))

    def test_unboxed_codex_seat_fails(self) -> None:
        plan, op, config, home = self.wired("watchman")
        config.unlink()
        checks, by = self.checks(plan)
        self.assertFalse(by["codex-hook-block"]["ok"])
        self.assertFalse(by["codex-hook-trusted"]["ok"])
        config.write_text('model = "x"\n')
        self.assertFalse(self.checks(plan)[1]["codex-hook-block"]["ok"])

    def test_edited_or_untrusted_block_fails(self) -> None:
        plan, op, config, home = self.wired("watchman")
        good = config.read_text()
        config.write_text(good.replace('matcher = "Bash"', 'matcher = "Bash|apply_patch"'))
        self.assertFalse(self.checks(plan)[1]["codex-hook-block"]["ok"])
        config.write_text(good.split("[hooks.state.")[0])
        by = self.checks(plan)[1]
        self.assertTrue(by["codex-hook-block"]["ok"])
        self.assertFalse(by["codex-hook-trusted"]["ok"])

    def test_missing_or_non_executable_script_or_box_fails(self) -> None:
        plan, op, config, home = self.wired("watchman")
        (home / self.csb.BOX_RELATIVE).chmod(0o644)
        self.assertFalse(self.checks(plan)[1]["codex-hook-targets"]["ok"])
        (home / self.csb.BOX_RELATIVE).chmod(0o755)
        (home / self.csb.HOOK_RELATIVE).unlink()
        self.assertFalse(self.checks(plan)[1]["codex-hook-targets"]["ok"])

    def test_user_manager_and_fallback_log_still_checked_for_codex_only(self) -> None:
        plan, op, config, home = self.wired("watchman")
        out = self.vs.containment_checks(plan, env={}, which=lambda n: None)
        self.assertFalse({c["name"]: c for c in out}["user-manager"]["ok"])
        log = home / ".local/state/pantheon/shell-box-fallback.log"
        log.parent.mkdir(parents=True)
        log.write_text("unboxed\n")
        self.assertFalse(self.checks(plan)[1]["fallback-log"]["ok"])

    def test_injected_reader_also_gates_on_the_stored_hash_matching_codexs(self) -> None:
        plan, op, config, home = self.wired("watchman")
        checks, by = self.checks(plan, read_hooks=fake_read_hooks)
        self.assertTrue(by["codex-hook-hash"]["ok"], by["codex-hook-hash"])
        good = config.read_text()
        stored = good.split('trusted_hash = "')[1].split('"')[0]
        config.write_text(good.replace(stored, "sha256:stale"))
        self.assertFalse(self.checks(plan, read_hooks=fake_read_hooks)[1]["codex-hook-hash"]["ok"])
        self.assertNotIn("codex-hook-hash", self.checks(plan)[1])  # presence-only without a reader

    def test_check_is_read_only(self) -> None:
        plan, *_ = self.wired("watchman")
        before = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        self.checks(plan, read_hooks=fake_read_hooks)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
