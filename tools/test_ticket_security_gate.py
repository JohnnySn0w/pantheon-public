"""T-5678: the ORG-105 security-ticket gate in `ticket.py file`.

Arc intent: security-shaped tickets are reviewed against the ratified threat model
(ORG-105 section 4) BEFORE they can enter the work queue.

Layer-3: real tmp git repo (same fixture as tools/test_ticket_clear_operator_intent.py);
the real tickets/ tree is never touched (no_push=True throughout).

Run: python3 -m unittest tools.test_ticket_security_gate -v
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import _security_gate as sg
from tools import inbox_consumer, ticket
from tools.test_ticket_decision_register import RegisterGateTest, _git

ADR = Path(__file__).resolve().parent.parent / "architecture" / "adr" / "ORG-105-threat-model.md"
CHAIN = "foothold=untrusted-content;impact=money-credentials;path=injected page makes agent print a token"


def _file_args(**over) -> MagicMock:
    a = MagicMock()
    a.requester = "orchestrator"
    a.target = "role:engineering_orchestrator"
    a.title = "Tidy the docs index"
    a.labels = None
    a.depends_on = None
    a.stale_after = None
    a.not_before = None
    a.ratifiable_file = None
    a.ask = "do it"
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
    a.orphan = "test fixture: gate tests, not DAG shape"
    a.no_file = None
    a.intent = None
    a.threat_chain = None
    a.path_justification = None
    a.not_security = None
    a.automated_filer = None
    for k, v in over.items():
        setattr(a, k, v)
    return a


def _pending_text(tid: str, *, gate: str | None = "pending", state: str = "inbox",
                  target: str = "role:engineering_orchestrator", requester: str = "artisan") -> str:
    fm = ["---", f"id: {tid}", "created: 2026-10-27T10:00:00Z", f"requester: {requester}",
          f"target: {target}", f"state: {state}", "parent_ticket: null", "depends_on: []",
          "labels: [security]"]
    if gate:
        fm.append(f"security_gate: {gate}")
    fm += ["---", "", f"# Title for {tid}", "", "## Ask", "", "Do it.", "",
           "## Transition log", "", "- 2026-10-27T10:00:00Z [artisan] filed in inbox", ""]
    return "\n".join(fm)


class _Base(unittest.TestCase):
    setUp = RegisterGateTest.setUp
    tearDown = RegisterGateTest.tearDown
    _run = RegisterGateTest._run

    def _file_ok(self, **over) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(ticket.cmd_file(_file_args(**over)), 0)
        m = re.search(r"T-\d+", buf.getvalue())
        self.assertIsNotNone(m, buf.getvalue())
        return m.group(0)

    def _fields(self, tid: str, state: str = "inbox") -> dict:
        p = self.repo / "tickets" / state / f"{tid}.md"
        return ticket.parse_front_matter(p.read_text(encoding="utf-8"))[0]

    def _put(self, tid: str, state: str = "inbox", **kw) -> None:
        (self.repo / "tickets" / state / f"{tid}.md").write_text(
            _pending_text(tid, state=state, **kw), encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", f"add {tid}")

    def _inbox_files(self) -> list[str]:
        return sorted(p.name for p in (self.repo / "tickets" / "inbox").glob("T-*.md"))


class DetectionTest(unittest.TestCase):
    def test_label_hit(self) -> None:
        self.assertTrue(sg.label_hits(["x", "worker-guard"]))
        self.assertTrue(sg.label_hits(["hardening"]))
        self.assertFalse(sg.label_hits(["audit", "usability"]))

    def test_keyword_hits_whole_word_case_insensitive(self) -> None:
        for text in ("Fix SUDO path", "guard bypass via env", "privilege escalation",
                     "escalates on retry", "CVE-2026-1234 in dep", "token leaked to log",
                     "exfiltration risk", "credentials rotate", "attacker-controlled"):
            self.assertTrue(sg.keyword_hits(text), text)
        for text in ("pseudo-random sorting", "bleak outlook", "Tidy the docs index",
                     "resudo rename"):
            self.assertFalse(sg.keyword_hits(text), text)


class AdrSyncTest(unittest.TestCase):
    """Pinned slug maps must match ORG-105's text (documentation is code)."""

    def _adr(self) -> str:
        return ADR.read_text(encoding="utf-8")

    def test_matrix_matches_adr(self) -> None:
        lines = [ln.strip() for ln in self._adr().splitlines() if ln.strip().startswith("|")]
        header = [c.strip() for c in lines[0].strip("|").split("|")][1:]
        self.assertEqual(header, [sg.ASSET_COLUMN[s] for s in sg.ASSETS])
        rows = {}
        for ln in lines[2:]:
            cells = [c.strip() for c in ln.strip("|").split("|")]
            rows[cells[0]] = cells[1:]
        self.assertEqual(set(rows), {sg.FOOTHOLD_ROW[s] for s in sg.FOOTHOLDS})
        for f in sg.FOOTHOLDS:
            expect = rows[sg.FOOTHOLD_ROW[f]]
            for a, cell in zip(sg.ASSETS, expect):
                self.assertEqual(sg.MATRIX[(f, a)], cell, (f, a))

    def test_section_bullet_counts_and_order(self) -> None:
        adr = self._adr()
        s1 = adr.split("2. **Who we defend against", 1)[0].split("The assets, in order:", 1)[1]
        assets = re.findall(r"^\s*- \*\*(.+?)\*\*", s1, re.M)
        self.assertEqual(len(assets), len(sg.ASSETS))
        s2 = adr.split("2. **Who we defend against (in-scope footholds).**", 1)[1].split("3. **", 1)[0]
        foots = re.findall(r"^\s*- \*\*(.+?)\*\*", s2, re.M)
        self.assertEqual(len(foots), len(sg.FOOTHOLDS))
        for slug, got in zip(sg.ASSETS, assets):
            self.assertEqual(sg.ASSET_TITLE[slug], got.rstrip(":."))
        for slug, got in zip(sg.FOOTHOLDS, foots):
            self.assertEqual(sg.FOOTHOLD_TITLE[slug], got.rstrip(":."))


