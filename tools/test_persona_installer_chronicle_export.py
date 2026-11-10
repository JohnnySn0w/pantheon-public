"""T-6118 PR-1: installer-owned chronicle-export tree (root / <seat> / .ingested).

Simulated backend and tempdir roots only. Nothing here touches /var/lib/pantheon,
a real home, or a live seat; os.chown/os.fchown/os.chmod/os.fchmod are recorded
(and only ever aimed at the running user's own uid/gid).
"""

from __future__ import annotations

import dataclasses
import grp
import os
import pwd
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tools import persona_installer as _pim
from tools.persona_installer import (
    EXECUTION_CONTEXT_ALL_IN_ONE,
    LocalHostBackend,
    Manifest,
    ManifestError,
    OperationFailed,
    SimulatedHostBackend,
)
from tools.test_persona_installer import REPO_ROOT, ManifestPinnedInstaller, _manifest_dict

EXPORT_ROOT = "/var/lib/pantheon/chronicle-export"
CFG = {"root": EXPORT_ROOT, "group": "shared-grp", "ingest_account": "ingest-acct"}
OP_IDS = ("chronicle-export.root", "chronicle-export.seat", "chronicle-export.ingested")


class ExportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest_path = self.root / "instance.yaml"
        self.set_manifest()
        self.installer = ManifestPinnedInstaller(REPO_ROOT)

    def set_manifest(self, cfg=CFG, mutate=None) -> None:
        data = _manifest_dict(second_persona=True)
        if cfg is not None:
            data["persona_installer"]["chronicle_export"] = dict(cfg)
        if mutate:
            mutate(data)
        self.manifest_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        self.manifest = Manifest.load(self.manifest_path)

    def plan(self, persona="recovery", root=None):
        return self.installer.plan(self.manifest, persona, root=root or self.root / "projection")

    def export_ops(self, plan):
        return [o for o in plan.operations if o.operation_id.startswith("chronicle-export.")]

    def op(self, plan, operation_id):
        return next(o for o in plan.operations if o.operation_id == operation_id)


class PlanShapeTests(ExportCase):
    def check_plan(self, persona, seat, account):
        projection = self.root / "projection"
        plan = self.plan(persona, projection)
        ops = self.export_ops(plan)
        self.assertEqual(tuple(o.operation_id for o in ops), OP_IDS)
        base = str(projection) + EXPORT_ROOT
        want = {
            "chronicle-export.root": (base, "root", "shared-grp", "2750"),
            "chronicle-export.seat": (f"{base}/{seat}", account, "shared-grp", "2750"),
            "chronicle-export.ingested": (f"{base}/.ingested", "ingest-acct", "shared-grp", "2755"),
        }
        for o in ops:
            target, owner, group, mode = want[o.operation_id]
            self.assertEqual(o.kind, "mkdir")
            self.assertEqual(o.target, target)
            self.assertEqual(dict(o.desired), {"mode": mode, "owner": owner, "group": group})
            self.assertEqual(o.inverse, {"state": "remove-if-created-and-empty"})
            self.assertEqual(o.privilege, "privileged-helper")
            self.assertEqual(int(o.desired["mode"], 8), int(mode, 8))
        self.assertEqual(int(want["chronicle-export.root"][3], 8), 0o2750)
        self.assertEqual(int(want["chronicle-export.ingested"][3], 8), 0o2755)
        # ordering: root precedes seat and .ingested
        ids = [o.operation_id for o in plan.operations]
        self.assertLess(ids.index(OP_IDS[0]), ids.index(OP_IDS[1]))
        self.assertLess(ids.index(OP_IDS[0]), ids.index(OP_IDS[2]))
        return plan

    def test_claude_persona_plan(self) -> None:
        self.check_plan("recovery", "recovery", "claude-fallback")

    def test_codex_persona_maps_to_appserver_seat_name(self) -> None:
        self.check_plan("consultant", "consultant", "codex")

    def test_allowed_roots_include_export_root(self) -> None:
        plan = self.plan()
        roots = plan.provenance[_pim.ALLOWED_TARGET_ROOTS_KEY].split(",")
        self.assertIn(str(self.root / "projection") + EXPORT_ROOT, roots)

    def test_absent_block_plans_nothing_and_roots_unchanged(self) -> None:
        self.set_manifest(cfg=None)
        plan = self.plan()
        self.assertEqual(self.export_ops(plan), [])
        self.assertNotIn("chronicle-export", plan.provenance[_pim.ALLOWED_TARGET_ROOTS_KEY])

    def test_malformed_block_is_manifest_error(self) -> None:
        bad = [
            {"root": EXPORT_ROOT, "group": "g"},
            {**CFG, "extra": "x"},
            {**CFG, "root": "relative/path"},
            {**CFG, "root": "/var/../etc"},
            {**CFG, "group": ""},
            {**CFG, "ingest_account": "bad name"},
            {**CFG, "group": 5},
        ]
        for cfg in bad:
            with self.subTest(cfg=cfg), self.assertRaises(ManifestError):
                self.set_manifest(cfg=cfg)
        with self.assertRaises(ManifestError):
            self.set_manifest(cfg=None, mutate=lambda d: d["persona_installer"].update(chronicle_export=["x"]))

    def test_existing_mkdir_ops_have_no_owner_or_group(self) -> None:
        for o in self.plan().operations:
            if o.kind == "mkdir" and not o.operation_id.startswith("chronicle-export."):
                self.assertEqual(set(o.desired), {"mode"}, o.operation_id)


