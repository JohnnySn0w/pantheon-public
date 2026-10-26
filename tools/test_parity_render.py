"""test_parity_render.py — T-3747 BT2.

Fixture-driven: every test builds its own tmp-dir axes.yaml / carve-outs.yaml /
instance.yaml rather than touching the live sop/standard/seat-parity/ manifests
or the live tools/parity_checks/ build-dir (T-1978-style discipline — never
smoke-test lifecycle/build tooling against the live tree).
"""
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parity_render as pr  # noqa: E402


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


AXES_YAML = """\
    schema_version: 1
    manifest_version: 1
    axes:
      - axis_id: unix-account-linger-enabled
        tier: HARD
        description: "linger enabled"
        applies_to: all-seats
        source_of_truth: sop/standard/pantheon-bringup.md
        check_method: systemd_query
        check_ref: "loginctl show-user <account> --property=Linger"
        gap_history: []
      - axis_id: ticket-persona-registered
        tier: HARD
        description: "registered"
        applies_to: all-seats
        source_of_truth: pantheon-config/instance.yaml
        check_method: ticket_lookup
        check_ref: "python3 -c 'assert \\"<persona>\\" == \\"orchestrator\\"'"
        gap_history: []
      - axis_id: permissions-allow-populated
        tier: HARD
        description: "permissions populated"
        applies_to: all-seats
        source_of_truth: sop/standard/pantheon-bringup.md
        check_method: file_grep
        check_ref: 'python3 -c "print(True)"'
        gap_history: []
      - axis_id: monitor-context-fill
        tier: SOFT
        description: "context fill monitor"
        applies_to: all-seats
        source_of_truth: sop/standard/pantheon-bringup.md
        check_method: systemd_query
        check_ref: "systemctl --user list-timers pantheon-context-fill-monitor@<persona>.timer"
        gap_history: []
      - axis_id: prose-only-axis
        tier: SOFT
        description: "a check_ref that is prose, not a command"
        applies_to: all-seats
        source_of_truth: sop/standard/pantheon-bringup.md
        check_method: canary
        check_ref: "log of successful reply-tool invocation; no operator-approval trace"
        gap_history: []
      - axis_id: role-scoped-axis
        tier: SOFT
        description: "only applies to one role"
        applies_to: [orchestrator]
        source_of_truth: sop/standard/pantheon-bringup.md
        check_method: file_grep
        check_ref: 'python3 -c "print(True)"'
        gap_history: []
      - axis_id: harness-cli-installed-authed
        tier: HARD
        description: "already sudo-wrapped"
        applies_to: all-seats
        source_of_truth: sop/standard/pantheon-bringup.md
        check_method: file_grep
        check_ref: "$(sudo -u <account> which claude) OR $(sudo -u <account> which codex)"
        gap_history: []
"""

CARVE_OUTS_YAML = """\
    schema_version: 1
    manifest_version: 1
    carve_outs:
      - seat: the-ancestor
        axis: monitor-context-fill
        verdict: intentional-absence
        rationale: "no context resets for the-ancestor"
        ratifier: operator-msg-2936
        ratified_date: 2026-06-09
        verifier: "systemctl --user list-unit-files 'pantheon-context-fill-monitor@the-ancestor.timer' returns nothing"
        review_trigger: "n/a"
      - seat: archivist
        axis: permissions-allow-populated
        verdict: intentional-variant
        rationale: "instance path assertion example"
        ratifier: ORG-042
        ratified_date: 2026-07-19
        verifier: "instance.yaml roles.second-head.billing_model == 'api-key'"
        review_trigger: "n/a"
      - seat: recovery
        axis: unix-account-linger-enabled
        verdict: migration-in-progress
        rationale: "ticket state assertion example"
        ratifier: T-2953
        ratified_date: 2026-08-14
        verifier: "T-9999 state == inbox or active"
        review_trigger: "n/a"
      - seat: consultant
        axis: unix-account-linger-enabled
        verdict: intentional-variant
        rationale: "compound verifier that must not partially match"
        ratifier: sop-section-12
        ratified_date: 2026-06-09
        verifier: "instance.yaml roles.peer-daemon-codex.harness == 'codex' + codex-supervisor unit present per section-12"
        review_trigger: "n/a"
"""

INSTANCE_YAML = """\
    manifest_version: 2
    roles:
      orchestrator:
        persona: orchestrator
        account: claude
        harness: claude-code
        domain: org orchestration
      second-head:
        persona: archivist
        account: exocortex
        harness: claude-code
        domain: second head
      recovery:
        persona: recovery
        account: claude-fallback
        harness: claude-code
        domain: recovery runbook
      peer-daemon-codex:
        persona: codex
        account: codex
        harness: codex
        domain: independent auditor
      peer-daemon-the-ancestor:
        persona: the-ancestor
        account: the-ancestor
        harness: claude-code
        domain: ancestor-veneration substrate
"""


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.axes_path = write(root / "axes.yaml", AXES_YAML)
        self.carve_outs_path = write(root / "carve-outs.yaml", CARVE_OUTS_YAML)
        self.instance_path = write(root / "instance.yaml", INSTANCE_YAML)


