"""Unit tests for tools/s3star_audit_runner.py.

Run from repo root:
    python3 -m pytest tools/test_s3star_audit_runner.py -q

No live Codex calls, no real git push — subprocess and git-log calls are
mocked. One integration-style test (test_compose_audit_brief_against_real_proposal)
runs section-extraction against the real, checked-in proposal.md to catch
drift between the runner's expectations and the source document.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from stat import S_IMODE
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import s3star_audit_runner as runner
from tools import ticket as ticket_mod
from tools import _git_utils


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
    "USER         PID    PPID STAT                  STARTED COMMAND         COMMAND\n"
    "root           1       0 Ss   Fri Jun 12 18:24:25 2026 systemd         /sbin/init\n"
    "root           2       0 S    Fri Jun 12 18:24:25 2026 kthreadd        [kthreadd]\n"
)
# Codex line matches T-1595's FALSIFIED section's own host-verified `id codex`
# output; the rest are illustrative (not host-verified) but real-shaped.
_SAMPLE_ID_STDOUT = (
    "uid=1000(claude) gid=1000(claude) groups=1000(claude),1006(pantheon-mailbox)\n"
    "uid=1002(claude-research) gid=1002(claude-research) groups=1002(claude-research),1006(pantheon-mailbox)\n"
    "uid=1005(claude-fallback) gid=1005(claude-fallback) groups=1005(claude-fallback),1006(pantheon-mailbox)\n"
    "uid=1003(codex) gid=1004(codex) groups=1004(codex),1006(pantheon-mailbox)\n"
    "uid=1004(the-ancestor) gid=1004(the-ancestor) groups=1004(the-ancestor),1006(pantheon-mailbox)\n"
)
_SAMPLE_SS_STDOUT = (
    "Netid State  Recv-Q Send-Q               Local Address:Port  Peer Address:Port Process\n"
    "tcp   LISTEN 0      512                      127.0.0.1:8443       0.0.0.0:*\n"
)
_SAMPLE_LS_LD_STDOUT = (
    "drwx--x--- 7 the-ancestor pantheon-mailbox 4096 Jun  9 05:17 /home/the-ancestor/mailbox\n"
    "drwxrws--- 8 the-ancestor pantheon-mailbox 4096 Jun  9 20:24 /home/the-ancestor/mailbox/codex\n"
    "drwxrws--- 8 the-ancestor pantheon-mailbox 4096 Jul  2 18:20 /home/the-ancestor/mailbox/orchestrator\n"
    "drwxrws--- 8 the-ancestor pantheon-mailbox 4096 Jun 10 00:23 /home/the-ancestor/mailbox/specialist\n"
    "drwxrws--- 8 the-ancestor pantheon-mailbox 4096 Jun 30 23:07 /home/the-ancestor/mailbox/recovery\n"
    "drwxrws--- 8 the-ancestor pantheon-mailbox 4096 Jun 13 19:31 /home/the-ancestor/mailbox/the-ancestor\n"
)
_SAMPLE_LS_LDN_STDOUT = (
    "drwx--x--- 7 1004 1006 4096 Jun  9 05:17 /home/the-ancestor/mailbox\n"
    "drwxrws--- 8 1004 1006 4096 Jun  9 20:24 /home/the-ancestor/mailbox/codex\n"
    "drwxrws--- 8 1004 1006 4096 Jul  2 18:20 /home/the-ancestor/mailbox/orchestrator\n"
    "drwxrws--- 8 1004 1006 4096 Jun 10 00:23 /home/the-ancestor/mailbox/specialist\n"
    "drwxrws--- 8 1004 1006 4096 Jun 30 23:07 /home/the-ancestor/mailbox/recovery\n"
    "drwxrws--- 8 1004 1006 4096 Jun 13 19:31 /home/the-ancestor/mailbox/the-ancestor\n"
)
_SAMPLE_GETENT_STDOUT = "pantheon-mailbox:x:1006:claude,claude-research,claude-fallback,the-ancestor,codex\n"

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
    if argv[0] == "id":
        return MagicMock(returncode=0, stdout=_SAMPLE_ID_STDOUT, stderr="")
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


def _sentinel_line(n: int) -> str:
    """T-4314: the closing sentinel, written literally (NOT via the runner's
    template) so this file pins the wire format independently of the runner."""
    return f"s3star-audit-end: findings={n}"


def _findings_file_text(
    *blocks: str, slot="weekly-narrow", when="2026-07-06", sealed=True
) -> str:
    """Frontmatter + `blocks`. T-4314: `sealed=True` (the default) appends the
    closing sentinel with N = the number of `## Finding` sections in `blocks`,
    i.e. what a complete, well-behaved auditor run writes. `sealed=False` is
    the pre-T-4314 / truncated-run shape."""
    body = "\n".join(blocks)
    text = f"""---
slot: {slot}
date: {when}
auditor: codex
---

{body}
"""
    if sealed:
        n = len(re.findall(r"^## Finding\b", body, flags=re.MULTILINE))
        text += "\n" + _sentinel_line(n) + "\n"
    return text


def _zero_finding_file_text(
    *, slot="weekly-narrow", when="2026-07-06", frontmatter=True, sealed=True
) -> str:
    """T-4307: a legitimate zero-finding audit file. Shape modeled on the real
    2026-09-28 weekly-narrow output (frontmatter, preamble, verification summary,
    retest verdicts, an explicit no-new-finding line, DECISIONS) -- text is
    original, and deliberately carries NO `## Finding` heading (the brief tells
    the auditor to omit that section when nothing is actionable).

    T-4314: `sealed=True` (default) ends with `s3star-audit-end: findings=0`;
    `sealed=False` is the real pre-sentinel shape -- indistinguishable, by
    structure alone, from a run that crashed after writing its DECISIONS."""
    fm = f"---\nslot: {slot}\ndate: {when}\nauditor: codex\n---\n\n" if frontmatter else ""
    text = fm + (
        "## Audit Preamble\n\n"
        "Coverage selected for this rotation: file permissions and ledger sampling.\n\n"
        "## Verification Summary\n\n"
        "- `permission_boundary_check`: PASS. All three write probes returned status 1.\n"
        "- `manifest_hashes_match`: PASS. Every artifact hash matched the manifest.\n\n"
        "## Retest Verdicts\n\n"
        "- T-0001: pass.\n\n"
        "No new actionable finding is filed this cycle.\n\n"
        "## DECISIONS\n\n"
        "- Recorded verdicts only, no remediation; justified-via P5.\n"
    )
    if sealed:
        text += "\n" + _sentinel_line(0) + "\n"
    return text


# ---------------------------------------------------------------------------
# T-1898 fixtures — a minimal ticket-file shape (frontmatter + H1 + ##
# Context), enough to exercise the marker/extraction regexes under test.
# Not a full tools/ticket.py round-trip; these tests only exercise the
# s3star-side reader.
# ---------------------------------------------------------------------------


def _wrap_ticket_text(ticket_id, title, context_body, *, not_before=None,
                       state="inbox", retest_status=None,
                       labels="[s3star-audit, audit-finding, severity-medium]") -> str:
    fm_lines = [
        "---",
        f"id: {ticket_id}",
        "created: 2026-07-20T23:11:00Z",
        "requester: orchestrator",
        "target: recovery",
        f"state: {state}",
        "parent_ticket: null",
        "depends_on: []",
        f"labels: {labels}",
    ]
    if not_before:
        fm_lines.append(f"not_before: {not_before}")
    if retest_status:
        fm_lines.append(f"retest_status: {retest_status}")
    fm_lines.append("---")
    frontmatter = "\n".join(fm_lines)
    return (
        f"{frontmatter}\n\n"
        f"# {title}\n\n"
        f"## Ask\n\nSome ask text.\n\n"
        f"## Context\n\n{context_body}\n\n"
        f"## Transition log\n\n"
        f"- 2026-07-20T23:11:00Z [orchestrator] filed in inbox\n"
    )


# Trimmed from the REAL on-disk tickets/inbox/T-1733.md (2026-07-13 filing) —
# a pre-T-1898 "legacy" finding ticket, same marker/field shape as current.
_LEGACY_T1733_SHAPED_CONTEXT = (
    "s3star-audit-finding: weekly-narrow:2026-07-13:S3STAR-2026-07-13-002\n\n"
    "Title: S3* audit worker can read Orchestrator mailbox metadata and message files\n"
    "Severity: Medium.\n\n"
    "Control objective: The S3* auditor should not have cross-agent mailbox access "
    "beyond the constrained findings path.\n\n"
    "Observed condition: The sandbox blocks mailbox writes, but the audit worker can "
    "traverse Orchestrator's mailbox.\n\n"
    "Evidence: `test -r /home/the-ancestor/mailbox/orchestrator` exited `0`.\n\n"
    "Impact: A compromised or prompt-injected audit worker could read cross-agent "
    "operational messages.\n\n"
    "Reproduction: From an S3* audit session, run `test -r /home/the-ancestor/mailbox/orchestrator`.\n\n"
    "Recommended outcome: Keep mailbox send/consume writes denied and additionally deny "
    "S3* audit sessions read access to live per-agent mailbox contents.\n\n"
    "Re-test criteria: In a fresh S3* audit session without mailbox-specific authorization, "
    "`test -w /home/the-ancestor/mailbox/orchestrator` and `test -r /home/the-ancestor/mailbox/orchestrator` "
    "both exit non-zero, while the sanctioned findings output path remains writable and "
    "sanitized runtime evidence remains readable.\n\n"
    "Justification: P1 favors enforcing cross-agent boundaries in filesystem permissions.\n\n"
    "Authorship-aware routing (ORG-038 Refinement 2): this finding's evidence/reproduction "
    "paths were checked against git history; no codex-authored commit was found on them.\n"
)

# A finding-marker-bearing ticket whose body was hand-edited and lost its
# structured "Re-test criteria:" field — exercises the tolerant fallback.
_MARKER_NO_CRITERIA_CONTEXT = (
    "s3star-audit-finding: weekly-narrow:2026-07-20:S3STAR-BROKEN-001\n\n"
    "Title: Something odd\n\n"
    "This finding body was hand-edited and lost its structured fields.\n"
)


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


class ComposeAuditBriefSeatEvidenceTests(unittest.TestCase):
    """T-4928: the snapshot section points at Recovery's per-seat bundle, or says it is absent."""

    def _brief(self, latest):
        slot = runner.get_slot("weekly-narrow")
        with patch.object(runner, "SEAT_EVIDENCE_LATEST", latest):
            return runner.compose_audit_brief(
                slot, date(2026, 7, 6), Path("/tmp/x.md"),
                proposal_text=_FIXTURE_PROPOSAL, snapshot_path=Path("/tmp/x/runtime-snapshot.md"))

    def test_present_bundle_is_cited_with_its_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            latest = Path(td)
            (latest / "MANIFEST.md").write_text("---\ngenerated_at: 2026-10-20T08:20:00Z\n---\n")
            brief = self._brief(latest)
        self.assertIn("### Per-seat evidence (Recovery-captured)\n", brief)
        self.assertIn("generated_at 2026-10-20T08:20:00Z", brief)
        self.assertIn("never content", brief)

    def test_absent_bundle_is_stated_not_silent(self):
        brief = self._brief(Path("/nonexistent/seat-evidence/latest"))
        self.assertIn("Per-seat evidence (Recovery-captured): ABSENT this cycle", brief)
        self.assertIn("absence-of-channel", brief)

    def test_no_snapshot_no_seat_section(self):
        slot = runner.get_slot("weekly-narrow")
        brief = runner.compose_audit_brief(slot, date(2026, 7, 6), Path("/tmp/x.md"),
                                           proposal_text=_FIXTURE_PROPOSAL)
        self.assertNotIn("Per-seat evidence", brief)


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
        self.assertIn("test -w /home/post-office/mailbox/orchestrator", brief)

    def test_section_instructs_findings_written_inside_workspace(self):
        slot = runner.get_slot("weekly-narrow")
        brief = runner.compose_audit_brief(
            slot, date(2026, 7, 6), Path("/tmp/x.md"), proposal_text=_FIXTURE_PROPOSAL
        )
        self.assertIn("INSIDE the workspace", brief)


class ComposeAuditBriefMethodologySectionTests(unittest.TestCase):
    """T-1596: methodology guidance + T-1595 worked example, gated on
    snapshot_path same as the runtime-evidence-snapshot section."""

    def test_methodology_present_with_snapshot_path(self):
        slot = runner.get_slot("weekly-narrow")
        snapshot_path = Path("/tmp/s3star/2026-07-14-weekly-narrow/runtime-snapshot.md")
        brief = runner.compose_audit_brief(
            slot, date(2026, 7, 14), Path("/tmp/x.md"),
            proposal_text=_FIXTURE_PROPOSAL, snapshot_path=snapshot_path,
        )
        self.assertIn("## Methodology", brief)
        self.assertIn("T-1595", brief)
        self.assertIn("S3STAR-W1-003", brief)
        self.assertIn("bwrap", brief)
        self.assertIn("MUST NOT be reported as host truth", brief)

    def test_methodology_absent_without_snapshot_path(self):
        slot = runner.get_slot("weekly-narrow")
        brief = runner.compose_audit_brief(
            slot, date(2026, 7, 14), Path("/tmp/x.md"), proposal_text=_FIXTURE_PROPOSAL
        )
        self.assertNotIn("## Methodology", brief)
        self.assertNotIn("S3STAR-W1-003", brief)

    def test_methodology_references_manifest_path(self):
        slot = runner.get_slot("weekly-narrow")
        snapshot_path = Path("/tmp/s3star/2026-07-14-weekly-narrow/runtime-snapshot.md")
        brief = runner.compose_audit_brief(
            slot, date(2026, 7, 14), Path("/tmp/x.md"),
            proposal_text=_FIXTURE_PROPOSAL, snapshot_path=snapshot_path,
        )
        self.assertIn(str(runner.manifest_path_for(snapshot_path)), brief)

    def test_methodology_between_independence_and_authority(self):
        slot = runner.get_slot("weekly-narrow")
        snapshot_path = Path("/tmp/x/runtime-snapshot.md")
        brief = runner.compose_audit_brief(
            slot, date(2026, 7, 14), Path("/tmp/x.md"),
            proposal_text=_FIXTURE_PROPOSAL, snapshot_path=snapshot_path,
        )
        independence_idx = brief.index("Does not close its own findings")
        methodology_idx = brief.index("## Methodology")
        authority_idx = brief.index("## Audit authority")
        self.assertTrue(independence_idx < methodology_idx < authority_idx)


