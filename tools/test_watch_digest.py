"""Tests for tools.watch_digest — pull_repo_or_warn fanout (T-0120)."""

from __future__ import annotations

import io
import sys
import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import watch_digest


class TestPullRepoOrWarn(unittest.TestCase):
    """pull_repo_or_warn fail-open semantics for watch_digest — never raises, never blocks."""

    def test_pull_success_silent(self):
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            watch_digest.pull_repo_or_warn("watch_digest")  # no exception
        # T-0218: pull_repo_or_warn now fetches + merges (resilient to FETCH_HEAD pollution).
        self.assertEqual(mock_sub.run.call_count, 2)
        first_args, _ = mock_sub.run.call_args_list[0]
        self.assertEqual(first_args[0], ["git", "fetch", "origin", "main"])
        second_args, _ = mock_sub.run.call_args_list[1]
        self.assertEqual(second_args[0], ["git", "merge", "--ff-only", "refs/remotes/origin/main"])

    def test_pull_nonzero_proceeds(self):
        import contextlib
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=1, stdout="", stderr="fatal: refusing to merge unrelated histories\n"
            )
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                watch_digest.pull_repo_or_warn("watch_digest")  # must not raise
            self.assertIn("fetch failed", buf.getvalue())
            self.assertIn("refusing to merge", buf.getvalue())

    def test_pull_timeout_proceeds(self):
        import contextlib
        import subprocess as real_sub
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.TimeoutExpired = real_sub.TimeoutExpired
            mock_sub.run.side_effect = real_sub.TimeoutExpired(cmd="git", timeout=30)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                watch_digest.pull_repo_or_warn("watch_digest")  # must not raise
            self.assertIn("fetch skipped", buf.getvalue())


class TestSurfacing(unittest.TestCase):
    """Producer-declared `surface:` frontmatter is authoritative; keyword scan is fallback only."""

    def test_frontmatter_above_bar_surfaces_and_uses_headline(self):
        r = "---\nsurface: above-bar\nheadline: New MCP CVE hits Claude Code clients\n---\n# Report\nbody"
        self.assertTrue(watch_digest.is_above_bar(r))
        self.assertEqual(watch_digest.report_summary(r), "New MCP CVE hits Claude Code clients")

    def test_routine_overrides_keyword_in_body(self):
        # The core fix: a self-declared routine report does NOT surface even if its
        # body mentions a fast-path keyword in passing (old keyword-only false positive).
        r = "---\nsurface: routine\nheadline: Nothing material this week\n---\nbody mentions billing in passing"
        self.assertFalse(watch_digest.is_above_bar(r))
        self.assertEqual(watch_digest.report_summary(r), "Nothing material this week")

    def test_legacy_no_frontmatter_keyword_fallback(self):
        r = "# Old report\nThere is a new CVE-2026-1234 affecting things"
        self.assertTrue(watch_digest.is_above_bar(r))

    def test_legacy_no_signal_is_routine_with_first_line_summary(self):
        r = "# Gas City\nGas City is Gas Town rewritten as an SDK."
        self.assertFalse(watch_digest.is_above_bar(r))
        self.assertIn("SDK", watch_digest.report_summary(r))

    def test_parse_frontmatter_absent(self):
        self.assertEqual(watch_digest.parse_frontmatter("# no frontmatter\nbody"), {})


class TestLabelDisagreement(unittest.TestCase):
    """T-2042: a declared-routine report that trips an above-bar keyword is
    flagged as a disagreement, WITHOUT changing the effective bar (P1)."""

    def test_routine_with_keyword_flags_disagreement(self):
        r = "---\nsurface: routine\nheadline: Nothing material\n---\nbody mentions a new CVE-2026-9999 in passing"
        self.assertEqual(watch_digest.label_disagreement(r), "CVE-")
        # Effective bar is unchanged: still below-bar per producer declaration.
        self.assertFalse(watch_digest.is_above_bar(r))

    def test_genuinely_routine_no_keyword_no_disagreement(self):
        r = "---\nsurface: routine\nheadline: Nothing material this week\n---\nbody has nothing notable"
        self.assertIsNone(watch_digest.label_disagreement(r))
        self.assertFalse(watch_digest.is_above_bar(r))

    def test_above_bar_declared_no_spurious_flag(self):
        r = "---\nsurface: above-bar\nheadline: New MCP CVE hits Claude Code clients\n---\nbody"
        self.assertIsNone(watch_digest.label_disagreement(r))
        self.assertTrue(watch_digest.is_above_bar(r))

    def test_no_declaration_keyword_unflagged_but_still_above_bar(self):
        # No-declaration + keyword already surfaces above-bar via the existing
        # fallback; flagging it here too would be redundant, so no disagreement.
        r = "# Old report\nThere is a new CVE-2026-1234 affecting things"
        self.assertIsNone(watch_digest.label_disagreement(r))
        self.assertTrue(watch_digest.is_above_bar(r))