class SimulatedBackendTests(ExportCase):
    def setUp(self) -> None:
        super().setUp()
        self.plan_ = self.plan()
        self.ops = [self.op(self.plan_, i) for i in OP_IDS]
        self.backend = SimulatedHostBackend()

    def converge(self):
        for o in self.ops:
            result = self.backend.preflight(o)
            self.assertTrue(result.ok, result)
            self.backend.execute(o, EXECUTION_CONTEXT_ALL_IN_ONE)
        return [self.backend.verify_operation(o) for o in self.ops]

    def test_create_records_owner_group_mode_and_verifies(self) -> None:
        for ev in self.converge():
            self.assertTrue(ev.ok, ev)
        e = self.backend.entries[self.ops[1].target]
        self.assertEqual((e["state"], e["owner"], e["group"], e["mode"]), ("directory", "claude-fallback", "shared-grp", "2750"))
        self.assertEqual(self.backend.entries[self.ops[2].target]["mode"], "2755")

    def test_idempotent_rerun_no_change(self) -> None:
        self.converge()
        before = {k: dict(v) for k, v in self.backend.entries.items()}
        for o in self.ops:
            result = self.backend.preflight(o)
            self.assertEqual((result.ok, result.classification), (True, "directory"))
            mutation, evidence = self.backend.execute(o, EXECUTION_CONTEXT_ALL_IN_ONE)
            self.assertFalse(mutation.changed)
            self.assertTrue(evidence.ok)
        self.assertEqual(before, self.backend.entries)

    def seed(self, operation, **override):
        entry = {"state": "directory", "owner": operation.desired["owner"], "group": operation.desired["group"], "mode": operation.desired["mode"]}
        entry.update(override)
        self.backend.entries[operation.target] = entry

    def test_wrong_owner_group_mode_each_refused_with_expected_vs_actual(self) -> None:
        seat = self.ops[1]
        for key, wrong in (("owner", "someone"), ("group", "othergrp"), ("mode", "2755"), ("mode", "0750")):
            with self.subTest(key=key, wrong=wrong):
                self.seed(seat, **{key: wrong})
                result = self.backend.preflight(seat)
                self.assertFalse(result.ok)
                self.assertEqual(result.classification, "conflicting")
                self.assertIn(key, result.detail)
                self.assertIn(wrong, result.detail)
                self.assertIn(seat.desired[key], result.detail)
                with self.assertRaises(OperationFailed):
                    self.backend.execute(seat, EXECUTION_CONTEXT_ALL_IN_ONE)
                # never silently fixed
                self.assertEqual(self.backend.entries[seat.target][key], wrong)

    def test_existing_dir_without_ownership_info_is_refused(self) -> None:
        self.backend.entries[self.ops[0].target] = {"state": "directory", "mode": "2750"}
        self.assertFalse(self.backend.preflight(self.ops[0]).ok)

    def test_symlink_target_or_ancestor_refused(self) -> None:
        self.backend.entries[self.ops[1].target] = {"state": "symlink", "target": "/elsewhere"}
        self.assertFalse(self.backend.preflight(self.ops[1]).ok)
        del self.backend.entries[self.ops[1].target]
        self.backend.entries[self.ops[0].target] = {"state": "symlink", "target": "/elsewhere"}
        self.assertFalse(self.backend.preflight(self.ops[1]).ok)

    def test_rollback_removes_only_created_dirs(self) -> None:
        self.seed(self.ops[0])  # pre-existing, correct
        mutations = []
        for o in self.ops:
            mutation, _ = self.backend.execute(o, EXECUTION_CONTEXT_ALL_IN_ONE)
            mutations.append(mutation)
        self.assertEqual([m.changed for m in mutations], [False, True, True])
        for mutation in reversed(mutations):
            check = self.backend.can_rollback(mutation)
            if check.classification == "not-applied":
                continue
            self.backend.rollback_mutation(mutation, allowed_roots=_pim._plan_allowed_roots(self.plan_))
        self.assertIn(self.ops[0].target, self.backend.entries)
        self.assertNotIn(self.ops[1].target, self.backend.entries)
        self.assertNotIn(self.ops[2].target, self.backend.entries)


