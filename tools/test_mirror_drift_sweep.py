#!/usr/bin/env python3
"""Unit tests for tools/mirror_drift_sweep.py (T-1994).

Network-free: `read_source_head` and `count_commits_behind` are patched so no
git subprocess runs. Ticket filing is patched (no live ticket.py invocation).

Run: python3 -m unittest tools.test_mirror_drift_sweep
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import mirror_drift_sweep as mds
from tools import mirror_state as ms


ANCHOR = "a" * 40
CURRENT = "c" * 40
STAGED = "5" * 40


def _pair(slug="codex-harness-mirror", gated=False, gate_tickets=None, local=False):
    src = {"ref": "HEAD"}
    if local:
        src["local_repo"] = "."
    else:
        src["ssh_alias"] = "github-codexconfig"
        src["repo"] = "example-org/codex-config.git"
    p = {
        "slug": slug,
        "source": src,
        "mirror": {"repo": "x/y"},
        "refresh_target": "recovery",
        "publish_via": "orchestrator",
        "gated": gated,
    }
    if gate_tickets:
        p["gate_tickets"] = gate_tickets
    return p


def _state_with(slug, anchor=None, staged_head=None):
    state = ms._empty_state()
    if anchor is not None:
        ms.record_published(state, slug, anchor, by="orchestrator")
    if staged_head is not None:
        ms.record_staged(state, slug, staged_head)
    return state


class TestEvaluatePair(unittest.TestCase):
    def _eval(self, pair, state, current_head, count=None):
        with mock.patch.object(mds, "read_source_head", return_value=current_head), \
             mock.patch.object(mds, "count_commits_behind", return_value=count):
            return mds.evaluate_pair(pair, state, Path("/nonexistent-cache"))

    def test_no_drift(self):
        pair = _pair()
        state = _state_with(pair["slug"], anchor=ANCHOR)
        r = self._eval(pair, state, current_head=ANCHOR)
        self.assertEqual(r.status, "no_drift")

    def test_drift_with_exact_count(self):
        pair = _pair()
        state = _state_with(pair["slug"], anchor=ANCHOR)
        r = self._eval(pair, state, current_head=CURRENT, count=7)
        self.assertEqual(r.status, "drift")
        self.assertEqual(r.commits_behind, 7)
        self.assertEqual(r.current_head, CURRENT)
        self.assertEqual(r.anchor, ANCHOR)

    def test_drift_uncountable(self):
        pair = _pair()
        state = _state_with(pair["slug"], anchor=ANCHOR)
        r = self._eval(pair, state, current_head=CURRENT, count=None)
        self.assertEqual(r.status, "drift")
        self.assertIsNone(r.commits_behind)
        self.assertIn("exact count unavailable", r.detail)

    def test_built_but_unpushed(self):
        # anchor = old published; staged = current (latest built, not published).
        pair = _pair()
        state = _state_with(pair["slug"], anchor=ANCHOR, staged_head=CURRENT)
        r = self._eval(pair, state, current_head=CURRENT)
        self.assertEqual(r.status, "built_unpushed")
        self.assertEqual(r.staged_head, CURRENT)

    def test_drift_beats_stale_staged(self):
        # source moved past BOTH anchor and an older staged build -> drift (rebuild).
        pair = _pair()
        state = _state_with(pair["slug"], anchor=ANCHOR, staged_head=STAGED)
        r = self._eval(pair, state, current_head=CURRENT, count=3)
        self.assertEqual(r.status, "drift")
        self.assertIn("staged-but-unpublished", r.detail)

    def test_coverage_gap_no_anchor_ungated(self):
        pair = _pair()
        state = ms._empty_state()  # no anchor, no staged
        r = self._eval(pair, state, current_head=CURRENT)
        self.assertEqual(r.status, "coverage_gap")

    def test_gated_no_anchor_is_gated_not_coverage_gap(self):
        pair = _pair(slug="pantheon-public-mirror", gated=True,
                     gate_tickets=["T-1963", "T-1970", "T-1989"], local=True)
        state = ms._empty_state()
        r = self._eval(pair, state, current_head=CURRENT)
        self.assertEqual(r.status, "gated")
        self.assertIn("T-1963", r.detail)

    def test_gated_with_drift_still_detected_not_filed_status(self):
        # A gated pair that HAS an anchor and has drifted: status=drift but the
        # detail marks it gated/detect-only; run_sweep must not file it.
        pair = _pair(slug="pantheon-public-mirror", gated=True,
                     gate_tickets=["T-1963"], local=True)
        state = _state_with("pantheon-public-mirror", anchor=ANCHOR)
        r = self._eval(pair, state, current_head=CURRENT, count=2)
        self.assertEqual(r.status, "drift")
        self.assertIn("GATED", r.detail)

    def test_read_error_is_reported_not_raised(self):
        pair = _pair()
        state = _state_with(pair["slug"], anchor=ANCHOR)
        with mock.patch.object(mds, "read_source_head",
                               side_effect=RuntimeError("network down")):
            r = mds.evaluate_pair(pair, state, Path("/nope"))
        self.assertEqual(r.status, "error")
        self.assertIn("network down", r.detail)


class TestIdempotentFiling(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tickets = Path(self._tmp.name) / "tickets"
        (self.tickets / "inbox").mkdir(parents=True)
        (self.tickets / "active").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_ticket(self, tid, state_dir, labels):
        # Minimal parseable ticket per ticket.py's front-matter schema.
        body = (
            f"---\n"
            f"id: {tid}\n"
            f"created: 2026-07-23T00:00:00Z\n"
            f"requester: recovery\n"
            f"target: recovery\n"
            f"state: {state_dir}\n"
            f"parent_ticket: null\n"
            f"depends_on: []\n"
            f"labels: [{', '.join(labels)}]\n"
            f"---\n\n"
            f"# {tid} test\n\n## Transition log\n\n- 2026-07-23T00:00:00Z [recovery] filed\n"
        )
        (self.tickets / state_dir / f"{tid}.md").write_text(body, encoding="utf-8")

    def test_finds_existing_open_refresh_ticket(self):
        self._write_ticket("T-9001", "active",
                           ["mirror-refresh", "pair:codex-harness-mirror", "infra"])
        found = mds.existing_open_refresh_ticket("codex-harness-mirror", self.tickets)
        self.assertEqual(found, "T-9001")

    def test_no_match_for_different_pair(self):
        self._write_ticket("T-9002", "inbox",
                           ["mirror-refresh", "pair:pwa-release-mirror"])
        found = mds.existing_open_refresh_ticket("codex-harness-mirror", self.tickets)
        self.assertIsNone(found)

    def test_no_match_without_refresh_label(self):
        self._write_ticket("T-9003", "active", ["pair:codex-harness-mirror"])
        found = mds.existing_open_refresh_ticket("codex-harness-mirror", self.tickets)
        self.assertIsNone(found)

    def test_run_sweep_skips_filing_when_ticket_exists(self):
        self._write_ticket("T-9004", "active",
                           ["mirror-refresh", "pair:codex-harness-mirror"])
        pairs = [_pair()]
        state = _state_with("codex-harness-mirror", anchor=ANCHOR)
        with mock.patch.object(mds, "load_pairs", return_value=pairs), \
             mock.patch.object(mds, "load_state", return_value=state), \
             mock.patch.object(mds, "read_source_head", return_value=CURRENT), \
             mock.patch.object(mds, "count_commits_behind", return_value=1), \
             mock.patch.object(mds, "file_refresh_ticket") as file_mock:
            results = mds.run_sweep(tickets_root=self.tickets, commit_prod=True)
        file_mock.assert_not_called()  # existing ticket -> no duplicate
        self.assertEqual(results[0].filed_ticket, "T-9004")

    def test_run_sweep_files_when_drift_and_no_existing(self):
        pairs = [_pair()]
        state = _state_with("codex-harness-mirror", anchor=ANCHOR)
        with mock.patch.object(mds, "load_pairs", return_value=pairs), \
             mock.patch.object(mds, "load_state", return_value=state), \
             mock.patch.object(mds, "read_source_head", return_value=CURRENT), \
             mock.patch.object(mds, "count_commits_behind", return_value=4), \
             mock.patch.object(mds, "file_refresh_ticket", return_value="T-9999") as file_mock:
            results = mds.run_sweep(tickets_root=self.tickets, commit_prod=True)
        file_mock.assert_called_once()
        self.assertEqual(results[0].filed_ticket, "T-9999")

    def test_run_sweep_dry_run_files_nothing(self):
        pairs = [_pair()]
        state = _state_with("codex-harness-mirror", anchor=ANCHOR)
        with mock.patch.object(mds, "load_pairs", return_value=pairs), \
             mock.patch.object(mds, "load_state", return_value=state), \
             mock.patch.object(mds, "read_source_head", return_value=CURRENT), \
             mock.patch.object(mds, "count_commits_behind", return_value=4), \
             mock.patch.object(mds, "file_refresh_ticket") as file_mock:
            results = mds.run_sweep(tickets_root=self.tickets, commit_prod=False)
        file_mock.assert_not_called()
        self.assertEqual(results[0].status, "drift")
        self.assertIsNone(results[0].filed_ticket)

    def test_run_sweep_never_files_gated_pair(self):
        pairs = [_pair(slug="pantheon-public-mirror", gated=True,
                       gate_tickets=["T-1963"], local=True)]
        state = _state_with("pantheon-public-mirror", anchor=ANCHOR)
        with mock.patch.object(mds, "load_pairs", return_value=pairs), \
             mock.patch.object(mds, "load_state", return_value=state), \
             mock.patch.object(mds, "read_source_head", return_value=CURRENT), \
             mock.patch.object(mds, "count_commits_behind", return_value=9), \
             mock.patch.object(mds, "file_refresh_ticket") as file_mock:
            results = mds.run_sweep(tickets_root=self.tickets, commit_prod=True)
        file_mock.assert_not_called()  # gated -> detect but never file
        self.assertEqual(results[0].status, "drift")
        self.assertIsNone(results[0].filed_ticket)


class TestDigest(unittest.TestCase):
    def test_digest_lists_all_pairs_and_mode(self):
        results = [
            mds.PairResult(slug="a", status="no_drift"),
            mds.PairResult(slug="b", status="drift", detail="2 behind"),
        ]
        d = mds.build_digest(results, committed=False)
        self.assertIn("dry-run", d)
        self.assertIn("a", d)
        self.assertIn("b", d)
        self.assertIn("Actionable", d)

    def test_digest_filing_mode(self):
        results = [mds.PairResult(slug="a", status="no_drift")]
        d = mds.build_digest(results, committed=True)
        self.assertIn("FILING", d)


class TestConfigParsing(unittest.TestCase):
    """The real repo config files parse and carry the expected shape."""

    def test_real_pairs_config_parses(self):
        pairs = mds.load_pairs(mds.DEFAULT_PAIRS_PATH)
        slugs = {p["slug"] for p in pairs}
        self.assertEqual(slugs, set(ms.KNOWN_SLUGS))
        for p in pairs:
            self.assertEqual(p["refresh_target"], "recovery")
            self.assertEqual(p["publish_via"], "orchestrator")  # all four via orchestrator (T-1994)
        seed = next(p for p in pairs if p["slug"] == "pantheon-public-mirror")
        self.assertTrue(seed["gated"])

    def test_real_state_config_parses(self):
        state = ms.load_state(mds.DEFAULT_STATE_PATH)
        # The three non-gated pairs are seeded with anchors.
        for slug in ("claude-harness-mirror", "codex-harness-mirror", "pwa-release-mirror"):
            self.assertIsNotNone(
                state["pairs"].get(slug, {}).get("source_head_at_last_publish"),
                f"{slug} should have a seeded anchor",
            )


if __name__ == "__main__":
    unittest.main()