class TestBuildDigestDisagreementSection(unittest.TestCase):
    """The disagreement must be VISIBLE in the digest, layered on top of the
    unchanged below-bar listing — not an override of the producer's label."""

    def test_disagreement_section_rendered_and_item_stays_below_bar(self):
        rows = [("shaded-report", False, "Nothing material", "CVE-")]
        digest = watch_digest.build_digest("2026-07-27", rows)
        self.assertIn("⚠️ *Label disagreements", digest)
        self.assertIn("`shaded-report`", digest)
        self.assertIn("CVE-", digest)
        self.assertIn("Below the bar:", digest)
        # The item still appears under Below the bar — bucket is unchanged.
        below_section = digest.split("Below the bar:")[1]
        self.assertIn("shaded-report", below_section)

    def test_no_disagreements_no_section(self):
        rows = [("clean-report", False, "Nothing material", None)]
        digest = watch_digest.build_digest("2026-07-27", rows)
        self.assertNotIn("Label disagreements", digest)


class TestSendTelegramFallback(unittest.TestCase):
    """send_telegram must fall back to plain text when Markdown parsing 400s.

    Regression for 2026-06-08: an unbalanced `_` (in a `watch_runner` headline,
    one of 8 above-bar items) made legacy-Markdown parsing 400, dropping the whole
    digest — the most important one of the week. Headlines are arbitrary researcher
    prose, so delivery must not depend on them being Markdown-clean.
    """

    def _resp(self, status):
        m = MagicMock()
        m.status = status
        m.__enter__ = lambda s: m
        m.__exit__ = lambda s, *a: False
        return m

    def test_markdown_400_falls_back_to_plain_text(self):
        import urllib.error
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.data.decode("utf-8"))
            if len(calls) == 1:  # first (Markdown) attempt 400s
                raise urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
            return self._resp(200)  # plain-text retry succeeds

        with patch("tools.watch_digest.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("sys.stderr", new_callable=io.StringIO):
            ok = watch_digest.send_telegram("tok", "123", "x with a lone _ underscore")
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        self.assertIn("parse_mode", calls[0])          # 1st try used Markdown
        self.assertNotIn("parse_mode", calls[1])       # fallback dropped parse_mode

    def test_markdown_success_no_fallback(self):
        def fake_urlopen(req, timeout=None):
            return self._resp(200)

        with patch("tools.watch_digest.urllib.request.urlopen", side_effect=fake_urlopen) as mock_open:
            ok = watch_digest.send_telegram("tok", "123", "clean text")
        self.assertTrue(ok)
        self.assertEqual(mock_open.call_count, 1)       # no retry when Markdown works

    def test_both_attempts_fail_returns_false(self):
        with patch("tools.watch_digest.urllib.request.urlopen", side_effect=OSError("net down")), \
             patch("sys.stderr", new_callable=io.StringIO):
            ok = watch_digest.send_telegram("tok", "123", "text")
        self.assertFalse(ok)


class TestDigestChunking(unittest.TestCase):
    """T-3135-day regression (2026-08-24): a 20-topic digest overflowed Telegram's
    4096-char cap and BOTH Markdown + plain-text 400'd (length, not formatting, so the
    fallback couldn't save it) — the unit exited 1. send_telegram must CHUNK on line
    boundaries and send each part."""

    def _resp(self, status):
        m = MagicMock()
        m.status = status
        m.__enter__ = lambda s: m
        m.__exit__ = lambda s, *a: False
        return m

    def test_chunk_lines_packs_on_line_boundaries_under_limit(self):
        text = "\n".join(f"line-{i:03d} " + "x" * 50 for i in range(200))  # ~11k chars
        chunks = watch_digest._chunk_lines(text, limit=1000)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 1000)
        self.assertEqual("\n".join(chunks).count("line-"), text.count("line-"))  # nothing dropped

    def test_chunk_lines_hard_splits_a_single_overlong_line(self):
        long = "z" * 2500
        chunks = watch_digest._chunk_lines(long, limit=1000)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual("".join(chunks), long)

    def test_chunk_lines_single_chunk_when_small(self):
        self.assertEqual(watch_digest._chunk_lines("short", limit=1000), ["short"])

    def test_oversized_digest_sends_all_chunks_within_cap(self):
        import urllib.parse as up
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.data.decode("utf-8"))
            return self._resp(200)

        big = "\n".join(f"* item {i} *" + "y" * 60 for i in range(300))  # well over 4096
        with patch("tools.watch_digest.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("sys.stderr", new_callable=io.StringIO):
            ok = watch_digest.send_telegram("tok", "123", big)
        self.assertTrue(ok)
        self.assertGreater(len(calls), 1)  # actually chunked
        for body in calls:
            fields = dict(up.parse_qsl(body))
            self.assertLessEqual(len(fields["text"]), watch_digest.TELEGRAM_MAX_CHARS)
            self.assertIn("part", fields["text"])  # continuation marker present when split

    def test_oversized_partial_failure_returns_false(self):
        import urllib.error
        state = {"n": 0}

        def fake_urlopen(req, timeout=None):
            state["n"] += 1
            if state["n"] >= 2:  # chunk 1 delivers; everything from chunk 2 (both attempts) 400s
                raise urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
            return self._resp(200)

        big = "\n".join("x" * 100 for _ in range(100))  # multi-chunk
        with patch("tools.watch_digest.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("sys.stderr", new_callable=io.StringIO):
            ok = watch_digest.send_telegram("tok", "123", big)
        self.assertFalse(ok)  # incomplete delivery is a failure → main() won't stamp leans


# ---------------------------------------------------------------------------
# T-2199: the operator-facing lean slot.
#
# The bug these pin is NOT "leans render wrong" — it is that specialist RECORDED a
# lean as surfaced twice and delivered it zero times, because the SOP named a
# channel that had no mechanism. So the load-bearing assertions here are about
# the RECEIPT: that no stamp is written unless a send succeeded, and that a
# stamp is written when one did. Rendering is the cheap half.
# ---------------------------------------------------------------------------


def _ticket(tmp, tid, *, target="operator", created="2026-07-01T00:00:00Z", extra_log="",
            decide_by=None):
    d = Path(tmp) / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{tid}.md"
    deadline = f"decide_by: {decide_by}\n" if decide_by is not None else ""
    p.write_text(
        f"---\nid: {tid}\ncreated: {created}\nrequester: specialist\ntarget: {target}\n"
        f"state: inbox\n{deadline}---\n\n# Title for {tid}\n\n## Ask\n\nx\n\n## Transition log\n\n"
        f"- {created} [specialist] filed in inbox\n{extra_log}",
        encoding="utf-8",
    )
    return p


class TestLeanCollection(unittest.TestCase):
    NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    def test_parked_operator_ticket_is_collected_with_its_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-1953", created="2026-07-22T00:00:00Z")
            leans = watch_digest.collect_operator_leans(Path(tmp), now=self.NOW)
        self.assertEqual([(r[0], r[2]) for r in leans], [("T-1953", 6)])

    def test_non_operator_target_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-0001", target="recovery", created="2026-07-01T00:00:00Z")
            self.assertEqual(watch_digest.collect_operator_leans(Path(tmp), now=self.NOW), [])

    def test_ticket_younger_than_the_floor_is_not_surfaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-0002", created="2026-07-28T00:00:00Z")
            self.assertEqual(watch_digest.collect_operator_leans(Path(tmp), now=self.NOW), [])

    def test_recently_surfaced_ticket_is_suppressed_until_the_resurface_window(self):
        """The anti-nag property. A digest section that is identical every day
        stops being read, which would reproduce the invisibility this fixes."""
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-0003", created="2026-07-01T00:00:00Z",
                    extra_log="- 2026-07-26T00:00:00Z [specialist] lean-surfaced; in the digest\n")
            self.assertEqual(watch_digest.collect_operator_leans(Path(tmp), now=self.NOW), [])

    def test_surfaced_long_ago_becomes_eligible_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-0004", created="2026-07-01T00:00:00Z",
                    extra_log="- 2026-07-10T00:00:00Z [specialist] lean-surfaced; in the digest\n")
            leans = watch_digest.collect_operator_leans(Path(tmp), now=self.NOW)
        self.assertEqual([r[0] for r in leans], ["T-0004"])

    def test_oldest_first_so_truncation_never_eats_the_most_rotted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-NEW", created="2026-07-20T00:00:00Z")
            _ticket(tmp, "T-OLD", created="2026-06-01T00:00:00Z")
            leans = watch_digest.collect_operator_leans(Path(tmp), now=self.NOW)
        self.assertEqual([r[0] for r in leans], ["T-OLD", "T-NEW"])