class OpenRetestsExtractionTests(unittest.TestCase):
    """T-1898: find_open_retest_tickets / render_open_retests_section."""

    def test_matches_current_runner_filed_ticket_shape(self):
        finding = runner.Finding(
            finding_id="F-9", title="Sample current finding", severity="Medium",
            control_objective="c", observed_condition="o", evidence="e", impact="i",
            reproduction="r", owner="recovery", recommended_outcome="ro",
            retest_criteria="File mode is 0600.", justification="j",
        )
        _, context = runner.build_finding_ticket_bodies(
            finding, "weekly-narrow", date(2026, 7, 20), flagged=False
        )
        text = _wrap_ticket_text("T-9001", "S3* audit finding F-9: Sample current finding", context)
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            (inbox / "T-9001.md").write_text(text, encoding="utf-8")
            retests = runner.find_open_retest_tickets(Path(tmp))
        self.assertEqual(len(retests), 1)
        self.assertEqual(retests[0].ticket_id, "T-9001")
        self.assertIn("Sample current finding", retests[0].title)
        self.assertEqual(retests[0].criteria, "File mode is 0600.")

    def test_matches_legacy_t1733_shaped_ticket(self):
        text = _wrap_ticket_text(
            "T-1733",
            "S3* audit finding S3STAR-2026-07-13-002: S3* audit worker can read "
            "Orchestrator mailbox metadata and message files",
            _LEGACY_T1733_SHAPED_CONTEXT,
            not_before="2026-07-27T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            (inbox / "T-1733.md").write_text(text, encoding="utf-8")
            retests = runner.find_open_retest_tickets(Path(tmp))
        self.assertEqual(len(retests), 1)
        self.assertEqual(retests[0].ticket_id, "T-1733")
        self.assertIn("mailbox", retests[0].title.lower())
        self.assertIn("test -w /home/the-ancestor/mailbox/orchestrator", retests[0].criteria)

    def test_tolerant_fallback_when_no_retest_criteria_line(self):
        text = _wrap_ticket_text("T-9002", "Broken finding", _MARKER_NO_CRITERIA_CONTEXT)
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            (inbox / "T-9002.md").write_text(text, encoding="utf-8")
            retests = runner.find_open_retest_tickets(Path(tmp))
        self.assertEqual(len(retests), 1)
        self.assertEqual(retests[0].ticket_id, "T-9002")
        self.assertIsNone(retests[0].criteria)
        rendered = runner.render_open_retests_section(retests, Path("/tmp/verdicts.jsonl"))
        self.assertIn("T-9002", rendered)
        self.assertIn("criteria not machine-extractable - read the ticket", rendered)

    def test_future_not_before_ticket_still_included(self):
        text = _wrap_ticket_text(
            "T-9003", "Future-gated finding",
            "s3star-audit-finding: weekly-narrow:2026-07-20:S3STAR-FUTURE-001\n\n"
            "Re-test criteria: Some criteria text.\n",
            not_before="2099-01-01T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            (inbox / "T-9003.md").write_text(text, encoding="utf-8")
            retests = runner.find_open_retest_tickets(Path(tmp))
        self.assertEqual(len(retests), 1)
        self.assertEqual(retests[0].ticket_id, "T-9003")

    def test_dispatch_ticket_marker_not_mistaken_for_finding(self):
        marker = runner._dispatch_marker("weekly-narrow", date(2026, 7, 20))
        text = _wrap_ticket_text(
            "T-9004", "S3* audit dispatch — weekly-narrow 2026-07-20",
            f"{marker}\n\n--- BRIEF (filed-time snapshot) ---\n\nSome brief text.\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            (inbox / "T-9004.md").write_text(text, encoding="utf-8")
            retests = runner.find_open_retest_tickets(Path(tmp))
        self.assertEqual(retests, [])

    def test_empty_inbox_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            self.assertEqual(runner.find_open_retest_tickets(Path(tmp)), [])

    def test_no_inbox_dir_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(runner.find_open_retest_tickets(Path(tmp)), [])


class ComposeAuditBriefOpenRetestsSectionTests(unittest.TestCase):
    """T-1898: compose_audit_brief's new optional tickets_root kwarg."""

    def _inbox_with(self, tmp, filename, text):
        inbox = Path(tmp) / "inbox"
        inbox.mkdir(exist_ok=True)
        (inbox / filename).write_text(text, encoding="utf-8")

    def test_section_renders_with_open_findings(self):
        slot = runner.get_slot("weekly-narrow")
        with tempfile.TemporaryDirectory() as tmp:
            text = _wrap_ticket_text(
                "T-1733",
                "S3* audit finding S3STAR-2026-07-13-002: mailbox finding",
                _LEGACY_T1733_SHAPED_CONTEXT,
                not_before="2026-07-27T00:00:00Z",
            )
            self._inbox_with(tmp, "T-1733.md", text)
            brief = runner.compose_audit_brief(
                slot, date(2026, 7, 20), Path("/tmp/x.md"),
                proposal_text=_FIXTURE_PROPOSAL, tickets_root=Path(tmp),
            )
        self.assertIn("## Open re-tests", brief)
        self.assertIn("T-1733", brief)
        self.assertIn("test -w /home/the-ancestor/mailbox/orchestrator", brief)

    def test_section_omitted_with_empty_inbox(self):
        slot = runner.get_slot("weekly-narrow")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "inbox").mkdir()
            brief = runner.compose_audit_brief(
                slot, date(2026, 7, 20), Path("/tmp/x.md"),
                proposal_text=_FIXTURE_PROPOSAL, tickets_root=Path(tmp),
            )
        self.assertNotIn("## Open re-tests", brief)

    def test_section_omitted_when_tickets_root_not_passed(self):
        slot = runner.get_slot("weekly-narrow")
        brief = runner.compose_audit_brief(
            slot, date(2026, 7, 20), Path("/tmp/x.md"), proposal_text=_FIXTURE_PROPOSAL
        )
        self.assertNotIn("## Open re-tests", brief)

    def test_t1593_probes_present_alongside_open_retests_section(self):
        slot = runner.get_slot("weekly-narrow")
        with tempfile.TemporaryDirectory() as tmp:
            text = _wrap_ticket_text(
                "T-1733",
                "S3* audit finding S3STAR-2026-07-13-002: mailbox finding",
                _LEGACY_T1733_SHAPED_CONTEXT,
                not_before="2026-07-27T00:00:00Z",
            )
            self._inbox_with(tmp, "T-1733.md", text)
            brief = runner.compose_audit_brief(
                slot, date(2026, 7, 20), Path("/tmp/x.md"),
                proposal_text=_FIXTURE_PROPOSAL, tickets_root=Path(tmp),
            )
        self.assertIn("## Open re-tests", brief)
        self.assertIn("test -w /home/codex/pantheon-config", brief)
        self.assertIn("test -w /home/codex/codex-config", brief)


class AuthorshipFlaggedRetestRoutingTests(unittest.TestCase):
    """T-2796: authorship-flagged open re-tests are excluded from the
    Codex-facing brief and route only to the claude-tier verification
    queue (ORG-038 Refinement 2 -- the T-2613 class)."""

    def _pair(self):
        standard = runner.OpenRetest(
            ticket_id="T-8001", title="Standard finding",
            criteria="Standard criteria.", authorship_flagged=False,
        )
        flagged = runner.OpenRetest(
            ticket_id="T-8002", title="Flagged finding",
            criteria="Flagged criteria.", authorship_flagged=True,
        )
        return standard, flagged

    def test_codex_brief_section_excludes_flagged(self):
        standard, flagged = self._pair()
        rendered = runner.render_open_retests_section(
            [standard, flagged], Path("/tmp/verdicts.jsonl")
        )
        self.assertIn("T-8001", rendered)
        self.assertIn("Standard criteria.", rendered)
        self.assertNotIn("T-8002", rendered)
        self.assertNotIn("Flagged criteria.", rendered)

    def test_claude_tier_queue_contains_only_flagged(self):
        standard, flagged = self._pair()
        rendered = runner.render_claude_tier_verification_queue([standard, flagged])
        self.assertIn("T-8002", rendered)
        self.assertIn("Flagged criteria.", rendered)
        self.assertNotIn("T-8001", rendered)
        self.assertNotIn("Standard criteria.", rendered)

    def test_claude_tier_queue_empty_when_nothing_flagged(self):
        standard, _ = self._pair()
        self.assertEqual(runner.render_claude_tier_verification_queue([standard]), "")

    def test_codex_section_omitted_entirely_when_only_flagged_open(self):
        _, flagged = self._pair()
        rendered = runner.render_open_retests_section([flagged], Path("/tmp/v.jsonl"))
        self.assertEqual(rendered, "")

    def test_live_composed_brief_excludes_all_currently_flagged_findings(self) -> None:
        """Whatever findings are ACTUALLY authorship-flagged-and-open in the
        real tickets/ tree right now must be enumerated (so the claude-tier
        queue can see them) but never reach a live composed Codex brief.
        Enumerated dynamically -- not hardcoded to a specific ticket ID --
        so this test doesn't rot the day that finding closes; any other
        flagged finding filed later is covered the same way.
        """
        retests = runner.find_open_retest_tickets(runner.REPO_ROOT / "tickets")
        flagged = [r for r in retests if r.authorship_flagged]
        if not flagged:
            self.skipTest("no authorship-flagged open findings in the live tree right now")

        slot = runner.get_slot("weekly-narrow")
        brief = runner.compose_audit_brief(
            slot, date(2026, 8, 10), Path("/tmp/x.md"),
            tickets_root=runner.REPO_ROOT / "tickets",
        )
        for r in flagged:
            self.assertNotIn(
                r.ticket_id, brief,
                f"{r.ticket_id} is authorship-flagged and open but leaked "
                f"into the live composed Codex brief",
            )

    def test_composed_brief_never_reveals_the_claude_tier_delivery_channel(self) -> None:
        """T-2801: push_claude_tier_queue is runner-side only (called from
        land_findings, an HO-run CLI mode Codex's brief never instructs
        running) -- the T-2795 isolation invariant means neither the
        delivery mechanism (function name) nor render_claude_tier_
        verification_queue's own section content may surface in the
        Codex-facing composed brief, on top of the per-ticket-id exclusion
        already proven above.

        Note: the brief's PRE-EXISTING self-permission-check probe
        (`test -w /home/the-ancestor/mailbox/orchestrator`) legitimately contains
        orchestrator's mailbox path already -- that's the auditor being told to
        confirm it CANNOT write there, unrelated to this ticket's push
        mechanism -- so ORCHESTRATOR_INBOX's bare path is deliberately not
        asserted-absent here; the section header is the honest signal.
        """
        slot = runner.get_slot("weekly-narrow")
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            text = _wrap_ticket_text(
                "T-8010", "Flagged finding",
                "s3star-audit-finding: weekly-narrow:2026-08-01:S3STAR-B-001\n\n"
                "Re-test criteria: File mode is 0600.\n",
                labels="[s3star-audit, audit-finding, authorship-flagged]",
            )
            (inbox / "T-8010.md").write_text(text, encoding="utf-8")
            brief = runner.compose_audit_brief(
                slot, date(2026, 7, 20), Path("/tmp/x.md"),
                proposal_text=_FIXTURE_PROPOSAL, tickets_root=Path(tmp),
            )
        self.assertNotIn("push_claude_tier_queue", brief)
        self.assertNotIn("Claude-tier verification queue", brief)


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

    def test_emit_brief_includes_manifest_path_preview_key(self):
        # T-1596: parity with snapshot_path -- preview only, no capture.
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
                self.assertIn("manifest_path", records[0])
                self.assertEqual(
                    records[0]["manifest_path"],
                    str(runner.manifest_path_for(Path(records[0]["snapshot_path"]))),
                )
                self.assertIn(str(records[0]["manifest_path"]), records[0]["brief"])

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

    def test_emit_brief_includes_verdicts_path_preview_key(self):
        # T-2799: parity with findings_path/snapshot_path -- preview only,
        # no side-effecting artifact write.
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
                self.assertIn("verdicts_path", records[0])
                self.assertIn("verdicts.jsonl", records[0]["verdicts_path"])
                self.assertTrue(
                    records[0]["verdicts_path"].startswith(str(runner.AUDIT_WORKSPACE_ROOT))
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
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runner, "AUDIT_WORKSPACE_ROOT", Path(tmp)), \
             patch.object(runner, "_invoke_ticket_file") as mock_invoke, \
             patch.object(runner, "capture_runtime_snapshot") as mock_capture:
            mock_invoke.return_value = ("filed", "T-9999", "retries=0")
            outcome, tid, detail = runner.file_dispatch_ticket(slot, today, findings_path)
        self.assertEqual(outcome, "filed")
        self.assertEqual(tid, "T-9999")
        mock_capture.assert_called_once()
        argv = mock_invoke.call_args[0][0]
        self.assertIn("--target", argv)
        self.assertEqual(argv[argv.index("--target") + 1], "role:engineering_orchestrator")
        self.assertIn("--labels", argv)
        self.assertIn("s3star-audit,audit-dispatch", argv)
        ask = argv[argv.index("--ask") + 1]
        # T-1604: the ask names the new dedicated-session mechanism, not the
        # unrealizable "paste into codex-session" instruction it replaces.
        self.assertIn("--launch-audit", ask)
        self.assertIn("--teardown-audit", ask)
        self.assertIn(runner.STANDING_WORKER_SESSION, ask)  # names what NOT to touch
        self.assertIn(runner.codex_audit_session_name(slot.slug, today), ask)
        self.assertIn(f"--profile {runner.AUDIT_PROFILE}", ask)
        self.assertIn("--profile s3star-audit-p2", ask)  # pins the Phase-2 name explicitly
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
            # T-1604 D4: per-run subdir, not a flat prefixed filename.
            expected_snapshot = workspace_root / "2026-07-06-weekly-narrow" / "runtime-snapshot.md"
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


class DispatchTicketLandAfterExitTextTests(unittest.TestCase):
    """T-4307: the dispatch ticket must tell HO to land only after the CODEX
    PROCESS has exited in the codex-audit pane. On 2026-09-28 Codex rewrote
    verdicts.jsonl ~40s after the file first appeared, so retest verdicts
    landed twice; 'when findings.md first appears' is the wrong trigger.

    The gate is the Codex process, NOT the tmux session: the session is
    `tmux new-session -d` and Codex is typed into its interactive bash pane,
    so when Codex exits the pane returns to a shell prompt and the session
    lives on until --teardown-audit. A "session has EXITED" gate can never
    become true (review REBOUND B2)."""

    def _ask(self):
        slot = runner.get_slot("weekly-narrow")
        today = date(2026, 7, 6)
        ask, _ctx = runner.build_dispatch_ticket_bodies(
            slot, today, "BRIEF", Path("/tmp/s3star/2026-07-06/findings.md")
        )
        return slot, today, ask

    def test_ask_gates_landing_on_codex_process_exit(self):
        slot, today, ask = self._ask()
        session = runner.codex_audit_session_name(slot.slug, today)
        self.assertIn("Codex process has EXITED", ask)
        self.assertIn(f"in the `{session}` pane", ask)
        self.assertIn("not when", ask)  # explicitly rules out landing on first appearance
        self.assertNotIn("confirm Codex's findings landed", ask)

    def test_ask_does_not_gate_on_the_tmux_session_exiting(self):
        # B2 regression pin: the session stays alive after Codex exits (it is
        # only killed by --teardown-audit), so a "session has EXITED" gate is
        # unsatisfiable. The text must say so explicitly.
        _slot, _today, ask = self._ask()
        self.assertNotIn("session has EXITED", ask)
        self.assertIn("stays alive until --teardown-audit", ask)

    def test_ask_gives_a_concrete_process_exit_check(self):
        _slot, _today, ask = self._ask()
        self.assertIn(
            f"pgrep -u {runner.CODEX_AUDIT_USER} -af 'codex --profile {runner.AUDIT_PROFILE}'",
            ask,
        )
        # T-4314 (scope item 3): the claude account cannot read that pane, so
        # the "pane's last line is a shell prompt" alternative check is gone;
        # pgrep is the one check.
        self.assertNotIn("equivalently", ask)
        self.assertNotIn("last line is a shell prompt", ask)

    def test_exit_gate_precedes_both_landing_commands(self):
        _slot, _today, ask = self._ask()
        gate = ask.index("Codex process has EXITED")
        self.assertLess(gate, ask.index("--land-findings"))
        self.assertLess(gate, ask.index("--land-verdicts"))

    def test_ask_names_the_verdicts_landing_command(self):
        slot, today, ask = self._ask()
        verdicts_path = runner.run_workspace_dir(slot.slug, today) / "verdicts.jsonl"
        self.assertIn(f"--land-verdicts --slot {slot.slug}", ask)
        self.assertIn(f"--verdicts-file {verdicts_path}", ask)

    # -- T-4314 (scope item 2, wording approved by Orchestrator on plan Q4) --

    def test_ask_says_what_ho_does_with_a_land_findings_rc1(self):
        _slot, _today, ask = self._ask()
        self.assertIn("rc 1 means the cycle is NOT clean; read the printed problem lines.", ask)
        # T-4368 (item 4): worded PER CAUSE. This replaced the T-4314 sentence
        # "A sentinel problem (missing, mismatched, duplicated, or content after
        # it) means the run was truncated or crashed.", which overclaimed for the
        # duplicate and content-after causes (more likely a rewrite or append).
        self.assertIn(
            "A missing sentinel, or an N that does not match the number of "
            "`## Finding` sections, means the run was truncated or crashed.",
            ask,
        )
        self.assertIn(
            "A duplicated sentinel, or content after it, means the file was "
            "rewritten or appended to after sealing.",
            ask,
        )
        self.assertIn("Nothing was filed in any of these cases. Re-run the audit.", ask)
        self.assertIn(
            "A shape problem in a sealed file files the parseable findings and returns "
            "rc 1 on every re-land by design. Hand-file the rest instead of looping.",
            ask,
        )
        self.assertIn("Either way, do not close the dispatch ticket as a clean cycle.", ask)

    def test_ask_does_not_blame_truncation_for_a_rewrite_or_append(self):
        # T-4368 (item 4): each cause is attributed to ITS mechanism, never the
        # other's, and the old lumped list is gone.
        _slot, _today, ask = self._ask()
        sentences = ask.split(". ")
        missing_all = [s for s in sentences if "A missing sentinel" in s]
        rewrite_all = [s for s in sentences if "A duplicated sentinel" in s]
        self.assertEqual(len(missing_all), 1, "no per-cause sentence for a missing sentinel")
        self.assertEqual(len(rewrite_all), 1, "no per-cause sentence for a duplicated sentinel")
        missing, rewrite = missing_all[0], rewrite_all[0]
        self.assertIn("truncated or crashed", missing)
        self.assertNotIn("rewritten", missing)
        self.assertIn("rewritten or appended to after sealing", rewrite)
        self.assertNotIn("truncated", rewrite)
        self.assertNotIn("crashed", rewrite)
        self.assertNotIn("(missing, mismatched, duplicated, or content after it)", ask)

    def test_rc1_guidance_sits_between_the_two_landing_commands(self):
        _slot, _today, ask = self._ask()
        guidance = ask.index("rc 1 means the cycle is NOT clean")
        self.assertGreater(guidance, ask.index("--land-findings"))
        self.assertLess(guidance, ask.index("--land-verdicts"))

    def test_ask_only_promises_a_clean_landing_for_a_sealed_zero_finding_audit(self):
        _slot, _today, ask = self._ask()
        self.assertIn("one ending in its closing sentinel", ask)
        self.assertIn("lands clean, rc 0, filed 0", ask)

    def test_teardown_still_last(self):
        _slot, _today, ask = self._ask()
        # the teardown COMMAND (the flag is also mentioned earlier, in the
        # explanation of why the session never exits on its own)
        teardown_cmd = "python3 tools/s3star_audit_runner.py --teardown-audit"
        self.assertIn(teardown_cmd, ask)
        self.assertGreater(ask.index(teardown_cmd), ask.index("--land-verdicts"))


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

    def test_zero_finding_file_with_valid_frontmatter_is_landed(self):
        # T-4307: the brief tells Codex to omit the `## Finding` section when
        # nothing is actionable, so a valid zero-finding file must count as
        # landed-ok (this reverses the old test_missing_finding_heading_not_landed
        # pin).
        text = _zero_finding_file_text()
        self.assertGreaterEqual(len(text.encode("utf-8")), runner.FINDINGS_MIN_BYTES)
        self.assertNotIn("## Finding", text)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.md"
            p.write_text(text, encoding="utf-8")
            self.assertTrue(runner.findings_landed_ok(p))

    def test_zero_finding_file_without_frontmatter_not_landed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.md"
            p.write_text(_zero_finding_file_text(frontmatter=False), encoding="utf-8")
            self.assertFalse(runner.findings_landed_ok(p))

    def test_zero_finding_file_frontmatter_missing_date_not_landed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.md"
            p.write_text(
                _zero_finding_file_text().replace("date: 2026-07-06\n", ""),
                encoding="utf-8",
            )
            self.assertFalse(runner.findings_landed_ok(p))

    def test_too_small_zero_finding_file_not_landed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.md"
            p.write_text("---\nslot: x\ndate: y\n---\nAll clear.\n", encoding="utf-8")
            self.assertLess(p.stat().st_size, runner.FINDINGS_MIN_BYTES)
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

    def test_strict_heading_regex_is_the_one_constant_the_parser_uses(self):
        # T-4307 B1: land_findings' guard counts headings with the SAME module
        # constant parse_findings splits on, so the two cannot drift.
        text = _findings_file_text(
            _finding_block(finding_id="F-1"), _finding_block(finding_id="F-2", title="Second")
        )
        self.assertEqual(len(runner._FINDING_HEADING_RE.findall(text)), 2)
        self.assertEqual(len(runner.parse_findings(text)), 2)
        # a near-miss is invisible to both
        near = text.replace("## Finding 1: Sample finding", "### Finding 1: Sample finding")
        self.assertEqual(len(runner._FINDING_HEADING_RE.findall(near)), 1)
        self.assertEqual(len(runner.parse_findings(near)), 1)

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
        self.assertEqual(argv[argv.index("--target") + 1], "role:system_orchestrator")
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
        self.assertEqual(argv[argv.index("--target") + 1], "role:research_orchestrator")
        context = argv[argv.index("--context") + 1]
        self.assertIn("no codex-authored", context)
        self.assertNotIn("AUTHORSHIP FLAG", context)


def _land_text(text):
    """Land `text` from a temp dir with filing/push mocked. Returns
    (rc, mock_invoke, mock_push, runner_log_text)."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        (cwd / "tickets" / "inbox").mkdir(parents=True)
        log_path = cwd / "runner.log"
        findings_file = cwd / "findings.md"
        findings_file.write_text(text, encoding="utf-8")
        with patch.object(runner, "RUNNER_LOG", log_path), \
             patch.object(runner, "authorship_flag", return_value=False), \
             patch.object(runner, "push_claude_tier_queue") as mock_push, \
             patch.object(runner, "_invoke_ticket_file") as mock_invoke:
            mock_invoke.return_value = ("filed", "T-0001", "retries=0")
            rc = runner.land_findings(
                "weekly-narrow", findings_file, date(2026, 7, 6), cwd=cwd
            )
        log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    return rc, mock_invoke, mock_push, log


def _run_land_cli(text):
    """T-4368: drive the real CLI entry, `main(["--land-findings", ...])`, on
    `text`. Every side effect is patched out: ticket filing (`_invoke_ticket_file`),
    the claude-tier queue push, the authorship git-log lookup, the on-disk
    duplicate scan, the runner log, and `subprocess.run` itself (a fail-loud
    trip-wire: no git and no ticket CLI may run). Returns
    (rc, mock_invoke, mock_push, mock_subprocess_run, runner_log, stderr)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "runner.log"
        findings_file = tmp_path / "findings.md"
        findings_file.write_text(text, encoding="utf-8")
        stderr = io.StringIO()
        with patch.object(runner, "RUNNER_LOG", log_path), \
             patch.object(runner, "authorship_flag", return_value=False), \
             patch.object(runner, "finding_already_filed", return_value=False), \
             patch.object(runner, "push_claude_tier_queue") as mock_push, \
             patch.object(runner, "_invoke_ticket_file") as mock_invoke, \
             patch.object(
                 runner.subprocess, "run",
                 side_effect=AssertionError("the CLI test must not spawn a subprocess"),
             ) as mock_run, \
             contextlib.redirect_stderr(stderr):
            mock_invoke.return_value = ("filed", "T-0001", "retries=0")
            rc = runner.main([
                "--land-findings", "--slot", "weekly-narrow",
                "--findings-file", str(findings_file),
            ])
        log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    return rc, mock_invoke, mock_push, mock_run, log, stderr.getvalue()


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

    # -- T-4307: a clean (zero-finding) cycle must land as clean, distinguishable
    # from a broken audit. --

    def test_land_findings_zero_finding_file_is_clean_rc0_and_files_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / "tickets" / "inbox").mkdir(parents=True)
            log_path = cwd / "runner.log"
            findings_file = cwd / "findings.md"
            findings_file.write_text(_zero_finding_file_text(), encoding="utf-8")
            with patch.object(runner, "RUNNER_LOG", log_path), \
                 patch.object(runner, "_invoke_ticket_file") as mock_invoke:
                rc = runner.land_findings("weekly-narrow", findings_file, date(2026, 7, 6), cwd=cwd)
            self.assertEqual(rc, 0)
            mock_invoke.assert_not_called()
            log = log_path.read_text(encoding="utf-8")
            self.assertIn("filed 0, skipped 0, errored 0", log)
            self.assertNotIn("not landed-ok", log)

    def test_land_findings_missing_file_still_rc1(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with patch.object(runner, "RUNNER_LOG", cwd / "runner.log"):
                rc = runner.land_findings(
                    "weekly-narrow", cwd / "does-not-exist.md", date(2026, 7, 6), cwd=cwd
                )
            self.assertEqual(rc, 1)

    def test_land_findings_zero_finding_file_without_frontmatter_still_rc1(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            findings_file = cwd / "findings.md"
            findings_file.write_text(_zero_finding_file_text(frontmatter=False), encoding="utf-8")
            with patch.object(runner, "RUNNER_LOG", cwd / "runner.log"), \
                 patch.object(runner, "_invoke_ticket_file") as mock_invoke:
                rc = runner.land_findings("weekly-narrow", findings_file, date(2026, 7, 6), cwd=cwd)
            self.assertEqual(rc, 1)
            mock_invoke.assert_not_called()

    def test_land_findings_headings_present_but_none_parse_is_error_not_clean(self):
        # A file that HAS `## Finding` headings but none parse means the
        # auditor's output was malformed -- "the audit broke", not "found
        # nothing". Must not be a silent rc 0 now that zero findings is legal.
        bad = "## Finding 1: Broken\n- **ID:** F-1\n- **Severity:** Low\n"  # missing required fields
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / "tickets" / "inbox").mkdir(parents=True)
            log_path = cwd / "runner.log"
            findings_file = cwd / "findings.md"
            findings_file.write_text(
                _findings_file_text(bad + "padding to clear the minimum size. " * 8),
                encoding="utf-8",
            )
            with patch.object(runner, "RUNNER_LOG", log_path), \
                 patch.object(runner, "_invoke_ticket_file") as mock_invoke:
                rc = runner.land_findings("weekly-narrow", findings_file, date(2026, 7, 6), cwd=cwd)
            self.assertEqual(rc, 1)
            mock_invoke.assert_not_called()
            self.assertIn("none parsed", log_path.read_text(encoding="utf-8"))

    # -- T-4307 REBOUND B1: with the structural check no longer requiring a
    # heading, a real finding under a near-miss heading would otherwise land
    # as a silent "clean" rc 0. Guards live in land_findings; they change only
    # the return code + message, never what gets filed. --

    def _run_land(self, text):
        return _land_text(text)

    _CANONICAL_HEADING = "## Finding 1: Sample finding"

    def _block_with_heading(self, heading, **kw):
        block = _finding_block(**kw)
        self.assertIn(self._CANONICAL_HEADING, block)
        return block.replace(self._CANONICAL_HEADING, heading, 1)

    def test_near_miss_finding_headings_are_errors_not_clean(self):
        # Every shape here was rc 1 on main (the old structural check needed the
        # exact heading) and must stay loud. Each block carries all 11 required
        # bullets, so only the heading is wrong.
        near_miss = [
            "### Finding 1: t", "# Finding 1: t",
            "## finding 1: t", "## FINDING 1: t",
            "## Findings", "## Finding1: t", "##Finding 1: t", " ## Finding 1: t",
            "## 1. Finding: t",
        ]
        for heading in near_miss:
            with self.subTest(heading=heading):
                text = _findings_file_text(self._block_with_heading(heading))
                rc, mock_invoke, _push, log = self._run_land(text)
                self.assertEqual(rc, 1)
                mock_invoke.assert_not_called()
                self.assertIn("near-miss", log)

    def test_bold_and_id_style_finding_blocks_are_errors_not_clean(self):
        # No '#' + 'finding' heading line to catch, so the severity-bullet
        # count guard is what catches these.
        for heading in ("**Finding 1: t**", "## F-1: t"):
            with self.subTest(heading=heading):
                text = _findings_file_text(self._block_with_heading(heading))
                rc, mock_invoke, _push, log = self._run_land(text)
                self.assertEqual(rc, 1)
                mock_invoke.assert_not_called()
                self.assertIn("Severity", log)

    def test_mixed_good_and_near_miss_files_the_good_one_but_rc1(self):
        good = _finding_block(finding_id="F-1", owner="recovery")
        near = self._block_with_heading("### Finding 2: t", finding_id="F-2")
        rc, mock_invoke, _push, log = self._run_land(_findings_file_text(good, near))
        self.assertEqual(rc, 1)
        self.assertEqual(mock_invoke.call_count, 1)  # the parseable block is still filed
        self.assertIn("near-miss", log)

    def test_partly_malformed_files_the_good_ones_but_rc1(self):
        # T-4307 (Orchestrator's call, closes the silent-drop gap): a malformed block
        # is still skipped and the parseable ones are still filed, but the run
        # is no longer a silent rc 0.
        good = _finding_block(finding_id="F-1", owner="recovery")
        bad = "## Finding 2: Broken\n- **ID:** F-2\n- **Severity:** Low\n"
        rc, mock_invoke, _push, log = self._run_land(_findings_file_text(good, bad))
        self.assertEqual(rc, 1)
        self.assertEqual(mock_invoke.call_count, 1)
        self.assertIn("1 of 2", log)

    def test_partly_malformed_block_with_no_severity_bullet_is_still_rc1(self):
        # The severity-bullet guard alone would miss this; the heading-vs-parsed
        # count catches it.
        good = _finding_block(finding_id="F-1", owner="recovery")
        bad = "## Finding 2: Broken\nJust prose, no bullets at all.\n"
        rc, mock_invoke, _push, log = self._run_land(_findings_file_text(good, bad))
        self.assertEqual(rc, 1)
        self.assertEqual(mock_invoke.call_count, 1)
        self.assertIn("1 of 2", log)

    def test_over_the_soft_cap_files_the_cap_but_rc1(self):
        # 31 well-formed findings: parse_findings files the first 30. The
        # dropped one used to be a silent rc 0; it is now surfaced.
        blocks = [_finding_block(finding_id=f"F-{i}") for i in range(31)]
        rc, mock_invoke, _push, _log = self._run_land(_findings_file_text(*blocks))
        self.assertEqual(rc, 1)
        self.assertEqual(mock_invoke.call_count, 30)

    def test_well_formed_file_with_other_sections_trips_no_guard(self):
        # No false positive: prose that mentions "finding" outside a heading
        # line, and non-Finding '##' sections, must not trip either guard.
        preamble = (
            "## Audit Preamble\n\nCoverage notes. Nothing here is a finding heading; "
            "every finding is filed below.\n"
        )
        rc, mock_invoke, _push, log = self._run_land(
            _findings_file_text(preamble, _finding_block(finding_id="F-1", owner="recovery"))
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mock_invoke.call_count, 1)
        self.assertNotIn("near-miss", log)

    # -- T-4307 REBOUND T1: pin when the claude-tier queue push runs. The push
    # reads the currently-open authorship-flagged retests off disk and is
    # independent of this cycle's findings (no-op when none are flagged), so a
    # clean or error cycle must still reach it exactly once. --

    def test_clean_zero_finding_cycle_pushes_queue_exactly_once(self):
        rc, _invoke, mock_push, _log = self._run_land(_zero_finding_file_text())
        self.assertEqual(rc, 0)
        mock_push.assert_called_once()

    def test_none_parsed_cycle_pushes_queue_exactly_once(self):
        bad = "## Finding 1: Broken\n- **ID:** F-1\n- **Severity:** Low\n"
        rc, _invoke, mock_push, _log = self._run_land(
            _findings_file_text(bad + "padding to clear the minimum size. " * 8)
        )
        self.assertEqual(rc, 1)
        mock_push.assert_called_once()

    def test_near_miss_cycle_pushes_queue_exactly_once(self):
        text = _findings_file_text(self._block_with_heading("### Finding 1: t"))
        rc, _invoke, mock_push, _log = self._run_land(text)
        self.assertEqual(rc, 1)
        mock_push.assert_called_once()

    def test_structurally_rejected_file_does_not_push_queue(self):
        # Unchanged pre-existing behaviour: a not-landed-ok file returns before
        # anything else runs.
        rc, _invoke, mock_push, _log = self._run_land("not structured")
        self.assertEqual(rc, 1)
        mock_push.assert_not_called()

    # -- T-2801: land_findings is the one prod-mode step every supervised
    # audit cycle runs (per the module docstring's dispatch -> launch ->
    # observe -> land-findings -> teardown flow), so the claude-tier queue
    # push is folded in here rather than a new CLI mode HO would have to
    # remember to call separately. --

    def _seed_flagged_ticket(self, cwd, ticket_id, *, marker_id) -> None:
        inbox = cwd / "tickets" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        context = (
            f"s3star-audit-finding: weekly-narrow:2026-08-01:{marker_id}\n\n"
            f"Re-test criteria: Criteria for {ticket_id}.\n"
        )
        text = _wrap_ticket_text(
            ticket_id, f"Flagged finding {ticket_id}", context,
            labels="[s3star-audit, audit-finding, authorship-flagged]",
        )
        (inbox / f"{ticket_id}.md").write_text(text, encoding="utf-8")

    def test_land_findings_pushes_claude_tier_queue_when_flagged_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            today = date(2026, 7, 6)
            self._seed_flagged_ticket(cwd, "T-9410", marker_id="S3STAR-C-001")
            findings_file = cwd / "findings.md"
            findings_file.write_text(_findings_file_text(_finding_block(finding_id="F-1")), encoding="utf-8")
            with patch.object(runner, "_invoke_ticket_file") as mock_invoke, \
                 patch("tools.mailbox.send") as msend:
                mock_invoke.return_value = ("filed", "T-9999", "retries=0")
                msend.return_value = "msg-id"
                rc = runner.land_findings("weekly-narrow", findings_file, today, cwd=cwd)
            self.assertEqual(rc, 0)
            msend.assert_called_once()
            self.assertIn("T-9410", msend.call_args[0][1])

    def test_land_findings_no_push_when_nothing_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / "tickets" / "inbox").mkdir(parents=True)
            today = date(2026, 7, 6)
            findings_file = cwd / "findings.md"
            findings_file.write_text(_findings_file_text(_finding_block(finding_id="F-1")), encoding="utf-8")
            with patch.object(runner, "_invoke_ticket_file") as mock_invoke, \
                 patch("tools.mailbox.send") as msend:
                mock_invoke.return_value = ("filed", "T-9999", "retries=0")
                rc = runner.land_findings("weekly-narrow", findings_file, today, cwd=cwd)
            self.assertEqual(rc, 0)
            msend.assert_not_called()

    def test_land_findings_return_code_unaffected_by_push_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            today = date(2026, 7, 6)
            self._seed_flagged_ticket(cwd, "T-9411", marker_id="S3STAR-C-002")
            findings_file = cwd / "findings.md"
            findings_file.write_text(_findings_file_text(_finding_block(finding_id="F-1")), encoding="utf-8")
            with patch.object(runner, "_invoke_ticket_file") as mock_invoke, \
                 patch("tools.mailbox.send", side_effect=RuntimeError("mailbox down")):
                mock_invoke.return_value = ("filed", "T-9999", "retries=0")
                rc = runner.land_findings("weekly-narrow", findings_file, today, cwd=cwd)
            self.assertEqual(rc, 0)  # finding-filing succeeded; push failure must not flip this

    def test_land_findings_threads_its_own_cwd_into_the_push(self):
        """Team-lead confirmation #2: the wired call must pass land_findings'
        OWN cwd, never fall back to push_claude_tier_queue's REPO_ROOT
        default -- otherwise the flagged set it pushes would silently
        diverge from what this cycle just landed."""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / "tickets" / "inbox").mkdir(parents=True)
            today = date(2026, 7, 6)
            findings_file = cwd / "findings.md"
            findings_file.write_text(_findings_file_text(_finding_block(finding_id="F-1")), encoding="utf-8")
            with patch.object(runner, "_invoke_ticket_file") as mock_invoke, \
                 patch.object(runner, "push_claude_tier_queue") as mock_push:
                mock_invoke.return_value = ("filed", "T-9999", "retries=0")
                runner.land_findings("weekly-narrow", findings_file, today, cwd=cwd)
            mock_push.assert_called_once_with(cwd=cwd)


