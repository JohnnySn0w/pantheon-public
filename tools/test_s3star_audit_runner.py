"""Unit tests for tools/s3star_audit_runner.py.

Run from repo root:
    python3 -m pytest tools/test_s3star_audit_runner.py -q

No live Codex calls, no real git push — subprocess and git-log calls are
mocked. One integration-style test (test_compose_audit_brief_against_real_proposal)
runs section-extraction against the real, checked-in proposal.md to catch
drift between the runner's expectations and the source document.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import s3star_audit_runner as runner


# ---------------------------------------------------------------------------
# T-1599 runtime-snapshot fixtures — output shapes captured from a REAL run of
# each command on the live host (2026-07-07), trimmed to representative lines.
# Per D11's test-fixture corollary: mock payloads must mirror real command
# output shapes, never be hand-invented.
# ---------------------------------------------------------------------------

_SAMPLE_TIMERS_STDOUT = (
    "NEXT                          LEFT LAST                              PASSED UNIT                                  ACTIVATES\n"
    "Tue 2026-07-07 01:30:00 UTC    27s Tue 2026-07-07 01:25:00 UTC 4min 32s ago pantheon-ho-resumer.timer             pantheon-ho-resumer.service\n"
    "Tue 2026-07-07 08:30:00 UTC     7h -                                      - pantheon-s3star-audit.timer           pantheon-s3star-audit.service\n"
    "\n10 timers listed.\n"
)
_SAMPLE_UNITS_STDOUT = (
    "  UNIT                                                                           LOAD   ACTIVE SUB       DESCRIPTION\n"
    "  sys-devices-pci0000:00-0000:00:03.0-virtio1-net-eth0.device                    loaded active plugged   Virtio network device\n"
)
_SAMPLE_PS_STDOUT = (
    "USER         PID    PPID STAT COMMAND         COMMAND\n"
    "root           1       0 Ss   systemd         /sbin/init\n"
    "root           2       0 S    kthreadd        [kthreadd]\n"
)
_SAMPLE_SS_STDOUT = (
    "Netid State  Recv-Q Send-Q               Local Address:Port  Peer Address:Port Process\n"
    "tcp   LISTEN 0      512                      127.0.0.1:8443       0.0.0.0:*\n"
)
_SAMPLE_LS_LD_STDOUT = (
    "drwx--x--- 7 the-ancestor veneration 4096 Jun  9 05:17 /home/the-ancestor/mailbox\n"
    "drwxrws--- 8 the-ancestor veneration 4096 Jun  9 20:24 /home/the-ancestor/mailbox/codex\n"
    "drwxrws--- 8 the-ancestor veneration 4096 Jul  2 18:20 /home/the-ancestor/mailbox/orchestrator\n"
    "drwxrws--- 8 the-ancestor veneration 4096 Jun 10 00:23 /home/the-ancestor/mailbox/specialist\n"
    "drwxrws--- 8 the-ancestor veneration 4096 Jun 30 23:07 /home/the-ancestor/mailbox/recovery\n"
    "drwxrws--- 8 the-ancestor veneration 4096 Jun 13 19:31 /home/the-ancestor/mailbox/the-ancestor\n"
)
_SAMPLE_LS_LDN_STDOUT = (
    "drwx--x--- 7 1004 1006 4096 Jun  9 05:17 /home/the-ancestor/mailbox\n"
    "drwxrws--- 8 1004 1006 4096 Jun  9 20:24 /home/the-ancestor/mailbox/codex\n"
    "drwxrws--- 8 1004 1006 4096 Jul  2 18:20 /home/the-ancestor/mailbox/orchestrator\n"
    "drwxrws--- 8 1004 1006 4096 Jun 10 00:23 /home/the-ancestor/mailbox/specialist\n"
    "drwxrws--- 8 1004 1006 4096 Jun 30 23:07 /home/the-ancestor/mailbox/recovery\n"
    "drwxrws--- 8 1004 1006 4096 Jun 13 19:31 /home/the-ancestor/mailbox/the-ancestor\n"
)
_SAMPLE_GETENT_STDOUT = "veneration:x:1006:claude,claude-research,claude-fallback,the-ancestor,codex\n"

# The real T-1596 finding's own captured failure shape for the user bus.
_SAMPLE_BUS_UNREACHABLE_STDERR = "Failed to connect to bus: No data available\n"


def _fake_run_all_ok(argv, **kwargs):
    """Fake `subprocess.run` stand-in returning canned real-shaped stdout for
    every command `_snapshot_commands()` issues, keyed on the command name."""
    joined = " ".join(argv)
    if argv[:2] == ["systemctl", "--user"] and "list-timers" in argv:
        return MagicMock(returncode=0, stdout=_SAMPLE_TIMERS_STDOUT, stderr="")
    if argv[:2] == ["systemctl", "--user"] and "list-units" in argv:
        return MagicMock(returncode=0, stdout=_SAMPLE_UNITS_STDOUT, stderr="")
    if argv[0] == "ps":
        return MagicMock(returncode=0, stdout=_SAMPLE_PS_STDOUT, stderr="")
    if argv[0] == "ss":
        return MagicMock(returncode=0, stdout=_SAMPLE_SS_STDOUT, stderr="")
    if argv[:2] == ["ls", "-ld"]:
        return MagicMock(returncode=0, stdout=_SAMPLE_LS_LD_STDOUT, stderr="")
    if argv[:2] == ["ls", "-ldn"]:
        return MagicMock(returncode=0, stdout=_SAMPLE_LS_LDN_STDOUT, stderr="")
    if argv[:2] == ["getent", "group"]:
        return MagicMock(returncode=0, stdout=_SAMPLE_GETENT_STDOUT, stderr="")
    raise AssertionError(f"unexpected snapshot command: {joined!r}")


_FIXTURE_PROPOSAL = """# Proposal — fixture