class TestLoadAxes(FixtureCase):
    def test_parses_all_required_fields(self):
        axes = pr.load_axes(self.axes_path)
        by_id = {a.axis_id: a for a in axes}
        self.assertEqual(by_id["unix-account-linger-enabled"].tier, "HARD")
        self.assertEqual(by_id["unix-account-linger-enabled"].check_method, "systemd_query")
        self.assertEqual(by_id["role-scoped-axis"].applies_to, ("orchestrator",))
        self.assertEqual(by_id["unix-account-linger-enabled"].applies_to, "all-seats")

    def test_skips_malformed_axis_with_warning(self):
        root = Path(self.tmp.name)
        bad = write(root / "bad-axes.yaml", """\
            schema_version: 1
            axes:
              - axis_id: ok-axis
                tier: HARD
                description: "fine"
                applies_to: all-seats
                source_of_truth: x
                check_method: file_grep
                check_ref: "true"
              - axis_id: missing-check-ref
                tier: HARD
                description: "missing check_ref field"
                applies_to: all-seats
                source_of_truth: x
                check_method: file_grep
        """)
        axes = pr.load_axes(bad)
        self.assertEqual([a.axis_id for a in axes], ["ok-axis"])

    def test_schema_version_mismatch_is_soft_warn_not_crash(self):
        root = Path(self.tmp.name)
        p = write(root / "v2-axes.yaml", """\
            schema_version: 2
            axes:
              - axis_id: ok-axis
                tier: HARD
                description: "fine"
                applies_to: all-seats
                source_of_truth: x
                check_method: file_grep
                check_ref: "true"
        """)
        axes = pr.load_axes(p)
        self.assertEqual(len(axes), 1)


class TestLoadCarveOuts(FixtureCase):
    def test_parses_schema(self):
        cos = pr.load_carve_outs(self.carve_outs_path)
        self.assertEqual(len(cos), 4)
        self.assertEqual(cos[0].seat, "the-ancestor")
        self.assertEqual(cos[0].axis, "monitor-context-fill")


class TestLoadSeats(FixtureCase):
    def test_extracts_persona_account_harness_domain(self):
        seats = pr.load_seats(self.instance_path)
        by_persona = {s.persona: s for s in seats}
        self.assertEqual(by_persona["orchestrator"].account, "claude")
        self.assertEqual(by_persona["orchestrator"].role_id, "orchestrator")
        self.assertEqual(by_persona["codex"].harness, "codex")
        self.assertEqual(len(seats), 5)


class TestLiveAxesResolve(unittest.TestCase):
    """Every applies_to id in the live axes.yaml names a current role id or persona.

    A stale id (e.g. the pre-ORG-100 'orchestrator') matches no seat, so the axis
    silently audits nobody and reads as passing (T-4730 triage finding).
    """

    def test_every_applies_to_entry_resolves_to_a_live_seat(self):
        seats = pr.load_seats()
        known = {s.role_id for s in seats} | {s.persona for s in seats}
        stale = sorted(
            (a.axis_id, rid)
            for a in pr.load_axes()
            if a.applies_to != "all-seats"
            for rid in a.applies_to
            if rid not in known
        )
        self.assertEqual(stale, [], f"applies_to ids that match no seat: {stale}")


class TestAppliesTo(unittest.TestCase):
    def test_all_seats_matches_any_seat(self):
        axis = pr.Axis("a", "HARD", "d", "all-seats", "s", "file_grep", "true")
        seat = pr.SeatBinding("orchestrator", "orchestrator", "claude")
        self.assertTrue(pr.axis_applies_to_seat(axis, seat))

    def test_role_list_filters_by_role_id_or_persona(self):
        axis = pr.Axis("a", "SOFT", "d", ("orchestrator",), "s", "file_grep", "true")
        orchestrator = pr.SeatBinding("orchestrator", "orchestrator", "claude")
        recovery = pr.SeatBinding("recovery", "recovery", "claude-fallback")
        self.assertTrue(pr.axis_applies_to_seat(axis, orchestrator))
        self.assertFalse(pr.axis_applies_to_seat(axis, recovery))


