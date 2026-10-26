"""T-5496: a ticket close must not turn `registry validate` red.

`resolve` / `drop` / `close-*` refuse when registry/systems.yaml cites the ticket as
the owner of an open gap, unless `--successor T-NNNN` re-cites those cells (minimal
textual edit + re-render, same commit).

Run: python3 -m unittest tools.test_ticket_registry_gate -v
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import _git_utils, ticket
from tools.germinate.registry import render
from tools.germinate.registry.load import loads
from tools.germinate.registry.validate import check_refs

REGISTRY = """\
# registry header comment
systems:
  - kind: component
    id: comp-a
    title: Component A
    steward: orchestrator
    legacy_coverage:
      install:
        status: missing   # keep this comment
        gap: nothing here
        ticket: T-9001
      remove:
        status: partial
        gap: half here
        ticket: "T-9001"
      update:
        status: missing
        gap: unrelated
        ticket: T-9002
    adoption_gaps:
      plan_configure:
        gap: no typed configure leg
        ticket: T-9001
  - kind: component
    id: comp-b
    title: Component B
    steward: orchestrator
    legacy_coverage:
      install:
        status: missing
        gap: other
        ticket: T-9003
"""


def _write_ticket(path: Path, tid: str, state: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---", f"id: {tid}", "created: 2026-10-01T00:00:00Z", "requester: orchestrator",
                "target: role:head_engineering_orchestrator", f"state: {state}",
                "parent_ticket: null", "depends_on: []", "---", "", f"# {tid} title", "",
                "## Ask", "", "## Context", "", "## Transition log", "",
                "- 2026-10-01T00:00:00Z [orchestrator] filed in inbox", "",
            ]
        ),
        encoding="utf-8",
    )


class TestRegistryCloseGate(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / "tickets" / s).mkdir(parents=True)
        (self.root / "registry").mkdir()
        self.reg = self.root / "registry" / "systems.yaml"
        self.reg.write_text(REGISTRY, encoding="utf-8")
        (self.root / "repos.yaml").write_text("repos: []\n", encoding="utf-8")
        for tid, state in [("T-9001", "active"), ("T-9002", "active"), ("T-9003", "active"),
                           ("T-9004", "inbox"), ("T-9005", "done"), ("T-9006", "active")]:
            _write_ticket(self.root / "tickets" / state / f"{tid}.md", tid, state)
        for target, val in [("REPO_ROOT", self.root), ("TICKETS_ROOT", self.root / "tickets")]:
            p = patch.object(ticket, target, val)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(_git_utils, "REPO_ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)

        self.committed: list[list[Path]] = []

        def fake_git(*args, **kw):
            if args[0] == "mv":
                shutil.move(str(self.root / args[1]), str(self.root / args[2]))
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        for name, fake in [
            ("_git", fake_git),
            ("_pull_if_needed", lambda *a, **k: None),
            ("_commit_and_push", lambda paths, *a, **k: self.committed.append(list(paths))),
            ("_record_appetite_outcome", lambda *a, **k: None),
        ]:
            p = patch.object(ticket, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def _resolve(self, tid: str, successor: str | None = None) -> int:
        return ticket.cmd_resolve(argparse.Namespace(
            ticket=tid, with_summary=None, no_push=True, receipt=None, successor=successor))

    def _drop(self, tid: str, successor: str | None = None) -> int:
        return ticket.cmd_drop(argparse.Namespace(
            ticket=tid, dropped_reason="abandoned", no_push=True, successor=successor))

    def _gap_errors(self) -> list[str]:
        doc = loads(self.reg.read_text(encoding="utf-8"))
        return [str(f) for f in check_refs(doc, self.root) if "cannot close" in str(f)]

    def test_refusal_lists_cells_and_changes_nothing(self) -> None:
        with self.assertRaises(ticket.TicketRefusal) as ctx:
            self._resolve("T-9001")
        msg = str(ctx.exception)
        for cell in ("comp-a.legacy_coverage.install", "comp-a.legacy_coverage.remove",
                     "comp-a.adoption_gaps.plan_configure"):
            self.assertIn(cell, msg)
        self.assertIn("--successor", msg)
        self.assertNotIn("comp-a.legacy_coverage.update", msg)
        self.assertTrue((self.root / "tickets/active/T-9001.md").exists())
        self.assertEqual(self.committed, [])
        self.assertEqual(self.reg.read_text(encoding="utf-8"), REGISTRY)

    def test_drop_is_refused_too(self) -> None:
        with self.assertRaises(ticket.TicketRefusal):
            self._drop("T-9001")
        self.assertTrue((self.root / "tickets/active/T-9001.md").exists())

    def test_successor_recites_rerenders_and_validator_passes(self) -> None:
        self.assertEqual(self._gap_errors(), [])
        self.assertEqual(self._resolve("T-9001", successor="T-9004"), 0)
        text = self.reg.read_text(encoding="utf-8")
        # only the three T-9001 values changed; comments, quoting, others intact
        expected = (REGISTRY.replace("ticket: T-9001", "ticket: T-9004")
                    .replace('ticket: "T-9001"', 'ticket: "T-9004"'))
        self.assertEqual(text, expected)
        self.assertIn("# keep this comment", text)
        self.assertEqual(self._gap_errors(), [])
        rendered = (self.root / render.RENDERED_REL).read_text(encoding="utf-8")
        self.assertEqual(rendered, render.render_matrix(loads(text)))
        (paths,) = self.committed
        self.assertIn(self.reg, paths)
        self.assertIn(self.root / render.RENDERED_REL, paths)
        self.assertTrue((self.root / "tickets/done/T-9001.md").exists())

    def test_drop_with_successor(self) -> None:
        self.assertEqual(self._drop("T-9001", successor="T-9004"), 0)
        self.assertEqual(self._gap_errors(), [])

    def test_successor_done_missing_or_self_refused(self) -> None:
        for bad in ("T-9005", "T-9999", "T-9001"):
            with self.subTest(successor=bad), self.assertRaises(ticket.TicketRefusal):
                self._resolve("T-9001", successor=bad)
        self.assertTrue((self.root / "tickets/active/T-9001.md").exists())
        self.assertEqual(self.reg.read_text(encoding="utf-8"), REGISTRY)

    def test_uncited_ticket_resolves_as_before(self) -> None:
        self.assertEqual(self._resolve("T-9006"), 0)
        self.assertEqual(self.reg.read_text(encoding="utf-8"), REGISTRY)
        (paths,) = self.committed
        self.assertNotIn(self.reg, paths)

    def test_successor_flag_ignored_when_ticket_not_cited(self) -> None:
        self.assertEqual(self._resolve("T-9006", successor="T-9004"), 0)
        self.assertEqual(self.reg.read_text(encoding="utf-8"), REGISTRY)

    def test_no_registry_file_is_fine(self) -> None:
        self.reg.unlink()
        self.assertEqual(self._resolve("T-9001"), 0)

    def test_missing_yaml_fails_open_for_plain_close(self) -> None:
        with patch.dict("sys.modules", {"yaml": None, "tools.ticket_registry_gate": None}):
            self.assertEqual(self._resolve("T-9001"), 0)
        self.assertTrue((self.root / "tickets/done/T-9001.md").exists())

    def test_missing_yaml_refuses_explicit_successor(self) -> None:
        with patch.dict("sys.modules", {"yaml": None, "tools.ticket_registry_gate": None}):
            with self.assertRaises(ticket.TicketRefusal) as ctx:
                self._resolve("T-9001", successor="T-9004")
        self.assertIn("--successor", str(ctx.exception))
        self.assertTrue((self.root / "tickets/active/T-9001.md").exists())

    def test_unresolvable_mark_is_a_refusal_not_a_traceback(self) -> None:
        from tools import ticket_registry_gate as gate

        with patch.object(gate, "_scalar_spans", side_effect=ValueError("no mark")):
            with self.assertRaises(ticket.TicketRefusal) as ctx:
                self._resolve("T-9001", successor="T-9004")
        self.assertIn("edit registry/systems.yaml by hand", str(ctx.exception))
        self.assertTrue((self.root / "tickets/active/T-9001.md").exists())
        self.assertEqual(self.reg.read_text(encoding="utf-8"), REGISTRY)

    def test_render_failure_is_a_refusal(self) -> None:
        from tools import ticket_registry_gate as gate

        with patch.object(gate.render, "render_matrix", side_effect=RuntimeError("boom")):
            with self.assertRaises(ticket.TicketRefusal):
                self._resolve("T-9001", successor="T-9004")

    def test_cli_accepts_successor(self) -> None:
        parser = ticket._build_parser()
        for argv in (["resolve", "T-1", "--successor", "T-2"],
                     ["close-noodle", "T-1", "--successor", "T-2"],
                     ["close-action", "T-1", "--successor", "T-2"],
                     ["drop", "T-1", "--dropped-reason", "x", "--successor", "T-2"]):
            self.assertEqual(parser.parse_args(argv).successor, "T-2")


REGISTRY_UNRENDERED = """\
systems:
  - kind: component
    id: comp-c
    title: Component C
    steward: orchestrator
    legacy_coverage:
      install:
        status: missing
        gap: shown in the matrix
        ticket: T-9100
    adoption_gaps:
      plan_configure:
        gap: adoption_gaps cells are NOT rendered in system-coverage.md
        ticket: T-9001