## Independence contract

The audit role should be structurally separated from implementation:

- Read-only by default.
- Does not close its own findings.

## Audit authority

Codex may:

- Read canonical policy.

## Audit program

### 1. Recurring security-assurance audit

Suggested cadence: weekly narrow, monthly integrated.

Coverage:

- Secret and credential reachability.

### 2. Policy-versus-reality sampling

Select a small random sample each cycle.

### 3. Change-triggered review

Not cadence-based.

## Finding format

Every finding should contain:

- **ID and severity:** Critical / High / Medium / Low.
- **Owner:** responsible domain/agent.

## Severity calibration

- **Critical:** current path to secret compromise.
"""


def _finding_block(
    finding_id="F-1",
    severity="Medium",
    owner="recovery",
    evidence="See `tools/foo.py` for details.",
    reproduction="Run `tools/foo.py --check`.",
    title="Sample finding",
) -> str:
    return f"""## Finding 1: {title}
- **ID:** {finding_id}
- **Severity:** {severity}
- **Control objective:** Secrets are not world-readable.
- **Observed condition:** A file is world-readable.
- **Evidence:** {evidence}
- **Impact:** Limited — read access only.
- **Reproduction:** {reproduction}
- **Owner:** {owner}
- **Recommended outcome:** Tighten permissions.
- **Re-test criteria:** File mode is 0600.
- **Justification:** P1, D3.
"""


def _findings_file_text(*blocks: str, slot="weekly-narrow", when="2026-07-06") -> str:
    body = "\n".join(blocks)
    return f"""---
slot: {slot}
date: {when}
auditor: codex
---

{body}
"""


class SectionExtractionTests(unittest.TestCase):
    def test_extract_top_level_section(self):
        section = runner._extract_markdown_section(_FIXTURE_PROPOSAL, "Independence contract")
        self.assertIn("Does not close its own findings", section)
        self.assertNotIn("Audit authority", section)

    def test_extract_nested_section(self):
        program = runner._extract_markdown_section(_FIXTURE_PROPOSAL, "Audit program")
        narrow = runner._extract_markdown_section(program, "1. Recurring security-assurance audit", level="###")
        self.assertIn("Secret and credential reachability", narrow)
        self.assertNotIn("Policy-versus-reality sampling", narrow)

    def test_missing_section_raises(self):
        with self.assertRaises(ValueError):
            runner._extract_markdown_section(_FIXTURE_PROPOSAL, "Does Not Exist")


class ComposeAuditBriefTests(unittest.TestCase):
    def test_deterministic_and_contains_required_sections(self):
        slot = runner.get_slot("weekly-narrow")
        today = date(2026, 7, 6)
        path = Path("/tmp/s3star-test/findings.md")
        b1 = runner.compose_audit_brief(slot, today, path, proposal_text=_FIXTURE_PROPOSAL)
        b2 = runner.compose_audit_brief(slot, today, path, proposal_text=_FIXTURE_PROPOSAL)
        self.assertEqual(b1, b2)
        self.assertIn("Does not close its own findings", b1)
        self.assertIn("READ-ONLY audit", b1)
        self.assertIn("Owner:", b1)
        self.assertIn(str(path), b1)
        self.assertIn("WEEKLY NARROW", b1)

    def test_monthly_slot_gets_integrated_scoping_text(self):
        slot = runner.get_slot("monthly-integrated")
        today = date(2026, 7, 6)
        brief = runner.compose_audit_brief(
            slot, today, Path("/tmp/x.md"), proposal_text=_FIXTURE_PROPOSAL
        )
        self.assertIn("MONTHLY INTEGRATED", brief)

    def test_unknown_slot_raises(self):
        with self.assertRaises(ValueError):
            runner.get_slot("nonexistent-slot")

    def test_compose_audit_brief_against_real_proposal(self):
        # Integration check: the runner's section anchors must still exist in
        # the real, checked-in proposal.md (catches drift between the tool
        # and the adopted-as-written source document).
        slot = runner.get_slot("monthly-integrated")
        brief = runner.compose_audit_brief(slot, date(2026, 7, 6), Path("/tmp/x.md"))
        self.assertIn("Does not close its own findings", brief)
        self.assertIn("Severity calibration", brief)


class ComposeAuditBriefSnapshotSectionTests(unittest.TestCase):
    """T-1599: compose_audit_brief's new optional snapshot_path kwarg."""

    def test_without_snapshot_path_section_is_omitted(self):
        slot = runner.get_slot("weekly-narrow")
        brief = runner.compose_audit_brief(
            slot, date(2026, 7, 6), Path("/tmp/x.md"), proposal_text=_FIXTURE_PROPOSAL
        )
        self.assertNotIn("Runtime evidence snapshot", brief)
        self.assertNotIn("runner-narrated", brief)

    def test_with_snapshot_path_references_path_and_caveat(self):
        slot = runner.get_slot("weekly-narrow")
        snapshot_path = Path("/tmp/s3star/2026-07-06/weekly-narrow-runtime-snapshot.md")
        brief = runner.compose_audit_brief(
            slot, date(2026, 7, 6), Path("/tmp/x.md"),
            proposal_text=_FIXTURE_PROPOSAL, snapshot_path=snapshot_path,
        )
        self.assertIn(str(snapshot_path), brief)
        self.assertIn("runner-narrated", brief)
        self.assertIn("P4", brief)

    def test_snapshot_section_placed_between_independence_and_authority(self):
        slot = runner.get_slot("weekly-narrow")
        snapshot_path = Path("/tmp/x/runtime-snapshot.md")
        brief = runner.compose_audit_brief(
            slot, date(2026, 7, 6), Path("/tmp/x.md"),
            proposal_text=_FIXTURE_PROPOSAL, snapshot_path=snapshot_path,
        )
        independence_idx = brief.index("Does not close its own findings")
        snapshot_idx = brief.index(str(snapshot_path))
        authority_idx = brief.index("## Audit authority")
        self.assertTrue(independence_idx < snapshot_idx < authority_idx)

    def test_deterministic_with_snapshot_path(self):
        slot = runner.get_slot("weekly-narrow")
        snapshot_path = Path("/tmp/x/runtime-snapshot.md")
        b1 = runner.compose_audit_brief(
            slot, date(2026, 7, 6), Path("/tmp/x.md"),
            proposal_text=_FIXTURE_PROPOSAL, snapshot_path=snapshot_path,
        )
        b2 = runner.compose_audit_brief(
            slot, date(2026, 7, 6), Path("/tmp/x.md"),
            proposal_text=_FIXTURE_PROPOSAL, snapshot_path=snapshot_path,
        )
        self.assertEqual(b1, b2)


