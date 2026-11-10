#!/usr/bin/env python3
"""Hermetic tests for tools/recovery_continuity_switch.py (T-3178 gate G2).

The load-bearing guarantee is that DRY-RUN never mutates a receiver. The core
test wraps FAKE base ports with dry_run_ports, runs a real resume_after_crash
-> rollback flow, and asserts the fake world's runtimes/maintenance were NEVER
actually touched (the no-op ports intercepted every mutation) while the
dry-run log recorded what WOULD have happened.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools import recovery_continuity_switch as wcs
from tools import continuity_switch_coordinator as csc
from tools import persona_continuity as pc
from tools.test_continuity_switch_coordinator import FakeWorld, _ports, _binding


class BuildConfigTests(unittest.TestCase):
    def test_home_defaults_are_resolved_when_called(self) -> None:
        state_dir = Path("/tmp/call-time-recovery-state")
        with mock.patch.dict(os.environ, {"PANTHEON_STATE_DIR": str(state_dir)}):
            cfg = wcs.build_config()
            args = wcs.build_parser().parse_args(["show-config"])
        state = state_dir / "personas"
        continuity = state / "recovery/continuity"
        self.assertEqual(cfg.state_root, state)
        self.assertEqual(cfg.bundle_path, continuity / "descriptor/bundle.yaml")
        self.assertEqual(Path(args.state_root), state)
        self.assertEqual(Path(args.continuity_root), continuity)

    def test_paths_reconstruct_from_continuity_root(self) -> None:
        cont = Path("/tmp/x/continuity")
        cfg = wcs.build_config(continuity_root=cont, repo_root=Path("/repo"),
                               state_root=Path("/state"))
        self.assertEqual(cfg.persona_id, "recovery")
        self.assertEqual(cfg.organism_id, "pantheon")
        self.assertEqual(cfg.runtime_manifest_path, Path("/repo/instance.yaml"))
        self.assertEqual(cfg.bundle_path, cont / "descriptor" / "bundle.yaml")
        self.assertEqual(cfg.descriptor_root, cont / "descriptor")
        self.assertEqual(cfg.checkpoint_root, cont / "checkpoints")
        self.assertEqual(cfg.switch_state_root, cont / "switch")
        self.assertEqual(cfg.actor.actor_id, "recovery")
        self.assertIn("authority:canonical", cfg.actor.authority_refs)

    def test_switch_state_root_override(self) -> None:
        cont = Path("/tmp/x/continuity")
        cfg = wcs.build_config(continuity_root=cont, switch_state_root=cont / "switch-dryrun")
        self.assertEqual(cfg.switch_state_root, cont / "switch-dryrun")


class LoadPinsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cont = Path(self.tmp.name) / "continuity"
        self.cont.mkdir(parents=True)

    def test_missing_inventory_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            wcs.load_pins(self.cont)

    def test_reads_inventory(self) -> None:
        (self.cont / "readback-inventory.json").write_text(json.dumps({"pinned_revision": "abc"}))
        self.assertEqual(wcs.load_pins(self.cont)["pinned_revision"], "abc")


class DryRunNoMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        manifest = self.root / "instance.yaml"
        manifest.write_text("roles: {}\n")
        self.old = _binding(self.root, role="recovery", persona="recovery",
                            account="claude-fallback", harness="claude")
        self.candidate = _binding(self.root, role="recovery", persona="recovery",
                                  account="claude-fallback", harness="codex")
        self.world = FakeWorld()
        self.world.live[self.world.ref(self.old)] = True
        base = _ports(self.world)
        base.list_bindings = lambda: (self.old, self.candidate)
        self.config = csc.CoordinatorConfig(
            persona_id="recovery", organism_id="pantheon",
            runtime_manifest_path=manifest,
            bundle_path=self.root / "descriptor" / "bundle.yaml",
            descriptor_root=self.root / "descriptor",
            repo_root=self.root, state_root=self.root / "state",
            checkpoint_root=self.root / "checkpoints",
            switch_state_root=self.root / "switch-dryrun",
            actor=pc.ActorContext("recovery", ("authority:canonical",), wcs.ACTOR_PURPOSES),
        )
        self.log = wcs._DryRunLog()
        self.ports = wcs.dry_run_ports(self.config, self.log, base=base)
        self.coord = csc.ContinuitySwitchCoordinator(self.config, self.ports)

    def test_resume_rollback_performs_no_real_mutation(self) -> None:
        # No transition record for this id -> resume reconstructs a plan and
        # rolls back. Every mutation must hit the dry-run no-ops, not the world.
        receipt = self.coord.resume_after_crash("plan-nonexistent")
        self.assertEqual(receipt.outcome, "rolled-back")
        # The fake world was never actually mutated:
        self.assertEqual(self.world.started, [], "start_runtime leaked to base")
        self.assertEqual(self.world.stopped, [], "stop_runtime leaked to base")
        self.assertEqual(self.world.maintenance_exits, [], "exit_maintenance leaked to base")
        # ...but the dry-run log recorded the intended mutations:
        joined = " ".join(self.log.calls)
        self.assertIn("start_runtime", joined)
        self.assertIn("stop_runtime", joined)
        self.assertIn("exit_maintenance", joined)

    def test_dry_run_receipt_lands_in_sandbox_switch_root(self) -> None:
        self.coord.resume_after_crash("plan-nonexistent")
        receipts = list((self.config.switch_state_root / "recovery" / "receipts").glob("*.json"))
        self.assertTrue(receipts, "expected a receipt in the sandbox switch root")


class MakeCoordinatorTests(unittest.TestCase):
    def test_dry_run_uses_sandbox_switch_root(self) -> None:
        args = argparse.Namespace(
            continuity_root="/tmp/x/continuity", repo_root="/repo",
            state_root="/state", live=False,
        )
        _, config, log = wcs.make_coordinator(args, dry_run=True)
        self.assertEqual(config.switch_state_root.name, "switch-dryrun")
        self.assertIsNotNone(log)


class HardeningTests(unittest.TestCase):
    """consultant T-3178 finding 4 corrections."""

    def _config(self, tmp: Path) -> csc.CoordinatorConfig:
        (tmp / "instance.yaml").write_text("roles: {}\n")
        return csc.CoordinatorConfig(
            persona_id="recovery", organism_id="pantheon",
            runtime_manifest_path=tmp / "instance.yaml",
            bundle_path=tmp / "descriptor" / "bundle.yaml", descriptor_root=tmp / "descriptor",
            repo_root=tmp, state_root=tmp / "state", checkpoint_root=tmp / "checkpoints",
            switch_state_root=tmp / "switch", actor=pc.ActorContext("recovery", ("authority:canonical",), wcs.ACTOR_PURPOSES),
        )

    def test_dry_run_isolate_candidate_never_calls_base(self) -> None:
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        world = FakeWorld()
        base = _ports(world)
        def tripwire(_b):
            raise AssertionError("real isolate_candidate must not be called in dry-run")
        base.isolate_candidate = tripwire
        log = wcs._DryRunLog()
        ports = wcs.dry_run_ports(self._config(tmp), log, base=base)
        binding = _binding(tmp, role="recovery", persona="recovery", account="claude-fallback", harness="codex")
        b, proof = ports.isolate_candidate(binding)  # must NOT raise
        self.assertEqual(proof.mechanism, "codex-writable-roots-empty")
        self.assertIn("isolate_candidate", " ".join(log.calls))

    def test_load_validated_plan_rejects_bad(self) -> None:
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        good = {f: "x" for f in wcs._PLAN_FIELDS}
        good["schema"] = csc.SWITCH_PLAN_SCHEMA_ID; good["persona_id"] = "recovery"
        p = tmp / "plan.json"
        # wrong persona
        bad = dict(good); bad["persona_id"] = "orchestrator"; p.write_text(json.dumps(bad))
        with self.assertRaises(ValueError):
            wcs.load_validated_plan(p)
        # wrong schema
        bad = dict(good); bad["schema"] = "nope"; p.write_text(json.dumps(bad))
        with self.assertRaises(ValueError):
            wcs.load_validated_plan(p)
        # missing field
        bad = dict(good); del bad["plan_sha256"]; p.write_text(json.dumps(bad))
        with self.assertRaises(ValueError):
            wcs.load_validated_plan(p)
        # good one parses
        p.write_text(json.dumps(good))
        self.assertEqual(wcs.load_validated_plan(p).persona_id, "recovery")

    def test_live_gate_requires_confirm_token(self) -> None:
        args = argparse.Namespace(continuity_root="/tmp/x", repo_root="/repo", confirm_fire=None, expect_rev=None)
        with self.assertRaises(SystemExit):
            wcs.enforce_live_gate(args)



class PersonaStateDirResolutionTests(unittest.TestCase):
    """T-6285: resolve the personas dir exactly as the runtime planes do
    (harness_state.state_root_from_env), not PANTHEON_STATE_DIR alone."""

    def _env(self, **kv):
        env = {k: v for k, v in os.environ.items()
               if k not in ("PANTHEON_STATE_DIR", "PANTHEON_PERSONA_STATE_DIR")}
        env.update(kv)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_persona_state_dir_alone_is_honoured(self) -> None:
        with self._env(PANTHEON_PERSONA_STATE_DIR="/tmp/t6285-root/personas"):
            self.assertEqual(wcs._default_state_root(), Path("/tmp/t6285-root/personas"))

    def test_both_set_and_agreeing(self) -> None:
        with self._env(PANTHEON_PERSONA_STATE_DIR="/tmp/t6285-root/personas",
                       PANTHEON_STATE_DIR="/tmp/t6285-root"):
            self.assertEqual(wcs._default_state_root(), Path("/tmp/t6285-root/personas"))

    def test_disagreeing_env_is_refused(self) -> None:
        with self._env(PANTHEON_PERSONA_STATE_DIR="/tmp/a/personas",
                       PANTHEON_STATE_DIR="/tmp/b"):
            with self.assertRaises(Exception):
                wcs._default_state_root()

    def test_default_is_recovery_home(self) -> None:
        with self._env():
            self.assertEqual(wcs._default_state_root(),
                             wcs.RECOVERY_HOME / ".local/state/pantheon/personas")

if __name__ == "__main__":
    unittest.main()