class DiskFixture(ExportCase):
    """Real filesystem under a tempdir, the running user's own uid/gid, recorders on
    every chown/chmod entry point."""

    def setUp(self) -> None:
        super().setUp()
        self.me = pwd.getpwuid(os.getuid()).pw_name
        self.mygrp = grp.getgrgid(os.getgid()).gr_name
        self.base = Path(os.path.realpath(self.root)) / "host"
        self.var = self.base / "var" / "lib" / "pantheon"
        self.var.mkdir(parents=True)
        self.calls: list[tuple] = []
        for name in ("chown", "fchown", "chmod", "fchmod", "lchown"):
            real = getattr(os, name)

            def recorder(*a, _n=name, _r=real, **k):
                self.calls.append((_n, a, k))
                return _r(*a, **k)

            patcher = mock.patch.object(os, name, recorder)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.plan_ = self.plan(root=self.base)

    def own(self, operation, **extra):
        desired = dict(operation.desired)
        desired.update(owner=self.me, group=self.mygrp, **extra)
        return dataclasses.replace(operation, desired=desired)

    def ops(self):
        return [self.own(self.op(self.plan_, i)) for i in OP_IDS]

    def preflight(self, operation):
        return _pim._owned_mkdir_preflight(operation)

    def apply(self, operation):
        return LocalHostBackend.__new__(LocalHostBackend)._simple_mutation(operation)


