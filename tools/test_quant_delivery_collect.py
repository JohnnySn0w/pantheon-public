from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import quant_delivery_collect as qd


def pr(**kw):
    base = {
        "repo": "pantheon-config",
        "number": 812,
        "source": "github-rest",
        "source_status": "ok",
        "title": "Ship T-4780 delivery collector",
        "body": "Implements T-4780.",
        "author": "mason",
        "url": "https://github.example/pull/812",
        "created_at": "2026-10-06T10:00:00Z",
        "merged_at": "2026-10-07T10:00:00Z",
        "closed_at": "2026-10-07T10:00:00Z",
        "head_ref": "agent/T-4780",
        "base_ref": "main",
        "merge_commit_sha": "abc123",
        "additions": 10,
        "deletions": 2,
        "changed_files": 1,
    }
    base.update(kw)
    return base


class QuantDeliveryTests(unittest.TestCase):
    def test_wilson_interval_returns_90_percent_bounds_and_empty_is_insufficient(self):
        self.assertIsNone(qd.wilson_interval(0, 0))
        got = qd.wilson_interval(1, 4)
        self.assertEqual(got["mid"], 0.25)
        self.assertLess(got["low"], got["mid"])
        self.assertGreater(got["high"], got["mid"])

    def test_ticket_parser_counts_rebounds_and_operator_touches(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "T-9999.md"
            path.write_text(
                """---
id: T-9999
created: 2026-10-01T00:00:00Z
state: done
parent_ticket: T-9000
labels: [observability]
---

# Quant delivery fixture

## Transition log

- REBOUND from reviewer.
- The operator TG 11575 changed the scope.
- TG 11576 follow-up.
""",
                encoding="utf-8",
            )
            got = qd.parse_ticket_file(path)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.ticket_id, "T-9999")
        self.assertEqual(got.parent_ticket, "T-9000")
        self.assertEqual(got.rebound_rounds, 1)
        self.assertEqual(got.operator_touches, 2)

    def test_enrich_pr_links_ticket_arc_and_lead_times(self):
        tickets = {
            "T-4780": qd.TicketInfo(
                ticket_id="T-4780",
                state="active",
                created_at="2026-10-05T10:00:00Z",
                parent_ticket="T-4700",
                title="collector",
                path="tickets/active/T-4780.md",
                rebound_rounds=2,
                operator_touches=1,
                operator_touch_ids=("TG-11575",),
                rollback_transitions=0,
            )
        }
        got = qd.enrich_pr(pr(), tickets, "run1", "2026-10-09T00:00:00Z")
        self.assertEqual(got["ticket_ids"], ["T-4780"])
        self.assertEqual(got["arc_id"], "T-4700")
        self.assertEqual(got["lead_time_pr_hours"], 24.0)
        self.assertEqual(got["lead_time_ticket_hours"], 48.0)
        self.assertEqual(got["rebound_rounds"], 2)
        self.assertEqual(got["operator_touches"], 1)
        self.assertEqual(got["operator_touch_ids"], ["TG-11575"])

    def test_join_finds_ticket_from_title_body_and_branch_then_folds_parent(self):
        tickets = {
            "T-1001": qd.TicketInfo("T-1001", "done", "2026-10-01T00:00:00Z", "T-1000", "body", "x", 0, 0, (), 0),
            "T-1002": qd.TicketInfo("T-1002", "done", "2026-10-01T00:00:00Z", "T-1000", "branch", "x", 0, 0, (), 0),
            "T-1003": qd.TicketInfo("T-1003", "done", "2026-10-01T00:00:00Z", "T-1000", "title", "x", 0, 0, (), 0),
        }
        got = qd.enrich_pr(
            pr(title="Ship T-1003", body="Also covers T-1001", head_ref="agent/T-1002"),
            tickets,
            "run1",
            "2026-10-09T00:00:00Z",
        )
        self.assertEqual(got["ticket_ids"], ["T-1001", "T-1002", "T-1003"])
        self.assertEqual(got["arc_id"], "T-1000")

    def test_automation_delivery_exclusions_cover_named_non_delivery_classes(self):
        cases = [
            (pr(title="tickets: claim T-4780"), "ticket.py"),
            (pr(title="ledger spine groom weekly"), "ledger"),
            (pr(title="research(watch) auto-commit"), "research"),
            (pr(author="github-actions[bot]"), "bot"),
        ]
        for item, needle in cases:
            excluded, reason = qd.is_automation_delivery(item)
            self.assertTrue(excluded)
            self.assertIn(needle.lower(), reason.lower())

    def test_classifier_avoids_rollback_body_false_positives_and_uses_subject_or_trailer(self):
        normal = qd.enrich_pr(
            pr(title="Ship T-1000", body="Rollback:\n- rollback file: old report\nThis is documentation."),
            {},
            "run1",
            "2026-10-09T00:00:00Z",
        )
        self.assertFalse(normal["is_revert"])
        self.assertFalse(normal["is_hotfix"])

        revert_title = qd.enrich_pr(pr(title="Revert \"Ship T-1000\""), {}, "run1", "2026-10-09T00:00:00Z")
        revert_trailer = qd.enrich_pr(
            pr(title="Undo T-1000", body="This reverts commit abcdef1234567."),
            {},
            "run1",
            "2026-10-09T00:00:00Z",
        )
        hotfix_label = qd.enrich_pr(
            pr(title="Repair T-1000", body="ordinary body", labels=["hotfix"]),
            {},
            "run1",
            "2026-10-09T00:00:00Z",
        )
        body_hotfix = qd.enrich_pr(
            pr(title="Repair T-1000", body="hotfix notes in a checklist"),
            {},
            "run1",
            "2026-10-09T00:00:00Z",
        )
        self.assertTrue(revert_title["is_revert"])
        self.assertTrue(revert_trailer["is_revert"])
        self.assertTrue(hotfix_label["is_hotfix"])
        self.assertFalse(body_hotfix["is_hotfix"])

    def test_ticket_rollback_transition_is_visible_but_not_pr_revert_by_inheritance(self):
        tickets = {
            "T-1000": qd.TicketInfo(
                "T-1000", "done", "2026-10-01T00:00:00Z", None, "rolled back", "x", 0, 0, (), 1
            )
        }
        got = qd.enrich_pr(pr(title="Follow up T-1000", body="normal prose"), tickets, "run1", "2026-10-09T00:00:00Z")
        self.assertFalse(got["is_revert"])
        self.assertEqual(got["ticket_rollback_transitions"], 1)

    def test_weekly_records_exclude_automation_pin_failure_interval_and_distinct_tg_ids(self):
        rows = [
            qd.enrich_pr(
                pr(number=1, title="Ship T-1000", body="T-1000"),
                {"T-1000": qd.TicketInfo("T-1000", "done", "2026-10-01T00:00:00Z", None, "a", "x", 3, 1, ("TG-1",), 0)},
                "run1",
                "2026-10-09T00:00:00Z",
            ),
            qd.enrich_pr(
                pr(number=2, title="Hotfix T-1001", body="T-1001", merged_at="2026-10-08T10:00:00Z"),
                {"T-1001": qd.TicketInfo("T-1001", "done", "2026-10-01T00:00:00Z", None, "b", "x", 0, 1, ("TG-1",), 0)},
                "run1",
                "2026-10-09T00:00:00Z",
            ),
            qd.enrich_pr(
                pr(number=3, title="tickets: resolve T-1002", merged_at="2026-10-08T11:00:00Z"),
                {},
                "run1",
                "2026-10-09T00:00:00Z",
            ),
        ]
        got = qd.weekly_records(rows, "run1", "2026-10-09T00:00:00Z", {"status": "insufficient-source", "readings": 0})
        repo_row = [r for r in got if r["repo"] == "pantheon-config"][0]
        self.assertEqual(repo_row["merged_prs"], 3)
        self.assertEqual(repo_row["delivery_prs"], 2)
        self.assertEqual(repo_row["excluded_prs"], 1)
        self.assertEqual(repo_row["change_failures"], 1)
        self.assertEqual(repo_row["change_failure_breakdown"], {"hotfix_prs": 1, "revert_prs": 0, "rollback_prs": 0})
        self.assertEqual(repo_row["failure_rate_wilson90"]["mid"], 0.5)
        self.assertEqual(repo_row["operator_touch_ids"], ["TG-1"])
        self.assertEqual(repo_row["operator_touches"], 1)
        self.assertEqual(repo_row["quota_source_status"], "insufficient-source")

    def test_change_failure_counts_rollbacks_but_not_rebounds(self):
        rows = [
            qd.enrich_pr(
                pr(number=1, title="Ship T-1000", body="T-1000"),
                {"T-1000": qd.TicketInfo("T-1000", "done", "2026-10-01T00:00:00Z", None, "a", "x", 4, 0, (), 0)},
                "run1",
                "2026-10-09T00:00:00Z",
            ),
            qd.enrich_pr(
                pr(number=2, title="Ship T-1001", body="T-1001", merged_at="2026-10-08T10:00:00Z"),
                {"T-1001": qd.TicketInfo("T-1001", "done", "2026-10-01T00:00:00Z", None, "b", "x", 0, 0, (), 1)},
                "run1",
                "2026-10-09T00:00:00Z",
            ),
        ]
        got = qd.weekly_records(rows, "run1", "2026-10-09T00:00:00Z", {"status": "insufficient-source", "readings": 0})
        repo_row = [r for r in got if r["repo"] == "pantheon-config"][0]
        self.assertEqual(repo_row["change_failures"], 1)
        self.assertEqual(repo_row["change_failure_breakdown"], {"hotfix_prs": 0, "revert_prs": 0, "rollback_prs": 1})

        arcs = qd.arc_records(rows, "run1", "2026-10-09T00:00:00Z")
        self.assertEqual(sum(row["change_failures"] for row in arcs), 1)
        self.assertEqual([row for row in arcs if row["change_failures"]][0]["failed_pr_refs"], ["pantheon-config#2"])

    def test_local_git_fallback_counts_squash_and_merge_pr_shapes(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocesses = [
                ["git", "init", "-b", "main"],
                ["git", "config", "user.email", "test@example.com"],
                ["git", "config", "user.name", "Test"],
            ]
            for cmd in subprocesses:
                subprocess.run(cmd, cwd=repo, check=True, stdout=subprocess.DEVNULL)
            (repo / "a.txt").write_text("a\n", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.DEVNULL)

            (repo / "a.txt").write_text("a\nsquash\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "Ship squash thing (#41)"], cwd=repo, check=True, stdout=subprocess.DEVNULL)

            subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            (repo / "a.txt").write_text("a\nsquash\nmerge\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "feature"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "merge", "--no-ff", "feature", "-m", "Merge pull request #42 from feature"], cwd=repo, check=True, stdout=subprocess.DEVNULL)

            (repo / "a.txt").write_text("a\nsquash\nmerge\nplain\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "plain non-pr maintenance"], cwd=repo, check=True, stdout=subprocess.DEVNULL)

            rows, status, note = qd.local_git_prs(qd.RepoSpec("fixture", None, repo), qd.parse_time("2026-01-01T00:00:00Z"))

        self.assertEqual(status, "ok")
        self.assertIsNone(note)
        self.assertEqual([row["number"] for row in rows], [42, 41])
        self.assertTrue(all(row["source"] == "local-git" for row in rows))
        self.assertTrue(all(row["source_status"] == "insufficient-source" for row in rows))

    def test_ticket_parser_tracks_distinct_tg_ids_and_timestamped_rollback_transitions(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "T-9998.md"
            path.write_text(
                """---
id: T-9998
created: 2026-10-01T00:00:00Z
state: done
parent_ticket: null
---

# Rollback fixture

## Transition log

- The operator TG 11575 set scope.
- TG 11575 repeated in a later note.
- 2026-10-02T00:00:00Z [mason] note @abc; rollback applied after review.
""",
                encoding="utf-8",
            )
            got = qd.parse_ticket_file(path)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.operator_touch_ids, ("TG-11575",))
        self.assertEqual(got.operator_touches, 1)
        self.assertEqual(got.rollback_transitions, 1)

    def test_ticket_parser_does_not_count_rollback_sections_as_transitions(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "T-9997.md"
            path.write_text(
                """---
id: T-9997
created: 2026-10-01T00:00:00Z
state: done
parent_ticket: null
---

# Rollback prose fixture

## Ask

Rollback:
- rollback file: old report

## Transition log

- 2026-10-02T00:00:00Z [mason] note @abc; ordinary note, no failure.
""",
                encoding="utf-8",
            )
            got = qd.parse_ticket_file(path)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.rollback_transitions, 0)

    def test_ticket_parser_does_not_count_hypothetical_or_referenced_rollbacks(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "T-9996.md"
            path.write_text(
                """---
id: T-9996
created: 2026-10-01T00:00:00Z
state: done
parent_ticket: null
---

# Referenced rollback fixture

## Transition log

- 2026-10-02T00:00:00Z [mason] note @abc; REVIEW found a gate which would have rolled back on Recovery.
- 2026-10-02T01:00:00Z [mason] note @def; CHARACTERIZATION from another ticket says attempt 2 rolled back, but this ticket did not.
""",
                encoding="utf-8",
            )
            got = qd.parse_ticket_file(path)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.rollback_transitions, 0)

    def test_collect_writes_deterministic_feed_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "repos.json"
            out = root / "out"
            config.write_text(
                json.dumps(
                    {
                        "repos": [
                            {"name": "pantheon-config", "github": "example-org/pantheon-config", "local_path": str(root / "missing")}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            fake_prs = [
                pr(number=7, title="Ship T-4780", body="T-4780", merged_at="2026-10-07T10:00:00Z"),
                pr(number=8, title="tickets: claim T-4780", merged_at="2026-10-07T11:00:00Z"),
            ]
            with patch.object(qd, "now_utc", return_value=qd.parse_time("2026-10-09T00:00:00Z")):
                with patch.object(qd, "fetch_github_prs", return_value=(fake_prs, "ok", None)):
                    with patch.object(qd, "load_tickets", return_value={}):
                        with patch.object(qd, "latest_fleet_usage", return_value={"status": "ok", "used_percentage": 50, "readings": 2}):
                            run = qd.collect(60, config, out)

            self.assertEqual(run["records"]["delivery_prs"], 2)
            self.assertTrue((out / "schema" / "version.json").exists())
            prs = [json.loads(line) for line in (out / "delivery_prs.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["number"] for row in prs], [7, 8])
            self.assertEqual(prs[0]["arc_id"], "T-4780")
            weekly = [json.loads(line) for line in (out / "delivery_weekly.jsonl").read_text(encoding="utf-8").splitlines()]
            all_row = [row for row in weekly if row["repo"] == "ALL"][0]
            expected_weekly = {
                "delivery_prs": 1,
                "excluded_prs": 1,
                "merged_prs": 2,
                "quota_percent_per_pr": 50.0,
                "repo": "ALL",
                "week_start": "2026-10-05",
            }
            self.assertEqual({k: all_row[k] for k in expected_weekly}, expected_weekly)


if __name__ == "__main__":
    unittest.main()