class TestRenderCommand(unittest.TestCase):
    def test_substitutes_account_and_persona(self):
        seat = pr.SeatBinding("orchestrator", "orchestrator", "claude")
        out = pr.substitute_placeholders("loginctl show-user <account> for <persona>", seat)
        self.assertEqual(out, "loginctl show-user claude for orchestrator")

    def test_substitutes_role(self):
        # G-k fix (T-3751 tranche 2): <role> substitution added for instance.yaml
        # roles-block lookup axes (billing-model-declared, domain-scope-declared).
        seat = pr.SeatBinding("orchestrator", "orchestrator", "claude")
        out = pr.substitute_placeholders('d.get("roles",{}).get("<role>",{})', seat)
        self.assertEqual(out, 'd.get("roles",{}).get("orchestrator",{})')

    def test_substitutes_all_three_placeholders_together(self):
        seat = pr.SeatBinding("specialist", "specialist", "claude-research")
        out = pr.substitute_placeholders("<persona>@<account> as <role>", seat)
        self.assertEqual(out, "specialist@claude-research as specialist")

    def test_looks_like_shell_command_rejects_known_prose(self):
        prose_examples = [
            "grep for X in Y; per persona's declared MCP set",
            "log of successful reply-tool invocation from persona's session; no operator-approval trace",
            "sudo -u <account> python3 -c 'import mailbox_mcp; ...' — first-send exercise",
            "stat /home/the-ancestor/mailbox/<persona>/ + getfacl check",
            "systemctl status pantheon-runtime-controller@<persona>.service (or documented pre-HC equivalent)",
        ]
        for text in prose_examples:
            with self.subTest(text=text):
                self.assertFalse(pr.looks_like_shell_command(text))

    def test_looks_like_shell_command_accepts_literal_refs(self):
        literal_examples = [
            "loginctl show-user <account> --property=Linger",
            "python3 -c 'from tools.agent_role_map import AGENT_ROLE_MAP; assert \"<persona>\" in AGENT_ROLE_MAP'",
            "sudo -u <account> pip show mcp | grep Version",
        ]
        for text in literal_examples:
            with self.subTest(text=text):
                self.assertTrue(pr.looks_like_shell_command(text))

    def test_needs_sudo_wrap_true_when_not_embedded_and_touches_account(self):
        axis = pr.Axis("a", "HARD", "d", "all-seats", "s", "systemd_query",
                        "loginctl show-user <account> --property=Linger")
        seat = pr.SeatBinding("recovery", "recovery", "claude-fallback")
        self.assertEqual(pr.resolve_sudo_account(axis, seat), "claude-fallback")

    def test_needs_sudo_wrap_false_when_check_ref_already_embeds_sudo(self):
        axis = pr.Axis("a", "HARD", "d", "all-seats", "s", "file_grep",
                        "$(sudo -u <account> which claude) OR $(sudo -u <account> which codex)")
        seat = pr.SeatBinding("recovery", "recovery", "claude-fallback")
        self.assertIsNone(pr.resolve_sudo_account(axis, seat))

    def test_needs_sudo_wrap_false_for_ticket_lookup(self):
        axis = pr.Axis("a", "HARD", "d", "all-seats", "s", "ticket_lookup",
                        "python3 -c 'assert \"<persona>\" in X'")
        seat = pr.SeatBinding("recovery", "recovery", "claude-fallback")
        self.assertIsNone(pr.resolve_sudo_account(axis, seat))

    def test_stale_check_wraps_to_seat_account(self):
        # T-4532: check_stale.py lives under /home/<account>, and a 0750 home
        # (suborch2) denies the auditor's uid -> UNMEASURABLE. Run it as the seat.
        axis = pr.Axis("persona-orientation-rendered-fresh", "SOFT", "d", "all-seats", "s",
                        "stale_check",
                        "python3 /home/<account>/pantheon-config/tools/persona_orientation/check_stale.py")
        seat = pr.SeatBinding("engineering-orchestrator", "artisan", "suborch2")
        self.assertEqual(pr.resolve_sudo_account(axis, seat), "suborch2")

    def test_probe_on_auditor_overrides_default_seat_wrap(self):
        # T-3766 tranche 3: probe_on: auditor MUST return None (run as auditor)
        # even for check_methods that otherwise default-wrap to seat.account.
        # Regression guard: recovery-run central-monitor axes (context-fill,
        # activity) rely on this to probe recovery's systemd instead of the seat's.
        axis = pr.Axis("m", "SOFT", "d", "all-seats", "s", "systemd_query",
                        "systemctl --user is-active foo@<persona>.timer",
                        probe_on="auditor")
        seat = pr.SeatBinding("orchestrator", "orchestrator", "claude")
        self.assertIsNone(pr.resolve_sudo_account(axis, seat))

    def test_probe_on_seat_default_preserves_wrap(self):
        # Complement to the above: default probe_on='seat' MUST still wrap to
        # seat.account for the standard cases (backward-compat).
        axis = pr.Axis("m", "SOFT", "d", "all-seats", "s", "systemd_query",
                        "systemctl --user is-enabled foo.timer")
        seat = pr.SeatBinding("orchestrator", "orchestrator", "claude")
        self.assertEqual(pr.resolve_sudo_account(axis, seat), "claude")