class ComposeAuditBriefIsolationProfileSectionTests(unittest.TestCase):
    """T-1598: compose_audit_brief always carries the s3star-audit isolation
    profile note (unconditional -- unlike the optional snapshot section)."""

    def test_section_present_and_names_workspace_root(self):
        slot = runner.get_slot("weekly-narrow")
        brief = runner.compose_audit_brief(
            slot, date(2026, 7, 6), Path("/tmp/x.md"), proposal_text=_FIXTURE_PROPOSAL
        )
        self.assertIn("Session isolation profile", brief)
        self.assertIn(str(runner.AUDIT_WORKSPACE_ROOT), brief)

    def test_section_present_even_without_snapshot_path(self):
        slot = runner.get_slot("weekly-narrow")
        brief = runner.compose_audit_brief(
            slot, date(2026, 7, 6), Path("/tmp/x.md"), proposal_text=_FIXTURE_PROPOSAL
        )
        self.assertIn("Session isolation profile", brief)

    def test_section_names_no_mailbox_and_no_network(self):
        slot = runner.get_slot("weekly-narrow")
        brief = runner.compose_audit_brief(
            slot, date(2026, 7, 6), Path("/tmp/x.md"), proposal_text=_FIXTURE_PROPOSAL
        )
        self.assertIn("no mailbox", brief.lower())
        self.assertIn("network", brief.lower())

    def test_section_includes_t1593_retest_probes(self):
        slot = runner.get_slot("weekly-narrow")
        brief = runner.compose_audit_brief(
            slot, date(2026, 7, 6), Path("/tmp/x.md"), proposal_text=_FIXTURE_PROPOSAL
        )
        self.assertIn("test -w /home/codex/pantheon-config", brief)
        self.assertIn("test -w /home/codex/codex-config", brief)
        self.assertIn("test -w /home/the-ancestor/mailbox/orchestrator", brief)

    def test_section_instructs_findings_written_inside_workspace(self):
        slot = runner.get_slot("weekly-narrow")
        brief = runner.compose_audit_brief(
            slot, date(2026, 7, 6), Path("/tmp/x.md"), proposal_text=_FIXTURE_PROPOSAL
        )
        self.assertIn("INSIDE the workspace", brief)


class DueComputationTests(unittest.TestCase):
    def test_never_fired_is_due(self):
        self.assertTrue(runner.is_due("weekly", None, date(2026, 7, 6)))

    def test_weekly_same_iso_week_not_due(self):
        # 2026-07-06 is a Monday; 2026-07-05 (Sunday) is the prior ISO week.
        self.assertFalse(runner.is_due("weekly", date(2026, 7, 6), date(2026, 7, 6)))

    def test_weekly_next_iso_week_due(self):
        self.assertTrue(runner.is_due("weekly", date(2026, 7, 6), date(2026, 7, 13)))

    def test_monthly_same_month_not_due(self):
        self.assertFalse(runner.is_due("monthly", date(2026, 7, 1), date(2026, 7, 30)))

    def test_monthly_next_month_due(self):
        self.assertTrue(runner.is_due("monthly", date(2026, 7, 1), date(2026, 8, 1)))