class SplitClosingSentinelTests(unittest.TestCase):
    """T-4314: `_split_closing_sentinel(text) -> (body, problems)`. Pure. `body`
    is `text` minus valid sentinel lines (what gets parsed and shape-checked);
    `problems` is empty iff the file carries exactly one well-formed sentinel,
    as its last non-blank line, whose N equals the number of strict
    `## Finding` headings."""

    def test_sealed_file_has_no_problems_and_body_drops_the_sentinel(self):
        text = _findings_file_text(_finding_block(finding_id="F-1"))
        body, problems = runner._split_closing_sentinel(text)
        self.assertEqual(problems, [])
        self.assertNotIn("s3star-audit-end", body)
        self.assertIn("## Finding 1: Sample finding", body)

    def test_trailing_blank_lines_after_the_sentinel_are_fine(self):
        text = _findings_file_text(_finding_block()) + "\n\n   \n"
        _body, problems = runner._split_closing_sentinel(text)
        self.assertEqual(problems, [])

    def test_horizontal_whitespace_around_the_sentinel_is_tolerated(self):
        text = _findings_file_text(_finding_block(), sealed=False)
        text += "\n  s3star-audit-end:   findings = 1 \t\n"
        _body, problems = runner._split_closing_sentinel(text)
        self.assertEqual(problems, [])

    def test_missing_sentinel_is_one_problem_naming_truncation_or_crash(self):
        _body, problems = runner._split_closing_sentinel(_zero_finding_file_text(sealed=False))
        self.assertEqual(len(problems), 1)
        self.assertIn("no closing sentinel", problems[0])
        self.assertIn("truncated or crashed", problems[0])

    def test_token_and_key_are_case_sensitive(self):
        for variant in ("S3STAR-AUDIT-END: findings=0", "s3star-audit-end: Findings=0"):
            with self.subTest(variant=variant):
                text = _zero_finding_file_text(sealed=False) + "\n" + variant + "\n"
                _body, problems = runner._split_closing_sentinel(text)
                self.assertTrue(any("no closing sentinel" in p for p in problems))

    # -- T-4368: line-ending and end-of-file pins (already-correct behaviour) --

    def test_crlf_line_endings_are_accepted(self):
        text = _findings_file_text(_finding_block(finding_id="F-1")).replace("\n", "\r\n")
        self.assertIn("\r\n", text)
        body, problems = runner._split_closing_sentinel(text)
        self.assertEqual(problems, [])
        self.assertNotIn("s3star-audit-end", body)

    def test_a_sentinel_with_no_final_newline_is_accepted(self):
        text = _findings_file_text(_finding_block(finding_id="F-1")).rstrip("\n")
        self.assertTrue(text.endswith("findings=1"))
        _body, problems = runner._split_closing_sentinel(text)
        self.assertEqual(problems, [])

    # -- T-4368 items 1 and 2: N is a bounded run of ASCII digits --

    def test_huge_n_is_a_malformed_sentinel_problem_and_raises_nothing(self):
        # 5000 digits is above int()'s default 4300-digit limit; on the T-4314
        # code this raised ValueError out of the split (a traceback, no problem
        # line). It must be an ordinary "malformed sentinel" problem instead, and
        # the quoted line must not echo the whole numeral.
        text = _zero_finding_file_text(sealed=False) + "\ns3star-audit-end: findings=" + "1" * 5000 + "\n"
        _body, problems = runner._split_closing_sentinel(text)
        self.assertEqual(len(problems), 1)
        self.assertIn("no closing sentinel", problems[0])
        self.assertIn("malformed", problems[0])
        self.assertLess(len(problems[0]), 1000)

    def test_non_ascii_digits_are_not_a_valid_n(self):
        for label, digit in (("fullwidth", "０"), ("arabic-indic", "٠")):
            with self.subTest(digit=label):
                text = _zero_finding_file_text(sealed=False) + "\ns3star-audit-end: findings=" + digit + "\n"
                _body, problems = runner._split_closing_sentinel(text)
                self.assertTrue(any("no closing sentinel" in p for p in problems))

    def test_duplicate_and_content_after_problems_do_not_claim_truncation(self):
        # T-4368 item 4 (runner side): the problem lines were already hedged per
        # cause; only the missing-sentinel line names truncation or a crash.
        sealed = _findings_file_text(_finding_block(finding_id="F-1"))
        _b, dup = runner._split_closing_sentinel(sealed + "\n" + _sentinel_line(1) + "\n")
        _b, after = runner._split_closing_sentinel(sealed + "\n## Appendix\n\nLate text.\n")
        self.assertEqual(len(dup), 1)
        self.assertEqual(len(after), 1)
        for problem in (dup[0], after[0]):
            self.assertNotIn("truncated", problem)
            self.assertNotIn("crashed", problem)
        self.assertIn("rewrite", dup[0])
        self.assertIn("rewritten or appended", after[0])


