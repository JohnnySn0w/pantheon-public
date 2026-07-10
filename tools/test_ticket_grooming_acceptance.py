"""Unit tests for the T-0693 closed-ticket acceptance check in
tools/ticket_grooming.py.

Run from repo root:
    python3 -m unittest tools.test_ticket_grooming_acceptance -v

WHY this is a sibling module rather than appended to test_ticket_grooming.py:
the acceptance check is a self-contained subsystem (done-transition detection,
artifact extraction, mechanical verification, separate-scope refile commit)
with its own fixture shapes — keeping it separate keeps both files legible.

Fixture discipline (directives.md D11 — captured-real-shape): the ticket
fixtures below mirror the structure of a real done/ ticket. The transition-log
`resolved; <detail>` form with embedded file/SHA references is copied from
tickets/done/T-0008.md ("resolved; ... tools/handoff_and_restart.sh ... at
5aca27f ..."), and the frontmatter field order + `labels` flow-list form match
tools/ticket.py's serializer. We do NOT hand-invent a novel ticket shape.

Fixture-string note (HERMES byte-discipline): no test here needs the trigger
phrase, so none is constructed. The separate-scope commit test exercises the
real git_commit_and_push_reopen against a real tmp git repo, so the HERMES
file/message guards run for real over clean content.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tools import ticket_grooming


# ---------------------------------------------------------------------------
# Fixture helpers — captured-real-shape done/ ticket.
# ---------------------------------------------------------------------------


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# Pinned "now" used across tests; closed-recently tickets time their resolved
# entry relative to this.
NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


def _make_done_ticket(
    path: Path,
    *,
    tid: str,
    resolved_at: datetime,
    body_extra: str = "",
    labels: str = "[handoff, automation]",
    resolve_detail: str = "resolved; all deliverables landed.",
) -> None:
    """Write a done/ ticket whose transition log ends in a `resolved` entry.

    Shape mirrors tickets/done/T-0008.md: frontmatter with a `labels` flow
    list + `state: done`, an `## Ask` / `## Context` body, and a `## Transition
    log` ending in a `[<actor>] resolved; <detail>` bullet that may embed
    file/SHA claims. `body_extra` is spliced into the Ask section so tests can
    place claimed artifacts in realistic prose.
    """
    fm = "\n".join(
        [
            "---",
            f"id: {tid}",
            "created: 2026-05-20T00:00:00Z",
            "requester: operator",
            "target: orchestrator",
            "state: done",
            "parent_ticket: null",
            "depends_on: []",
            f"labels: {labels}",
            "stale_after: 7d",
            "---",
        ]
    )
    body = (
        f"\n# Example done ticket {tid}\n\n"
        "## Ask\n\n"
        "Do the thing.\n"
        f"{body_extra}\n\n"
        "## Context\n\n"
        "Because reasons.\n\n"
        "## Transition log\n\n"
        "- 2026-05-20T00:00:00Z [operator] filed in inbox\n"
        "- 2026-05-20T01:00:00Z [orchestrator] claimed; moving to active\n"
        f"- {_ts(resolved_at)} [orchestrator] {resolve_detail}\n"
    )
    path.write_text(fm + body, encoding="utf-8")


# ---------------------------------------------------------------------------
# find_done_transition.
# ---------------------------------------------------------------------------


class TestFindDoneTransition(unittest.TestCase):
    def _body(self, lines: list[str]) -> str:
        log = "\n".join(lines)
        return f"# T\n\n## Transition log\n\n{log}\n"

    def test_resolved_entry_returns_ts(self) -> None:
        body = self._body(
            [
                "- 2026-05-20T01:00:00Z [orchestrator] claimed; moving to active",
                "- 2026-05-29T05:00:00Z [orchestrator] resolved; landed it",
            ]
        )
        ts = ticket_grooming.find_done_transition(body)
        self.assertEqual(ts, datetime(2026, 5, 29, 5, 0, 0, tzinfo=timezone.utc))

    def test_literal_done_verb_returns_ts(self) -> None:
        body = self._body(
            ["- 2026-05-29T05:00:00Z [recovery] done; closed it out"]
        )
        ts = ticket_grooming.find_done_transition(body)
        self.assertEqual(ts, datetime(2026, 5, 29, 5, 0, 0, tzinfo=timezone.utc))

    def test_most_recent_close_wins(self) -> None:
        # reopen-then-reclose: the latest resolved entry is the close ts.
        body = self._body(
            [
                "- 2026-05-22T01:00:00Z [orchestrator] resolved; first close",
                "- 2026-05-24T01:00:00Z [grooming] reopened; acceptance unmet",
                "- 2026-05-29T05:00:00Z [orchestrator] resolved; reclosed",
            ]
        )
        ts = ticket_grooming.find_done_transition(body)
        self.assertEqual(ts, datetime(2026, 5, 29, 5, 0, 0, tzinfo=timezone.utc))

    def test_no_resolved_entry_returns_none(self) -> None:
        body = self._body(
            ["- 2026-05-20T01:00:00Z [orchestrator] claimed; moving to active"]
        )
        self.assertIsNone(ticket_grooming.find_done_transition(body))


# ---------------------------------------------------------------------------
# extract_artifacts — the conservative heuristic.
# ---------------------------------------------------------------------------


class TestExtractArtifacts(unittest.TestCase):
    def test_extracts_concrete_path(self) -> None:
        arts = ticket_grooming.extract_artifacts(
            "landed tools/handoff_and_restart.sh and the test."
        )
        self.assertIn(("tools/handoff_and_restart.sh", "path"), arts)

    def test_extracts_adr_path(self) -> None:
        arts = ticket_grooming.extract_artifacts("created adr/ORG-001-foo.md")
        self.assertIn(("adr/ORG-001-foo.md", "path"), arts)

    def test_extracts_sha_with_cue(self) -> None:
        arts = ticket_grooming.extract_artifacts("deliverable at 5aca27f via reviewer")
        self.assertIn(("5aca27f", "sha"), arts)

    def test_extracts_commit_sha_word_cue(self) -> None:
        arts = ticket_grooming.extract_artifacts("see commit 1234abcd for details")
        self.assertIn(("1234abcd", "sha"), arts)

    def test_glob_path_dropped(self) -> None:
        arts = ticket_grooming.extract_artifacts(
            "scans tickets/done/T-*.md for matches"
        )
        paths = [a for a, k in arts if k == "path"]
        self.assertEqual(paths, [])

    def test_placeholder_path_dropped(self) -> None:
        arts = ticket_grooming.extract_artifacts(
            "writes to path/to/<name>.py at runtime"
        )
        paths = [a for a, k in arts if k == "path"]
        self.assertEqual(paths, [])

    def test_ellipsis_path_dropped(self) -> None:
        arts = ticket_grooming.extract_artifacts(
            "lands under research/.../README.md eventually"
        )
        paths = [a for a, k in arts if k == "path"]
        self.assertEqual(paths, [])

    # --- T-0977: illustrative example-paths in resolution prose -------------
    def test_t0977_literal_t0973_line_not_extracted(self) -> None:
        # The exact line that false-reopened T-0973. Both path tokens are
        # parenthetical asides → no claimed paths.
        arts = ticket_grooming.extract_artifacts(
            "resolved; split approach: _split_joined_filename_list splits a "
            "slash-joined filename list (repos.yaml/instance.yaml) at each .ext/ "
            "boundary so each real file is verified separately; normal dir-sep "
            "paths (tools/foo.py) untouched."
        )
        paths = [a for a, k in arts if k == "path"]
        self.assertEqual(paths, [])

    # --- T-1056: slash-joined filename-list split — both the false-positive it
    # fixes (T-0882/T-0973) AND the .d/-drop-in true-negative the naive
    # "split on two extensions" approach would get WRONG. The split logic
    # (`_split_joined_filename_list` / `_EXT_SLASH_RE`) only splits at a
    # `.<knownext>/` boundary, so a drop-in directory segment (`*.d/`, or a
    # `.service.d/` whose intermediate `.service` segment LOOKS like a complete
    # file) is left intact. Regression guard so a future `_EXT_SLASH_RE` refactor
    # can't silently re-break drop-in paths.
    def test_t1056_split_true_positive(self) -> None:
        split = ticket_grooming._split_joined_filename_list
        self.assertEqual(split("repos.yaml/instance.yaml"),
                         ["repos.yaml", "instance.yaml"])
        self.assertEqual(split("a.yaml/b.yaml/c.json"),
                         ["a.yaml", "b.yaml", "c.json"])

    def test_t1056_dropin_dir_true_negative_not_split(self) -> None:
        split = ticket_grooming._split_joined_filename_list
        # `.d/` — `d` is not a known extension → no boundary → not split.
        self.assertEqual(split("settings.d/config.yaml"),
                         ["settings.d/config.yaml"])
        # `.service.d/` — the separator is `.d/`, NOT `.service/`; the
        # intermediate `.service` segment looks like a complete file but is a
        # directory prefix → must NOT split (the critical edge case).
        self.assertEqual(split("mosquitto.service.d/tailscale-ip-wait.conf"),
                         ["mosquitto.service.d/tailscale-ip-wait.conf"])
        # A normal nested path has no `.<ext>/` boundary → unchanged.
        self.assertEqual(split("a/b/c.yaml"), ["a/b/c.yaml"])

    def test_t1056_split_end_to_end_through_extract(self) -> None:
        # The T-0882 false-reopen case: a slash-joined list on a landing-cue
        # line yields BOTH files as separate path candidates...
        arts = ticket_grooming.extract_artifacts("added repos.yaml/instance.yaml")
        paths = [a for a, k in arts if k == "path"]
        self.assertIn("repos.yaml", paths)
        self.assertIn("instance.yaml", paths)
        # ...while a drop-in path stays a single candidate (not mis-split).
        arts = ticket_grooming.extract_artifacts("created settings.d/config.yaml")
        paths = [a for a, k in arts if k == "path"]
        self.assertEqual(paths, ["settings.d/config.yaml"])

    def test_t0977_parenthetical_aside_path_dropped(self) -> None:
        # Sole path token is a parenthetical aside on a landing-cue line.
        arts = ticket_grooming.extract_artifacts(
            "landed at boot (tools/foo.py) for clarity"
        )
        paths = [a for a, k in arts if k == "path"]
        self.assertEqual(paths, [])

    def test_t0977_untouched_cue_drops_inline_path(self) -> None:
        # Non-parenthetical, but "untouched" marks it the unaffected case.
        arts = ticket_grooming.extract_artifacts(
            "created at startup; tools/foo.py untouched"
        )
        paths = [a for a, k in arts if k == "path"]
        self.assertEqual(paths, [])

    def test_t0977_inline_real_claim_still_extracted(self) -> None:
        # Regression: a genuinely-landed inline path is STILL mined.
        arts = ticket_grooming.extract_artifacts(
            "resolved; created tools/loader.py and wired it in at startup."
        )
        self.assertIn(("tools/loader.py", "path"), arts)

    def test_t0977_mixed_line_keeps_inline_real(self) -> None:
        # An inline real claim PLUS a parenthetical aside on one line: the
        # inline path is kept AND (T-0819 path-granular guard) the parenthetical
        # aside is now dropped — the line-level "ALL paths parenthetical"
        # predicate no longer leaves the aside as a conservative residual.
        arts = ticket_grooming.extract_artifacts(
            "landed tools/real.py (mirrors tools/foo.py pattern) at boot."
        )
        paths = [a for a, k in arts if k == "path"]
        self.assertIn("tools/real.py", paths)
        self.assertNotIn("tools/foo.py", paths)

    def test_t0819_parenthetical_meta_path_dropped_mixed_line(self) -> None:
        # T-0819 class-3 regression (the real T-0751 shape): a long re-resolve
        # bullet names a genuine inline deliverable AND, in a parenthetical
        # aside, a path belonging to ANOTHER ticket ("that one is Orchestrator's").
        # The line-level all-parenthetical guard does NOT fire (an inline path
        # co-exists), so before the per-path guard the meta-path leaked and
        # false-reopened the ticket. It must now be dropped while the genuine
        # inline claim survives.
        # NB: deliberately avoids the "untouched"/"left alone" illustrative cues
        # so this isolates the PARENTHETICAL guard, not the line-level cue.
        line = (
            "resolved; guide landed at sop/standard/pantheon-bringup.md; note "
            "T-0752 was also reopened (tools/ledger.py path), handled separately."
        )
        paths = [a for a, k in ticket_grooming.extract_artifacts(line) if k == "path"]
        self.assertIn("sop/standard/pantheon-bringup.md", paths)
        self.assertNotIn("tools/ledger.py", paths)

    def test_t0819_only_final_resolve_bullet_scanned(self) -> None:
        # T-0819: in a reopen->re-resolve history only the FINAL close bullet is
        # the closer's authoritative claim. A path named ONLY in a superseded
        # earlier resolve bullet is NOT mined (it accumulates stale/other-ticket
        # narration); the final bullet's genuine claim still is.
        body = (
            "# T\n\n## Transition log\n\n"
            "- 2026-06-01T00:00:00Z [a] resolved; created tools/superseded_old.py\n"
            "- 2026-06-02T00:00:00Z [a] resolved; created tools/final_real.py at boot\n"
        )
        paths = [a for a, k in ticket_grooming.extract_artifacts(body) if k == "path"]
        self.assertIn("tools/final_real.py", paths)
        self.assertNotIn("tools/superseded_old.py", paths)

    def test_t0819_acceptance_section_still_scanned(self) -> None:
        # Regression: the `## Acceptance` section remains part of the claim
        # region alongside the final resolve bullet.
        body = (
            "# T\n\n## Acceptance\n\nLanded tools/accept_me.py at startup.\n\n"
            "## Transition log\n\n"
            "- 2026-06-02T00:00:00Z [a] resolved; done\n"
        )
        paths = [a for a, k in ticket_grooming.extract_artifacts(body) if k == "path"]
        self.assertIn("tools/accept_me.py", paths)

    def test_t0819_paren_helpers(self) -> None:
        # Unit-level coverage of the extracted span helpers.
        line = "created a/b.py (see c/d.py) at boot"
        spans = ticket_grooming._paren_spans(line)
        self.assertEqual(len(spans), 1)
        # the parenthetical path c/d.py is inside the span; the inline a/b.py is not
        cd = line.index("c/d.py")
        ab = line.index("a/b.py")
        self.assertTrue(ticket_grooming._span_in_parens(spans, cd, cd + len("c/d.py")))
        self.assertFalse(ticket_grooming._span_in_parens(spans, ab, ab + len("a/b.py")))

    def test_illustrative_eg_line_dropped(self) -> None:
        arts = ticket_grooming.extract_artifacts(
            "e.g. tools/example_helper.py would be a good home"
        )
        paths = [a for a, k in arts if k == "path"]
        self.assertEqual(paths, [])

    def test_recommend_line_dropped(self) -> None:
        arts = ticket_grooming.extract_artifacts(
            "recommend adding sop/standard/new-thing.md later"
        )
        paths = [a for a, k in arts if k == "path"]
        self.assertEqual(paths, [])

    def test_bare_filename_not_extracted(self) -> None:
        # No slash -> not treated as a repo-relative path claim.
        arts = ticket_grooming.extract_artifacts("touched README.md somewhere")
        self.assertEqual(arts, [])

    def test_arbitrary_hex_without_cue_not_extracted(self) -> None:
        arts = ticket_grooming.extract_artifacts(
            "the colour was deadbeef in the palette"
        )
        shas = [a for a, k in arts if k == "sha"]
        self.assertEqual(shas, [])

    def test_dedup(self) -> None:
        arts = ticket_grooming.extract_artifacts(
            "landed tools/x.py and again landed tools/x.py"
        )
        self.assertEqual(arts.count(("tools/x.py", "path")), 1)

    def test_prose_only_no_artifacts(self) -> None:
        arts = ticket_grooming.extract_artifacts(
            "Acceptance: the system behaves correctly and the operator is happy."
        )
        self.assertEqual(arts, [])

    def test_path_without_landing_cue_not_extracted(self) -> None:
        # Path referenced but NOT claimed-landed (no created/landed/at cue) ->
        # treated as a mere reference (work-to-do / cross-repo), not a claim.
        arts = ticket_grooming.extract_artifacts(
            "Fix: in the frontend (src-frontend/index.html), add a row."
        )
        self.assertEqual([a for a, k in arts if k == "path"], [])

    def test_disjunctive_or_line_dropped(self) -> None:
        # "documented in X or Y" is a suggestion among options, not a claim.
        arts = ticket_grooming.extract_artifacts(
            "explicit exclusion documented in pantheon-config/README.md or a comment"
        )
        self.assertEqual([a for a, k in arts if k == "path"], [])

    def test_cross_repo_path_skipped_when_repo_root_given(self) -> None:
        # Top-level dir absent in repo_root -> cross-repo artifact -> skipped.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()  # only 'tools/' exists
            arts = ticket_grooming.extract_artifacts(
                "landed src-frontend/index.html in the demo app", root
            )
            self.assertEqual([a for a, k in arts if k == "path"], [])
            # In-repo path still extracted.
            arts2 = ticket_grooming.extract_artifacts(
                "landed tools/real.py", root
            )
            self.assertIn(("tools/real.py", "path"), arts2)

    def test_sha_on_pr_branch_line_skipped(self) -> None:
        # A SHA on a line mentioning a PR/agent-branch is merge-pending, not a
        # mis-close -> not extracted.
        arts = ticket_grooming.extract_artifacts(
            "implementer commit 5ba1d52 pushed agent/T-0642; PR #63 open"
        )
        self.assertEqual([a for a, k in arts if k == "sha"], [])

    def test_path_on_pr_line_skipped(self) -> None:
        # T-0683 shape: a path claimed in an echo-prismatic PR line collides
        # with pantheon-config's own tools/ dir; PR mention -> skip.
        arts = ticket_grooming.extract_artifacts(
            "PR #74 squash-merged; tools/install-hooks.sh activates via core.hooksPath"
        )
        self.assertEqual([a for a, k in arts if k == "path"], [])

    def test_path_on_revenue_project_line_skipped(self) -> None:
        # T-0549 shape: a revenue-project deliverable referenced by a BARE path
        # (no revenue-project/ prefix → top-dir guard can't catch it) whose top
        # dir (`notes/`) collides with pantheon-config's own → must be skipped
        # because the line names the sibling repo `revenue-project`. Pre-fix
        # (revenue-project absent from _OTHER_REPO_MARKERS) this false-reopened a
        # correctly-resolved ticket.
        arts = ticket_grooming.extract_artifacts(
            "reframe doc notes/2026-05-22-financial-instrument-reframe.md "
            "-> RATIFIED (revenue-project commit 7b7e0b1)"
        )
        self.assertEqual([a for a, k in arts if k == "path"], [])

    def test_path_on_claude_config_line_skipped(self) -> None:
        # T-1489 shape (false-reopened T-1443): a claude-config deliverable whose
        # top dir (`pinned-rules/`) COLLIDES with pantheon-config's own same-named
        # dir — so the top-level-dir guard is fooled (the dir exists here, but the
        # file `orchestrator.md` lives in claude-config). The line names the sibling repo
        # `claude-config`, which is the reliable "resolve against the other repo"
        # signal. Pre-fix (claude-config absent from _OTHER_REPO_MARKERS) this
        # false-reopened a correctly-resolved ticket.
        # NOTE: called with repo_root=None, so the top-level-dir guard never
        # fires here — this test isolates the `_line_mentions_other_repo` marker
        # fix specifically (the real-world dir collision above is precisely WHY
        # that line-level guard, not the top-dir guard, is the load-bearing fix).
        arts = ticket_grooming.extract_artifacts(
            "resolved; correction committed to claude-config "
            "pinned-rules/orchestrator.md, symlinked as the auto-loaded CLAUDE.md"
        )
        self.assertEqual([a for a, k in arts if k == "path"], [])

    def test_mobile_ticket_path_skipped(self) -> None:
        # T-0667 shape: a claimed tickets/<state>/T-NNNN.md path is mobile and
        # not a deliverable artifact -> never extracted.
        arts = ticket_grooming.extract_artifacts(
            "scope clean: created tickets/inbox/T-0678.md follow-up"
        )
        self.assertEqual([a for a, k in arts if k == "path"], [])

    # --- regression: the `at` cue must be word-bounded (T-0693 review) ---
    # A prose word ending in "-at" + a hex token must NOT extract a SHA, and a
    # path on such a line must NOT be treated as landed. Pre-fix, the bare "at"
    # substring/alternative matched "that"/"combat"/"format" -> false reopen.
    def test_word_ending_at_does_not_extract_sha(self) -> None:
        self.assertEqual(
            [a for a, k in ticket_grooming.extract_artifacts(
                "resolved; that 7f3a9b1 worked") if k == "sha"],
            [],
        )
        self.assertEqual(
            [a for a, k in ticket_grooming.extract_artifacts(
                "resolved; combat 0000000 fixed") if k == "sha"],
            [],
        )

    def test_word_ending_at_is_not_a_landing_cue_for_paths(self) -> None:
        # "format" / "that" contain "at" but are not the landing cue "at".
        self.assertEqual(
            [a for a, k in ticket_grooming.extract_artifacts(
                "format tools/x.py output") if k == "path"],
            [],
        )

    def test_standalone_at_cue_still_works(self) -> None:
        # Positive control: the legit "at <path>" / "at <sha>" cue must survive
        # the word-boundary fix.
        path_arts = ticket_grooming.extract_artifacts("delivered at tools/foo.py")
        self.assertIn(("tools/foo.py", "path"), path_arts)
        sha_arts = ticket_grooming.extract_artifacts("landed at 5aca27f via review")
        self.assertIn(("5aca27f", "sha"), sha_arts)


# ---------------------------------------------------------------------------
# _claim_region scoping — Ask/Context excluded; resolved bullet + Acceptance in.
# ---------------------------------------------------------------------------


class TestClaimRegion(unittest.TestCase):
    def test_ask_context_paths_excluded(self) -> None:
        body = (
            "# T\n\n"
            "## Ask\n\n"
            "Create tools/should_not_count.py to do the thing.\n\n"
            "## Context\n\n"
            "Related to tools/also_not.py historically.\n\n"
            "## Transition log\n\n"
            "- 2026-05-29T05:00:00Z [recovery] resolved; landed tools/the_real_one.py\n"
        )
        arts = ticket_grooming.extract_artifacts(body)
        paths = [a for a, k in arts if k == "path"]
        self.assertIn("tools/the_real_one.py", paths)
        self.assertNotIn("tools/should_not_count.py", paths)
        self.assertNotIn("tools/also_not.py", paths)

    def test_acceptance_section_included(self) -> None:
        body = (
            "# T\n\n"
            "## Ask\n\nstuff\n\n"
            "## Acceptance\n\n"
            "- created tools/accept_artifact.py\n\n"
            "## Transition log\n\n"
            "- 2026-05-29T05:00:00Z [recovery] resolved; done\n"
        )
        arts = ticket_grooming.extract_artifacts(body)
        self.assertIn(("tools/accept_artifact.py", "path"), arts)


# ---------------------------------------------------------------------------
# check_acceptance — disk + git verification.
# ---------------------------------------------------------------------------


class TestCheckAcceptance(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "tools").mkdir()
        (self.root / "tools" / "exists.py").write_text("x", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_existing_path_passes(self) -> None:
        self.assertIsNone(
            ticket_grooming.check_acceptance("landed tools/exists.py", self.root)
        )

    def test_missing_path_fails(self) -> None:
        miss = ticket_grooming.check_acceptance(
            "landed tools/never_made.py", self.root
        )
        self.assertEqual(miss, ("tools/never_made.py", "path"))

    def test_prose_only_passes_silently(self) -> None:
        self.assertIsNone(
            ticket_grooming.check_acceptance(
                "Acceptance: behaves correctly.", self.root
            )
        )

    def test_first_missing_reported(self) -> None:
        miss = ticket_grooming.check_acceptance(
            "landed tools/exists.py and tools/never_made.py", self.root
        )
        # exists.py verifies; never_made.py is the first failure.
        self.assertEqual(miss, ("tools/never_made.py", "path"))


class TestCheckAcceptanceLedgerMigration(unittest.TestCase):
    """T-0817: a flat `ledger/<year>.json[l]` reference must NOT false-reopen
    once the T-0148 weekly-shard layout exists; a genuinely-missing ledger
    (no shards, no archive for that year) still flags. The prose `.jsonl`
    extracts as `.json` via _PATH_RE, so assertions tolerate both forms."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "ledger").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_passes_when_weekly_shard_dir_exists(self) -> None:
        (self.root / "ledger" / "2026-W18").mkdir()
        # Faithful to the real T-0778 case: prose mentions the old flat path on
        # a line carrying a landing cue, so the path is extracted, then resolved
        # to the migrated layout rather than flagged.
        self.assertIsNone(
            ticket_grooming.check_acceptance(
                "landed freshness pass; fixed ledger/2026.jsonl in 3 docs", self.root
            )
        )

    def test_passes_when_archive_snapshot_exists(self) -> None:
        (self.root / "ledger" / "2026.jsonl.archive-pre-d10-shard").write_text(
            "x", encoding="utf-8"
        )
        self.assertIsNone(
            ticket_grooming.check_acceptance("landed ledger/2026.jsonl", self.root)
        )

    def test_json_variant_recognized(self) -> None:
        (self.root / "ledger" / "2026-W23").mkdir()
        self.assertIsNone(
            ticket_grooming.check_acceptance("landed ledger/2026.json", self.root)
        )

    def test_genuinely_missing_ledger_still_flags(self) -> None:
        # No shard dirs, no archive snapshot -> the ledger really is missing.
        miss = ticket_grooming.check_acceptance("landed ledger/2026.jsonl", self.root)
        self.assertIsNotNone(miss)
        self.assertTrue(miss[0].startswith("ledger/2026.json"))

    def test_other_year_shards_do_not_satisfy(self) -> None:
        # Shards exist for 2025 but the referenced year (2026) has none -> flags.
        (self.root / "ledger" / "2025-W40").mkdir()
        miss = ticket_grooming.check_acceptance("landed ledger/2026.jsonl", self.root)
        self.assertIsNotNone(miss)
        self.assertTrue(miss[0].startswith("ledger/2026.json"))


