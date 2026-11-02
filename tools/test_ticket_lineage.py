"""Tests for the T-3332 read-only lineage-graph query surface.

Fixture: tools/fixtures/ticket_lineage/ is a CAPTURED real ticket subtree
(copied byte-for-byte from tickets/ on 2026-10-27; T-5677 from origin/main)
plus the `## ` heading lines of rulebook/goals.md with their T-5679
`<!-- goal-key: ... -->` marker lines (goal_headings.md). Cases the
real corpus never shows (a cycle, a missing parent, the post-backfill shape of
a legacy operator-ad-hoc ticket) are made by copying the fixture into a
test-owned temp dir and re-pointing ONE real frontmatter line; each such
mutation is named in its test.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import goal_registry, ticket, ticket_lineage as tl, ticket_purpose as tp

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ticket_lineage"
FIXTURE_TICKETS = FIXTURE / "tickets"
FIXTURE_GOALS = FIXTURE / "goal_headings.md"


def _goal_keys() -> frozenset[str]:
    return goal_registry.load_goal_keys(FIXTURE, path=FIXTURE_GOALS)


def _graph(root: Path = FIXTURE_TICKETS, edges=tl.DEFAULT_UP_FIELDS) -> tl.LineageGraph:
    return tl.build_graph(root, _goal_keys(), edges)


def _run(*argv: str, tickets_root: Path = FIXTURE_TICKETS) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with patch.object(ticket, "TICKETS_ROOT", tickets_root), \
            patch.object(ticket, "LINEAGE_GOALS_PATH", FIXTURE_GOALS), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ticket.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class _TempCorpus(unittest.TestCase):
    """A test-owned copy of the captured fixture, for one-line mutations."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "tickets"
        shutil.copytree(FIXTURE_TICKETS, self.root)

    def repoint(self, rel: str, old: str, new: str) -> None:
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"{rel}: {old!r}")
        path.write_text(text.replace(old, new), encoding="utf-8")


class GoalKeyTests(unittest.TestCase):
    def test_goal_keys_read_through_goal_registry_markers(self) -> None:
        self.assertEqual(
            _goal_keys(),
            frozenset({"grand-project", "echo-prismatic", "revenue-project", "pantheon",
                       "pantheon-pwa", "vibecamp-scheduler", "vibecamp-trials-tracker",
                       "personal-site"}),
        )

    def test_stopgap_heading_parser_is_gone(self) -> None:
        # T-5679: goal_registry is the one goal-key parser.
        self.assertFalse(hasattr(tl, "load_goal_keys"))

    def test_missing_goals_file_is_named_on_stderr_not_silent(self) -> None:
        with patch.object(ticket, "TICKETS_ROOT", FIXTURE_TICKETS), \
                patch.object(ticket, "LINEAGE_GOALS_PATH", FIXTURE / "absent.md"), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = ticket.main(["lineage", "roots", "--counts"])
        self.assertEqual(rc, 0)
        self.assertIn("goal-registry-unavailable: goal registry missing", err.getvalue())


