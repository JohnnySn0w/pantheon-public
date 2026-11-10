#!/usr/bin/env python3
"""Unit tests for tools/mirror_drift_sweep.py (T-1994).

Network-free: `read_source_head` and `count_commits_behind` are patched so no
git subprocess runs. Ticket filing is patched (no live ticket.py invocation).

Run: python3 -m unittest tools.test_mirror_drift_sweep
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools import mirror_drift_sweep as mds
from tools import mirror_state as ms

# Never let a suite run deliver the owner digest into the live mailbox hub,
# even in tests that strip PANTHEON_DISABLE_LIVE_TELEGRAM to exercise the
# token read (the 2026-10-12 ticket_grooming leak class).
os.environ.setdefault("PANTHEON_DISABLE_LIVE_MAILBOX", "1")


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


def _state_with(slug, anchor=None, staged_head=None, published_at=None):
    state = ms._empty_state()
    if anchor is not None:
        ms.record_published(state, slug, anchor, by="orchestrator", now_iso=published_at)
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

    def test_dry_run_never_says_would_file_for_a_gated_pair(self):
        # T-3638: run_sweep never files a gated pair, so the dry-run digest must not
        # claim it would ("GATED ...; detect-only, not filing (would file)").
        results = [
            mds.PairResult(slug="open", status="drift", detail="1 behind"),
            mds.PairResult(slug="gated", status="drift", gated=True,
                           detail="7340 behind — GATED on T-1327; detect-only, not filing"),
        ]
        lines = mds.build_digest(results, committed=False).splitlines()
        [open_line] = [l for l in lines if "open:" in l]
        [gated_line] = [l for l in lines if "gated:" in l]
        self.assertIn("(would file)", open_line)
        self.assertNotIn("(would file)", gated_line)


class TestConfigParsing(unittest.TestCase):
    """The real repo config files parse and carry the expected shape."""

    def test_real_pairs_config_parses(self):
        pairs = mds.load_pairs(mds.DEFAULT_PAIRS_PATH)
        slugs = {p["slug"] for p in pairs}
        self.assertEqual(slugs, set(ms.KNOWN_SLUGS))
        for p in pairs:
            self.assertEqual(p["refresh_target"], "role:system_orchestrator")
            self.assertEqual(p["publish_via"], "orchestrator")  # all four via orchestrator (T-1994)
        seed = next(p for p in pairs if p["slug"] == "pantheon-public-mirror")
        self.assertTrue(seed["gated"])
        # T-4768 PR5 (orchestrator T8): the harness pairs are generated from pantheon-config.
        by = {p["slug"]: p for p in pairs}
        for slug, mirror in (("claude-harness-mirror", "claude"),
                             ("codex-harness-mirror", "codex")):
            self.assertEqual(by[slug]["generator"],
                             {"tool": "build_harness_mirror", "mirror": mirror})
            self.assertEqual(by[slug]["source"]["local_repo"], ".")
        for slug in ("pwa-release-mirror", "pantheon-public-mirror"):  # legacy, unchanged
            self.assertNotIn("generator", by[slug])

    def test_real_state_config_parses(self):
        state = ms.load_state(mds.DEFAULT_STATE_PATH)
        # The three non-gated pairs are seeded with anchors.
        for slug in ("claude-harness-mirror", "codex-harness-mirror", "pwa-release-mirror"):
            self.assertIsNotNone(
                state["pairs"].get(slug, {}).get("source_head_at_last_publish"),
                f"{slug} should have a seeded anchor",
            )

    def test_real_state_generator_pairs_carry_the_published_tree(self):
        # T-4768 C5: the state pins each pair's latest publish (claude: T-6361 62536e17,
        # tree 9f04a88b, source a778e41d; codex: T-6362 67b5de93,
        # tree 316f7ce0, source 3b7ab62a), and each manifest's
        # acceptance block is anchored to exactly that publish, so a re-pin lands in the
        # same commit as its record-published. record-published drops the T9/T10 rebased_from (a real publish
        # supersedes the rebase), so none may linger.
        import yaml
        state = ms.load_state(mds.DEFAULT_STATE_PATH)
        for slug, mirror in (("claude-harness-mirror", "claude"),
                             ("codex-harness-mirror", "codex")):
            entry = state["pairs"][slug]
            m = yaml.safe_load((mds.REPO_ROOT / mds._bhm().MIRRORS[mirror]).read_text())
            self.assertEqual(entry["published_payload_tree"], m["acceptance"]["published_tree"])
            self.assertEqual(entry["source_head_at_last_publish"], m["acceptance"]["source_sha"])
            self.assertNotIn("rebased_from", entry)


class TestStaleDays(unittest.TestCase):
    NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)

    def test_whole_days_since_published(self):
        self.assertEqual(mds._stale_days("2026-08-23T00:00:00Z", now=self.NOW), 20)

    def test_none_for_missing_or_unparseable(self):
        self.assertIsNone(mds._stale_days(None, now=self.NOW))
        self.assertIsNone(mds._stale_days("", now=self.NOW))
        self.assertIsNone(mds._stale_days("not-a-timestamp", now=self.NOW))

    def test_future_stamp_clamps_to_zero(self):
        # clock skew must not produce a negative age
        self.assertEqual(mds._stale_days("2026-09-20T00:00:00Z", now=self.NOW), 0)


class TestSlaBreached(unittest.TestCase):
    def _r(self, status, stale_days):
        return mds.PairResult(slug="x", status=status, stale_days=stale_days)

    def test_actionable_at_or_over_threshold_breaches(self):
        self.assertTrue(mds.sla_breached(self._r("drift", 15), 14))
        self.assertTrue(mds.sla_breached(self._r("built_unpushed", 14), 14))  # inclusive

    def test_actionable_under_threshold_ok(self):
        self.assertFalse(mds.sla_breached(self._r("drift", 13), 14))

    def test_non_actionable_never_breaches(self):
        for status in ("no_drift", "gated", "coverage_gap", "error"):
            self.assertFalse(mds.sla_breached(self._r(status, 999), 14), status)

    def test_unknown_age_never_breaches(self):
        self.assertFalse(mds.sla_breached(self._r("drift", None), 14))

    def test_gated_pair_never_breaches(self):
        # a ratification-gated pair is deliberately suppressed (it surfaces as
        # GATED every digest) — not "losing priority to more-urgent work", so it
        # must not false-fire the SLA-breach escalation.
        r = mds.PairResult(slug="pantheon-public-mirror", status="drift", stale_days=99, gated=True)
        self.assertFalse(mds.sla_breached(r, 14))


class TestEvaluatePairStaleDays(unittest.TestCase):
    def test_stale_days_populated_from_published_at(self):
        now = datetime(2026, 9, 12, tzinfo=timezone.utc)
        pair = _pair()
        state = _state_with(pair["slug"], anchor=ANCHOR, published_at="2026-08-13T00:00:00Z")
        with mock.patch.object(mds, "_now", return_value=now), \
             mock.patch.object(mds, "read_source_head", return_value=CURRENT), \
             mock.patch.object(mds, "count_commits_behind", return_value=3):
            r = mds.evaluate_pair(pair, state, Path("/nope"))
        self.assertEqual(r.status, "drift")
        self.assertEqual(r.published_at, "2026-08-13T00:00:00Z")
        self.assertEqual(r.stale_days, 30)
        self.assertTrue(mds.sla_breached(r, 14))


class TestSlaDigest(unittest.TestCase):
    def _r(self, slug, status, stale_days):
        return mds.PairResult(slug=slug, status=status, stale_days=stale_days, detail="d")

    def test_breach_section_rendered_most_stale_first(self):
        results = [
            self._r("aaa", "drift", 40),
            self._r("bbb", "built_unpushed", 20),
            self._r("ccc", "no_drift", 999),
        ]
        digest = mds.build_digest(results, committed=False, sla_threshold=14)
        self.assertIn("SLA BREACH", digest)
        sla_part = digest.split("Actionable:")[0]
        self.assertIn("aaa:", sla_part)
        self.assertIn("bbb:", sla_part)
        self.assertNotIn("ccc:", sla_part)  # current pair is not overdue
        self.assertLess(sla_part.index("aaa:"), sla_part.index("bbb:"))  # 40d before 20d
        self.assertIn("40d since last publish", sla_part)

    def test_no_breach_section_when_all_within_sla(self):
        results = [self._r("aaa", "drift", 3), self._r("bbb", "no_drift", 999)]
        digest = mds.build_digest(results, committed=False, sla_threshold=14)
        self.assertNotIn("SLA BREACH", digest)

    def test_threshold_is_honoured(self):
        results = [self._r("aaa", "drift", 10)]
        self.assertNotIn("SLA BREACH", mds.build_digest(results, committed=False, sla_threshold=14))
        self.assertIn("SLA BREACH", mds.build_digest(results, committed=False, sla_threshold=7))


# ---------------------------------------------------------------------------
# Generator pairs (T-4768 PR5): payload-tree drift against a throwaway repo.
# The fixture is test_build_harness_mirror.Scratch (mirror "x" at harness/x, the
# real leak-gate pattern sources copied in), registered as a MIRRORS entry.
# ---------------------------------------------------------------------------

from tools import test_build_harness_mirror as tbhm  # noqa: E402

GEN_SLUG = "claude-harness-mirror"


class GenScratch(tbhm.Scratch):
    def setUp(self):
        super().setUp()
        bhm = mds._bhm()
        patcher = mock.patch.dict(bhm.MIRRORS, {"x": self.MANIFEST_REL})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base = self.sha
        self.base_tree = self.tree(self.base)

    def pair(self, **kw):
        p = {"slug": GEN_SLUG, "source": {"local_repo": str(self.repo), "ref": "HEAD"},
             "generator": {"tool": "build_harness_mirror", "mirror": "x"},
             "mirror": {"repo": "x/y"}, "refresh_target": "recovery", "publish_via": "orchestrator",
             "gated": False}
        p.update(kw)
        return p

    def tree(self, sha):
        res, _ = self.build(sha)
        self.assertEqual(res.errors, [])
        return mds._payload_tree(res)

    def state(self, anchor=None, tree=None, staged_tree=None):
        state = ms._empty_state()
        ms.record_published(state, GEN_SLUG, anchor or self.base, by="orchestrator",
                            payload_tree=tree or self.base_tree)
        if staged_tree:
            ms.record_staged(state, GEN_SLUG, "5" * 40, payload_tree=staged_tree)
        return state

    def edit(self, rel, text):
        self.files[rel] = text
        self.sha = self.commit()
        return self.sha

    def evaluate(self, state=None, pair=None):
        return mds.evaluate_pair(pair or self.pair(), state or self.state(), Path("/nonexistent"))


class TestGeneratedPair(GenScratch):
    def test_tree_equal_is_no_drift(self):
        r = self.evaluate()
        self.assertEqual(r.status, "no_drift", r.detail)
        self.assertEqual(r.payload_tree, self.base_tree)
        self.assertEqual(mds.pair_exit_code(r), mds.EXIT_OK)

    def test_tree_change_is_drift(self):
        self.edit("harness/x/agents/b.md", "keep /home/claude/x there\n")
        r = self.evaluate()
        self.assertEqual(r.status, "drift", r.detail)
        self.assertEqual(r.payload_commits_behind, 1)
        self.assertEqual(mds.pair_exit_code(r), mds.EXIT_DRIFT)

    def test_payload_path_commit_that_keeps_the_tree_is_not_drift(self):
        # The core PR5 claim: a commit count no longer drives drift.
        self.edit("harness/x/tests/t.py", "assert True\n")  # excluded by tests/*
        r = self.evaluate()
        self.assertEqual(r.payload_commits_behind, 1)
        self.assertEqual(r.status, "no_drift", r.detail)

    def test_stale_shadow_exit_3_and_ticket_lists_it(self):
        self.edit("harness/x/install.sh", "#!/bin/sh\necho seat v2\n")  # shadow source moved
        r = self.evaluate()
        self.assertEqual(r.status, "stale", r.detail)
        self.assertEqual(len(r.stale), 1)
        self.assertTrue(r.stale[0].startswith("STALE install.sh: source harness/x/install.sh"))
        self.assertEqual(mds.pair_exit_code(r), mds.EXIT_STALE)
        title, ask = mds._generated_ticket_text(r, str(self.repo), "HEAD")
        self.assertIn("1 STALE shadow(s)", title)
        self.assertIn(r.stale[0], ask)
        # The refresh pointer must name the anchor-reproduces-live check (SOP step 2a),
        # not a bare `check` against the frozen acceptance tree (T-4768 PR6 tool gap).
        self.assertIn("--expect-tree <live mirror tree>", ask)
        self.assertIn("--against-repo <mirror clone>", ask)
        self.assertIn("refreshing-an-org-039-release-mirror.md", ask)
        self.assertIn("diff --against <live head> --against-repo <mirror clone>", ask)

    def test_stale_beats_drift(self):
        self.files["harness/x/agents/b.md"] = "keep /home/claude/x there\n"
        self.edit("harness/x/install.sh", "#!/bin/sh\necho seat v2\n")
        r = self.evaluate()
        self.assertEqual(r.status, "stale")
        self.assertIn("payload tree also drifted", r.detail)

    def test_staged_tree_equal_head_is_built_unpushed(self):
        self.edit("harness/x/agents/b.md", "keep /home/claude/x there\n")
        r = self.evaluate(self.state(staged_tree=self.tree(self.sha)))
        self.assertEqual(r.status, "built_unpushed", r.detail)
        self.assertEqual(mds.pair_exit_code(r), mds.EXIT_DRIFT)

    def test_no_published_tree_is_coverage_gap(self):
        state = ms._empty_state()
        ms.record_published(state, GEN_SLUG, self.base, by="orchestrator")
        r = self.evaluate(state)
        self.assertEqual(r.status, "coverage_gap", r.detail)
        self.assertEqual(mds.pair_exit_code(r), mds.EXIT_OK)

    def test_manifest_error_is_config_error_exit_2(self):
        self.edit("harness/x/agents/new.md", "unclassified\n")
        r = self.evaluate()
        self.assertTrue(r.config_error, r.detail)
        self.assertIn("UNCLASSIFIED", r.detail)
        self.assertEqual(mds.pair_exit_code(r), mds.EXIT_CONFIG)

    def test_unknown_head_ref_is_config_error(self):
        r = self.evaluate(pair=self.pair(source={"local_repo": str(self.repo),
                                                 "ref": "no-such-ref"}))
        self.assertTrue(r.config_error)
        self.assertEqual(mds.pair_exit_code(r), mds.EXIT_CONFIG)


class TestGeneratedLeak(GenScratch):
    LEAKY = "harness/x/agents/b.md"

    def leak(self):
        # A persona name the gate derives from instance.yaml (PR4); the secret string
        # must never reach the alert (recovery W8).
        return self.edit(self.LEAKY, "keep /home/claude/x here\nask artisan first\n")

    def test_leak_is_status_leak_exit_4_redacted(self):
        self.leak()
        r = self.evaluate()
        self.assertEqual(r.status, "leak", r.detail)
        self.assertGreaterEqual(r.leaks, 1)
        self.assertEqual(mds.pair_exit_code(r), mds.EXIT_LEAK)
        self.assertTrue(any(f.startswith("LEAK agents/b.md:") for f in r.leak_findings),
                        r.leak_findings)
        digest = mds.build_digest([r], committed=True)
        self.assertIn("LEAK ALERT", digest)
        for text in [digest, r.detail, *r.leak_findings]:
            self.assertNotIn("artisan", text)

    def test_leak_beats_stale_and_drift(self):
        self.files["harness/x/install.sh"] = "#!/bin/sh\necho seat v2\n"
        self.leak()
        self.assertEqual(self.evaluate().status, "leak")

    def test_leak_is_never_filed(self):
        self.leak()
        with tempfile.TemporaryDirectory() as t, \
                mock.patch.object(mds, "load_pairs", return_value=[self.pair()]), \
                mock.patch.object(mds, "load_state", return_value=self.state()), \
                mock.patch.object(mds, "file_refresh_ticket") as file_mock, \
                mock.patch.object(mds, "_send_telegram") as tg, \
                mock.patch.object(mds, "_send_owner_digest") as owner:
            rc = mds.main(["--commit-prod", "--no-fetch", "--tickets-root", t])
        file_mock.assert_not_called()
        self.assertEqual(rc, mds.EXIT_LEAK)
        tg.assert_called_once()  # a leak needs the operator
        self.assertIn("leak", tg.call_args[0][0])
        self.assertNotIn("artisan", tg.call_args[0][0])
        self.assertNotIn("artisan", owner.call_args[0][0])


class TestRedactLeakLine(unittest.TestCase):
    def test_every_report_shape_drops_the_matched_string(self):
        cases = {
            "LEAK a/b.md:12: [d:persona:artisan] 'artisan'": "LEAK a/b.md:12 [d:persona]",
            "LEAK artisan/x.md: [d:persona:artisan] 'artisan' in the path":
                "LEAK <path withheld: path-name hit> [d:persona]",
            "LEAK a/b.md: [e:bytes] byte-sequence trigger present": "LEAK a/b.md [e:bytes]",
            "LEAK p.txt: is the gate's identity pattern file": "LEAK p.txt [d:pattern-file]",
            "ALLOWLIST a/b.md: 'secret' occurs 2 times, allowlisted 1":
                "ALLOWLIST a/b.md [leak_allowlist entry does not hold]",
            "something new 'secret'": "LEAK <finding withheld: unrecognised report line>",
        }
        for line, want in cases.items():
            got = mds.redact_leak_line(line)
            self.assertEqual(got, want)
            self.assertNotIn("artisan", got)
            self.assertNotIn("secret", got)


class TestMatchesPublished(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(mds._matches_published(None, {}, "t", "t"), (True, []))

    def test_not_the_acceptance_tree_never_consults_the_allowlist(self):
        with mock.patch.object(mds._bhm(), "check") as check:
            ok, lines = mds._matches_published(None, {"acceptance": {"published_tree": "p"}},
                                               "t", "q")
        check.assert_not_called()
        self.assertFalse(ok)
        self.assertTrue(lines[0].startswith("MISMATCH"))

    def test_acceptance_tree_passes_through_check(self):
        res = mds._bhm().BuildResult()
        with mock.patch.object(mds._bhm(), "check", return_value=(0, ["ALLOWLISTED a: r"])):
            ok, _ = mds._matches_published(res, {"acceptance": {"published_tree": "p"}}, "t", "p")
        self.assertTrue(ok)


class TestPairValidation(unittest.TestCase):
    def _bad(self, **kw):
        p = {"slug": "s", "source": {"local_repo": ".", "ref": "HEAD"},
             "generator": {"tool": "build_harness_mirror", "mirror": "codex"}}
        p.update(kw)
        with self.assertRaises(ValueError):
            mds._validate_pair("cfg", p)

    def test_ok(self):
        mds._validate_pair("cfg", {"slug": "s", "source": {"local_repo": "."},
                                   "generator": {"tool": "build_harness_mirror",
                                                 "mirror": "claude"}})

    def test_generator_without_local_repo(self):
        self._bad(source={"ssh_alias": "a", "repo": "r"})

    def test_unknown_mirror_or_tool_or_keys(self):
        self._bad(generator={"tool": "build_harness_mirror", "mirror": "nope"})
        self._bad(generator={"tool": "other", "mirror": "codex"})
        self._bad(generator={"tool": "build_harness_mirror", "mirror": "codex", "x": 1})

    def test_source_paths_is_rejected(self):
        # Payload paths are derived from the manifest (payload_paths, orchestrator T3).
        self._bad(source={"local_repo": ".", "paths": ["harness/codex"]})

    def test_load_pairs_exits_2_on_bad_pair(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "pairs.yaml"
            path.write_text("version: 1\npairs:\n  - slug: s\n    source: {ssh_alias: a, repo: r}\n"
                            "    generator: {tool: build_harness_mirror, mirror: codex}\n")
            with mock.patch.object(mds, "_send_telegram"):
                rc = mds.main(["--pairs-path", str(path), "--no-fetch"])
        self.assertEqual(rc, mds.EXIT_CONFIG)


class TestFindAnchor(GenScratch):
    def test_exact_newest_wins_among_equal_trees(self):
        same = self.edit("harness/x/tests/t.py", "assert True\n")  # equal tree, newer
        self.edit("elsewhere.txt", "not a payload path\n")          # not a candidate
        found, _ = mds.find_anchor(self.repo, self.sha, "x", self.base_tree)
        self.assertEqual((found.sha, found.match), (same, "exact"))

    def test_allowlisted_fallback_is_newest_match(self):
        newer = self.edit("harness/x/agents/b.md", "keep /home/claude/x there\n")
        with mock.patch.object(mds, "_matches_published", return_value=(True, [])):
            found, _ = mds.find_anchor(self.repo, self.sha, "x", "e" * 40)
        self.assertEqual((found.sha, found.match), (newer, "allowlisted"))
        with mock.patch.object(mds, "_matches_published",
                               side_effect=lambda r, m, t, p: (t == self.base_tree, [])):
            found, _ = mds.find_anchor(self.repo, self.sha, "x", "e" * 40)
        self.assertEqual((found.sha, found.match), (self.base, "allowlisted"))

    def test_stops_at_the_unbuildable_floor(self):
        self.edit("harness/x/agents/new.md", "unclassified\n")  # unbuildable
        del self.files["harness/x/agents/new.md"]
        (self.repo / "harness/x/agents/new.md").unlink()
        self.edit("harness/x/agents/b.md", "keep /home/claude/x there\n")
        found, why = mds.find_anchor(self.repo, self.sha, "x", self.base_tree)
        self.assertIsNone(found)  # base (== published) lies below the floor
        self.assertIn("unbuildable", why)

    def test_none_found(self):
        found, why = mds.find_anchor(self.repo, self.sha, "x", "e" * 40)
        self.assertIsNone(found)
        self.assertIn("no build", why)


class TestRebaseAnchorCli(GenScratch):
    def setUp(self):
        super().setUp()
        self.newer = self.edit("harness/x/tests/t.py", "assert True\n")
        self.pairs_path = self.tmp / "pairs.yaml"
        import yaml
        self.pairs_path.write_text(yaml.safe_dump({"version": 1, "pairs": [self.pair()]}))
        self.state_path = self.tmp / "state.yaml"
        state = ms._empty_state()
        ms.record_published(state, GEN_SLUG, "a" * 40, mirror_head="b" * 40, by="recovery",
                            now_iso="2026-10-05T00:00:00Z")
        ms.save_state(state, self.state_path)

    def cli(self, *extra):
        return mds.main(["--pairs-path", str(self.pairs_path), "--state-path",
                         str(self.state_path), "--rebase-anchor", GEN_SLUG, *extra])

    def test_dry_run_writes_nothing(self):
        before = self.state_path.read_bytes()
        self.assertEqual(self.cli("--published-tree", self.base_tree), 0)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_write_rebases_and_keeps_publish_fields(self):
        self.assertEqual(self.cli("--published-tree", self.base_tree, "--write"), 0)
        e = ms.load_state(self.state_path)["pairs"][GEN_SLUG]
        self.assertEqual(e["source_head_at_last_publish"], self.newer)
        self.assertEqual(e["published_payload_tree"], self.base_tree)
        self.assertEqual(e["published_mirror_head"], "b" * 40)
        self.assertEqual(e["published_at"], "2026-10-05T00:00:00Z")
        self.assertEqual(e["rebased_from"]["source_head"], "a" * 40)
        self.assertEqual(e["rebased_from"]["match"], "exact")
        # ...and the sweep now reads the pair as current.
        r = mds.evaluate_pair(self.pair(), ms.load_state(self.state_path), Path("/x"))
        self.assertEqual(r.status, "no_drift")

    def test_none_found_exit_1_state_untouched(self):
        before = self.state_path.read_bytes()
        self.assertEqual(self.cli("--published-tree", "e" * 40, "--write"), 1)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_no_published_tree_is_exit_2(self):
        self.assertEqual(self.cli(), mds.EXIT_CONFIG)

    def test_legacy_pair_is_exit_2(self):
        import yaml
        self.pairs_path.write_text(yaml.safe_dump({"version": 1, "pairs": [
            {"slug": GEN_SLUG, "source": {"local_repo": str(self.repo)}}]}))
        self.assertEqual(self.cli("--published-tree", self.base_tree), mds.EXIT_CONFIG)


class TestCadence(GenScratch):
    STATE_REL = "mirror-published-state.yaml"

    def write_state(self, entry):
        self.files[self.STATE_REL] = __import__("yaml").safe_dump(
            {"version": 1, "pairs": {GEN_SLUG: entry}})
        return self.commit()

    def history(self):
        """base (import) -> C (tree change) -> H-only (excluded path) -> unrelated ->
        R (mirror head moves) -> A (anchor only)."""
        self.changed = self.edit("harness/x/agents/b.md", "keep /home/claude/x there\n")
        self.edit("harness/x/tests/t.py", "assert True\n")
        self.edit("elsewhere.txt", "x\n")
        self.write_state({"source_head_at_last_publish": self.base,
                          "published_mirror_head": "b" * 40,
                          "published_payload_tree": self.base_tree})
        self.sha = self.write_state({"source_head_at_last_publish": self.changed,
                                     "published_mirror_head": "b" * 40,
                                     "published_payload_tree": self.base_tree})

    def report(self, state=None, **kw):
        state = state or {"pairs": {GEN_SLUG: {"source_head_at_last_publish": self.base,
                                               "published_payload_tree": self.base_tree}}}
        return mds.cadence_for_pair(self.pair(), state, self.repo / self.STATE_REL, **kw)

    def test_counts(self):
        self.history()
        rep = self.report()
        self.assertEqual(rep.since, self.base)  # the first commit touching source_root
        self.assertEqual((rep.harness_commits, len(rep.payload_changing), rep.unbuildable,
                          rep.publishes, rep.anchor_only), (2, 1, 0, 1, 1))
        self.assertEqual(rep.payload_changing, [self.changed])
        self.assertEqual(rep.verdicts, ["healthy"])

    def test_over_publish(self):
        self.history()
        self.sha = self.write_state({"source_head_at_last_publish": self.changed,
                                     "published_mirror_head": "c" * 40})
        rep = self.report()
        self.assertEqual(rep.publishes, 2)
        self.assertTrue(rep.verdicts[0].startswith("over-publish"), rep.verdicts)

    def test_under_publish_lag(self):
        self.history()
        later = datetime.now(timezone.utc).replace(year=datetime.now().year + 1)
        rep = self.report(now=later)
        self.assertGreater(rep.lag_days, mds.SLA_STALE_DAYS)
        self.assertTrue(any(v.startswith("under-publish") for v in rep.verdicts), rep.verdicts)

    def test_current_head_has_no_lag(self):
        self.history()
        state = {"pairs": {GEN_SLUG: {"source_head_at_last_publish": self.changed,
                                      "published_payload_tree": self.tree(self.sha)}}}
        self.assertIsNone(self.report(state).lag_days)

    def test_unbuildable_counted(self):
        self.edit("harness/x/agents/new.md", "unclassified\n")
        del self.files["harness/x/agents/new.md"]
        (self.repo / "harness/x/agents/new.md").unlink()
        self.edit("harness/x/agents/b.md", "keep /home/claude/x there\n")
        rep = self.report()
        self.assertEqual((rep.harness_commits, rep.unbuildable), (2, 2))

    def test_since_and_bad_since(self):
        self.history()
        self.assertEqual(self.report(since=self.changed).harness_commits, 1)
        with self.assertRaises(ValueError):
            self.report(since="no-such-rev")


class TestBehindOrigin(tbhm.Scratch):
    def test_warns_only_when_behind(self):
        clone = self.tmp / "clone"
        tbhm.git(self.tmp, "clone", "-q", str(self.repo), str(clone))
        self.assertEqual(mds._behind_origin(clone, "HEAD", fetch=True), [])
        self.files["harness/x/tests/t.py"] = "assert 1\n"
        self.commit()
        self.assertEqual(mds._behind_origin(clone, "HEAD", fetch=False), [])  # not fetched yet
        got = mds._behind_origin(clone, "HEAD", fetch=True)
        self.assertEqual(len(got), 1)
        self.assertIn("1 commit(s) behind origin/main", got[0])

    def test_only_local_pairs_checked_once(self):
        with mock.patch.object(mds, "_behind_origin", return_value=["w"]) as b:
            got = mds.behind_origin_warnings([_pair(), _pair(local=True),
                                              _pair(slug="other", local=True)], fetch=False)
        self.assertEqual(got, ["w"])
        b.assert_called_once()


class TestExitCodes(unittest.TestCase):
    def r(self, status, **kw):
        return mds.PairResult(slug=kw.pop("slug", status), status=status, **kw)

    def test_pair_codes(self):
        self.assertEqual(mds.pair_exit_code(self.r("no_drift")), 0)
        self.assertEqual(mds.pair_exit_code(self.r("drift")), 10)
        self.assertEqual(mds.pair_exit_code(self.r("built_unpushed")), 10)
        self.assertEqual(mds.pair_exit_code(self.r("stale")), 13)
        self.assertEqual(mds.pair_exit_code(self.r("leak")), 4)
        self.assertEqual(mds.pair_exit_code(self.r("leak", gated=True)), 4)
        self.assertEqual(mds.pair_exit_code(self.r("drift", gated=True)), 0)
        self.assertEqual(mds.pair_exit_code(self.r("stale", gated=True)), 0)
        self.assertEqual(mds.pair_exit_code(self.r("coverage_gap")), 0)
        self.assertEqual(mds.pair_exit_code(self.r("error")), 0)  # legacy single read error
        self.assertEqual(mds.pair_exit_code(self.r("error", config_error=True)), 2)
        self.assertEqual(mds.pair_exit_code(self.r("drift", filing_failed=True)), 2)
        self.assertEqual(mds.pair_exit_code(self.r("stale", filing_failed=True)), 2)

    def test_precedence_4_2_13_10_0(self):
        ex = mds.sweep_exit_code
        self.assertEqual(ex([]), 0)
        self.assertEqual(ex([self.r("drift"), self.r("stale")]), 13)
        self.assertEqual(ex([self.r("stale"), self.r("error", config_error=True)]), 2)
        self.assertEqual(ex([self.r("error", config_error=True), self.r("leak")]), 4)
        self.assertEqual(ex([self.r("no_drift"), self.r("error")]), 0)

    def test_all_pairs_errored_is_2(self):
        # recovery W6: was exit 1 before T-4768.
        self.assertEqual(mds.sweep_exit_code([self.r("error", slug="a"),
                                              self.r("error", slug="b")]), 2)


class _MainHarness(unittest.TestCase):
    """Shared main() harness (no tests of its own)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tickets = Path(self._tmp.name)

    def run_main(self, pairs, state, *, extra=(), file_effect=None, heads=CURRENT):
        tg = mock.MagicMock()
        with mock.patch.object(mds, "load_pairs", return_value=pairs), \
             mock.patch.object(mds, "load_state", return_value=state), \
             mock.patch.object(mds, "read_source_head", return_value=heads), \
             mock.patch.object(mds, "count_commits_behind", return_value=3), \
             mock.patch.object(mds, "file_refresh_ticket", side_effect=file_effect) as fm, \
             mock.patch.object(mds, "_send_telegram", tg), \
             mock.patch.object(mds, "_send_owner_digest") as owner:
            rc = mds.main(["--no-fetch", "--tickets-root", str(self.tickets), *extra])
        self.owner = owner
        return rc, fm, tg