class TestCheckAcceptanceSha(unittest.TestCase):
    """SHA verification needs a real git repo so `git cat-file -e <sha>^{commit}`
    resolves against an object store.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._run("git", "init", "-q")
        self._run("git", "config", "user.email", "t@t")
        self._run("git", "config", "user.name", "t")
        (self.root / "f.txt").write_text("hello", encoding="utf-8")
        self._run("git", "add", "f.txt")
        self._run("git", "commit", "-q", "-m", "seed")
        self.real_sha = self._run("git", "rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> str:
        return subprocess.run(
            list(args), cwd=self.root, capture_output=True, text=True, check=False
        ).stdout

    def test_existing_sha_passes(self) -> None:
        short = self.real_sha[:8]
        self.assertIsNone(
            ticket_grooming.check_acceptance(f"landed at {short}", self.root)
        )

    def test_missing_sha_indeterminate_not_flagged(self) -> None:
        # T-0697: an absent SHA is INDETERMINATE — cherry-pick / squash / rebase
        # relocate SHAs while the content still landed, so check_acceptance must
        # NOT flag it. Only a missing PATH (a concrete on-disk deliverable)
        # triggers a reopen. (Was test_missing_sha_fails asserting the
        # pre-T-0697 flag-on-missing-SHA behavior — stale, left red under the
        # pytest-no-op; realigned to the intended behavior.)
        bogus = "0123456abcdef"
        self.assertIsNone(
            ticket_grooming.check_acceptance(f"landed at {bogus}", self.root)
        )


class TestCheckAcceptanceRelocated(unittest.TestCase):
    """T-0818: a missing PATH that git has history for was relocated / renamed /
    deleted (INDETERMINATE), not never-created. Needs a real git repo so
    `git log` resolves against history."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._run("git", "init", "-q")
        self._run("git", "config", "user.email", "t@t")
        self._run("git", "config", "user.name", "t")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> str:
        return subprocess.run(
            list(args), cwd=self.root, capture_output=True, text=True, check=False
        ).stdout

    def _commit(self, msg: str) -> None:
        self._run("git", "add", "-A")
        self._run("git", "commit", "-q", "-m", msg)

    def test_deleted_path_is_indeterminate(self) -> None:
        # Create + commit, then delete + commit -> old path missing but tracked.
        # Mirrors T-0751: a resolve note naming a since-deleted path.
        (self.root / "docs").mkdir()
        f = self.root / "docs" / "guide.md"
        f.write_text("x", encoding="utf-8")
        self._commit("add guide")
        f.unlink()
        self._commit("delete guide")
        self.assertIsNone(
            ticket_grooming.check_acceptance("landed docs/guide.md", self.root)
        )

    def test_renamed_path_is_indeterminate(self) -> None:
        # Create + commit, then git mv -> old path missing but has history.
        (self.root / "docs").mkdir()
        (self.root / "docs" / "old.md").write_text("x", encoding="utf-8")
        self._commit("add old")
        self._run("git", "mv", "docs/old.md", "docs/new.md")
        self._commit("rename old->new")
        self.assertIsNone(
            ticket_grooming.check_acceptance("landed docs/old.md", self.root)
        )

    def test_never_tracked_path_still_flags(self) -> None:
        # tools/ exists (passes the cross-repo guard) but tools/never_made.py was
        # never created or committed -> a genuine never-landed deliverable.
        (self.root / "tools").mkdir()
        (self.root / "tools" / "real.py").write_text("x", encoding="utf-8")
        self._commit("seed tools")
        miss = ticket_grooming.check_acceptance("landed tools/never_made.py", self.root)
        self.assertEqual(miss, ("tools/never_made.py", "path"))

    def test_existing_committed_path_passes(self) -> None:
        # Exists on disk -> _artifact_exists short-circuits before the git check.
        (self.root / "tools").mkdir()
        (self.root / "tools" / "real.py").write_text("x", encoding="utf-8")
        self._commit("add real")
        self.assertIsNone(
            ticket_grooming.check_acceptance("landed tools/real.py", self.root)
        )


