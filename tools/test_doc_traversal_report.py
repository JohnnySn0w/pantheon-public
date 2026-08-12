#!/usr/bin/env python3
"""Tests for doc_traversal_report.py (T-1805).

Hermetic: synthetic jsonl fixtures in temp dirs; no sudo, no real transcripts.
Run: python3 -m unittest tools.test_doc_traversal_report -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.doc_traversal_report import (
    analyze_session,
    build_rollup,
    extract_bash_read_paths,
    filter_main_chain,
    inventory_governance_docs,
    is_governance_path,
    is_index_path,
    list_local_sessions,
    main,
    normalize_gov_key,
    parse_transcript_lines,
)

REPO = "/home/x/pantheon-config"


def tool_use_line(name, tool_input, ts=None, sidechain=False):
    rec = {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "input": tool_input}],
        },
    }
    if ts is not None:
        rec["timestamp"] = ts
    return json.dumps(rec)


def read_line(path, **kw):
    return tool_use_line("Read", {"file_path": path}, **kw)


def bash_line(command, **kw):
    return tool_use_line("Bash", {"command": command}, **kw)


class ParseTests(unittest.TestCase):
    def test_extracts_tool_use_events(self):
        events = parse_transcript_lines([read_line("/a.md"), bash_line("ls")])
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["tool"], "Read")
        self.assertEqual(events[0]["input"]["file_path"], "/a.md")
        self.assertEqual(events[1]["tool"], "Bash")

    def test_sidechain_records_skipped(self):
        events = parse_transcript_lines(
            [read_line("/a.md", sidechain=True), read_line("/b.md")]
        )
        self.assertEqual([e["input"]["file_path"] for e in events], ["/b.md"])

    def test_malformed_and_irrelevant_lines_skipped_silently(self):
        lines = [
            "not json at all {{{",
            '"just a string"',
            json.dumps({"type": "user", "message": {"content": "plain string"}}),
            json.dumps({"type": "mode", "mode": "normal"}),
            read_line("/ok.md"),
            "",
        ]
        events = parse_transcript_lines(lines)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["input"]["file_path"], "/ok.md")

    def test_missing_timestamps_fall_back_to_line_order(self):
        events = parse_transcript_lines(
            [read_line("/first.md"), read_line("/second.md", ts="2026-01-01T00:00:00Z")]
        )
        self.assertEqual(
            [e["input"]["file_path"] for e in events], ["/first.md", "/second.md"]
        )


class ClassificationTests(unittest.TestCase):
    def test_governance_classification(self):
        self.assertTrue(is_governance_path(REPO + "/sop/standard/foo.md", REPO))
        self.assertTrue(is_governance_path(REPO + "/adr/ADR-001.md", REPO))
        self.assertTrue(is_governance_path(REPO + "/rulebook/directives.md", REPO))
        self.assertTrue(is_governance_path("/home/claude-fallback/HANDOFF.md", REPO))
        self.assertTrue(is_governance_path(REPO + "/README.md", REPO))
        self.assertTrue(
            is_governance_path(REPO + "/notes/recent-policy-changes.md", REPO)
        )
        self.assertFalse(is_governance_path(REPO + "/tools/ticket.py", REPO))
        self.assertFalse(is_governance_path(REPO + "/notes/scratch.md", REPO))

    def test_index_path_detection(self):
        self.assertTrue(is_index_path(REPO + "/sop/standard/README.md"))
        self.assertTrue(is_index_path(REPO + "/adr/README.md"))
        self.assertFalse(is_index_path(REPO + "/README.md"))
        self.assertFalse(is_index_path(REPO + "/sop/standard/deploy.md"))

    def test_normalize_gov_key_strips_checkout_prefix(self):
        self.assertEqual(
            normalize_gov_key("/home/claude/pantheon-config/sop/standard/a.md", REPO),
            "sop/standard/a.md",
        )
        self.assertEqual(
            normalize_gov_key("/home/claude/HANDOFF.md", REPO), "HANDOFF.md"
        )


class BashReadTests(unittest.TestCase):
    def test_cat_with_quoted_path(self):
        self.assertEqual(
            extract_bash_read_paths('cat "/tmp/some dir/notes file.md"'),
            ["/tmp/some dir/notes file.md"],
        )

    def test_grep_with_flags(self):
        self.assertEqual(
            extract_bash_read_paths("grep -rn stale_after /x/tools/ticket.py"),
            ["/x/tools/ticket.py"],
        )

    def test_sed_n_read(self):
        self.assertEqual(
            extract_bash_read_paths("sed -n '10,20p' /a/b/doc.md"), ["/a/b/doc.md"]
        )

    def test_python3_with_path_arg_is_not_a_read(self):
        self.assertEqual(extract_bash_read_paths("python3 tools/ticket.py --list"), [])

    def test_separator_ends_reader_scope(self):
        self.assertEqual(
            extract_bash_read_paths("cat /a.md && python3 /b.py"), ["/a.md"]
        )

    def test_glob_pattern_args_are_not_counted_as_reads(self):
        # seen in a real transcript: `head sop/standard/*.md` — the unexpanded
        # glob token is not a concrete file read
        self.assertEqual(extract_bash_read_paths("head -5 sop/standard/*.md"), [])

    def test_sudo_bash_c_nesting_unwrapped(self):
        self.assertEqual(
            extract_bash_read_paths("sudo -nu claude bash -c 'cat /home/claude/H.md'"),
            ["/home/claude/H.md"],
        )


class SessionAnalysisTests(unittest.TestCase):
    def analyze(self, lines):
        return analyze_session("s1", parse_transcript_lines(lines), REPO)

    def test_boot_with_handoff_and_readme(self):
        rep = self.analyze(
            [read_line("/home/claude-fallback/HANDOFF.md"), read_line(REPO + "/README.md")]
        )
        self.assertTrue(rep["boot"]["handoff_read"])
        self.assertTrue(rep["boot"]["repo_readme_read"])
        self.assertEqual(
            rep["boot"]["governance_touched"],
            ["/home/claude-fallback/HANDOFF.md", REPO + "/README.md"],
        )

    def test_boot_without_handoff_in_first_15(self):
        lines = [bash_line("ls") for _ in range(15)]
        lines.append(read_line("/home/claude-fallback/HANDOFF.md"))
        rep = self.analyze(lines)
        self.assertFalse(rep["boot"]["handoff_read"])
        # ...but the touch still counts as a governance touch overall
        self.assertEqual(
            rep["governance_touches"],
            [{"path": "/home/claude-fallback/HANDOFF.md", "count": 1}],
        )

    def test_index_followed(self):
        rep = self.analyze(
            [
                read_line(REPO + "/sop/standard/README.md"),
                read_line(REPO + "/sop/standard/deploy.md"),
            ]
        )
        self.assertEqual(len(rep["index_chain"]), 1)
        self.assertTrue(rep["index_chain"][0]["followed"])
        self.assertEqual(
            rep["index_chain"][0]["followed_by"], REPO + "/sop/standard/deploy.md"
        )

    def test_index_not_followed(self):
        rep = self.analyze(
            [
                read_line(REPO + "/sop/standard/README.md"),
                read_line(REPO + "/adr/ADR-001.md"),
            ]
        )
        self.assertFalse(rep["index_chain"][0]["followed"])

    def test_index_rereading_readme_does_not_count_as_followed(self):
        rep = self.analyze(
            [
                read_line(REPO + "/sop/standard/README.md"),
                read_line(REPO + "/sop/standard/README.md"),
            ]
        )
        self.assertEqual(len(rep["index_chain"]), 1)
        self.assertFalse(rep["index_chain"][0]["followed"])

    def test_earlier_doc_read_does_not_satisfy_later_index(self):
        rep = self.analyze(
            [
                read_line(REPO + "/sop/standard/deploy.md"),
                read_line(REPO + "/sop/standard/README.md"),
            ]
        )
        self.assertFalse(rep["index_chain"][0]["followed"])

    def test_bash_touches_counted_separately_from_tool_reads(self):
        rep = self.analyze(
            [read_line(REPO + "/adr/ADR-001.md"), bash_line("cat %s/adr/ADR-002.md" % REPO)]
        )
        self.assertEqual(
            rep["bash_touches"], [{"path": REPO + "/adr/ADR-002.md", "count": 1}]
        )
        gov_paths = [g["path"] for g in rep["governance_touches"]]
        self.assertIn(REPO + "/adr/ADR-002.md", gov_paths)

    def test_task_spawns_counted(self):
        rep = self.analyze(
            [
                tool_use_line("Task", {"prompt": "go do x"}),
                tool_use_line("Agent", {"prompt": "go do y"}),
                bash_line("ls"),
            ]
        )
        self.assertEqual(rep["task_spawns"], 2)

    def test_top_files_categorized(self):
        rep = self.analyze(
            [
                read_line(REPO + "/adr/ADR-001.md"),
                read_line(REPO + "/adr/ADR-001.md"),
                read_line(REPO + "/tools/ticket.py"),
                read_line("/etc/hosts"),
            ]
        )
        cats = {tf["path"]: tf["category"] for tf in rep["top_files"]}
        self.assertEqual(cats[REPO + "/adr/ADR-001.md"], "repo-docs")
        self.assertEqual(cats[REPO + "/tools/ticket.py"], "code")
        self.assertEqual(cats["/etc/hosts"], "other")
        self.assertEqual(rep["top_files"][0]["count"], 2)


class RollupTests(unittest.TestCase):
    def make_repo(self, tmp):
        root = Path(tmp) / "pantheon-config"
        (root / "sop" / "standard").mkdir(parents=True)
        (root / "adr").mkdir()
        (root / "rulebook").mkdir()
        (root / "sop" / "standard" / "deploy.md").write_text("x")
        (root / "sop" / "standard" / "never-read.md").write_text("x")
        (root / "adr" / "ADR-001.md").write_text("x")
        (root / "README.md").write_text("x")
        return str(root)

    def test_suffix_matching_across_checkout_prefixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_repo(tmp)
            # touch comes from a DIFFERENT checkout prefix than repo-root
            rep = analyze_session(
                "s1",
                parse_transcript_lines(
                    [read_line("/home/claude/pantheon-config/sop/standard/deploy.md")]
                ),
                root,
            )
            rollup = build_rollup([rep, rep], root)
            self.assertNotIn("sop/standard/deploy.md", rollup["never_touched"])
            self.assertIn("sop/standard/never-read.md", rollup["never_touched"])
            self.assertIn("adr/ADR-001.md", rollup["never_touched"])

    def test_rollup_aggregates_and_boot_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_repo(tmp)
            rep1 = analyze_session(
                "s1",
                parse_transcript_lines(
                    [
                        read_line("/home/claude/pantheon-config/adr/ADR-001.md"),
                        read_line("/home/claude/HANDOFF.md"),
                    ]
                ),
                root,
            )
            rep2 = analyze_session(
                "s2",
                parse_transcript_lines([read_line(root + "/adr/ADR-001.md")]),
                root,
            )
            rollup = build_rollup([rep1, rep2], root)
            counts = {g["path"]: g["count"] for g in rollup["governance_touch_counts"]}
            self.assertEqual(counts["adr/ADR-001.md"], 2)  # prefixes merged
            self.assertEqual(len(rollup["boot_adherence"]), 2)
            self.assertTrue(rollup["boot_adherence"][0]["handoff"])
            self.assertFalse(rollup["boot_adherence"][1]["handoff"])

    def test_inventory_recursive_md_plus_readme(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_repo(tmp)
            inv = inventory_governance_docs(root)
            self.assertIn("sop/standard/deploy.md", inv)
            self.assertIn("adr/ADR-001.md", inv)
            self.assertIn("README.md", inv)
            self.assertNotIn("tools/anything.py", inv)


class SessionSelectionTests(unittest.TestCase):
    def test_agent_jsonl_excluded_from_main_chain(self):
        paths = ["/d/aaa.jsonl", "/d/agent-123.jsonl", "/d/bbb.jsonl"]
        self.assertEqual(filter_main_chain(paths), ["/d/aaa.jsonl", "/d/bbb.jsonl"])

    def test_list_local_sessions_newest_first_excluding_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.jsonl"
            new = Path(tmp) / "new.jsonl"
            agent = Path(tmp) / "agent-x.jsonl"
            for p in (old, new, agent):
                p.write_text("")
            os.utime(old, (1000, 1000))
            os.utime(new, (2000, 2000))
            os.utime(agent, (3000, 3000))
            got = list_local_sessions(tmp, 5)
            self.assertEqual([Path(p).name for p in got], ["new.jsonl", "old.jsonl"])


class CliTests(unittest.TestCase):
    def test_end_to_end_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pantheon-config"
            (root / "sop" / "standard").mkdir(parents=True)
            (root / "adr").mkdir()
            (root / "rulebook").mkdir()
            (root / "sop" / "standard" / "deploy.md").write_text("x")
            (root / "README.md").write_text("x")
            s1 = Path(tmp) / "s1.jsonl"
            s1.write_text(
                "\n".join(
                    [
                        read_line("/home/claude/HANDOFF.md"),
                        read_line(str(root / "README.md")),
                        "garbage line",
                    ]
                )
            )
            s2 = Path(tmp) / "s2.jsonl"
            s2.write_text(bash_line("cat %s/sop/standard/deploy.md" % root))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main([str(s1), str(s2), "--json", "--repo-root", str(root)])
            self.assertEqual(rc, 0)
            doc = json.loads(buf.getvalue())
            self.assertEqual(rc, 0)
            self.assertEqual(len(doc["sessions"]), 2)
            self.assertTrue(doc["sessions"][0]["boot"]["handoff_read"])
            self.assertIsNotNone(doc["rollup"])
            self.assertNotIn("sop/standard/deploy.md", doc["rollup"]["never_touched"])

    def test_markdown_output_and_single_session_has_no_rollup(self):
        with tempfile.TemporaryDirectory() as tmp:
            s1 = Path(tmp) / "solo.jsonl"
            s1.write_text(read_line("/home/claude/HANDOFF.md"))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main([str(s1)])
            self.assertEqual(rc, 0)
            text = buf.getvalue()
            self.assertIn("## Session solo", text)
            self.assertIn("HANDOFF.md read: yes", text)
            self.assertNotIn("Fleet rollup", text)

    def test_no_inputs_is_an_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main([])
        self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
