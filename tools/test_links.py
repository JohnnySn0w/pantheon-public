#!/usr/bin/env python3
"""Hermetic tests for tools/links.py (T-1087).

Builds a synthetic ticket tree + adr/ in a tmpdir — no dependency on the live
repo's data — and exercises every direct-read + derived-edge query. The git-log
commit query is covered by a tiny throwaway git repo.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import links  # noqa: E402


def _write_ticket(tickets_root: Path, state: str, tid: str, *, frontmatter: dict, log: list[str]) -> Path:
    d = tickets_root / state
    d.mkdir(parents=True, exist_ok=True)
    fm_lines = ["---"]
    for k, v in frontmatter.items():
        if isinstance(v, list):
            fm_lines.append(f"{k}: [{', '.join(v)}]")
        else:
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    body = [f"\n# {tid} title\n", "## Transition log\n"] + log
    text = "\n".join(fm_lines) + "\n" + "\n".join(body) + "\n"
    p = d / f"{tid}.md"
    p.write_text(text, encoding="utf-8")
    return p


class LinksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tickets = self.root / "tickets"
        self.adr = self.root / "adr"
        self.notes = self.root / "notes"
        self.adr.mkdir(parents=True, exist_ok=True)
        self.notes.mkdir(parents=True, exist_ok=True)

        # T-0001 parent; T-0002 child + depends on T-0003; T-0003 blocker.
        _write_ticket(
            self.tickets, "done", "T-0001",
            frontmatter={"id": "T-0001", "parent_ticket": "None", "depends_on": [],
                         "labels": ["ORG-099", "misc"], "ratifiable_files": ["adr/ORG-099-x.md"]},
            log=["- 2026-06-01T00:00:00Z [recovery] filed", "- 2026-06-02T00:00:00Z [grooming] stale"],
        )
        _write_ticket(
            self.tickets, "active", "T-0002",
            frontmatter={"id": "T-0002", "parent_ticket": "T-0001", "depends_on": ["T-0003"],
                         "labels": ["ORG-099"], "ratifiable_files": []},
            log=["- 2026-06-03T00:00:00Z [claude-fallback] claimed",
                 "- 2026-06-04T00:00:00Z [specialist] reviewed per msg_777"],
        )
        _write_ticket(
            self.tickets, "inbox", "T-0003",
            frontmatter={"id": "T-0003", "parent_ticket": "None", "depends_on": [],
                         "labels": [], "ratifiable_files": []},
            log=["- 2026-06-05T00:00:00Z [orchestrator] filed citing msg_777 and msg_888"],
        )
        (self.adr / "ORG-099-x.md").write_text("# ORG-099\n", encoding="utf-8")
        (self.notes / "n.md").write_text("see msg_777 here\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # --- direct frontmatter reads ---
    def test_parents(self):
        self.assertEqual(links.get_parents("T-0002", self.tickets), ["T-0001"])
        self.assertEqual(links.get_parents("T-0001", self.tickets), [])  # "None" filtered

    def test_children(self):
        self.assertEqual(links.get_children("T-0001", self.tickets), ["T-0002"])
        self.assertEqual(links.get_children("T-0003", self.tickets), [])

    def test_deps_and_blocks(self):
        self.assertEqual(links.get_deps("T-0002", self.tickets), ["T-0003"])
        self.assertEqual(links.get_blocks("T-0003", self.tickets), ["T-0002"])
        self.assertEqual(links.get_blocks("T-0001", self.tickets), [])

    def test_ratifies(self):
        self.assertEqual(links.get_ratifies("T-0001", self.tickets), ["adr/ORG-099-x.md"])
        self.assertEqual(links.get_ratifies("T-0002", self.tickets), [])

    def test_actors_distinct_in_order(self):
        # grooming actor IS reported by `actors` (it's a real transition actor);
        # only `find_last_non_grooming_entry` (in the groomer) filters it.
        self.assertEqual(links.get_actors("T-0001", self.tickets), ["recovery", "grooming"])
        self.assertEqual(links.get_actors("T-0002", self.tickets), ["claude-fallback", "specialist"])

    def test_org_labels(self):
        self.assertEqual(links.get_org_labels("T-0001", self.tickets), ["ORG-099"])
        self.assertEqual(links.get_org_labels("T-0003", self.tickets), [])

    # --- derived reverse edges ---
    def test_touched_by_alias_expansion(self):
        # recovery query must catch BOTH [recovery] (T-0001) and [claude-fallback] (T-0002).
        self.assertEqual(links.get_touched_by("recovery", self.tickets), ["T-0001", "T-0002"])
        # claude-fallback (account name) expands to the same group.
        self.assertEqual(links.get_touched_by("claude-fallback", self.tickets), ["T-0001", "T-0002"])
        # specialist only touched T-0002.
        self.assertEqual(links.get_touched_by("specialist", self.tickets), ["T-0002"])
        # unknown/special actor matches only itself.
        self.assertEqual(links.get_touched_by("grooming", self.tickets), ["T-0001"])

    def test_adr_tickets_label_only(self):
        # Forward: tickets with the ORG-099 *label* (not body mentions).
        self.assertEqual(links.get_adr_tickets("ORG-099", self.tickets), ["T-0001", "T-0002"])
        self.assertEqual(links.get_adr_tickets("ORG-100", self.tickets), [])

    def test_find_adr_file(self):
        self.assertEqual(links.find_adr_file("ORG-099", self.adr, self.root), "adr/ORG-099-x.md")
        self.assertIsNone(links.find_adr_file("ORG-100", self.adr, self.root))

    def test_cites_scans_tickets_and_notes(self):
        files = links.get_cites("msg_777", self.tickets, self.notes, self.root)
        # T-0002 (per msg_777), T-0003 (citing msg_777), notes/n.md all appear.
        joined = "\n".join(files)
        self.assertIn("T-0002.md", joined)
        self.assertIn("T-0003.md", joined)
        self.assertIn("n.md", joined)
        # msg_888 appears in exactly one source: T-0003.
        files888 = links.get_cites("msg_888", self.tickets, self.notes, self.root)
        self.assertEqual(files888, ["tickets/inbox/T-0003.md"])

    def test_alias_group_unknown_self(self):
        self.assertEqual(links.alias_group("recovery"), {"recovery", "claude-fallback"})
        self.assertEqual(links.alias_group("grooming"), {"grooming"})

    # --- rebuild-index snapshot ---
    def test_rebuild_index_records(self):
        recs = links.build_index_records(self.tickets)
        types = {(r["type"], r["from"], r["to"]) for r in recs}
        self.assertIn(("parent_ticket", "T-0002", "T-0001"), types)
        self.assertIn(("depends_on", "T-0002", "T-0003"), types)
        self.assertIn(("ratifies", "T-0001", "adr/ORG-099-x.md"), types)
        self.assertIn(("governed_by", "T-0001", "ORG-099"), types)
        self.assertIn(("actor", "recovery", "T-0001"), types)
        self.assertIn(("cites", "T-0003", "msg_888"), types)

    def test_rebuild_index_writes_file(self):
        out = self.root / "links.index.jsonl"
        n = links.rebuild_index(out, self.tickets)
        self.assertTrue(out.is_file())
        lines = [l for l in out.read_text().splitlines() if l.strip()]
        self.assertEqual(n, len(lines))
        for l in lines:
            json.loads(l)  # each line valid JSON

    # --- neighbors aggregation ---
    def test_neighbors_shape(self):
        # Use a repo_root with no git history → commits empty, rest from frontmatter.
        data = links.neighbors("T-0002", self.tickets, repo_root=self.root)
        self.assertEqual(data["parents"], ["T-0001"])
        self.assertEqual(data["depends_on"], ["T-0003"])
        self.assertEqual(data["governed_by"], ["ORG-099"])
        self.assertEqual(data["actors"], ["claude-fallback", "specialist"])
        self.assertEqual(data["commits"], [])


class ReadDocTest(unittest.TestCase):
    """Direct coverage of the lenient frontmatter reader — its whole reason for
    existing is to parse partial/odd inputs without raising."""

    def _doc(self, text: str) -> links._Doc:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.md"
            p.write_text(text, encoding="utf-8")
            return links.read_doc(p)

    def test_flow_list_scalar_quotes(self):
        d = self._doc("---\nid: T-0001\nlabels: [a, 'b', \"c\"]\ndepends_on: []\n"
                      "parent: T-0002\n---\nbody here\n")
        self.assertEqual(d.fields["id"], "T-0001")
        self.assertEqual(d.fields["labels"], ["a", "b", "c"])
        self.assertEqual(d.fields["depends_on"], [])
        self.assertEqual(d.fields["parent"], "T-0002")
        self.assertEqual(d.body.strip(), "body here")

    def test_value_with_colon(self):
        d = self._doc("---\nurl: https://example.com/x\n---\n")
        self.assertEqual(d.fields["url"], "https://example.com/x")

    def test_no_frontmatter_is_all_body(self):
        d = self._doc("# just a title\nno frontmatter\n")
        self.assertEqual(d.fields, {})
        self.assertIn("just a title", d.body)

    def test_unterminated_frontmatter_is_all_body(self):
        d = self._doc("---\nid: T-0001\nnever closes\n")
        self.assertEqual(d.fields, {})
        self.assertIn("never closes", d.body)

    def test_empty_file(self):
        d = self._doc("")
        self.assertEqual(d.fields, {})
        self.assertEqual(d.body, "")


class MalformedTicketRobustnessTest(unittest.TestCase):
    """The central design claim: one malformed ticket must not crash a query."""

    def test_queries_survive_a_garbage_ticket(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tickets = root / "tickets"
            _write_ticket(
                tickets, "active", "T-0010",
                frontmatter={"id": "T-0010", "parent_ticket": "None",
                             "depends_on": [], "labels": ["ORG-001"], "ratifiable_files": []},
                log=["- 2026-06-01T00:00:00Z [recovery] filed"],
            )
            # A genuinely malformed neighbour: unterminated frontmatter + binary.
            (tickets / "active" / "T-0011.md").write_bytes(
                b"---\nid: T-0011\nbroken\x00\xff no close\n"
            )
            # None of these may raise.
            self.assertEqual(links.get_touched_by("recovery", tickets), ["T-0010"])
            self.assertEqual(links.get_adr_tickets("ORG-001", tickets), ["T-0010"])
            self.assertEqual(links.get_children("T-0010", tickets), [])
            recs = links.build_index_records(tickets)
            self.assertTrue(any(r["type"] == "actor" and r["to"] == "T-0010" for r in recs))


class CommitQueryTest(unittest.TestCase):
    """get_commits against a throwaway git repo — verifies digit-boundary anchoring."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        self._env = env
        self._git("init", "-q")
        (self.repo / "f.txt").write_text("x\n")
        self._git("add", ".")
        self._commit("work on T-0042 done")
        (self.repo / "f.txt").write_text("y\n")
        self._git("add", ".")
        self._commit("unrelated T-00420 longer id")
        (self.repo / "f.txt").write_text("z\n")
        self._git("add", ".")
        self._commit("trailer form\n\nPantheon-Ticket: T-0042")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args):
        import os
        e = dict(os.environ, **self._env)
        subprocess.run(["git", *args], cwd=str(self.repo), check=True,
                       capture_output=True, text=True, env=e)

    def _commit(self, msg):
        self._git("commit", "-q", "-m", msg)

    def test_commit_boundary(self):
        commits = links.get_commits("T-0042", repo_root=self.repo)
        subjects = " ".join(s for _, s in commits)
        # Matches both the inline "T-0042 done" and the trailer commit...
        self.assertIn("work on T-0042 done", subjects)
        self.assertIn("trailer form", subjects)
        # ...but NOT the longer id T-00420.
        self.assertNotIn("longer id", subjects)


if __name__ == "__main__":
    unittest.main()