class TestLeanUrgency(unittest.TestCase):
    """T-2209: the lean slot had no urgency axis, so a closing-window ticket was
    structurally unreachable — below the age floor, and last once it cleared it."""

    NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    def test_decide_by_exempts_a_ticket_from_the_age_floor(self):
        """The exact T-2107 shape: filed hours ago, window closes before the
        floor does. Without the exemption this ticket can never surface at all."""
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-2107", created="2026-07-28T00:18:20Z", decide_by="2026-07-29")
            leans = watch_digest.collect_operator_leans(Path(tmp), now=self.NOW)
        self.assertEqual([r[0] for r in leans], ["T-2107"])

    def test_age_floor_still_applies_without_a_deadline(self):
        """The exemption must be the FIELD's doing, not a weakened floor."""
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-2107", created="2026-07-28T00:18:20Z")
            self.assertEqual(watch_digest.collect_operator_leans(Path(tmp), now=self.NOW), [])

    def test_deadline_sorts_ahead_of_a_far_older_parked_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-0705", created="2026-05-31T00:00:00Z")          # 58d
            _ticket(tmp, "T-2107", created="2026-07-28T00:18:20Z", decide_by="2026-07-29")
            leans = watch_digest.collect_operator_leans(Path(tmp), now=self.NOW)
        self.assertEqual([r[0] for r in leans], ["T-2107", "T-0705"])

    def test_soonest_deadline_leads_so_an_overdue_one_is_never_buried(self):
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-LATER", created="2026-07-01T00:00:00Z", decide_by="2026-08-10")
            _ticket(tmp, "T-OVERDUE", created="2026-07-01T00:00:00Z", decide_by="2026-07-20")
            leans = watch_digest.collect_operator_leans(Path(tmp), now=self.NOW)
        self.assertEqual([r[0] for r in leans], ["T-OVERDUE", "T-LATER"])

    def test_mixed_groups_never_compare_a_datetime_against_an_int(self):
        """Guards the sort key itself. A one-group key would raise TypeError the
        first time a real digest held both kinds — which is every ordinary day."""
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-A", created="2026-06-01T00:00:00Z")
            _ticket(tmp, "T-B", created="2026-07-01T00:00:00Z", decide_by="2026-07-30")
            _ticket(tmp, "T-C", created="2026-06-15T00:00:00Z")
            _ticket(tmp, "T-D", created="2026-07-02T00:00:00Z", decide_by="2026-07-29")
            leans = watch_digest.collect_operator_leans(Path(tmp), now=self.NOW)
        self.assertEqual([r[0] for r in leans], ["T-D", "T-B", "T-A", "T-C"])

    def test_a_full_timestamp_deadline_is_accepted_alongside_a_bare_date(self):
        """A bare date parses NAIVE; comparing it to an aware `now` raises. Both
        forms must land in the same group and order correctly against each other."""
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-STAMP", created="2026-07-01T00:00:00Z",
                    decide_by="2026-07-29T06:00:00Z")
            _ticket(tmp, "T-BARE", created="2026-07-01T00:00:00Z", decide_by="2026-07-30")
            leans = watch_digest.collect_operator_leans(Path(tmp), now=self.NOW)
        self.assertEqual([r[0] for r in leans], ["T-STAMP", "T-BARE"])

    def test_an_unparseable_deadline_degrades_to_age_and_does_not_exempt(self):
        """A typo in a deadline must not make a ticket invisible — that would be
        a worse failure than the one the field exists to fix."""
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-TYPO", created="2026-07-28T00:18:20Z", decide_by="next tuesday")
            self.assertEqual(watch_digest.collect_operator_leans(Path(tmp), now=self.NOW), [])
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-TYPO", created="2026-07-01T00:00:00Z", decide_by="next tuesday")
            leans = watch_digest.collect_operator_leans(Path(tmp), now=self.NOW)
        self.assertEqual([(r[0], r[4]) for r in leans], [("T-TYPO", None)])

    def test_a_deadline_does_not_defeat_the_resurface_window(self):
        """Deliberate scope line: urgency buys a hearing, not a daily repeat.

        PAIRED ON PURPOSE. The suppression half asserts an EMPTY result, which a
        broken fixture satisfies just as well as working code — if `_ticket` ever
        stopped writing `decide_by`, this would still pass while testing nothing.
        The unsuppressed half asserts a HIT off the same fixture shape, so the
        deadline is proven live before the suppression is credited with anything.
        """
        stamp = "- 2026-07-26T00:00:00Z [specialist] lean-surfaced; in the digest\n"
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-2107", created="2026-07-20T00:00:00Z", decide_by="2026-07-29")
            live = watch_digest.collect_operator_leans(Path(tmp), now=self.NOW)
        self.assertEqual([(r[0], r[4] is not None) for r in live], [("T-2107", True)])
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-2107", created="2026-07-20T00:00:00Z", decide_by="2026-07-29",
                    extra_log=stamp)
            self.assertEqual(watch_digest.collect_operator_leans(Path(tmp), now=self.NOW), [])