class LandFindingsClosingSentinelTests(unittest.TestCase):
    """T-4314: a crashed or truncated Codex run must not land as a clean
    zero-finding audit. A missing/mismatched/misplaced/duplicated closing
    sentinel is FAIL-CLOSED: rc 1 and NOTHING filed (Orchestrator ruling on plan Q2:
    the run is untrustworthy as a whole, and partial filing plus a later-day
    re-land would duplicate tickets since markers key on the landing date).
    Shape problems inside a correctly sealed file keep T-4307's contract:
    parseable findings filed, rc 1."""

    def _assert_sentinel_rejected(self, text, *, needle):
        rc, invoke, mock_push, log = _land_text(text)
        self.assertEqual(rc, 1)
        self.assertEqual(invoke.call_count, 0, "a sentinel problem must file NOTHING")
        mock_push.assert_not_called()
        self.assertIn(needle, log)
        self.assertIn("nothing filed", log)
        return log

    # -- missing sentinel / truncated run --

    def test_missing_sentinel_with_findings_is_rc1_and_files_zero_tickets(self):
        text = _findings_file_text(
            _finding_block(finding_id="F-1"),
            _finding_block(finding_id="F-2", title="Second"),
            sealed=False,
        )
        log = self._assert_sentinel_rejected(text, needle="no closing sentinel")
        self.assertIn("truncated or crashed", log)

    def test_missing_sentinel_on_zero_finding_file_is_not_a_clean_cycle(self):
        # The exact false-assurance shape: valid frontmatter, no Finding
        # sections, ends on DECISIONS -- what the real 2026-09-07/-14/-28 files
        # look like, and what a crash after DECISIONS looks like.
        self._assert_sentinel_rejected(
            _zero_finding_file_text(sealed=False), needle="no closing sentinel"
        )

    def test_truncated_after_frontmatter_and_preamble_is_rc1(self):
        text = (
            "---\nslot: weekly-narrow\ndate: 2026-07-06\nauditor: codex\n---\n\n"
            "## Audit Preamble\n\n"
            "Coverage selected for this rotation: file permissions, ledger sampling, "
            "and the mailbox hub boundary. Beginning the evidence read now.\n"
        )
        self.assertGreaterEqual(len(text.encode("utf-8")), runner.FINDINGS_MIN_BYTES)
        self._assert_sentinel_rejected(text, needle="no closing sentinel")

    def test_pre_sentinel_legacy_file_is_not_accepted_and_message_says_so(self):
        # No legacy path (plan decision): a file shaped like the 12 real
        # pre-T-4314 findings files (findings, no sentinel) is rc 1, ever.
        text = _findings_file_text(_finding_block(finding_id="F-1"), sealed=False)
        log = self._assert_sentinel_rejected(text, needle="no closing sentinel")
        self.assertIn("before T-4314", log)

    def test_malformed_sentinel_like_line_alone_is_rc1_and_is_quoted(self):
        text = _findings_file_text(_finding_block(), sealed=False)
        text += "\ns3star-audit-end: findings=two\n"
        log = self._assert_sentinel_rejected(text, needle="no closing sentinel")
        self.assertIn("findings=two", log)

    def test_prose_mention_of_the_sentinel_is_not_a_false_positive(self):
        preamble = (
            "## Audit Preamble\n\nI will end this file with "
            "`s3star-audit-end: findings=1` as instructed.\n"
        )
        rc, invoke, _push, _log = _land_text(
            _findings_file_text(preamble, _finding_block(finding_id="F-1"))
        )
        self.assertEqual(rc, 0)
        self.assertEqual(invoke.call_count, 1)

    # -- N mismatch --

    def test_n_mismatch_is_rc1_and_files_zero_tickets(self):
        blocks = (_finding_block(finding_id="F-1"), _finding_block(finding_id="F-2", title="B"))
        for n in (0, 1, 3):  # too low, too low, too high (file has 2)
            with self.subTest(n=n):
                text = _findings_file_text(*blocks, sealed=False) + "\n" + _sentinel_line(n) + "\n"
                log = self._assert_sentinel_rejected(text, needle=f"claims {n}")
                self.assertIn("2 '## Finding' heading(s)", log)

    def test_clean_claim_on_a_file_that_has_findings_is_rc1(self):
        text = _findings_file_text(_finding_block(), sealed=False) + "\n" + _sentinel_line(0) + "\n"
        self._assert_sentinel_rejected(text, needle="claims 0")

    # -- correct files land --

    def test_correct_zero_finding_file_with_zero_sentinel_is_clean_rc0(self):
        text = _zero_finding_file_text()
        self.assertTrue(text.rstrip().endswith("s3star-audit-end: findings=0"))
        rc, invoke, mock_push, log = _land_text(text)
        self.assertEqual(rc, 0)
        self.assertEqual(invoke.call_count, 0)
        mock_push.assert_called_once()
        self.assertIn("filed 0, skipped 0, errored 0", log)

    def test_correct_n_finding_files_land_rc0_and_file_n_tickets(self):
        for n in (1, 3):
            with self.subTest(n=n):
                blocks = [_finding_block(finding_id=f"F-{i}", title=f"T{i}") for i in range(n)]
                text = _findings_file_text(*blocks)
                self.assertTrue(text.rstrip().endswith(f"s3star-audit-end: findings={n}"))
                rc, invoke, mock_push, _log = _land_text(text)
                self.assertEqual(rc, 0)
                self.assertEqual(invoke.call_count, n)
                mock_push.assert_called_once()

    def test_sentinel_directly_after_last_bullet_does_not_leak_into_the_ticket(self):
        # parse_findings folds a non-blank line after a bullet into that field;
        # the sentinel must be stripped BEFORE parsing or it is filed inside the
        # last finding's Justification.
        text = _findings_file_text(_finding_block(finding_id="F-1"), sealed=False).rstrip("\n")
        text += "\n" + _sentinel_line(1) + "\n"
        rc, invoke, _push, _log = _land_text(text)
        self.assertEqual(rc, 0)
        self.assertEqual(invoke.call_count, 1)
        argv = invoke.call_args[0][0]
        self.assertNotIn("s3star-audit-end", argv[argv.index("--context") + 1])
        self.assertNotIn("s3star-audit-end", argv[argv.index("--ask") + 1])

    # -- misplaced / duplicated sentinel --

    def test_content_after_the_sentinel_is_rc1_and_files_zero_tickets(self):
        text = _findings_file_text(_finding_block(finding_id="F-1"))
        text += "\n## Appendix\n\nA late addition written after the file was sealed.\n"
        self._assert_sentinel_rejected(text, needle="follow the closing sentinel")

    def test_sentinel_mid_file_is_rc1_and_files_zero_tickets(self):
        early = _findings_file_text(_finding_block(finding_id="F-1")).rstrip("\n")
        text = early + "\n\n" + _finding_block(finding_id="F-2", title="Second")
        self._assert_sentinel_rejected(text, needle="follow the closing sentinel")

    def test_two_sentinels_is_rc1_and_files_zero_tickets(self):
        text = _findings_file_text(_finding_block(finding_id="F-1"))
        text += "\n" + _sentinel_line(1) + "\n"
        self._assert_sentinel_rejected(text, needle="2 closing sentinels")

    def test_quoted_sentinel_in_a_fence_plus_the_real_one_is_rc1(self):
        # Documented loud false positive: the brief tells the auditor to write
        # the sentinel once and never quote it.
        preamble = "## Audit Preamble\n\n```\n" + _sentinel_line(0) + "\n```\n"
        text = _findings_file_text(preamble, _finding_block(finding_id="F-1"))
        self._assert_sentinel_rejected(text, needle="2 closing sentinels")

    # -- T-4368 items 1 and 2: bounded, ASCII-only N --

    def _with_sentinel(self, sentinel_line):
        return (
            _findings_file_text(_finding_block(finding_id="F-1"), sealed=False)
            + "\n" + sentinel_line + "\n"
        )

    def test_huge_n_is_rc1_with_a_log_line_and_no_traceback(self):
        # 7 digits is just past the cap; 5000 is past int()'s 4300-digit limit,
        # where the T-4314 code raised ValueError out of land_findings (fail
        # closed, but a traceback and no runner-log line). Both must be an
        # ordinary malformed-sentinel problem: rc 1, nothing filed, logged, and
        # the log must not echo the whole numeral.
        for digits in (7, 5000):
            with self.subTest(digits=digits):
                text = self._with_sentinel("s3star-audit-end: findings=" + "1" * digits)
                log = self._assert_sentinel_rejected(text, needle="no closing sentinel")
                self.assertNotIn("claims", log)
                self.assertLess(len(log), 2000)

    def test_six_digit_n_is_still_an_ordinary_mismatch(self):
        # The cap boundary: 6 digits parse and are compared to the count.
        text = self._with_sentinel("s3star-audit-end: findings=999999")
        self._assert_sentinel_rejected(text, needle="claims 999999")

    def test_non_ascii_digit_n_is_rejected_even_when_its_value_matches(self):
        # `\d` accepts Unicode digits, and int() parses them, so a count-matching
        # fullwidth or Arabic-Indic "1" used to seal a one-finding file.
        for label, digit in (("fullwidth", "１"), ("arabic-indic", "١")):
            with self.subTest(digit=label):
                text = self._with_sentinel("s3star-audit-end: findings=" + digit)
                self._assert_sentinel_rejected(text, needle="no closing sentinel")

    # -- T-4368: line-ending and end-of-file pins through land_findings --

    def test_a_crlf_file_lands_like_an_lf_file(self):
        text = _findings_file_text(
            _finding_block(finding_id="F-1"), _finding_block(finding_id="F-2", title="B")
        ).replace("\n", "\r\n")
        rc, invoke, mock_push, _log = _land_text(text)
        self.assertEqual(rc, 0)
        self.assertEqual(invoke.call_count, 2)
        mock_push.assert_called_once()
        # ...and no carriage return leaks into the filed ticket text.
        for call in invoke.call_args_list:
            argv = call[0][0]
            self.assertNotIn("\r", argv[argv.index("--ask") + 1])
            self.assertNotIn("\r", argv[argv.index("--context") + 1])

    def test_a_crlf_zero_finding_file_is_clean(self):
        rc, invoke, _push, _log = _land_text(_zero_finding_file_text().replace("\n", "\r\n"))
        self.assertEqual(rc, 0)
        self.assertEqual(invoke.call_count, 0)

    def test_a_crlf_unsealed_file_is_still_rejected(self):
        text = _findings_file_text(_finding_block(finding_id="F-1"), sealed=False)
        self._assert_sentinel_rejected(text.replace("\n", "\r\n"), needle="no closing sentinel")

    def test_a_file_with_no_final_newline_lands(self):
        text = _findings_file_text(_finding_block(finding_id="F-1")).rstrip("\n")
        self.assertTrue(text.endswith("findings=1"))
        rc, invoke, _push, _log = _land_text(text)
        self.assertEqual(rc, 0)
        self.assertEqual(invoke.call_count, 1)

    def test_a_zero_finding_file_with_no_final_newline_is_clean(self):
        text = _zero_finding_file_text().rstrip("\n")
        self.assertTrue(text.endswith("findings=0"))
        rc, invoke, _push, _log = _land_text(text)
        self.assertEqual(rc, 0)
        self.assertEqual(invoke.call_count, 0)

    # -- ordering / T-4307 contract --

    def test_sealed_file_with_a_malformed_block_still_files_the_good_ones_rc1(self):
        # T-4307 regression pin: shape problems INSIDE a correctly sealed file
        # keep the old contract (parseable findings filed, rc 1). The fail-closed
        # short-circuit is for sentinel problems only.
        good = _finding_block(finding_id="F-1", owner="recovery")
        bad = "## Finding 2: Broken\n- **ID:** F-2\n- **Severity:** Low\n"
        text = _findings_file_text(good, bad)
        self.assertTrue(text.rstrip().endswith("s3star-audit-end: findings=2"))
        rc, invoke, mock_push, log = _land_text(text)
        self.assertEqual(rc, 1)
        self.assertEqual(invoke.call_count, 1)
        self.assertIn("1 of 2", log)
        self.assertNotIn("sentinel", log)
        mock_push.assert_called_once()

    def test_sentinel_check_short_circuits_before_any_parse_or_filing(self):
        text = _findings_file_text(_finding_block(finding_id="F-1"), sealed=False)
        with patch.object(runner, "parse_findings") as mock_parse, \
             patch.object(runner, "file_finding") as mock_file:
            rc, _invoke, _push, _log = _land_text(text)
        self.assertEqual(rc, 1)
        mock_parse.assert_not_called()
        mock_file.assert_not_called()

    def test_over_cap_sealed_file_reports_one_problem_line_for_one_cause(self):
        # T-4307 round-2 scope item 5: 31 sealed findings -> parse caps at 30;
        # the cap is ONE problem ("only 30 of 31"), not that plus a redundant
        # severity-bullet line.
        text = _findings_file_text(*[_finding_block(finding_id=f"F-{i}") for i in range(31)])
        body, sentinel_problems = runner._split_closing_sentinel(text)
        self.assertEqual(sentinel_problems, [])
        problems = runner._findings_shape_problems(body, 30)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("30 of 31", problems[0])


class LandFindingsCliTests(unittest.TestCase):
    """T-4368: the sentinel gate holds through the real CLI entry,
    `main(["--land-findings", ...])`, not just `land_findings` called directly.
    Everything with a side effect is patched (see `_run_land_cli`), and the
    subprocess trip-wire must never fire."""

    def test_unsealed_file_is_rc1_with_nothing_filed_and_nothing_pushed(self):
        for label, text in (
            ("findings", _findings_file_text(_finding_block(finding_id="F-1"), sealed=False)),
            ("zero-finding", _zero_finding_file_text(sealed=False)),
        ):
            with self.subTest(file=label):
                rc, invoke, push, run, log, err = _run_land_cli(text)
                self.assertEqual(rc, 1)
                self.assertEqual(invoke.call_count, 0, "an unsealed file must file NOTHING")
                push.assert_not_called()
                self.assertEqual(run.call_count, 0)
                self.assertIn("no closing sentinel", err)
                self.assertIn("nothing filed", log)

    def test_n_mismatch_is_rc1_with_nothing_filed_and_nothing_pushed(self):
        text = _findings_file_text(_finding_block(finding_id="F-1"), sealed=False)
        text += "\n" + _sentinel_line(2) + "\n"
        rc, invoke, push, run, _log, err = _run_land_cli(text)
        self.assertEqual(rc, 1)
        self.assertEqual(invoke.call_count, 0)
        push.assert_not_called()
        self.assertEqual(run.call_count, 0)
        self.assertIn("claims 2", err)

    def test_sealed_file_with_n_findings_is_rc0_and_files_n(self):
        for n in (1, 3):
            with self.subTest(n=n):
                blocks = [_finding_block(finding_id=f"F-{i}", title=f"T{i}") for i in range(n)]
                rc, invoke, push, run, _log, err = _run_land_cli(_findings_file_text(*blocks))
                self.assertEqual(rc, 0)
                self.assertEqual(invoke.call_count, n)
                push.assert_called_once()
                self.assertEqual(run.call_count, 0)
                self.assertEqual(err, "")

    def test_sealed_zero_finding_file_is_clean_rc0(self):
        rc, invoke, push, run, log, err = _run_land_cli(_zero_finding_file_text())
        self.assertEqual(rc, 0)
        self.assertEqual(invoke.call_count, 0)
        push.assert_called_once()
        self.assertEqual(run.call_count, 0)
        self.assertIn("filed 0, skipped 0, errored 0", log)
        self.assertEqual(err, "")

    def test_huge_n_through_the_cli_is_rc1_not_a_traceback(self):
        text = _zero_finding_file_text(sealed=False) + "\ns3star-audit-end: findings=" + "9" * 5000 + "\n"
        rc, invoke, push, _run, log, err = _run_land_cli(text)
        self.assertEqual(rc, 1)
        self.assertEqual(invoke.call_count, 0)
        push.assert_not_called()
        self.assertIn("no closing sentinel", err)
        self.assertIn("closing-sentinel check failed", log)


class ComposeAuditBriefClosingSentinelTests(unittest.TestCase):
    """T-4314: the brief tells Codex to end the file with the sentinel, and the
    literal it shows is the one land_findings accepts."""

    def _brief(self):
        return runner.compose_audit_brief(
            runner.get_slot("weekly-narrow"), date(2026, 7, 6),
            Path("/tmp/s3star-test/findings.md"), proposal_text=_FIXTURE_PROPOSAL,
        )

    def test_brief_shows_the_sentinel_literal_once_on_its_own_line(self):
        brief = self._brief()
        self.assertEqual(
            len(re.findall(r"^s3star-audit-end: findings=N$", brief, flags=re.MULTILINE)), 1
        )

    def test_brief_says_last_line_exactly_once_nothing_after(self):
        brief = self._brief()
        self.assertIn("as the LAST line of the file", brief)
        self.assertIn("exactly once", brief)
        self.assertIn("Nothing may follow it", brief)
        self.assertIn("0 for a clean cycle", brief)

    def test_sentinel_instruction_comes_after_the_finding_sections_instruction(self):
        brief = self._brief()
        self.assertLess(
            brief.index("one `## Finding <n>: <title>` section per finding"),
            brief.index("s3star-audit-end: findings=N"),
        )

    # -- T-4368 item 3: the sentinel is shown bare, and the brief says so --

    def test_brief_shows_the_sentinel_bare_not_inside_a_code_fence(self):
        # An auditor that copies a fenced example writes the fence too, and the
        # closing ``` counts as content after the sentinel: rc 1 and a wasted
        # Codex run. So the literal must sit OUTSIDE any fence in the brief.
        brief = self._brief()
        in_fence = False
        seen = False
        for line in brief.split("\n"):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
            elif line == "s3star-audit-end: findings=N":
                seen = True
                self.assertFalse(in_fence, "the sentinel literal is shown inside a code fence")
        self.assertTrue(seen)
        self.assertFalse(in_fence, "unbalanced code fences in the brief")

    def test_brief_says_the_sentinel_is_a_plain_line_not_in_a_code_fence(self):
        flat = " ".join(self._brief().split())
        self.assertIn("It is a plain line of text, NOT inside a code fence", flat)
        # ...and names the consequence, so the auditor sees why it matters.
        self.assertIn("counts as content after the sentinel and fails the run", flat)

    def test_brief_pins_the_zero_finding_heading_wording(self):
        # T-4307 round-2 scope item 1: any other heading containing "finding"
        # trips the near-miss guard (rc 1), so the brief must say so.
        brief = self._brief()
        self.assertIn('ONLY for the `## Finding <n>: <title>` sections', brief)
        self.assertIn("No findings this cycle", brief)

    def test_the_briefs_own_sentinel_literal_round_trips_through_land_findings(self):
        # Drift guard: extract the literal FROM the brief, substitute N, and
        # land it. If the brief's example and the parser ever disagree, this
        # fails instead of the next live audit.
        brief = self._brief()
        line = re.search(r"^s3star-audit-end: findings=N$", brief, flags=re.MULTILINE).group(0)
        for n in (0, 2):
            with self.subTest(n=n):
                blocks = [_finding_block(finding_id=f"F-{i}", title=f"T{i}") for i in range(n)]
                text = (
                    _findings_file_text(*blocks, sealed=False)
                    if blocks else _zero_finding_file_text(sealed=False)
                )
                text = text.rstrip("\n") + "\n\n" + line.replace("N", str(n)) + "\n"
                rc, invoke, _push, _log = _land_text(text)
                self.assertEqual(rc, 0)
                self.assertEqual(invoke.call_count, n)


