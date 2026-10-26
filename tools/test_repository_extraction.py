"""tools/test_repository_extraction.py — T-3183 §5 extraction matrix.

Real temp-git extraction of a monorepo prefix into its own repo, on
`git_fixture.BubbleGit` fixtures with `functional=True` inside the bubble: the
canonical cut, the dual-canonical + reverse-write refusals, old-path disposition,
rollback, history/authorship/data preservation, the keep-together no-op, and the
scope guard (no project formation / organ enable).
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

import yaml

from tools.assembly_resolver import AssemblyResolverError, RefusalKind
from tools.component_catalogue import parse_repository_boundary_decision
from tools.git_fixture import GIT_ENV, BubbleSafeCase, git as _git
from tools.repository_extraction import (
    CANONICAL_MARKER,
    TOMBSTONE,
    assert_single_canonical_source,
    execute_extraction,
    plan_extraction,
)

_PREFIX = "lib/widget"
_COMPONENT = "widget"
_PROJECT = "alpha-project"
_DECISION_ID = "repository-boundary:widget"
_NEW_LOGICAL = "widget-repo"


class _ExtractionBase(BubbleSafeCase):
    def setUp(self) -> None:
        super().setUp()
        self.src = Path(self.path("monorepo"))
        _git("init", "-b", "main", str(self.src))
        (self.src / _PREFIX).mkdir(parents=True)
        (self.src / "other").mkdir()
        # The monorepo is the (old) canonical source before extraction. The marker
        # lives at the repo ROOT (extraction metadata), NOT inside the prefix, so
        # `git subtree split --prefix=<prefix>` never carries it into the new repo.
        (self.src / CANONICAL_MARKER).write_text("monorepo\n", encoding="utf-8")
        (self.src / _PREFIX / "a.txt").write_text("w1\n", encoding="utf-8")
        (self.src / "other" / "z.txt").write_text("o1\n", encoding="utf-8")
        _git("add", "-A", cwd=self.src)
        self._commit_as("Alice", "alice@x.invalid", "Alice", "alice@x.invalid", "c1")
        (self.src / _PREFIX / "b.txt").write_text("w2\n", encoding="utf-8")
        (self.src / _PREFIX / "data.bin").write_bytes(b"\x00DATA\x01")
        _git("add", "-A", cwd=self.src)
        self._commit_as("Bob", "bob@x.invalid", "Carol", "carol@x.invalid", "c2")

        self.new_remote = self.path("widget.git")
        self.work_root = Path(self.path("work"))
        self.receipt_root = Path(self.path("receipts"))
        self.new_work = self.work_root / "new-canonical"
        self.old_prefix = self.src / _PREFIX
        self.old_marker = self.src / CANONICAL_MARKER  # canonical marker at monorepo root

    def _commit_as(self, an: str, ae: str, cn: str, ce: str, msg: str) -> None:
        env = {
            **GIT_ENV,
            "GIT_AUTHOR_NAME": an,
            "GIT_AUTHOR_EMAIL": ae,
            "GIT_COMMITTER_NAME": cn,
            "GIT_COMMITTER_EMAIL": ce,
        }
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(self.src),
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

    def _decision_dict(self, *, decision: str = "extract", old_disposition=None) -> dict:
        data = {
            "schema_version": 1,
            "decision_id": _DECISION_ID,
            "project_id": _PROJECT,
            "component_id": _COMPONENT,
            "current_source": {"logical_repo": "monorepo", "path": _PREFIX},
            "contract": {
                "id": "widget-contract",
                "version": "1.0.0",
                "schema_refs": ["schema:widget"],
                "test_evidence_refs": ["evidence:widget-tests"],
            },
            "independence_signals": ["independent-cadence"],
            "keep_together_reasons": [],
            "decision": decision,
            "migration": {
                "history": ["history:full"],
                "data": ["data:widget"],
                "source_cutover": ["cutover:once"],
                "old_path_disposition": old_disposition or ["tombstone:redirect"],
                "rollback": ["rollback:restore-marker"],
            },
            "operational_contract": {
                "install": ["op:install"],
                "update": ["op:update"],
                "configure": ["op:configure"],
                "monitor": ["op:monitor"],
                "remove": ["op:remove"],
            },
            "compatibility_impact": {
                "affected_assemblies": ["assembly:pilot"],
                "catalogue_changes": ["change:logical-repo"],
            },
            "implication_acknowledgements": ["impl:ack"],
            "upkeep_estimate_refs": ["upkeep:est"],
            "authority": {
                "decision_ref": "decision:ratified",
                "reviewer_evidence_refs": ["review:evidence"],
            },
            "re_evaluate_when": ["when:cadence-diverges"],
        }
        if decision == "keep-together":
            data["keep_together_reasons"] = ["shared-release-identity"]
            data["independence_signals"] = []
        return data

    def _decision(self, **kw):
        data = self._decision_dict(**kw)
        path = Path(self.path("decisions", f"{_COMPONENT}.yaml"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return parse_repository_boundary_decision(
            path, project_ids={_PROJECT}, component_ids={_COMPONENT}
        )

    def _plan(self, **kw):
        return plan_extraction(
            self._decision(**kw),
            source_repo=self.src,
            new_remote=self.new_remote,
            new_logical_repo=_NEW_LOGICAL,
        )

    def _execute(self, plan, *, functional=True, fault_after=None):
        return execute_extraction(
            plan,
            functional=functional,
            receipt_root=self.receipt_root,
            work_root=self.work_root,
            fault_after=fault_after,
        )


class TestExtraction(_ExtractionBase):
    def test_extract_cuts_canonical_source_once(self) -> None:
        receipt = self._execute(self._plan())
        self.assertEqual(receipt.outcome, "extracted")
        self.assertEqual(receipt.migrated_commits, 2)
        self.assertEqual(receipt.new_logical_repo, _NEW_LOGICAL)
        # NEW repo canonical; OLD prefix marker removed; exactly one canonical.
        self.assertEqual(
            (self.new_work / CANONICAL_MARKER).read_text(encoding="utf-8"), _NEW_LOGICAL + "\n"
        )
        self.assertFalse(self.old_marker.exists())
        assert_single_canonical_source([self.src, self.new_work])  # does not raise

    def test_dual_canonical_source_refused(self) -> None:
        a = Path(self.path("repo-a"))
        b = Path(self.path("repo-b"))
        a.mkdir()
        b.mkdir()
        (a / CANONICAL_MARKER).write_text("a\n", encoding="utf-8")
        (b / CANONICAL_MARKER).write_text("b\n", encoding="utf-8")
        with self.assertRaises(AssemblyResolverError) as ctx:
            assert_single_canonical_source([a, b])
        self.assertEqual(ctx.exception.refusal.kind, RefusalKind.DUAL_CANONICAL_SOURCE)

    def test_reverse_write_at_old_path_refused(self) -> None:
        self._execute(self._plan())
        # Post-cutover, re-marking the OLD source canonical must refuse.
        self.old_marker.write_text("monorepo\n", encoding="utf-8")
        with self.assertRaises(AssemblyResolverError) as ctx:
            assert_single_canonical_source([self.src, self.new_work])
        self.assertEqual(ctx.exception.refusal.kind, RefusalKind.DUAL_CANONICAL_SOURCE)

    def test_old_path_disposition_applied(self) -> None:
        self._execute(self._plan(old_disposition=["tombstone:redirect", "redirect:widget-repo"]))
        tombstone = self.old_prefix / TOMBSTONE
        self.assertTrue(tombstone.is_file())
        body = tombstone.read_text(encoding="utf-8")
        self.assertIn(f"extracted-to: {_NEW_LOGICAL}", body)
        self.assertIn("redirect:widget-repo", body)

    def test_extraction_rollback_restores_old_canonical(self) -> None:
        receipt = self._execute(self._plan(), fault_after="source-cutover")
        self.assertEqual(receipt.outcome, "rolled-back")
        self.assertTrue(receipt.retained_diagnostics)
        # old canonical marker restored; new-repo working tree discarded.
        self.assertTrue(self.old_marker.is_file())
        self.assertEqual(self.old_marker.read_text(encoding="utf-8"), "monorepo\n")
        self.assertFalse(self.new_work.exists())
        # (a) MAJOR-1: the new bare remote carries NO canonical marker after
        # rollback (the marker commit+push is deferred past source-cutover).
        remote_tree = _git(
            "ls-tree", "-r", "--name-only", "main", cwd=Path(self.new_remote)
        ).stdout.split()
        self.assertNotIn(CANONICAL_MARKER, remote_tree)
        # exactly ONE canonical source remains (the old monorepo).
        assert_single_canonical_source([self.src, self.new_work])
        # (b) a re-run against the SAME new_remote succeeds (retry-safe).
        receipt2 = self._execute(self._plan())
        self.assertEqual(receipt2.outcome, "extracted")
        self.assertFalse(self.old_marker.exists())
        self.assertEqual(
            (self.new_work / CANONICAL_MARKER).read_text(encoding="utf-8"), _NEW_LOGICAL + "\n"
        )

    def test_extract_preserves_history_authorship_and_data(self) -> None:
        prefix_log_before = (
            _git("log", "--format=%an|%ae|%cn|%ce|%s", "--", _PREFIX, cwd=self.src)
            .stdout.strip()
            .splitlines()
        )
        data_before = (self.src / _PREFIX / "data.bin").read_bytes()
        self._execute(self._plan())
        # per-commit author AND committer preserved (subtree split), minus the
        # executor's own canonical-marker commit at the new HEAD.
        new_log = [
            line
            for line in _git("log", "--format=%an|%ae|%cn|%ce|%s", cwd=self.new_work)
            .stdout.strip()
            .splitlines()
            if "mark canonical-source" not in line
        ]
        self.assertEqual(new_log, prefix_log_before)
        self.assertIn("Bob|bob@x.invalid|Carol|carol@x.invalid|c2", new_log)
        # migrated data byte-for-byte at the new repo root.
        self.assertEqual((self.new_work / "data.bin").read_bytes(), data_before)
        self.assertEqual((self.new_work / "a.txt").read_text(encoding="utf-8"), "w1\n")

    def test_keep_together_is_noop(self) -> None:
        plan = self._plan(decision="keep-together")
        self.assertEqual(plan.steps, ())
        receipt = self._execute(plan)
        self.assertEqual(receipt.outcome, "kept-together")
        # no mutation: old canonical intact, no new repo, no tombstone.
        self.assertTrue(self.old_marker.is_file())
        self.assertFalse(self.new_work.exists())
        self.assertFalse(Path(self.new_remote).exists())
        self.assertFalse((self.old_prefix / TOMBSTONE).exists())

    def test_extraction_does_not_form_project_or_enable_organ(self) -> None:
        receipt = self._execute(self._plan())
        self.assertEqual(receipt.outcome, "extracted")
        # The only catalogue change is the component's logical_repo (additive).
        sealed = list(self.receipt_root.rglob("*.extraction.json"))
        self.assertEqual(len(sealed), 1)
        receipt_json = json.loads(sealed[0].read_bytes())
        self.assertEqual(receipt_json["new_logical_repo"], _NEW_LOGICAL)
        # scope guard: no project-formation / organ-enable artifact anywhere.
        self.assertFalse(any("project" in k or "organ" in k for k in receipt_json))
        for root in (self.work_root, self.receipt_root, self.src):
            self.assertEqual(list(root.rglob("projects")), [])
            self.assertEqual(list(root.rglob("organs")), [])


if __name__ == "__main__":
    unittest.main()