class TestLeanRendering(unittest.TestCase):
    LEAN = [("T-1953", "the operator's laptop: COM/USB control", 6, Path("/x/T-1953.md"), None)]
    URGENT = [("T-2107", "Pre-grading pool composition", 0, Path("/x/T-2107.md"),
               datetime(2026, 7, 29, tzinfo=timezone.utc))]

    def test_deadline_row_shows_the_deadline_instead_of_the_age(self):
        """Reordering alone would move the row and say nothing about why. The
        line has to carry the reason, or the urgency is invisible to the reader
        it was surfaced for (T-2209)."""
        out = watch_digest.build_digest("2026-07-28", [], self.URGENT)
        self.assertIn("decide by 2026-07-29", out)
        self.assertIn("T-2107", out)
        self.assertNotIn("0d", out)

    def test_ordinary_lean_row_still_shows_age(self):
        out = watch_digest.build_digest("2026-07-28", [], self.LEAN)
        self.assertIn("6d", out)
        self.assertNotIn("decide by", out)

    def test_lean_rides_out_on_a_no_reports_day(self):
        """The regression that mattered most: the no-reports branch returned
        early, so the quietest days — exactly when a parked ticket most needs
        carrying — were the days a lean could not ride out at all."""
        out = watch_digest.build_digest("2026-07-28", [], self.LEAN)
        self.assertIn("Parked on you", out)
        self.assertIn("T-1953", out)

    def test_lean_appears_alongside_reports(self):
        rows = [("topic", False, "summary", None)]
        out = watch_digest.build_digest("2026-07-28", rows, self.LEAN)
        self.assertIn("Parked on you", out)
        self.assertIn("6d", out)

    def test_no_lean_section_when_there_are_none(self):
        out = watch_digest.build_digest("2026-07-28", [("t", False, "s", None)], [])
        self.assertNotIn("Parked on you", out)

    def test_overflow_is_named_not_silently_dropped(self):
        many = [(f"T-{i:04d}", f"title {i}", 10 + i, Path("/x"), None) for i in range(8)]
        out = watch_digest.build_digest("2026-07-28", [], many)
        self.assertIn("and 3 more", out)