class ClassifyTerminusTests(unittest.TestCase):
    """The one swappable classifier, over real captured frontmatter."""

    def setUp(self) -> None:
        self.g = _graph()

    def own(self, tid: str) -> tuple[str, str]:
        c = self.g.own(tid)
        return c.cls.value, c.code

    def test_four_classes_and_their_codes(self) -> None:
        self.assertEqual(self.own("T-4780"), ("goal", "goal-ref-unregistered"))
        self.assertEqual(self.own("T-5677"), ("operational-root", "operational-root-label"))
        self.assertEqual(self.own("T-4973"), ("maintenance", "maintenance-ref"))
        self.assertEqual(self.own("T-5589"), ("maintenance", "goal-neutral-serves"))
        self.assertEqual(self.own("T-0037"), ("maintenance", "hygiene-label"))
        self.assertEqual(self.own("T-3328"), ("UNROOTED", "free-text-serves"))
        self.assertEqual(self.own("T-3344"), ("UNROOTED", "legacy-operator-ad-hoc"))
        self.assertEqual(self.own("T-3851"), ("UNROOTED", "malformed-ref"))
        self.assertEqual(self.own("T-3001"), ("UNROOTED", "no-purpose"))
        self.assertEqual(self.own("ORG-065#E4"), ("UNROOTED", "adr-terminus"))
        self.assertEqual(self.own("T-9999"), ("UNROOTED", "missing-ticket"))

    def test_serves_naming_a_goals_md_key_is_goal_free_text_is_not(self) -> None:
        keys = _goal_keys()
        base = dict(self.g.nodes["T-3328"].fields)
        for serves in ("pantheon", "goal:echo-prismatic"):
            c = tl.classify_terminus(dict(base, serves=serves), keys)
            self.assertEqual((c.cls, c.code), (tl.Terminus.GOAL, "goal-serves"), serves)
        # Free text that merely mentions a goal is NOT goal-classified.
        c = tl.classify_terminus(dict(base, serves="pantheon (org system) - assurance"), keys)
        self.assertEqual(c.cls, tl.Terminus.UNROOTED)
        c = tl.classify_terminus(dict(base, nearest_intent_ref="goal:pantheon"), keys)
        self.assertEqual((c.cls, c.code), (tl.Terminus.GOAL, "goal-ref"))

    def test_maintenance_class_is_exactly_is_hygiene(self) -> None:
        """Drift guard: maintenance is decided by ticket._is_hygiene, not a copy."""
        for tid, node in self.g.nodes.items():
            if tp.OPERATIONAL_ROOT_LABEL in (node.fields.get("labels") or []):
                continue
            if self.g.own(tid).cls is tl.Terminus.GOAL:
                continue
            self.assertEqual(
                self.g.own(tid).cls is tl.Terminus.MAINTENANCE,
                ticket._is_hygiene(node.fields), tid,
            )

    def test_label_constant_is_shared_with_ticket_purpose(self) -> None:
        self.assertIs(tl.OPERATIONAL_ROOT_LABEL, tp.OPERATIONAL_ROOT_LABEL)