class PushClaudeTierQueueTests(unittest.TestCase):
    """T-2801: push_claude_tier_queue is the structural delivery half of
    render_claude_tier_verification_queue -- reads currently-open flagged
    retests straight off disk (find_open_retest_tickets, real fixtures, not
    a patched reader -- so the byte-stability assertion below is honest
    about what the real reader does), renders, and mailbox.send()s to
    ORCHESTRATOR_INBOX when non-empty."""

    def _seed(self, tickets_root, ticket_id, *, flagged, marker_id):
        inbox = tickets_root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        labels = (
            "[s3star-audit, audit-finding, authorship-flagged]" if flagged
            else "[s3star-audit, audit-finding]"
        )
        context = (
            f"s3star-audit-finding: weekly-narrow:2026-08-01:{marker_id}\n\n"
            f"Re-test criteria: Criteria for {ticket_id}.\n"
        )
        text = _wrap_ticket_text(ticket_id, f"Finding {ticket_id}", context, labels=labels)
        (inbox / f"{ticket_id}.md").write_text(text, encoding="utf-8")

    def test_pushes_when_flagged_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._seed(cwd / "tickets", "T-9401", flagged=True, marker_id="S3STAR-A-001")
            self._seed(cwd / "tickets", "T-9402", flagged=False, marker_id="S3STAR-A-002")
            with patch("tools.mailbox.send") as msend:
                msend.return_value = "msg-1"
                result = runner.push_claude_tier_queue(cwd=cwd)
            self.assertEqual(result, "msg-1")
            msend.assert_called_once()
            args, kwargs = msend.call_args
            self.assertEqual(args[0], runner.ORCHESTRATOR_INBOX)
            self.assertIn("T-9401", args[1])
            self.assertNotIn("T-9402", args[1])
            self.assertEqual(args[2], "s3star_audit_runner")
            self.assertIn("1", kwargs.get("subject", ""))
            # dedupe_pending must stay at mailbox.send's own default (True) --
            # cross-cycle no-spam relies on that, not a reimplementation here.
            self.assertNotIn("dedupe_pending", kwargs)

    def test_no_push_when_nothing_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._seed(cwd / "tickets", "T-9403", flagged=False, marker_id="S3STAR-A-003")
            with patch("tools.mailbox.send") as msend:
                result = runner.push_claude_tier_queue(cwd=cwd)
            msend.assert_not_called()
            self.assertIsNone(result)

    def test_no_push_when_tickets_root_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with patch("tools.mailbox.send") as msend:
                result = runner.push_claude_tier_queue(cwd=cwd)
            msend.assert_not_called()
            self.assertIsNone(result)

    def test_send_failure_is_logged_and_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._seed(cwd / "tickets", "T-9404", flagged=True, marker_id="S3STAR-A-004")
            log_path = cwd / "s3star.log"
            with patch("tools.mailbox.send", side_effect=RuntimeError("mailbox down")), \
                 patch.object(runner, "RUNNER_LOG", log_path):
                result = runner.push_claude_tier_queue(cwd=cwd)
            self.assertIsNone(result)
            self.assertIn("claude-tier queue push failed", log_path.read_text(encoding="utf-8"))

    def test_render_is_byte_stable_across_repeated_calls_on_unchanged_ticket_set(self):
        """Team-lead confirmation #1: cross-cycle dedupe in mailbox.send only
        collapses a BYTE-IDENTICAL resend, so the rendered payload must be
        deterministic when the on-disk flagged set hasn't changed. Pins
        find_open_retest_tickets' existing fixed-state-order +
        sorted(glob(...)) shape (verified at source; not reimplemented
        here -- no new sort added, this only pins the existing guarantee)."""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._seed(cwd / "tickets", "T-9405", flagged=True, marker_id="S3STAR-A-005")
            self._seed(cwd / "tickets", "T-9406", flagged=True, marker_id="S3STAR-A-006")
            first = runner.render_claude_tier_verification_queue(
                runner.find_open_retest_tickets(cwd / "tickets")
            )
            second = runner.render_claude_tier_verification_queue(
                runner.find_open_retest_tickets(cwd / "tickets")
            )
            self.assertEqual(first, second)
            self.assertNotEqual(first, "")


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
                 patch.object(runner, "AUDIT_WORKSPACE_ROOT", Path(tmp)), \
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
        #
        # dispatch_ticket_already_filed is patched (T-2067) because it scans the
        # LIVE tickets/inbox/ — main() does not thread a tickets_root through.
        # Without the patch this test is CALENDAR-COUPLED and fails on exactly
        # the days the audit actually runs: a real "S3* audit dispatch — <slot>
        # <today>" ticket sitting in inbox/ trips the idempotency guard, the
        # slot is skipped, file_dispatch_ticket (mocked to fail) is never
        # called, no failure is recorded, and main() returns 0 — surfacing as
        # `AssertionError: 0 != 1` on the RETURN CODE, not on any log count.
        # Observed 2026-07-27 against T-2063; it self-healed the next day when
        # T-2063 moved to done/, which is what made it look like a phantom
        # regression that nobody could reproduce.
        with tempfile.TemporaryDirectory() as tmp:
            own_log = Path(tmp) / "s3star.log"
            state_file = Path(tmp) / "state.json"
            with patch.object(runner, "RUNNER_LOG", own_log), \
                 patch.object(runner, "STATE_FILE", state_file), \
                 patch.object(runner, "STATE_DIR", Path(tmp)), \
                 patch.object(runner, "file_dispatch_ticket", return_value=("errored", None, "boom")), \
                 patch.object(runner, "dispatch_ticket_already_filed", return_value=False), \
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

    def test_exactly_eight_commands(self):
        cmds = runner._snapshot_commands()
        self.assertEqual(len(cmds), 8)

    def test_roots_cover_hub_and_five_named_agents(self):
        cmds = runner._snapshot_commands()
        ls_cmd = next(c for c in cmds if c.argv[:2] == ["ls", "-ld"])
        self.assertIn(str(runner._SNAPSHOT_MAILBOX_HUB), ls_cmd.argv)
        for agent in ("codex", "orchestrator", "specialist", "recovery"):
            self.assertIn(str(runner._SNAPSHOT_MAILBOX_HUB / agent), ls_cmd.argv)
        # T-3421: the-ancestor is retired from the fleet; its hub dir is no longer audited.
        self.assertNotIn(str(runner._SNAPSHOT_MAILBOX_HUB / "the-ancestor"), ls_cmd.argv)

    def test_identity_users_command_covers_five_unix_accounts(self):
        # T-1596: `id` must target real unix ACCOUNT names, not the mailbox-dir
        # PERSONA names used by the ls -ld command above -- those are a
        # different namespace (`id orchestrator` fails; there is no unix user orchestrator).
        cmds = runner._snapshot_commands()
        id_cmd = next(c for c in cmds if c.argv[0] == "id")
        self.assertEqual(id_cmd.argv[1:], list(runner._SNAPSHOT_UNIX_USERS))
        for account in ("claude", "claude-research", "claude-fallback", "codex"):
            self.assertIn(account, runner._SNAPSHOT_UNIX_USERS)
        self.assertNotIn("the-ancestor", runner._SNAPSHOT_UNIX_USERS)  # T-3421 retired
        self.assertNotIn("orchestrator", runner._SNAPSHOT_UNIX_USERS)
        self.assertNotIn("recovery", runner._SNAPSHOT_UNIX_USERS)

    def test_all_commands_have_unique_slugs(self):
        cmds = runner._snapshot_commands()
        slugs = [c.slug for c in cmds]
        self.assertEqual(len(slugs), len(set(slugs)))

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
        self.assertIn("pantheon-mailbox:x:1006", text)
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
        self.assertNotIn("pantheon-mailbox:x:1006", text)  # getent section degraded, not real output
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
        self.assertIn("pantheon-mailbox:x:1006", text)  # other sections unaffected

    def test_all_commands_fail_never_raises_still_writes_file(self):
        def fake_run(argv, **kwargs):
            raise OSError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "snap.md"
            runner.capture_runtime_snapshot(snapshot_path, run=fake_run)
            self.assertTrue(snapshot_path.exists())
            text = snapshot_path.read_text(encoding="utf-8")
        self.assertEqual(text.count("WARNING"), 8)

    def test_default_generated_at_is_utc_now(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "snap.md"
            with patch.object(runner, "_utc_iso_now", return_value="2099-01-01T00:00:00Z"):
                runner.capture_runtime_snapshot(snapshot_path, run=_fake_run_all_ok)
            text = snapshot_path.read_text(encoding="utf-8")
        self.assertIn("2099-01-01T00:00:00Z", text)


class ManifestPathForTests(unittest.TestCase):
    """T-1596: manifest_path_for -- pure function of snapshot_path."""

    def test_manifest_path_is_evidence_subdir_of_snapshot_parent(self):
        snapshot_path = Path("/tmp/x/2026-07-14-weekly-narrow/runtime-snapshot.md")
        self.assertEqual(
            runner.manifest_path_for(snapshot_path),
            Path("/tmp/x/2026-07-14-weekly-narrow/evidence/MANIFEST.md"),
        )

    def test_manifest_path_deterministic(self):
        snapshot_path = Path("/tmp/x/run/runtime-snapshot-at-launch.md")
        self.assertEqual(
            runner.manifest_path_for(snapshot_path),
            runner.manifest_path_for(snapshot_path),
        )


class EvidenceBundleTests(unittest.TestCase):
    """T-1596: capture_runtime_snapshot's per-artifact evidence bundle +
    MANIFEST, written alongside the composed snapshot markdown."""

    _ALL_SLUGS = (
        "systemd-timers", "systemd-units", "processes", "sockets",
        "ownership-named", "ownership-numeric", "identity-group", "identity-users",
    )

    def test_bundle_writes_one_file_per_command_plus_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "run" / "runtime-snapshot.md"
            runner.capture_runtime_snapshot(
                snapshot_path, generated_at="2026-07-14T00:00:00Z", run=_fake_run_all_ok
            )
            evidence_dir = snapshot_path.parent / "evidence"
            for slug in self._ALL_SLUGS:
                self.assertTrue((evidence_dir / f"{slug}.txt").exists(), slug)
            self.assertTrue((evidence_dir / "MANIFEST.md").exists())

    def test_artifact_files_are_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "run" / "runtime-snapshot.md"
            runner.capture_runtime_snapshot(snapshot_path, run=_fake_run_all_ok)
            evidence_dir = snapshot_path.parent / "evidence"
            mode = S_IMODE((evidence_dir / "processes.txt").stat().st_mode)
            manifest_mode = S_IMODE((evidence_dir / "MANIFEST.md").stat().st_mode)
        self.assertEqual(mode, 0o444)
        self.assertEqual(manifest_mode, 0o444)

    def test_manifest_sha256_matches_written_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "run" / "runtime-snapshot.md"
            runner.capture_runtime_snapshot(snapshot_path, run=_fake_run_all_ok)
            evidence_dir = snapshot_path.parent / "evidence"
            manifest = (evidence_dir / "MANIFEST.md").read_text(encoding="utf-8")
            for slug in self._ALL_SLUGS:
                artifact = evidence_dir / f"{slug}.txt"
                expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
                self.assertIn(expected, manifest, slug)

    def test_manifest_has_limits_section_with_named_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "run" / "runtime-snapshot.md"
            runner.capture_runtime_snapshot(snapshot_path, run=_fake_run_all_ok)
            manifest = (snapshot_path.parent / "evidence" / "MANIFEST.md").read_text(encoding="utf-8")
        self.assertIn("## Limits", manifest)
        self.assertIn("absence-of-channel", manifest)
        self.assertIn("systemctl --user", manifest)
        self.assertIn("Cross-account socket-to-process", manifest)

    def test_failed_capture_recorded_as_warning_not_fatal(self):
        def fake_run(argv, **kwargs):
            if argv[:2] == ["getent", "group"]:
                raise FileNotFoundError("getent not found")
            return _fake_run_all_ok(argv, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "run" / "runtime-snapshot.md"
            runner.capture_runtime_snapshot(snapshot_path, run=fake_run)  # must not raise
            evidence_dir = snapshot_path.parent / "evidence"
            manifest = (evidence_dir / "MANIFEST.md").read_text(encoding="utf-8")
            identity_group_body = (evidence_dir / "identity-group.txt").read_text(encoding="utf-8")
        self.assertIn("WARNING", identity_group_body)
        self.assertIn("`identity-group.txt`", manifest)
        self.assertIn("## Capture notes", manifest)
        self.assertIn("`processes.txt`", manifest)
        self.assertIn(" OK ", manifest)  # at least one unaffected artifact still OK

    def test_manifest_frontmatter_has_generated_at_and_captured_as(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "run" / "runtime-snapshot.md"
            with patch.object(runner, "_captured_as", return_value="claude"):
                runner.capture_runtime_snapshot(
                    snapshot_path, generated_at="2026-07-14T00:00:00Z", run=_fake_run_all_ok
                )
            manifest = (snapshot_path.parent / "evidence" / "MANIFEST.md").read_text(encoding="utf-8")
        self.assertIn("generated_at: 2026-07-14T00:00:00Z", manifest)
        self.assertIn("captured_as: claude", manifest)

    def test_no_duplicate_subprocess_calls(self):
        # T-1596 plan review: the composed doc and the evidence bundle must
        # come from ONE execution pass, not two -- assert each command's argv
        # is invoked exactly once.
        calls: list[tuple] = []

        def counting_run(argv, **kwargs):
            calls.append(tuple(argv))
            return _fake_run_all_ok(argv, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "run" / "runtime-snapshot.md"
            runner.capture_runtime_snapshot(snapshot_path, run=counting_run)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(len(calls), 8)


class EvidenceImmutabilityTests(unittest.TestCase):
    """T-1732 (S3STAR-2026-07-13-001): runner-captured evidence artifacts
    (runtime snapshot(s), brief.md) must be non-writable to the audit worker
    immediately after capture. findings.md is deliberately NOT covered here --
    it's created by the auditor, not the runner, and must stay writable."""

    def _fake_run_ok(self, argv, **kwargs):
        return MagicMock(returncode=0, stdout="", stderr="")

    def test_capture_runtime_snapshot_writes_read_only_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "snap.md"
            runner.capture_runtime_snapshot(snapshot_path, run=_fake_run_all_ok)
            mode = S_IMODE(snapshot_path.stat().st_mode)
        self.assertEqual(mode, 0o444)

    def test_capture_runtime_snapshot_rewrite_does_not_crash_and_stays_locked(self):
        # Simulates a same-day re-run of file_dispatch_ticket targeting a path
        # a prior cycle already locked down -- must not raise PermissionError,
        # and the new content (not the stale content) must be what's readable.
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "snap.md"
            runner.capture_runtime_snapshot(snapshot_path, generated_at="2026-07-13T00:00:00Z", run=_fake_run_all_ok)
            runner.capture_runtime_snapshot(snapshot_path, generated_at="2026-07-13T01:00:00Z", run=_fake_run_all_ok)
            text = snapshot_path.read_text(encoding="utf-8")
            mode = S_IMODE(snapshot_path.stat().st_mode)
        self.assertIn("2026-07-13T01:00:00Z", text)
        self.assertNotIn("2026-07-13T00:00:00Z", text)
        self.assertEqual(mode, 0o444)

    def test_launch_audit_session_brief_is_read_only(self):
        slot = runner.get_slot("weekly-narrow")
        today = date(2026, 7, 13)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner, "AUDIT_WORKSPACE_ROOT", Path(tmp)), \
                 patch.object(runner, "capture_runtime_snapshot") as mock_capture:
                mock_capture.side_effect = lambda path, **kw: path.write_text("x", encoding="utf-8") or path
                result = runner.launch_audit_session(slot, today, run=self._fake_run_ok)
            mode = S_IMODE(result["brief_path"].stat().st_mode)
        self.assertEqual(mode, 0o444)

    def test_launch_audit_session_relaunch_does_not_crash_on_locked_brief(self):
        # Re-launch (e.g. after teardown) targets the same run_dir/brief.md a
        # prior launch already locked down -- must not raise PermissionError.
        slot = runner.get_slot("weekly-narrow")
        today = date(2026, 7, 13)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner, "AUDIT_WORKSPACE_ROOT", Path(tmp)), \
                 patch.object(runner, "capture_runtime_snapshot") as mock_capture:
                mock_capture.side_effect = lambda path, **kw: path.write_text("x", encoding="utf-8") or path
                runner.launch_audit_session(slot, today, run=self._fake_run_ok)
                result2 = runner.launch_audit_session(slot, today, run=self._fake_run_ok)
            mode = S_IMODE(result2["brief_path"].stat().st_mode)
        self.assertEqual(mode, 0o444)


class RunWorkspaceDirTests(unittest.TestCase):
    """T-1604 D4: per-run subdir + session-name pure helpers."""

    def test_run_workspace_dir_shape(self):
        d = runner.run_workspace_dir("weekly-narrow", date(2026, 7, 13))
        self.assertEqual(d, runner.AUDIT_WORKSPACE_ROOT / "2026-07-13-weekly-narrow")

    def test_run_workspace_dir_respects_patched_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner, "AUDIT_WORKSPACE_ROOT", Path(tmp)):
                d = runner.run_workspace_dir("monthly-integrated", date(2026, 7, 13))
            self.assertEqual(d, Path(tmp) / "2026-07-13-monthly-integrated")

    def test_codex_audit_session_name(self):
        name = runner.codex_audit_session_name("weekly-narrow", date(2026, 7, 13))
        self.assertEqual(name, "codex-audit-weekly-narrow-2026-07-13")

    def test_ensure_run_workspace_dir_creates_and_chmods(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner, "AUDIT_WORKSPACE_ROOT", Path(tmp)):
                run_dir = runner.ensure_run_workspace_dir("weekly-narrow", date(2026, 7, 13))
            self.assertTrue(run_dir.is_dir())
            # 2770: setgid + rwx for owner/group, nothing for other.
            self.assertEqual(run_dir.stat().st_mode & 0o7777, 0o2770)

    def test_ensure_run_workspace_dir_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner, "AUDIT_WORKSPACE_ROOT", Path(tmp)):
                d1 = runner.ensure_run_workspace_dir("weekly-narrow", date(2026, 7, 13))
                d2 = runner.ensure_run_workspace_dir("weekly-narrow", date(2026, 7, 13))
            self.assertEqual(d1, d2)


class QuotingTests(unittest.TestCase):
    """T-1884: the wrapper-script redirect collapses this to ONE real
    shell-parsing layer. `_sudo_codex_argv` passes args through to the
    wrapper as literal argv elements (no shell in between — sudo execs the
    wrapper directly, and the wrapper execs tmux directly), so the only
    place a hostile string gets re-parsed by a shell is the codex-audit
    PANE's own interactive bash recovering `read_instruction` from
    `codex --profile s3star-audit '<...>'` after `tmux send-keys` types it
    in. shlex.split is an exact stand-in for that pass because
    shlex.quote's output is always single-quoted (or has no special
    chars), and single-quoted content has zero metacharacter/expansion
    processing in POSIX shells. (Prior to T-1884 there was a second layer —
    the now-removed `sudo -u codex bash -lc "<inner>"` — see git history if
    that shape needs re-deriving.)
    """

    _HOSTILE = (
        "Read ./brief.md and say 'hello world', don't stop; "
        "run `id` && echo $HOME safely."
    )

    def test_codex_launch_command_round_trips_hostile_read_instruction(self):
        command_string = runner._codex_launch_command(self._HOSTILE)
        # The wrapper's `send` verb relays this string into
        # `tmux send-keys ... "<command_string>" Enter` as ONE literal argv
        # element end-to-end (proven by test_sudo_codex_argv_passes_hostile_args_as_literal_elements
        # below + WrapperScriptTests.test_send_accepted_hostile_command_string_stays_one_argv_element) —
        # the pane's own bash is the only remaining parser, and it must
        # recover the exact original hostile instruction.
        recovered = shlex.split(command_string)
        self.assertEqual(recovered, ["codex", "--profile", runner.AUDIT_PROFILE, self._HOSTILE])

    def test_sudo_codex_argv_shape(self):
        argv = runner._sudo_codex_argv("kill", "codex-audit-weekly-narrow-2026-07-13")
        self.assertEqual(
            argv,
            ["sudo", "-n", "-u", runner.CODEX_AUDIT_USER, runner.WRAPPER_PATH,
             "kill", "codex-audit-weekly-narrow-2026-07-13"],
        )

    def test_sudo_codex_argv_passes_hostile_args_as_literal_elements(self):
        # No shell sits between this list and the wrapper's argv (sudo execs
        # it directly) — a hostile arg is never re-parsed, just carried.
        hostile = "; rm -rf / # `id` && echo $HOME"
        argv = runner._sudo_codex_argv("send", "codex-audit-weekly-narrow-2026-07-13", hostile)
        self.assertEqual(argv[-1], hostile)


class LaunchAuditSessionTests(unittest.TestCase):
    """T-1604 D1-D3: launch_audit_session composes a fresh brief+snapshot,
    writes brief.md into the per-run subdir, and spawns/launches via the
    sudo -u codex tmux pattern (mocked `run`; no live sudo/tmux)."""

    def _fake_run_ok(self, argv, **kwargs):
        return MagicMock(returncode=0, stdout="", stderr="")

    def test_writes_brief_and_launch_time_snapshot_not_overwriting_filed_one(self):
        slot = runner.get_slot("weekly-narrow")
        today = date(2026, 7, 13)
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp)
            with patch.object(runner, "AUDIT_WORKSPACE_ROOT", workspace_root):
                run_dir = runner.ensure_run_workspace_dir(slot.slug, today)
                # Simulate a prior --file-dispatch-ticket run having already
                # captured the filed-time snapshot at the canonical name.
                filed_snapshot = run_dir / "runtime-snapshot.md"
                filed_snapshot.write_text("FILED-TIME EVIDENCE — do not destroy\n", encoding="utf-8")

                with patch.object(runner, "capture_runtime_snapshot") as mock_capture:
                    mock_capture.side_effect = lambda path, **kw: path.write_text("fresh\n", encoding="utf-8") or path
                    result = runner.launch_audit_session(slot, today, run=self._fake_run_ok)

            # Filed-time evidence must survive untouched (directive 1).
            self.assertEqual(filed_snapshot.read_text(encoding="utf-8"), "FILED-TIME EVIDENCE — do not destroy\n")
            # Launch-time snapshot is a distinct sibling file.
            self.assertEqual(result["snapshot_path"], run_dir / "runtime-snapshot-at-launch.md")
            mock_capture.assert_called_once_with(run_dir / "runtime-snapshot-at-launch.md")
            self.assertTrue(result["brief_path"].exists())
            self.assertIn("READ-ONLY audit", result["brief_path"].read_text(encoding="utf-8"))
            self.assertEqual(result["findings_path"], run_dir / "findings.md")
            self.assertEqual(result["session"], "codex-audit-weekly-narrow-2026-07-13")

    def test_spawns_and_launches_with_expected_argv(self):
        slot = runner.get_slot("weekly-narrow")
        today = date(2026, 7, 13)
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return MagicMock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner, "AUDIT_WORKSPACE_ROOT", Path(tmp)), \
                 patch.object(runner, "capture_runtime_snapshot") as mock_capture:
                mock_capture.side_effect = lambda path, **kw: path.write_text("x", encoding="utf-8") or path
                result = runner.launch_audit_session(slot, today, run=fake_run)

        self.assertEqual(len(calls), 2)
        spawn_argv, launch_argv = calls
        self.assertEqual(spawn_argv[:5], ["sudo", "-n", "-u", "codex", runner.WRAPPER_PATH])
        self.assertEqual(spawn_argv[5], "new")
        self.assertEqual(spawn_argv[6], "codex-audit-weekly-narrow-2026-07-13")
        self.assertEqual(spawn_argv[7], str(result["run_dir"]))
        self.assertEqual(launch_argv[:5], ["sudo", "-n", "-u", "codex", runner.WRAPPER_PATH])
        self.assertEqual(launch_argv[5], "send")
        self.assertEqual(launch_argv[6], "codex-audit-weekly-narrow-2026-07-13")
        self.assertIn("codex --profile s3star-audit", launch_argv[7])

    def test_spawn_failure_raises_loud(self):
        slot = runner.get_slot("weekly-narrow")
        today = date(2026, 7, 13)

        def fake_run(argv, **kwargs):
            return MagicMock(returncode=1, stdout="", stderr="sudo: a password is required")

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner, "AUDIT_WORKSPACE_ROOT", Path(tmp)), \
                 patch.object(runner, "capture_runtime_snapshot") as mock_capture:
                mock_capture.side_effect = lambda path, **kw: path.write_text("x", encoding="utf-8") or path
                with self.assertRaises(RuntimeError) as ctx:
                    runner.launch_audit_session(slot, today, run=fake_run)
        self.assertIn("password is required", str(ctx.exception))
        # T-1884: the -n refusal shape gets a loud, specific install pointer,
        # not just the raw rc/stderr.
        self.assertIn("tools/sudoers.d/claude-codex-audit", str(ctx.exception))
        self.assertIn("tools/sudoers.d/pantheon-s3star-tmux", str(ctx.exception))

    def test_non_refusal_failure_does_not_get_nopasswd_hint(self):
        slot = runner.get_slot("weekly-narrow")
        today = date(2026, 7, 13)

        def fake_run(argv, **kwargs):
            return MagicMock(returncode=1, stdout="", stderr="no such tmux server")

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner, "AUDIT_WORKSPACE_ROOT", Path(tmp)), \
                 patch.object(runner, "capture_runtime_snapshot") as mock_capture:
                mock_capture.side_effect = lambda path, **kw: path.write_text("x", encoding="utf-8") or path
                with self.assertRaises(RuntimeError) as ctx:
                    runner.launch_audit_session(slot, today, run=fake_run)
        self.assertNotIn("tools/sudoers.d", str(ctx.exception))

    def test_launch_failure_after_successful_spawn_raises_loud(self):
        slot = runner.get_slot("weekly-narrow")
        today = date(2026, 7, 13)
        call_count = 0

        def fake_run(argv, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="no such tmux pane")

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner, "AUDIT_WORKSPACE_ROOT", Path(tmp)), \
                 patch.object(runner, "capture_runtime_snapshot") as mock_capture:
                mock_capture.side_effect = lambda path, **kw: path.write_text("x", encoding="utf-8") or path
                with self.assertRaises(RuntimeError) as ctx:
                    runner.launch_audit_session(slot, today, run=fake_run)
        self.assertIn("no such tmux pane", str(ctx.exception))
        self.assertEqual(call_count, 2)