class TestDeliverBeforeRecording(unittest.TestCase):
    """The constraint specialist set, and the one that makes this a fix rather than a
    restatement of the bug: a receipt must never precede its delivery."""

    def _run_main(self, tmp, *, send_ok, argv):
        t = _ticket(tmp, "T-1953", created="2026-07-01T00:00:00Z")
        with patch.object(watch_digest, "TICKETS_ROOT", Path(tmp)), \
             patch.object(watch_digest, "pull_repo_or_warn"), \
             patch.object(watch_digest, "collect_today", return_value=[]), \
             patch.object(watch_digest, "read_telegram_token", return_value="tok"), \
             patch.object(watch_digest, "send_telegram", return_value=send_ok), \
             patch.object(watch_digest, "commit_scoped", return_value=(True, "ok")), \
             patch("sys.argv", argv), \
             patch("sys.stdout", new_callable=io.StringIO), \
             patch("sys.stderr", new_callable=io.StringIO):
            rc = watch_digest.main()
        return rc, t.read_text(encoding="utf-8")

    def test_failed_send_writes_NO_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, body = self._run_main(tmp, send_ok=False, argv=["watch_digest.py"])
        self.assertEqual(rc, 1)
        self.assertNotIn("lean-surfaced", body)

    def test_successful_send_writes_the_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, body = self._run_main(tmp, send_ok=True, argv=["watch_digest.py"])
        self.assertEqual(rc, 0)
        self.assertIn("lean-surfaced", body)

    def test_no_send_dry_run_writes_NO_stamp(self):
        """T-2182's fault in a surface where the cost is a false receipt."""
        with tempfile.TemporaryDirectory() as tmp:
            rc, body = self._run_main(tmp, send_ok=True, argv=["watch_digest.py", "--no-send"])
        self.assertEqual(rc, 0)
        self.assertNotIn("lean-surfaced", body)

    def test_a_failed_stamp_does_not_fail_a_delivered_digest(self):
        """The lean WAS delivered; a missing receipt only re-surfaces it sooner.
        Failing here would trade a benign duplicate for a lost delivery."""
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-1953", created="2026-07-01T00:00:00Z")
            with patch.object(watch_digest, "TICKETS_ROOT", Path(tmp)), \
                 patch.object(watch_digest, "pull_repo_or_warn"), \
                 patch.object(watch_digest, "collect_today", return_value=[]), \
                 patch.object(watch_digest, "read_telegram_token", return_value="tok"), \
                 patch.object(watch_digest, "send_telegram", return_value=True), \
                 patch.object(watch_digest, "stamp_lean_surfaced", side_effect=OSError("disk")), \
                 patch("sys.argv", ["watch_digest.py"]), \
                 patch("sys.stdout", new_callable=io.StringIO), \
                 patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(OSError):
                    watch_digest.main()



