"""Query GitHub for small companion-memory entrants.

The grand-goal competitor watch cares about small repos that combine personal
AI, memory, local ownership, and companion framing. This helper turns the
standing weekly GitHub search into a repeatable export.

Usage:
    python3 -m tools.github_companion_watch
    python3 -m tools.github_companion_watch --format json --limit 20
    GITHUB_TOKEN=... python3 -m tools.github_companion_watch --since-days 180
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Callable, Iterable, Mapping, Sequence


DEFAULT_TERMS: tuple[str, ...] = (
    "personal-ai",
    "ai-companion",
    "agent-memory",
    "persistent-memory",
    "local-first",
    "self-hosted-ai",
)

DEFAULT_SINCE_DAYS = 120
DEFAULT_LIMIT = 10
DEFAULT_SAMPLE_SIZE = 50
GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
USER_AGENT = "pantheon-grand-goal-competitor-watch"


@dataclass(frozen=True)
class SearchLane:
    name: str
    qualifier: str
    date_field: str
    description: str


LANES: tuple[SearchLane, ...] = (
    SearchLane(
        name="recently-pushed",
        qualifier="pushed",
        date_field="pushed_at",
        description="Repos tagged with the term, sorted locally by newest push.",
    ),
    SearchLane(
        name="recently-created",
        qualifier="created",
        date_field="created_at",
        description="Repos tagged with the term, sorted locally by newest creation date.",
    ),
)


@dataclass(frozen=True)
class RepoHit:
    term: str
    lane: str
    rank: int
    full_name: str
    html_url: str
    description: str
    created_at: str
    pushed_at: str
    stars: int
    forks: int
    open_issues: int
    language: str
    license: str
    topics: tuple[str, ...]
    archived: bool
    fork: bool
    homepage: str


@dataclass(frozen=True)
class QueryResult:
    term: str
    lane: str
    query: str
    total_count: int
    incomplete_results: bool
    hits: tuple[RepoHit, ...]


def since_date(today: date, days: int) -> date:
    if days < 1:
        raise ValueError("days must be positive")
    return today - timedelta(days=days)


def build_query(term: str, lane: SearchLane, since: date) -> str:
    """Build a GitHub repository search query for one term/lane.

    The terms are the topic slugs named in T-4978. GitHub's repository search
    only exposes an "updated" server-side sort for freshness; the created lane
    therefore narrows by `created:>=` and sorts by `created_at` locally.
    """

    return f"topic:{term} {lane.qualifier}:>={since.isoformat()}"


def search_url(query: str, *, page: int, per_page: int) -> str:
    params = {
        "q": query,
        "sort": "updated",
        "order": "desc",
        "page": str(page),
        "per_page": str(per_page),
    }
    return GITHUB_SEARCH_URL + "?" + urllib.parse.urlencode(params)


def github_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_json(
    url: str,
    *,
    token: str | None = None,
    opener: Callable[[urllib.request.Request], object] = urllib.request.urlopen,
) -> Mapping[str, object]:
    request = urllib.request.Request(url, headers=github_headers(token))
    with opener(request) as response:
        raw = response.read()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("GitHub search response was not a JSON object")
    return value


def _license_name(item: Mapping[str, object]) -> str:
    license_obj = item.get("license")
    if isinstance(license_obj, dict):
        spdx = license_obj.get("spdx_id")
        if isinstance(spdx, str) and spdx:
            return spdx
    return ""


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def repo_hit(term: str, lane: SearchLane, rank: int, item: Mapping[str, object]) -> RepoHit:
    topics_obj = item.get("topics")
    topics = tuple(str(t) for t in topics_obj) if isinstance(topics_obj, list) else ()
    return RepoHit(
        term=term,
        lane=lane.name,
        rank=rank,
        full_name=_string(item.get("full_name")),
        html_url=_string(item.get("html_url")),
        description=_string(item.get("description")),
        created_at=_string(item.get("created_at")),
        pushed_at=_string(item.get("pushed_at")),
        stars=_int(item.get("stargazers_count")),
        forks=_int(item.get("forks_count")),
        open_issues=_int(item.get("open_issues_count")),
        language=_string(item.get("language")),
        license=_license_name(item),
        topics=topics,
        archived=bool(item.get("archived")),
        fork=bool(item.get("fork")),
        homepage=_string(item.get("homepage")),
    )


def run_query(
    term: str,
    lane: SearchLane,
    *,
    since: date,
    limit: int,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    token: str | None = None,
    fetch: Callable[[str, str | None], Mapping[str, object]] | None = None,
) -> QueryResult:
    if limit < 1:
        raise ValueError("limit must be positive")
    if sample_size < limit:
        raise ValueError("sample_size must be >= limit")
    fetcher = fetch or (lambda url, tok: fetch_json(url, token=tok))
    query = build_query(term, lane, since)
    per_page = min(100, sample_size)
    data = fetcher(search_url(query, page=1, per_page=per_page), token)
    items_obj = data.get("items")
    items = items_obj if isinstance(items_obj, list) else []
    sorted_items = sorted(items, key=lambda item: _string(item.get(lane.date_field)), reverse=True)
    hits = tuple(repo_hit(term, lane, index + 1, item) for index, item in enumerate(sorted_items[:limit]))
    return QueryResult(
        term=term,
        lane=lane.name,
        query=query,
        total_count=_int(data.get("total_count")),
        incomplete_results=bool(data.get("incomplete_results")),
        hits=hits,
    )


def run_queries(
    terms: Sequence[str],
    *,
    lanes: Sequence[SearchLane] = LANES,
    today: date | None = None,
    since_days: int = DEFAULT_SINCE_DAYS,
    limit: int = DEFAULT_LIMIT,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    token: str | None = None,
    fetch: Callable[[str, str | None], Mapping[str, object]] | None = None,
    pause_seconds: float = 0.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[QueryResult, ...]:
    anchor = today or date.today()
    since = since_date(anchor, since_days)
    results: list[QueryResult] = []
    pairs = [(term, lane) for term in terms for lane in lanes]
    for index, (term, lane) in enumerate(pairs):
        results.append(
            run_query(
                term,
                lane,
                since=since,
                limit=limit,
                sample_size=max(limit, sample_size),
                token=token,
                fetch=fetch,
            )
        )
        if pause_seconds > 0 and index < len(pairs) - 1:
            sleeper(pause_seconds)
    return tuple(results)


def as_jsonable(results: Sequence[QueryResult]) -> list[dict[str, object]]:
    return [
        {
            **asdict(result),
            "hits": [asdict(hit) for hit in result.hits],
        }
        for result in results
    ]


def _md_cell(value: object) -> str:
    text = str(value).replace("\n", " ").replace("|", "\\|")
    return text


def _source_summary(result: QueryResult) -> str:
    suffix = " incomplete" if result.incomplete_results else ""
    return f"`{result.query}` -> {result.total_count} total{suffix}"


def render_markdown(results: Sequence[QueryResult], *, generated_on: date | None = None) -> str:
    stamp = generated_on or date.today()
    lines: list[str] = [
        "# GitHub companion-memory entrant queries",
        "",
        f"Generated: {stamp.isoformat()}",
        "",
        "Standing query families: "
        + ", ".join(f"`{term}`" for term in DEFAULT_TERMS)
        + ". Each term is queried as a GitHub topic in a recently-pushed lane and a recently-created lane.",
        "",
    ]
    for result in results:
        lines.extend(
            [
                f"## {result.term} / {result.lane}",
                "",
                _source_summary(result),
                "",
                "| Rank | Repo | Stars | Forks | Created | Pushed | License | Language | Topics | Description |",
                "|---:|---|---:|---:|---|---|---|---|---|---|",
            ]
        )
        if not result.hits:
            lines.append("|  | No hits |  |  |  |  |  |  |  |  |")
        for hit in result.hits:
            repo = f"[{_md_cell(hit.full_name)}]({hit.html_url})"
            flags = []
            if hit.archived:
                flags.append("archived")
            if hit.fork:
                flags.append("fork")
            if flags:
                repo += " (" + ", ".join(flags) + ")"
            topics = ", ".join(f"`{_md_cell(topic)}`" for topic in hit.topics[:8])
            lines.append(
                "| "
                + " | ".join(
                    (
                        str(hit.rank),
                        repo,
                        str(hit.stars),
                        str(hit.forks),
                        _md_cell(hit.created_at[:10]),
                        _md_cell(hit.pushed_at[:10]),
                        _md_cell(hit.license),
                        _md_cell(hit.language),
                        topics,
                        _md_cell(hit.description),
                    )
                )
                + " |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _parse_terms(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_TERMS
    terms = tuple(term.strip() for term in raw.split(",") if term.strip())
    if not terms:
        raise ValueError("--terms produced no terms")
    return terms


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="hits per term/lane")
    parser.add_argument(
        "--sample-size",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        help="GitHub results fetched per term/lane before local date sorting",
    )
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    parser.add_argument("--terms", help="comma-separated topic slugs; default is the T-4978 set")
    parser.add_argument("--output", help="write output to this path instead of stdout")
    parser.add_argument(
        "--pause",
        type=float,
        default=None,
        help="seconds to sleep between GitHub search calls; default 7 without token, 0 with token",
    )
    parser.add_argument(
        "--token-env",
        default="GITHUB_TOKEN",
        help="environment variable for a GitHub token; falls back to GH_TOKEN",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    token = os.environ.get(args.token_env) or os.environ.get("GH_TOKEN")
    terms = _parse_terms(args.terms)
    pause = args.pause if args.pause is not None else (0.0 if token else 7.0)
    results = run_queries(
        terms,
        since_days=args.since_days,
        limit=args.limit,
        sample_size=args.sample_size,
        token=token,
        pause_seconds=pause,
    )
    if args.format == "json":
        output = json.dumps(as_jsonable(results), indent=2, sort_keys=True) + "\n"
    else:
        output = render_markdown(results)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