class TeardownAuditSessionTests(unittest.TestCase):
    def test_normal_teardown_succeeds(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return MagicMock(returncode=0, stdout="", stderr="")

        ok = runner.teardown_audit_session("weekly-narrow", date(2026, 7, 13), run=fake_run)
        self.assertTrue(ok)
        self.assertEqual(calls[0][:5], ["sudo", "-n", "-u", "codex", runner.WRAPPER_PATH])
        self.assertEqual(calls[0][5], "kill")
        self.assertEqual(calls[0][6], "codex-audit-weekly-narrow-2026-07-13")

    def test_already_gone_session_is_idempotent_success(self):
        def fake_run(argv, **kwargs):
            return MagicMock(returncode=1, stdout="", stderr="can't find session codex-audit-x")

        ok = runner.teardown_audit_session("weekly-narrow", date(2026, 7, 13), run=fake_run)
        self.assertTrue(ok)

    def test_other_failure_raises_loud(self):
        def fake_run(argv, **kwargs):
            return MagicMock(returncode=1, stdout="", stderr="sudo: a password is required")

        with self.assertRaises(RuntimeError) as ctx:
            runner.teardown_audit_session("weekly-narrow", date(2026, 7, 13), run=fake_run)
        self.assertIn("password is required", str(ctx.exception))
        self.assertIn("tools/sudoers.d/claude-codex-audit", str(ctx.exception))
        self.assertIn("tools/sudoers.d/pantheon-s3star-tmux", str(ctx.exception))


class WrapperScriptTests(unittest.TestCase):
    """T-1884: exercises tools/sudoers.d/pantheon-s3star-tmux's bash
    validation directly, invoking the real repo copy as a subprocess — no
    sudo and no install needed. S3STAR_WORKSPACE_ROOT / S3STAR_TMUX_BIN are
    test-only env overrides that let this validation logic be exercised
    pre-install; their inertness whenever SUDO_USER is present (the real
    cross-account path) is CODE-ENFORCED in the wrapper (T-1884 REBOUND) —
    see the SudoUserOverrideInertnessTests below for that proof.
    """

    SCRIPT = runner.REPO_ROOT / "tools" / "sudoers.d" / "pantheon-s3star-tmux"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace_root = Path(self.tmp.name) / "s3star-workspace"
        self.workspace_root.mkdir()
        self.stub_tmux = Path(self.tmp.name) / "tmux-stub.sh"
        self.stub_tmux.write_text(
            "#!/usr/bin/env bash\n"
            'echo "ARGC:$#"\n'
            'printf \'%s\\n\' "$@"\n',
            encoding="utf-8",
        )
        self.stub_tmux.chmod(0o755)

    def _run(self, *args, env_extra=None):
        env = dict(os.environ)
        # T-1973: the wrapper CODE-ENFORCES ignoring both test overrides when
        # SUDO_USER is present (the T-1884 inertness property, proven by
        # SudoUserOverrideInertnessTests below, which injects the marker
        # deliberately). This class needs the overrides HONORED — but seat
        # sessions historically inherited SUDO_* from their sudo spawn
        # (T-1974), so building the child env from ambient os.environ made
        # these tests hard-fail on real tmux off the home account. Scrub the
        # markers from the CHILD env explicitly: full coverage on every
        # account, no ambient-environment sensitivity, no skip needed.
        for sudo_var in ("SUDO_USER", "SUDO_UID", "SUDO_GID", "SUDO_COMMAND"):
            env.pop(sudo_var, None)
        env["S3STAR_WORKSPACE_ROOT"] = str(self.workspace_root)
        env["S3STAR_TMUX_BIN"] = str(self.stub_tmux)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [str(self.SCRIPT), *args],
            capture_output=True, text=True, timeout=10, env=env,
        )

    def test_new_accepted_and_execs_stub_tmux_with_resolved_run_dir(self):
        run_dir = self.workspace_root / "2026-07-13-weekly-narrow"
        run_dir.mkdir()
        result = self._run("new", "codex-audit-weekly-narrow-2026-07-13", str(run_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "ARGC:6")
        self.assertEqual(
            lines[1:],
            ["new-session", "-d", "-s", "codex-audit-weekly-narrow-2026-07-13",
             "-c", str(run_dir.resolve())],
        )

    def test_send_accepted_hostile_command_string_stays_one_argv_element(self):
        hostile = "; rm -rf / # `id` && echo $HOME"
        result = self._run("send", "codex-audit-monthly-integrated-2026-08-01", hostile)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "ARGC:5")
        self.assertEqual(
            lines[1:],
            ["send-keys", "-t", "codex-audit-monthly-integrated-2026-08-01", hostile, "Enter"],
        )

    def test_kill_accepted(self):
        result = self._run("kill", "codex-audit-weekly-narrow-2026-07-13")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["ARGC:3", "kill-session", "-t", "codex-audit-weekly-narrow-2026-07-13"],
        )

    def test_unknown_verb_rejected(self):
        result = self._run("delete", "codex-audit-weekly-narrow-2026-07-13")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown verb", result.stderr)

    def test_bad_session_shape_rejected(self):
        result = self._run("kill", "not-a-codex-audit-session")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rejected session", result.stderr)

    def test_unknown_slot_in_session_rejected(self):
        result = self._run("kill", "codex-audit-not-a-real-slot-2026-07-13")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rejected session", result.stderr)

    def test_run_dir_outside_workspace_root_rejected(self):
        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        result = self._run("new", "codex-audit-weekly-narrow-2026-07-13", str(outside))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside workspace root", result.stderr)

    def test_run_dir_traversal_outside_workspace_root_rejected(self):
        # A path that LOOKS prefixed but escapes via `..` must still be
        # caught — `readlink -f` canonicalizes before the prefix check.
        run_dir = self.workspace_root / "2026-07-13-weekly-narrow"
        run_dir.mkdir()
        (Path(self.tmp.name) / "elsewhere").mkdir(exist_ok=True)
        traversal = str(run_dir) + "/../../elsewhere"
        result = self._run("new", "codex-audit-weekly-narrow-2026-07-13", traversal)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside workspace root", result.stderr)

    def test_wrong_arg_count_rejected(self):
        result = self._run("kill")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires exactly", result.stderr)


class SudoUserOverrideInertnessTests(unittest.TestCase):
    """T-1884 REBOUND: with SUDO_USER present (sudo always sets this on the
    process it execs), the wrapper must unset both test-only overrides
    BEFORE reading them -- proven here by injecting attacker-controlled
    values for both alongside SUDO_USER and showing they have zero effect,
    not merely by inspecting the code. This closes the gap the reviewer
    found: inertness no longer depends solely on `claude-codex-audit`
    omitting SETENV/env_keep (a property this test suite can't observe
    pre-install anyway) -- it's now directly observable in the wrapper's
    own behavior.
    """

    SCRIPT = runner.REPO_ROOT / "tools" / "sudoers.d" / "pantheon-s3star-tmux"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stub_tmux = Path(self.tmp.name) / "tmux-stub.sh"
        self.stub_tmux.write_text(
            "#!/usr/bin/env bash\n"
            'echo "ARGC:$#"\n'
            'printf \'%s\\n\' "$@"\n',
            encoding="utf-8",
        )
        self.stub_tmux.chmod(0o755)

    def _run_as_sudo(self, *args, extra_env=None):
        env = dict(os.environ)
        env["SUDO_USER"] = "claude"
        env["S3STAR_WORKSPACE_ROOT"] = str(Path(self.tmp.name) / "attacker-workspace")
        env["S3STAR_TMUX_BIN"] = str(self.stub_tmux)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(self.SCRIPT), *args],
            capture_output=True, text=True, timeout=10, env=env,
        )

    def test_workspace_root_override_ignored_under_sudo_user(self):
        attacker_dir = Path(self.tmp.name) / "attacker-workspace" / "run"
        attacker_dir.mkdir(parents=True)
        result = self._run_as_sudo(
            "new", "codex-audit-weekly-narrow-2026-07-13", str(attacker_dir),
        )
        # If the override had been honored, this would be accepted (the
        # attacker dir IS under their own injected root). It's rejected,
        # and the "outside workspace root (...)" parenthetical names the
        # REAL production default, not the injected override -- proving
        # the override was never read (the run_dir itself still mentions
        # the attacker path, since that's the legitimate resolved arg).
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "outside workspace root (/var/lib/pantheon/s3star-workspace):",
            result.stderr,
        )

    def test_tmux_bin_override_ignored_under_sudo_user(self):
        result = self._run_as_sudo("kill", "codex-audit-weekly-narrow-2026-07-13")
        # If the override had been honored, the stub would have run and
        # printed its distinctive ARGC: echo with rc=0. Instead the real
        # /usr/bin/tmux runs (no session/server exists in the test
        # environment) and fails -- the stub is never invoked.
        self.assertNotIn("ARGC:", result.stdout)
        self.assertNotEqual(result.returncode, 0)


class CliNewModesTests(unittest.TestCase):
    def test_launch_audit_requires_slot(self):
        rc = runner.main(["--launch-audit"])
        self.assertEqual(rc, 64)

    def test_teardown_audit_requires_slot(self):
        rc = runner.main(["--teardown-audit"])
        self.assertEqual(rc, 64)

    def test_launch_audit_conflicts_with_other_modes(self):
        rc = runner.main(["--launch-audit", "--slot", "weekly-narrow", "--emit-brief"])
        self.assertEqual(rc, 64)

    def test_bad_date_rejected(self):
        rc = runner.main(["--launch-audit", "--slot", "weekly-narrow", "--date", "not-a-date"])
        self.assertEqual(rc, 64)

    def test_launch_audit_dispatches_with_slot_and_date(self):
        with patch.object(runner, "launch_audit_session") as mock_launch, \
             patch("sys.stdout"):
            mock_launch.return_value = {
                "session": "codex-audit-weekly-narrow-2026-07-13",
                "run_dir": Path("/tmp/x"),
                "brief_path": Path("/tmp/x/brief.md"),
                "findings_path": Path("/tmp/x/findings.md"),
                "snapshot_path": Path("/tmp/x/runtime-snapshot-at-launch.md"),
            }
            rc = runner.main(["--launch-audit", "--slot", "weekly-narrow", "--date", "2026-07-13"])
        self.assertEqual(rc, 0)
        mock_launch.assert_called_once()
        called_slot, called_date = mock_launch.call_args[0]
        self.assertEqual(called_slot.slug, "weekly-narrow")
        self.assertEqual(called_date, date(2026, 7, 13))

    def test_launch_audit_defaults_date_to_today(self):
        with patch.object(runner, "launch_audit_session") as mock_launch, \
             patch("sys.stdout"):
            mock_launch.return_value = {
                "session": "s", "run_dir": Path("/tmp/x"), "brief_path": Path("/tmp/x/b"),
                "findings_path": Path("/tmp/x/f"), "snapshot_path": Path("/tmp/x/s"),
            }
            runner.main(["--launch-audit", "--slot", "weekly-narrow"])
        called_slot, called_date = mock_launch.call_args[0]
        self.assertEqual(called_date, datetime.now(timezone.utc).date())

    def test_teardown_audit_dispatches_with_slot_and_date(self):
        with patch.object(runner, "teardown_audit_session") as mock_teardown, \
             patch("sys.stdout"):
            mock_teardown.return_value = True
            rc = runner.main(["--teardown-audit", "--slot", "weekly-narrow", "--date", "2026-07-13"])
        self.assertEqual(rc, 0)
        mock_teardown.assert_called_once_with("weekly-narrow", date(2026, 7, 13))


class TestSnapshotIdentityCoversTheDeclaredFleet(unittest.TestCase):
    """T-2405: the evidence channel must cover the CURRENT fleet, not a copy of
    an older one.

    The literals here are the independently-typed ones from
    test_account_persona_map (README Topology, cross-checked against
    `getent passwd`), imported from the TEST module rather than from
    agent_role_map. That matters: asserting the runner's derived tuples equal
    agent_role_map's derived tuples is a tautology that passes forever. These
    assert the runner covers a fleet written down somewhere else.
    """

    def test_every_declared_account_is_in_the_id_capture(self) -> None:
        from tools.test_account_persona_map import ALL_ACCOUNTS

        missing = sorted(set(ALL_ACCOUNTS) - set(runner._SNAPSHOT_UNIX_USERS))
        self.assertEqual(missing, [], f"accounts absent from the id capture: {missing}")

    def test_every_declared_persona_has_a_mailbox_root(self) -> None:
        from tools.test_account_persona_map import ALL_ACCOUNTS

        missing = sorted(set(ALL_ACCOUNTS.values()) - set(runner._SNAPSHOT_MAILBOX_AGENTS))
        self.assertEqual(missing, [], f"personas absent from the mailbox roots: {missing}")

    def test_group_query_names_the_live_group_not_the_retired_one(self) -> None:
        """`veneration` was RENAMED to pantheon-mailbox and no longer resolves,
        so the old query exited 2 every cycle — which reads as a degraded
        capture rather than as a wrong question."""
        self.assertNotEqual(runner._SNAPSHOT_MAILBOX_GROUP, "veneration")
        argv = [c.argv for c in runner._snapshot_commands() if c.slug == "identity-group"]
        self.assertEqual(argv, [["getent", "group", runner._SNAPSHOT_MAILBOX_GROUP]])

    def test_brief_does_not_hardcode_a_group_as_host_truth(self) -> None:
        """The bwrap worked example used to ASSERT `the-ancestor:veneration` as
        the host truth that corrected a false finding. A correction fixture that
        hardcodes the answer becomes a second source of the same drift."""
        brief = runner.compose_audit_brief(
            runner.get_slot("monthly-integrated"),
            date(2026, 8, 1),
            Path("/tmp/findings.md"),
            proposal_text=_FIXTURE_PROPOSAL,
        )
        self.assertNotIn("veneration", brief)


class TestEveryFiledTicketCarriesItsGoalMapping(unittest.TestCase):
    """T-2406: both filing paths pass --serves.

    Machine-filed tickets are the one place `serves` is unambiguously free to
    set — no author to interrupt, no judgment to make. The 2026-08-01 audit
    filed its own dispatch ticket (T-2385) through this path with no mapping,
    then found it unmapped. Asserted on BOTH paths because they are separate
    argv builders that have already drifted from each other once.
    """

    def test_dispatch_ticket_carries_serves(self) -> None:
        slot = runner.get_slot("weekly-narrow")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runner, "AUDIT_WORKSPACE_ROOT", Path(tmp)), \
             patch.object(runner, "_invoke_ticket_file") as mock_invoke, \
             patch.object(runner, "capture_runtime_snapshot"):
            mock_invoke.return_value = ("filed", "T-9999", "retries=0")
            runner.file_dispatch_ticket(slot, date(2026, 7, 6), Path("/tmp/f.md"))
        argv = mock_invoke.call_args[0][0]
        self.assertIn("--serves", argv)
        self.assertEqual(argv[argv.index("--serves") + 1], runner._SERVES)

    def test_finding_ticket_carries_serves(self) -> None:
        finding = runner.Finding(
            finding_id="S3STAR-X-001",
            title="t",
            severity="Medium",
            control_objective="co",
            observed_condition="oc",
            evidence="e",
            impact="i",
            reproduction="r",
            owner="orchestrator",
            recommended_outcome="ro",
            retest_criteria="rc",
            justification="j",
        )
        with patch.object(runner, "_invoke_ticket_file") as mock_invoke, \
             patch.object(runner, "authorship_flag", return_value=False):
            mock_invoke.return_value = ("filed", "T-9998", "retries=0")
            runner.file_finding(finding, "weekly-narrow", date(2026, 7, 6))
        argv = mock_invoke.call_args[0][0]
        self.assertIn("--serves", argv)
        self.assertEqual(argv[argv.index("--serves") + 1], runner._SERVES)

    def test_serves_names_a_registered_goal(self) -> None:
        """A mapping that points at nothing in rulebook/goals.md is a string, not
        a mapping — the failure this field exists to prevent, one level up."""
        goals = (Path(__file__).resolve().parent.parent / "rulebook" / "goals.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("pantheon (org system)", goals)
        self.assertIn("pantheon (org system)", runner._SERVES)


class TestCoverageParity(unittest.TestCase):
    """T-2405: deriving the COMMANDS from the manifest fixes the question asked;
    it does not prove the answer came back. These pin the second half."""

    def _results(self, **overrides):
        users = " ".join(f"uid=0({a}) gid=0({a})" for a in runner._SNAPSHOT_UNIX_USERS)
        roots = "\n".join(
            f"drwxrws--- 2 x y {runner._SNAPSHOT_MAILBOX_HUB / p}"
            for p in runner._SNAPSHOT_MAILBOX_AGENTS
        )
        bodies = {
            "identity-users": users,
            "ownership-named": roots,
            "identity-group": f"{runner._SNAPSHOT_MAILBOX_GROUP}:x:1006:claude",
        }
        bodies.update(overrides)
        return [
            (runner._SnapshotCommand(slug, [slug], slug=slug), body)
            for slug, body in bodies.items()
        ]

    def test_complete_bundle_is_ok(self) -> None:
        ok, lines = runner._coverage_parity(self._results())
        self.assertTrue(ok)
        self.assertTrue(any("OK" in line for line in lines))

    def test_missing_account_degrades(self) -> None:
        absent = runner._SNAPSHOT_UNIX_USERS[-1]
        users = " ".join(
            f"uid=0({a})" for a in runner._SNAPSHOT_UNIX_USERS if a != absent
        )
        ok, lines = runner._coverage_parity(self._results(**{"identity-users": users}))
        self.assertFalse(ok)
        self.assertIn(absent, "\n".join(lines))

    def test_missing_mailbox_root_degrades(self) -> None:
        ok, lines = runner._coverage_parity(self._results(**{"ownership-named": ""}))
        self.assertFalse(ok)
        self.assertIn("mailbox roots", "\n".join(lines))

    def test_failed_group_query_degrades(self) -> None:
        """The exact 2026-08-01 shape: `getent` exits 2, the runner records a
        WARNING body, and the bundle still looked complete."""
        ok, lines = runner._coverage_parity(
            self._results(**{"identity-group": "WARNING: exited 2"})
        )
        self.assertFalse(ok)
        self.assertIn(runner._SNAPSHOT_MAILBOX_GROUP, "\n".join(lines))

    def test_degraded_parity_is_visible_in_the_manifest_heading(self) -> None:
        """A gap recorded only in a body paragraph is a gap a reader skims past."""
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp) / "evidence"
            runner._write_evidence_bundle(
                evidence,
                self._results(**{"identity-group": "WARNING: exited 2"}),
                generated_at="2026-08-02T00:00:00Z",
            )
            text = (evidence / "MANIFEST.md").read_text(encoding="utf-8")
        self.assertIn("## Coverage parity vs instance.yaml — DEGRADED", text)


