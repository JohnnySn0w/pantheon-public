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


def _entry(ts: str, eid: str | None = None, agent: str = "mason") -> dict:
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
        entry = {"id": f"{TS_W31}-mason-x", "agent": "mason"}  # no "ts"
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


class TestSharedFraming(unittest.TestCase):
    """T-4202: added lines are framed by the shared structural parser
    (hermes_diff_scan.iter_added_lines) over a bytes diff. Each case below
    failed on the old text=True + splitlines() + `+++`-skip parser."""

    SEG = "2026-W30"
    REL = "ledger/2026-W30/2026-W30.jsonl"

    def _git(self, repo: Path, *args: str) -> None:
        env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
               "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com"}
        subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, check=True, env=env)

    def _repo(self, t: str, base_lines: "list[bytes]") -> "tuple[Path, Path]":
        repo = Path(t) / "r"
        repo.mkdir()
        self._git(repo, "init", "-q")
        shard = repo / self.REL
        shard.parent.mkdir(parents=True)
        shard.write_bytes(b"".join(l + b"\n" for l in base_lines))
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-q", "-m", "base")
        return repo, shard

    def _append(self, shard: Path, lines: "list[bytes]") -> None:
        shard.write_bytes(shard.read_bytes() + b"".join(l + b"\n" for l in lines))

    @staticmethod
    def _raw(entry: dict, note: str = "") -> bytes:
        e = dict(entry)
        if note:
            e["note"] = note
        return json.dumps(e, ensure_ascii=False).encode("utf-8")

    def test_staged_valid_line_with_raw_u2028_is_not_a_false_block(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo, shard = self._repo(t, [self._raw(_entry(TS_W30, eid="a"))])
            self._append(shard, [self._raw(_entry(TS_W30, eid="b"), "x y\u0085z\x1cw")])
            self._git(repo, "add", "-A")
            self.assertEqual(lpl.lint_staged_validity(repo_root=repo), [])

    def test_staged_invalid_plus_plus_line_is_caught(self) -> None:
        # Content `++{...}` renders `+++{...}`; the old parser skipped it.
        bad = b"++" + self._raw(_entry(TS_W30, eid="bad"))
        with tempfile.TemporaryDirectory() as t:
            repo, shard = self._repo(t, [self._raw(_entry(TS_W30, eid="a"))])
            self._append(shard, [bad])
            self._git(repo, "add", "-A")
            out = lpl.lint_staged_validity(repo_root=repo)
        self.assertEqual(len(out), 1, out)
        self.assertIn("invalid JSON", out[0]["error"])
        self.assertEqual(out[0]["line"], bad.decode())
        self.assertEqual(out[0]["path"], self.REL)

    def test_non_utf8_line_is_flagged_not_skipped(self) -> None:
        # Broken JSON AND a non-UTF-8 byte: must surface, never vanish.
        with tempfile.TemporaryDirectory() as t:
            repo, shard = self._repo(t, [self._raw(_entry(TS_W30, eid="a"))])
            self._append(shard, [b'{"a": "\xff"'])
            self._git(repo, "add", "-A")
            out = lpl.lint_staged_validity(repo_root=repo)  # must not raise
        self.assertEqual(len(out), 1, out)
        self.assertEqual(out[0]["error"], "not valid UTF-8")
        self.assertEqual(out[0]["path"], self.REL)
        self.assertEqual(out[0]["line"], '{"a": "�"')
        self.assertEqual(len(lpl.format_validity_lines(out)), 1)

    def test_non_utf8_line_does_not_hide_other_lines(self) -> None:
        bad = b"++" + self._raw(_entry(TS_W30, eid="bad"))
        ok = self._raw(_entry(TS_W30, eid="ok"))
        with tempfile.TemporaryDirectory() as t:
            repo, shard = self._repo(t, [self._raw(_entry(TS_W30, eid="a"))])
            self._append(shard, [b'{"id": "\xff\xfe"}', ok, bad])
            self._git(repo, "add", "-A")
            out = lpl.lint_staged_validity(repo_root=repo)
        self.assertEqual([v["error"] for v in out], ["not valid UTF-8", out[1]["error"]])
        self.assertIn("invalid JSON", out[1]["error"])
        self.assertEqual(out[1]["line"], bad.decode())

    def test_range_non_utf8_misplaced_entry_is_still_judged(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo, shard = self._repo(t, [self._raw(_entry(TS_W30, eid="ok"))])
            base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()
            line = json.dumps({"id": "bad", "ts": TS_W31, "agent": "mason", "n": "X"}).encode()
            self._append(shard, [line.replace(b'"X"', b'"\xff"')])
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-q", "-m", "tip")
            out = lpl.lint_range(base, "HEAD", repo_root=repo)
        self.assertEqual([m["id"] for m in out], ["bad"])

    def test_range_misplaced_entry_with_raw_u2028_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo, shard = self._repo(t, [self._raw(_entry(TS_W30, eid="ok"))])
            base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()
            self._append(shard, [self._raw(_entry(TS_W31, eid="bad"), "p q")])
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-q", "-m", "tip")
            out = lpl.lint_range(base, "HEAD", repo_root=repo)
        self.assertEqual([m["id"] for m in out], ["bad"])

    def test_all_scan_misplaced_entry_with_raw_u2028_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            shard = Path(t) / "ledger" / self.SEG / f"{self.SEG}.jsonl"
            shard.parent.mkdir(parents=True)
            shard.write_bytes(self._raw(_entry(TS_W31, eid="bad"), "p q") + b"\n")
            out = lpl.lint_all(Path(t) / "ledger")
        self.assertEqual([m["id"] for m in out], ["bad"])


class TestStagedRenames(unittest.TestCase):
    """T-4451: a staged `git mv` of a ledger shard is status R. The validity check
    used to drop R (`--diff-filter=ACM`), so a renamed-and-edited shard was never
    validated; naively adding R would instead read a PURE rename as all-added
    lines and false-block the ledger groomer on any pre-existing bad line. Git's
    own rename pairing (`-M`) over the `ledger` pathspec gives the right scope:
    only genuinely added lines are judged, attributed to the destination path."""

    OLD = "ledger/2026-W30/2026-W30.jsonl"
    NEW = "ledger/2026-W30/2026-W30-regrouped.jsonl"

    def _git(self, repo: Path, *args: str) -> None:
        env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
               "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com"}
        subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, check=True, env=env)

    @staticmethod
    def _good(n: int) -> bytes:
        return json.dumps(_entry(TS_W30, eid=f"ok-{n}")).encode()

    @staticmethod
    def _bad(tag: str) -> bytes:
        return json.dumps(_entry(TS_W30, eid=tag)).encode()[:-1]  # missing "}"

    def _repo(self, t: str, base_lines: "list[bytes]", rel: str = None) -> Path:
        repo = Path(t) / "r"
        repo.mkdir()
        self._git(repo, "init", "-q")
        self._put(repo, rel or self.OLD, base_lines)
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-q", "-m", "base")
        return repo

    @staticmethod
    def _put(repo: Path, rel: str, lines: "list[bytes]") -> None:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(l + b"\n" for l in lines))

    def _move(self, repo: Path, old: str, new: str, lines: "list[bytes] | None" = None) -> None:
        (repo / new).parent.mkdir(parents=True, exist_ok=True)
        self._git(repo, "mv", old, new)
        if lines is not None:
            self._put(repo, new, lines)
        self._git(repo, "add", "-A")

    def _base(self) -> "list[bytes]":
        return [self._good(i) for i in range(6)]

    def test_pure_rename_of_a_shard_with_a_pre_existing_bad_line_is_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo = self._repo(t, self._base() + [self._bad("old-bad")])
            self._move(repo, self.OLD, self.NEW)
            self.assertEqual(lpl.lint_staged_validity(repo_root=repo), [])

    def test_rename_plus_a_valid_added_line_is_clean_despite_a_pre_existing_bad_line(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            base = self._base() + [self._bad("old-bad")]
            repo = self._repo(t, base)
            self._move(repo, self.OLD, self.NEW, base + [self._good(99)])
            self.assertEqual(lpl.lint_staged_validity(repo_root=repo), [])

    def test_rename_plus_an_invalid_added_line_is_flagged_at_the_destination(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            base = self._base() + [self._bad("old-bad")]
            repo = self._repo(t, base)
            new_bad = self._bad("new-bad")
            self._move(repo, self.OLD, self.NEW, base + [new_bad])
            out = lpl.lint_staged_validity(repo_root=repo)
        self.assertEqual(len(out), 1, out)
        self.assertIn("invalid JSON", out[0]["error"])
        self.assertEqual(out[0]["path"], self.NEW)
        self.assertEqual(out[0]["line"], new_bad.decode())

    def test_a_file_moved_into_ledger_from_outside_is_validated_in_full(self) -> None:
        """No source inside the `ledger` pathspec, so git sees a new file: every
        line counts as added (the bad one is judged, nothing is credited)."""
        with tempfile.TemporaryDirectory() as t:
            repo = self._repo(t, self._base() + [self._bad("staged-bad")], rel="scratch/pile.jsonl")
            self._move(repo, "scratch/pile.jsonl", self.NEW)
            out = lpl.lint_staged_validity(repo_root=repo)
        self.assertEqual(len(out), 1, out)
        self.assertEqual(out[0]["path"], self.NEW)

    def test_a_rename_out_of_the_ledger_tree_is_never_judged(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo = self._repo(t, self._base() + [self._bad("old-bad")])
            self._move(repo, self.OLD, "scratch/pile.jsonl")
            self.assertEqual(lpl.lint_staged_validity(repo_root=repo), [])

    # -- diff-prefix config must not blind the guard ---------------------------
    # Added lines are attributed by the diff's `+++ <prefix><path>` header. A seat
    # that sets diff.mnemonicPrefix (`+++ i/<path>`), diff.noprefix (`+++ <path>`) or
    # diff.dstPrefix would hand the attribution a path that is not `ledger/...`, and
    # the line would be silently SKIPPED -- the guard turned off, not failed. The
    # argv pins `--src-prefix=a/ --dst-prefix=b/`, which overrides all three.

    # (config key, value, header the config must produce, key exists in every git?)
    # diff.dstPrefix only exists in newer git (it is absent from 2.43): there the
    # precondition cannot hold, so that row skips instead of passing vacuously.
    PREFIX_CONFIGS = (
        ("diff.mnemonicPrefix", "true", "+++ i/", True),
        ("diff.noprefix", "true", "+++ ledger/", True),
        ("diff.dstPrefix", "zz/", "+++ zz/", False),
    )

    def _raw_staged_diff(self, repo: Path) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "-M", "--unified=0", "--", "ledger"],
            capture_output=True, text=True, check=True).stdout

    def _require_header(self, repo: Path, header: str, always: bool) -> None:
        raw = self._raw_staged_diff(repo)
        if header in raw:
            return
        if not always:
            self.skipTest("this git does not honour that diff config key")
        self.fail(f"precondition: the config must actually change the diff header; got {raw!r}")

    def test_a_rename_plus_an_invalid_line_is_flagged_under_any_diff_prefix_config(self) -> None:
        for key, value, header, always in self.PREFIX_CONFIGS:
            with self.subTest(config=f"{key}={value}"), tempfile.TemporaryDirectory() as t:
                base = self._base() + [self._bad("old-bad")]
                repo = self._repo(t, base)
                self._git(repo, "config", key, value)
                new_bad = self._bad("new-bad")
                self._move(repo, self.OLD, self.NEW, base + [new_bad])
                self._require_header(repo, header, always)
                out = lpl.lint_staged_validity(repo_root=repo)
                self.assertEqual(len(out), 1, out)
                self.assertEqual(out[0]["path"], self.NEW)
                self.assertEqual(out[0]["line"], new_bad.decode())

    def test_a_new_shard_with_an_invalid_line_is_flagged_under_any_diff_prefix_config(self) -> None:
        for key, value, header, always in self.PREFIX_CONFIGS:
            with self.subTest(config=f"{key}={value}"), tempfile.TemporaryDirectory() as t:
                repo = self._repo(t, self._base())
                self._git(repo, "config", key, value)
                self._put(repo, self.NEW, self._base() + [self._bad("new-bad")])
                self._git(repo, "add", "-A")
                self._require_header(repo, header, always)
                out = lpl.lint_staged_validity(repo_root=repo)
                self.assertEqual([v["path"] for v in out], [self.NEW], out)


class TestDiffToolConfigDoesNotBlindTheLint(unittest.TestCase):
    """T-4487: both lint diffs parse the patch BODY, so a seat-level `diff.external`
    (replaces the body with whatever the tool prints) or a `.gitattributes` textconv
    driver (diffs converted text, not the file's bytes) would hand the parser a body
    with no added lines -- the invalid line is silently never judged, the guard off
    rather than failed. The argv pins `--no-ext-diff --no-textconv` on both paths:
    the staged validity diff (pre-commit) and `_added_lines` (pre-push `--range`, whose
    judgement is placement: an added line is "invalid" there when it is misplaced).

    Each case first proves the config really blinds a plain `git diff` (the existing
    `_require_header` pattern), so the test cannot pass vacuously."""

    REL = "ledger/2026-W30/2026-W30.jsonl"
    MARK = "T4487-HIDDEN"
    CONFIGS = ("diff.external", "diff.<driver>.textconv")

    def _git(self, repo: Path, *args: str) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
               "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com"}
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, check=True, env=env).stdout.strip()

    @staticmethod
    def _tool(t: str, body: str) -> str:
        path = Path(t) / "difftool.sh"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return str(path)

    def _repo(self, t: str, config: str) -> "tuple[Path, Path]":
        """A repo whose seat config blinds `git diff`, with one good line committed."""
        repo = Path(t) / "r"
        repo.mkdir()
        self._git(repo, "init", "-q")
        if config == "diff.external":
            self._git(repo, "config", "diff.external", self._tool(t, "exit 0\n"))
        else:
            (repo / ".gitattributes").write_text("*.jsonl diff=blind\n")
            self._git(repo, "config", "diff.blind.textconv", self._tool(t, "echo converted\n"))
        shard = repo / self.REL
        shard.parent.mkdir(parents=True)
        shard.write_text(json.dumps(_entry(TS_W30, eid="ok")) + "\n")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-q", "-m", "base")
        return repo, shard

    def _assert_blinded(self, repo: Path, *diff_args: str) -> None:
        raw = self._git(repo, "diff", "--unified=0", *diff_args, "--", "ledger")
        self.assertNotIn(self.MARK, raw,
                         "precondition: the seat config must hide the added line from a plain diff")

    def test_an_unparseable_staged_line_is_flagged_under_a_hostile_diff_config(self) -> None:
        for config in self.CONFIGS:
            with self.subTest(config=config), tempfile.TemporaryDirectory() as t:
                repo, shard = self._repo(t, config)
                bad = json.dumps(_entry(TS_W30, eid=self.MARK))[:-1]  # missing "}"
                shard.write_text(shard.read_text() + bad + "\n")
                self._git(repo, "add", "-A")
                self._assert_blinded(repo, "--cached")
                out = lpl.lint_staged_validity(repo_root=repo)
                self.assertEqual([v["path"] for v in out], [self.REL], out)
                self.assertEqual(out[0]["line"], bad)

    def test_a_misplaced_added_line_is_flagged_by_the_range_scan_under_a_hostile_diff_config(self) -> None:
        for config in self.CONFIGS:
            with self.subTest(config=config), tempfile.TemporaryDirectory() as t:
                repo, shard = self._repo(t, config)
                base = self._git(repo, "rev-parse", "HEAD")
                shard.write_text(shard.read_text()
                                 + json.dumps(_entry(TS_W31, eid=self.MARK)) + "\n")  # W31 line in the W30 shard
                self._git(repo, "add", "-A")
                self._git(repo, "commit", "-q", "-m", "tip")
                self._assert_blinded(repo, f"{base}..HEAD")
                out = lpl.lint_range(base, "HEAD", repo_root=repo)
                self.assertEqual([m["id"] for m in out], [self.MARK], out)


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
                ledger_shard.append_entry(ledger, {"id": "no-ts", "agent": "mason"})

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
                "id": "x", "agent": "mason"}
        with mock.patch.object(lpl, "lint_default", return_value=[fake]):
            rc = lpl.main([])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