class UpWalkTests(_TempCorpus):
    def test_up_walk_multi_hop_to_free_text_root(self) -> None:
        g = _graph()
        res = tl.walk(g, ["T-3332"], "up")
        self.assertEqual(res["visited"], ["T-3332", "T-3328"])
        self.assertEqual(res["edges"], [{"src": "T-3332", "dst": "T-3328", "field": "parent_ticket"}])
        cls, via = tl.ticket_terminus(g, "T-3332")
        self.assertEqual((cls.cls, cls.code, via), (tl.Terminus.UNROOTED, "free-text-serves", "T-3328"))

    def test_up_walk_follows_obligation_edges_and_reports_missing(self) -> None:
        g = _graph()
        res = tl.walk(g, ["T-2843"], "up")
        self.assertEqual(
            {(e["dst"], e["field"]) for e in res["edges"]},
            {("T-2840", "parent_ticket"), ("ORG-065#E4", "implements"),
             ("ORG-065#E9", "implements"), ("T-2673", "parent_ticket")},
        )
        # T-2840's real parent T-2673 is outside the captured subtree.
        self.assertEqual(res["missing"], ["T-2673"])
        cls, via = tl.ticket_terminus(g, "T-2843")
        self.assertEqual((cls.code, via), ("adr-terminus", "ORG-065#E4"))

    def test_precedence_rooted_ancestor_beats_own_unrooted(self) -> None:
        g = _graph()
        # T-4535's own serves is legacy operator-ad-hoc, but its intent ref
        # reaches T-4531 (maintenance:pantheon-config).
        self.assertEqual(g.own("T-4535").cls, tl.Terminus.UNROOTED)
        cls, via = tl.ticket_terminus(g, "T-4535")
        self.assertEqual((cls.cls, via), (tl.Terminus.MAINTENANCE, "T-4531"))

    def test_informational_edges_are_opt_in(self) -> None:
        self.assertEqual(tl.walk(_graph(), ["T-5520"], "up")["edges"], [])
        res = tl.walk(_graph(edges=tl.ALL_UP_FIELDS), ["T-5520"], "up")
        self.assertEqual(res["edges"], [{"src": "T-5520", "dst": "T-3309", "field": "informs"}])
        self.assertEqual(res["missing"], ["T-3309"])

    def test_cycle_terminates_and_is_reported(self) -> None:
        # Mutation: T-3328's real `parent_ticket: null` re-pointed at its child.
        self.repoint("done/T-3328.md", "parent_ticket: null", "parent_ticket: T-3332")
        g = _graph(self.root)
        res = tl.walk(g, ["T-3332"], "up")
        self.assertEqual(res["cycles"], [["T-3332", "T-3328", "T-3332"]])
        cls, _via = tl.ticket_terminus(g, "T-3332")
        self.assertEqual((cls.cls, cls.code), (tl.Terminus.UNROOTED, "cycle"))
        text = "\n".join(tl.render_tree(g, ["T-3332"], "up", None))
        self.assertIn("CYCLE", text)

    def test_missing_parent_is_a_finding_not_a_crash(self) -> None:
        # Mutation: T-3328's parent re-pointed at an id the corpus lacks.
        self.repoint("done/T-3328.md", "parent_ticket: null", "parent_ticket: T-0001")
        g = _graph(self.root)
        res = tl.walk(g, ["T-3332"], "up")
        self.assertEqual(res["missing"], ["T-0001"])
        cls, via = tl.ticket_terminus(g, "T-3332")
        self.assertEqual((cls.code, via), ("missing-ticket", "T-0001"))
        self.assertIn("MISSING", "\n".join(tl.render_tree(g, ["T-3332"], "up", None)))

    def test_legacy_ad_hoc_before_and_after_the_t5677_backfill(self) -> None:
        cls, _ = tl.ticket_terminus(_graph(self.root), "T-3344")
        self.assertEqual((cls.cls, cls.code), (tl.Terminus.UNROOTED, "legacy-operator-ad-hoc"))
        # Mutation: the backfill artisan runs separately adds the intent ref.
        self.repoint("done/T-3344.md", "parent_ticket: null\n",
                     "nearest_intent_ref: ticket:T-5677\nparent_ticket: null\n")
        cls, via = tl.ticket_terminus(_graph(self.root), "T-3344")
        self.assertEqual((cls.cls, via), (tl.Terminus.OPERATIONAL_ROOT, "T-5677"))

    def test_2000_deep_chain_walks_and_renders_without_recursion_error(self) -> None:
        """r1 LOW-3: walk() and render_tree() are iterative. Synthetic chain:
        2000 copies of the captured T-3331, re-id'd T-20000..T-21999, each
        re-parented on the previous one (the first on T-3331 itself)."""
        src = (self.root / "done" / "T-3331.md").read_text(encoding="utf-8")
        n = 2000
        parent = "T-3331"
        for i in range(n):
            tid = f"T-{20000 + i}"
            text = src.replace("id: T-3331\n", f"id: {tid}\n", 1).replace(
                "parent_ticket: T-3328\n", f"parent_ticket: {parent}\n", 1)
            (self.root / "done" / f"{tid}.md").write_text(text, encoding="utf-8")
            parent = tid
        g = _graph(self.root)
        bottom = f"T-{20000 + n - 1}"
        up = tl.walk(g, [bottom], "up")
        self.assertEqual(len(up["visited"]), n + 2)  # chain + T-3331 + T-3328
        self.assertEqual(up["visited"][-1], "T-3328")
        self.assertEqual(up["cycles"], [])
        up_lines = tl.render_tree(g, [bottom], "up", None)
        self.assertIn("T-3328 [done]", up_lines[-3])
        down = tl.walk(g, ["T-3328"], "down")
        self.assertIn(bottom, down["visited"])
        self.assertTrue(tl.render_tree(g, ["T-3328"], "down", None))
        cls, via = tl.ticket_terminus(g, bottom)
        self.assertEqual((cls.code, via), ("free-text-serves", "T-3328"))

    def test_iterative_walk_keeps_recursive_preorder(self) -> None:
        # Sibling order and "(seen above)" placement match the recursive form:
        # T-4535's two edges to T-4531 expand the first, mark the second.
        lines = tl.render_tree(_graph(), ["T-4535"], "up", None)
        self.assertEqual(
            [ln.strip() for ln in lines if "->" in ln],
            ["-> nearest_intent_ref -> T-4531 [inbox]",
             "-> parent_ticket -> T-4531  (seen above)"],
        )

    def test_down_tree_prints_class_line_for_unrooted_parents(self) -> None:
        # r1 LOW-4: in a down tree every node's own class is shown, including
        # an UNROOTED node that has children (T-3331 has child T-5589).
        lines = tl.render_tree(_graph(), ["T-3328"], "down", None)
        idx = next(i for i, ln in enumerate(lines) if "<- parent_ticket <- T-3331" in ln)
        self.assertIn("=> UNROOTED: serves", lines[idx + 2])
        # Up trees still omit it for non-terminal UNROOTED nodes.
        up = tl.render_tree(_graph(), ["T-5589"], "up", None)
        self.assertFalse(any("=> UNROOTED" in ln and "T-3328 lineage-graph completeness" in ln
                             for ln in up))

    def test_malformed_ref_is_reported_not_raised(self) -> None:
        g = _graph()
        self.assertIn("T-3851", {m["ticket"] for m in g.malformed})
        self.assertEqual(tl.ticket_terminus(g, "T-3851")[0].code, "malformed-ref")

    def test_unparseable_file_is_skipped_and_counted(self) -> None:
        (self.root / "inbox" / "T-0002.md").write_text("no frontmatter\n", encoding="utf-8")
        g = _graph(self.root)
        self.assertNotIn("T-0002", g.nodes)
        self.assertEqual(len(g.unparseable), 1)


