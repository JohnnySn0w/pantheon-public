"""T-5689: a YAML `|` / `>` block scalar in ticket frontmatter can never stop
ticket filing, and no reader mistakes block content for keys.

2026-10-27: T-5686 (filed through the T-5678 security gate) carried
`threat_path: |`. tools/ticket_purpose's parser special-cased only
evidence_snapshot, so it raised PurposeError, which broke
load_ticket_purpose_index -- and with it `ticket.py file` / `purpose`, the
staged-content gate and the lint digest. The lenient line readers would have
turned an indented `  state: done` inside a block into a real `state` key.

Fixtures (tools/fixtures/frontmatter_block/):
  T-5686-pre-mitigation.md  -- `git show 3c5100786e^:tickets/active/T-5686.md`,
                               the captured real ticket that broke the index.
  T-9901-synthetic-blocks.md -- `>` folded, `|` with adversarial `key: value`
                               continuation lines, `|-` strip and `|+` keep.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import _frontmatter_block as fb
from tools import _git_utils, ticket
from tools import ticket_purpose as tp

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "frontmatter_block"
PRE_T5686 = FIXTURES / "T-5686-pre-mitigation.md"
SYNTH = FIXTURES / "T-9901-synthetic-blocks.md"

T5686_THREAT_PATH = (
    "a subagent steered by injected content runs sudo or pantheon-sudo, reads the "
    "sudo password file or a token/credential file, edits a protected doc, or "
    "pushes/opens a PR from its shell\n"
)
SYNTH_EXPECTED = {
    "serves": "keep filing working when a ticket carries\na folded block scalar\n",
    "threat_path": "first line: with a colon\nstate: done\nlabels: [bogus]\n",
    "threat_justification": "strip chomping, no trailing newline",
    "not_security_reason": "keep chomping\n\n",
}
PLAIN = (
    "---\nid: T-9902\ncreated: 2026-10-27T16:30:00Z\nrequester: artisan\n"
    "target: role:engineering_orchestrator\nstate: inbox\nproject_id: null\n"
    "nearest_intent_ref: maintenance:ticket-board\nparent_ticket: null\n"
    "depends_on: [T-9901]\nlabels: [ticket-system, frontmatter]\n"
    "serves: plain one-line purpose: with a colon\n---\n\n# Plain ticket\n\n"
    "## Transition log\n\n- 2026-10-27T16:30:00Z [artisan] filed in inbox\n"
)

SPLITLINES_SEPARATORS = (
    "\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e",
    "\x85", "\u2028", "\u2029",
)


def _fm(text: str) -> list[str]:
    return text[4 : text.find("\n---\n", 4)].splitlines()


class LeafParser(unittest.TestCase):
    def test_headers(self) -> None:
        self.assertEqual(fb.parse_block_header("|"), ("|", "", None))
        self.assertEqual(fb.parse_block_header(">-"), (">", "-", None))
        self.assertEqual(fb.parse_block_header("|+"), ("|", "+", None))
        self.assertEqual(fb.parse_block_header("|2-"), ("|", "-", 2))
        self.assertEqual(fb.parse_block_header(">-4"), (">", "-", 4))
        for not_block in ("", "||", "|x", "| a", "a|", ">0", "[|]", "null"):
            self.assertIsNone(fb.parse_block_header(not_block), not_block)

    def test_literal_chomping(self) -> None:
        lines = ["  a", "", "  b", "", "", "next: x"]
        self.assertEqual(fb.consume_block(lines, 0, "|"), ("a\n\nb\n", 5))
        self.assertEqual(fb.consume_block(lines, 0, "|-"), ("a\n\nb", 5))
        self.assertEqual(fb.consume_block(lines, 0, "|+"), ("a\n\nb\n\n\n", 5))

    def test_folded(self) -> None:
        lines = ["  one", "  two", "", "  three", "    more", "  four"]
        self.assertEqual(fb.consume_block(lines, 0, ">")[0], "one two\nthree\n  more\nfour\n")
        self.assertEqual(fb.consume_block(["  x", "  y"], 0, ">-")[0], "x y")

    def test_indent_detection_and_explicit_indicator(self) -> None:
        self.assertEqual(fb.consume_block(["    a", "      b", "  c: d"], 0, "|"),
                         ("a\n  b\n", 2))
        self.assertEqual(fb.consume_block(["    a", "  b"], 0, "|2")[0], "  a\nb\n")

    def test_no_content_consumes_nothing(self) -> None:
        self.assertEqual(fb.consume_block(["next: x"], 0, "|"), (None, 0))
        self.assertEqual(fb.consume_block(["", "next: x"], 0, ">"), (None, 0))
        self.assertEqual(fb.consume_block([], 0, "|"), (None, 0))

    def test_render_reads_back(self) -> None:
        for value in ("a\nb\n", "a\nb", "a\n\n\n", "  lead\nx\n", "x\n\n  y\n"):
            rendered = fb.render_block(value).split("\n")
            header, body = rendered[0], rendered[1:] + ["next: z"]
            self.assertEqual(fb.consume_block(body, 0, header)[0], value, value)
        # Block fields: bare `|` always (old-writer bytes), fixed 2-space read-back.
        for value in ("a\nb\n", "  lead\nx\n", "  all\n\n  indented\n"):
            rendered = fb.render_block(value, always_clip=True).split("\n")
            self.assertEqual(rendered[0], "|")
            self.assertEqual(
                fb.consume_block(rendered[1:] + ["next: z"], 0, "|", key="threat_path")[0], value)

    def test_agrees_with_pyyaml_when_available(self) -> None:
        try:
            import yaml  # noqa: PLC0415 -- optional oracle
        except ImportError:
            self.skipTest("PyYAML not installed")
        for path in (SYNTH, PRE_T5686):
            text = path.read_text(encoding="utf-8")
            loaded = yaml.safe_load("\n".join(_fm(text)))
            fields, _ = ticket.parse_front_matter(text)
            for key, value in fields.items():
                if isinstance(value, str) and "\n" in value:
                    self.assertEqual(loaded[key], value, f"{path.name}:{key}")


class PurposeIndex(unittest.TestCase):
    def test_index_loads_block_scalar_tickets_with_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "active").mkdir()
            (root / "inbox").mkdir()
            shutil.copy(PRE_T5686, root / "active" / "T-5686.md")
            shutil.copy(SYNTH, root / "active" / "T-9901.md")
            (root / "inbox" / "T-9902.md").write_text(PLAIN, encoding="utf-8")
            index = tp.load_ticket_purpose_index(root)
        self.assertEqual(set(index), {"T-5686", "T-9901", "T-9902"})
        self.assertEqual(index["T-5686"].nearest_intent_ref, "maintenance:worker-guard")
        self.assertEqual(index["T-9901"].state, "active")  # not the `  state: done` line
        self.assertEqual(index["T-9901"].labels, frozenset({"ticket-system", "frontmatter"}))
        self.assertEqual(index["T-9901"].serves,
                         "keep filing working when a ticket carries\na folded block scalar")

    def test_purpose_parser_keeps_block_values(self) -> None:
        f = tp._parse_frontmatter_text(PRE_T5686.read_text(encoding="utf-8"), "t")
        self.assertEqual(f["threat_path"], T5686_THREAT_PATH)
        self.assertEqual(f["security_rank"], "primary")
        s = tp._parse_frontmatter_text(SYNTH.read_text(encoding="utf-8"), "t")
        for key, value in SYNTH_EXPECTED.items():
            self.assertEqual(s[key], value, key)
        self.assertEqual(s["state"], "active")

    def test_ticket_and_purpose_parsers_agree(self) -> None:
        for path in (PRE_T5686, SYNTH):
            text = path.read_text(encoding="utf-8")
            tf, _ = ticket.parse_front_matter(text)
            pf = tp._parse_frontmatter_text(text, path.name)
            self.assertEqual(set(tf), set(pf), path.name)
            for key, value in tf.items():
                if isinstance(value, str) and "\n" in value:
                    self.assertEqual(pf[key], value, f"{path.name}:{key}")
        tf, _ = ticket.parse_front_matter(SYNTH.read_text(encoding="utf-8"))
        for key, value in SYNTH_EXPECTED.items():
            self.assertEqual(tf[key], value, key)

    def test_plain_frontmatter_unchanged(self) -> None:
        pf = dict(tp._parse_frontmatter_text(PLAIN, "t"))
        self.assertEqual(pf["serves"], "plain one-line purpose: with a colon")
        self.assertEqual(pf["depends_on"], ["T-9901"])
        tf, body = ticket.parse_front_matter(PLAIN)
        self.assertEqual(tf["serves"], "plain one-line purpose: with a colon")
        self.assertTrue(body.startswith("# Plain ticket"))


class LenientReaders(unittest.TestCase):
    """Indented block content never creates or overwrites a key."""

    BOGUS = ("first line", "  state", "  labels", "keep chomping", "a folded block scalar")

    def _check(self, name: str, fields: dict, *, state_key: str = "state") -> None:
        self.assertEqual(fields.get(state_key), "active", name)
        for k in self.BOGUS:
            self.assertNotIn(k, fields, name)
        labels = fields.get("labels")
        self.assertNotIn("bogus", str(labels), name)

    def _plain_path(self, td: str) -> Path:
        p = Path(td) / "T-9902.md"
        p.write_text(PLAIN, encoding="utf-8")
        return p

    def test_weekly_ticket_summary(self) -> None:
        from tools import weekly_ticket_summary as wts
        self._check("wts", wts._parse_frontmatter(SYNTH.read_text(encoding="utf-8")))
        f = wts._parse_frontmatter(PRE_T5686.read_text(encoding="utf-8"))
        self.assertEqual(f["state"], "active")
        self.assertEqual(f["security_rank"], "primary")
        plain = wts._parse_frontmatter(PLAIN)
        self.assertEqual(plain["serves"], "plain one-line purpose: with a colon")
        self.assertEqual(plain["state"], "inbox")

    def test_spine_index_groom(self) -> None:
        from tools import spine_index_groom as sig
        self._check("sig", sig._parse_frontmatter(SYNTH))
        with tempfile.TemporaryDirectory() as td:
            plain = sig._parse_frontmatter(self._plain_path(td))
        self.assertEqual(plain["serves"], "plain one-line purpose: with a colon")
        self.assertEqual(plain["depends_on"], "[T-9901]")

    def test_ticket_state_sweep(self) -> None:
        from tools import ticket_state_sweep as tss
        self._check("tss", tss._parse_ticket_front_matter(SYNTH))
        with tempfile.TemporaryDirectory() as td:
            plain = tss._parse_ticket_front_matter(self._plain_path(td))
        self.assertEqual(plain["state"], "inbox")
        self.assertEqual(plain["depends_on"], ["T-9901"])

    def test_links(self) -> None:
        from tools import links
        self._check("links", links.read_doc(SYNTH).fields)
        with tempfile.TemporaryDirectory() as td:
            plain = links.read_doc(self._plain_path(td)).fields
        self.assertEqual(plain["state"], "inbox")
        self.assertEqual(plain["depends_on"], ["T-9901"])

    def test_quant_delivery_collect(self) -> None:
        from tools import quant_delivery_collect as qdc
        with tempfile.TemporaryDirectory() as td:
            adv = Path(td) / "T-9903.md"
            # parent_ticket appears ONLY as an indented continuation line.
            adv.write_text(
                "---\nid: T-9903\ncreated: 2026-10-27T16:30:00Z\nrequester: artisan\n"
                "target: orchestrator\nstate: active\nthreat_path: |\n  parent_ticket: T-0001\n"
                "  state: done\n---\n\n# adversarial\n",
                encoding="utf-8",
            )
            info = qdc.parse_ticket_file(adv)
            plain = qdc.parse_ticket_file(self._plain_path(td))
        self.assertIsNotNone(info)
        self.assertEqual(info.state, "active")
        self.assertIsNone(info.parent_ticket)
        self.assertEqual(plain.ticket_id, "T-9902")
        self.assertEqual(plain.state, "inbox")


class OldWriterLeadingSpace(unittest.TestCase):
    """r1 MED-1: origin/main's writer rendered a block-field value whose lines
    start with spaces with a bare `|` and a fixed 2-space indent ("    a" /
    "  b"). Fixture T-9904 is that writer's literal output (captured once from
    origin/main e541c6670ad2, not imported here). YAML auto-detection would read
    indent 4 and break on "  b" -- the outage class again -- or silently drop
    the leading spaces when every line is indented."""

    OLD = FIXTURES / "T-9904-old-writer-leading-space.md"
    WANT = {"threat_path": "  a\nb\n", "threat_justification": "  every line\n\n  indented\n"}

    def test_both_parsers_read_old_writer_bytes_back(self) -> None:
        text = self.OLD.read_text(encoding="utf-8")
        tf, _ = ticket.parse_front_matter(text)
        pf = tp._parse_frontmatter_text(text, "t")
        for key, value in self.WANT.items():
            self.assertEqual(tf[key], value, key)
            self.assertEqual(pf[key], value, key)
        self.assertEqual(tf["delivered_to"], "artisan")

    def test_purpose_index_loads_old_writer_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "active").mkdir()
            shutil.copy(self.OLD, root / "active" / "T-9904.md")
            self.assertIn("T-9904", tp.load_ticket_purpose_index(root))

    def test_new_writer_matches_old_writer_bytes(self) -> None:
        text = self.OLD.read_text(encoding="utf-8")
        fields, _ = ticket.parse_front_matter(text)
        self.assertEqual(ticket.serialize_front_matter(fields), text[: text.find("\n---\n", 4) + 5])

    def test_non_block_keys_stay_yaml_faithful(self) -> None:
        text = ("---\nid: T-1\ncreated: x\nrequester: a\ntarget: b\nstate: inbox\n"
                "serves: |\n    a\n    b\n---\n\n# t\n")
        self.assertEqual(ticket.parse_front_matter(text)[0]["serves"], "a\nb\n")
        self.assertEqual(tp._parse_frontmatter_text(text, "t")["serves"], "a\nb\n")
        # An explicit indicator on a block field is honoured as written.
        explicit = text.replace("serves: |", "threat_path: |4")
        self.assertEqual(ticket.parse_front_matter(explicit)[0]["threat_path"], "a\nb\n")


class SingleLineBlockFieldsWritePlain(unittest.TestCase):
    """T-5689 item 3 (orchestrator): every --threat-chain filing used to write
    `threat_path: |`, which pre-T-5689 readers reject, so seats on old code went
    red until the ticket was flattened by hand. A single-line block-field value
    now writes as a plain scalar. Fixture: the new writer's bytes for tricky
    values plus what origin/main's two readers parsed from those exact bytes
    (captured once; no old code is imported here)."""

    CAPTURE = FIXTURES / "single_line_block_fields_main_readers.json"

    def test_old_and_new_readers_parse_the_plain_form(self) -> None:
        import json
        data = json.loads(self.CAPTURE.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(data["cases"]), 20)
        for case in data["cases"]:
            value, text = case["value"], case["text"]
            fields, _ = ticket.parse_front_matter(text)
            # The writer still produces exactly the captured bytes.
            self.assertEqual(ticket.serialize_front_matter(fields) + "\n# fixture\n", text, value)
            self.assertIn(f"\nthreat_path: {value}\n", text)
            # origin/main's readers (captured) and the new ones all read it back exactly.
            self.assertEqual(case["main_parse_front_matter"], value)
            self.assertEqual(case["main_ticket_purpose"], value)
            self.assertEqual(fields["threat_path"], value)
            self.assertEqual(tp._parse_frontmatter_text(text, "t")["threat_path"], value)

    def test_unsafe_or_multiline_values_stay_block(self) -> None:
        base = {"id": "T-1", "created": "x", "requester": "a", "target": "b", "state": "inbox"}
        for value in ("two\nlines\n", " leading", "trailing ", "null", "[a, b]", "|", ">-"):
            out = ticket.serialize_front_matter({**base, "threat_path": value})
            self.assertIn("\nthreat_path: |\n", out, repr(value))
        # r2 HIGH: every str.splitlines() separator, not just \n / \r, forbids plain.
        for sep in ("\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e",
                    "\x85", " ", " "):
            value = f"a{sep}b"
            self.assertFalse(fb.plain_scalar_safe(value), repr(sep))
            out = ticket.serialize_front_matter({**base, "threat_path": value})
            self.assertNotIn(f"\nthreat_path: {value}\n", out, repr(sep))
            self.assertIn("\nthreat_path: |", out, repr(sep))
        out = ticket.serialize_front_matter({**base, "threat_path": "plain text: ok"})
        self.assertIn("\nthreat_path: plain text: ok\n", out)


class WriterRoundTrip(unittest.TestCase):
    def test_block_tickets_round_trip_values(self) -> None:
        for path in (PRE_T5686, SYNTH):
            fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
            again, _ = ticket.parse_front_matter(
                ticket.serialize_front_matter(fields) + "\n# t\n")
            # BLOCK_SCALAR_FIELDS re-render as clip `|` (the pre-T-5689 on-disk
            # format), so a `|+` value normalizes to one newline -- except a
            # single-line value (from `|-`), which now writes plain and is exact.
            for key, value in fields.items():
                if key in ticket.BLOCK_SCALAR_FIELDS and isinstance(value, str):
                    want = value if fb.plain_scalar_safe(value) else value.rstrip("\n") + "\n"
                    self.assertEqual(again[key], want, key)
                else:
                    self.assertEqual(again[key], value, f"{path.name}:{key}")

    def test_pre_mitigation_t5686_round_trips_byte_identical(self) -> None:
        text = PRE_T5686.read_text(encoding="utf-8")
        fields, _ = ticket.parse_front_matter(text)
        self.assertEqual(ticket.serialize_front_matter(fields),
                         text[: text.find("\n---\n", 4) + 5])

    def test_non_block_multiline_values_keep_exact_chomping(self) -> None:
        base = {"id": "T-1", "created": "x", "requester": "a", "target": "b", "state": "inbox"}
        for value in ("a\nb", "a\nb\n", "a\n\n\n", "  lead\nx\n"):
            out = ticket.serialize_front_matter({**base, "serves": value})
            self.assertEqual(ticket.parse_front_matter(out + "\n# t\n")[0]["serves"], value)

    def test_plain_ticket_byte_identical(self) -> None:
        fields, _ = ticket.parse_front_matter(PLAIN)
        self.assertEqual(ticket.serialize_front_matter(fields), PLAIN[: PLAIN.find("\n---\n", 4) + 5])


class NonLfLineSeparators(unittest.TestCase):
    """T-5694: only LF delimits frontmatter source lines.

    ``str.splitlines()`` also splits eight control characters and two Unicode
    separators.  Those are value bytes, not ticket-format delimiters; treating
    them as source lines made one written value corrupt the whole purpose index.
    """

    BASE = {
        "id": "T-1", "created": "x", "requester": "a", "target": "b",
        "state": "inbox",
    }

    def test_both_readers_preserve_every_splitlines_separator_in_a_block(self) -> None:
        for sep in SPLITLINES_SEPARATORS:
            with self.subTest(separator=repr(sep)):
                value = f"left{sep}right"
                text = (
                    ticket.serialize_front_matter({**self.BASE, "serves": value})
                    + "\n# t\n"
                )
                self.assertEqual(ticket.parse_front_matter(text)[0]["serves"], value)
                self.assertEqual(tp._parse_frontmatter_text(text, "t")["serves"], value)

    def test_non_block_writer_uses_a_block_and_round_trips_every_separator(self) -> None:
        for sep in SPLITLINES_SEPARATORS:
            with self.subTest(separator=repr(sep)):
                value = f"left{sep}right"
                rendered = ticket.serialize_front_matter({**self.BASE, "serves": value})
                self.assertIn("\nserves: |", rendered)
                fields, _ = ticket.parse_front_matter(rendered + "\n# t\n")
                self.assertEqual(fields["serves"], value)


def _git(cwd: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True, check=True)


class FileWithBlockScalarCorpus(unittest.TestCase):
    """Regression: `ticket.py file` succeeds while the corpus holds a block-scalar
    ticket (the 2026-10-27 outage). Temp repo + bare origin; the real purpose
    check runs (not stubbed)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        origin = base / "origin.git"
        self.root = base / "work"
        _git(base, "init", "--bare", "-b", "main", str(origin))
        _git(base, "clone", str(origin), str(self.root))
        _git(self.root, "config", "user.email", "t@example.invalid")
        _git(self.root, "config", "user.name", "t")
        _git(self.root, "checkout", "-b", "main")
        for s in ticket.STATES:
            (self.root / "tickets" / s).mkdir(parents=True)
            (self.root / "tickets" / s / ".gitkeep").write_text("")
        (self.root / "tickets" / "README.md").write_text(
            "# Tickets\n\n## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n\n## Related\n\n- Link\n",
            encoding="utf-8",
        )
        shutil.copy(PRE_T5686, self.root / "tickets" / "active" / "T-5686.md")
        shutil.copy(SYNTH, self.root / "tickets" / "active" / "T-9901.md")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "init")
        _git(self.root, "push", "origin", "main")
        root = self.root
        real_git = _git_utils._git

        def _git_in_root(*a, cwd=None):
            return real_git(*a, cwd=cwd if cwd is not None else root)

        self._patches = [
            patch.object(ticket, "REPO_ROOT", root),
            patch.object(ticket, "TICKETS_ROOT", root / "tickets"),
            patch.object(_git_utils, "REPO_ROOT", root),
            patch.object(ticket, "_git", _git_in_root),
            patch("tools.ticket_lint.ratifiable_path_status", return_value=True),
            patch.dict(os.environ, {
                "PANTHEON_ACTOR": "orchestrator",
                "PANTHEON_TICKET_ALLOC_LOCKFILE": str(base / "alloc.lock"),
            }),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _args(self) -> MagicMock:
        a = MagicMock()
        a.requester = "orchestrator"
        a.target = "role:system_orchestrator"
        a.title = "Block-scalar corpus filing regression"
        a.labels = None
        a.depends_on = None
        a.stale_after = None
        a.not_before = None
        a.ratifiable_file = None
        a.ask = "test"
        a.context = "ctx"
        a.parent_ticket = None
        a.no_push = True
        a.spawn_impl = False
        a.terminal = False
        a.impl_title = None
        a.impl_ask = None
        a.appetite = None
        a.bet_ref = None
        a.impl_target = None
        a.impl_existing = None
        a.orphan = "test fixture: T-5689 filing regression, not DAG shape"
        a.no_file = None
        a.intent = None
        return a

    def test_file_succeeds_with_block_scalar_ticket_in_corpus(self) -> None:
        real = ticket._purpose_issues_for_fields
        with patch.object(ticket, "_purpose_issues_for_fields", side_effect=real) as spy:
            self.assertEqual(ticket.cmd_file(self._args()), 0)
        spy.assert_called_once()  # the real corpus purpose index was loaded
        filed = [p for p in (self.root / "tickets" / "inbox").glob("T-*.md")]
        self.assertEqual(len(filed), 1)
        fields, _ = ticket.parse_front_matter(filed[0].read_text(encoding="utf-8"))
        self.assertEqual(fields["state"], "inbox")


if __name__ == "__main__":
    unittest.main()
