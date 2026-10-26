#!/usr/bin/env python3
"""Tests for welltrod_stats.py (T-1811).

Hermetic: tmpdir repo roots + fabricated session reports; no sudo, no live
git push (tmp repo with a local bare origin), no Telegram (tool has none).

Run: python3 -m unittest tools.test_welltrod_stats -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import welltrod_stats as ws


def fake_report(session_id, gov_touches, handoff=True):
    """Minimal analyze_session()-shaped dict for aggregation tests.
    gov_touches: list of (path, count)."""
    return {
        "session_id": session_id,
        "tool_use_total": 1,
        "tool_use_by_tool": {},
        "task_spawns": 0,
        "boot": {
            "handoff_read": handoff,
            "repo_readme_read": False,
            "governance_touched": [],
        },
        "governance_touches": [{"path": p, "count": n} for p, n in gov_touches],
        "index_chain": [],
        "bash_touches": [],
        "top_files": [],
        "touched_paths": [p for p, _ in gov_touches],
    }


def make_gov_repo(root):
    """Governance inventory: two sop docs + one adr + top README."""
    (root / "sop" / "standard").mkdir(parents=True)
    (root / "architecture" / "adr").mkdir(parents=True)
    (root / "sop" / "standard" / "alpha.md").write_text("a\n")
    (root / "sop" / "standard" / "beta.md").write_text("b\n")
    (root / "architecture" / "adr" / "ORG-001.md").write_text("c\n")
    (root / "README.md").write_text("r\n")


class BuildSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_gov_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_well_trod_is_distinct_sessions_not_touches(self):
        reports = {
            "recovery": [
                # one session grinding alpha 10x
                fake_report("s1", [("sop/standard/alpha.md", 10)]),
            ],
            "orchestrator": [
                # two sessions touching beta once each
                fake_report("t1", [("sop/standard/beta.md", 1)]),
                fake_report("t2", [("sop/standard/beta.md", 1)]),
            ],
        }
        snap = ws.build_snapshot(reports, str(self.root), 14)
        by_path = {d["path"]: d for d in snap["docs"]}
        self.assertEqual(by_path["sop/standard/beta.md"]["well_trod"], 2)
        self.assertEqual(by_path["sop/standard/alpha.md"]["well_trod"], 1)
        self.assertEqual(by_path["sop/standard/alpha.md"]["touches"], 10)
        # beta (well_trod 2) ranks above alpha (well_trod 1, touches 10)
        self.assertEqual(snap["docs"][0]["path"], "sop/standard/beta.md")

    def test_per_agent_split_and_key_normalization(self):
        # Same doc reached via absolute and repo-relative paths must merge.
        reports = {
            "recovery": [fake_report("s1", [("/home/x/repo/sop/standard/alpha.md", 2)])],
            "specialist": [fake_report("a1", [("sop/standard/alpha.md", 1)])],
        }
        snap = ws.build_snapshot(reports, str(self.root), 14)
        by_path = {d["path"]: d for d in snap["docs"]}
        alpha = by_path["sop/standard/alpha.md"]
        self.assertEqual(alpha["well_trod"], 2)
        self.assertEqual(alpha["touches"], 3)
        self.assertEqual(alpha["per_agent"]["recovery"], {"sessions": 1, "touches": 2})
        self.assertEqual(alpha["per_agent"]["specialist"], {"sessions": 1, "touches": 1})

    def test_boot_adherence_and_totals(self):
        reports = {
            "recovery": [
                fake_report("s1", [], handoff=True),
                fake_report("s2", [], handoff=False),
                fake_report("s3", [], handoff=True),
            ],
            "orchestrator": [],
        }
        snap = ws.build_snapshot(reports, str(self.root), 14)
        self.assertEqual(snap["agents"]["recovery"]["boot_handoff_read"], 2)
        self.assertAlmostEqual(snap["agents"]["recovery"]["boot_handoff_rate"], 0.667)
        self.assertIsNone(snap["agents"]["orchestrator"]["boot_handoff_rate"])
        self.assertEqual(snap["totals"]["sessions_analyzed"], 3)

    def test_coverage_never_touched(self):
        reports = {"recovery": [fake_report("s1", [("sop/standard/alpha.md", 1)])]}
        snap = ws.build_snapshot(reports, str(self.root), 14)
        cov = snap["coverage"]
        self.assertEqual(cov["inventory_size"], 4)  # alpha, beta, ORG-001, README
        self.assertEqual(cov["touched_count"], 1)
        self.assertIn("sop/standard/beta.md", cov["never_touched"])
        self.assertIn("architecture/adr/ORG-001.md", cov["never_touched"])
        self.assertNotIn("sop/standard/alpha.md", cov["never_touched"])

    def test_handoff_counts_outside_inventory(self):
        # HANDOFF.md is well-trod but not in the sop/adr/rulebook inventory;
        # it must appear in docs[] without inflating coverage.touched_count.
        reports = {"recovery": [fake_report("s1", [("/home/x/HANDOFF.md", 1)])]}
        snap = ws.build_snapshot(reports, str(self.root), 14)
        self.assertEqual(snap["docs"][0]["path"], "HANDOFF.md")
        self.assertEqual(snap["coverage"]["touched_count"], 0)


class RemoteListingTest(unittest.TestCase):
    """The sudo find -newermt path, hermetic via a canned subprocess.run.

    (welltrod_stats uses subprocess.run only — no Popen — so patching run
    covers the whole surface.)"""

    CFG = {"user": "welltrod-other-seat", "projects_dir": "/home/other/p"}  # never the running account

    def _completed(self, rc, stdout="", stderr=""):
        return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)

    def test_parses_sorts_filters_and_caps(self):
        stdout = (
            "1721385600.5 /x/older.jsonl\n"
            "1721385800.5 /x/agent-sub.jsonl\n"
            "1721385900.5 /x/a with space.jsonl\n"
            "1721385700.5 /x/mid.jsonl\n"
            "garbage-line\n"
        )
        with mock.patch.object(
            ws.subprocess, "run", return_value=self._completed(0, stdout)
        ):
            got = ws.list_sessions_in_window("orchestrator", self.CFG, 0, 10)
        # newest-first, agent-* subagent transcript dropped, space path intact
        self.assertEqual(
            got, ["/x/a with space.jsonl", "/x/mid.jsonl", "/x/older.jsonl"]
        )
        with mock.patch.object(
            ws.subprocess, "run", return_value=self._completed(0, stdout)
        ):
            capped = ws.list_sessions_in_window("orchestrator", self.CFG, 0, 1)
        self.assertEqual(capped, ["/x/a with space.jsonl"])

    def test_nonzero_rc_is_failure_sentinel_not_empty(self):
        # Broken sudo grant must NOT publish as "0 sessions".
        with mock.patch.object(
            ws.subprocess,
            "run",
            return_value=self._completed(1, "", "sudo: a password is required"),
        ):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                got = ws.list_sessions_in_window("orchestrator", self.CFG, 0, 10)
        self.assertIsNone(got)
        self.assertIn("rc=1", err.getvalue())

    def test_oserror_is_failure_sentinel(self):
        with mock.patch.object(ws.subprocess, "run", side_effect=OSError("boom")):
            with contextlib.redirect_stderr(io.StringIO()):
                got = ws.list_sessions_in_window("orchestrator", self.CFG, 0, 10)
        self.assertIsNone(got)


class OwnAccountListingTest(unittest.TestCase):
    """T-4671: when the collector runs AS the target account, no sudo (sudo-to-self
    fails and would read as listing_failed)."""

    def test_both_listings_run_without_sudo_for_own_account(self):
        import getpass
        cfg = {"user": getpass.getuser(), "projects_dir": "/x/p"}
        done = subprocess.CompletedProcess([], 0, stdout="1721385600.5 /x/p/s.jsonl\n", stderr="")
        for fn in (ws.list_session_entries, ws.list_subagent_entries):
            with mock.patch.object(ws.subprocess, "run", return_value=done) as run:
                self.assertIsNotNone(fn("self", cfg, 0))
            self.assertEqual(run.call_args[0][0][:2], ["bash", "-c"], fn.__name__)


class ListingFailurePropagationTest(unittest.TestCase):
    def test_failed_agent_marked_in_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            make_gov_repo(Path(td))

            def gather(agent, cfg, cutoff, mx, rr):
                if agent == "orchestrator":
                    return None  # listing failed
                return [fake_report("s1", [("sop/standard/alpha.md", 1)])]

            snap = ws.collect(td, 14, 10, gather_fn=gather)
        self.assertTrue(snap["agents"]["orchestrator"]["listing_failed"])
        self.assertEqual(snap["agents"]["orchestrator"]["sessions_analyzed"], 0)
        self.assertNotIn("listing_failed", snap["agents"]["recovery"])


class WindowListingTest(unittest.TestCase):
    def test_local_window_filters_by_mtime_and_caps(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            old = d / "old.jsonl"
            new1 = d / "new1.jsonl"
            new2 = d / "new2.jsonl"
            agent_side = d / "agent-xyz.jsonl"  # subagent — must be dropped
            for f in (old, new1, new2, agent_side):
                f.write_text("{}\n")
            past = 1_000_000
            os.utime(old, (past, past))
            cutoff = past + 10
            cfg = {"user": None, "projects_dir": td}
            got = ws.list_sessions_in_window("recovery", cfg, cutoff, 10)
            names = {os.path.basename(p) for p in got}
            self.assertEqual(names, {"new1.jsonl", "new2.jsonl"})
            got_capped = ws.list_sessions_in_window("recovery", cfg, cutoff, 1)
            self.assertEqual(len(got_capped), 1)


class SnapshotIOTest(unittest.TestCase):
    def test_write_and_prune_history(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hist = root / ws.SNAPSHOT_DIR / "history"
            hist.mkdir(parents=True)
            stale = hist / "2020-01-01.json"
            stale.write_text("{}\n")
            snap = {"schema_version": 1, "generated_at": "2026-07-19T00:00:00Z"}
            scope = ws.write_snapshot(snap, td)
            self.assertEqual(scope, [ws.SNAPSHOT_DIR + "/"])
            latest = root / ws.SNAPSHOT_DIR / "latest.json"
            self.assertTrue(latest.exists())
            self.assertEqual(json.loads(latest.read_text())["schema_version"], 1)
            self.assertTrue((hist / "2026-07-19.json").exists())
            self.assertFalse(stale.exists())


class CliModeTest(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ws.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_no_mode_refuses_64(self):
        rc, _, err = self.run_main([])
        self.assertEqual(rc, ws.EX_USAGE)
        self.assertIn("refusing", err)

    def test_collect_without_mode_refuses_64(self):
        rc, _, _ = self.run_main(["--collect"])
        self.assertEqual(rc, ws.EX_USAGE)

    def test_conflicting_modes_refuse(self):
        rc, _, err = self.run_main(["--collect", "--commit-prod", "--dry-run"])
        self.assertEqual(rc, ws.EX_USAGE)
        self.assertIn("conflict", err)

    def test_unknown_agent_refuses(self):
        rc, _, _ = self.run_main(["--collect", "--dry-run", "--agents", "nobody"])
        self.assertEqual(rc, ws.EX_USAGE)

    def test_serve_mode_no_snapshot_is_renderable_empty_state(self):
        # exit 0: kino drops stdout on non-zero exit, and "collector hasn't
        # run yet" must reach the panel as a payload, not a 500.
        with tempfile.TemporaryDirectory() as td:
            rc, out, _ = self.run_main(["--format", "json", "--repo-root", td])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["error"], "no_snapshot")

    def test_serve_mode_prints_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / ws.SNAPSHOT_DIR
            base.mkdir(parents=True)
            (base / "latest.json").write_text('{"schema_version": 1}\n')
            rc, out, _ = self.run_main(["--format", "json", "--repo-root", td])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out), {"schema_version": 1})

    def test_dry_run_emits_json_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            make_gov_repo(Path(td))
            fake = {"recovery": [fake_report("s1", [("sop/standard/alpha.md", 1)])]}
            with mock.patch.object(
                ws, "gather_session_reports",
                side_effect=lambda agent, cfg, cut, mx, rr: fake.get(agent, []),
            ):
                rc, out, _ = self.run_main(
                    ["--collect", "--dry-run", "--repo-root", td]
                )
            self.assertEqual(rc, 0)
            snap = json.loads(out)
            self.assertEqual(snap["schema_version"], ws.SCHEMA_VERSION)
            self.assertEqual(snap["docs"][0]["path"], "sop/standard/alpha.md")
            self.assertFalse((Path(td) / ws.SNAPSHOT_DIR).exists())


class CommitProdTest(unittest.TestCase):
    """--commit-prod against a tmp git repo with a local bare origin.

    setUp points the git-mutation lockfile at a tmpdir: commit_scoped anchors
    its lock on REPO_ROOT's .git regardless of cwd, so without the override
    this suite would contend with live ticket.py/grooming writers."""

    def setUp(self):
        self._lock_td = tempfile.TemporaryDirectory()
        self._old_lock = os.environ.get("PANTHEON_GIT_MUTATION_LOCKFILE")
        os.environ["PANTHEON_GIT_MUTATION_LOCKFILE"] = os.path.join(
            self._lock_td.name, "mutation.lock"
        )

    def tearDown(self):
        if self._old_lock is None:
            os.environ.pop("PANTHEON_GIT_MUTATION_LOCKFILE", None)
        else:
            os.environ["PANTHEON_GIT_MUTATION_LOCKFILE"] = self._old_lock
        self._lock_td.cleanup()

    def _git(self, cwd, *args):
        subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        )

    def test_commit_prod_commits_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin.git"
            work = Path(td) / "work"
            subprocess.run(
                ["git", "init", "--bare", "-b", "main", str(origin)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "clone", str(origin), str(work)],
                check=True, capture_output=True,
            )
            self._git(work, "config", "user.email", "t@t")
            self._git(work, "config", "user.name", "t")
            make_gov_repo(work)
            self._git(work, "add", "-A")
            self._git(work, "commit", "-m", "init")
            self._git(work, "push", "origin", "HEAD:main")

            fake = {"recovery": [fake_report("s1", [("sop/standard/alpha.md", 1)])]}
            with mock.patch.object(
                ws, "gather_session_reports",
                side_effect=lambda agent, cfg, cut, mx, rr: fake.get(agent, []),
            ):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = ws.main(
                        ["--collect", "--commit-prod", "--repo-root", str(work)]
                    )
            self.assertEqual(rc, 0, err.getvalue())
            log = subprocess.run(
                ["git", "log", "--format=%s", "-1"],
                cwd=work, check=True, capture_output=True, text=True,
            ).stdout
            self.assertIn("welltrod", log)
            self.assertTrue((work / ws.SNAPSHOT_DIR / "latest.json").exists())
            # pushed to origin too (no --no-push)
            remote_log = subprocess.run(
                ["git", "log", "--format=%s", "-1", "main"],
                cwd=origin, check=True, capture_output=True, text=True,
            ).stdout
            self.assertIn("welltrod", remote_log)

    def test_commit_prod_stages_pruned_history_deletion(self):
        # A stale committed history file must land in the commit as a
        # DELETION (git add -- <dir>/ stages removals) — the only write
        # behavior the fresh-add test doesn't prove.
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin.git"
            work = Path(td) / "work"
            subprocess.run(
                ["git", "init", "--bare", "-b", "main", str(origin)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "clone", str(origin), str(work)],
                check=True, capture_output=True,
            )
            self._git(work, "config", "user.email", "t@t")
            self._git(work, "config", "user.name", "t")
            make_gov_repo(work)
            stale = work / ws.SNAPSHOT_DIR / "history" / "2020-01-01.json"
            stale.parent.mkdir(parents=True)
            stale.write_text("{}\n")
            self._git(work, "add", "-A")
            self._git(work, "commit", "-m", "init with stale history")
            self._git(work, "push", "origin", "HEAD:main")

            fake = {"recovery": [fake_report("s1", [("sop/standard/alpha.md", 1)])]}
            with mock.patch.object(
                ws, "gather_session_reports",
                side_effect=lambda agent, cfg, cut, mx, rr: fake.get(agent, []),
            ):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = ws.main(
                        ["--collect", "--commit-prod", "--repo-root", str(work)]
                    )
            self.assertEqual(rc, 0, err.getvalue())
            self.assertFalse(stale.exists())
            tracked = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "HEAD"],
                cwd=work, check=True, capture_output=True, text=True,
            ).stdout
            self.assertNotIn("2020-01-01.json", tracked)
            self.assertIn(ws.SNAPSHOT_DIR + "/latest.json", tracked)

    def test_prod_fire_routes_through_worktree(self):
        # Without --repo-root the write+commit must happen inside
        # with_worktree(fn) — never in the live shared checkout.
        from tools import _git_utils

        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin.git"
            work = Path(td) / "wt"
            subprocess.run(
                ["git", "init", "--bare", "-b", "main", str(origin)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "clone", str(origin), str(work)],
                check=True, capture_output=True,
            )
            self._git(work, "config", "user.email", "t@t")
            self._git(work, "config", "user.name", "t")
            make_gov_repo(work)
            self._git(work, "add", "-A")
            self._git(work, "commit", "-m", "init")
            self._git(work, "push", "origin", "HEAD:main")

            def fake_with_worktree(fn):
                return fn(work)

            fake = {"recovery": [fake_report("s1", [("sop/standard/alpha.md", 1)])]}
            with mock.patch.object(
                _git_utils, "with_worktree", side_effect=fake_with_worktree
            ) as wt_mock, mock.patch.object(
                ws, "gather_session_reports",
                side_effect=lambda agent, cfg, cut, mx, rr: fake.get(agent, []),
            ):
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(err):
                    rc = ws.main(["--collect", "--commit-prod", "--no-push"])
            self.assertEqual(rc, 0, err.getvalue())
            wt_mock.assert_called_once()
            log = subprocess.run(
                ["git", "log", "--format=%s", "-1"],
                cwd=work, check=True, capture_output=True, text=True,
            ).stdout
            self.assertIn("welltrod", log)
            self.assertTrue((work / ws.SNAPSHOT_DIR / "latest.json").exists())




def _sidechain_line(path):
    return json.dumps(
        {
            "isSidechain": True,
            "timestamp": "2026-10-01T00:00:00Z",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": path}}
                ]
            },
        }
    )


class SubagentListingTest(unittest.TestCase):
    """T-4509: subagent transcripts at <projects_dir>/<sid>/subagents/agent-*.jsonl."""

    CFG = {"user": "welltrod-other-seat", "projects_dir": "/home/other/p"}  # never the running account

    def test_local_finds_nested_agent_files_only_within_window(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "sess1" / "subagents").mkdir(parents=True)
            (d / "sess2" / "subagents").mkdir(parents=True)
            (d / "sess2" / "other").mkdir(parents=True)
            new = d / "sess1" / "subagents" / "agent-aaa.jsonl"
            old = d / "sess2" / "subagents" / "agent-old.jsonl"
            wrong_dir = d / "sess2" / "other" / "agent-zzz.jsonl"
            not_agent = d / "sess1" / "subagents" / "notes.jsonl"
            main = d / "main.jsonl"
            for f in (new, old, wrong_dir, not_agent, main):
                f.write_text("{}\n")
            os.utime(old, (1_000_000, 1_000_000))
            got = ws.list_subagent_entries("recovery", {"user": None, "projects_dir": td}, 1_000_010)
        self.assertEqual([p for _, p in got], [str(new)])

    def test_local_missing_projects_dir_is_listing_failure_not_zero(self):
        with tempfile.TemporaryDirectory() as td:
            missing = os.path.join(td, "nope")
            self.assertIsNone(ws.list_subagent_entries("recovery", {"user": None, "projects_dir": missing}, 0))

    def test_remote_find_argv_and_parse(self):
        stdout = "1721385600.5 /x/s/subagents/agent-a.jsonl\n1721385900.5 /x/s/subagents/agent-b b.jsonl\njunk\n"
        with mock.patch.object(
            ws.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""),
        ) as run:
            got = ws.list_subagent_entries("orchestrator", self.CFG, 100)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:4], ["sudo", "-n", "-u", "welltrod-other-seat"])
        for frag in ("-maxdepth 3", "*/subagents/*", "agent-*.jsonl", "-newermt @100"):
            self.assertIn(frag, cmd[-1])
        self.assertEqual(
            got,
            [
                (1721385900.5, "/x/s/subagents/agent-b b.jsonl"),
                (1721385600.5, "/x/s/subagents/agent-a.jsonl"),
            ],
        )

    def test_failures_are_the_none_sentinel(self):
        with mock.patch.object(
            ws.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="no"),
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertIsNone(ws.list_subagent_entries("orchestrator", self.CFG, 0))
        with mock.patch.object(ws.subprocess, "run", side_effect=OSError("boom")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertIsNone(ws.list_subagent_entries("orchestrator", self.CFG, 0))

    def test_main_listing_still_excludes_subagent_files(self):
        stdout = "1721385800.5 /x/s/subagents/agent-sub.jsonl\n1721385700.5 /x/mid.jsonl\n"
        with mock.patch.object(
            ws.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""),
        ) as run:
            got = ws.list_sessions_in_window("orchestrator", self.CFG, 0, 10)
        self.assertEqual(got, ["/x/mid.jsonl"])
        self.assertIn("-maxdepth 1", run.call_args[0][0][-1])


class SubagentGatherTest(unittest.TestCase):
    def test_all_sidechain_transcript_yields_events_and_stem_session_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            root.mkdir()
            make_gov_repo(root)
            proj = Path(td) / "proj"
            sub = proj / "sess1" / "subagents"
            sub.mkdir(parents=True)
            gov = str(root / "sop" / "standard" / "alpha.md")
            (sub / "agent-abc123.jsonl").write_text(_sidechain_line(gov) + "\n")
            cfg = {"user": None, "projects_dir": str(proj)}
            reports = ws.gather_subagent_reports("recovery", cfg, 0, 10, str(root))
        self.assertEqual([r["session_id"] for r in reports], ["agent-abc123"])
        self.assertEqual(reports[0]["governance_touches"][0]["count"], 1)
        self.assertIsNotNone(reports[0]["mtime"])

    def test_listing_failure_propagates_none(self):
        with mock.patch.object(ws, "list_subagent_entries", return_value=None):
            self.assertIsNone(ws.gather_subagent_reports("recovery", {"user": None, "projects_dir": "/x"}, 0, 10, "/r"))


class SubagentColumnTest(unittest.TestCase):
    def _run(self, td, sub_fn):
        make_gov_repo(Path(td))

        def gather(agent, cfg, cutoff, mx, rr):
            return [fake_report("m1", [("sop/standard/alpha.md", 2)])]

        return ws.collect_all(
            td, 14, 10, ["orchestrator"], gather_fn=gather, subagent_gather_fn=sub_fn
        )

    def test_separate_block_and_main_docs_unchanged(self):
        def sub(agent, cfg, cutoff, mx, rr):
            return [
                fake_report("agent-1", [("sop/standard/alpha.md", 1)]),
                fake_report("agent-2", [("sop/standard/beta.md", 3)]),
            ]

        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            snap, rows = self._run(t1, sub)
            base, base_rows = self._run(t2, None)
        for k in ("docs", "agents", "totals", "coverage", "schema_version"):
            self.assertEqual(snap[k], base[k])
        self.assertEqual(snap["schema_version"], 1)
        sa = snap["subagents"]
        self.assertEqual(sa["agents"]["orchestrator"]["sessions_analyzed"], 2)
        self.assertEqual(sa["totals"]["sessions_analyzed"], 2)
        by = {d["path"]: d for d in sa["docs"]}
        self.assertEqual(by["sop/standard/beta.md"]["well_trod"], 1)
        self.assertEqual(by["sop/standard/beta.md"]["touches"], 3)
        self.assertEqual(by["sop/standard/beta.md"]["per_agent"], {"orchestrator": {"sessions": 1, "touches": 3}})
        self.assertNotIn("subagents", base)
        self.assertEqual(
            sorted((r["session_id"], r.get("source")) for r in rows),
            [("agent-1", "subagent"), ("agent-2", "subagent"), ("m1", None)],
        )
        self.assertEqual(base_rows, [r for r in rows if r["session_id"] == "m1"])

    def test_subagent_listing_failure_is_flagged_in_subagent_block_only(self):
        with tempfile.TemporaryDirectory() as td:
            snap, rows = self._run(td, lambda *a: None)
        self.assertTrue(snap["subagents"]["agents"]["orchestrator"]["listing_failed"])
        self.assertEqual(snap["subagents"]["agents"]["orchestrator"]["sessions_analyzed"], 0)
        self.assertNotIn("listing_failed", snap["agents"]["orchestrator"])

    def test_subagents_are_cut_by_window(self):
        def sub(agent, cfg, cutoff, mx, rr):
            old = fake_report("agent-old", [("sop/standard/alpha.md", 1)])
            old["mtime"] = 1.0
            return [old, fake_report("agent-new", [("sop/standard/alpha.md", 1)])]

        with tempfile.TemporaryDirectory() as td:
            snap, _ = self._run(td, sub)
        self.assertEqual(snap["subagents"]["totals"]["sessions_analyzed"], 1)

    def test_injected_main_gather_alone_skips_subagents(self):
        # hermetic-skip rule: documented in collect_all
        with mock.patch.object(ws, "gather_subagent_reports") as g:
            with tempfile.TemporaryDirectory() as td:
                snap, _ = self._run(td, None)
        g.assert_not_called()
        self.assertNotIn("subagents", snap)

    def test_production_default_calls_real_subagent_gatherer(self):
        # No injection at all: the skip must NOT be the production behaviour.
        with tempfile.TemporaryDirectory() as td:
            make_gov_repo(Path(td))
            with mock.patch.object(
                ws, "gather_session_reports", return_value=[]
            ), mock.patch.object(
                ws,
                "gather_subagent_reports",
                return_value=[fake_report("agent-p", [("sop/standard/alpha.md", 1)])],
            ) as g:
                snap, rows = ws.collect_all(td, 14, 10, ["orchestrator"])
        self.assertEqual(g.call_count, 1)
        self.assertEqual(g.call_args[0][0], "orchestrator")
        self.assertEqual(snap["subagents"]["totals"]["sessions_analyzed"], 1)
        self.assertEqual(rows[0]["source"], "subagent")

    def test_cli_has_max_subagent_sessions_default_1000(self):
        args = ws.build_arg_parser().parse_args([])
        self.assertEqual(args.max_subagent_sessions, 1000)


if __name__ == "__main__":
    unittest.main()