class FakeRunner(pr.Runner):
    def __init__(self, responses=None, fail_if_called=False):
        self.responses = responses or {}
        self.calls = []
        self.fail_if_called = fail_if_called

    def execute(self, command, sudo_account, timeout=30):
        if self.fail_if_called:
            raise AssertionError(f"Runner.execute must not be called in dry-run: {command!r}")
        self.calls.append((command, sudo_account))
        for pattern, result in self.responses.items():
            if pattern in command:
                return result
        return pr.ExecResult(rc=0, stdout="", stderr="")


class TestClassifyOutcome(unittest.TestCase):
    def test_rc127_is_unmeasurable_malformed(self):
        v, ev = pr.classify_outcome("file_grep", pr.ExecResult(127, "", "bash: presence: command not found"))
        self.assertEqual(v, "UNMEASURABLE")
        self.assertIn("127", ev)

    def test_timeout_rc_none_is_unmeasurable(self):
        v, _ = pr.classify_outcome("canary", pr.ExecResult(None, "", ""))
        self.assertEqual(v, "UNMEASURABLE")

    def test_boolean_stdout_true_is_pass(self):
        v, _ = pr.classify_outcome("file_grep", pr.ExecResult(0, "True\n", ""))
        self.assertEqual(v, "PASS")

    def test_boolean_stdout_false_is_fail(self):
        v, _ = pr.classify_outcome("file_grep", pr.ExecResult(0, "False\n", ""))
        self.assertEqual(v, "FAIL")

    def test_ticket_lookup_assertion_error_is_fail(self):
        v, _ = pr.classify_outcome("ticket_lookup", pr.ExecResult(1, "", "AssertionError\n"))
        self.assertEqual(v, "FAIL")

    def test_ticket_lookup_import_error_is_unmeasurable_not_fail(self):
        v, ev = pr.classify_outcome(
            "ticket_lookup", pr.ExecResult(1, "", "ModuleNotFoundError: No module named 'tools.ticket2'"))
        self.assertEqual(v, "UNMEASURABLE")
        self.assertIn("unexpected error", ev)

    def test_file_grep_file_not_found_is_fail(self):
        v, _ = pr.classify_outcome("file_grep", pr.ExecResult(1, "", "FileNotFoundError: [Errno 2] ..."))
        self.assertEqual(v, "FAIL")

    def test_file_grep_permission_denied_is_unmeasurable(self):
        v, _ = pr.classify_outcome("file_grep", pr.ExecResult(1, "", "PermissionError: Permission denied"))
        self.assertEqual(v, "UNMEASURABLE")

    def test_systemd_query_never_auto_passes_from_rc0(self):
        v, _ = pr.classify_outcome("systemd_query", pr.ExecResult(0, "0 timers listed.\n", ""))
        self.assertEqual(v, "UNMEASURABLE")

    def test_canary_never_auto_passes_from_rc0(self):
        v, _ = pr.classify_outcome("canary", pr.ExecResult(0, "some output", ""))
        self.assertEqual(v, "UNMEASURABLE")

    def test_stale_check_rc0_fresh_is_pass(self):
        v, _ = pr.classify_outcome("stale_check", pr.ExecResult(0, "OK -- 28 source binding(s) fresh.\n", ""))
        self.assertEqual(v, "PASS")

    def test_stale_check_rc2_stale_is_fail_not_unmeasurable(self):
        # T-3813: check_stale.py prints "STALE -- N of M drifted" to STDERR + exits 2.
        # This MUST classify FAIL (real drift), not UNMEASURABLE. The regression witness
        # below shows the OLD `file_grep` labelling buried the exact same input as
        # UNMEASURABLE (its "unrecognized failure shape" fallback) — the bug this fixes.
        stale = pr.ExecResult(2, "", "STALE -- 2 of 28 source binding(s) drifted:\n  reviewer-methodologies.md")
        v, ev = pr.classify_outcome("stale_check", stale)
        self.assertEqual(v, "FAIL")
        self.assertIn("STALE", ev)
        self.assertEqual(pr.classify_outcome("file_grep", stale)[0], "UNMEASURABLE")

    def test_stale_check_rc2_missing_bindings_is_fail(self):
        v, _ = pr.classify_outcome("stale_check", pr.ExecResult(2, "", "source-bindings.yaml missing"))
        self.assertEqual(v, "FAIL")

    def test_stale_check_permission_denied_is_unmeasurable(self):
        v, _ = pr.classify_outcome("stale_check", pr.ExecResult(1, "", "PermissionError: Permission denied"))
        self.assertEqual(v, "UNMEASURABLE")

    def test_stale_check_unexpected_rc_is_unmeasurable(self):
        # a broken invocation (traceback, rc=1) must not read as PASS or FAIL
        v, _ = pr.classify_outcome("stale_check", pr.ExecResult(1, "", "Traceback (most recent call last): ValueError"))
        self.assertEqual(v, "UNMEASURABLE")

    def test_default_fallback_unrecognized_method_alarms_unconditional(self):
        # G-l (T-3751): unrecognized check_method always → UNMEASURABLE regardless
        # of rc. Trusting rc==0 as PASS on an unhandled method was a silent-green
        # seam per ORG-099 §8; adding a new axis with a novel check_method now
        # requires adding an explicit classify branch above.
        self.assertEqual(pr.classify_outcome("gh_api", pr.ExecResult(0, "", ""))[0], "UNMEASURABLE")
        self.assertEqual(pr.classify_outcome("gh_api", pr.ExecResult(1, "", ""))[0], "UNMEASURABLE")
        self.assertEqual(pr.classify_outcome("gh_api", pr.ExecResult(3, "", ""))[0], "UNMEASURABLE")


