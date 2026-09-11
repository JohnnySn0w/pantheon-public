"""Tests for the candidate response-capture adapter (T-3461).

Pure unit tests -- no live tmux/subprocess environment, no Arc/FakeWorld
fixture (that apparatus belongs to test_continuity_switch_coordinator.py's
state-machine tests). Every dependency `default_communicate` needs (submit,
capture_pane, sleep, nonce_factory) is injected, matching the ports-style
dependency-injection convention used throughout this coordinator bundle.

Run: python3 -m unittest tools.test_continuity_response_capture -v
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from tools import continuity_response_capture as rc
from tools import runtime_controller
from tools.harness_adapter.types import (
    FailoverPolicy,
    HarnessLaunchConfig,
    RuntimeControllerConfig,
    TmuxTarget,
)


def _handle() -> "runtime_controller.RuntimeHandle":
    from datetime import datetime, timezone

    launch = HarnessLaunchConfig(
        harness="claude", profile="interactive", executable=Path("/usr/bin/claude"),
        model=None, mode="interactive", meter="subscription", quota_source="unavailable",
        native={}, sources=(), writable_roots=(),
    )
    return runtime_controller.RuntimeHandle(
        profile=launch, tmux=TmuxTarget(session="s", window="0", pane="0"),
        session_id="sess", started_at=datetime.now(timezone.utc),
    )


def _receipt(evidence: dict | None = None) -> "runtime_controller.DeliveryReceipt":
    return runtime_controller.DeliveryReceipt(
        harness="claude-code", correlation_id="", state="accepted",
        mechanism="tmux-submit-verify", retryable=False,
        evidence=evidence or {"tmux_target": "s:0.0", "transport_verified": "true"},
    )


# --------------------------------------------------------------------------
# build_self_report_prompt
# --------------------------------------------------------------------------


class BuildSelfReportPromptTests(unittest.TestCase):
    def test_embeds_intent_and_nonce_markers(self) -> None:
        prompt = rc.build_self_report_prompt("identify yourself", "selfreport-deadbeef")
        self.assertIn("identify yourself", prompt)
        self.assertIn(f"{rc._BEGIN_PREFIX}selfreport-deadbeef", prompt)
        self.assertIn(f"{rc._END_PREFIX}selfreport-deadbeef", prompt)


# --------------------------------------------------------------------------
# extract_self_report
# --------------------------------------------------------------------------


class ExtractSelfReportTests(unittest.TestCase):
    def _pane(self, nonce: str, body: str) -> str:
        return f"some prior output\n{rc._BEGIN_PREFIX}{nonce} {body} {rc._END_PREFIX}{nonce}\nmore output\n"

    def test_happy_path_parses_full_json(self) -> None:
        body = '{"role_id": "repo:role.json", "capability_evidence_complete": true}'
        parsed = rc.extract_self_report(self._pane("nonce-1", body), "nonce-1")
        self.assertEqual(parsed, {"role_id": "repo:role.json", "capability_evidence_complete": True})

    def test_silence_returns_none(self) -> None:
        self.assertIsNone(rc.extract_self_report("nothing here at all", "nonce-1"))
        self.assertIsNone(rc.extract_self_report("", "nonce-1"))

    def test_wrong_nonce_returns_none(self) -> None:
        """Adversarial-content-injection: a stale/foreign block tagged with a
        DIFFERENT nonce (e.g. leftover visible-pane content from a prior
        turn) must never be mistaken for the current response."""
        pane = self._pane("stale-nonce", '{"role_id": "repo:role.json"}')
        self.assertIsNone(rc.extract_self_report(pane, "current-nonce"))

    def test_malformed_json_returns_none(self) -> None:
        pane = self._pane("nonce-1", "{not valid json")
        self.assertIsNone(rc.extract_self_report(pane, "nonce-1"))

    def test_non_object_json_returns_none(self) -> None:
        pane = self._pane("nonce-1", "[1, 2, 3]")
        self.assertIsNone(rc.extract_self_report(pane, "nonce-1"))
        pane_scalar = self._pane("nonce-1", '"just a string"')
        self.assertIsNone(rc.extract_self_report(pane_scalar, "nonce-1"))

    def test_oversized_match_returns_none(self) -> None:
        oversized_body = "{" + "x" * (rc.MAX_SELF_REPORT_BYTES + 10) + "}"
        pane = self._pane("nonce-1", oversized_body)
        self.assertIsNone(rc.extract_self_report(pane, "nonce-1"))

    def test_wrapped_multiline_pane_text_still_parses(self) -> None:
        """A hard-wrapped terminal capture can split a logical line across
        several rows -- the join-then-search must still find it."""
        nonce = "nonce-1"
        body = '{"role_id": "repo:role.json"}'
        pane = f"{rc._BEGIN_PREFIX}{nonce}\n {body}\n {rc._END_PREFIX}{nonce}\n"
        parsed = rc.extract_self_report(pane, nonce)
        self.assertEqual(parsed, {"role_id": "repo:role.json"})


# --------------------------------------------------------------------------
# candidate_source_revisions_from_payload
# --------------------------------------------------------------------------


class CandidateSourceRevisionsFromPayloadTests(unittest.TestCase):
    def test_happy_path(self) -> None:
        raw = [{"uri": "repo:a.json", "source_revision": "rev1", "source_sha256": "a" * 64}]
        result = rc.candidate_source_revisions_from_payload(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].uri, "repo:a.json")
        self.assertEqual(result[0].source_revision, "rev1")
        self.assertEqual(result[0].source_sha256, "a" * 64)

    def test_absence_defaults_to_sentinel(self) -> None:
        self.assertEqual(rc.candidate_source_revisions_from_payload(None), rc._UNIDENTIFIED_SOURCE_REVISIONS)

    def test_empty_list_defaults_to_sentinel(self) -> None:
        self.assertEqual(rc.candidate_source_revisions_from_payload([]), rc._UNIDENTIFIED_SOURCE_REVISIONS)

    def test_wrong_type_defaults_to_sentinel(self) -> None:
        self.assertEqual(rc.candidate_source_revisions_from_payload("not-a-list"), rc._UNIDENTIFIED_SOURCE_REVISIONS)

    def test_sentinel_is_never_empty(self) -> None:
        self.assertGreater(len(rc._UNIDENTIFIED_SOURCE_REVISIONS), 0)

    def test_malformed_entries_degrade_without_crashing(self) -> None:
        result = rc.candidate_source_revisions_from_payload(["not-a-dict", 42, None])
        self.assertEqual(len(result), 3)
        for item in result:
            self.assertEqual(item.uri, "")
            self.assertEqual(item.source_revision, "")
            self.assertEqual(item.source_sha256, "")


# --------------------------------------------------------------------------
# capture_pane_joined
# --------------------------------------------------------------------------


class CapturePaneJoinedTests(unittest.TestCase):
    def test_returns_empty_on_tmux_failure(self) -> None:
        target = TmuxTarget(session="s", window="0", pane="0")
        with mock.patch.object(rc.subprocess, "run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=1, stdout="")
            self.assertEqual(rc.capture_pane_joined(target), "")

    def test_returns_stdout_on_success(self) -> None:
        target = TmuxTarget(session="s", window="0", pane="0")
        with mock.patch.object(rc.subprocess, "run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="pane contents")
            self.assertEqual(rc.capture_pane_joined(target), "pane contents")


# --------------------------------------------------------------------------
# default_communicate
# --------------------------------------------------------------------------


class DefaultCommunicateTests(unittest.TestCase):
    def test_happy_path_parses_self_report(self) -> None:
        handle = _handle()
        captured_nonces: list[str] = []

        def fake_submit(h, request, *, state_root):
            return _receipt()

        def fake_capture_pane(target):
            nonce = captured_nonces[0]
            body = (
                '{"role_id": "repo:role.json", "communication_correlation_id": "corr-abc"}'
            )
            return f"{rc._BEGIN_PREFIX}{nonce} {body} {rc._END_PREFIX}{nonce}"

        def fake_nonce_factory():
            nonce = "selfreport-fixed"
            captured_nonces.append(nonce)
            return nonce

        outcome = rc.default_communicate(
            handle, "identify yourself",
            repo_root=Path("/tmp"), state_root=Path("/tmp/state"),
            submit=fake_submit, capture_pane=fake_capture_pane,
            attempts=3, poll_interval_seconds=0.0, sleep=lambda s: None,
            nonce_factory=fake_nonce_factory,
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.structured_payload, {"role_id": "repo:role.json", "communication_correlation_id": "corr-abc"})
        self.assertEqual(outcome.correlation_id, "corr-abc")

    def test_silence_returns_empty_payload_ok_true(self) -> None:
        handle = _handle()
        sleeps: list[float] = []

        outcome = rc.default_communicate(
            handle, "identify yourself",
            repo_root=Path("/tmp"), state_root=Path("/tmp/state"),
            submit=lambda h, request, *, state_root: _receipt(),
            capture_pane=lambda target: "no marker here",
            attempts=3, poll_interval_seconds=0.0, sleep=sleeps.append,
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.structured_payload, {})
        self.assertTrue(outcome.correlation_id)  # fresh id, non-empty
        self.assertEqual(len(sleeps), 2)  # slept between attempts 1->2 and 2->3, not after the last

    def test_transport_failure_returns_ok_false(self) -> None:
        handle = _handle()

        def raising_submit(h, request, *, state_root):
            raise RuntimeError("tmux send-keys failed")

        outcome = rc.default_communicate(
            handle, "identify yourself",
            repo_root=Path("/tmp"), state_root=Path("/tmp/state"),
            submit=raising_submit, capture_pane=lambda target: "irrelevant",
            attempts=2, poll_interval_seconds=0.0, sleep=lambda s: None,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.structured_payload, {})

    def test_retry_then_success(self) -> None:
        handle = _handle()
        nonce = "selfreport-fixed"
        calls = {"count": 0}

        def fake_capture_pane(target):
            calls["count"] += 1
            if calls["count"] < 3:
                return "still typing..."
            return f'{rc._BEGIN_PREFIX}{nonce} {{"role_id": "repo:role.json"}} {rc._END_PREFIX}{nonce}'

        outcome = rc.default_communicate(
            handle, "identify yourself",
            repo_root=Path("/tmp"), state_root=Path("/tmp/state"),
            submit=lambda h, request, *, state_root: _receipt(),
            capture_pane=fake_capture_pane,
            attempts=5, poll_interval_seconds=0.0, sleep=lambda s: None,
            nonce_factory=lambda: nonce,
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.structured_payload, {"role_id": "repo:role.json"})
        self.assertEqual(calls["count"], 3)

    def test_uses_injected_nonce_and_ignores_stale_nonce_block(self) -> None:
        """Composition-level adversarial-content-injection coverage: a pane
        carrying a well-formed block under a STALE nonce, followed later by
        the correct-nonce block, must only match the correct one."""
        handle = _handle()
        nonce = "selfreport-current"
        attempts_seen = {"count": 0}

        def fake_capture_pane(target):
            attempts_seen["count"] += 1
            if attempts_seen["count"] == 1:
                return f'{rc._BEGIN_PREFIX}stale-nonce {{"role_id": "repo:WRONG.json"}} {rc._END_PREFIX}stale-nonce'
            return f'{rc._BEGIN_PREFIX}{nonce} {{"role_id": "repo:right.json"}} {rc._END_PREFIX}{nonce}'

        outcome = rc.default_communicate(
            handle, "identify yourself",
            repo_root=Path("/tmp"), state_root=Path("/tmp/state"),
            submit=lambda h, request, *, state_root: _receipt(),
            capture_pane=fake_capture_pane,
            attempts=3, poll_interval_seconds=0.0, sleep=lambda s: None,
            nonce_factory=lambda: nonce,
        )
        self.assertEqual(outcome.structured_payload, {"role_id": "repo:right.json"})


if __name__ == "__main__":
    unittest.main()