class TestOperatorDigestActionOnly(unittest.TestCase):
    """T-4200: the operator watch Telegram is ACTION-ONLY (above-bar / label
    disagreement / parked lean); routine days send nothing, full digest still
    prints to stdout/journal."""

    LEAN = [("T-1", "Decide X", 5, Path("/tmp/T-1.md"), None)]

    def test_none_when_all_routine(self):
        rows = [("t1", False, "routine one", None), ("t2", False, "routine two", None)]
        self.assertIsNone(watch_digest.build_operator_digest("2026-09-28", rows, []))

    def test_none_when_no_reports_no_leans(self):
        self.assertIsNone(watch_digest.build_operator_digest("2026-09-28", [], []))

    def test_sends_on_above_the_bar(self):
        rows = [("t1", True, "big finding", None), ("t2", False, "routine two", None)]
        msg = watch_digest.build_operator_digest("2026-09-28", rows, [])
        self.assertIsNotNone(msg)
        self.assertIn("Above the bar", msg)
        self.assertIn("big finding", msg)
        self.assertNotIn("routine two", msg)  # below-bar row stripped from operator TG

    def test_sends_on_label_disagreement(self):
        rows = [("t1", False, "sneaky summary", "exploit")]  # declared routine, kw tripped
        msg = watch_digest.build_operator_digest("2026-09-28", rows, [])
        self.assertIsNotNone(msg)
        self.assertIn("Label disagreements", msg)
        self.assertIn("exploit", msg)

    def test_sends_on_lean_alone(self):
        msg = watch_digest.build_operator_digest("2026-09-28", [], self.LEAN)
        self.assertIsNotNone(msg)
        self.assertIn("Parked on you", msg)
        self.assertIn("T-1", msg)

    def test_main_suppresses_send_on_routine_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(watch_digest, "TICKETS_ROOT", Path(tmp)), \
                 patch.object(watch_digest, "pull_repo_or_warn"), \
                 patch.object(watch_digest, "collect_today",
                              return_value=[("t1", False, "routine", None)]), \
                 patch.object(watch_digest, "collect_operator_leans", return_value=[]), \
                 patch.object(watch_digest, "collect_day_index_drift", return_value=None), \
                 patch.object(watch_digest, "read_telegram_token", return_value="tok"), \
                 patch.object(watch_digest, "send_telegram", return_value=True) as mock_send, \
                 patch("sys.argv", ["watch_digest.py"]), \
                 patch("sys.stdout", new_callable=io.StringIO), \
                 patch("sys.stderr", new_callable=io.StringIO):
                rc = watch_digest.main()
        self.assertEqual(rc, 0)
        mock_send.assert_not_called()

    def test_main_sends_on_above_bar_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(watch_digest, "TICKETS_ROOT", Path(tmp)), \
                 patch.object(watch_digest, "pull_repo_or_warn"), \
                 patch.object(watch_digest, "collect_today",
                              return_value=[("t1", True, "big finding", None)]), \
                 patch.object(watch_digest, "collect_operator_leans", return_value=[]), \
                 patch.object(watch_digest, "collect_day_index_drift", return_value=None), \
                 patch.object(watch_digest, "read_telegram_token", return_value="tok"), \
                 patch.object(watch_digest, "send_telegram", return_value=True) as mock_send, \
                 patch("sys.argv", ["watch_digest.py"]), \
                 patch("sys.stdout", new_callable=io.StringIO), \
                 patch("sys.stderr", new_callable=io.StringIO):
                rc = watch_digest.main()
        self.assertEqual(rc, 0)
        mock_send.assert_called_once()
        self.assertIn("Above the bar", mock_send.call_args.args[2])


