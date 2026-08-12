#!/usr/bin/env python3
"""Tests for the ledger shard-placement linter + append_entry helper (T-2077).

Covers:
  * shard_segment path parsing (shard vs legacy vs junk)
  * check_entry judgement (misplaced / correct / unjudgeable)
  * lint_all over a synthetic ledger tree (incl. legacy-skip)
  * lint_range over a real git repo (only ADDED entries flagged; frozen
    historical misplacements not re-flagged)
  * ledger_shard.append_entry places by the entry's OWN ts, not wall-clock
  * check_entry_validity / lint_staged_validity — the write-path JSON-validity
    guard (T-2650): unparseable/non-object ADDED ledger lines are flagged;
    pre-existing (already-committed) invalid content is never re-flagged.
  * current_scope_segments / lint_default — the bare-invocation DEFAULT scope
    (T-2659): current + prior ISO-week shard only, via an injectable `now`
    seam for determinism; out-of-scope older-shard misplacements are ignored.
  * main() dispatch — bare invocation routes to lint_default, --all routes to
    lint_all, --staged is unchanged (T-2659 regression coverage).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

# Work whether invoked from the repo root (`python3 -m unittest
# tools.test_ledger_placement_lint`) or the tools dir — put tools/ on the path
# so the bare module imports below resolve either way.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_placement_lint as lpl  # noqa: E402
import ledger_shard  # noqa: E402

# A ts in ISO week 2026-W30 and one in 2026-W31. 2026-07-26 is a Sunday (last
# day of W30); 2026-07-27 is a Monday (first day of W31) — the exact rollover
# where boundary-spillover happens.
TS_W30 = "2026-07-26T12:00:00Z"
TS_W31 = "2026-07-27T12:00:00Z"


def _entry(ts: str, eid: str | None = None, agent: str = "metis") -> dict:
    return {"id": eid or f"{ts}-{agent}-x", "ts": ts, "agent": agent}


class TestShardSegment(unittest.TestCase):
    def test_valid_shard_path(self) -> None:
        self.assertEqual(
            lpl.shard_segment("ledger/2026-W31/2026-W31.jsonl"), "2026-W31")

    def test_legacy_flat_file_is_none(self) -> None:
        self.assertIsNone(lpl.shard_segment("ledger/2026.jsonl"))

    def test_non_shard_paths_are_none(self) -> None:
        self.assertIsNone(lpl.shard_segment("ledger/2026-W31/SPINE.md"))
        self.assertIsNone(lpl.shard_segment("ledger/INDEX.md"))
        # filename must match its parent segment
        self.assertIsNone(lpl.shard_segment("ledger/2026-W31/2026-W30.jsonl"))


class TestCheckEntry(unittest.TestCase):
    def test_correct_placement_returns_none(self) -> None:
        self.assertIsNone(lpl.check_entry("2026-W30", json.dumps(_entry(TS_W30))))

    def test_misplaced_is_flagged(self) -> None:
        m = lpl.check_entry("2026-W30", json.dumps(_entry(TS_W31, eid="e1")))
        self.assertIsNotNone(m)
        self.assertEqual(m["shard"], "2026-W30")
        self.assertEqual(m["want"], "2026-W31")
        self.assertEqual(m["id"], "e1")

    def test_ts_derived_from_id_when_field_absent(self) -> None:
        # entry_ts falls back to the id's leading ts prefix (T-1879)
        entry = {"id": f"{TS_W31}-metis-x", "agent": "metis"}  # no "ts"
        m = lpl.check_entry("2026-W30", json.dumps(entry))
        self.assertIsNotNone(m)
        self.assertEqual(m["want"], "2026-W31")

    def test_unjudgeable_lines_return_none(self) -> None:
        self.assertIsNone(lpl.check_entry("2026-W30", ""))
        self.assertIsNone(lpl.check_entry("2026-W30", "not json"))
        self.assertIsNone(lpl.check_entry("2026-W30", "[1,2,3]"))  # not an object
        self.assertIsNone(lpl.check_entry("2026-W30", json.dumps({"id": "no-ts"})))


class TestLintAll(unittest.TestCase):
    def _tree(self, tmp: Path) -> Path:
        ledger = tmp / "ledger"
        (ledger / "2026-W30").mkdir(parents=True)
        (ledger / "2026-W31").mkdir(parents=True)
        return ledger

    def test_flags_only_misplaced(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ledger = self._tree(Path(t))
            # W30 shard: one correct (W30 ts) + one misplaced (W31 ts)
            (ledger / "2026-W30" / "2026-W30.jsonl").write_text(
                json.dumps(_entry(TS_W30, eid="ok")) + "\n"
                + json.dumps(_entry(TS_W31, eid="bad")) + "\n")
            # W31 shard: correct
            (ledger / "2026-W31" / "2026-W31.jsonl").write_text(
                json.dumps(_entry(TS_W31, eid="ok2")) + "\n")
            out = lpl.lint_all(ledger)
            self.assertEqual([m["id"] for m in out], ["bad"])

    def test_skips_legacy_flat_file(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ledger = self._tree(Path(t))
            # legacy file with a "misplaced-looking" entry must be ignored
            (ledger / ledger_shard.LEGACY_LEDGER_FILENAME).write_text(
                json.dumps(_entry(TS_W31)) + "\n")
            self.assertEqual(lpl.lint_all(ledger), [])

    def test_missing_ledger_dir_is_empty(self) -> None:
        self.assertEqual(lpl.lint_all("/nonexistent/ledger/xyz"), [])


class TestCurrentScopeSegments(unittest.TestCase):
    def test_mid_week_returns_current_and_prior(self) -> None:
        # TS_W31 (2026-07-27, a Monday) -> current W31, prior W30.
        now = datetime.strptime(TS_W31, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self.assertEqual(lpl.current_scope_segments(now), ["2026-W31", "2026-W30"])

    def test_iso_year_boundary(self) -> None:
        # 2026-12-31 is a Thursday -> ISO week 53 of ISO-year 2026 (2026 has
        # a week 53); the prior day (2026-12-30, Wed) is in the SAME ISO
        # week, so use a date at the very start of an ISO week that crosses
        # the calendar-year boundary instead: 2027-01-01 (Fri) is ISO week
        # 53 of 2026; 2026-12-28 (Mon, start of that week) - 1 day = Sunday
        # 2026-12-27, still week 52 of 2026.
        now = datetime(2027, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(lpl.current_scope_segments(now), ["2026-W53", "2026-W52"])

    def test_defaults_to_real_clock_when_now_omitted(self) -> None:
        segs = lpl.current_scope_segments()
        self.assertEqual(len(segs), 2)
        for seg in segs:
            self.assertRegex(seg, r"^\d{4}-W\d{2}$")


class TestLintDefault(unittest.TestCase):
    def _now(self) -> datetime:
        # Fixes "current" = W31, "prior" = W30 (the exact rollover boundary).
        return datetime.strptime(TS_W31, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    def test_flags_misplacement_in_current_shard(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ledger = Path(t) / "ledger"
            shard = ledger / "2026-W31" / "2026-W31.jsonl"
            shard.parent.mkdir(parents=True)
            # A W30-ts entry sitting in the current (W31) shard.
            shard.write_text(json.dumps(_entry(TS_W30, eid="stray")) + "\n")
            out = lpl.lint_default(ledger, now=self._now())
            self.assertEqual([m["id"] for m in out], ["stray"])

    def test_flags_misplacement_in_prior_shard(self) -> None:
        # The actual historical root cause: a current-week-ts entry landed
        # in the PRIOR shard by session habit near the Monday rollover.
        with tempfile.TemporaryDirectory() as t:
            ledger = Path(t) / "ledger"
            shard = ledger / "2026-W30" / "2026-W30.jsonl"
            shard.parent.mkdir(parents=True)
            shard.write_text(json.dumps(_entry(TS_W31, eid="rolled")) + "\n")
            out = lpl.lint_default(ledger, now=self._now())
            self.assertEqual([m["id"] for m in out], ["rolled"])

    def test_ignores_misplacement_outside_scope(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ledger = Path(t) / "ledger"
            old_shard = ledger / "2026-W28" / "2026-W28.jsonl"
            old_shard.parent.mkdir(parents=True)
            # Misplaced (belongs in W27) but outside the current+prior scope.
            old_shard.write_text(
                json.dumps(_entry("2026-07-06T12:00:00Z", eid="old-bad")) + "\n")
            self.assertEqual(lpl.lint_default(ledger, now=self._now()), [])

    def test_correct_placement_in_scope_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ledger = Path(t) / "ledger"
            for seg, ts in (("2026-W30", TS_W30), ("2026-W31", TS_W31)):
                shard = ledger / seg / f"{seg}.jsonl"
                shard.parent.mkdir(parents=True)
                shard.write_text(json.dumps(_entry(ts, eid=f"{seg}-ok")) + "\n")
            self.assertEqual(lpl.lint_default(ledger, now=self._now()), [])

    def test_missing_ledger_dir_is_empty(self) -> None:
        self.assertEqual(
            lpl.lint_default("/nonexistent/ledger/xyz", now=self._now()), [])


class TestLintRange(unittest.TestCase):
    def _git(self, repo: Path, *args: str) -> str:
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
        })
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()

    def _init(self, repo: Path) -> None:
        repo.mkdir(parents=True, exist_ok=True)
        self._git(repo, "init", "-q")

    def _commit_shard(self, repo: Path, seg: str, lines: list[str], msg: str) -> str:
        shard = repo / "ledger" / seg / f"{seg}.jsonl"
        shard.parent.mkdir(parents=True, exist_ok=True)
        existing = shard.read_text() if shard.exists() else ""
        shard.write_text(existing + "".join(l + "\n" for l in lines))
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-q", "-m", msg)
        return self._git(repo, "rev-parse", "HEAD")

    def test_flags_added_misplaced_only(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t) / "r"
            self._init(repo)
            base = self._commit_shard(
                repo, "2026-W30", [json.dumps(_entry(TS_W30, eid="ok"))], "base")
            tip = self._commit_shard(
                repo, "2026-W30", [json.dumps(_entry(TS_W31, eid="bad"))], "add misplaced")
            out = lpl.lint_range(base, tip, repo_root=repo)
            self.assertEqual([m["id"] for m in out], ["bad"])

    def test_pre_existing_misplaced_not_in_range_is_ignored(self) -> None:
        # A misplaced entry already present at BASE must not be re-flagged when
        # it is outside the range — this is what keeps the frozen 107 quiet.
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t) / "r"
            self._init(repo)
            base = self._commit_shard(
                repo, "2026-W30", [json.dumps(_entry(TS_W31, eid="old-bad"))], "base misplaced")
            tip = self._commit_shard(
                repo, "2026-W30", [json.dumps(_entry(TS_W30, eid="new-ok"))], "add correct")
            self.assertEqual(lpl.lint_range(base, tip, repo_root=repo), [])

    def test_unreadable_range_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t) / "r"
            self._init(repo)
            self._commit_shard(repo, "2026-W30", [json.dumps(_entry(TS_W30))], "base")
            # bogus revs -> git diff errors -> [] (fail open, never raise)
            self.assertEqual(lpl.lint_range("deadbeef", "cafebabe", repo_root=repo), [])


class TestCheckEntryValidity(unittest.TestCase):
    def test_valid_line_is_none(self) -> None:
        self.assertIsNone(lpl.check_entry_validity(json.dumps(_entry(TS_W30))))

    def test_blank_line_is_none(self) -> None:
        self.assertIsNone(lpl.check_entry_validity(""))
        self.assertIsNone(lpl.check_entry_validity("   "))

    def test_missing_closing_brace_is_flagged(self) -> None:
        line = json.dumps(_entry(TS_W30))[:-1]  # drop trailing "}"
        v = lpl.check_entry_validity(line)
        self.assertIsNotNone(v)
        self.assertIn("invalid JSON", v["error"])
        self.assertEqual(v["line"], line)

    def test_non_object_json_is_flagged(self) -> None:
        v = lpl.check_entry_validity("[1, 2, 3]")
        self.assertIsNotNone(v)
        self.assertIn("not a JSON object", v["error"])


class TestLintStagedValidity(unittest.TestCase):
    def _git(self, repo: Path, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
        })
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, check=True, env=env,
        )

    def _init(self, repo: Path) -> None:
        repo.mkdir(parents=True, exist_ok=True)
        self._git(repo, "init", "-q")

    def _commit(self, repo: Path, msg: str) -> None:
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-q", "-m", msg)

    def test_staged_valid_addition_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t) / "r"
            self._init(repo)
            shard = repo / "ledger" / "2026-W30" / "2026-W30.jsonl"
            shard.parent.mkdir(parents=True)
            shard.write_text(json.dumps(_entry(TS_W30, eid="ok")) + "\n")
            self._git(repo, "add", "-A")  # staged, not committed
            self.assertEqual(lpl.lint_staged_validity(repo_root=repo), [])

    def test_staged_invalid_addition_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t) / "r"
            self._init(repo)
            shard = repo / "ledger" / "2026-W30" / "2026-W30.jsonl"
            shard.parent.mkdir(parents=True)
            bad_line = json.dumps(_entry(TS_W30, eid="bad"))[:-1]  # missing "}"
            shard.write_text(bad_line + "\n")
            self._git(repo, "add", "-A")
            out = lpl.lint_staged_validity(repo_root=repo)
            self.assertEqual(len(out), 1)
            self.assertIn("invalid JSON", out[0]["error"])
            self.assertEqual(out[0]["path"], "ledger/2026-W30/2026-W30.jsonl")

    def test_preexisting_invalid_in_head_not_reflagged(self) -> None:
        # A missing-brace line already committed (HEAD) must NOT be flagged
        # when only an unrelated valid line is newly staged in the same file
        # -- this is the added-lines-only scoping property the guard depends
        # on to never block on legacy/frozen content.
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t) / "r"
            self._init(repo)
            shard = repo / "ledger" / "2026-W30" / "2026-W30.jsonl"
            shard.parent.mkdir(parents=True)
            bad_line = json.dumps(_entry(TS_W30, eid="old-bad"))[:-1]
            shard.write_text(bad_line + "\n")
            self._commit(repo, "base with pre-existing invalid line")

            with shard.open("a") as f:
                f.write(json.dumps(_entry(TS_W30, eid="new-ok")) + "\n")
            self._git(repo, "add", "-A")  # stage only the new valid line

            self.assertEqual(lpl.lint_staged_validity(repo_root=repo), [])

    def test_non_jsonl_ledger_path_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t) / "r"
            self._init(repo)
            spine = repo / "ledger" / "2026-W30" / "SPINE.md"
            spine.parent.mkdir(parents=True)
            spine.write_text("not json at all {{{\n")
            self._git(repo, "add", "-A")
            self.assertEqual(lpl.lint_staged_validity(repo_root=repo), [])

    def test_bogus_repo_root_fails_open(self) -> None:
        self.assertEqual(
            lpl.lint_staged_validity(repo_root="/nonexistent/not/a/repo"), [])


class TestAppendEntry(unittest.TestCase):
    def test_places_by_entry_ts_not_now(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ledger = Path(t) / "ledger"
            ledger.mkdir()
            # entry ts is W31; append_entry must pick the W31 shard regardless
            # of the wall clock
            target = ledger_shard.append_entry(ledger, _entry(TS_W31, eid="e"))
            self.assertEqual(target, ledger / "2026-W31" / "2026-W31.jsonl")
            written = json.loads(target.read_text().splitlines()[-1])
            self.assertEqual(written["id"], "e")
            # and it is self-consistent: lint sees no misplacement
            self.assertEqual(lpl.lint_all(ledger), [])

    def test_raises_without_parseable_ts(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ledger = Path(t) / "ledger"
            ledger.mkdir()
            with self.assertRaises(ValueError):
                ledger_shard.append_entry(ledger, {"id": "no-ts", "agent": "metis"})

    def test_honors_legacy_file_transition(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            ledger = Path(t) / "ledger"
            ledger.mkdir()
            legacy = ledger / ledger_shard.LEGACY_LEDGER_FILENAME
            legacy.write_text("")  # legacy present -> append there
            target = ledger_shard.append_entry(ledger, _entry(TS_W31))
            self.assertEqual(target, legacy)


class TestMainDispatch(unittest.TestCase):
    """T-2659 regression: bare invocation routes to lint_default (the new
    DEFAULT), --all still routes to lint_all (full audit), and --staged is
    completely unaffected (never touches either placement-scan function)."""

    def test_bare_invocation_calls_lint_default_not_all(self) -> None:
        with mock.patch.object(lpl, "lint_default", return_value=[]) as md, \
                mock.patch.object(lpl, "lint_all") as ma:
            rc = lpl.main(["--ledger-dir", "ledger"])
        md.assert_called_once_with("ledger")
        ma.assert_not_called()
        self.assertEqual(rc, 0)

    def test_all_flag_calls_lint_all_not_default(self) -> None:
        with mock.patch.object(lpl, "lint_default") as md, \
                mock.patch.object(lpl, "lint_all", return_value=[]) as ma:
            rc = lpl.main(["--all", "--ledger-dir", "ledger"])
        ma.assert_called_once_with("ledger")
        md.assert_not_called()
        self.assertEqual(rc, 0)

    def test_staged_dispatch_unchanged(self) -> None:
        with mock.patch.object(lpl, "lint_staged_validity", return_value=[]) as ms, \
                mock.patch.object(lpl, "lint_default") as md, \
                mock.patch.object(lpl, "lint_all") as ma:
            rc = lpl.main(["--staged"])
        ms.assert_called_once()
        md.assert_not_called()
        ma.assert_not_called()
        self.assertEqual(rc, 0)

    def test_bare_invocation_nonzero_exit_on_misplacement(self) -> None:
        fake = {"shard": "2026-W30", "want": "2026-W31", "ts": TS_W31,
                "id": "x", "agent": "metis"}
        with mock.patch.object(lpl, "lint_default", return_value=[fake]):
            rc = lpl.main([])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