class LocalBackendTests(DiskFixture):
    def test_create_chown_then_chmod_last_and_setgid_survives(self) -> None:
        root_op, seat_op, ing_op = self.ops()
        for o in (root_op, seat_op, ing_op):
            self.assertTrue(self.preflight(o).ok)
            self.assertTrue(self.apply(o).changed)
        for o, mode in ((root_op, 0o2750), (seat_op, 0o2750), (ing_op, 0o2755)):
            st = os.lstat(o.target)
            self.assertEqual(stat.S_IMODE(st.st_mode), mode)
            self.assertTrue(stat.S_ISDIR(st.st_mode))
        names = [c[0] for c in self.calls]
        self.assertLess(names.index("fchown"), names.index("fchmod"))
        self.assertTrue(all(c[0] in {"fchown", "fchmod"} for c in self.calls), self.calls)

    def test_idempotent_rerun_makes_no_change(self) -> None:
        for o in self.ops():
            self.apply(o)
        self.calls.clear()
        for o in self.ops():
            result = self.preflight(o)
            self.assertEqual((result.ok, result.classification), (True, "directory"))
            self.assertFalse(self.apply(o).changed)
        self.assertEqual(self.calls, [])

    def test_existing_dir_mismatch_refused_at_preflight_and_mutation_never_fixed(self) -> None:
        root_op = self.ops()[0]
        self.apply(root_op)
        os.chmod(root_op.target, 0o2755)
        self.calls.clear()
        result = self.preflight(root_op)
        self.assertFalse(result.ok)
        self.assertIn("2755", result.detail)
        self.assertIn("2750", result.detail)
        with self.assertRaises(OperationFailed):
            self.apply(root_op)
        self.assertEqual(self.calls, [])
        self.assertEqual(stat.S_IMODE(os.lstat(root_op.target).st_mode), 0o2755)

    def test_wrong_owner_or_group_names_refused(self) -> None:
        root_op = self.ops()[0]
        self.apply(root_op)
        other_gid = next((g for g in os.getgroups() if g != os.getgid()), None)
        if other_gid is not None:
            os.chown(root_op.target, -1, other_gid)
            self.assertFalse(self.preflight(root_op).ok)
        bad_owner = dataclasses.replace(root_op, desired={**root_op.desired, "owner": "no-such-user-xyz"})
        result = self.preflight(bad_owner)
        self.assertFalse(result.ok)
        self.assertIn("no-such-user-xyz", result.detail)
        bad_group = dataclasses.replace(root_op, desired={**root_op.desired, "group": "no-such-group-xyz"})
        self.assertFalse(self.preflight(bad_group).ok)

    def test_symlink_at_target_refused_nothing_changed_through_it(self) -> None:
        root_op, seat_op, _ = self.ops()
        self.apply(root_op)
        decoy = self.base / "decoy"
        decoy.mkdir(mode=0o755)
        os.chmod(decoy, 0o755)
        Path(seat_op.target).symlink_to(decoy)
        self.calls.clear()
        result = self.preflight(seat_op)
        self.assertFalse(result.ok)
        self.assertIn("symlink", result.detail)
        with self.assertRaises(OperationFailed):
            self.apply(seat_op)
        self.assertEqual(self.calls, [])
        self.assertEqual(stat.S_IMODE(os.lstat(decoy).st_mode), 0o755)
        self.assertTrue(os.path.islink(seat_op.target))

    def test_symlink_at_intermediate_component_refused_nothing_created_or_changed(self) -> None:
        root_op, seat_op, ing_op = self.ops()
        decoy = self.base / "decoy"
        decoy.mkdir(mode=0o755)
        Path(root_op.target).symlink_to(decoy)  # planted where the root op would create
        self.calls.clear()
        for o in (seat_op, ing_op):
            result = self.preflight(o)
            self.assertFalse(result.ok, o.operation_id)
            self.assertIn("symlink", result.detail)
            with self.assertRaises(OperationFailed):
                self.apply(o)
        result = self.preflight(root_op)  # the root op itself also refuses its own planted link
        self.assertFalse(result.ok)
        self.assertEqual(self.calls, [])
        self.assertEqual(list(decoy.iterdir()), [])
        self.assertEqual(stat.S_IMODE(os.lstat(decoy).st_mode), 0o755)

    def test_symlink_in_ancestor_above_export_root_refused(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (self.var / "x").symlink_to(elsewhere)
        root_op = self.ops()[0]
        moved = dataclasses.replace(root_op, target=str(self.var / "x" / "chronicle-export"))
        self.assertFalse(self.preflight(moved).ok)
        with self.assertRaises(OperationFailed):
            self.apply(moved)
        self.assertEqual(self.calls, [])
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_unprivileged_dry_run_refuses_to_create_foreign_owned_dir(self) -> None:
        root_op = self.ops()[0]
        foreign = dataclasses.replace(root_op, desired={**root_op.desired, "owner": "root"})
        if os.geteuid() == 0:
            self.skipTest("running as root")
        result = self.preflight(foreign)
        self.assertFalse(result.ok)
        self.assertIn("needs root", result.detail)

    def test_non_directory_target_refused(self) -> None:
        root_op = self.ops()[0]
        Path(root_op.target).write_text("x")
        self.assertFalse(self.preflight(root_op).ok)

    def test_rollback_removes_only_created_and_only_when_empty(self) -> None:
        root_op, seat_op, ing_op = self.ops()
        pre = self.apply(root_op)  # created by us, but treat as the pre-existing one below
        backend = LocalHostBackend.__new__(LocalHostBackend)
        mutations = [backend._simple_mutation(root_op), backend._simple_mutation(seat_op), backend._simple_mutation(ing_op)]
        self.assertEqual([m.changed for m in mutations], [False, True, True])
        (Path(ing_op.target) / "receipt").write_text("keep")  # non-empty: kept
        roots = (str(self.base),)
        for m in reversed(mutations):
            check = backend.can_rollback(m)
            if check.classification != "not-applied":
                self.assertTrue(check.ok, check)
                backend.rollback_mutation(m, allowed_roots=roots)
        self.assertTrue(Path(root_op.target).is_dir())  # pre-existing: untouched
        self.assertFalse(Path(seat_op.target).exists())  # created + empty: removed
        self.assertTrue(Path(ing_op.target, "receipt").exists())  # non-empty: kept
        self.assertEqual(pre.owner, root_op.owner)

    def test_rollback_does_not_follow_swapped_symlink(self) -> None:
        root_op, seat_op, _ = self.ops()
        self.apply(root_op)
        m = self.apply(seat_op)
        decoy = self.base / "decoy2"
        (decoy / "recovery").mkdir(parents=True)
        os.rmdir(seat_op.target)
        os.rmdir(root_op.target)
        Path(root_op.target).symlink_to(decoy)
        backend = LocalHostBackend.__new__(LocalHostBackend)
        try:
            backend.rollback_mutation(m, allowed_roots=(str(self.base),))
        except OperationFailed:
            pass
        self.assertTrue((decoy / "recovery").is_dir())


class RegressionTests(DiskFixture):
    def test_plain_mkdir_op_unchanged_on_both_backends(self) -> None:
        plain = next(o for o in self.plan_.operations if o.operation_id == "connective.config-dir")
        self.assertEqual(set(plain.desired), {"mode"})
        # simulated: entry shape unchanged (no owner/group keys)
        sim = SimulatedHostBackend()
        sim.execute(plain, EXECUTION_CONTEXT_ALL_IN_ONE)
        self.assertEqual(sim.entries[plain.target], {"state": "directory", "mode": "0755"})
        # a mode mismatch on an existing plain dir is still accepted (no owner/group => no check)
        sim.entries[plain.target] = {"state": "directory", "mode": "0700"}
        self.assertTrue(sim.preflight(plain).ok)
        # local: plain mkdir path creates + chmods through os.chmod, never fchown
        target = self.base / "plain"
        op = dataclasses.replace(plain, target=str(target))
        self.calls.clear()
        mutation = self.apply(op)
        self.assertTrue(mutation.changed)
        self.assertEqual([c[0] for c in self.calls], ["chmod"])
        self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), 0o755)

    def test_mode_string_round_trips_octal(self) -> None:
        self.assertEqual(int("2750", 8), 0o2750)
        self.assertEqual(int("2755", 8), 0o2755)
        self.assertEqual(int("0755", 8), 0o755)