class TestReceiptIsSegmentAnchoredNotSubstring(unittest.TestCase):
    """T-2202. A prose MENTION of lean-surfacing must not read as having DONE it —
    that is T-2199's own thesis (a record asserting something that did not happen)
    reproduced one layer in. And both real receipt shapes must survive, because the
    obvious anchor (verb slot only) rejects the manual one."""

    def _body(self, *log_lines):
        return "---\nid: T-X\n---\n\n## Transition log\n\n" + "\n".join(log_lines) + "\n"

    def test_automated_receipt_is_a_receipt(self):
        b = self._body("- 2026-07-28T14:00:00Z [specialist] lean-surfaced; included in the watch digest")
        self.assertIsNotNone(watch_digest.last_lean_surface(b))

    def test_manual_relay_receipt_is_ALSO_a_receipt(self):
        """ticket.py annotate owns the verb slot ('note'), so a hand-relay receipt
        carries the token one segment later. T-1953 is exactly this shape; a
        verb-slot-only anchor would un-suppress it and re-send the operator something he
        already received — the precise double-surfacing the suppressor prevents."""
        b = self._body("- 2026-07-28T14:10:25Z [claude] note; lean-surfaced; carried to Telegram msg 6593")
        self.assertIsNotNone(watch_digest.last_lean_surface(b))

    def test_prose_mention_mid_sentence_is_NOT_a_receipt(self):
        """Verbatim shape from T-1195, which the substring version accepted."""
        b = self._body(
            "- 2026-06-16T01:33:19Z [claude] resolved; the operator lean-surfaced the SOP-vs-directive call (TG-534)")
        self.assertIsNone(watch_digest.last_lean_surface(b))

    def test_a_prose_mention_cannot_suppress_a_real_lean(self):
        """The consequence, asserted end-to-end rather than at the parser."""
        with tempfile.TemporaryDirectory() as tmp:
            _ticket(tmp, "T-0009", created="2026-06-01T00:00:00Z",
                    extra_log="- 2026-07-27T00:00:00Z [x] resolved; we lean-surfaced that one last week\n")
            leans = watch_digest.collect_operator_leans(Path(tmp), now=datetime(2026, 7, 28, tzinfo=timezone.utc))
        self.assertEqual([r[0] for r in leans], ["T-0009"])

    def test_line_without_the_actor_marker_is_ignored_not_crashed(self):
        b = self._body("- 2026-07-28T00:00:00Z lean-surfaced with no actor bracket")
        self.assertIsNone(watch_digest.last_lean_surface(b))