class TestMain(_MainHarness):
    def test_drift_week_exit_10(self):
        rc, fm, tg = self.run_main([_pair()], _state_with("codex-harness-mirror", anchor=ANCHOR),
                                   extra=["--commit-prod"], file_effect=["T-9999"])
        self.assertEqual(rc, 10)
        fm.assert_called_once()
        # Routine drift is owned by the filed ticket: full digest to the owner's
        # mailbox, nothing to the operator's Telegram (TG 6039-6040).
        tg.assert_not_called()
        self.owner.assert_called_once()

    def test_filing_failure_exits_2_and_digest_still_sent(self):
        # recovery W6: ANY failure to file / commit / push a refresh ticket -> exit 2.
        import subprocess
        err = subprocess.CalledProcessError(1, ["ticket.py"], output="", stderr="push rejected")
        rc, fm, tg = self.run_main([_pair()], _state_with("codex-harness-mirror", anchor=ANCHOR),
                                   extra=["--commit-prod"], file_effect=err)
        self.assertEqual(rc, mds.EXIT_CONFIG)
        fm.assert_called_once()
        tg.assert_not_called()  # exit 2 pages the owner via OnFailure
        self.owner.assert_called_once()
        self.assertIn("FILING FAILED", self.owner.call_args[0][0])
        self.assertIn("push rejected", self.owner.call_args[0][0])

    def test_filing_failure_is_not_counted_in_dry_run(self):
        rc, fm, _ = self.run_main([_pair()], _state_with("codex-harness-mirror", anchor=ANCHOR))
        fm.assert_not_called()
        self.assertEqual(rc, 10)

    def test_all_pairs_errored_exit_2(self):
        with mock.patch.object(mds, "load_pairs", return_value=[_pair()]), \
             mock.patch.object(mds, "load_state", return_value=ms._empty_state()), \
             mock.patch.object(mds, "read_source_head", side_effect=RuntimeError("down")), \
             mock.patch.object(mds, "_send_telegram") as tg, \
             mock.patch.object(mds, "_send_owner_digest") as owner:
            rc = mds.main(["--no-fetch", "--tickets-root", str(self.tickets)])
        self.assertEqual(rc, 2)
        tg.assert_not_called()
        owner.assert_not_called()  # dry run: report-only, nothing durable

    def test_sweep_failed_to_run_still_sends_digest(self):
        with mock.patch.object(mds, "load_pairs", side_effect=ValueError("bad yaml")), \
             mock.patch.object(mds, "_send_telegram") as tg, \
             mock.patch.object(mds, "_send_owner_digest") as owner:
            rc = mds.main(["--no-fetch", "--commit-prod"])
        self.assertEqual(rc, 2)
        tg.assert_not_called()
        owner.assert_called_once()
        self.assertIn("SWEEP FAILED TO RUN", owner.call_args[0][0])

    def test_gated_legacy_pair_unchanged_exit_0(self):
        rc, fm, _ = self.run_main(
            [_pair(slug="pantheon-public-mirror", gated=True, gate_tickets=["T-1327"],
                   local=True)],
            _state_with("pantheon-public-mirror", anchor=ANCHOR), extra=["--commit-prod"])
        self.assertEqual(rc, 0)
        fm.assert_not_called()

    def test_flag_misuse_is_argparse_error(self):
        for argv in (["--write"], ["--since", "x"], ["--published-tree", "x"],
                     ["--cadence-report", "--rebase-anchor", "s"]):
            with self.assertRaises(SystemExit) as cm, \
                    mock.patch("sys.stderr"):
                mds.main(argv)
            self.assertEqual(cm.exception.code, 2)


