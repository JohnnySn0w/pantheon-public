"""T-6099: ``persona_installer.py apply --only <op-id>[,<op-id>]``.

Intent: wire one operation (the codex shell-box hook, T-6097) onto a live seat without
running the rest of its plan (on consultant the full plan is the control-plane hand-move),
while keeping the digest-bound plan and audit trail. The sub-plan must refuse anything
it cannot show is safe in isolation: unknown ids and named ops whose prerequisites are
outside the subset.

Acceptance (2) is pinned here too: wiring the shell-box hook next to an already TRUSTED
worker-guard leaves EVERY PreToolUse hook trusted. Codex trust keys are positional
(``pre_tool_use:<group>:<hook>``) and an untrusted hook is silently skipped, so a
shifted worker-guard key would switch the guard off without any error.

Tmp roots, the simulated codex hooks/list and a tmp config only: nothing touches a live
seat, a real ~/.codex or codex itself.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path

from tools import persona_installer as _pim
from tools import persona_installer_apply as apply_cli
from tools.persona_installer import ExecutionContext, LocalHostBackend
from tools.test_codex_shell_box import fake_read_hooks
from tools.test_persona_installer_codex_shell_box import OP_ID, make_op

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "instance.yaml"


class OnlyPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def plan(self, persona: str = "codex", **kw):
        return apply_cli.build_plan(MANIFEST, persona, repo_root=REPO, root=self.root, **kw)[1]

    def test_only_shell_box_is_exactly_that_op_with_its_own_stable_digest(self) -> None:
        full = self.plan()
        sub = self.plan(only=[OP_ID])
        self.assertGreater(len(full.operations), 1)
        self.assertEqual([op.operation_id for op in sub.operations], [OP_ID])
        self.assertNotEqual(sub.digest, full.digest)
        self.assertEqual(self.plan(only=[OP_ID]).digest, sub.digest)
        self.assertEqual(sub.operator_actions, ())

    def test_subset_keeps_full_plan_order_whatever_the_flag_order(self) -> None:
        full_ids = [op.operation_id for op in self.plan().operations]
        pair = [OP_ID, "connective.config-dir"]
        self.assertIn("connective.config-dir", full_ids)
        got = [op.operation_id for op in self.plan(only=pair).operations]
        self.assertEqual(got, [i for i in full_ids if i in pair])
        self.assertEqual(self.plan(only=list(reversed(pair))).digest, self.plan(only=pair).digest)

    def test_unknown_id_is_refused_naming_it(self) -> None:
        with self.assertRaisesRegex(apply_cli.ApplyRefused, "not in the 'codex' plan: no.such-op"):
            self.plan(only=[OP_ID, "no.such-op"])

    def test_empty_selection_is_refused(self) -> None:
        with self.assertRaises(apply_cli.ApplyRefused):
            self.plan(only=["", " "])

    def test_named_op_with_prerequisite_outside_subset_is_refused(self) -> None:
        full = self.plan()
        gated = [op for op in full.operations if apply_cli._prerequisites(op)]
        self.assertTrue(gated, "fixture: the codex plan must contain an op with an 'after' prerequisite")
        op = gated[0]
        with self.assertRaisesRegex(apply_cli.ApplyRefused, "prerequisite not in the subset"):
            self.plan(only=[op.operation_id])

    def test_cross_op_references_count_as_prerequisites(self) -> None:
        import dataclasses

        full = self.plan()
        op = next(o for o in full.operations if o.operation_id == OP_ID)
        ref = dataclasses.replace(op, desired={**op.desired, "residue_cleared_by": "service.x",
                                               "trust_set_by": "harness.y"})
        self.assertEqual(apply_cli._prerequisites(ref), {"service.x", "harness.y"})
        self.assertEqual(apply_cli._prerequisites(op), set())

    def test_execute_with_a_sub_plan_digest_but_no_only_is_refused(self) -> None:
        sub = self.plan(only=[OP_ID])
        account = sub.account
        with self.assertRaisesRegex(apply_cli.ApplyRefused, "does not match the freshly built plan"):
            apply_cli.execute(
                MANIFEST, "codex", plan_digest=sub.digest, repo_root=REPO, root=self.root,
                euid=1000, uid_of=lambda name: 1000 if name == account else None,
                backend=_pim.SimulatedHostBackend(),
            )

    def test_naming_the_prerequisites_too_is_accepted(self) -> None:
        full = self.plan()
        ids = {o.operation_id for o in full.operations}
        for op in full.operations:
            needs = apply_cli._prerequisites(op)
            if needs and needs <= ids and all(
                not apply_cli._prerequisites(o) for o in full.operations if o.operation_id in needs
            ):
                sub = self.plan(only=[op.operation_id, *sorted(needs)])
                self.assertEqual({o.operation_id for o in sub.operations}, {op.operation_id, *needs})
                return
        self.skipTest("no op whose prerequisites are themselves unprerequisited in this manifest")

    def test_only_with_another_workflow_is_refused(self) -> None:
        with self.assertRaisesRegex(apply_cli.ApplyRefused, "all-in-one"):
            self.plan(only=[OP_ID], workflow="stage-prefix")

    def test_without_only_the_plan_is_unchanged(self) -> None:
        self.assertEqual(self.plan().digest, self.plan(only=None).digest)

    def test_execute_with_the_full_plan_digest_is_refused_before_any_change(self) -> None:
        full = self.plan()
        account = full.account
        with self.assertRaisesRegex(apply_cli.ApplyRefused, "does not match the freshly built plan"):
            apply_cli.execute(
                MANIFEST, "codex", plan_digest=full.digest, only=[OP_ID], repo_root=REPO,
                root=self.root, euid=1000, uid_of=lambda name: 1000 if name == account else None,
                backend=_pim.SimulatedHostBackend(),
            )

    def test_cli_passes_the_comma_list_to_dry_run_and_execute(self) -> None:
        from unittest import mock

        seen = {}

        def fake(name):
            def call(*_a, **kw):
                seen[name] = kw.get("only")
                return 0, {}
            return call
        with mock.patch.object(apply_cli, "dry_run", side_effect=fake("dry")), \
                mock.patch.object(apply_cli, "execute", side_effect=fake("exec")), \
                mock.patch.object(_pim, "_print_json"):
            _pim.main(["apply", "--persona", "codex", "--only", f"{OP_ID},connective.config-dir"])
            _pim.main(["apply", "--persona", "codex", "--execute", "--plan-digest", "d",
                       "--only", OP_ID])
            _pim.main(["apply", "--persona", "codex"])
        self.assertEqual(seen["dry"], None)  # last dry-run call had no --only
        self.assertEqual(seen["exec"], [OP_ID])

    def test_cli_dry_run_receives_the_split_list(self) -> None:
        from unittest import mock

        with mock.patch.object(apply_cli, "dry_run", return_value=(0, {})) as dry, \
                mock.patch.object(_pim, "_print_json"):
            _pim.main(["apply", "--persona", "codex", "--only", f"{OP_ID},connective.config-dir"])
        self.assertEqual(dry.call_args.kwargs["only"], [OP_ID, "connective.config-dir"])

def _real_key(codex_home: Path, group: int, hook: int) -> str:
    return f"{os.path.realpath(codex_home)}/config.toml:pre_tool_use:{group}:{hook}"


def _codex_hash(matcher: str, hook: dict) -> str:
    # The same definition hash fake_read_hooks (the codex hooks/list stand-in) reports.
    return "sha256:" + hashlib.sha256(json.dumps([matcher, hook], sort_keys=True).encode()).hexdigest()


class PositionalTrustAcceptanceTests(unittest.TestCase):
    """T-6099 acceptance (2): every PreToolUse hook stays trusted, worker-guard included."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        oom = self.home / "pantheon-config" / "tools" / "oom"
        oom.mkdir(parents=True)
        for name in ("pantheon-codex-shell-box-hook.py", "pantheon-shell-box.sh"):
            (oom / name).write_text("#!/bin/sh\n")
            (oom / name).chmod(0o755)
        self.codex_home = self.home / ".codex"
        self.codex_home.mkdir()
        self.config = self.codex_home / "config.toml"
        guard = {
            "type": "command",
            "command": f"/usr/bin/python3 -I {self.home}/pantheon-config/tools/hooks/worker-guard-v2.py",
            "timeout": 30,
        }
        self.guard_key = _real_key(self.codex_home, 0, 0)
        self.guard_hash = _codex_hash("Bash|apply_patch", guard)
        self.config.write_text(
            'model = "gpt-x"\n\n'
            "[[hooks.PreToolUse]]\n"
            'matcher = "Bash|apply_patch"\n\n'
            "[[hooks.PreToolUse.hooks]]\n"
            'type = "command"\n'
            f'command = "{guard["command"]}"\n'
            "timeout = 30\n\n"
            "[[hooks.SessionStart]]\n"
            'matcher = "startup"\n\n'
            "[[hooks.SessionStart.hooks]]\n"
            'type = "command"\n'
            'command = "/usr/bin/true"\n\n'
            f'[hooks.state."{self.guard_key}"]\n'
            f'trusted_hash = "{self.guard_hash}"\n'
        )
        self.config.chmod(0o600)
        self.backend = LocalHostBackend()
        self.backend.codex_hooks_reader = fake_read_hooks

    def statuses(self) -> dict[str, str]:
        return {h["key"]: h["trustStatus"] for h in fake_read_hooks(None, codex_home=self.codex_home)}

    def test_fixture_starts_with_a_trusted_worker_guard(self) -> None:
        self.assertEqual(self.statuses(), {self.guard_key: "trusted"})

    def test_wiring_the_shell_box_leaves_every_pre_tool_use_hook_trusted(self) -> None:
        op = make_op(str(self.config), str(self.home))
        _mutation, evidence = self.backend.execute(
            op, ExecutionContext("all-in-one", allowed_roots=(str(self.home),))
        )
        self.assertTrue(evidence.ok)
        statuses = self.statuses()
        self.assertEqual(len(statuses), 2)
        self.assertEqual(set(statuses.values()), {"trusted"}, statuses)
        # The guard keeps its POSITION and its stored hash: shell-box is appended after it.
        self.assertEqual(statuses[self.guard_key], "trusted")
        parsed = tomllib.loads(self.config.read_text())
        groups = parsed["hooks"]["PreToolUse"]
        self.assertIn("worker-guard-v2.py", groups[0]["hooks"][0]["command"])
        self.assertIn("pantheon-codex-shell-box-hook.py", groups[1]["hooks"][0]["command"])
        self.assertEqual(parsed["hooks"]["state"][self.guard_key]["trusted_hash"], self.guard_hash)
        self.assertEqual(len(parsed["hooks"]["SessionStart"]), 1)


if __name__ == "__main__":
    unittest.main()