class FileGateTest(_Base):
    def test_non_security_ticket_unaffected(self) -> None:
        tid = self._file_ok()
        f = self._fields(tid)
        self.assertNotIn("security_gate", f)

    def test_refused_without_chain_label(self) -> None:
        with self.assertRaises(ticket.TicketRefusal) as cm:
            ticket.cmd_file(_file_args(labels="security"))
        msg = str(cm.exception)
        self.assertIn("ORG-105", msg)
        self.assertIn("--threat-chain", msg)
        self.assertIn("untrusted-content", msg)
        self.assertIn("money-credentials", msg)
        self.assertEqual(self._inbox_files(), [])

    def test_refusal_message_is_short(self) -> None:
        with self.assertRaises(ticket.TicketRefusal) as cm:
            ticket.cmd_file(_file_args(title="worker-guard bypass via env (HIGH)"))
        msg = str(cm.exception)
        self.assertLessEqual(len(msg.splitlines()), 4)
        self.assertIn("--not-security", msg)

    def test_refused_by_keyword_in_ask(self) -> None:
        with self.assertRaises(ticket.TicketRefusal):
            ticket.cmd_file(_file_args(ask="an attacker can read the file"))

    def test_accept_valid_chain_writes_pending(self) -> None:
        tid = self._file_ok(labels="security", threat_chain=CHAIN)
        f = self._fields(tid)
        self.assertEqual(f["security_gate"], "pending")
        self.assertEqual(f["threat_foothold"], "untrusted-content")
        self.assertEqual(f["threat_impact"], "money-credentials")
        self.assertIn("injected page", f["threat_path"])

    def test_invalid_foothold_refused(self) -> None:
        bad = CHAIN.replace("untrusted-content", "root-shell")
        with self.assertRaises(ticket.TicketRefusal) as cm:
            ticket.cmd_file(_file_args(labels="security", threat_chain=bad))
        self.assertIn("root-shell", str(cm.exception))

    def test_invalid_impact_refused(self) -> None:
        bad = CHAIN.replace("money-credentials", "everything")
        with self.assertRaises(ticket.TicketRefusal):
            ticket.cmd_file(_file_args(labels="security", threat_chain=bad))

    def test_malformed_and_empty_path_refused(self) -> None:
        for bad in ("foothold=supply-chain", "supply-chain->integrity",
                    "foothold=supply-chain;impact=integrity;path=  "):
            with self.subTest(bad=bad), self.assertRaises(ticket.TicketRefusal):
                ticket.cmd_file(_file_args(labels="security", threat_chain=bad))

    def test_dash_cell_needs_justification(self) -> None:
        chain = "foothold=guest-voice;impact=money-credentials;path=guest asks voice agent for key"
        with self.assertRaises(ticket.TicketRefusal) as cm:
            ticket.cmd_file(_file_args(labels="security", threat_chain=chain))
        self.assertIn("--path-justification", str(cm.exception))
        tid = self._file_ok(labels="security", threat_chain=chain,
                            path_justification="voice tool reads the keyring, shown by T-1")
        f = self._fields(tid)
        self.assertEqual(f["security_gate"], "pending")
        self.assertIn("keyring", f["threat_justification"])

    def test_not_security_passes_keyword_only(self) -> None:
        tid = self._file_ok(title="Memex injection bar precision",
                            not_security="injection here means prompt-context injection of memory")
        f = self._fields(tid)
        self.assertNotIn("security_gate", f)
        self.assertIn("prompt-context", f["not_security_reason"])

    def test_not_security_refused_for_labelled(self) -> None:
        with self.assertRaises(ticket.TicketRefusal) as cm:
            ticket.cmd_file(_file_args(labels="worker-guard", not_security="not really"))
        self.assertIn("label", str(cm.exception))

    def test_not_security_empty_reason_refused(self) -> None:
        with self.assertRaises(ticket.TicketRefusal):
            ticket.cmd_file(_file_args(title="sudo thing", not_security="  "))

    def test_chain_and_not_security_conflict(self) -> None:
        with self.assertRaises(ticket.TicketRefusal):
            ticket.cmd_file(_file_args(labels="security", threat_chain=CHAIN, not_security="x"))

    def test_refusal_exit_code_is_refusal(self) -> None:
        self.assertTrue(issubclass(ticket.TicketRefusal, ticket.TicketError))
        parser = ticket._build_parser()
        ns = parser.parse_args([
            "file", "--requester", "orchestrator", "--target", "role:engineering_orchestrator",
            "--title", "t", "--threat-chain", CHAIN, "--path-justification", "j",
            "--not-security", "r"])
        self.assertEqual(ns.threat_chain, CHAIN)
        self.assertEqual(ns.path_justification, "j")
        self.assertEqual(ns.not_security, "r")