"""


class TestCloseAtomicity(unittest.TestCase):
    """T-5511: against a REAL git repo (the mocked class above never reaches the scope guard)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        ident = {f"GIT_{w}_{k}": v for w in ("AUTHOR", "COMMITTER")
                 for k, v in (("NAME", "t"), ("EMAIL", "t@example.invalid"))}
        p = patch.dict(os.environ, ident)
        p.start()
        self.addCleanup(p.stop)
        for s in ticket.STATES:
            (self.root / "tickets" / s).mkdir(parents=True)
        (self.root / "registry").mkdir()
        self.reg = self.root / "registry" / "systems.yaml"
        self.reg.write_text(REGISTRY_UNRENDERED, encoding="utf-8")
        self.rendered = self.root / render.RENDERED_REL
        self.rendered.write_text(render.render_matrix(loads(REGISTRY_UNRENDERED)), encoding="utf-8")
        (self.root / "repos.yaml").write_text("repos: []\n", encoding="utf-8")
        for tid, state in [("T-9001", "active"), ("T-9004", "inbox"), ("T-9100", "active"), ("T-9101", "active")]:
            _write_ticket(self.root / "tickets" / state / f"{tid}.md", tid, state)
        # a dependant that the ORG-013 cascade edits in place when T-9001 closes
        dep = self.root / "tickets" / "inbox" / "T-9008.md"
        _write_ticket(dep, "T-9008", "inbox")
        dep.write_text(dep.read_text(encoding="utf-8").replace(
            "depends_on: []", "depends_on: [T-9001]"), encoding="utf-8")
        for target, val in [("REPO_ROOT", self.root), ("TICKETS_ROOT", self.root / "tickets")]:
            p = patch.object(ticket, target, val)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(_git_utils, "REPO_ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)
        (self.root / "tickets" / "README.md").write_text(
            "# Tickets\n\n## Current state\n\n| stale |\n\n## After\n", encoding="utf-8")
        self._run("init", "-q", "-b", "main")
        self._run("add", "-A")
        self._run("commit", "-q", "-m", "fixture")

        def real_git(*args, cwd=None):
            return subprocess.run(
                ["git", *args], cwd=cwd or self.root, capture_output=True, text=True, check=False)

        for name, fake in [("_git", real_git), ("_pull_if_needed", lambda *a, **k: None),
                           ("_record_appetite_outcome", lambda *a, **k: None)]:
            p = patch.object(ticket, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def _run(self, *args: str) -> str:
        r = subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                           text=True, check=True)
        return r.stdout

    def _resolve(self, tid: str, successor: str | None = None) -> int:
        return ticket.cmd_resolve(argparse.Namespace(
            ticket=tid, with_summary=None, no_push=True, receipt=None, successor=successor))

    def _snapshot(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in sorted(self.root.rglob("*")) if p.is_file() and ".git" not in p.parts}

    def _stage_stray(self) -> None:
        (self.root / "stray.txt").write_text("someone else's staged file\n", encoding="utf-8")
        self._run("add", "stray.txt")  # staged outside our scope -> the scope guard refuses

    def test_unrendered_cell_commits_registry_without_rendered_matrix(self) -> None:
        before = self.rendered.read_bytes()
        self.assertEqual(self._resolve("T-9001", successor="T-9004"), 0)
        self.assertEqual(self.rendered.read_bytes(), before)
        files = set(self._run("show", "--name-only", "--format=", "--no-renames", "HEAD").split())
        self.assertIn("registry/systems.yaml", files)
        self.assertNotIn("registry/system-coverage.md", files)
        self.assertIn("tickets/done/T-9001.md", files)
        self.assertEqual(self._run("status", "--porcelain"), "")

    def test_scope_guard_refusal_after_writes_restores_the_tree(self) -> None:
        self._stage_stray()
        before = self._snapshot()
        head = self._run("rev-parse", "HEAD")
        with self.assertRaises(ticket.TicketError) as ctx:
            self._resolve("T-9001", successor="T-9004")
        self.assertIn("scope guard refused", str(ctx.exception))
        self.assertEqual(self._snapshot(), before)  # bytes + no stray files
        self.assertTrue((self.root / "tickets/active/T-9001.md").exists())
        self.assertFalse((self.root / "tickets/done/T-9001.md").exists())
        self.assertEqual(self._run("rev-parse", "HEAD"), head)
        # the guard unstages the foreign file (pre-existing); nothing of ours remains
        self.assertEqual(self._run("status", "--porcelain"), "?? stray.txt\n")

    def test_refusal_on_plain_close_also_restores_the_tree(self) -> None:
        """Rollback is not registry-specific: any scope-guard refusal unwinds."""
        self._stage_stray()
        before = self._snapshot()
        with self.assertRaises(ticket.TicketError):
            self._resolve("T-9101")
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._run("status", "--porcelain"), "?? stray.txt\n")

    def test_rollback_never_touches_paths_the_transition_did_not_write(self) -> None:
        """A concurrent writer (editor, another seat) is not covered by the mutation lock:
        its edits made mid-transition must survive a rollback."""
        dirty = self.root / "tickets" / "inbox" / "T-9004.md"
        dirty.write_text(dirty.read_text(encoding="utf-8") + "\nuncommitted before\n", encoding="utf-8")
        self._stage_stray()
        clean_tracked = self.root / "repos.yaml"
        fresh = self.root / "fresh-untracked.txt"
        real_cascade = ticket._cascade_unblock_dependants

        def cascade_with_concurrent_writers(parent_tid):
            clean_tracked.write_text("repos: [edited-by-someone-else]\n", encoding="utf-8")
            fresh.write_text("created mid-transition\n", encoding="utf-8")
            dirty.write_text(dirty.read_text(encoding="utf-8") + "edited further mid-transition\n",
                             encoding="utf-8")
            return real_cascade(parent_tid)

        with patch.object(ticket, "_cascade_unblock_dependants", cascade_with_concurrent_writers):
            with self.assertRaises(ticket.TicketError):
                self._resolve("T-9001", successor="T-9004")
        self.assertEqual(clean_tracked.read_text(encoding="utf-8"), "repos: [edited-by-someone-else]\n")
        self.assertEqual(fresh.read_text(encoding="utf-8"), "created mid-transition\n")
        self.assertTrue(dirty.read_text(encoding="utf-8").endswith(
            "uncommitted before\nedited further mid-transition\n"))
        self.assertTrue((self.root / "tickets/active/T-9001.md").exists())
        self.assertFalse((self.root / "tickets/done/T-9001.md").exists())

    def test_rollback_restores_index_for_a_ticket_with_uncommitted_edits(self) -> None:
        path = self.root / "tickets" / "active" / "T-9101.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nhand edit\n", encoding="utf-8")
        edited = path.read_bytes()
        # fail AFTER the mv but without the scope guard's own index reset
        with patch.object(ticket, "_cascade_unblock_dependants",
                          side_effect=ticket.TicketError("boom")):
            with self.assertRaises(ticket.TicketError):
                self._resolve("T-9101")
        self.assertEqual(path.read_bytes(), edited)
        # unstaged modification only: no staged deletion/addition left behind
        self.assertEqual(self._run("status", "--porcelain"), " M tickets/active/T-9101.md\n")


if __name__ == "__main__":
    unittest.main()