class TestCarveOutMatching(FixtureCase):
    def test_finds_declared_carve_out(self):
        cos = pr.load_carve_outs(self.carve_outs_path)
        seat = pr.SeatBinding("peer-daemon-the-ancestor", "the-ancestor", "the-ancestor")
        found = pr.find_carve_out(seat, "monitor-context-fill", cos)
        self.assertIsNotNone(found)
        self.assertEqual(found.ratifier, "operator-msg-2936")

    def test_returns_none_when_undeclared(self):
        cos = pr.load_carve_outs(self.carve_outs_path)
        seat = pr.SeatBinding("orchestrator", "orchestrator", "claude")
        self.assertIsNone(pr.find_carve_out(seat, "monitor-context-fill", cos))

    def test_consultant_never_matches_live_codex_persona(self):
        """G-d: carve-outs.yaml names the codex seat 'consultant'; the live persona is 'codex'."""
        cos = pr.load_carve_outs(self.carve_outs_path)
        codex_seat = pr.SeatBinding("peer-daemon-codex", "codex", "codex")
        self.assertIsNone(pr.find_carve_out(codex_seat, "unix-account-linger-enabled", cos))


class TestVerifyCarveOut(FixtureCase):
    def setUp(self):
        super().setUp()
        self.instance_raw = pr._load_yaml(self.instance_path)
        self.seats_by_persona = {s.persona: s for s in pr.load_seats(self.instance_path)}
        self.cos = {co.seat: co for co in pr.load_carve_outs(self.carve_outs_path)}

    def test_dry_run_never_executes_and_is_unmeasurable(self):
        runner = FakeRunner(fail_if_called=True)
        v, ev = pr.verify_carve_out(self.cos["the-ancestor"], self.instance_raw, runner,
                                     self.seats_by_persona, execute=False)
        self.assertEqual(v, "UNMEASURABLE")
        self.assertIn("dry-run", ev)

    def test_instance_yaml_path_assertion_pass(self):
        runner = FakeRunner()
        v, ev = pr.verify_carve_out(self.cos["archivist"], self.instance_raw, runner,
                                     self.seats_by_persona, execute=True)
        # fixture instance.yaml has no billing_model field at all -> BROKEN, not VERIFIED
        self.assertEqual(v, "CARVE_OUT_BROKEN")
        self.assertIn("does not exist", ev)

    def test_instance_yaml_path_assertion_pass_when_field_present(self):
        root = Path(self.tmp.name)
        inst = write(root / "instance2.yaml", """\
            roles:
              second-head:
                persona: archivist
                account: exocortex
                billing_model: api-key
        """)
        raw = pr._load_yaml(inst)
        runner = FakeRunner()
        v, ev = pr.verify_carve_out(self.cos["archivist"], raw, runner, self.seats_by_persona, execute=True)
        self.assertEqual(v, "CARVE_OUT_VERIFIED")

    def test_ticket_state_assertion_unmeasurable_when_ticket_absent(self):
        runner = FakeRunner()
        v, ev = pr.verify_carve_out(self.cos["recovery"], self.instance_raw, runner,
                                     self.seats_by_persona, execute=True)
        self.assertEqual(v, "UNMEASURABLE")
        self.assertIn("not found", ev)

    def test_compound_verifier_does_not_partial_match(self):
        """The load-bearing fullmatch test: consultant's verifier has real content past the
        instance.yaml clause; a search()-based match would wrongly claim VERIFIED/BROKEN
        on half the assertion. It must fall through to the unrecognized-shape branch."""
        runner = FakeRunner()
        v, ev = pr.verify_carve_out(self.cos["consultant"], self.instance_raw, runner,
                                     self.seats_by_persona, execute=True)
        self.assertEqual(v, "UNMEASURABLE")
        self.assertIn("not match a recognized structural shape", ev)

    def test_systemd_absence_verified_when_empty(self):
        runner = FakeRunner(responses={"list-unit-files": pr.ExecResult(0, "", "")})
        v, ev = pr.verify_carve_out(self.cos["the-ancestor"], self.instance_raw, runner,
                                     self.seats_by_persona, execute=True)
        self.assertEqual(v, "CARVE_OUT_VERIFIED")

    def test_systemd_absence_broken_when_present(self):
        runner = FakeRunner(responses={
            "list-unit-files": pr.ExecResult(0, "pantheon-context-fill-monitor@the-ancestor.timer enabled\n", "")})
        v, ev = pr.verify_carve_out(self.cos["the-ancestor"], self.instance_raw, runner,
                                     self.seats_by_persona, execute=True)
        self.assertEqual(v, "CARVE_OUT_BROKEN")


