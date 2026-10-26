"""Tests for tools.github_companion_watch.

Run:
    python3 -m unittest tools.test_github_companion_watch -v
"""

from __future__ import annotations

import json
import unittest
import urllib.parse
from datetime import date

from tools import github_companion_watch as gcw


def _item(
    name: str,
    *,
    created_at: str,
    pushed_at: str,
    stars: int = 0,
    topics: list[str] | None = None,
) -> dict[str, object]:
    return {
        "full_name": name,
        "html_url": f"https://github.com/{name}",
        "description": f"{name} description",
        "created_at": created_at,
        "pushed_at": pushed_at,
        "stargazers_count": stars,
        "forks_count": 1,
        "open_issues_count": 2,
        "language": "Python",
        "license": {"spdx_id": "MIT"},
        "topics": topics or ["personal-ai"],
        "archived": False,
        "fork": False,
        "homepage": "",
    }


class GitHubCompanionWatchTests(unittest.TestCase):
    def test_build_query_uses_topic_and_lane_date_qualifier(self) -> None:
        lane = gcw.LANES[0]
        self.assertEqual(
            gcw.build_query("personal-ai", lane, date(2026, 6, 16)),
            "topic:personal-ai pushed:>=2026-06-16",
        )

    def test_search_url_encodes_query_and_updated_sort(self) -> None:
        url = gcw.search_url("topic:ai-companion created:>=2026-06-16", page=2, per_page=50)
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "api.github.com")
        self.assertEqual(params["q"], ["topic:ai-companion created:>=2026-06-16"])
        self.assertEqual(params["sort"], ["updated"])
        self.assertEqual(params["order"], ["desc"])
        self.assertEqual(params["page"], ["2"])
        self.assertEqual(params["per_page"], ["50"])

    def test_run_query_sorts_created_lane_locally_by_created_at(self) -> None:
        seen_urls: list[str] = []

        def fetch(_url: str, _token: str | None) -> dict[str, object]:
            seen_urls.append(_url)
            return {
                "total_count": 2,
                "incomplete_results": False,
                "items": [
                    _item("old/repo", created_at="2026-01-01T00:00:00Z", pushed_at="2026-10-13T00:00:00Z"),
                    _item("new/repo", created_at="2026-10-01T00:00:00Z", pushed_at="2026-10-01T00:00:00Z"),
                ],
            }

        result = gcw.run_query(
            "personal-ai",
            gcw.LANES[1],
            since=date(2026, 6, 16),
            limit=1,
            sample_size=2,
            fetch=fetch,
        )

        self.assertEqual([hit.full_name for hit in result.hits], ["new/repo"])
        self.assertEqual(result.query, "topic:personal-ai created:>=2026-06-16")
        params = urllib.parse.parse_qs(urllib.parse.urlparse(seen_urls[0]).query)
        self.assertEqual(params["per_page"], ["2"])

    def test_run_query_refuses_sample_smaller_than_limit(self) -> None:
        with self.assertRaises(ValueError):
            gcw.run_query(
                "personal-ai",
                gcw.LANES[0],
                since=date(2026, 6, 16),
                limit=5,
                sample_size=4,
                fetch=lambda _url, _token: {"total_count": 0, "incomplete_results": False, "items": []},
            )

    def test_run_queries_covers_default_terms_and_two_lanes(self) -> None:
        seen: list[str] = []

        def fetch(url: str, _token: str | None) -> dict[str, object]:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
            seen.append(query)
            return {"total_count": 0, "incomplete_results": False, "items": []}

        results = gcw.run_queries(
            gcw.DEFAULT_TERMS,
            today=date(2026, 10, 14),
            since_days=120,
            limit=1,
            fetch=fetch,
        )

        self.assertEqual(len(results), len(gcw.DEFAULT_TERMS) * 2)
        self.assertIn("topic:personal-ai pushed:>=2026-06-16", seen)
        self.assertIn("topic:self-hosted-ai created:>=2026-06-16", seen)

    def test_json_and_markdown_outputs_are_renderable(self) -> None:
        result = gcw.QueryResult(
            term="personal-ai",
            lane="recently-pushed",
            query="topic:personal-ai pushed:>=2026-06-16",
            total_count=1,
            incomplete_results=False,
            hits=(
                gcw.repo_hit(
                    "personal-ai",
                    gcw.LANES[0],
                    1,
                    _item("owner/repo", created_at="2026-10-01T00:00:00Z", pushed_at="2026-10-13T00:00:00Z", stars=9),
                ),
            ),
        )

        payload = gcw.as_jsonable([result])
        json.dumps(payload)
        markdown = gcw.render_markdown([result], generated_on=date(2026, 10, 14))
        self.assertIn("# GitHub companion-memory entrant queries", markdown)
        self.assertIn("[owner/repo](https://github.com/owner/repo)", markdown)
        self.assertIn("topic:personal-ai pushed:>=2026-06-16", markdown)


if __name__ == "__main__":
    unittest.main()