class StateTests(unittest.TestCase):
    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            with patch.object(runner, "STATE_FILE", state_file), \
                 patch.object(runner, "STATE_DIR", Path(tmp)):
                state = runner.load_state()
                self.assertEqual(state, {"schema_version": 1, "slots": {}})
                runner.update_state(state, "weekly-narrow", date(2026, 7, 6))
                runner.save_state(state)
                reloaded = runner.load_state()
                self.assertEqual(
                    reloaded["slots"]["weekly-narrow"]["last_fire"], "2026-07-06"
                )
                self.assertEqual(
                    runner.last_fire_date(reloaded, "weekly-narrow"), date(2026, 7, 6)
                )


class EmitBriefSafeModeTests(unittest.TestCase):
    def test_emit_brief_no_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            with patch.object(runner, "STATE_FILE", state_file), \
                 patch.object(runner, "STATE_DIR", Path(tmp)), \
                 patch("sys.stdout"):
                rc = runner.main(["--emit-brief", "--slot", "weekly-narrow"])
                self.assertEqual(rc, 0)
                # No side effects: nothing was written to STATE_FILE.
                self.assertFalse(state_file.exists())

    def test_emit_brief_prints_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            with patch.object(runner, "STATE_FILE", state_file), \
                 patch.object(runner, "STATE_DIR", Path(tmp)):
                import io
                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    rc = runner.main(["--emit-brief", "--slot", "monthly-integrated", "--force"])
                self.assertEqual(rc, 0)
                records = json.loads(buf.getvalue())
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["slot"], "monthly-integrated")
                self.assertIn("brief", records[0])

    def test_emit_brief_includes_snapshot_path_preview_key(self):
        # Parity with findings_path: --emit-brief previews where the runtime
        # snapshot WOULD land, without actually capturing it (T-1599).
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            with patch.object(runner, "STATE_FILE", state_file), \
                 patch.object(runner, "STATE_DIR", Path(tmp)):
                import io
                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    rc = runner.main(["--emit-brief", "--slot", "weekly-narrow", "--force"])
                self.assertEqual(rc, 0)
                records = json.loads(buf.getvalue())
                self.assertIn("snapshot_path", records[0])
                self.assertIn("runtime-snapshot", records[0]["snapshot_path"])
                self.assertIn(str(records[0]["snapshot_path"]), records[0]["brief"])

    def test_emit_brief_findings_and_snapshot_paths_under_workspace(self):
        # T-1598: both artifact paths must be computed under the shared
        # codex-writable/claude-readable workspace, not STATE_DIR.
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            with patch.object(runner, "STATE_FILE", state_file), \
                 patch.object(runner, "STATE_DIR", Path(tmp)):
                import io
                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    rc = runner.main(["--emit-brief", "--slot", "weekly-narrow", "--force"])
                self.assertEqual(rc, 0)
                records = json.loads(buf.getvalue())
                self.assertTrue(
                    records[0]["findings_path"].startswith(str(runner.AUDIT_WORKSPACE_ROOT))
                )
                self.assertTrue(
                    records[0]["snapshot_path"].startswith(str(runner.AUDIT_WORKSPACE_ROOT))
                )

    def test_emit_brief_never_captures_runtime_snapshot(self):
        # Safe-mode contract pin: --emit-brief must have ZERO capture side
        # effects (no subprocess calls, no snapshot file written).
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            with patch.object(runner, "STATE_FILE", state_file), \
                 patch.object(runner, "STATE_DIR", Path(tmp)), \
                 patch.object(runner, "capture_runtime_snapshot") as mock_capture, \
                 patch("sys.stdout"):
                rc = runner.main(["--emit-brief", "--slot", "weekly-narrow", "--force"])
                self.assertEqual(rc, 0)
                mock_capture.assert_not_called()