class ExactRootContainmentTests(ExportCase):
    """Review fix 1: only the export root may BE an allowed root; home and the mailbox hub may not."""

    def setUp(self) -> None:
        super().setUp()
        self.proj = Path(os.path.realpath(self.root)) / "projection"
        self.plan_ = self.plan(root=self.proj)
        self.roots = tuple(self.plan_.provenance[_pim.ALLOWED_TARGET_ROOTS_KEY].split(","))

    def owned_op(self, target):
        base = self.op(self.plan_, "chronicle-export.root")
        return dataclasses.replace(base, target=str(target))

    def test_roots_order_is_home_hub_export(self) -> None:
        self.assertEqual(self.roots[2], str(self.proj) + EXPORT_ROOT)
        self.assertEqual(len(self.roots), 3)

    def test_export_root_owned_mkdir_accepted(self) -> None:
        self.assertIsNone(_pim._realpath_containment_refusal(self.owned_op(self.roots[2]), self.roots))

    def test_home_and_hub_exact_targets_refused_for_ops(self) -> None:
        for root in self.roots[:2]:
            for kind_op in (
                self.owned_op(root),
                dataclasses.replace(self.owned_op(root), desired={"mode": "0755"}),  # plain mkdir
            ):
                self.assertIn("outside allowed roots", _pim._realpath_containment_refusal(kind_op, self.roots) or "")

    def test_export_root_exact_refused_for_non_owned_op(self) -> None:
        plain = dataclasses.replace(self.owned_op(self.roots[2]), desired={"mode": "0755"})
        self.assertIsNotNone(_pim._realpath_containment_refusal(plain, self.roots))

    def test_receipt_paths_exact_root_rules(self) -> None:
        def mutation(target, before):
            return _pim.MutationRecord("op", "mkdir", str(target), "installer", True, before, None)

        for before in ({"state": "absent", "nofollow_root": "/"}, {"state": "absent"}):
            self.assertIsNone(_pim._rollback_containment_refusal(mutation(self.roots[2], before), self.roots))
            for root in self.roots[:2]:
                self.assertIsNotNone(_pim._rollback_containment_refusal(mutation(root, before), self.roots))
        other = _pim.MutationRecord("op", "atomic-write", self.roots[2], "installer", True, {"state": "absent"}, None)
        self.assertIsNotNone(_pim._rollback_containment_refusal(other, self.roots))

    def test_receipt_exact_root_refused_unless_before_absent(self) -> None:
        # a crafted receipt with before.state directory/file must not reach the legacy
        # chmod / mkstemp rollback branches at the export root
        for before in ({"state": "directory", "mode": "0755"}, {"state": "file"}, {"state": "directory", "nofollow_root": "/"}):
            m = _pim.MutationRecord("op", "mkdir", self.roots[2], "installer", True, before, None)
            self.assertIsNotNone(_pim._rollback_containment_refusal(m, self.roots), before)