class DownWalkTests(unittest.TestCase):
    def test_down_from_ticket(self) -> None:
        g = _graph()
        res = tl.walk(g, ["T-3328"], "down")
        self.assertEqual(res["visited"], ["T-3328", "T-3331", "T-5589", "T-3332"])
        self.assertIn({"src": "T-5589", "dst": "T-3331", "field": "parent_ticket"}, res["edges"])

    def test_down_from_goal_and_roots(self) -> None:
        g = _graph()
        self.assertEqual(tl.resolve_down_seeds(g, "goal:operator-bandwidth"), ["T-4780"])
        self.assertEqual(tl.resolve_down_seeds(g, "operational-root"), ["T-5677"])
        self.assertEqual(tl.resolve_down_seeds(g, "maintenance:pantheon-config"), ["T-4531"])
        res = tl.walk(g, ["T-4531"], "down")
        self.assertEqual(res["visited"], ["T-4531", "T-4535"])
        self.assertEqual(tl.resolve_down_seeds(g, "ORG-065"), ["ORG-065#E4", "ORG-065#E9"])

    def test_unknown_seeds_raise_seed_error(self) -> None:
        g = _graph()
        for spec in ("T-9999", "goal:no-such-goal", "nonsense", "ORG-999"):
            with self.subTest(spec=spec), self.assertRaises(tl.SeedError):
                tl.resolve_down_seeds(g, spec)

    def test_registered_goal_with_no_tickets_is_empty_not_error(self) -> None:
        self.assertEqual(tl.resolve_down_seeds(_graph(), "goal:pantheon"), [])

    def test_superseded_ticket_is_annotated(self) -> None:
        rec = tl.node_record(_graph(), "T-2993")
        self.assertEqual(rec["superseded_by"], ["T-3001"])


class RootsSummaryTests(unittest.TestCase):
    def test_open_counts(self) -> None:
        s = tl.roots_summary(_graph(), tl.OPEN_STATES)
        self.assertEqual(s["total"], 8)
        self.assertEqual(
            s["counts"], {"goal": 1, "operational-root": 1, "maintenance": 5, "UNROOTED": 1}
        )
        self.assertEqual(s["detail"]["UNROOTED"], {"free-text-serves": 1})