class DispatchTicketTests(unittest.TestCase):
    def test_file_dispatch_ticket_argv_shape(self):
        slot = runner.get_slot("weekly-narrow")
        today = date(2026, 7, 6)
        findings_path = Path("/tmp/s3star/2026-07-06/weekly-narrow-findings.md")
        with patch.object(runner, "_invoke_ticket_file") as mock_invoke, \
             patch.object(runner, "capture_runtime_snapshot") as mock_capture:
            mock_invoke.return_value = ("filed", "T-9999", "retries=0")
            outcome, tid, detail = runner.file_dispatch_ticket(slot, today, findings_path)
        self.assertEqual(outcome, "filed")
        self.assertEqual(tid, "T-9999")
        mock_capture.assert_called_once()
        argv = mock_invoke.call_args[0][0]
        self.assertIn("--target", argv)
        self.assertEqual(argv[argv.index("--target") + 1], "orchestrator")
        self.assertIn("--labels", argv)
        self.assertIn("s3star-audit,audit-dispatch", argv)
        ask = argv[argv.index("--ask") + 1]
        self.assertIn(runner.CODEX_TMUX_SESSION, ask)
        self.assertIn(runner.CODEX_LAUNCH_CMD, ask)
        self.assertIn("--profile s3star-audit", ask)
        context = argv[argv.index("--context") + 1]
        self.assertIn(str(findings_path), context)

    def test_file_dispatch_ticket_captures_and_references_snapshot(self):
        # Integration: file_dispatch_ticket computes a default snapshot path,
        # actually invokes capture, and the embedded brief (in the ticket
        # context) references that exact path (T-1599, path convention
        # updated under T-1598 to the shared AUDIT_WORKSPACE_ROOT).
        slot = runner.get_slot("weekly-narrow")
        today = date(2026, 7, 6)
        findings_path = Path("/tmp/s3star/2026-07-06/weekly-narrow-findings.md")
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp)
            with patch.object(runner, "AUDIT_WORKSPACE_ROOT", workspace_root), \
                 patch.object(runner, "_invoke_ticket_file") as mock_invoke, \
                 patch.object(runner, "capture_runtime_snapshot") as mock_capture:
                mock_invoke.return_value = ("filed", "T-9999", "retries=0")
                runner.file_dispatch_ticket(slot, today, findings_path)
            expected_snapshot = workspace_root / "2026-07-06-weekly-narrow-runtime-snapshot.md"
            mock_capture.assert_called_once_with(expected_snapshot)
            context = mock_invoke.call_args[0][0][
                mock_invoke.call_args[0][0].index("--context") + 1
            ]
            self.assertIn(str(expected_snapshot), context)
            self.assertIn("runner-narrated", context)

    def test_dispatch_idempotency_marker_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            inbox = tickets_root / "inbox"
            inbox.mkdir(parents=True)
            today = date(2026, 7, 6)
            marker = runner._dispatch_marker("weekly-narrow", today)
            (inbox / "T-0001.md").write_text(f"body\n\n{marker}\n", encoding="utf-8")
            self.assertTrue(
                runner.dispatch_ticket_already_filed("weekly-narrow", today, tickets_root=tickets_root)
            )
            self.assertFalse(
                runner.dispatch_ticket_already_filed("monthly-integrated", today, tickets_root=tickets_root)
            )

    def test_auto_dispatch_flag_raises_without_touching_ticket_bin(self):
        with patch.object(runner, "_invoke_ticket_file") as mock_invoke:
            with self.assertRaises(NotImplementedError) as ctx:
                runner.main(["--file-dispatch-ticket", "--slot", "weekly-narrow", "--auto-dispatch"])
            mock_invoke.assert_not_called()
        self.assertIn("not implemented", str(ctx.exception))
        self.assertIn("T-1583", str(ctx.exception))


class FindingsStructuralCheckTests(unittest.TestCase):
    def test_missing_file_not_landed(self):
        self.assertFalse(runner.findings_landed_ok(Path("/nonexistent/path.md")))

    def test_too_small_file_not_landed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.md"
            p.write_text("---\nslot: x\ndate: y\n---\n## Finding 1\n", encoding="utf-8")
            self.assertFalse(runner.findings_landed_ok(p))

    def test_missing_frontmatter_not_landed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.md"
            p.write_text(_finding_block() * 5, encoding="utf-8")
            self.assertFalse(runner.findings_landed_ok(p))

    def test_missing_finding_heading_not_landed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.md"
            p.write_text(
                "---\nslot: weekly-narrow\ndate: 2026-07-06\nauditor: codex\n---\n\n"
                + "no findings this cycle, all clear, nothing to report here at all.\n" * 5,
                encoding="utf-8",
            )
            self.assertFalse(runner.findings_landed_ok(p))

    def test_well_formed_findings_file_is_landed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.md"
            p.write_text(_findings_file_text(_finding_block()), encoding="utf-8")
            self.assertTrue(runner.findings_landed_ok(p))


class ParseFindingsTests(unittest.TestCase):
    def test_parses_all_required_fields(self):
        text = _findings_file_text(_finding_block(finding_id="F-42", severity="High", owner="specialist"))
        findings = runner.parse_findings(text)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.finding_id, "F-42")
        self.assertEqual(f.severity, "High")
        self.assertEqual(f.owner, "specialist")
        self.assertEqual(f.title, "Sample finding")
        self.assertIn("secrets", f.control_objective.lower())

    def test_multiple_findings(self):
        text = _findings_file_text(
            _finding_block(finding_id="F-1", title="First"),
            _finding_block(finding_id="F-2", title="Second"),
        )
        findings = runner.parse_findings(text)
        self.assertEqual([f.finding_id for f in findings], ["F-1", "F-2"])

    def test_malformed_block_skipped_not_crashed(self):
        good = _finding_block(finding_id="F-1")
        bad = "## Finding 2: Broken\n- **ID:** F-2\n- **Severity:** Low\n"  # missing required fields
        text = _findings_file_text(good, bad)
        findings = runner.parse_findings(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].finding_id, "F-1")

    def test_no_findings_section_returns_empty(self):
        text = "---\nslot: x\ndate: y\nauditor: codex\n---\n\nAll clear this cycle.\n"
        self.assertEqual(runner.parse_findings(text), [])


class OwnerResolutionTests(unittest.TestCase):
    def test_known_aliases(self):
        self.assertEqual(runner.resolve_owner("recovery"), "recovery")
        self.assertEqual(runner.resolve_owner("Specialist"), "specialist")
        self.assertEqual(runner.resolve_owner("research"), "specialist")
        self.assertEqual(runner.resolve_owner("security"), "orchestrator")
        self.assertEqual(runner.resolve_owner("operator"), "operator")

    def test_unknown_owner_falls_back_to_orchestrator(self):
        self.assertEqual(runner.resolve_owner("some-unheard-of-domain"), "orchestrator")

    def test_codex_never_resolves_to_codex(self):
        # Structural no-self-closure: whatever case/spelling, "codex" must
        # never become the ticket target.
        for variant in ("codex", "Codex", "CODEX", " codex "):
            self.assertNotEqual(runner.resolve_owner(variant), "codex")
            self.assertEqual(runner.resolve_owner(variant), "orchestrator")