class TestEvaluateSeat(FixtureCase):
    def setUp(self):
        super().setUp()
        self.axes = pr.load_axes(self.axes_path)
        self.carve_outs = pr.load_carve_outs(self.carve_outs_path)
        self.instance_raw = pr._load_yaml(self.instance_path)
        self.seats_by_persona = {s.persona: s for s in pr.load_seats(self.instance_path)}

    def test_dry_run_never_invokes_runner(self):
        runner = FakeRunner(fail_if_called=True)
        seat = self.seats_by_persona["orchestrator"]
        report = pr.evaluate_seat(seat, self.axes, self.carve_outs, self.instance_raw,
                                   self.seats_by_persona, runner, execute=False)
        self.assertTrue(all(o.verdict == "UNMEASURABLE" for o in report.outcomes))

    def test_aggregates_to_broken_on_hard_fail(self):
        runner = FakeRunner(responses={"assert": pr.ExecResult(1, "", "AssertionError")})
        seat = self.seats_by_persona["orchestrator"]
        report = pr.evaluate_seat(seat, self.axes, self.carve_outs, self.instance_raw,
                                   self.seats_by_persona, runner, execute=True)
        self.assertEqual(report.state, "BROKEN")

    def test_aggregates_to_full_when_clean(self):
        axes = [a for a in self.axes if a.tier == "HARD" and a.axis_id == "permissions-allow-populated"]
        runner = FakeRunner()
        seat = self.seats_by_persona["orchestrator"]
        report = pr.evaluate_seat(seat, axes, [], self.instance_raw, self.seats_by_persona,
                                   runner, execute=True)
        self.assertEqual(report.state, "FULL")

    def test_aggregates_to_partial_on_soft_lag_only(self):
        hard_ok = next(a for a in self.axes if a.axis_id == "permissions-allow-populated")
        soft_axis = pr.Axis("soft-lag", "SOFT", "d", "all-seats", "s", "gh_api", "false")
        runner = FakeRunner(responses={"false": pr.ExecResult(1, "", "")})
        seat = self.seats_by_persona["orchestrator"]
        report = pr.evaluate_seat(seat, [hard_ok, soft_axis], [], self.instance_raw,
                                   self.seats_by_persona, runner, execute=True)
        self.assertEqual(report.state, "PARTIAL")

    def test_carve_out_verified_counts_as_passing_for_hard_axis(self):
        axes = [a for a in self.axes if a.axis_id == "monitor-context-fill"]
        cos = [co for co in self.carve_outs if co.seat == "the-ancestor"]
        runner = FakeRunner(responses={"list-unit-files": pr.ExecResult(0, "", "")})
        seat = pr.SeatBinding("peer-daemon-the-ancestor", "the-ancestor", "the-ancestor")
        report = pr.evaluate_seat(seat, axes, cos, self.instance_raw, self.seats_by_persona,
                                   runner, execute=True)
        self.assertEqual(report.state, "FULL")