class CliTests(_TempCorpus):
    def test_up_text_output(self) -> None:
        rc, out, err = _run("lineage", "up", "T-3332")
        self.assertEqual(rc, 0, err)
        self.assertIn("T-3332 [active]", out)
        self.assertIn("-> parent_ticket -> T-3328 [done]", out)
        self.assertIn("terminus: UNROOTED (free-text-serves", out)

    def test_up_json_is_graph_shaped(self) -> None:
        rc, out, err = _run("lineage", "up", "T-2843", "--json")
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(set(data), {"query", "nodes", "edges", "terminus", "findings"})
        self.assertEqual(data["nodes"]["T-2843"]["title"],
                         "ORG-065 BT3: lineage query surface (leak-detector over the corpus)")
        self.assertEqual(data["nodes"]["ORG-065#E4"]["kind"], "obligation")
        self.assertEqual(data["findings"]["missing"], ["T-2673"])
        self.assertEqual(data["terminus"]["code"], "adr-terminus")

    def test_down_goal_json(self) -> None:
        rc, out, err = _run("lineage", "down", "goal:operator-bandwidth", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["query"]["seeds"], ["T-4780"])

    def test_roots_counts_text_and_json(self) -> None:
        rc, out, err = _run("lineage", "roots", "--counts")
        self.assertEqual(rc, 0, err)
        self.assertIn("UNROOTED", out)
        self.assertIn("honesty metric", out)
        rc, out, err = _run("lineage", "roots", "--counts", "--json")
        data = json.loads(out)
        self.assertNotIn("members", data)
        self.assertEqual(data["counts"]["maintenance"], 5)
        rc, out, _ = _run("lineage", "roots", "--state", "all", "--class", "UNROOTED", "--json")
        self.assertEqual(json.loads(out)["counts"]["UNROOTED"], 9)

    def test_unknown_node_exits_2(self) -> None:
        for argv in (("lineage", "up", "T-9999"), ("lineage", "down", "goal:nope")):
            rc, _out, err = _run(*argv)
            self.assertEqual(rc, 2)
            self.assertIn("error:", err)

    def test_bad_edges_value_is_an_error(self) -> None:
        rc, _out, err = _run("lineage", "up", "T-3332", "--edges", "bogus")
        self.assertEqual(rc, ticket.EXIT_REFUSAL)
        self.assertIn("unknown edge field", err)

    def test_read_only_no_git_no_writes_runs_off_main(self) -> None:
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

        def _boom(*_a, **_k):
            raise AssertionError("lineage must not run git or subprocesses")

        with patch.object(ticket, "_git", _boom), \
                patch.object(ticket, "_current_branch", _boom), \
                patch.object(subprocess, "run", _boom), \
                patch.object(subprocess, "Popen", _boom):
            for argv in (("lineage", "up", "T-3332", "--json"),
                         ("lineage", "down", "T-3328"),
                         ("lineage", "roots", "--counts"),
                         ("lineage", "roots", "--state", "all")):
                rc, _out, err = _run(*argv, tickets_root=self.root)
                self.assertEqual(rc, 0, f"{argv}: {err}")
        after = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


def _file_option_help(dest: str) -> str:
    parser = ticket._build_parser()
    (subs,) = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    (action,) = [a for a in subs.choices["file"]._actions if a.dest == dest]
    return " ".join(action.help.split())


class FileHelpPointsAtOperationalRootTests(unittest.TestCase):
    def test_nearest_intent_ref_help_names_t5677(self) -> None:
        self.assertIn("ticket:T-5677", _file_option_help("nearest_intent_ref"))

    def test_serves_help_calls_operator_ad_hoc_legacy(self) -> None:
        text = _file_option_help("serves")
        self.assertIn("'operator-ad-hoc' is LEGACY", text)
        self.assertIn("--nearest-intent-ref ticket:T-5677", text)


if __name__ == "__main__":
    unittest.main()