class AuthorshipFlagTests(unittest.TestCase):
    def _finding(self, evidence="See `tools/foo.py`.", reproduction="Run `tools/bar.py`."):
        return runner.Finding(
            finding_id="F-1", title="t", severity="Low", control_objective="c",
            observed_condition="o", evidence=evidence, impact="i",
            reproduction=reproduction, owner="recovery", recommended_outcome="r",
            retest_criteria="rt", justification="j",
        )

    def test_flagged_when_git_log_shows_codex_author(self):
        finding = self._finding()
        with patch.object(runner.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Codex Worker <codex@pantheon>\n")
            self.assertTrue(runner.authorship_flag(finding))

    def test_unflagged_when_no_codex_author(self):
        finding = self._finding()
        with patch.object(runner.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Orchestrator <claude@pantheon>\n")
            self.assertFalse(runner.authorship_flag(finding))

    def test_unflagged_when_no_paths_extracted(self):
        finding = self._finding(evidence="no backticked paths here", reproduction="none either")
        with patch.object(runner.subprocess, "run") as mock_run:
            self.assertFalse(runner.authorship_flag(finding))
            mock_run.assert_not_called()

    def test_extract_paths(self):
        paths = runner._extract_paths("See `tools/foo.py` and `sop/standard/bar.md` for detail.")
        self.assertEqual(paths, ["tools/foo.py", "sop/standard/bar.md"])


class FileFindingTests(unittest.TestCase):
    def test_ticket_body_includes_flagged_note(self):
        finding = runner.Finding(
            finding_id="F-1", title="Title", severity="High", control_objective="c",
            observed_condition="o", evidence="`tools/foo.py`", impact="i",
            reproduction="`tools/foo.py`", owner="recovery", recommended_outcome="r",
            retest_criteria="rt", justification="j",
        )
        with patch.object(runner, "authorship_flag", return_value=True), \
             patch.object(runner, "_invoke_ticket_file") as mock_invoke:
            mock_invoke.return_value = ("filed", "T-1234", "retries=0")
            outcome, tid, _detail = runner.file_finding(finding, "weekly-narrow", date(2026, 7, 6))
        self.assertEqual(outcome, "filed")
        argv = mock_invoke.call_args[0][0]
        self.assertEqual(argv[argv.index("--target") + 1], "recovery")
        self.assertIn("severity-high", argv[argv.index("--labels") + 1])
        self.assertIn("authorship-flagged", argv[argv.index("--labels") + 1])
        context = argv[argv.index("--context") + 1]
        self.assertIn("AUTHORSHIP FLAG", context)

    def test_ticket_body_unflagged_note(self):
        finding = runner.Finding(
            finding_id="F-2", title="Title", severity="Low", control_objective="c",
            observed_condition="o", evidence="no path", impact="i",
            reproduction="no path", owner="specialist", recommended_outcome="r",
            retest_criteria="rt", justification="j",
        )
        with patch.object(runner, "authorship_flag", return_value=False), \
             patch.object(runner, "_invoke_ticket_file") as mock_invoke:
            mock_invoke.return_value = ("filed", "T-1235", "retries=0")
            runner.file_finding(finding, "weekly-narrow", date(2026, 7, 6))
        argv = mock_invoke.call_args[0][0]
        self.assertEqual(argv[argv.index("--target") + 1], "specialist")
        context = argv[argv.index("--context") + 1]
        self.assertIn("no codex-authored", context)
        self.assertNotIn("AUTHORSHIP FLAG", context)


class LandFindingsTests(unittest.TestCase):
    def test_land_findings_rejects_unstructured_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.md"
            p.write_text("not structured", encoding="utf-8")
            rc = runner.land_findings("weekly-narrow", p, date(2026, 7, 6), cwd=Path(tmp))
            self.assertEqual(rc, 1)

    def test_land_findings_files_one_ticket_per_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / "tickets" / "inbox").mkdir(parents=True)
            findings_file = cwd / "findings.md"
            findings_file.write_text(
                _findings_file_text(
                    _finding_block(finding_id="F-1", owner="recovery"),
                    _finding_block(finding_id="F-2", owner="specialist", title="Second"),
                ),
                encoding="utf-8",
            )
            with patch.object(runner, "authorship_flag", return_value=False), \
                 patch.object(runner, "_invoke_ticket_file") as mock_invoke:
                mock_invoke.return_value = ("filed", "T-0001", "retries=0")
                rc = runner.land_findings("weekly-narrow", findings_file, date(2026, 7, 6), cwd=cwd)
            self.assertEqual(rc, 0)
            self.assertEqual(mock_invoke.call_count, 2)

    def test_land_findings_idempotent_per_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            inbox = cwd / "tickets" / "inbox"
            inbox.mkdir(parents=True)
            today = date(2026, 7, 6)
            block = _finding_block(finding_id="F-1", owner="recovery")
            marker = runner._finding_marker("weekly-narrow", today, runner.parse_findings(_findings_file_text(block))[0])
            (inbox / "T-existing.md").write_text(f"body\n\n{marker}\n", encoding="utf-8")
            findings_file = cwd / "findings.md"
            findings_file.write_text(_findings_file_text(block), encoding="utf-8")
            with patch.object(runner, "_invoke_ticket_file") as mock_invoke:
                rc = runner.land_findings("weekly-narrow", findings_file, today, cwd=cwd)
            self.assertEqual(rc, 0)
            mock_invoke.assert_not_called()


class LoggerAttributionRegressionTests(unittest.TestCase):
    """T-1583 REBOUND (reviewer-reproduced): before the fix, every ticket-filing
    failure landed in tools/watch_runner.py's OWN RUNNER_LOG under an
    "[specialist] actuation: ERROR" tag, and nothing landed in this module's
    documented RUNNER_LOG. This exercises the REAL `_invoke_ticket_file`
    kernel (only `subprocess.run` is mocked, not the helper itself) and pins
    that a filing failure now lands in s3star's own log, correctly tagged,
    and does NOT land in watch_runner's log at all.
    """

    def test_dispatch_ticket_failure_logs_to_own_log_not_watch_runners(self):
        slot = runner.get_slot("weekly-narrow")
        today = date(2026, 7, 6)
        findings_path = Path("/tmp/s3star/2026-07-06/weekly-narrow-findings.md")
        with tempfile.TemporaryDirectory() as tmp:
            own_log = Path(tmp) / "s3star.log"
            watch_log = Path(tmp) / "watch_runner.log"
            with patch("tools.watch_runner.subprocess.run") as mock_run, \
                 patch("tools.watch_runner.time.sleep"), \
                 patch.object(runner, "RUNNER_LOG", own_log), \
                 patch.object(runner, "capture_runtime_snapshot"), \
                 patch("tools.watch_runner.RUNNER_LOG", watch_log):
                mock_run.return_value = MagicMock(
                    returncode=2, stdout="", stderr="HERMES guard refused\n"
                )
                outcome, _tid, _detail = runner.file_dispatch_ticket(slot, today, findings_path)

            self.assertEqual(outcome, "errored")
            self.assertTrue(own_log.exists(), "s3star's own RUNNER_LOG must be written on failure")
            logged = own_log.read_text(encoding="utf-8")
            self.assertIn("[orchestrator] s3star: ERROR", logged)
            self.assertIn("dispatch weekly-narrow", logged)
            self.assertFalse(
                watch_log.exists(),
                "watch_runner's RUNNER_LOG must NOT receive s3star's failures (the misattribution bug)",
            )

    def test_finding_ticket_failure_logs_to_own_log_not_watch_runners(self):
        finding = runner.Finding(
            finding_id="F-1", title="Title", severity="High", control_objective="c",
            observed_condition="o", evidence="no path", impact="i",
            reproduction="no path", owner="recovery", recommended_outcome="r",
            retest_criteria="rt", justification="j",
        )
        with tempfile.TemporaryDirectory() as tmp:
            own_log = Path(tmp) / "s3star.log"
            watch_log = Path(tmp) / "watch_runner.log"
            with patch("tools.watch_runner.subprocess.run") as mock_run, \
                 patch("tools.watch_runner.time.sleep"), \
                 patch.object(runner, "RUNNER_LOG", own_log), \
                 patch("tools.watch_runner.RUNNER_LOG", watch_log):
                mock_run.return_value = MagicMock(
                    returncode=2, stdout="", stderr="HERMES guard refused\n"
                )
                outcome, _tid, _detail = runner.file_finding(finding, "weekly-narrow", date(2026, 7, 6))

            self.assertEqual(outcome, "errored")
            self.assertTrue(own_log.exists())
            self.assertIn("[orchestrator] s3star: ERROR", own_log.read_text(encoding="utf-8"))
            self.assertFalse(watch_log.exists())

    def test_main_dispatch_failure_branch_writes_own_log(self):
        # main()'s own error branch (in addition to the log_error callback
        # inside file_dispatch_ticket) must also write a durable log line —
        # stderr alone is not durable (T-1583 REBOUND item 3).
        with tempfile.TemporaryDirectory() as tmp:
            own_log = Path(tmp) / "s3star.log"
            state_file = Path(tmp) / "state.json"
            with patch.object(runner, "RUNNER_LOG", own_log), \
                 patch.object(runner, "STATE_FILE", state_file), \
                 patch.object(runner, "STATE_DIR", Path(tmp)), \
                 patch.object(runner, "file_dispatch_ticket", return_value=("errored", None, "boom")), \
                 patch("sys.stderr"):
                rc = runner.main(["--file-dispatch-ticket", "--slot", "weekly-narrow"])
            self.assertEqual(rc, 1)
            self.assertTrue(own_log.exists())
            self.assertIn("file-dispatch-ticket", own_log.read_text(encoding="utf-8"))


class DefaultSafeGateTests(unittest.TestCase):
    def test_no_mode_refused(self):
        rc = runner.main([])
        self.assertEqual(rc, 64)

    def test_conflicting_modes_refused(self):
        rc = runner.main(["--emit-brief", "--file-dispatch-ticket"])
        self.assertEqual(rc, 64)

    def test_land_findings_requires_slot_and_file(self):
        rc = runner.main(["--land-findings"])
        self.assertEqual(rc, 64)


class RuntimeSnapshotCommandsTests(unittest.TestCase):
    """T-1599: _snapshot_commands() shape + structural no-secrets guard."""

    def test_exactly_seven_commands(self):
        cmds = runner._snapshot_commands()
        self.assertEqual(len(cmds), 7)

    def test_roots_cover_hub_and_five_named_agents(self):
        cmds = runner._snapshot_commands()
        ls_cmd = next(c for c in cmds if c.argv[:2] == ["ls", "-ld"])
        self.assertIn(str(runner._SNAPSHOT_MAILBOX_HUB), ls_cmd.argv)
        for agent in ("codex", "orchestrator", "specialist", "recovery", "the-ancestor"):
            self.assertIn(str(runner._SNAPSHOT_MAILBOX_HUB / agent), ls_cmd.argv)

    def test_no_command_reads_env_or_file_contents(self):
        # Structural sanitization guard: none of the capture commands can read
        # env vars or file contents (metadata/listings only, per T-1599).
        banned = {"env", "printenv", "cat", "less", "more", "xxd", "od"}
        for cmd in runner._snapshot_commands():
            self.assertNotIn(cmd.argv[0], banned)
            self.assertNotIn("env", cmd.argv)


class RuntimeSnapshotCaptureTests(unittest.TestCase):
    """T-1599: capture_runtime_snapshot — happy path + warn-not-fail."""

    def test_happy_path_writes_all_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "2026-07-06" / "weekly-narrow-runtime-snapshot.md"
            result = runner.capture_runtime_snapshot(
                snapshot_path, generated_at="2026-07-06T12:00:00Z", run=_fake_run_all_ok
            )
            self.assertEqual(result, snapshot_path)
            text = snapshot_path.read_text(encoding="utf-8")
        self.assertIn("2026-07-06T12:00:00Z", text)
        self.assertIn("runner-narrated", text)
        self.assertIn("P4", text)
        self.assertIn("pantheon-s3star-audit.timer", text)
        self.assertIn("veneration:x:1006", text)
        self.assertIn("/home/the-ancestor/mailbox/codex", text)
        self.assertIn("1004 1006", text)  # numeric ids from ls -ldn
        self.assertIn("tcp   LISTEN", text)
        self.assertIn("root           1       0 Ss", text)

    def test_missing_binary_degrades_to_warning_others_unaffected(self):
        def fake_run(argv, **kwargs):
            if argv[0] == "getent":
                raise FileNotFoundError("getent not found")
            return _fake_run_all_ok(argv, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "snap.md"
            runner.capture_runtime_snapshot(snapshot_path, run=fake_run)
            text = snapshot_path.read_text(encoding="utf-8")
        self.assertIn("WARNING: command not found", text)
        self.assertNotIn("veneration:x:1006", text)  # getent section degraded, not real output
        self.assertIn("pantheon-s3star-audit.timer", text)  # other sections unaffected

    def test_timeout_degrades_to_warning(self):
        def fake_run(argv, **kwargs):
            if argv[0] == "ss":
                raise subprocess.TimeoutExpired(cmd=argv, timeout=15)
            return _fake_run_all_ok(argv, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "snap.md"
            runner.capture_runtime_snapshot(snapshot_path, run=fake_run)
            text = snapshot_path.read_text(encoding="utf-8")
        self.assertIn("WARNING", text)
        self.assertIn("timed out", text)
        self.assertIn("pantheon-s3star-audit.timer", text)

    def test_nonzero_exit_degrades_to_warning_with_stderr(self):
        # Mirrors the real T-1596 finding's own captured failure shape.
        def fake_run(argv, **kwargs):
            if argv[:2] == ["systemctl", "--user"] and "list-timers" in argv:
                return MagicMock(
                    returncode=1, stdout="", stderr=_SAMPLE_BUS_UNREACHABLE_STDERR
                )
            return _fake_run_all_ok(argv, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "snap.md"
            runner.capture_runtime_snapshot(snapshot_path, run=fake_run)
            text = snapshot_path.read_text(encoding="utf-8")
        self.assertIn("WARNING: exited 1", text)
        self.assertIn("Failed to connect to bus", text)
        self.assertIn("veneration:x:1006", text)  # other sections unaffected

    def test_all_commands_fail_never_raises_still_writes_file(self):
        def fake_run(argv, **kwargs):
            raise OSError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "snap.md"
            runner.capture_runtime_snapshot(snapshot_path, run=fake_run)
            self.assertTrue(snapshot_path.exists())
            text = snapshot_path.read_text(encoding="utf-8")
        self.assertEqual(text.count("WARNING"), 7)

    def test_default_generated_at_is_utc_now(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "snap.md"
            with patch.object(runner, "_utc_iso_now", return_value="2099-01-01T00:00:00Z"):
                runner.capture_runtime_snapshot(snapshot_path, run=_fake_run_all_ok)
            text = snapshot_path.read_text(encoding="utf-8")
        self.assertIn("2099-01-01T00:00:00Z", text)


if __name__ == "__main__":
    unittest.main()