class LabelAddGateTest(_Base):
    def _label_args(self, tid: str, label: str, **kw) -> argparse.Namespace:
        d = dict(ticket=tid, labels=[label], no_push=True, threat_chain=None,
                 path_justification=None, not_security=None)
        d.update(kw)
        return argparse.Namespace(**d)

    def _plain(self, tid: str) -> None:
        p = self.repo / "tickets" / "inbox" / f"{tid}.md"
        p.write_text(_pending_text(tid, gate=None).replace("labels: [security]", "labels: [test]"),
                     encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", f"add {tid}")

    def test_security_label_add_needs_chain(self) -> None:
        self._plain("T-9201")
        with self.assertRaises(ticket.TicketRefusal):
            self._run(ticket.cmd_label_add, self._label_args("T-9201", "security"))
        self.assertNotIn("security", self._fields("T-9201").get("labels", []))

    def test_security_label_add_with_chain_goes_pending(self) -> None:
        self._plain("T-9202")
        self._run(ticket.cmd_label_add, self._label_args("T-9202", "security", threat_chain=CHAIN))
        f = self._fields("T-9202")
        self.assertEqual(f["security_gate"], "pending")
        self.assertIn("security", f["labels"])

    def test_non_security_label_add_unaffected(self) -> None:
        self._plain("T-9203")
        self._run(ticket.cmd_label_add, self._label_args("T-9203", "usability"))
        self.assertNotIn("security_gate", self._fields("T-9203"))


class PendingInvisibleTest(_Base):
    def _parse(self, *argv: str) -> argparse.Namespace:
        ns = ticket._build_parser().parse_args(list(argv))
        ns.no_push = True
        return ns

    def test_claim_refused_when_pending(self) -> None:
        self._put("T-9301")
        with self.assertRaises(ticket.TicketRefusal) as cm:
            self._run(ticket.cmd_claim, self._parse("claim", "T-9301"))
        self.assertIn("security", str(cm.exception).lower())
        self.assertTrue((self.repo / "tickets/inbox/T-9301.md").exists())

    def test_claim_allowed_when_accepted(self) -> None:
        self._put("T-9302", gate="accepted")
        self.assertEqual(self._run(ticket.cmd_claim, self._parse("claim", "T-9302")), 0)

    def test_ready_hides_pending(self) -> None:
        self._put("T-9303")
        self._put("T-9304", gate=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ticket.cmd_ready(self._parse("ready"))
        self.assertIn("T-9304", buf.getvalue())
        self.assertNotIn("T-9303", buf.getvalue())

    def test_prime_moves_pending_out_of_ready(self) -> None:
        self._put("T-9305")
        self._put("T-9306", gate=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ticket.cmd_prime(self._parse("prime"))
        data = json.loads(buf.getvalue())
        ready_ids = [t["id"] for t in data["ready"]]
        self.assertIn("T-9306", ready_ids)
        self.assertNotIn("T-9305", ready_ids)
        self.assertEqual(data["counts"]["security_pending"], 1)

    def test_list_marks_pending(self) -> None:
        self._put("T-9307")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), patch.object(ticket, "_record_priority_list_health"):
            ticket.cmd_list(self._parse("list", "--sort", "created"))
        self.assertIn("[security-pending]", buf.getvalue())

    def test_consumer_skips_pending(self) -> None:
        self._put("T-9308")
        with patch.object(inbox_consumer, "TICKETS_ROOT", self.repo / "tickets"):
            self.assertTrue(inbox_consumer._is_non_workable(self._fields("T-9308")))
            self.assertFalse(inbox_consumer._is_non_workable(
                dict(self._fields("T-9308"), security_gate="accepted")))

    def test_consumer_list_eligible_skips_pending(self) -> None:
        self._put("T-9309")
        with patch.object(inbox_consumer, "TICKETS_ROOT", self.repo / "tickets"), \
                patch.object(inbox_consumer, "_target_addresses_seat", return_value=True), \
                patch.object(inbox_consumer, "_load_grants", return_value=None), \
                patch.object(inbox_consumer, "resolve_role", return_value="engineering_orchestrator"), \
                patch.object(inbox_consumer, "_safe_functional_roles", return_value=frozenset()):
            with self.assertRaises(inbox_consumer.SkipCycle) as cm:
                inbox_consumer.list_eligible_tickets("artisan")
        self.assertEqual(str(cm.exception), "security_pending")


class KeepGoingParityTest(_Base):
    def test_keep_going_does_not_count_pending(self) -> None:
        from tools import keep_going
        self._put("T-9310")
        self._put("T-9311", gate="accepted")
        root = self.repo / "tickets"
        tg = frozenset({"role:engineering_orchestrator"})
        self.assertFalse(keep_going._ticket_is_actionable(root / "inbox/T-9310.md", tg, root))
        self.assertTrue(keep_going._ticket_is_actionable(root / "inbox/T-9311.md", tg, root))


class VerbsTest(_Base):
    def _acc(self, tid: str, **kw) -> argparse.Namespace:
        d = dict(ticket=tid, rank="primary", note="chain is real: ok", no_push=True,
                 threat_chain=None, path_justification=None, not_security=None)
        d.update(kw)
        return argparse.Namespace(**d)

    def _rej(self, tid: str, **kw) -> argparse.Namespace:
        d = dict(ticket=tid, note="no foothold", no_push=True)
        d.update(kw)
        return argparse.Namespace(**d)

    def test_ho_accept(self) -> None:
        self._put("T-9401")
        self.assertEqual(self._run(ticket.cmd_security_accept, self._acc("T-9401")), 0)
        f = self._fields("T-9401")
        self.assertEqual(f["security_gate"], "accepted")
        self.assertEqual(f["security_rank"], "primary")
        text = (self.repo / "tickets/inbox/T-9401.md").read_text()
        self.assertIn("security-accept", text)
        self.assertIn("chain is real", text)

    def test_ho_reject_drops(self) -> None:
        self._put("T-9402")
        self.assertEqual(self._run(ticket.cmd_security_reject, self._rej("T-9402")), 0)
        f = self._fields("T-9402", "done")
        self.assertEqual(f["substate"], "dropped")
        self.assertIn("no foothold", f["dropped_reason"])

    def test_non_ho_refused_including_filer(self) -> None:
        self._put("T-9403", requester="artisan")
        for actor in ("artisan", "mason", "specialist", "operator"):
            with self.subTest(actor=actor), patch.dict(os.environ, {"PANTHEON_ACTOR": actor}):
                for fn, args in ((ticket.cmd_security_accept, self._acc("T-9403")),
                                 (ticket.cmd_security_reject, self._rej("T-9403"))):
                    with self.assertRaises(ticket.TicketRefusal) as cm:
                        self._run(fn, args)
                    self.assertIn("head engineering orchestrator", str(cm.exception))
        self.assertEqual(self._fields("T-9403")["security_gate"], "pending")

    def test_accept_requires_pending_and_valid_rank_and_note(self) -> None:
        self._put("T-9404", gate="accepted")
        with self.assertRaises(ticket.TicketRefusal):
            self._run(ticket.cmd_security_accept, self._acc("T-9404"))
        self._put("T-9405")
        with self.assertRaises(ticket.TicketRefusal):
            self._run(ticket.cmd_security_accept, self._acc("T-9405", rank="tertiary"))
        with self.assertRaises(ticket.TicketRefusal):
            self._run(ticket.cmd_security_accept, self._acc("T-9405", note="  "))
        self._put("T-9406", gate=None)
        with self.assertRaises(ticket.TicketRefusal):
            self._run(ticket.cmd_security_reject, self._rej("T-9406"))

    def test_cli_wiring_and_help(self) -> None:
        parser = ticket._build_parser()
        ns = parser.parse_args(["security-accept", "T-1", "--rank", "secondary", "--note", "n"])
        self.assertIs(ns.func, ticket.cmd_security_accept)
        ns = parser.parse_args(["security-reject", "T-1", "--note", "n"])
        self.assertIs(ns.func, ticket.cmd_security_reject)
        with self.assertRaises(SystemExit):
            with patch("sys.stderr"):
                parser.parse_args(["security-accept", "T-1", "--rank", "x", "--note", "n"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            parser.parse_args(["file", "--help"])
        for flag in ("--threat-chain", "--path-justification", "--not-security"):
            self.assertIn(flag, out.getvalue())


class ChainParseTest(unittest.TestCase):
    def test_order_independent(self) -> None:
        got = sg.parse_chain("path=a reason; with semicolon;impact=integrity;foothold=supply-chain")
        self.assertEqual(got, ("supply-chain", "integrity", "a reason; with semicolon"))

    def test_semicolon_inside_path_kept(self) -> None:
        got = sg.parse_chain("foothold=supply-chain;impact=integrity;path=step one; step two")
        self.assertEqual(got[2], "step one; step two")

    def test_duplicate_key_refused(self) -> None:
        with self.assertRaises(sg.GateRefusal):
            sg.parse_chain("foothold=supply-chain;foothold=supply-chain;impact=integrity;path=x")

    def test_refusal_text_is_trimmed(self) -> None:
        try:
            sg.evaluate(title="a bypass", ask=None, labels=[])
        except sg.GateRefusal as exc:
            self.assertLessEqual(len(str(exc)), 460)
            self.assertIn("--not-security", str(exc))
        else:
            self.fail("expected refusal")


def _automated_ns(argv: list[str]) -> argparse.Namespace:
    ns = ticket._build_parser().parse_args(argv[argv.index("file"):])
    ns.no_push = True
    return ns


class AutomatedFilerTest(_Base):
    """The reviewer's r1 blocker: real filers' argv must land pending, not rc 3."""

    def _drive(self, argv: list[str]) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(ticket.cmd_file(_automated_ns(argv)), 0)
        return re.search(r"T-\d+", buf.getvalue()).group(0)

    def _assert_unclassified(self, tid: str, filer: str) -> None:
        f = self._fields(tid)
        self.assertEqual(f["security_gate"], "pending")
        self.assertEqual(str(f["threat_unclassified"]).lower(), "true")
        self.assertEqual(f["security_filer"], filer)
        self.assertNotIn("threat_foothold", f)

    def test_gate_level_automated_unclassified(self) -> None:
        out = sg.evaluate(title="x credential y", ask=None, labels=[], automated_filer="watch_runner")
        self.assertEqual(out["security_gate"], "pending")
        self.assertEqual(out["security_filer"], "watch_runner")
        self.assertEqual(out["threat_unclassified"], "true")

    def test_interactive_still_refused(self) -> None:
        with self.assertRaises(ticket.TicketRefusal):
            ticket.cmd_file(_file_args(title="a bypass"))

    def test_automated_with_chain_is_classified(self) -> None:
        tid = self._file_ok(labels="security", threat_chain=CHAIN, automated_filer="watch_runner")
        f = self._fields(tid)
        self.assertEqual(f["threat_foothold"], "untrusted-content")
        self.assertNotIn("threat_unclassified", f)

    def test_not_in_scope_unaffected(self) -> None:
        tid = self._file_ok(automated_filer="watch_runner")
        self.assertNotIn("security_gate", self._fields(tid))

    def test_watch_runner_followup(self) -> None:
        from tools import watch_runner
        captured: list[list[str]] = []

        def fake(argv, what, env, **kw):
            captured.append(argv)
            return ("filed", None, "")

        finding = MagicMock()
        finding.slug = "mcp-supply-chain"
        with patch.object(watch_runner, "_invoke_ticket_file", side_effect=fake):
            watch_runner.file_followup("Audit credential handling in plugin X",
                                       "look at the credential store", finding, parent_id=None)
        self._assert_unclassified(self._drive(captured[0]), "watch_runner")

    def test_s3star_finding(self) -> None:
        from tools import s3star_audit_runner as s3
        captured: list[list[str]] = []
        finding = MagicMock()
        finding.finding_id = "F-1"
        finding.title = "sudo path reads credential file"
        finding.severity = "high"
        finding.owner = "x"
        with patch.object(s3, "_invoke_ticket_file",
                          side_effect=lambda argv, *a, **k: captured.append(argv) or ("filed", None, "")), \
                patch.object(s3, "authorship_flag", return_value=False), \
                patch.object(s3, "build_finding_ticket_bodies", return_value=("ask", "ctx")), \
                patch.object(s3, "resolve_owner", return_value="x"), \
                patch.object(s3, "owner_target", return_value="role:engineering_orchestrator"):
            s3.file_finding(finding, "slot", __import__("datetime").date(2026, 10, 27))
        self._assert_unclassified(self._drive(captured[0]), "s3star_audit_runner")

    def test_knowledge_sweep_dispatch(self) -> None:
        from tools import knowledge_sweep as ks
        captured: list[list[str]] = []
        diff = {"added": ["a"], "changed": [], "removed": []}
        with patch.object(ks, "_check_day_index_drift"), \
                patch.object(ks, "compute_corpus_fingerprint", return_value={}), \
                patch.object(ks, "load_state", return_value={}), \
                patch.object(ks, "diff_fingerprint", return_value=diff), \
                patch.object(ks, "pending_dispatch_in_inbox", return_value=None), \
                patch.object(ks, "_append_runner_log"), \
                patch.object(ks, "_build_ask", return_value="triage the injection notes"), \
                patch.object(ks, "_invoke_ticket_file",
                             side_effect=lambda argv, *a, **k: captured.append(argv) or ("filed", "T-1", "")):
            ks.file_sweep_ticket(__import__("datetime").date(2026, 10, 27), force=True)
        self._assert_unclassified(self._drive(captured[0]), "knowledge_sweep")

    def test_every_filer_module_passes_automated_filer(self) -> None:
        root = Path(__file__).resolve().parent
        offenders = []
        for p in sorted(root.glob("*.py")):
            if p.name.startswith("test_") or p.name in ("ticket.py", "_security_gate.py", "ticket_purpose.py"):  # purpose lint only parses argv
                continue
            src = p.read_text(encoding="utf-8")
            if '"--requester"' in src and '"--automated-filer"' not in src:
                offenders.append(p.name)
        self.assertEqual(offenders, [])


class AcceptUnclassifiedTest(_Base):
    def _unclassified(self, tid: str) -> None:
        text = _pending_text(tid).replace(
            "security_gate: pending",
            "security_gate: pending\nthreat_unclassified: true\nsecurity_filer: watch_runner")
        (self.repo / "tickets/inbox" / f"{tid}.md").write_text(text, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", f"add {tid}")

    def _acc(self, tid: str, **kw) -> argparse.Namespace:
        d = dict(ticket=tid, rank="primary", note="reviewed", no_push=True,
                 threat_chain=None, path_justification=None, not_security=None)
        d.update(kw)
        return argparse.Namespace(**d)

    def test_accept_needs_chain_on_unclassified(self) -> None:
        self._unclassified("T-9501")
        with self.assertRaises(ticket.TicketRefusal) as cm:
            self._run(ticket.cmd_security_accept, self._acc("T-9501"))
        self.assertIn("--threat-chain", str(cm.exception))
        self.assertEqual(self._fields("T-9501")["security_gate"], "pending")

    def test_accept_with_chain_classifies(self) -> None:
        self._unclassified("T-9502")
        self._run(ticket.cmd_security_accept, self._acc("T-9502", threat_chain=CHAIN))
        f = self._fields("T-9502")
        self.assertEqual(f["security_gate"], "accepted")
        self.assertEqual(f["threat_foothold"], "untrusted-content")
        self.assertNotIn("threat_unclassified", f)

    def test_accept_chain_validates_dash_cell(self) -> None:
        self._unclassified("T-9503")
        chain = "foothold=guest-voice;impact=money-credentials;path=p"
        with self.assertRaises(ticket.TicketRefusal) as cm:
            self._run(ticket.cmd_security_accept, self._acc("T-9503", threat_chain=chain))
        self.assertIn("--path-justification", str(cm.exception))
        with self.assertRaises(ticket.TicketRefusal):
            self._run(ticket.cmd_security_accept,
                      self._acc("T-9503", threat_chain=CHAIN.replace("untrusted-content", "nope")))

    def test_classified_pending_refuses_chain_flag(self) -> None:
        self._put("T-9504")
        with self.assertRaises(ticket.TicketRefusal):
            self._run(ticket.cmd_security_accept, self._acc("T-9504", threat_chain=CHAIN))

    def test_not_security_releases(self) -> None:
        self._unclassified("T-9505")
        self._run(ticket.cmd_security_accept,
                  self._acc("T-9505", rank=None, not_security="watch report keyword false positive"))
        f = self._fields("T-9505")
        self.assertNotIn("security_gate", f)
        self.assertNotIn("threat_unclassified", f)
        self.assertIn("false positive", f["not_security_reason"])
        self.assertIn("not-security", (self.repo / "tickets/inbox/T-9505.md").read_text())

    def test_not_security_is_ho_only_and_exclusive(self) -> None:
        self._unclassified("T-9506")
        with patch.dict(os.environ, {"PANTHEON_ACTOR": "artisan"}):
            with self.assertRaises(ticket.TicketRefusal):
                self._run(ticket.cmd_security_accept,
                          self._acc("T-9506", rank=None, not_security="x"))
        with self.assertRaises(ticket.TicketRefusal):  # both --rank and --not-security
            self._run(ticket.cmd_security_accept, self._acc("T-9506", not_security="x"))
        with self.assertRaises(ticket.TicketRefusal):  # neither
            self._run(ticket.cmd_security_accept, self._acc("T-9506", rank=None))
        self.assertEqual(self._fields("T-9506")["security_gate"], "pending")

    def test_label_add_on_active_ticket_refused(self) -> None:
        p = self.repo / "tickets/active/T-9507.md"
        p.write_text(_pending_text("T-9507", gate=None, state="active")
                     .replace("labels: [security]", "labels: [test]"), encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "add")
        args = argparse.Namespace(ticket="T-9507", labels=["security"], no_push=True,
                                  threat_chain=CHAIN, path_justification=None, not_security=None)
        with self.assertRaises(ticket.TicketRefusal) as cm:
            self._run(ticket.cmd_label_add, args)
        self.assertIn("unclaim", str(cm.exception))


class ActivityMonitorAwkTest(_Base):
    """Runs the REAL awk pipeline from _scan_actionable_tickets over a tmp tree."""

    def test_pending_not_counted(self) -> None:
        from tools import activity_monitor as am
        self._put("T-9601")
        self._put("T-9602", gate="accepted")
        self._put("T-9603", gate=None)
        with patch.object(am, "_sudo", return_value=[]):
            n = am._scan_actionable_tickets(
                self.repo, frozenset({"role:engineering_orchestrator"}), "claude")
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