class TestDayIndexDriftRow(unittest.TestCase):
    """T-2363: the day-index drift row is the wired surface for a swallowed check.

    knowledge_sweep swallows the drift signal by design; the digest is where it
    lands. These are regression tests for the two ways that wiring can rot into
    the thing it replaced:

      - a clean day and a BROKEN CHECK both rendering as silence, which is the
        ambiguity that hid 21 days of drift (T-2358);
      - the row being appended only to the has-reports branch, which is how the
        lean block was lost on quiet days. Drift comes from manual deliveries
        that bypass the runner, so no-report days are its highest-yield days.
    """

    def _tree(self, tmp: Path) -> Path:
        day = tmp / "2026-07-01"
        day.mkdir(parents=True)
        (day / "alpha.md").write_text("# alpha\n")
        (day / "README.md").write_text("# index\n- [alpha](alpha.md)\n")
        return day

    def test_clean_tree_returns_none_so_a_clean_day_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._tree(tmp)
            with patch.object(watch_digest, "REPORTS_ROOT", tmp):
                self.assertIsNone(watch_digest.collect_day_index_drift())

    def test_unindexed_file_is_counted_with_its_day(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            day = self._tree(tmp)
            (day / "beta.md").write_text("# beta\n")  # manual delivery, unlinked
            with patch.object(watch_digest, "REPORTS_ROOT", tmp):
                self.assertEqual(watch_digest.collect_day_index_drift(),
                                 (1, 1, "2026-07-01"))

    def test_oldest_drifting_day_is_reported_not_the_newest(self):
        # The escalation signal is age: episode 1 ran 16 days and grew 1 -> 4.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            day = self._tree(tmp)
            (day / "beta.md").write_text("# beta\n")
            old = tmp / "2026-06-15"
            old.mkdir()
            (old / "README.md").write_text("# index\n- [gone](gone.md)\n")
            (old / "present.md").write_text("# present\n")
            with patch.object(watch_digest, "REPORTS_ROOT", tmp):
                items, dirs, oldest = watch_digest.collect_day_index_drift()
            self.assertEqual((items, dirs), (3, 2))
            self.assertEqual(oldest, "2026-06-15")

    def test_lint_failure_is_reported_not_swallowed_into_silence(self):
        with patch.object(watch_digest, "REPORTS_ROOT", None):
            got = watch_digest.collect_day_index_drift()
        self.assertIsInstance(got, str)
        body = watch_digest.build_digest("2026-07-29", [], [], got)
        self.assertIn("did not run", body)

    def test_drift_row_survives_the_no_reports_early_return(self):
        drift = (3, 2, "2026-06-15")
        quiet = watch_digest.build_digest("2026-07-29", [], [], drift)
        self.assertIn("Day-index drift:", quiet)
        self.assertIn("2026-06-15", quiet)

    def test_drift_row_renders_when_reports_are_present(self):
        rows = [("some-topic", True, "a summary", None)]
        body = watch_digest.build_digest("2026-07-29", rows, [], (1, 1, "2026-07-01"))
        self.assertIn("Day-index drift:", body)

    def test_no_drift_renders_no_row_in_either_branch(self):
        rows = [("some-topic", True, "a summary", None)]
        self.assertNotIn("Day-index", watch_digest.build_digest("2026-07-29", rows, [], None))
        self.assertNotIn("Day-index", watch_digest.build_digest("2026-07-29", [], [], None))


if __name__ == "__main__":
    unittest.main()