# ---------------------------------------------------------------------------
# refile_done_to_inbox + _scan_done_for_acceptance (filesystem behavior).
# ---------------------------------------------------------------------------


class TestScanAndRefile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.tickets_root = self.repo_root / "tickets"
        (self.tickets_root / "done").mkdir(parents=True)
        (self.tickets_root / "inbox").mkdir(parents=True)
        (self.repo_root / "tools").mkdir()
        (self.repo_root / "tools" / "real.py").write_text("x", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scan(self, *, apply: bool) -> list[ticket_grooming.ReopenedTicket]:
        return ticket_grooming._scan_done_for_acceptance(
            NOW, self.repo_root, self.tickets_root, apply_refile=apply
        )

    # (a) recently-closed with a missing claimed file -> reopened.
    def test_missing_file_reopened(self) -> None:
        p = self.tickets_root / "done" / "T-0148.md"
        _make_done_ticket(
            p, tid="T-0148", resolved_at=NOW - timedelta(hours=3),
            resolve_detail="resolved; migration landed at tools/missing_migration.py",
        )
        reopened = self._scan(apply=True)
        self.assertEqual(len(reopened), 1)
        self.assertEqual(reopened[0].tid, "T-0148")
        self.assertEqual(reopened[0].missing_artifact, "tools/missing_migration.py")
        # File moved done -> inbox.
        self.assertFalse(p.exists())
        moved = self.tickets_root / "inbox" / "T-0148.md"
        self.assertTrue(moved.exists())
        text = moved.read_text(encoding="utf-8")
        # Label added, state flipped, marker appended.
        self.assertIn("acceptance-unmet", text)
        self.assertIn("state: inbox", text)
        self.assertIn("[grooming] reopened:", text)
        self.assertIn("tools/missing_migration.py missing", text)

    # (b) recently-closed whose claimed artifacts all exist -> NOT reopened.
    def test_existing_file_not_reopened(self) -> None:
        p = self.tickets_root / "done" / "T-0200.md"
        _make_done_ticket(
            p, tid="T-0200", resolved_at=NOW - timedelta(hours=3),
            resolve_detail="resolved; landed tools/real.py",
        )
        reopened = self._scan(apply=True)
        self.assertEqual(reopened, [])
        self.assertTrue(p.exists())  # untouched

    # (c) prose-only acceptance -> skipped, not flagged.
    def test_prose_only_skipped(self) -> None:
        p = self.tickets_root / "done" / "T-0201.md"
        _make_done_ticket(
            p, tid="T-0201", resolved_at=NOW - timedelta(hours=3),
            resolve_detail="resolved; behaves correctly and the operator confirmed",
        )
        reopened = self._scan(apply=True)
        self.assertEqual(reopened, [])
        self.assertTrue(p.exists())

    # (d) closed >24h ago -> not scanned.
    def test_closed_too_long_ago_not_scanned(self) -> None:
        p = self.tickets_root / "done" / "T-0202.md"
        _make_done_ticket(
            p, tid="T-0202", resolved_at=NOW - timedelta(hours=30),
            resolve_detail="resolved; landed at tools/also_missing.py",
        )
        reopened = self._scan(apply=True)
        self.assertEqual(reopened, [])
        self.assertTrue(p.exists())

    # (e) idempotency: already-labeled acceptance-unmet -> not re-processed.
    def test_already_labeled_not_reprocessed(self) -> None:
        p = self.tickets_root / "done" / "T-0203.md"
        _make_done_ticket(
            p, tid="T-0203", resolved_at=NOW - timedelta(hours=3),
            labels="[handoff, acceptance-unmet]",
            resolve_detail="resolved; landed at tools/missing2.py",
        )
        reopened = self._scan(apply=True)
        self.assertEqual(reopened, [])
        self.assertTrue(p.exists())  # not moved

    def test_dry_run_no_file_changes(self) -> None:
        p = self.tickets_root / "done" / "T-0148.md"
        _make_done_ticket(
            p, tid="T-0148", resolved_at=NOW - timedelta(hours=3),
            resolve_detail="resolved; landed at tools/missing_migration.py",
        )
        original = p.read_text(encoding="utf-8")
        reopened = self._scan(apply=False)
        self.assertEqual(len(reopened), 1)
        # No file touched in dry mode.
        self.assertTrue(p.exists())
        self.assertEqual(p.read_text(encoding="utf-8"), original)
        self.assertFalse((self.tickets_root / "inbox" / "T-0148.md").exists())
        # new_path points at the prospective inbox location.
        self.assertEqual(
            reopened[0].new_path, self.tickets_root / "inbox" / "T-0148.md"
        )

    # (g) SHA reference exists vs missing — exercised via a real git repo.
    def test_missing_sha_not_reopened(self) -> None:
        # T-0697: a closed ticket whose only artifact is an absent SHA is NOT
        # reopened — absent SHAs are INDETERMINATE (relocated by cherry-pick /
        # squash / rebase), never a reopen trigger; only a missing PATH reopens.
        # (Was test_missing_sha_reopened asserting the pre-T-0697 behavior —
        # stale, left red under the pytest-no-op; realigned.)
        p = self.tickets_root / "done" / "T-0210.md"
        _make_done_ticket(
            p, tid="T-0210", resolved_at=NOW - timedelta(hours=3),
            resolve_detail="resolved; shipped at deadbee123",
        )
        # repo_root is not a git repo here, so cat-file fails -> SHA absent.
        reopened = self._scan(apply=True)
        self.assertEqual(reopened, [])


class TestScanShaExists(unittest.TestCase):
    """(g) the existing-SHA half: a claimed SHA that resolves -> not reopened."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.tickets_root = self.repo_root / "tickets"
        (self.tickets_root / "done").mkdir(parents=True)
        (self.tickets_root / "inbox").mkdir(parents=True)
        for a in (
            ("git", "init", "-q"),
            ("git", "config", "user.email", "t@t"),
            ("git", "config", "user.name", "t"),
        ):
            subprocess.run(list(a), cwd=self.repo_root, capture_output=True, check=False)
        (self.repo_root / "seed.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "seed.txt"], cwd=self.repo_root, capture_output=True, check=False)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=self.repo_root, capture_output=True, check=False)
        self.sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_root,
            capture_output=True, text=True, check=False,
        ).stdout.strip()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_existing_sha_not_reopened(self) -> None:
        p = self.tickets_root / "done" / "T-0211.md"
        _make_done_ticket(
            p, tid="T-0211", resolved_at=NOW - timedelta(hours=3),
            resolve_detail=f"resolved; shipped at {self.sha[:10]}",
        )
        reopened = ticket_grooming._scan_done_for_acceptance(
            NOW, self.repo_root, self.tickets_root, apply_refile=True
        )
        self.assertEqual(reopened, [])
        self.assertTrue(p.exists())


# ---------------------------------------------------------------------------
# (f) separate-scope commit: stages only tickets/done|inbox; scope guard OK.
# ---------------------------------------------------------------------------


class TestReopenCommitScope(unittest.TestCase):
    """git_commit_and_push_reopen against a REAL tmp git repo with a bare origin.

    Asserts: the refile (done deletion + inbox addition) commits cleanly and
    the only staged paths are under tickets/done/ + tickets/inbox/. Also covers
    the negative: an out-of-scope staged path is refused + reset.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.origin = base / "origin.git"
        self.repo = base / "repo"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=False, capture_output=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=False, capture_output=True)
        self._g("config", "user.email", "t@t")
        self._g("config", "user.name", "t")
        self._g("config", "init.defaultBranch", "main")
        self._g("checkout", "-q", "-b", "main")
        # Seed: a done ticket + empty inbox dir tracked via .gitkeep.
        (self.repo / "tickets" / "done").mkdir(parents=True)
        (self.repo / "tickets" / "inbox").mkdir(parents=True)
        (self.repo / "tickets" / "inbox" / ".gitkeep").write_text("", encoding="utf-8")
        self.done_ticket = self.repo / "tickets" / "done" / "T-0300.md"
        _make_done_ticket(
            self.done_ticket, tid="T-0300", resolved_at=NOW - timedelta(hours=2),
            resolve_detail="resolved; landed tools/whatever.py",
        )
        self._g("add", "-A")
        self._g("commit", "-q", "-m", "seed")
        self._g("remote", "add", "origin", str(self.origin))
        self._g("push", "-q", "origin", "main")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _g(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True, check=False
        )

    def test_refile_commit_stages_only_done_inbox(self) -> None:
        # Perform the refile against the real repo's tickets dir.
        tickets_root = self.repo / "tickets"
        new_path = ticket_grooming.refile_done_to_inbox(
            self.done_ticket, "tools/whatever.py", NOW, tickets_root
        )
        self.assertTrue(new_path.exists())
        self.assertFalse(self.done_ticket.exists())

        msg = ticket_grooming._build_reopen_commit_message(
            "2026-05-29",
            [
                ticket_grooming.ReopenedTicket(
                    tid="T-0300", title="t",
                    missing_artifact="tools/whatever.py", missing_kind="path",
                    new_path=new_path,
                )
            ],
        )
        # cwd is the real repo (shared-tree path); fetch+merge will run against
        # the bare origin we set up.
        ok, detail = ticket_grooming.git_commit_and_push_reopen(
            [new_path], msg, cwd=self.repo,
        )
        self.assertTrue(ok, f"expected success; got {detail!r}")
        # The HEAD commit's changed files: only under tickets/done|inbox.
        files = self._g(
            "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
        ).stdout.split()
        self.assertTrue(files, "commit should have changed files")
        for f in files:
            self.assertTrue(
                f.startswith("tickets/done/") or f.startswith("tickets/inbox/"),
                f"out-of-scope path committed: {f}",
            )
        # The rename specifically: done deletion + inbox addition both present.
        self.assertIn("tickets/done/T-0300.md", files)
        self.assertIn("tickets/inbox/T-0300.md", files)

    def test_out_of_scope_staged_path_refused(self) -> None:
        # Stage an out-of-scope file BEFORE calling; the function's `git add`
        # narrows to done/inbox, but a pre-staged stray must be caught by the
        # readback scope guard and reset.
        stray = self.repo / "stray.txt"
        stray.write_text("x", encoding="utf-8")
        self._g("add", "stray.txt")
        # Also do a real refile so there's an in-scope change too.
        tickets_root = self.repo / "tickets"
        new_path = ticket_grooming.refile_done_to_inbox(
            self.done_ticket, "tools/whatever.py", NOW, tickets_root
        )
        ok, detail = ticket_grooming.git_commit_and_push_reopen(
            [new_path], "tickets(grooming): reopen 1 — 2026-05-29\n", cwd=self.repo,
        )
        self.assertFalse(ok)
        self.assertIn("scope", detail.lower())
        # No commit landed (HEAD still at seed).
        log = self._g("log", "--oneline").stdout
        self.assertEqual(log.strip().count("\n"), 0)  # single seed commit