class FindOpenRetestTicketsAcrossStatesTests(unittest.TestCase):
    """T-2613: find_open_retest_tickets scans inbox/active/done, gating done/
    on the retest_status frontmatter field."""

    def _seed(self, tmp, state, ticket_id, *, retest_status=None):
        state_dir = Path(tmp) / state
        state_dir.mkdir(parents=True, exist_ok=True)
        text = _wrap_ticket_text(
            ticket_id, f"Finding for {ticket_id}",
            "s3star-audit-finding: weekly-narrow:2026-08-01:S3STAR-X-001\n\n"
            "Re-test criteria: Some criteria text.\n",
            state=state, retest_status=retest_status,
        )
        (state_dir / f"{ticket_id}.md").write_text(text, encoding="utf-8")

    def test_active_ticket_included_unconditionally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp, "active", "T-9101")  # no retest_status at all
            ids = {r.ticket_id for r in runner.find_open_retest_tickets(Path(tmp))}
        self.assertIn("T-9101", ids)

    def test_done_ticket_pending_included(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp, "done", "T-9102", retest_status="pending")
            ids = {r.ticket_id for r in runner.find_open_retest_tickets(Path(tmp))}
        self.assertIn("T-9102", ids)

    def test_done_ticket_missing_field_excluded(self) -> None:
        """Backward compat: pre-T-2613 done/ findings (T-1733/T-1734/T-1885
        shape) have no retest_status field at all and must NOT resurface."""
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp, "done", "T-9103")  # no retest_status
            ids = {r.ticket_id for r in runner.find_open_retest_tickets(Path(tmp))}
        self.assertNotIn("T-9103", ids)

    def test_done_ticket_verified_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp, "done", "T-9104", retest_status="verified")
            ids = {r.ticket_id for r in runner.find_open_retest_tickets(Path(tmp))}
        self.assertNotIn("T-9104", ids)

    def test_done_ticket_overridden_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp, "done", "T-9105", retest_status="overridden")
            ids = {r.ticket_id for r in runner.find_open_retest_tickets(Path(tmp))}
        self.assertNotIn("T-9105", ids)

    def test_inbox_ticket_verified_excluded(self) -> None:
        """T-3849 defect 2 repro shape: T-2795/T-2796 stayed in inbox/ past
        their PASS verdict (2026-09-13 governance-ambiguity), and the
        pre-fix code only ever gated on retest_status for done/ -- so a
        verified inbox finding kept getting re-enumerated as "open" every
        cycle."""
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp, "inbox", "T-9111", retest_status="verified")
            ids = {r.ticket_id for r in runner.find_open_retest_tickets(Path(tmp))}
        self.assertNotIn("T-9111", ids)

    def test_active_ticket_overridden_excluded(self) -> None:
        """Uniformity check: active/ is the other previously-ungated state."""
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp, "active", "T-9112", retest_status="overridden")
            ids = {r.ticket_id for r in runner.find_open_retest_tickets(Path(tmp))}
        self.assertNotIn("T-9112", ids)

    def test_inbox_ticket_pending_still_included(self) -> None:
        """The uniform closed-status gate must not swallow the normal
        still-open case: an explicit retest_status: pending in inbox/
        (e.g. written by a fail/partial re-verdict) stays routed."""
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp, "inbox", "T-9113", retest_status="pending")
            ids = {r.ticket_id for r in runner.find_open_retest_tickets(Path(tmp))}
        self.assertIn("T-9113", ids)

    def test_malformed_frontmatter_in_done_is_skipped_not_raised(self) -> None:
        """This scan runs inside an unattended brief-compose path — one bad
        ticket file must not blank the whole section (fail open)."""
        with tempfile.TemporaryDirectory() as tmp:
            done_dir = Path(tmp) / "done"
            done_dir.mkdir(parents=True)
            (done_dir / "T-9106.md").write_text(
                "s3star-audit-finding: weekly-narrow:2026-08-01:S3STAR-X-002\n\n"
                "Re-test criteria: unreachable — no front matter at all.\n",
                encoding="utf-8",
            )
            retests = runner.find_open_retest_tickets(Path(tmp))  # must not raise
        self.assertEqual([r for r in retests if r.ticket_id == "T-9106"], [])

    def test_inbox_ticket_authorship_flagged_field_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "inbox"
            state_dir.mkdir(parents=True)
            text = _wrap_ticket_text(
                "T-9107", "Flagged finding",
                "s3star-audit-finding: weekly-narrow:2026-08-01:S3STAR-X-003\n\n"
                "Re-test criteria: Some criteria text.\n",
                state="inbox",
                labels="[s3star-audit, audit-finding, authorship-flagged]",
            )
            (state_dir / "T-9107.md").write_text(text, encoding="utf-8")
            retests = runner.find_open_retest_tickets(Path(tmp))
        match = next(r for r in retests if r.ticket_id == "T-9107")
        self.assertTrue(match.authorship_flagged)

    def test_standard_finding_authorship_flagged_field_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp, "active", "T-9108")
            retests = runner.find_open_retest_tickets(Path(tmp))
        match = next(r for r in retests if r.ticket_id == "T-9108")
        self.assertFalse(match.authorship_flagged)

    def test_flagged_finding_with_unparseable_frontmatter_is_dropped_not_leaked(self) -> None:
        """R2 (T-2799 review): a finding whose frontmatter fails to parse
        must be excluded from enumeration ENTIRELY -- not defaulted to
        authorship_flagged=False -- because defaulting-unflagged on a
        parse failure would let a genuinely authorship-flagged finding
        (whose labels line we simply couldn't structurally read) leak into
        the Codex-facing brief as if it were standard.

        Uses an inbox/ ticket specifically: done/'s retest_status gate
        already excludes unparseable frontmatter via an unrelated
        pre-existing mechanism (test_malformed_frontmatter_in_done_is_
        skipped_not_raised above); inbox/active previously had NO
        frontmatter gate at all (T-1898's "always included, no field
        needed" contract), so this is the state where a leak was actually
        possible before find_open_retest_tickets was extended to parse
        frontmatter for every state.

        The corrupted fixture deliberately still contains the literal
        "authorship-flagged" substring in its raw bytes (in an otherwise
        frontmatter-less file) -- proving the drop is a genuine structural
        exclusion, not merely "we didn't happen to find the label".
        """
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            (inbox / "T-9110.md").write_text(
                "s3star-audit-finding: weekly-narrow:2026-08-01:S3STAR-X-004\n\n"
                "labels: [s3star-audit, audit-finding, authorship-flagged]\n\n"
                "Re-test criteria: unreachable — no front matter at all.\n",
                encoding="utf-8",
            )
            retests = runner.find_open_retest_tickets(Path(tmp))  # must not raise
            self.assertEqual([r for r in retests if r.ticket_id == "T-9110"], [])

            slot = runner.get_slot("weekly-narrow")
            brief = runner.compose_audit_brief(
                slot, date(2026, 8, 10), Path("/tmp/x.md"),
                proposal_text=_FIXTURE_PROPOSAL, tickets_root=Path(tmp),
            )
        self.assertNotIn("T-9110", brief)


class RecordRetestVerdictTests(unittest.TestCase):
    """T-2613: record_retest_verdict flips retest_status + appends a
    transition-log line. Mirrors TestCmdBlock's fixture pattern in
    test_ticket.py — same mutation primitives, same isolation shape."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket_mod.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket_mod, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._repo_patch = patch.object(ticket_mod, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._commit_patch = patch.object(ticket_mod, "_commit_and_push")
        self.mock_commit = self._commit_patch.start()
        self._pull_patch = patch.object(ticket_mod, "_pull_if_needed")
        self.mock_pull = self._pull_patch.start()

    def tearDown(self) -> None:
        self._pull_patch.stop()
        self._commit_patch.stop()
        self._repo_patch.stop()
        self._gu_repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _seed_finding(self, ticket_id, state="done", retest_status="pending") -> Path:
        path = self.root / state / f"{ticket_id}.md"
        path.write_text(
            _wrap_ticket_text(
                ticket_id, "Sample finding",
                "s3star-audit-finding: weekly-narrow:2026-08-01:S3STAR-X-003\n\n"
                "Re-test criteria: File mode is 0600.\n",
                state=state, retest_status=retest_status,
            ),
            encoding="utf-8",
        )
        return path

    def test_pass_verdict_sets_verified(self) -> None:
        path = self._seed_finding("T-9201")
        new_status = runner.record_retest_verdict(
            "T-9201", verdict="pass", note="fresh bundle confirms fix"
        )
        self.assertEqual(new_status, "verified")
        fields, body = ticket_mod.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["retest_status"], "verified")
        self.assertIn("retest-verdict-recorded", body)
        self.assertIn("pass: fresh bundle confirms fix", body)
        self.mock_commit.assert_called_once()

    def test_fail_verdict_keeps_pending(self) -> None:
        path = self._seed_finding("T-9202")
        new_status = runner.record_retest_verdict(
            "T-9202", verdict="fail", note="still reproduces"
        )
        self.assertEqual(new_status, "pending")
        fields, body = ticket_mod.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["retest_status"], "pending")
        self.assertIn("fail: still reproduces", body)

    def test_partial_verdict_keeps_pending(self) -> None:
        self._seed_finding("T-9203")
        new_status = runner.record_retest_verdict(
            "T-9203", verdict="partial", note="one of two criteria met"
        )
        self.assertEqual(new_status, "pending")

    def test_override_sets_overridden(self) -> None:
        path = self._seed_finding("T-9204")
        new_status = runner.record_retest_verdict(
            "T-9204", override=True, note="the operator call: accepting residual risk"
        )
        self.assertEqual(new_status, "overridden")
        fields, body = ticket_mod.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["retest_status"], "overridden")
        self.assertIn("override: the operator call: accepting residual risk", body)

    def test_missing_note_raises(self) -> None:
        self._seed_finding("T-9205")
        with self.assertRaises(ticket_mod.TicketError) as cm:
            runner.record_retest_verdict("T-9205", verdict="pass", note="")
        self.assertIn("--note is required", str(cm.exception))

    def test_both_verdict_and_override_raises(self) -> None:
        self._seed_finding("T-9206")
        with self.assertRaises(ticket_mod.TicketError):
            runner.record_retest_verdict(
                "T-9206", verdict="pass", override=True, note="x"
            )

    def test_neither_verdict_nor_override_raises(self) -> None:
        self._seed_finding("T-9207")
        with self.assertRaises(ticket_mod.TicketError):
            runner.record_retest_verdict("T-9207", note="x")

    def test_invalid_verdict_choice_raises(self) -> None:
        self._seed_finding("T-9208")
        with self.assertRaises(ticket_mod.TicketError):
            runner.record_retest_verdict("T-9208", verdict="maybe", note="x")

    def test_non_finding_ticket_refused(self) -> None:
        path = self.root / "done" / "T-9209.md"
        path.write_text(
            _wrap_ticket_text("T-9209", "Not a finding", "Just a normal ticket body.",
                               state="done"),
            encoding="utf-8",
        )
        with self.assertRaises(ticket_mod.TicketError) as cm:
            runner.record_retest_verdict("T-9209", verdict="pass", note="x")
        self.assertIn("not an s3star finding ticket", str(cm.exception))

    def test_fail_verdict_downgrades_from_verified(self) -> None:
        """T-3849 scenario (d): a fail/partial re-verdict on a prior PASS
        must still land -- downgrade is the whole point of independent
        re-test (a next-cycle FAIL must be able to re-open a prior PASS)."""
        path = self._seed_finding("T-9210", retest_status="verified")
        new_status = runner.record_retest_verdict(
            "T-9210", verdict="fail", note="regression reproduces again"
        )
        self.assertEqual(new_status, "pending")
        fields, body = ticket_mod.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["retest_status"], "pending")
        self.assertIn("fail: regression reproduces again", body)


class RecordRetestVerdictReplayGuardTests(unittest.TestCase):
    """T-3849 (remediates T-2795 Codex FAIL, defect 1): record_retest_verdict
    refuses to silently re-write a ticket that already carries a structural
    retest_status closure marker (verified/overridden), while the verified
    -> pending downgrade path (a next-cycle fail/partial re-verdict) stays
    legal. Same fixture shape as RecordRetestVerdictTests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket_mod.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket_mod, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._repo_patch = patch.object(ticket_mod, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._commit_patch = patch.object(ticket_mod, "_commit_and_push")
        self.mock_commit = self._commit_patch.start()
        self._pull_patch = patch.object(ticket_mod, "_pull_if_needed")
        self.mock_pull = self._pull_patch.start()

    def tearDown(self) -> None:
        self._pull_patch.stop()
        self._commit_patch.stop()
        self._repo_patch.stop()
        self._gu_repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _seed_finding(self, ticket_id, state="done", retest_status="pending") -> Path:
        path = self.root / state / f"{ticket_id}.md"
        path.write_text(
            _wrap_ticket_text(
                ticket_id, "Sample finding",
                "s3star-audit-finding: weekly-narrow:2026-08-01:S3STAR-X-003\n\n"
                "Re-test criteria: File mode is 0600.\n",
                state=state, retest_status=retest_status,
            ),
            encoding="utf-8",
        )
        return path

    def test_duplicate_pass_after_verified_refused(self) -> None:
        """T-3849 scenario (a), unit-level: the same PASS verdict landed
        twice on a ticket that's already verified must refuse the second
        call outright, independent of any caller-side open-set check."""
        path = self._seed_finding("T-9601", retest_status="verified")
        with self.assertRaises(ticket_mod.TicketError) as cm:
            runner.record_retest_verdict("T-9601", verdict="pass", note="re-landed")
        self.assertIn("already retest_status: verified", str(cm.exception))
        fields, body = ticket_mod.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["retest_status"], "verified")
        self.assertNotIn("re-landed", body)
        self.mock_commit.assert_not_called()

    def test_overridden_refuses_new_pass(self) -> None:
        path = self._seed_finding("T-9602", retest_status="overridden")
        with self.assertRaises(ticket_mod.TicketError) as cm:
            runner.record_retest_verdict("T-9602", verdict="pass", note="x")
        self.assertIn("already retest_status: overridden", str(cm.exception))
        fields, _ = ticket_mod.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["retest_status"], "overridden")
        self.mock_commit.assert_not_called()

    def test_overridden_refuses_new_fail(self) -> None:
        """overridden blocks EVERY subsequent call, not just pass -- it is
        a terminal administrative closure, not a re-test-verdict outcome."""
        self._seed_finding("T-9603", retest_status="overridden")
        with self.assertRaises(ticket_mod.TicketError):
            runner.record_retest_verdict("T-9603", verdict="fail", note="x")
        self.mock_commit.assert_not_called()

    def test_overridden_refuses_new_override(self) -> None:
        self._seed_finding("T-9604", retest_status="overridden")
        with self.assertRaises(ticket_mod.TicketError):
            runner.record_retest_verdict("T-9604", override=True, note="x")
        self.mock_commit.assert_not_called()

    def test_verified_can_still_be_overridden(self) -> None:
        """Scope boundary: only the pass-replay case is blocked on
        `verified` -- overriding an already-verified finding is a distinct,
        unblocked administrative action (not asked to be blocked by T-3849)."""
        path = self._seed_finding("T-9605", retest_status="verified")
        new_status = runner.record_retest_verdict(
            "T-9605", override=True, note="the operator call: override anyway"
        )
        self.assertEqual(new_status, "overridden")
        fields, _ = ticket_mod.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["retest_status"], "overridden")


