"""ticket.py ORG-076 repair-field surface + ticket_lint duplicate-episode scan (T-4707 BT1).

RFC-004 §3(a) (runtime repair-ticket frontmatter), §2 (episode_id uniqueness against the
COMMITTED corpus, enforced by the CLI and by lint). Hermetic: temp repo + bare origin.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import _frontmatter_block, ticket
from tools._episode_fixtures import SRC_ROOT, RepoFixture, git

BASE_SHA = "b7831b0703f1"  # origin/main when T-4707 was staged (pre-change ticket.py)

FILE_BASE = ["file", "--requester", "recovery", "--target", "role:system_orchestrator", "--title", "repair x",
             "--serves", "ORG-076 fleet health-and-repair", "--appetite", "5",
             "--labels", "repair,org-076", "--no-push"]


def _fields(**over):
    d = dict(fault_id="quota-probe-unreadable:specialist", episode_id="ep-q-20261008T000000Z-aaaa0001",
             domain_key="quota", subsystem="quota", detector="quota-probe", verdict="unknown",
             repair_route="recovery", action="germinate repair --system quota",
             evidence_snapshot="line a\n\n  indented b\n---\nline c\n", evidence_deferred=False)
    d.update(over)
    return d


class RepairFieldsCli(unittest.TestCase):
    def setUp(self):
        self.fx = RepoFixture()
        self.addCleanup(self.fx.cleanup)

    def _file(self, fields, extra=None, kill=""):
        p = self.fx.root / "rf.json"
        p.write_text(json.dumps(fields))
        return self.fx.ticket_cli(FILE_BASE + ["--repair-fields-json", str(p)] + (extra or []), kill=kill)

    def _inbox(self):
        return sorted((self.fx.repo / "tickets" / "inbox").glob("T-*.md"))

    def test_emits_all_fields_and_roundtrips_block_scalar(self):
        r = self._file(_fields())
        self.assertEqual(r.returncode, 0, r.stderr)
        (path,) = self._inbox()
        fm, _ = ticket.parse_front_matter(path.read_text())
        self.assertEqual(fm["episode_id"], "ep-q-20261008T000000Z-aaaa0001")
        self.assertEqual(fm["attempts_budget"], "2")  # default
        self.assertEqual(fm["evidence_deferred"], "false")
        self.assertEqual(fm["verdict"], "unknown")
        self.assertEqual(fm["evidence_snapshot"], "line a\n\n  indented b\n---\nline c\n")
        # body / closing delimiter unaffected by a "---" line inside the snapshot
        self.assertIn("## Transition log", path.read_text())

    def test_deferred_omits_snapshot_key(self):
        f = _fields(evidence_deferred=True)
        del f["evidence_snapshot"]
        self.assertEqual(self._file(f).returncode, 0)
        text = self._inbox()[0].read_text()
        self.assertNotIn("evidence_snapshot", text)
        self.assertIn("evidence_deferred: true", text)

    def test_refuses_bad_fields(self):
        cases = {
            "unknown key": _fields(bogus="x"),
            "bad verdict": _fields(verdict="healthy"),
            "bool as str": _fields(evidence_deferred="yes"),
            "multiline scalar": _fields(fault_id="a\nb"),
            "list-looking": _fields(fault_id="[a]"),
        }
        for name, f in cases.items():
            with self.subTest(name):
                r = self._file(f)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(self._inbox(), [])
        f = _fields()
        del f["episode_id"]
        self.assertNotEqual(self._file(f).returncode, 0)

    def test_refuses_duplicate_local_ahead_commit(self):
        self.assertEqual(self._file(_fields()).returncode, 0)  # --no-push: committed, local-ahead
        r = self._file(_fields())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already committed", r.stderr)
        self.assertEqual(len(self._inbox()), 1)

    def test_refuses_duplicate_on_origin(self):
        self.assertEqual(self._file(_fields()).returncode, 0)
        git("push", "origin", "HEAD:main", cwd=self.fx.repo)
        r = self._file(_fields())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already committed (origin", r.stderr)

    def test_untracked_working_tree_file_does_not_count(self):
        (self.fx.repo / "tickets" / "inbox" / "T-9999.md").write_text(
            "---\nid: T-9999\nepisode_id: ep-q-20261008T000000Z-aaaa0001\n---\n")
        r = self._file(_fields())
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_prefix_colliding_id_is_not_a_duplicate(self):
        self.assertEqual(self._file(_fields(episode_id="ep-q-1")).returncode, 0)
        git("push", "origin", "HEAD:main", cwd=self.fx.repo)
        self.assertEqual(self._file(_fields(episode_id="ep-q-12")).returncode, 0)

    def test_unknown_lookup_fails_closed(self):
        git("remote", "set-url", "origin", str(self.fx.root / "nonexistent.git"), cwd=self.fx.repo)
        r = self._file(_fields())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("fail-closed", r.stderr)
        self.assertEqual(self._inbox(), [])

    def test_lookup_only_runs_with_repair_flag(self):  # amendment E
        mark = self.fx.root / "mark"
        r = self.fx.ticket_cli(FILE_BASE, env_extra={"EPISODE_TEST_CORPUS_MARK": str(mark)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(mark.exists())
        p = self.fx.root / "rf.json"
        p.write_text(json.dumps(_fields()))
        r = self.fx.ticket_cli(FILE_BASE + ["--repair-fields-json", str(p)],
                               env_extra={"EPISODE_TEST_CORPUS_MARK": str(mark)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(mark.exists())

    def test_plain_file_has_no_repair_keys(self):
        self.assertEqual(self.fx.ticket_cli(FILE_BASE).returncode, 0)
        text = self._inbox()[0].read_text()
        for k in ("episode_id", "fault_id", "attempts_budget", "evidence_snapshot"):
            self.assertNotIn(k + ":", text)


_FM_KEY_RE = re.compile(r"^([a-z_][a-z0-9_]*):\s*(.*)$")


def _block_scalar_exempt(old, new, text: str) -> bool:
    """The base parser (pre-T-4707) cannot read a YAML block scalar by design:
    exempt a ticket iff the base raised, the new parser read it, and the ticket
    really carries a block -- a ticket.BLOCK_SCALAR_FIELDS value (ORG-076
    evidence_snapshot, first T-5030; the T-5678 security-gate fields, first
    T-5686) that contains a newline, or (T-5689) a `|` / `>` block opener on any
    other key. Merely HAVING a single-line threat_* key exempts nothing (r1 LOW-3)."""
    if not (isinstance(old, tuple) and old and old[0] == "ERR"):
        return False
    if not (isinstance(new, tuple) and new and isinstance(new[0], dict)):
        return False
    fields = new[0]
    if any(isinstance(fields.get(k), str) and "\n" in fields[k]
           for k in ticket.BLOCK_SCALAR_FIELDS):
        return True
    fm = text[4 : text.find("\n---", 4)] if text.startswith("---\n") else ""
    for line in fm.splitlines():
        m = _FM_KEY_RE.match(line.rstrip())
        if (m and m.group(1) not in ticket.BLOCK_SCALAR_FIELDS
                and _frontmatter_block.parse_block_header(m.group(2).strip()) is not None):
            return True
    return False


class ParserBackCompat(unittest.TestCase):
    """parse_front_matter output is unchanged for every existing ticket in the real corpus."""

    def _base_module(self):
        r = subprocess.run(["git", "show", f"{BASE_SHA}:tools/ticket.py"], cwd=SRC_ROOT,
                           capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest(f"base blob {BASE_SHA} not reachable here")
        spec = importlib.util.spec_from_loader("ticket_base_T4707", loader=None)
        mod = importlib.util.module_from_spec(spec)
        mod.__file__ = str(SRC_ROOT / "tools" / "ticket.py")
        sys.modules[spec.name] = mod  # dataclasses resolves annotations via sys.modules
        self.addCleanup(sys.modules.pop, spec.name, None)
        exec(compile(r.stdout, "ticket_base.py", "exec"), mod.__dict__)
        return mod

    def test_whole_corpus_parse_identical_to_base(self):
        base = self._base_module()
        n = 0
        for state in ("inbox", "active", "done"):
            for f in sorted((SRC_ROOT / "tickets" / state).glob("T-*.md")):
                text = f.read_text(encoding="utf-8")
                try:
                    old = base.parse_front_matter(text)
                except base.TicketError as e:
                    old = ("ERR", str(e))
                try:
                    new = ticket.parse_front_matter(text)
                except ticket.TicketError as e:
                    new = ("ERR", str(e))
                if _block_scalar_exempt(old, new, text):
                    continue
                self.assertEqual(old, new, f.name)
                n += 1
        self.assertGreater(n, 100)

    def test_block_scalar_fixtures_exempt_or_identical(self):
        """T-5689: a multi-line threat_path (the pre-mitigation T-5686, captured from
        git) is exempt -- the base parser can't read it, the new one does; the same
        ticket with a single-line threat_path parses identically through both."""
        base = self._base_module()
        multi = (SRC_ROOT / "tools" / "fixtures" / "frontmatter_block"
                 / "T-5686-pre-mitigation.md").read_text(encoding="utf-8")
        single = multi.replace("threat_path: |\n  ", "threat_path: ")
        self.assertNotEqual(multi, single)
        for text, want_exempt in ((multi, True), (single, False)):
            try:
                old = base.parse_front_matter(text)
            except base.TicketError as e:
                old = ("ERR", str(e))
            new = ticket.parse_front_matter(text)
            self.assertEqual(_block_scalar_exempt(old, new, text), want_exempt)
            if want_exempt:
                self.assertTrue(new[0]["threat_path"].startswith("a subagent steered"))
            else:
                self.assertEqual(old, new)

    def test_exemption_needs_a_real_block(self):
        """r1 LOW-3: a base-parser error on a ticket whose threat_* value is single-line
        (no block anywhere) is NOT exempt -- it must still be compared."""
        text = ("---\nid: T-1\ncreated: x\nrequester: a\ntarget: b\nstate: inbox\n"
                "threat_path: one line, no block\n---\n\nbody\n")
        new = ticket.parse_front_matter(text)
        self.assertFalse(_block_scalar_exempt(("ERR", "other failure"), new, text))
        block = text.replace("threat_path: one line, no block", "threat_path: |\n  one line")
        self.assertTrue(_block_scalar_exempt(("ERR", "x"), ticket.parse_front_matter(block), block))
        folded = text.replace("threat_path: one line, no block", "serves: >-\n  one line")
        self.assertTrue(_block_scalar_exempt(("ERR", "x"), ticket.parse_front_matter(folded), folded))

    def test_bare_pipe_value_on_other_keys_unchanged(self):
        text = "---\nid: T-1\ncreated: x\nrequester: a\ntarget: b\nstate: inbox\nserves: |\n---\n\nbody\n"
        self.assertEqual(ticket.parse_front_matter(text)[0]["serves"], "|")


class PurposeParserBlockScalar(unittest.TestCase):
    """tools/ticket_purpose.py has its own strict frontmatter parser; it must tolerate the
    evidence_snapshot block scalar or every corpus purpose scan breaks once one exists."""

    def test_purpose_parser_skips_block_scalar_and_keeps_following_keys(self):
        from tools import ticket_purpose
        text = ("---\nid: T-1\nevidence_snapshot: |\n  a: b\n\n  ---\n  c\nrepair_route: recovery@claude\n"
                "state: inbox\n---\n\n# t\n")
        fields = ticket_purpose._parse_frontmatter_text(text, "t")
        self.assertEqual(fields["repair_route"], "recovery@claude")
        self.assertEqual(fields["state"], "inbox")

    def test_purpose_parser_unchanged_for_other_keys(self):
        from tools import ticket_purpose
        with self.assertRaises(ticket_purpose.PurposeError):
            ticket_purpose._parse_frontmatter_text("---\nid: T-1\n  stray indented\n---\n", "t")


class LintDuplicateEpisode(unittest.TestCase):
    def test_flags_duplicate_episode_id_across_corpus(self):
        from tools import ticket_lint
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for st in ("inbox", "active", "done"):
                (root / st).mkdir()
            def mk(st, tid, ep):
                (root / st / f"{tid}.md").write_text(
                    f"---\nid: {tid}\ncreated: 2026-10-08T00:00:00Z\nrequester: recovery\ntarget: recovery\n"
                    f"state: {st}\nepisode_id: {ep}\n---\n\n# x\n")
            mk("inbox", "T-9001", "ep-a-1")
            mk("done", "T-9002", "ep-a-1")
            mk("inbox", "T-9003", "ep-a-2")
            found = ticket_lint.detect_duplicate_episode_ids(root)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].kind, "duplicate_episode_id")
            self.assertIn("T-9001", found[0].detail)
            self.assertIn("T-9002", found[0].detail)
            self.assertNotIn("T-9003", found[0].detail)

    def test_clean_corpus_has_no_findings(self):
        from tools import ticket_lint
        self.assertEqual(ticket_lint.detect_duplicate_episode_ids(SRC_ROOT / "tickets"), [])


if __name__ == "__main__":
    unittest.main()