# ---------------------------------------------------------------------------
# T-0769: clause-level negation guard.
# ---------------------------------------------------------------------------


class TestNegationGuard(unittest.TestCase):
    """Verify that _path_is_negated_in_clause and its integration into
    extract_artifacts correctly drops negated paths while preserving
    co-claimed legitimate paths on the same line.
    """

    # (a) Live T-0752 shape: one bullet, negated path + legit co-claimed path
    #     on the same line.  Only the legit path must be extracted; the negated
    #     one must be dropped.
    def test_t0752_shape_negated_path_dropped_legit_path_kept(self) -> None:
        line = "Verified: NO tools/ledger.py exists; landed sop/foo.md commit abc123"
        arts = ticket_grooming.extract_artifacts(line)
        paths = [a for a, k in arts if k == "path"]
        self.assertIn("sop/foo.md", paths, "legit co-claimed path must be extracted")
        self.assertNotIn("tools/ledger.py", paths, "negated path must be dropped")

    # (b) Negation-only line — no paths extracted.
    def test_negation_only_line_empty(self) -> None:
        line = "Verified: does not exist tools/ledger.py (never created)"
        arts = ticket_grooming.extract_artifacts(line)
        paths = [a for a, k in arts if k == "path"]
        self.assertEqual(paths, [], "negated path on a negation-only line must yield empty")

    # (c) Plain claim with no negation — still extracted (regression guard).
    def test_plain_claim_no_negation_extracted(self) -> None:
        arts = ticket_grooming.extract_artifacts("landed tools/foo.py at commit abc1234")
        paths = [a for a, k in arts if k == "path"]
        self.assertIn("tools/foo.py", paths, "non-negated claim must still be extracted")

    # (d) Word-boundary: trap word BEFORE path in same clause must NOT suppress.
    #     "another" contains "not" as a substring; "landed another file tools/foo.py"
    #     must still extract tools/foo.py because \bnot\b does not match inside
    #     "another".
    def test_word_boundary_another_does_not_suppress(self) -> None:
        line = "landed another file tools/foo.py"
        arts = ticket_grooming.extract_artifacts(line)
        paths = [a for a, k in arts if k == "path"]
        self.assertIn(
            "tools/foo.py", paths,
            "embedded 'not' in 'another' must not suppress the path via word-boundary"
        )

    # Additional coverage: "no longer" phrasing drops the path.
    def test_no_longer_phrasing_drops_path(self) -> None:
        line = "landed tools/foo.py; no longer landed tools/old.py"
        arts = ticket_grooming.extract_artifacts(line)
        paths = [a for a, k in arts if k == "path"]
        self.assertIn("tools/foo.py", paths, "pre-semicolon path must be kept")
        self.assertNotIn("tools/old.py", paths, "'no longer' must drop the path")

    # "never" phrasing drops the path.
    def test_never_phrasing_drops_path(self) -> None:
        line = "landed tools/real.py; never created tools/ghost.py"
        arts = ticket_grooming.extract_artifacts(line)
        paths = [a for a, k in arts if k == "path"]
        self.assertIn("tools/real.py", paths)
        self.assertNotIn("tools/ghost.py", paths)

    # Negation after the path (different clause) must NOT suppress.
    def test_negation_after_path_does_not_suppress(self) -> None:
        # The path comes before the clause delimiter; the negation follows it.
        # "landed tools/foo.py — not thoroughly tested yet" should still extract.
        line = "landed tools/foo.py — not thoroughly tested yet"
        arts = ticket_grooming.extract_artifacts(line)
        paths = [a for a, k in arts if k == "path"]
        self.assertIn(
            "tools/foo.py", paths,
            "negation AFTER the path in a later clause must not suppress it"
        )

    # Direct unit test on the helper itself.
    def test_helper_no_negation_returns_false(self) -> None:
        line = "landed tools/foo.py"
        idx = line.index("tools/")
        self.assertFalse(ticket_grooming._path_is_negated_in_clause(line, idx))

    def test_helper_negation_before_path_returns_true(self) -> None:
        line = "Verified: NO tools/ledger.py exists"
        idx = line.index("tools/")
        self.assertTrue(ticket_grooming._path_is_negated_in_clause(line, idx))

    def test_helper_negation_in_prior_clause_returns_false(self) -> None:
        # "NO" is before the semicolon; path is after it — different clause.
        line = "NO match found; landed tools/foo.py"
        idx = line.index("tools/")
        self.assertFalse(ticket_grooming._path_is_negated_in_clause(line, idx))


if __name__ == "__main__":
    unittest.main()