class TestRenderOutputs(FixtureCase):
    def setUp(self):
        super().setUp()
        self.axes = pr.load_axes(self.axes_path)
        self.carve_outs = pr.load_carve_outs(self.carve_outs_path)
        self.seats_by_persona = {s.persona: s for s in pr.load_seats(self.instance_path)}

    def test_seat_script_contains_run_as_wrapper_and_axis_comments(self):
        seat = self.seats_by_persona["orchestrator"]
        text = pr.render_seat_script(seat, self.axes, self.carve_outs)
        self.assertIn("run_as()", text)
        self.assertIn("axis: unix-account-linger-enabled", text)
        self.assertIn("NOT RENDERABLE", text)  # prose-only-axis

    def test_bash_single_quote_round_trips_through_real_bash(self):
        """ticket-persona-registered's check_ref is itself a python3 -c '...' invocation;
        naive f"'{command}'" wrapping would break the generated bash line. Round-trip the
        escaped text through a real bash -c printf to prove it survives, not just eyeball it."""
        import subprocess
        original = """python3 -c 'from tools.agent_role_map import AGENT_ROLE_MAP; assert "orchestrator" in AGENT_ROLE_MAP'"""
        quoted = pr._bash_single_quote(original)
        result = subprocess.run(["bash", "-c", f"printf %s {quoted}"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, original)

    def test_seat_script_documents_carve_out_not_reimplements_it(self):
        seat = pr.SeatBinding("second-head", "archivist", "exocortex")
        text = pr.render_seat_script(seat, self.axes, self.carve_outs)
        self.assertIn("carve-out declared", text)

    def test_fleet_table_md_has_seat_rows_and_axis_columns(self):
        runner = FakeRunner()
        instance_raw = pr._load_yaml(self.instance_path)
        seats = list(self.seats_by_persona.values())
        reports = [pr.evaluate_seat(s, self.axes, self.carve_outs, instance_raw,
                                     self.seats_by_persona, runner, execute=False) for s in seats]
        table = pr.render_fleet_table_md(reports, self.axes)
        self.assertIn("| seat |", table)
        for axis in self.axes:
            self.assertIn(axis.axis_id, table)
        for seat in seats:
            self.assertIn(seat.persona, table)

    def test_envelope_shape_matches_t2372_fields(self):
        outcome = pr.CheckOutcome("some-axis", "orchestrator", "PASS", "2026-09-16T00:00:00+00:00", "ok")
        self.assertEqual(set(outcome.to_envelope().keys()),
                          {"axis_id", "seat", "verdict", "timestamp", "evidence"})


class TestCLI(FixtureCase):
    def test_seat_dry_run_prints_script_without_executing(self):
        argv = ["--seat", "orchestrator", "--dry-run",
                "--axes-path", str(self.axes_path),
                "--carve-outs-path", str(self.carve_outs_path),
                "--instance-path", str(self.instance_path)]
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = pr.main(argv)
        self.assertEqual(rc, 0)
        self.assertIn("run_as()", buf.getvalue())

    def test_seat_unknown_returns_error_code(self):
        argv = ["--seat", "nobody", "--dry-run",
                "--axes-path", str(self.axes_path),
                "--carve-outs-path", str(self.carve_outs_path),
                "--instance-path", str(self.instance_path)]
        self.assertEqual(pr.main(argv), 2)

    def test_fleet_dry_run_format_json(self):
        argv = ["--fleet", "--dry-run", "--format", "json",
                "--axes-path", str(self.axes_path),
                "--carve-outs-path", str(self.carve_outs_path),
                "--instance-path", str(self.instance_path)]
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = pr.main(argv)
        payload = json.loads(buf.getvalue())
        self.assertIn("seats", payload)
        self.assertTrue(len(payload["seats"]) == 5)
        # A dry run executed nothing; every seat aggregates to BROKEN by construction
        # (HARD axes all read UNMEASURABLE), but that's a preview artifact, not a
        # verdict — exit code must stay 0, mirroring --seat's dry-run convention.
        self.assertEqual(rc, 0)
        self.assertTrue(all(s["state"] == "BROKEN" for s in payload["seats"]))


class TestAgainstLiveManifest(unittest.TestCase):
    """One integration check against the REAL repo manifests (read-only, --dry-run
    only — never executes a check) so a schema break in the live axes.yaml/carve-outs.yaml/
    instance.yaml surfaces here rather than only at first live invocation."""

    def test_live_manifests_load_without_raising(self):
        axes = pr.load_axes()
        carve_outs = pr.load_carve_outs()
        seats = pr.load_seats()
        self.assertGreater(len(axes), 0)
        self.assertGreater(len(carve_outs), 0)
        self.assertGreater(len(seats), 0)

    def test_live_seat_dry_run_orchestrator_never_executes(self):
        runner = FakeRunner(fail_if_called=True)
        axes = pr.load_axes()
        carve_outs = pr.load_carve_outs()
        seats = pr.load_seats()
        seats_by_persona = {s.persona: s for s in seats}
        instance_raw = pr._load_yaml(pr.DEFAULT_INSTANCE_PATH)
        orchestrator = seats_by_persona.get("orchestrator")
        if orchestrator is None:
            self.skipTest("orchestrator not present in live instance.yaml")
        report = pr.evaluate_seat(orchestrator, axes, carve_outs, instance_raw, seats_by_persona,
                                   runner, execute=False)
        self.assertTrue(all(o.verdict == "UNMEASURABLE" for o in report.outcomes))


def _strict(obj):
    """Type-tagged canonical form: equal iff same values, types and key order."""
    if isinstance(obj, dict):
        return ("dict", tuple((_strict(k), _strict(v)) for k, v in obj.items()))
    if isinstance(obj, list):
        return ("list", tuple(_strict(v) for v in obj))
    return (type(obj).__name__, repr(obj))


class TestLoadYamlParseRoutingT4298(unittest.TestCase):
    """T-4298: _load_yaml parses via agent_role_map.load_instance_document (content-
    keyed cache, CSafeLoader, deep copy). Pins the PRE-routing contract: OSError ->
    ManifestError; yaml.YAMLError / UnicodeDecodeError propagate unwrapped; empty ->
    {}; non-mapping -> ManifestError; an edit between calls is observed (even a
    same-size one with the mtime restored); the returned dict is private (mutating
    it cannot leak into the next call)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "instance.yaml"

    def test_missing_and_unreadable_raise_manifest_error(self):
        with self.assertRaisesRegex(pr.ManifestError, "cannot read"):
            pr._load_yaml(self.path)
        self.path.mkdir()
        with self.assertRaisesRegex(pr.ManifestError, "cannot read"):
            pr._load_yaml(self.path)

    def test_malformed_yaml_propagates_yaml_error_unwrapped(self):
        import yaml
        self.path.write_text("roles: [1, 2\n", encoding="utf-8")
        with self.assertRaises(yaml.YAMLError) as cm:
            pr._load_yaml(self.path)
        self.assertNotIsInstance(cm.exception, pr.ManifestError)

    def test_invalid_utf8_propagates_unicode_decode_error(self):
        self.path.write_bytes(b"roles: \xff\xfe\n")
        with self.assertRaises(UnicodeDecodeError):
            pr._load_yaml(self.path)

    def test_empty_is_empty_mapping_and_non_mapping_raises(self):
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(pr._load_yaml(self.path), {})
        self.path.write_text("- a\n- b\n", encoding="utf-8")
        with self.assertRaisesRegex(pr.ManifestError, "expected a YAML mapping"):
            pr._load_yaml(self.path)

    def test_edit_between_calls_observed_same_size_same_mtime(self):
        self.path.write_text("roles:\n  r:\n    persona: aaaa\n    account: x\n", encoding="utf-8")
        st = self.path.stat()
        self.assertEqual([s.persona for s in pr.load_seats(self.path)], ["aaaa"])
        self.path.write_text("roles:\n  r:\n    persona: bbbb\n    account: x\n", encoding="utf-8")
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(self.path.stat().st_size, st.st_size)
        self.assertEqual([s.persona for s in pr.load_seats(self.path)], ["bbbb"])

    def test_returned_mapping_is_private(self):
        self.path.write_text("roles:\n  r:\n    persona: p\n    account: x\n", encoding="utf-8")
        first = pr._load_yaml(self.path)
        first["roles"]["r"]["persona"] = "MUTATED"
        first["injected"] = True
        second = pr._load_yaml(self.path)
        self.assertEqual(second, {"roles": {"r": {"persona": "p", "account": "x"}}})

    def test_routed_through_cache_not_safe_load(self):
        from unittest import mock
        import yaml
        self.path.write_text("roles: {}\n", encoding="utf-8")
        with mock.patch.object(yaml, "safe_load",
                               side_effect=AssertionError("safe_load must not be called")):
            self.assertEqual(pr._load_yaml(self.path), {"roles": {}})

    def test_live_seat_parity_manifests_parse_identically_under_both_loaders(self):
        # _load_yaml also reads axes.yaml / carve-outs.yaml, which now go through
        # CSafeLoader too: prove parity on the REAL files, type-strict.
        import yaml
        if getattr(yaml, "CSafeLoader", None) is None:
            self.skipTest("PyYAML built without libyaml: no CSafeLoader")
        for path in (pr.DEFAULT_AXES_PATH, pr.DEFAULT_CARVE_OUTS_PATH, pr.DEFAULT_INSTANCE_PATH):
            with self.subTest(path=path.name):
                with open(path) as fh:
                    old = yaml.safe_load(fh) or {}
                new = pr._load_yaml(path)
                self.assertEqual(_strict(new), _strict(old))


if __name__ == "__main__":
    unittest.main()