class RecordRetestVerdictAuthorshipGateTests(unittest.TestCase):
    """T-2796: record_retest_verdict fails closed on an authorship-flagged
    finding from an ineligible principal, without mutating the ticket, and
    succeeds for an eligible claude-tier verifier (orchestrator/recovery). Same
    fixture shape as RecordRetestVerdictTests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket_mod.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket_mod, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._repo_patch = patch.object(ticket_mod, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._commit_patch = patch.object(ticket_mod, "_commit_and_push")
        self.mock_commit = self._commit_patch.start()
        self._pull_patch = patch.object(ticket_mod, "_pull_if_needed")
        self.mock_pull = self._pull_patch.start()

    def tearDown(self) -> None:
        self._pull_patch.stop()
        self._commit_patch.stop()
        self._repo_patch.stop()
        self._gu_repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _seed_flagged_finding(self, ticket_id) -> Path:
        path = self.root / "done" / f"{ticket_id}.md"
        path.write_text(
            _wrap_ticket_text(
                ticket_id, "Flagged finding",
                "s3star-audit-finding: weekly-narrow:2026-08-01:S3STAR-Y-001\n\n"
                "Re-test criteria: File mode is 0600.\n",
                state="done", retest_status="pending",
                labels="[s3star-audit, audit-finding, authorship-flagged]",
            ),
            encoding="utf-8",
        )
        return path

    def _seed_standard_finding(self, ticket_id) -> Path:
        path = self.root / "done" / f"{ticket_id}.md"
        path.write_text(
            _wrap_ticket_text(
                ticket_id, "Standard finding",
                "s3star-audit-finding: weekly-narrow:2026-08-01:S3STAR-Z-001\n\n"
                "Re-test criteria: File mode is 0600.\n",
                state="done", retest_status="pending",
            ),
            encoding="utf-8",
        )
        return path

    def test_codex_principal_refused_pass(self) -> None:
        path = self._seed_flagged_finding("T-9501")
        with self.assertRaises(ticket_mod.TicketError) as cm:
            runner.record_retest_verdict(
                "T-9501", verdict="pass", note="codex says fixed", principal="codex"
            )
        self.assertIn("authorship-flagged", str(cm.exception))
        fields, _ = ticket_mod.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["retest_status"], "pending")
        self.mock_commit.assert_not_called()

    def test_codex_principal_refused_fail(self) -> None:
        self._seed_flagged_finding("T-9502")
        with self.assertRaises(ticket_mod.TicketError):
            runner.record_retest_verdict(
                "T-9502", verdict="fail", note="codex says broken", principal="codex"
            )
        self.mock_commit.assert_not_called()

    def test_codex_principal_refused_partial(self) -> None:
        self._seed_flagged_finding("T-9503")
        with self.assertRaises(ticket_mod.TicketError):
            runner.record_retest_verdict(
                "T-9503", verdict="partial", note="codex says half-fixed", principal="codex"
            )
        self.mock_commit.assert_not_called()

    def test_codex_principal_refused_override(self) -> None:
        self._seed_flagged_finding("T-9504")
        with self.assertRaises(ticket_mod.TicketError):
            runner.record_retest_verdict(
                "T-9504", override=True, note="codex override attempt", principal="codex"
            )
        self.mock_commit.assert_not_called()

    def test_eligible_verifier_orchestrator_succeeds(self) -> None:
        self._seed_flagged_finding("T-9505")
        new_status = runner.record_retest_verdict(
            "T-9505", verdict="pass", note="orchestrator verified independently", principal="orchestrator"
        )
        self.assertEqual(new_status, "verified")

    def test_eligible_verifier_recovery_succeeds(self) -> None:
        self._seed_flagged_finding("T-9506")
        new_status = runner.record_retest_verdict(
            "T-9506", verdict="fail", note="recovery verified independently", principal="recovery"
        )
        self.assertEqual(new_status, "pending")

    def test_default_principal_derived_from_actor_env(self) -> None:
        # No explicit principal kwarg: derives from PANTHEON_ACTOR, mirroring
        # T-2795's own reproduction command (PANTHEON_ACTOR=codex ...
        # --record-retest-verdict).
        self._seed_flagged_finding("T-9507")
        with patch.dict(os.environ, {"PANTHEON_ACTOR": "codex"}):
            with self.assertRaises(ticket_mod.TicketError):
                runner.record_retest_verdict("T-9507", verdict="pass", note="x")

    def test_default_principal_orchestrator_from_actor_env_succeeds(self) -> None:
        self._seed_flagged_finding("T-9509")
        with patch.dict(os.environ, {"PANTHEON_ACTOR": "orchestrator"}):
            new_status = runner.record_retest_verdict(
                "T-9509", verdict="pass", note="orchestrator verified"
            )
        self.assertEqual(new_status, "verified")

    def test_standard_finding_unaffected_by_gate(self) -> None:
        # Non-flagged findings: any principal, including codex, still works
        # -- the gate applies ONLY to the authorship-flagged subset.
        self._seed_standard_finding("T-9508")
        new_status = runner.record_retest_verdict(
            "T-9508", verdict="pass", note="ok", principal="codex"
        )
        self.assertEqual(new_status, "verified")


class LandVerdictsTests(unittest.TestCase):
    """T-2799 (remediates T-2795): --land-verdicts reads a workspace
    verdicts.jsonl artifact and lands each valid entry via
    record_retest_verdict. Same fixture shape as RecordRetestVerdictTests
    -- land_verdicts calls the real mutation-library path per entry."""

    def setUp(self) -> None:
        # `cwd` mirrors production's REPO_ROOT; tickets live under
        # `cwd/tickets/{state}` -- same relationship as the real
        # TICKETS_ROOT = REPO_ROOT / "tickets" (ticket.py) -- since
        # land_verdicts derives its find_open_retest_tickets root as
        # `cwd / "tickets"` (matching land_findings' own convention) while
        # record_retest_verdict resolves tickets via the patched
        # ticket_mod.TICKETS_ROOT global. Both must point at the same
        # on-disk directory for the two to agree.
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tickets_root = self.root / "tickets"
        for s in ticket_mod.STATES:
            (self.tickets_root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket_mod, "TICKETS_ROOT", self.tickets_root)
        self._tickets_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._repo_patch = patch.object(ticket_mod, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._commit_patch = patch.object(ticket_mod, "_commit_and_push")
        self.mock_commit = self._commit_patch.start()
        self._pull_patch = patch.object(ticket_mod, "_pull_if_needed")
        self.mock_pull = self._pull_patch.start()

    def tearDown(self) -> None:
        self._pull_patch.stop()
        self._commit_patch.stop()
        self._repo_patch.stop()
        self._gu_repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _seed_finding(self, ticket_id, *, flagged=False, state="done") -> Path:
        labels = "[s3star-audit, audit-finding, severity-medium]"
        if flagged:
            labels = "[s3star-audit, audit-finding, severity-medium, authorship-flagged]"
        path = self.tickets_root / state / f"{ticket_id}.md"
        path.write_text(
            _wrap_ticket_text(
                ticket_id, "Sample finding",
                "s3star-audit-finding: weekly-narrow:2026-08-01:S3STAR-X-003\n\n"
                "Re-test criteria: File mode is 0600.\n",
                state=state, retest_status="pending", labels=labels,
            ),
            encoding="utf-8",
        )
        return path

    def _verdicts_path(self, *entries) -> Path:
        p = self.root / "verdicts.jsonl"
        p.write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
        )
        return p

    def test_lands_pass_fail_partial_atomically_preserving_notes(self) -> None:
        self._seed_finding("T-9401")
        self._seed_finding("T-9402")
        self._seed_finding("T-9403")
        vp = self._verdicts_path(
            {"ticket": "T-9401", "verdict": "pass", "evidence_note": "fixed"},
            {"ticket": "T-9402", "verdict": "fail", "evidence_note": "still broken"},
            {"ticket": "T-9403", "verdict": "partial", "evidence_note": "half fixed"},
        )
        rc = runner.land_verdicts("weekly-narrow", vp, date(2026, 8, 10), cwd=self.root)
        self.assertEqual(rc, 0)
        f1, _ = ticket_mod.parse_front_matter((self.tickets_root / "done" / "T-9401.md").read_text())
        self.assertEqual(f1["retest_status"], "verified")
        f2, b2 = ticket_mod.parse_front_matter((self.tickets_root / "done" / "T-9402.md").read_text())
        self.assertEqual(f2["retest_status"], "pending")
        self.assertIn("fail: still broken", b2)
        f3, b3 = ticket_mod.parse_front_matter((self.tickets_root / "done" / "T-9403.md").read_text())
        self.assertEqual(f3["retest_status"], "pending")
        self.assertIn("partial: half fixed", b3)
        self.assertEqual(self.mock_commit.call_count, 3)  # each landed separately/atomically

    def test_removes_only_pass_from_open_routing(self) -> None:
        self._seed_finding("T-9404")
        self._seed_finding("T-9405")
        vp = self._verdicts_path(
            {"ticket": "T-9404", "verdict": "pass", "evidence_note": "fixed"},
            {"ticket": "T-9405", "verdict": "fail", "evidence_note": "nope"},
        )
        runner.land_verdicts("weekly-narrow", vp, date(2026, 8, 10), cwd=self.root)
        ids = {r.ticket_id for r in runner.find_open_retest_tickets(self.tickets_root)}
        self.assertNotIn("T-9404", ids)
        self.assertIn("T-9405", ids)

    def test_rejects_forged_ticket_not_in_open_set(self) -> None:
        self._seed_finding("T-9406")
        vp = self._verdicts_path(
            {"ticket": "T-9999", "verdict": "pass", "evidence_note": "forged"},
        )
        rc = runner.land_verdicts("weekly-narrow", vp, date(2026, 8, 10), cwd=self.root)
        self.assertEqual(rc, 1)
        self.mock_commit.assert_not_called()

    def test_rejects_duplicate_ticket_entries_neither_lands(self) -> None:
        self._seed_finding("T-9407")
        vp = self._verdicts_path(
            {"ticket": "T-9407", "verdict": "pass", "evidence_note": "a"},
            {"ticket": "T-9407", "verdict": "fail", "evidence_note": "b"},
        )
        rc = runner.land_verdicts("weekly-narrow", vp, date(2026, 8, 10), cwd=self.root)
        self.assertEqual(rc, 1)
        fields, _ = ticket_mod.parse_front_matter(
            (self.tickets_root / "done" / "T-9407.md").read_text()
        )
        self.assertEqual(fields["retest_status"], "pending")  # untouched
        self.mock_commit.assert_not_called()

    def test_requires_nonempty_evidence_note(self) -> None:
        self._seed_finding("T-9408")
        vp = self._verdicts_path(
            {"ticket": "T-9408", "verdict": "pass", "evidence_note": ""},
        )
        rc = runner.land_verdicts("weekly-narrow", vp, date(2026, 8, 10), cwd=self.root)
        self.assertEqual(rc, 1)
        self.mock_commit.assert_not_called()

    def test_missing_artifact_file_refused(self) -> None:
        rc = runner.land_verdicts(
            "weekly-narrow", self.root / "nope.jsonl", date(2026, 8, 10), cwd=self.root
        )
        self.assertEqual(rc, 1)

    def test_authorship_flagged_ineligible_principal_fails_closed_stays_routed(self) -> None:
        self._seed_finding("T-9409", flagged=True)
        vp = self._verdicts_path(
            {"ticket": "T-9409", "verdict": "pass", "evidence_note": "codex says fixed"},
        )
        rc = runner.land_verdicts("weekly-narrow", vp, date(2026, 8, 10), cwd=self.root)
        self.assertEqual(rc, 1)
        fields, _ = ticket_mod.parse_front_matter(
            (self.tickets_root / "done" / "T-9409.md").read_text()
        )
        self.assertEqual(fields["retest_status"], "pending")  # untouched
        ids = {r.ticket_id for r in runner.find_open_retest_tickets(self.tickets_root)}
        self.assertIn("T-9409", ids)  # never dropped from routing
        self.mock_commit.assert_not_called()

    def test_second_invocation_replays_same_artifact_refused(self) -> None:
        """T-3849 scenario (a), end-to-end repro shape: the same 2026-09-07
        workspace PASS artifact was landed twice, 24s apart (T-2795
        transitions @4e50d8cf04 and @a6e8b4c33a), and both invocations
        succeeded. Landing the identical verdicts-artifact object a second
        time must now refuse -- only the first invocation lands.

        Seeded in inbox/ specifically (not done/): that's where T-2795/
        T-2796 actually sat past their PASS verdict (2026-09-13
        governance-ambiguity), and where the pre-fix find_open_retest_
        tickets never gated on retest_status at all -- a done/-seeded
        ticket would already refuse the replay via done/'s pre-existing
        gate, without exercising this fix."""
        self._seed_finding("T-9411", state="inbox")
        vp = self._verdicts_path(
            {"ticket": "T-9411", "verdict": "pass", "evidence_note": "fixed"},
        )
        rc1 = runner.land_verdicts("weekly-narrow", vp, date(2026, 8, 10), cwd=self.root)
        self.assertEqual(rc1, 0)
        rc2 = runner.land_verdicts("weekly-narrow", vp, date(2026, 8, 10), cwd=self.root)
        self.assertEqual(rc2, 1)
        fields, _ = ticket_mod.parse_front_matter(
            (self.tickets_root / "inbox" / "T-9411.md").read_text()
        )
        self.assertEqual(fields["retest_status"], "verified")  # unchanged by 2nd call
        self.assertEqual(self.mock_commit.call_count, 1)  # only the first landed

    def test_no_network_dependency_in_land_verdicts_round_trip(self) -> None:
        self._seed_finding("T-9410")
        vp = self._verdicts_path(
            {"ticket": "T-9410", "verdict": "pass", "evidence_note": "fixed"},
        )
        with patch("socket.socket", side_effect=AssertionError("network attempted")):
            rc = runner.land_verdicts("weekly-narrow", vp, date(2026, 8, 10), cwd=self.root)
        self.assertEqual(rc, 0)

    def test_mixed_batch_lands_valid_entries_refuses_invalid_ones(self) -> None:
        self._seed_finding("T-9411")
        self._seed_finding("T-9412")
        vp = self._verdicts_path(
            {"ticket": "T-9411", "verdict": "pass", "evidence_note": "fixed"},
            {"ticket": "T-9998", "verdict": "pass", "evidence_note": "forged"},
        )
        rc = runner.land_verdicts("weekly-narrow", vp, date(2026, 8, 10), cwd=self.root)
        self.assertEqual(rc, 1)  # any refusal -> nonzero, even with a real landing
        f1, _ = ticket_mod.parse_front_matter((self.tickets_root / "done" / "T-9411.md").read_text())
        self.assertEqual(f1["retest_status"], "verified")  # still landed
        f2, _ = ticket_mod.parse_front_matter((self.tickets_root / "done" / "T-9412.md").read_text())
        self.assertEqual(f2["retest_status"], "pending")  # untouched, wasn't in this artifact


class ParseVerdictsArtifactTests(unittest.TestCase):
    def test_parses_well_formed_lines(self) -> None:
        text = (
            '{"ticket": "T-1001", "verdict": "pass", "evidence_note": "ok"}\n'
            '{"ticket": "T-1002", "verdict": "fail", "evidence_note": "no"}\n'
        )
        entries = runner.parse_verdicts_artifact(text)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].ticket_id, "T-1001")
        self.assertEqual(entries[0].verdict, "pass")
        self.assertEqual(entries[0].evidence_note, "ok")

    def test_skips_malformed_json_line(self) -> None:
        text = 'not json at all\n{"ticket": "T-1003", "verdict": "pass", "evidence_note": "ok"}\n'
        entries = runner.parse_verdicts_artifact(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].ticket_id, "T-1003")

    def test_skips_line_missing_required_key(self) -> None:
        text = '{"ticket": "T-1004", "verdict": "pass"}\n'  # no evidence_note
        self.assertEqual(runner.parse_verdicts_artifact(text), [])

    def test_skips_line_with_invalid_verdict(self) -> None:
        text = '{"ticket": "T-1005", "verdict": "maybe", "evidence_note": "x"}\n'
        self.assertEqual(runner.parse_verdicts_artifact(text), [])

    def test_skips_blank_lines(self) -> None:
        text = '\n\n{"ticket": "T-1006", "verdict": "pass", "evidence_note": "ok"}\n\n'
        entries = runner.parse_verdicts_artifact(text)
        self.assertEqual(len(entries), 1)

    def test_empty_text_returns_empty_list(self) -> None:
        self.assertEqual(runner.parse_verdicts_artifact(""), [])


class IsolatedAuditorArtifactChannelTests(unittest.TestCase):
    """T-2795: proves the workspace-artifact verdict channel operates
    correctly WITHIN the permission boundary the isolated auditor's
    profile enforces (T-1598, unchanged by this ticket) -- writable
    workspace, but no canonical-repo write and no mailbox write.

    Scoping caveat: the real profile (bwrap / s3star-audit-p2 codex
    config) is external to this repo and not re-tested here -- this
    fixture reproduces its file-permission SHAPE using real chmod bits
    (a stand-in, not a re-test of the external mechanism), to prove the
    NEW artifact-channel code added by this ticket behaves correctly
    within that boundary.
    """

    def setUp(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("permission bits do not restrict root")
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.canonical_repo = self.root / "canonical_repo"
        self.mailbox = self.root / "mailbox" / "orchestrator"
        self.workspace = self.root / "workspace"
        self.canonical_repo.mkdir(parents=True)
        self.mailbox.mkdir(parents=True)
        self.workspace.mkdir(parents=True)
        os.chmod(self.canonical_repo, 0o500)
        os.chmod(self.mailbox, 0o500)
        os.chmod(self.workspace, 0o770)

    def tearDown(self) -> None:
        os.chmod(self.canonical_repo, 0o700)
        os.chmod(self.mailbox, 0o700)
        self._tmp.cleanup()

    def test_auditor_cannot_write_canonical_repo(self) -> None:
        with self.assertRaises(PermissionError):
            (self.canonical_repo / "verdicts.jsonl").write_text("x", encoding="utf-8")

    def test_auditor_cannot_write_mailbox(self) -> None:
        with self.assertRaises(PermissionError):
            (self.mailbox / "msg.txt").write_text("x", encoding="utf-8")

    def test_auditor_can_write_workspace_verdicts_artifact(self) -> None:
        target = self.workspace / "verdicts.jsonl"
        target.write_text(
            json.dumps({"ticket": "T-1", "verdict": "pass", "evidence_note": "ok"}) + "\n",
            encoding="utf-8",
        )
        self.assertTrue(target.exists())

    def test_workspace_artifact_round_trips_with_zero_network_calls(self) -> None:
        target = self.workspace / "verdicts.jsonl"
        target.write_text(
            json.dumps({"ticket": "T-1", "verdict": "pass", "evidence_note": "ok"}) + "\n",
            encoding="utf-8",
        )
        with patch("socket.socket", side_effect=AssertionError("network attempted")):
            entries = runner.parse_verdicts_artifact(target.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].ticket_id, "T-1")


class OpenRetestsLifecycleTests(unittest.TestCase):
    """T-2613: end-to-end shape of the gap this ticket closes — a finding
    that is remediated-and-resolved-to-done/ WITHOUT an independent verdict
    stays in Open re-tests; only a recorded verdict removes it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket_mod.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket_mod, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._repo_patch = patch.object(ticket_mod, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._commit_patch = patch.object(ticket_mod, "_commit_and_push")
        self._commit_patch.start()
        self._pull_patch = patch.object(ticket_mod, "_pull_if_needed")
        self._pull_patch.start()

    def tearDown(self) -> None:
        self._pull_patch.stop()
        self._commit_patch.stop()
        self._repo_patch.stop()
        self._gu_repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _file_then_remediate_to_done(self, ticket_id) -> None:
        """Mirrors the production shape without a live subprocess: file_finding
        (via ticket.py `file --retest-status pending`) writes inbox/, then a
        domain-owner claim+resolve moves it to done/ WITHOUT touching
        retest_status — exactly like cmd_claim/cmd_resolve, which never
        reference that field."""
        finding = runner.Finding(
            finding_id="F-1", title="Lifecycle finding", severity="Medium",
            control_objective="c", observed_condition="o", evidence="e",
            impact="i", reproduction="r", owner="recovery",
            recommended_outcome="ro", retest_criteria="File mode is 0600.",
            justification="j",
        )
        _, context = runner.build_finding_ticket_bodies(
            finding, "weekly-narrow", date(2026, 8, 1), flagged=False
        )
        text = _wrap_ticket_text(
            ticket_id, f"S3* audit finding F-1: {finding.title}", context,
            state="inbox", retest_status="pending",
        )
        (self.root / "inbox" / f"{ticket_id}.md").write_text(text, encoding="utf-8")
        # claim + resolve: move file, no retest_status change (mirrors
        # cmd_claim/cmd_resolve, which never touch this field).
        (self.root / "active" / f"{ticket_id}.md").write_text(text, encoding="utf-8")
        (self.root / "inbox" / f"{ticket_id}.md").unlink()
        done_path = self.root / "done" / f"{ticket_id}.md"
        done_path.write_text(text.replace("state: inbox", "state: done"), encoding="utf-8")
        (self.root / "active" / f"{ticket_id}.md").unlink()

    def test_remediated_not_verified_stays_open(self) -> None:
        self._file_then_remediate_to_done("T-9301")
        retests = runner.find_open_retest_tickets(self.root)
        ids = {r.ticket_id for r in retests}
        self.assertIn("T-9301", ids)
        match = next(r for r in retests if r.ticket_id == "T-9301")
        self.assertEqual(match.criteria, "File mode is 0600.")

    def test_pass_verdict_closes_it_out_of_routing(self) -> None:
        self._file_then_remediate_to_done("T-9302")
        # Confirm it starts open (mirrors the prior test's assertion).
        self.assertIn(
            "T-9302",
            {r.ticket_id for r in runner.find_open_retest_tickets(self.root)},
        )
        runner.record_retest_verdict(
            "T-9302", verdict="pass", note="fresh independent bundle confirms fix"
        )
        ids_after = {r.ticket_id for r in runner.find_open_retest_tickets(self.root)}
        self.assertNotIn("T-9302", ids_after)

    def test_fail_verdict_keeps_it_open(self) -> None:
        self._file_then_remediate_to_done("T-9303")
        runner.record_retest_verdict("T-9303", verdict="fail", note="still reproduces")
        ids_after = {r.ticket_id for r in runner.find_open_retest_tickets(self.root)}
        self.assertIn("T-9303", ids_after)


class LiveRepoOpenRetestsRoutingTests(unittest.TestCase):
    """T-2613: real on-disk tickets/ must route the currently-known
    remediated-but-unverified S3* findings into Open re-tests. Exercised
    against real repository state (same convention as
    test_compose_audit_brief_against_real_proposal above), not a fixture."""

    def test_remediated_unverified_findings_enumerate(self) -> None:
        retests = runner.find_open_retest_tickets(runner.REPO_ROOT / "tickets")
        ids = {r.ticket_id for r in retests}
        for tid in ("T-2406", "T-2407"):
            self.assertIn(
                tid, ids,
                f"{tid} should route into Open re-tests (remediated, not yet "
                f"independently re-verified per its own transition log)",
            )


class WorkspaceRootOffTheAncestor(unittest.TestCase):
    """T-5514: the audit workspace moves off /home/the-ancestor before that account is
    deleted. The runner, the seat-evidence writer and the root-installed tmux wrapper
    must name the SAME root (the wrapper refuses any run_dir outside it), and it must
    not live under the-ancestor."""

    def _wrapper_default(self) -> str:
        text = (runner.REPO_ROOT / "tools" / "sudoers.d" / "pantheon-s3star-tmux").read_text()
        m = re.search(r'WORKSPACE_ROOT="\$\{S3STAR_WORKSPACE_ROOT:-([^}]+)\}"', text)
        self.assertIsNotNone(m, "wrapper default WORKSPACE_ROOT not found")
        return m.group(1)

    def test_all_three_name_the_same_root(self):
        from tools import s3star_seat_evidence as seat_evidence
        roots = {str(runner.AUDIT_WORKSPACE_ROOT), str(seat_evidence.WORKSPACE_ROOT), self._wrapper_default()}
        self.assertEqual(len(roots), 1, roots)

    def test_root_is_not_under_the_ancestor(self):
        self.assertEqual(str(runner.AUDIT_WORKSPACE_ROOT), "/var/lib/pantheon/s3star-workspace")
        self.assertNotIn("the-ancestor", self._wrapper_default())


if __name__ == "__main__":
    unittest.main()