class NoReadThroughLinkTests(DiskFixture):
    """Review fix 3: a planted link must be refused before anything reads the file it points at."""

    def test_link_to_file_refused_and_file_never_read(self) -> None:
        root_op, seat_op, _ = self.ops()
        self.apply(root_op)
        secret = self.base / "secret.txt"
        secret.write_bytes(b"TOP-SECRET-BYTES")
        Path(seat_op.target).symlink_to(secret)
        opened: list[str] = []
        real_read_bytes = Path.read_bytes

        def spy_read(self_, *a, **k):
            opened.append(str(self_))
            return real_read_bytes(self_, *a, **k)

        with mock.patch.object(Path, "read_bytes", spy_read):
            with mock.patch("builtins.open", side_effect=AssertionError("open() called")):
                with self.assertRaises(OperationFailed) as ctx:
                    self.apply(seat_op)
        self.assertNotIn("TOP-SECRET", str(ctx.exception))
        self.assertFalse([p for p in opened if p == str(secret) or p == str(seat_op.target)])
        self.assertTrue(os.path.islink(seat_op.target))
        self.assertEqual(secret.read_bytes(), b"TOP-SECRET-BYTES")

    def test_lstat_snapshot_never_reads_content(self) -> None:
        f = self.base / "f"
        f.write_bytes(b"x")
        link = self.base / "l"
        link.symlink_to(f)
        self.assertEqual(_pim._lstat_snapshot(link), {"state": "other"})
        self.assertEqual(_pim._lstat_snapshot(f), {"state": "other"})
        self.assertEqual(_pim._lstat_snapshot(self.base / "nope"), {"state": "absent"})


class RollbackNoReadThroughLinkTests(DiskFixture):
    """Review fix: can_rollback on an owned mkdir receipt must lstat, never follow or read a link."""

    def test_can_rollback_link_to_file_never_reads(self) -> None:
        root_op, seat_op, _ = self.ops()
        self.apply(root_op)
        m = self.apply(seat_op)
        os.rmdir(seat_op.target)
        secret = self.base / "secret.txt"
        secret.write_bytes(b"TOP-SECRET-BYTES")
        Path(seat_op.target).symlink_to(secret)
        opened: list[str] = []
        real_read_bytes = Path.read_bytes

        def spy_read(self_, *a, **k):
            opened.append(str(self_))
            return real_read_bytes(self_, *a, **k)

        backend = LocalHostBackend.__new__(LocalHostBackend)
        with mock.patch.object(Path, "read_bytes", spy_read):
            with mock.patch("builtins.open", side_effect=AssertionError("open() called")):
                result = backend.can_rollback(m)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.classification, "drifted")
        self.assertFalse([p for p in opened if p in (str(secret), str(seat_op.target))])
        self.assertTrue(os.path.islink(seat_op.target))

    def test_create_then_rollback_round_trip(self) -> None:
        root_op, seat_op, _ = self.ops()
        self.apply(root_op)
        backend = LocalHostBackend.__new__(LocalHostBackend)
        m = backend._simple_mutation(seat_op)
        self.assertTrue(m.changed)
        check = backend.can_rollback(m)
        self.assertEqual((check.ok, check.classification), (True, "owned"))
        backend.rollback_mutation(m, allowed_roots=(str(self.base),))
        self.assertFalse(Path(seat_op.target).exists())


if __name__ == "__main__":
    unittest.main()