# ---------------------------------------------------------------------------
# recovery PR5 review round 1 (B1, B2, N1-N3).
# ---------------------------------------------------------------------------

import io  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

UNIT = mds.REPO_ROOT / "tools" / "systemd" / "pantheon-mirror-drift.service"


def _unit_success_codes() -> set[int]:
    codes = set()
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        if line.startswith("SuccessExitStatus="):
            codes |= {int(c) for c in line.split("=", 1)[1].split()}
    return codes


class TestCrashNeverReadsAsSuccess(_MainHarness):
    """recovery B1: no crash path may exit with a code the unit treats as success."""

    def _filing_fails(self):
        return subprocess.CalledProcessError(1, ["ticket.py"], output="", stderr="x")

    def test_drift_and_stale_codes_are_10_and_13(self):
        self.assertEqual((mds.EXIT_DRIFT, mds.EXIT_STALE), (10, 13))
        self.assertEqual(_unit_success_codes(), {10, 13})
        for crash_code in (1, mds.EXIT_CONFIG, mds.EXIT_LEAK):
            self.assertNotIn(crash_code, _unit_success_codes())

    def test_digest_build_failure_after_filing_failure_exits_2(self):
        with mock.patch.object(mds, "build_digest", side_effect=RuntimeError("boom")):
            rc, _, tg = self.run_main([_pair()],
                                      _state_with("codex-harness-mirror", anchor=ANCHOR),
                                      extra=["--commit-prod"], file_effect=self._filing_fails())
        self.assertEqual(rc, 2)
        tg.assert_not_called()
        self.assertIn("DIGEST FAILED", self.owner.call_args[0][0])  # best-effort proof-of-life

    def test_send_failure_after_filing_failure_exits_2(self):
        tg = mock.MagicMock(side_effect=PermissionError("denied"))
        with mock.patch.object(mds, "load_pairs", return_value=[_pair()]), \
             mock.patch.object(mds, "load_state",
                               return_value=_state_with("codex-harness-mirror", anchor=ANCHOR)), \
             mock.patch.object(mds, "read_source_head", return_value=CURRENT), \
             mock.patch.object(mds, "count_commits_behind", return_value=3), \
             mock.patch.object(mds, "file_refresh_ticket", side_effect=self._filing_fails()), \
             mock.patch.object(mds, "operator_reasons", return_value=["x"]), \
             mock.patch.object(mds, "_send_owner_digest"), \
             mock.patch.object(mds, "_send_telegram", tg), mock.patch("sys.stderr"):
            rc = mds.main(["--no-fetch", "--commit-prod", "--tickets-root", str(self.tickets)])
        self.assertEqual(rc, 2)

    def test_token_read_failure_exits_2(self):
        # The reproduction: filing fails, then read_telegram_token raises.
        env = {k: v for k, v in os.environ.items() if k != "PANTHEON_DISABLE_LIVE_TELEGRAM"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("tools.watch_digest.read_telegram_token",
                        side_effect=PermissionError("denied")), \
             mock.patch.object(mds, "load_pairs", return_value=[_pair()]), \
             mock.patch.object(mds, "load_state",
                               return_value=_state_with("codex-harness-mirror", anchor=ANCHOR)), \
             mock.patch.object(mds, "read_source_head", return_value=CURRENT), \
             mock.patch.object(mds, "count_commits_behind", return_value=3), \
             mock.patch.object(mds, "file_refresh_ticket", side_effect=self._filing_fails()), \
             mock.patch.object(mds, "operator_reasons", return_value=["x"]), \
             mock.patch.object(mds, "_send_owner_digest"), \
             mock.patch("sys.stderr"), mock.patch("sys.stdout"):
            rc = mds.main(["--no-fetch", "--commit-prod", "--tickets-root", str(self.tickets)])
        self.assertEqual(rc, 2)

    def test_any_uncaught_exception_returns_2_but_systemexit_passes(self):
        with mock.patch.object(mds, "_main", side_effect=MemoryError()), mock.patch("sys.stderr"):
            self.assertEqual(mds.main([]), 2)
        with mock.patch.object(mds, "_main", side_effect=SystemExit(2)):
            with self.assertRaises(SystemExit):
                mds.main([])

    def test_import_failure_exits_1_which_the_unit_pages_on(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "yaml.py").write_text("raise ImportError('forced for T-4768 B1')\n")
            env = {**os.environ, "PYTHONPATH": d, "PANTHEON_DISABLE_LIVE_TELEGRAM": "1"}
            proc = subprocess.run([sys.executable, str(mds.REPO_ROOT / "tools" /
                                                       "mirror_drift_sweep.py"), "--no-digest"],
                                  env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 1, proc.stderr[-500:])
        self.assertIn("forced for T-4768 B1", proc.stderr)
        self.assertNotIn(proc.returncode, _unit_success_codes())


class TestLeakPathWithheldEverywhere(GenScratch):
    """recovery B2: a path flagged by NAME never appears in any output."""

    LEAKY_PATH = "agents/artisan-notes.md"

    def test_redact_lines_withholds_path_hit_paths_in_all_lines(self):
        lines = [
            "LEAK docs/orchestrator-notes.md: [c:persona:orchestrator] 'orchestrator' in the path",
            "LEAK docs/orchestrator-notes.md:4: [c:persona:orchestrator] 'orchestrator'",
            "LEAK docs/orchestrator-notes.md: [e:bytes] byte-sequence trigger present",
            "ALLOWLIST docs/orchestrator-notes.md: 'x' occurs 2 time(s), entry says 1",
            "LEAK other.md:2: [c:persona:orchestrator] 'orchestrator'",
        ]
        got, withheld = mds.redact_leak_lines(lines)
        self.assertEqual(withheld, {"docs/orchestrator-notes.md"})
        joined = "\n".join(got)
        self.assertNotIn("orchestrator", joined)
        self.assertIn("LEAK other.md:2 [c:persona]", got)
        self.assertIn("LEAK <path withheld>:4 [c:persona]", got)

    def _leaky_build(self):
        self.manifest["files"].append({"path": self.LEAKY_PATH, "class": "generated"})
        self.files["harness/x/" + self.LEAKY_PATH] = "ask artisan first\n"
        self.sha = self.commit()

    def test_path_and_content_hit_same_file_nowhere_in_digest_or_json(self):
        self._leaky_build()
        r = self.evaluate()
        self.assertEqual(r.status, "leak", r.detail)
        self.assertEqual(r.mismatches, [])
        out, tg, owner = io.StringIO(), mock.MagicMock(), mock.MagicMock()
        with mock.patch.object(mds, "load_pairs", return_value=[self.pair()]), \
             mock.patch.object(mds, "load_state", return_value=self.state()), \
             mock.patch.object(mds, "_send_telegram", tg), \
             mock.patch.object(mds, "_send_owner_digest", owner), \
             mock.patch("sys.stdout", out):
            rc_json = mds.main(["--no-fetch", "--json", "--tickets-root", str(self.tmp)])
            rc = mds.main(["--no-fetch", "--tickets-root", str(self.tmp)])
        self.assertEqual((rc_json, rc), (mds.EXIT_LEAK, mds.EXIT_LEAK))
        owner.assert_not_called()  # dry run
        # Full detail (redacted) goes to stdout/json (and, with --commit-prod, the owner digest).
        for text in [out.getvalue()]:
            self.assertNotIn("artisan", text)
            self.assertIn("<path withheld", text)
        # the operator's Telegram gets the short alert only: no path, no matched string.
        self.assertTrue(tg.call_args_list)
        for c in tg.call_args_list:
            self.assertNotIn("artisan", c[0][0])
            self.assertNotIn(self.LEAKY_PATH, c[0][0])

    def test_config_error_plus_leak_reports_the_leak_exit_4(self):
        # recovery N2: build() skips the gate on a config error; the sweep scans anyway.
        self.files["harness/x/agents/new.md"] = "unclassified\n"
        self.edit("harness/x/agents/b.md", "keep /home/claude/x here\nask artisan first\n")
        r = self.evaluate()
        self.assertEqual(r.status, "leak", r.detail)
        self.assertTrue(r.config_error)
        self.assertIn("config error", r.detail)
        self.assertEqual(mds.pair_exit_code(r), mds.EXIT_LEAK)
        self.assertEqual(mds.sweep_exit_code([r]), mds.EXIT_LEAK)
        self.assertNotIn("artisan", mds.build_digest([r], committed=True))


class TestFilingBugKeepsOtherPairs(_MainHarness):
    def test_filing_valueerror_alongside_leak_pair_still_alerts_exit_4(self):
        # recovery N1.
        drift = mds.PairResult(slug="pwa-release-mirror", status="drift", detail="d")
        leak = mds.PairResult(slug="claude-harness-mirror", status="leak", leaks=1,
                              leak_findings=["LEAK a.md:1 [c:persona]"], detail="LEAK GATE FAILED")
        pairs = [_pair(slug="pwa-release-mirror"), _pair(slug="claude-harness-mirror")]
        tg = mock.MagicMock()
        with mock.patch.object(mds, "load_pairs", return_value=pairs), \
             mock.patch.object(mds, "load_state", return_value=ms._empty_state()), \
             mock.patch.object(mds, "evaluate_pair", side_effect=[drift, leak]), \
             mock.patch.object(mds, "file_refresh_ticket", side_effect=ValueError("bad")), \
             mock.patch.object(mds, "_send_telegram", tg), \
             mock.patch.object(mds, "_send_owner_digest") as owner, mock.patch("sys.stdout"):
            rc = mds.main(["--no-fetch", "--commit-prod", "--tickets-root", str(self.tickets)])
        self.assertEqual(rc, mds.EXIT_LEAK)
        self.assertTrue(drift.filing_failed)
        self.assertIn("LEAK ALERT", owner.call_args[0][0])
        self.assertIn("FILING FAILED: bad", owner.call_args[0][0])
        self.assertIn("leak", tg.call_args[0][0])  # the leak still reaches the operator


class TestReadErrorDetailInDigest(unittest.TestCase):
    def test_read_error_prints_its_detail(self):
        # recovery N3 (known gap: not escalated, exit 0 for a single legacy read error).
        r = mds.PairResult(slug="pwa-release-mirror", status="error",
                           detail="read failed: ls-remote timed out")
        line = [l for l in mds.build_digest([r, mds.PairResult(slug="a", status="no_drift")],
                                            committed=False).splitlines()
                if "pwa-release-mirror" in l][-1]
        self.assertIn("ls-remote timed out", line)



class TestOperatorRouting(unittest.TestCase):
    """the operator TG 6039-6040: Telegram only when something needs him."""

    def _r(self, **kw):
        base = dict(slug="codex-harness-mirror", status="stale", detail="d")
        base.update(kw)
        return mds.PairResult(**base)

    def test_routine_drift_and_stale_need_nobody(self):
        results = [self._r(status="stale", stale_days=3), self._r(slug="b", status="drift", stale_days=5),
                   self._r(slug="g", status="drift", gated=True, stale_days=400)]
        self.assertEqual(mds.operator_reasons(results, sla_threshold=14), [])

    def test_leak_and_sla_breach_need_operator(self):
        results = [self._r(status="leak"), self._r(slug="b", status="drift", stale_days=30,
                                                   filed_ticket="T-1")]
        reasons = mds.operator_reasons(results, sla_threshold=14)
        self.assertEqual(len(reasons), 2)
        self.assertIn("leak", reasons[0])
        self.assertIn("30d", reasons[1])
        self.assertIn("T-1", reasons[1])

    def test_alert_is_short_and_carries_no_hashes(self):
        alert = mds.build_operator_alert(["possible leak in a (alert only, nothing filed)"])
        self.assertLessEqual(len(alert.splitlines()), 4)
        self.assertNotRegex(alert, r"[0-9a-f]{40}")
        self.assertIn("mailbox", alert)


class TestCountPaths(unittest.TestCase):
    def test_no_count_paths_counts_everything(self):
        self.assertIsNone(mds._count_paths({"slug": "s", "source": {"local_repo": "."}}))

    def test_seed_uses_the_seed_allowlist(self):
        from tools.neutralize_seed import seed_pathspecs
        paths = mds._count_paths({"slug": "s", "source": {"local_repo": ".", "count_paths": "seed"}})
        self.assertEqual(paths, seed_pathspecs())
        self.assertIn("rulebook", paths)
        self.assertNotIn("tickets", paths)
        self.assertNotIn("ledger", paths)

    def test_explicit_list_and_bad_value(self):
        self.assertEqual(mds._count_paths({"slug": "s", "source": {"count_paths": ["a", "b"]}}),
                         ["a", "b"])
        with self.assertRaises(ValueError):
            mds._count_paths({"slug": "s", "source": {"count_paths": 3}})

    def test_rev_list_passes_pathspecs(self):
        with mock.patch.object(mds, "_run_git") as rg:
            rg.return_value = mock.Mock(returncode=0, stdout="7\n")
            self.assertEqual(mds._rev_list_count(Path("."), "a", "b", paths=["rulebook"]), 7)
        self.assertEqual(rg.call_args[0][0], ["rev-list", "--count", "a..b", "--", "rulebook"])

    def test_live_pairs_file_scopes_the_public_mirror(self):
        pairs = {p["slug"]: p for p in mds.load_pairs(mds.DEFAULT_PAIRS_PATH)}
        self.assertEqual(pairs["pantheon-public-mirror"]["source"].get("count_paths"), "seed")



class TestAlertSurvivesOwnerFailure(_MainHarness):
    """recovery review of cc26b9c947: an owner-mailbox failure must never swallow
    the operator's leak/SLA alert or downgrade exit 4."""

    def _run(self, results, *, extra=("--commit-prod",), owner_effect=OSError("hub down")):
        tg = mock.MagicMock()
        with mock.patch.object(mds, "load_pairs", return_value=[_pair(slug=r.slug) for r in results]), \
             mock.patch.object(mds, "load_state", return_value=ms._empty_state()), \
             mock.patch.object(mds, "evaluate_pair", side_effect=results), \
             mock.patch.object(mds, "file_refresh_ticket", return_value="T-1"), \
             mock.patch.object(mds, "_send_telegram", tg), \
             mock.patch.object(mds, "_send_owner_digest", side_effect=owner_effect) as owner, \
             mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            rc = mds.main(["--no-fetch", "--tickets-root", str(self.tickets), *extra])
        return rc, tg, owner

    def test_leak_alert_fires_and_exit_4_survives_owner_failure(self):
        leak = mds.PairResult(slug="claude-harness-mirror", status="leak", leaks=1,
                              leak_findings=["LEAK a.md:1 [c:persona]"], detail="LEAK GATE FAILED")
        rc, tg, owner = self._run([leak])
        tg.assert_called_once()
        self.assertIn("leak", tg.call_args[0][0])
        owner.assert_called_once()
        self.assertEqual(rc, mds.EXIT_LEAK)

    def test_owner_failure_on_routine_week_exits_2(self):
        drift = mds.PairResult(slug="pwa-release-mirror", status="drift", detail="d")
        rc, tg, _ = self._run([drift])
        tg.assert_not_called()
        self.assertEqual(rc, mds.EXIT_CONFIG)

    def test_dry_run_sends_no_owner_digest(self):
        drift = mds.PairResult(slug="pwa-release-mirror", status="drift", detail="d")
        rc, _, owner = self._run([drift], extra=(), owner_effect=None)
        owner.assert_not_called()


class TestZeroScopedCommitsIsCurrent(unittest.TestCase):
    def test_zero_seed_commits_reports_no_drift(self):
        pair = {"slug": "pantheon-public-mirror", "gated": True, "gate_tickets": ["T-1327"],
                "source": {"local_repo": ".", "count_paths": "seed"}}
        state = {"pairs": {"pantheon-public-mirror": {"source_head_at_last_publish": "a" * 40}}}
        with mock.patch.object(mds, "read_source_head", return_value="b" * 40), \
             mock.patch.object(mds, "count_commits_behind", return_value=0):
            r = mds.evaluate_pair(pair, state, Path("/nonexistent"))
        self.assertEqual(r.status, "no_drift", r.detail)


if __name__ == "__main__":
    unittest.main()
