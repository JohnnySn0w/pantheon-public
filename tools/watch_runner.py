#!/usr/bin/env python3
"""Pantheon watch-loop driver.

Reads `pantheon-config/knowledge/watch/topics.md` and computes which topics are
due based on per-topic cadence + last-fire state. Under the ORG-025 in-session
model the actual researcher dispatch happens in Specialist's interactive session via
the Agent tool; this driver's live modes are: emit the due briefs as JSON
(`--emit-due-briefs`), land the reports Specialist's subagents wrote + commit + actuate
(`--land-commit`), file the daily watch-dispatch handoff ticket
(`--file-watch-ticket`, the migrated timer entrypoint), and re-run actuation only
(`--actuate-only`). Each report writes to
`knowledge/watch/reports/<YYYY-MM-DD>/<slug>.md`; the day's `README.md` index is
appended with a one-line entry per report as it lands.

Designed to run from systemd-user-timer (see `tools/systemd/` templates) but can
be invoked manually for testing:

    python3 tools/watch_runner.py --emit-due-briefs  # print due topics as JSON for in-session dispatch
    python3 tools/watch_runner.py --land-commit      # land in-session reports + commit + push + actuate
    python3 tools/watch_runner.py --file-watch-ticket # file the daily watch-dispatch ticket (timer entrypoint)
    python3 tools/watch_runner.py --actuate-only     # run only actuation against today's reports (manual smoke)
    python3 tools/watch_runner.py --topic <slug>     # restrict emit/land to one topic by slug
    python3 tools/watch_runner.py --force            # ignore cadence; treat all topics as due
    python3 tools/watch_runner.py --no-actuate       # (with --land-commit) skip the post-digest actuation phase

State file: `~/.local/state/pantheon/watch/state.json` (outside repo;
regenerable from reports/ filesystem dates if lost).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

# Make `from tools._git_utils import ...` work both as a script
# (`python3 tools/watch_runner.py` per systemd unit ExecStart) and as a
# package import (`python3 -m unittest tools.test_watch_runner`). When
# invoked as a script, sys.path prepends `tools/` rather than the repo root,
# so the `tools` package isn't reachable; this bootstrap injects the repo
# root in that case. In package mode `__package__ == "tools"`, no-op.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_utils import (  # noqa: E402  — after sys.path bootstrap.
    _git,
    _hermes_scan_dir,
    _hermes_scan_message,
    commit_scoped,
    git_mutation_lock,
    pull_repo_or_warn,
    pull_repo_strict,
    push_with_retry,
)
from tools._audit import append_ledger_entries, build_ledger_entry  # noqa: E402
from tools._md_table import _md_row  # noqa: E402  — leaf module, re-only (T-1936)
from tools.ledger_shard import current_write_target  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPICS_FILE = REPO_ROOT / "knowledge" / "watch" / "topics.md"
REPORTS_ROOT = REPO_ROOT / "knowledge" / "watch" / "reports"

STATE_DIR = Path.home() / ".local" / "state" / "pantheon" / "watch"
STATE_FILE = STATE_DIR / "state.json"
LOG_DIR = Path.home() / ".local" / "log" / "pantheon" / "watch"

# Cadence values; everything else is treated as "paused".
VALID_CADENCES = {"daily", "weekly", "monthly", "quarterly", "paused"}

# B11 actuation: post-digest phase that auto-files tickets for findings.
RUNNER_LOG = Path.home() / ".local" / "log" / "pantheon" / "watch_runner.log"
TICKET_BIN = REPO_ROOT / "tools" / "ticket.py"
_MAX_ACTUATION_RETRIES = 3

# Topic policy: maps topic-slug → routing policy for B11 actuation.
# Schema: {"target": "orchestrator"|"recovery",
#          "labels": list[str] (merged with always-applied),
#          "interactive_required": bool}
# To add a topic, add a key here. Missing topics fall through to _DEFAULT_POLICY,
# which sets interactive_required=True so unknowns surface for review rather
# than silently auto-actuating.
TOPIC_POLICY: dict[str, dict] = {
    "anthropic-claude-code-changelog": {
        "target": "orchestrator",
        "labels": ["watch-finding"],
        "interactive_required": False,
    },
    "anthropic-quota-interface": {
        "target": "orchestrator",
        "labels": ["watch-finding", "harness", "quota"],
        "interactive_required": False,
    },
    "agentic-harness-landscape": {
        "target": "orchestrator",
        "labels": ["watch-finding"],
        "interactive_required": False,
    },
    # T-2808: the small-group/early-signal complement to agentic-harness-landscape.
    # A research/competitive-intel digest → HO triage (ORG-012 portion), so
    # interactive_required=False = deliver to HO's queue (matches the other digests).
    "grand-goal-competitor-watch": {
        "target": "orchestrator",
        "labels": ["watch-finding", "competitive-intel", "grand-goal"],
        "interactive_required": False,
    },
    "mcp-supply-chain": {
        "target": "recovery",
        "labels": ["watch-finding", "security"],
        "interactive_required": True,
    },
    "anthropic-policy-and-billing": {
        "target": "recovery",
        "labels": ["watch-finding", "cost-or-billing"],
        "interactive_required": True,
    },
    # T-2433: the five standing topics that had been falling through to
    # _DEFAULT_POLICY since they were added to topics.md. They are research
    # DIGESTS whose triage is HO's ORG-012 portion, so interactive_required is
    # False — that flag gates DELIVERY (inbox_consumer treats it as
    # not-auto-pullable), not autonomous action, so False means "deliver this to
    # HO's queue for triage", which is precisely what was silently not happening.
    "llm-agent-failure-modes-research": {
        "target": "orchestrator",
        "labels": ["watch-finding", "research-digest"],
        "interactive_required": False,
    },
    "context-engineering-research": {
        "target": "orchestrator",
        "labels": ["watch-finding", "research-digest"],
        "interactive_required": False,
    },
    "context-management": {
        "target": "orchestrator",
        "labels": ["watch-finding", "research-digest"],
        "interactive_required": False,
    },
    "voice-as-interface": {
        "target": "orchestrator",
        "labels": ["watch-finding", "research-digest", "echo"],
        "interactive_required": False,
    },
    # Vendor-surface topic rather than a pure digest: its findings can carry a
    # real "act now" item (T-2051 carried the Haiku 3 retirement). Delivery is
    # exactly what that needs, so False here too. Billing-shaped findings are
    # already routed separately by the T-1442 rule documented in topics.md.
    "echo-stack-vendor-changes": {
        "target": "orchestrator",
        "labels": ["watch-finding", "vendor-change", "echo"],
        "interactive_required": False,
    },
}

_DEFAULT_POLICY = {
    "target": "orchestrator",
    "labels": ["watch-finding", "unknown-topic"],
    "interactive_required": True,
}


@dataclass
class Topic:
    slug: str
    cadence: str
    budget: int
    scope: str
    seed_sources: list[str]
    notes: str

    def is_due(self, last_fire: date | None, today: date) -> bool:
        if self.cadence == "paused" or self.cadence not in VALID_CADENCES:
            return False
        if last_fire is None:
            return True
        if self.cadence == "daily":
            return last_fire < today
        if self.cadence == "weekly":
            # ISO week boundary; fire if not yet fired in current ISO week.
            iso_today = today.isocalendar()
            iso_last = last_fire.isocalendar()
            return (iso_last.year, iso_last.week) < (iso_today.year, iso_today.week)
        if self.cadence == "monthly":
            return (last_fire.year, last_fire.month) < (today.year, today.month)
        if self.cadence == "quarterly":
            q_today = (today.year, (today.month - 1) // 3)
            q_last = (last_fire.year, (last_fire.month - 1) // 3)
            return q_last < q_today
        return False


def parse_topics(path: Path, warnings: list[str] | None = None) -> list[Topic]:
    """Parse the knowledge/watch/topics.md format. Each topic is an H2 (`## slug`) followed
    by a bullet list with `- **field:** value` entries. Seed sources are nested
    bullets under `**seed sources:**`.

    T-2353: `warnings` (optional accumulator) collects human-readable strings for
    stanzas that LOOK like topics but fail to parse — missing cadence, out-of-vocab
    cadence, missing scope. Before this, all three degraded to "never fires"
    indistinguishably from a deliberate `paused`, and the first symptom of a
    silently retired topic is an absence nobody is looking for. Prose sections
    (no recognized field bullets at all) stay silent by design — that filter is
    load-bearing for "Report output convention". Return type unchanged (a list),
    so existing callers and test mocks are unaffected.
    """
    text = path.read_text(encoding="utf-8")
    topics: list[Topic] = []

    # Split on H2 markers (`## slug`). Skip H1 and other content.
    sections = re.split(r"^## (.+?)$", text, flags=re.MULTILINE)
    # sections is [preamble, slug1, body1, slug2, body2, ...]
    it = iter(sections[1:])
    for slug, body in zip(it, it):
        slug = slug.strip()
        if slug.lower() == "cadence summary":  # the trailing index section, not a topic
            continue
        topic = parse_one_topic(slug, body, warnings=warnings)
        if topic is not None:
            topics.append(topic)

    # T-2433: an ACTIVE standing topic with no TOPIC_POLICY entry silently falls
    # through to _DEFAULT_POLICY, which labels it `unknown-topic` +
    # `interactive-required` — and inbox_consumer skips interactive-required by
    # construction, so its findings are filed and then never delivered. Nothing
    # reports that: the dispatch succeeds, the report lands, the ticket exists.
    # Nine such tickets accumulated across five topics (6-13 days) before a hand
    # sweep found them.
    #
    # Keyed on CADENCE, not mere presence: paused topics legitimately need no
    # policy, but un-pausing one without registering it must fail loudly at that
    # moment rather than a fortnight later. Keyed on topics.md (the config), NOT
    # on report filenames — one-off commissioned dispatches have no stanza and
    # SHOULD hit the default; warning on those would fire forever.
    # An out-of-vocab cadence already warns above and can never fire, so adding
    # "also register it" would be noise stacked on a louder, more actionable
    # problem. Only well-formed ACTIVE topics reach this check.
    for t in topics:
        if (
            t.cadence != "paused"
            and t.cadence in VALID_CADENCES
            and t.slug not in TOPIC_POLICY
        ):
            _warn_unregistered_topic(t.slug, t.cadence, warnings)
    return topics


def _warn_unregistered_topic(
    slug: str, cadence: str, warnings: list[str] | None
) -> None:
    """T-2433: emit the unregistered-active-topic warning (see parse_topics)."""
    if warnings is None:
        return
    warnings.append(
        f"topic '{slug}': ACTIVE (cadence '{cadence}') but has NO TOPIC_POLICY "
        "entry in tools/watch_runner.py — its findings fall through to "
        "_DEFAULT_POLICY and are filed `unknown-topic` + `interactive-required`, "
        "which inbox_consumer SKIPS, so they will never be delivered to anyone. "
        "Add a TOPIC_POLICY entry (target / labels / interactive_required)."
    )


def parse_one_topic(slug: str, body: str, warnings: list[str] | None = None) -> Topic | None:
    fields: dict[str, str] = {}
    seed_sources: list[str] = []
    in_seed = False
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            in_seed = False
            continue
        # Match `- **field:** value`
        m = re.match(r"-\s+\*\*([^:*]+):\*\*\s*(.*)$", stripped)
        if m:
            key = m.group(1).strip().lower()
            val = m.group(2).strip()
            if key == "seed sources":
                in_seed = True
                continue
            in_seed = False
            fields[key] = val
            continue
        # Match nested seed-source bullets: `  - <url>` or `  - \`url\``
        if in_seed and stripped.startswith("- "):
            src = stripped[2:].strip().strip("`")
            seed_sources.append(src)
            continue
    def _warn(msg: str) -> None:
        if warnings is not None:
            warnings.append(msg)

    # T-2353: an H2 with NO recognized field bullets at all is prose (the
    # "Report output convention" section), silent by design. Anything with at
    # least one recognized field is trying to be a topic, and its parse
    # failures must be LOUD — before this, a forgotten/typo'd field silently
    # retired the topic, indistinguishable from a deliberate `paused`.
    looks_like_topic = bool(fields) or bool(seed_sources)

    if "cadence" not in fields and looks_like_topic:
        _warn(
            f"topic '{slug}': MISSING cadence — treated as paused, will never "
            "fire until knowledge/watch/topics.md is fixed"
        )
    cadence = fields.get("cadence", "paused").lower()
    if cadence != "paused" and cadence not in VALID_CADENCES:
        _warn(
            f"topic '{slug}': cadence '{cadence}' not in {sorted(VALID_CADENCES)} "
            "— will never fire until knowledge/watch/topics.md is fixed"
        )
    budget_str = fields.get("budget", "300 lines")
    m = re.search(r"(\d+)", budget_str)
    budget = int(m.group(1)) if m else 300
    scope = fields.get("scope", "")
    notes = fields.get("notes", "")
    if not scope:
        if looks_like_topic:
            _warn(
                f"topic '{slug}': MISSING scope — stanza DROPPED from the topic "
                "set entirely; fix knowledge/watch/topics.md"
            )
        return None
    return Topic(
        slug=slug,
        cadence=cadence,
        budget=budget,
        scope=scope,
        seed_sources=seed_sources,
        notes=notes,
    )


def write_topic_warnings(date_dir: Path, warnings: list[str]) -> None:
    """T-2353: land topic-parse warnings where the DIGEST will carry them.

    The digest reads every non-README `*.md` in the day's dir and surfaces via
    `surface:`/`headline:` frontmatter; ACTUATION drives off the README index
    rows (status + TL;DR) and reads each landed report's frontmatter only for
    the optional `belongs_to_topic` cross-ownership declaration (T-2523). This
    warnings file has no README row, so it reaches the operator's digest with zero
    new plumbing while staying invisible to the auto-filing path (which has a
    per-DAY idempotency key and would otherwise file one ticket per day until
    the edit was fixed — bugs/actuator-followup-ticket-explosion-no-dedup).

    No warnings → no file AND no dir creation (a mkdir here would make an
    empty date-dir appear on quiet days, which downstream reads as "the run
    happened").
    """
    if not warnings:
        return
    date_dir.mkdir(parents=True, exist_ok=True)
    n = len(warnings)
    body = "\n".join(f"- {w}" for w in warnings)
    (date_dir / "_topic-warnings.md").write_text(
        "---\n"
        "surface: above-bar\n"
        f"headline: topics.md has {n} topic issue(s) — silently retired or undelivered until fixed\n"
        "---\n\n"
        "# Topic configuration warnings (watch_runner, T-2353 / T-2433)\n\n"
        f"{body}\n\n"
        "Operator action: fix `knowledge/watch/topics.md`, or add the missing "
        "`TOPIC_POLICY` entry in `tools/watch_runner.py` — each line above says "
        "which. Both classes fail SILENTLY and neither is visible anywhere else: "
        "a malformed stanza is NOT WATCHED AT ALL (the timer exits 0 and the "
        "digest only reports what DID run), and an active topic with no policy "
        "entry IS watched and files findings that are then NEVER DELIVERED "
        "(they land `interactive-required`, which the inbox consumer skips).\n",
        encoding="utf-8",
    )


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"schema_version": 1, "topics": {}}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def last_fire_date(state: dict, slug: str) -> date | None:
    entry = state.get("topics", {}).get(slug)
    if not entry or not entry.get("last_fire"):
        return None
    return date.fromisoformat(entry["last_fire"])


def update_state(state: dict, slug: str, fired_on: date, outcome: str) -> None:
    state.setdefault("topics", {})[slug] = {
        "last_fire": fired_on.isoformat(),
        "last_outcome": outcome,
    }


def build_brief(
    topic: Topic, output_path: Path, corrections: list[dict] | None = None
) -> str:
    """Construct the prompt for the researcher dispatch.

    `output_path` must be an absolute path inside the worktree so the brief
    tells the researcher exactly where to write.  Passing a relative path
    reintroduces the T-0660 variance: researchers resolve relative paths
    against their subprocess cwd, which may differ from the worktree root.

    `corrections` (T-2614 back-propagation): the topic's un-cleared
    `brief_corrections` entries carried forward from state (see
    `accumulate_brief_corrections` / `--clear-correction`). `None` (the
    default, and every pre-T-2614 call site) or `[]` yields output
    byte-identical to before T-2614 — the block below is a pure no-op in
    that case. A non-empty list appends one operator-context section so the
    next cycle's researcher sees the correction instead of re-deriving it.
    """
    sources = "\n".join(f"  - {s}" for s in topic.seed_sources) or "  - (none specified — discover via WebSearch)"
    corrections_block = ""
    if corrections:
        items = "\n".join(
            f"- [{c['id']}] {c['text']} (found {c['source_date']}, source: `{c['source_report']}`)"
            for c in corrections
        )
        corrections_block = (
            "\n## Known brief corrections (pending — not yet folded into topics.md)\n\n"
            "A previous cycle's researcher found THIS topic's brief or seed config "
            "defective. Treat these as authoritative context — incorporate them, do "
            "NOT re-derive or re-report them as new findings, and do NOT edit "
            "`knowledge/watch/topics.md` yourself (operator-curated, D2; an operator "
            "clears each one after folding it in):\n"
            f"{items}\n"
        )
    return f"""You are the `researcher` worker persona. The Orchestrator (the cron'd Pantheon watch-loop driver) is dispatching you.

## Topic
{topic.slug}

## Scope
{topic.scope}

## Seed sources (anchor on 3-5 of these, derive the rest)
{sources}

## Operator-side notes
{topic.notes or "(none)"}
{corrections_block}
## Output

Write your report to: `{output_path}`
Length budget: ~{topic.budget} lines (do not exceed by 2× without escalating).

**Begin the report with a frontmatter block — this is how the end-of-day digest decides what to surface to the operator (the digest reads this; it does NOT re-read your whole report). Be honest: most weekly checks ARE routine.**
```
---
surface: above-bar | routine
headline: <one line — if above-bar, the specific "why the operator cares"; if routine, the one-line state-of-the-world>
belongs_to_topic: <slug>   # OPTIONAL — omit unless the note below applies
---
```
Set `surface: above-bar` ONLY for genuinely operator-noteworthy findings — a new CVE / supply-chain incident / billing-or-policy change that affects Pantheon, a breaking change to a tool we run, a strategic shift in the landscape that changes a prior verdict, or a finding that contradicts something we rely on. Routine "nothing material changed this week" is `surface: routine` — that is the common and correct case; do not inflate. The `headline` is the single line that represents your whole report in the digest, so make it carry the signal.

`belongs_to_topic` is an OPTIONAL cross-topic-ownership declaration and should be omitted in the ordinary case. Set it to another topic's slug ONLY when the SAME event you are reporting, raising the SAME ask, is genuinely owned by that other topic (e.g. a Speechmatics billing change surfacing in `voice-as-interface` but owned by `echo-stack-vendor-changes`). When you set it, the auto-filer files your report as a cross-reference NOTE pointing at that topic instead of a second full actuation ticket, so triage adjudicates the event once. Do NOT set it when the same event legitimately raises a DIFFERENT ask in your topic — leave it off and file normally; both angles deserve their own ticket. `duplicate_of_topic` is accepted as an alias. A self-reference (your own slug) is ignored.

Structure (after the frontmatter): section headers, inline citations, distinguish primary from secondary, tag `[weak]` / `[single-source]` where appropriate. End with a `## Sources` section listing every URL/citation with one-line characterization.

Honor your persona's hard rules: one topic, cite everything non-synthesis, mark weak claims, no code, no editing hand-curated docs outside the assigned output path, stay under budget, surface what couldn't be determined.

House discipline: if your report references the Anthropic billing-classifier trigger phrase (the harness-detection filename discussed in the anthropic-policy-and-billing / agentic-harness-landscape topics), write it ONLY in the escaped form `HERMES\\.md` (uppercase, backslash before the period) — never the bare contiguous sequence. The bare form in a committed file refuses the land-commit (your report gets orphaned off origin); the escaped form reads identically in prose and is safe.

## Instruments — try a second one before you record a negative

A negative finding asserted from a SINGLE tool is not a finding. Before you record a source as "unavailable", "not found", or "text could not be extracted", try a SECOND instrument, and state in your report which instruments you tried. Instrument failure and real absence render identically ("nothing came back") — only a second instrument (or a control query you know should succeed) tells them apart.

Specific instruments, in preference order:

- PDF text: WebFetch CANNOT parse PDFs — a PDF that "returns structure but no readable text" via WebFetch is a WebFetch limitation, not an unreadable source. Extract locally: curl -sL -o p.pdf <url> then pdftotext -layout p.pdf - (both installed). If pdftotext gives poor output, fall back to pypdf (PdfReader(...).pages, .extract_text()). For arXiv, arxiv.org/html/<id> is a second route to the same content as HTML.
- Vendor docs: modern docs platforms (Mintlify-class — OpenAI, Anthropic, Speechmatics, Gradium all confirmed) serve clean Markdown. Try docs.<vendor>/llms-full.txt (whole corpus, one fetch), then docs.<vendor>/llms.txt (index), then <page-url>.md, and only THEN scrape HTML — HTML tag-stripping has produced factually WRONG answers that the .md corrected. The docs live on the docs SUBDOMAIN; the marketing host (www.<vendor>) 404s/403s these and says nothing about the docs host.
- A 403/404/empty on ONE host or ONE API is not a vendor-level or topic-level negative. Third-party release-note hosts (featurebase, canny, launchnotes) share no path structure with the docs host; for "did this vendor change its commercial terms", the archived marketing page (Wayback, two pre-change captures) is often the only record — vendors do not put repricings in product changelogs.

If the research surfaces any actionable follow-up work (tooling gaps, harvest candidates, adjacent bugs, "worth evaluating" items), end the report with a `## Follow-ups` section enumerating each as a one-line filable item — `<short title> — <one-line ask>`. Surface them there; do NOT try to file tickets yourself (that is the dispatching tier's job). Omit the section if there are none. Per sop/standard/research-dispatch-filing.md.

After writing the report, return a ≤200-word summary back to me. CRITICAL: the FIRST line of your stdout reply must be `TL;DR: <one-line finding, ≤120 chars>` — a substantive content sentence, not metadata. Bad: "TL;DR: report landed at knowledge/watch/reports/...". Good: "TL;DR: Anthropic shipped Opus 4.7 with 1M-token context window, GA on 2026-04-21." After the TL;DR line, give the ≤200-word body: what landed, key findings, what couldn't be determined, blockers hit. Begin.
"""


def compute_due_briefs(
    today: date,
    *,
    force: bool = False,
    topic_filter: str | None = None,
    reports_root: "Path | None" = None,
) -> list[dict]:
    """Return due topics as dispatch records for in-session orchestration (ORG-025 / T-0855).

    This is the read-only input to the in-session dispatch path that REPLACED the
    retired headless `claude -p` subprocess. It computes which topics
    are due — reusing the exact cadence/state logic main() uses — and for each
    emits {slug, cadence, output_path, brief}. Specialist (the interactive session) reads
    this JSON, dispatches one general-purpose `researcher`-brief subagent (Agent
    tool, model sonnet) per record, and the subagent writes its report to
    `output_path`. No dispatch, no state mutation, no commit happens here.

    `output_path` is the REAL reports path (REPORTS_ROOT/<date>/<slug>.md) unless
    `reports_root` overrides it (smoke/worktree). The brief is byte-identical to
    what the legacy path fed `claude -p`, so the research contract is unchanged.
    """
    _reports_root = reports_root if reports_root is not None else REPORTS_ROOT
    topic_warnings: list[str] = []
    topics = parse_topics(TOPICS_FILE, warnings=topic_warnings)
    state = load_state()
    if topic_filter:
        topics = [t for t in topics if t.slug == topic_filter]
    date_dir = _reports_root / today.isoformat()
    # T-2353: loud on both channels — stderr for the journal, and a
    # report-convention file so the digest carries it to the operator.
    for w in topic_warnings:
        print(f"[topic-parse] WARNING: {w}", file=sys.stderr)
    write_topic_warnings(date_dir, topic_warnings)
    records: list[dict] = []
    for topic in topics:
        if force or topic_filter:
            due = topic.cadence != "paused"
        else:
            due = topic.is_due(last_fire_date(state, topic.slug), today)
        if not due:
            continue
        output_path = date_dir / f"{topic.slug}.md"
        # T-2614: un-cleared corrections for this topic ride into the brief.
        # .get()-defensive — state predating this change has no
        # "brief_corrections" key at all.
        corrections = state.get("brief_corrections", {}).get(topic.slug, [])
        records.append({
            "slug": topic.slug,
            "cadence": topic.cadence,
            "output_path": str(output_path),
            "brief": build_brief(topic, output_path, corrections=corrections),
        })
    return records


def append_day_index_entry(date_dir: Path, slug: str, status: str, summary: str) -> None:
    index = date_dir / "README.md"
    today_iso = date_dir.name
    if not index.exists():
        # Initialize the day's index.
        index.write_text(
            f"""# Watch reports — {today_iso}

Reports filed by the watch loop on {today_iso}. One line per topic.

| Topic | Status | One-line summary |
|---|---|---|
""",
            encoding="utf-8",
        )
    entry = _md_row(f"[{slug}]({slug}.md)", status, summary) + "\n"
    with index.open("a", encoding="utf-8") as f:
        f.write(entry)


def index_unindexed_reports(date_dir: Path) -> list[str]:
    """Index every landed report in `date_dir` that has no day-index row yet.

    T-2148 finding 1. `land_reports()` iterates the entries parsed from
    knowledge/watch/topics.md and only calls `append_day_index_entry()` for slugs
    found there. Any report that reached disk by another route — an operator-originated
    one-off, an orphan-landed dispatch, an ad-hoc Agent-tool run — is therefore
    INVISIBLE to the day index forever, no matter how many times the runner fires.
    Three such reports were found unindexed on 2026-07-27/28 (agent-optimization-
    landscape, semantic-search-graph-db-stack, cache-ttl-invalidation).

    The cause is that the index was driven by the DISPATCH LIST rather than by what
    is actually on disk. This sweep inverts that: the filesystem is the defining
    surface, topics.md is merely how most reports get there.

    Idempotent via day_index_has_row(); returns the slugs it added, oldest first.
    README.md is the index itself and is skipped.
    """
    if not date_dir.is_dir():
        return []
    added: list[str] = []
    for report in sorted(date_dir.glob("*.md")):
        if report.name == "README.md":
            continue
        slug = report.stem
        if day_index_has_row(date_dir, slug):
            continue
        if not report_landed_ok(report):
            # A stub/empty file is not a report. Indexing it would assert a
            # landing that did not happen — the same "created is not delivered"
            # shape this sweep exists to fix, one level down.
            continue
        headline = parse_report_headline(report)
        summary = f"TL;DR: {headline}" if headline else "TL;DR: (no headline frontmatter)"
        append_day_index_entry(date_dir, slug, "ok", summary)
        added.append(slug)
    return added


def day_index_has_row(date_dir: Path, slug: str) -> bool:
    """True iff `date_dir/README.md` already has an index row for `slug`.

    Idempotency guard for the in-session land path (ORG-025 / T-0855): a re-run
    of --land-commit (e.g. after a push failure) must not append a duplicate
    `| [slug](slug.md) | ... |` row. The legacy fire path is once-per-day via
    state and never re-runs the same slug, so it doesn't need this; land-commit
    can be re-invoked by the operator, so it does.
    """
    index = date_dir / "README.md"
    if not index.exists():
        return False
    needle = f"[{slug}]({slug}.md)"
    return needle in index.read_text(encoding="utf-8")


def parse_report_headline(report_path: Path) -> str | None:
    """Extract the `headline:` value from a report's leading frontmatter block.

    The brief (build_brief) instructs the researcher to open the report with a
    `--- surface/headline ---` frontmatter block; `headline` is "the single line
    that represents your whole report in the digest." The in-session land path
    uses it as the day-index `TL;DR:` summary (the subagent's stdout TL;DR is not
    threaded through disk, but the report's own headline is the durable
    equivalent — and it is what the digest reads anyway). Returns None if the
    file has no parseable frontmatter headline.
    """
    if not report_path.exists():
        return None
    text = report_path.read_text(encoding="utf-8")
    # Frontmatter must be the very first block: ^---\n ... \n---.
    m = re.match(r"^---\n(.*?)\n---", text, flags=re.DOTALL)
    if not m:
        return None
    for line in m.group(1).splitlines():
        hm = re.match(r"\s*headline\s*:\s*(.+?)\s*$", line)
        if hm:
            return hm.group(1).strip().strip('"').strip("'") or None
    return None


# ---------------------------------------------------------------------------
# T-2614: watch-loop back-propagation. A researcher that finds its own topic
# brief defective emits the correction as a `brief_corrections:` frontmatter
# list; the land path accumulates it into state, keyed per topic;
# compute_due_briefs/build_brief carries un-cleared entries into the NEXT
# brief for that topic. topics.md itself is never auto-edited (D2) — an
# operator folds the correction in by hand, then clears it via
# --clear-correction. See tickets/done/T-2509.md (the design ruling) and
# tickets/inbox/T-2614.md (the build ask).
# ---------------------------------------------------------------------------


def parse_report_brief_corrections(
    report_path: Path, warnings: list[str] | None = None
) -> list[str]:
    """Extract free-text `brief_corrections:` list items from a report's
    leading frontmatter block.

    Mirrors `parse_report_headline`'s frontmatter-anchor style: pure regex,
    never raises. Expected shape is a nested YAML-ish list:

        ---
        headline: ...
        brief_corrections:
          - seed source X is 404; correct URL is Y
        ---

    Two malformed shapes are detected and appended to `warnings` (never
    raised — this runs in the unattended land path, matching watch_runner's
    never-crash-unattended philosophy): an inline scalar value instead of a
    list, and a key present with zero parseable items. Both yield `[]` for
    this report, the same as "no corrections" — the caller cannot
    distinguish "researcher found nothing to correct" from "researcher wrote
    it wrong", which is the deliberate fail-safe direction (never abort the
    land/commit/actuate pipeline over a malformed correction block).
    """
    if not report_path.exists():
        return []
    text = report_path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", text, flags=re.DOTALL)
    if not m:
        return []
    corrections: list[str] = []
    saw_key = False
    saw_malformed_inline = False
    in_block = False
    for line in m.group(1).splitlines():
        if re.match(r"^brief_corrections\s*:\s*$", line):
            saw_key = True
            in_block = True
            continue
        if re.match(r"^brief_corrections\s*:\s*\S", line):
            saw_key = True
            saw_malformed_inline = True
            in_block = False
            if warnings is not None:
                warnings.append(
                    f"{report_path}: brief_corrections: has an inline value, "
                    "expected a YAML list — skipped"
                )
            continue
        if in_block:
            item_m = re.match(r"^\s*-\s+(.+?)\s*$", line)
            if item_m:
                item = item_m.group(1).strip().strip('"').strip("'")
                if item:
                    corrections.append(item)
                continue
            in_block = False
    if saw_key and not saw_malformed_inline and not corrections and warnings is not None:
        warnings.append(f"{report_path}: brief_corrections: present but no items parsed")
    return corrections


def _correction_id(slug: str, text: str) -> str:
    """Stable content-hash id for a brief_corrections entry.

    Hashes `slug\\x1e text` (record-separator, not bare concatenation) so no
    slug/text boundary can collide two distinct corrections onto the same
    id. Idempotent: the same correction landed twice (a re-run of
    --land-commit, or the identical defect recurring in a later report)
    hashes to the same id, so `accumulate_brief_corrections` naturally
    dedupes without any external bookkeeping.
    """
    return hashlib.sha256(f"{slug}\x1e{text}".encode("utf-8")).hexdigest()[:12]


def accumulate_brief_corrections(
    state: dict, slug: str, report_path: Path, today: date
) -> list[str]:
    """Scan a landed report's frontmatter for `brief_corrections:` and store
    any new (by stable id) entries in `state`, keyed per topic — the "rides
    the same pipe as the defect" step: a correction enters state exactly
    when the report that found it lands.

    Never raises: malformed frontmatter yields `[]` from
    `parse_report_brief_corrections` (a stderr + RUNNER_LOG warning, not an
    exception) — indistinguishable here from "no corrections found" by
    design.

    Mutates `state` in place; caller (`land_reports`) already owns
    `save_state(state)`. Returns the list of newly-added correction ids
    (empty if nothing new — including the case where every parsed text was
    already present).
    """
    warnings: list[str] = []
    texts = parse_report_brief_corrections(report_path, warnings=warnings)
    for w in warnings:
        print(f"[brief-corrections] WARNING: {w}", file=sys.stderr)
        _append_runner_log(f"[specialist] brief_corrections: {w}")
    if not texts:
        return []
    bucket = state.setdefault("brief_corrections", {}).setdefault(slug, [])
    existing_ids = {c["id"] for c in bucket}
    try:
        rel_report = report_path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel_report = str(report_path)
    added: list[str] = []
    for text in texts:
        cid = _correction_id(slug, text)
        if cid in existing_ids:
            continue
        bucket.append({
            "id": cid,
            "text": text,
            "source_date": today.isoformat(),
            "source_report": rel_report,
        })
        existing_ids.add(cid)
        added.append(cid)
    return added


def clear_correction(state: dict, topic_slug: str, correction_id: str) -> bool:
    """Remove one un-cleared brief_corrections entry for `topic_slug` by its
    stable id — the operator ack/clear path, run after folding the
    correction into `knowledge/watch/topics.md` by hand.

    Mutates `state` in place; caller saves. Pops the topic's bucket entirely
    once it empties out (mirrors `write_topic_warnings`'s "no warnings → no
    file": state.json shouldn't accumulate permanent empty-list clutter).
    Returns True iff an entry was found and removed; False for an unknown
    topic/id — a typo'd id or a double-clear is a no-op, not an error, since
    it must never corrupt state.
    """
    bucket = state.get("brief_corrections", {}).get(topic_slug)
    if not bucket:
        return False
    remaining = [c for c in bucket if c["id"] != correction_id]
    if len(remaining) == len(bucket):
        return False
    if remaining:
        state["brief_corrections"][topic_slug] = remaining
    else:
        del state["brief_corrections"][topic_slug]
    return True


# T-1581: minimum report size (bytes) for a report to count as substantive
# rather than a stub or an error-dump. Every currently-registered topic's
# historical report on disk runs 8KB-24KB, so this floor has zero
# false-positive risk against genuine reports while catching a few-line stub
# or a bare "I could not complete this task" dump.
REPORT_MIN_BYTES = 500


def _report_is_structured(text: str) -> bool:
    """True iff `text` has the report template's required shape: a leading
    frontmatter block (`^---\\n...\\n---`) with a non-empty `headline:` key,
    plus at least one `## ` section heading in the body (per build_brief's
    contract to researchers). Cheap, template-anchored regex check — not a
    markdown parser.
    """
    m = re.match(r"^---\n(.*?)\n---", text, flags=re.DOTALL)
    if not m:
        return False
    has_headline = any(
        re.match(r"\s*headline\s*:\s*\S", line) for line in m.group(1).splitlines()
    )
    if not has_headline:
        return False
    return bool(re.search(r"^## ", text, flags=re.MULTILINE))


def report_landed_ok(report_path: Path) -> bool:
    """Structural completion check (T-1581): a report counts as landed iff it
    exists, is at least `REPORT_MIN_BYTES`, and has the template's required
    structure (frontmatter `headline:` + at least one `## ` heading).

    This extends the T-0662 exists-only guard per 2606.09863's
    structural-detector evidence — bare existence passes stubs and
    error-dumps. Shared by both structural gates: the T-0662 partial-stage
    guard in `git_commit_and_push` (pre-commit) and the post-dispatch
    delivery-status derivation in `land_reports` (pre-landing).
    """
    if not report_path.exists():
        return False
    if report_path.stat().st_size < REPORT_MIN_BYTES:
        return False
    return _report_is_structured(report_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Auto-commit-and-push: lands watch reports on origin/main with narrow scope.
# Gated on HERMES guard + scope filter + ff-only pull. No force, no recovery.
# Author identity = the executing user's git config (claude-research on VPS).
# Shared HERMES-guard + git-subprocess helpers live in tools/_git_utils.py.
# ---------------------------------------------------------------------------


def _build_commit_message(
    date_iso: str, fired_topics: list[Topic], statuses: dict[str, str]
) -> str:
    slugs = [t.slug for t in fired_topics]
    status_pairs = ", ".join(f"{s}={statuses.get(s, '?')}" for s in slugs)
    return (
        f"research(watch): land {date_iso} reports\n"
        f"\n"
        f"{len(slugs)} topic(s): {', '.join(slugs)}\n"
        f"Statuses: {status_pairs}\n"
        f"\n"
        f"Auto-committed by watch_runner.py (specialist unattended).\n"
        f"Scope: knowledge/watch/reports/{date_iso}/ only.\n"
    )


def git_commit_and_push(
    date_iso: str, fired_topics: list[Topic], statuses: dict[str, str],
    *,
    cwd: "Path | None" = None,
) -> tuple[bool, str]:
    """Commit reports under `knowledge/watch/reports/<date_iso>/` and push to origin/main.

    Order: HERMES guard -> [ff-only pull (skipped in worktree mode) -> narrow
    stage -> scope assert -> partial-stage guard -> empty-set short-circuit ->
    message HERMES belt -> commit -> push]. Each step fails fast. No force, no
    force-with-lease, no auto-recovery.

    The bracketed span (pull through push) runs inside `git_mutation_lock()`
    (T-1774, closing the residual gap flagged off T-1770 Part b): it is the
    SAME mutate -> stage -> commit -> push critical section shape as
    `commit_scoped` and `git_commit_ledger_entry` (this module's other
    committer, already migrated onto `commit_scoped`), serialized on the same
    repo-anchored lockfile so no writer on the shared checkout can ever
    observe another writer's in-flight staging (`bugs/ticket-py-shared-
    checkout-interleaved-writer-race.md`).

    NOT routed through `commit_scoped` directly (T-1774 contract comparison):
    `commit_scoped`'s leak-check (out-of-scope staged paths) and its
    empty-staged short-circuit are two adjacent statements inside one
    monolithic function body with no caller-supplied extension point between
    them. This function's partial-stage guard (step 4b) is domain-specific
    business logic — it must run strictly AFTER the scope/leak check (so an
    out-of-scope-path leak is reported as a scope-guard refusal, not masked by
    an unrelated partial-stage refusal — see
    `TestCommitAndPushScopeFilter.test_out_of_scope_path_rejects_and_resets`)
    and strictly BEFORE the empty-staged short-circuit and commit. There is no
    way to splice that in through `commit_scoped`'s public contract (`paths`,
    `message`, `sync`, `allow_prefix`, `conflict_resolver`) without changing
    `commit_scoped` itself, which would ripple into every other caller
    (ticket.py, ticket_grooming.py, `git_commit_ledger_entry`) for the sake of
    one caller's bespoke mid-pipeline check. Wrapping the existing span
    directly is the narrower, lower-risk change — it also means every other
    behavior here (message wording, HERMES-guard call shape, ff-only-refuse-
    don't-rebase pull semantics) is preserved byte-for-byte; `commit_scoped`'s
    `sync=True` path uses `pull_or_rebase_strict` (rebase-reconcile) instead
    of this function's `pull_repo_strict` (hard ff-only refuse), a real
    behavior change this function's existing callers don't need.

    Partial-stage guard (step 4b): for every topic in `fired_topics` with
    status 'ok', assert that `reports_dir/<slug>.md` exists on disk.  If any
    are missing (researcher wrote the file outside the worktree — T-0660
    regression vector), log loud to RUNNER_LOG and return (False, msg) without
    committing.  The log line is greppable: "partial-stage refused: [<slugs>]".

    `cwd` is the working tree for git operations. None = REPO_ROOT (legacy
    path, includes pull_repo_strict sync); pass worktree path for T-0471
    isolated-worktree commits (worktree already at origin/main HEAD, so sync
    is redundant and adds latency).
    """
    _cwd = cwd if cwd is not None else REPO_ROOT
    scope_prefix = f"knowledge/watch/reports/{date_iso}/"
    reports_dir = _cwd / "knowledge" / "watch" / "reports" / date_iso

    # 1. HERMES guard on report files. Refusal is loud — surfaces that the
    #    researcher emitted the trigger phrase in landed output. Cheap and
    #    side-effect-free (pure read scan) — stays outside the lock, same as
    #    commit_scoped's own message-guard (step 1 there).
    ok, detail = _hermes_scan_dir(reports_dir)
    if not ok:
        msg = f"HERMES guard refused: {detail}"
        print(msg, file=sys.stderr)
        return False, msg

    # T-1774: pull through push (steps 2-8) is the mutate -> stage -> commit
    # -> push critical section against other writers on the SAME shared
    # checkout (ticket.py / ticket_grooming.py / commit_scoped's other
    # callers all serialize on the same lockfile).
    with git_mutation_lock(label=f"watch_runner land {date_iso}"):
        # 2. Pull-first, fast-forward only via resilient fetch+ff-merge
        #    (T-0442 propagation of T-0218): bypasses FETCH_HEAD multi-branch
        #    pollution. Skipped in worktree mode: with_worktree already
        #    fetched origin/main and the worktree starts at its HEAD; second
        #    fetch+merge is redundant.
        if cwd is None:
            ok, pmsg = pull_repo_strict()
            if not ok:
                return False, pmsg

        # 3. Stage strictly the date's reports dir.
        res = _git("add", "--", scope_prefix, cwd=_cwd)
        if res.returncode != 0:
            return False, f"git add failed: {res.stderr.strip() or res.stdout.strip()}"

        # 4. Read back staged paths; assert every one is in scope. If
        #    anything leaked outside (shouldn't, but belt), reset the index
        #    and refuse.
        res = _git("diff", "--cached", "--name-only", cwd=_cwd)
        if res.returncode != 0:
            return False, f"git diff --cached failed: {res.stderr.strip() or res.stdout.strip()}"
        staged = [p for p in res.stdout.split("\n") if p.strip()]

        leaked = [p for p in staged if not p.startswith(scope_prefix)]
        if leaked:
            _git("reset", "HEAD", cwd=_cwd)
            return False, f"scope guard refused: out-of-scope paths staged: {leaked}"

        # 4b. Partial-stage guard (T-0662, strengthened T-1581): for every
        #     ok-status topic, assert its report file exists inside the
        #     worktree AND is nontrivial+structured (report_landed_ok —
        #     exists+size+frontmatter/heading), not merely present.  If a
        #     researcher wrote to the wrong path (e.g. the canonical
        #     REPO_ROOT instead of the isolated worktree — T-0660 regression
        #     vector) the file will be absent here; if a researcher wrote a
        #     stub or error-dump, it will be present but fail the structural
        #     check.  Either way, refuse without committing and log loud so
        #     the gap is greppable.
        missing = [
            t.slug
            for t in fired_topics
            if statuses.get(t.slug) == "ok"
            and not report_landed_ok(reports_dir / f"{t.slug}.md")
        ]
        if missing:
            msg = f"partial-stage refused: {missing}"
            print(msg, file=sys.stderr)
            RUNNER_LOG.parent.mkdir(parents=True, exist_ok=True)
            with RUNNER_LOG.open("a", encoding="utf-8") as _log:
                _log.write(f"[specialist] {msg}\n")
            return False, msg

        # 5. Nothing-to-commit short-circuit.
        if not staged:
            return True, "no changes"

        # 6. Build message + cheap belt scan on it before committing.
        message = _build_commit_message(date_iso, fired_topics, statuses)
        if not _hermes_scan_message(message):
            msg = "HERMES guard refused: commit message construction"
            print(msg, file=sys.stderr)
            return False, msg

        # 7. Commit (no signing flags, no --no-verify).
        res = _git("commit", "-m", message, cwd=_cwd)
        if res.returncode != 0:
            return False, f"git commit failed: {res.stderr.strip() or res.stdout.strip()}"

        # 8. Push with retry for non-ff (T-0487). No force, no
        #    force-with-lease. HEAD:main: detached-HEAD worktree commit lands
        #    on origin's main ref (T-0486: 'origin main' was a no-op from
        #    with_worktree --detach).
        ok_push, push_msg = push_with_retry(_cwd)
        if not ok_push:
            return False, f"git push failed: {push_msg}"

        return True, f"pushed {len(staged)} file(s) under {scope_prefix}"


# ---------------------------------------------------------------------------
# B11 actuation phase: parse the day's report index, file one ticket per
# ok+TL;DR finding via tools/ticket.py, log + ledger the activity. Idempotent
# on the report relpath (already-filed → skip). Best-effort: failures here
# never block watch_runner exit code.
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    slug: str
    tldr: str
    report_relpath: str
    headline_excerpt: str
    # T-2523: a researcher-declarable "this event is really topic X's" signal,
    # read from the report's frontmatter (`belongs_to_topic:` / its alias
    # `duplicate_of_topic:`). None when the report makes no such declaration —
    # which is the common case and the one that MUST stay a normal peer file.
    # This is DECLARED, never inferred: two topics legitimately raising
    # DIFFERENT asks about the same event carry no declaration and both file
    # (the case a naive same-entity dedupe would wrongly collapse — see the
    # ticket's Context). Only a researcher who judged "same event, same ask,
    # the other topic owns it" writes the field.
    belongs_to_topic: str | None = None


# T-2523: frontmatter keys a researcher can use to declare cross-topic
# ownership. Both spellings accepted; `belongs_to_topic` is canonical.
_BELONGS_TO_TOPIC_KEYS = ("belongs_to_topic", "duplicate_of_topic")


def _parse_report_frontmatter(report_path: Path) -> dict[str, str]:
    """Return the leading `--- ... ---` YAML-ish frontmatter as a flat
    key->value dict (values stripped). Empty dict if the file has no
    frontmatter or does not exist. Deliberately a line-oriented `key: value`
    reader (not a YAML dep): the watch frontmatter is flat by contract
    (`surface:`/`headline:`), and this keeps actuation dependency-free.
    """
    if not report_path.exists():
        return {}
    text = report_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    # Split off the first fenced block: lines between the opening `---` and the
    # next line that is exactly `---`.
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fm: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_findings(date_dir: Path, statuses: dict[str, str]) -> list[Finding]:
    """Parse `date_dir/README.md` for index rows of shape
    `| [<slug>](<slug>.md) | <status> | <summary> |`. For each ok+TL;DR row,
    construct a Finding. Skipped rows print a one-line reason to stderr.
    """
    index = date_dir / "README.md"
    if not index.exists():
        return []
    findings: list[Finding] = []
    row_re = re.compile(r"^\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$")
    for line in index.read_text(encoding="utf-8").splitlines():
        m = row_re.match(line)
        if not m:
            continue
        slug, _link, status, summary = m.group(1), m.group(2), m.group(3).strip(), m.group(4).strip()
        if status != "ok":
            print(f"[actuation] skip {slug}: status={status!r}", file=sys.stderr)
            continue
        if not summary.startswith("TL;DR:"):
            print(f"[actuation] skip {slug}: summary missing TL;DR prefix", file=sys.stderr)
            continue
        tldr = summary[len("TL;DR:"):].strip()
        report_path = date_dir / f"{slug}.md"
        excerpt = ""
        if report_path.exists():
            body = report_path.read_text(encoding="utf-8")
            # `[^\n]*` (not `.*`) on the H1 line: DOTALL flag makes `.`
            # swallow newlines, which would consume the whole body. Restrict
            # the H1 match to a single line, then DOTALL applies to the body
            # capture only.
            em = re.search(r"^# [^\n]*\n(.*?)(?=\n## |\n---|\Z)", body, flags=re.DOTALL | re.MULTILINE)
            if em:
                excerpt = em.group(1).strip()[:500]
        try:
            relpath = report_path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            relpath = f"knowledge/watch/reports/{date_dir.name}/{slug}.md"
        # T-2523: read the optional cross-topic-ownership declaration from the
        # report frontmatter. A researcher writes `belongs_to_topic: <slug>`
        # when the event they are reporting is the same event+ask another
        # topic owns (the T-2488/T-2502 Speechmatics case). A self-referential
        # declaration (`belongs_to_topic: <own slug>`) is meaningless and
        # dropped, so a stray self-tag can never suppress a report's own file.
        fm = _parse_report_frontmatter(report_path)
        belongs_to = None
        for key in _BELONGS_TO_TOPIC_KEYS:
            if fm.get(key):
                belongs_to = fm[key].strip()
                break
        if belongs_to == slug:
            belongs_to = None
        findings.append(
            Finding(
                slug=slug,
                tldr=tldr,
                report_relpath=relpath,
                headline_excerpt=excerpt,
                belongs_to_topic=belongs_to,
            )
        )
    return findings


def already_filed(report_relpath: str, tickets_root: Path) -> bool:
    """True iff any ticket file under tickets_root/{inbox,active,done}/
    contains `report_relpath` verbatim in its body.
    """
    for sub in ("inbox", "active", "done"):
        d = tickets_root / sub
        if not d.exists():
            continue
        for path in d.rglob("*.md"):
            try:
                if report_relpath in path.read_text(encoding="utf-8"):
                    return True
            except (OSError, UnicodeDecodeError):
                continue
    return False


def build_ticket_bodies(finding: Finding) -> tuple[str, str]:
    """Render (ask, context) bodies for a Finding."""
    date_iso = finding.report_relpath.split("/")[2] if "/" in finding.report_relpath else "unknown"
    ask = (
        f"Watch finding from {date_iso} dispatch on topic `{finding.slug}`. "
        f"Source report: `{finding.report_relpath}`. "
        f"TL;DR: {finding.tldr}. "
        f"Decide whether to actuate: if so, do the work and resolve; "
        f"if not, resolve with reason 'no action'."
    )
    context = (
        f"Source: {finding.report_relpath}\n\n"
        f"TL;DR: {finding.tldr}\n\n"
        f"{finding.headline_excerpt}"
    )
    return ask, context


def _invoke_ticket_file(
    argv: list[str],
    log_tag: str,
    env: dict,
    *,
    log_error: Callable[[str, str], None] | None = None,
) -> tuple[str, str | None, str]:
    """Shared invocation + transient-retry kernel for ticket.py file calls.

    Retries only on transient git-push failures (origin contention). Other
    failures (HERMES guard, scope guard, pull conflict) bail immediately —
    they are structural, not transient.

    Returns ``(outcome, ticket_id, detail)`` where:
    - outcome ∈ {"filed", "errored"}
    - ticket_id is the T-NNNN string extracted from stdout, or None
    - detail carries a ``retries=N`` token plus any stderr snippet

    `log_error` (T-1583 REBOUND finding): this kernel is shared by callers
    outside this module (e.g. `tools/s3star_audit_runner.py`), each of which
    owns its own log file and tag convention. Defaulting to ``None`` preserves
    this module's own behavior EXACTLY — logs `"[specialist] actuation: ERROR
    {log_tag} -> {detail}"` to THIS module's `RUNNER_LOG` — for every existing
    in-module caller (`file_finding`, `file_followup`, `file_watch_ticket`), so
    nothing here changes for them. A caller that wants errors attributed to
    its own log/tag passes `log_error=<callable(log_tag, detail) -> None>`
    instead; this kernel never assumes where a non-default logger writes.
    """
    last_stderr = ""
    last_stdout = ""
    retries = 0
    for attempt in range(_MAX_ACTUATION_RETRIES):
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        last_stderr = result.stderr or ""
        last_stdout = result.stdout or ""
        if result.returncode == 0:
            m = re.search(r"filed (T-\d+)\s*", last_stdout)
            tid = m.group(1) if m else None
            return "filed", tid, f"retries={retries}"
        # Non-zero — decide whether to retry.
        if "git push" in last_stderr and attempt < _MAX_ACTUATION_RETRIES - 1:
            retries += 1
            time.sleep(2 ** (attempt + 1))
            continue
        break

    detail = f"retries={retries}; stderr={last_stderr.strip()[:200]}"
    if log_error is not None:
        log_error(log_tag, detail)
    else:
        _append_runner_log(f"[specialist] actuation: ERROR {log_tag} → {detail}")
    return "errored", None, detail


# Maximum follow-ups parsed from a single report. Guards against a malformed
# report spamming the ticket queue in the unattended cron path.
_FOLLOWUP_SOFT_CAP = 20

# Leading markdown list marker on a follow-up bullet (`- `, `* `, `+ `, `1. `,
# `2) `). Stripped BEFORE title derivation so it never leaks into the ticket H1
# — the "# - **Title" broken-heading class the operator hand-fixed on T-0859/T-0946.
_LIST_MARKER_RE = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)")
# A bolded title span followed by ` — <ask>`. Non-greedy so the FIRST `**` pair
# bounds the title; lets the title itself contain an em-dash (`**A — B** — ask`)
# which the plain first-em-dash split would otherwise mangle.
_BOLD_TITLE_RE = re.compile(r"^\*\*(.+?)\*\*\s+—\s+(.+)$")
# Common Markdown alternatives emitted by researchers despite the prompt's
# canonical em-dash contract: ``**Title:** ask`` and ``**Title**: ask``.
_BOLD_COLON_INSIDE_RE = re.compile(r"^\*\*(.+?):\*\*\s+(.+)$")
_BOLD_COLON_OUTSIDE_RE = re.compile(r"^\*\*(.+?)\*\*\s*:\s+(.+)$")
# Stray leading/trailing emphasis (`**`, `*`, `__`, `_`, backtick) left on a
# derived title edge — e.g. an unclosed `**` when an em-dash split fell inside a
# bold span. Trimmed from both ends.
_EDGE_EMPHASIS_RE = re.compile(r"^(?:\*\*|\*|__|_|`)+|(?:\*\*|\*|__|_|`)+$")

# A researcher should omit the section when there is no work, but this exact
# small sentinel family occurs in real reports. It is intentional absence, not
# content that needs a recovery ticket. Keep the vocabulary narrow so prose
# beginning with "none" cannot accidentally suppress an actionable line.
_NO_FOLLOWUP_SENTINEL_RE = re.compile(
    r"^(?:none\.?|none\.\s+routine monitoring sufficient\.?|"
    r"no (?:actionable )?follow[- ]?ups?(?: required)?\.?)$",
    re.IGNORECASE,
)

# The auto-commit classifier's forbidden filename is constructed in pieces so
# that the trigger bytes never occur in source. Escape at the ticket-filing
# sink: parsed items and lossless catch-alls share that sink, so neither can
# smuggle researcher-authored trigger bytes into a commit.
_CLASSIFIER_TRIGGER = "HERME" + "S.md"
_CLASSIFIER_TRIGGER_ESCAPED = "HERME" + r"S\.md"


@dataclass(frozen=True)
class FollowupParseResult:
    """Lossless result for one report's follow-up section."""

    parsed: tuple[tuple[str, str], ...]
    rejected: tuple[str, ...]


def _clean_followup_title(title: str) -> str:
    """Trim stray edge emphasis markers from a derived follow-up title. Idempotent."""
    return _EDGE_EMPHASIS_RE.sub("", title).strip()


def _split_followup_line(line: str) -> tuple[str, str] | None:
    """Parse one marker-free line using the accepted researcher shapes."""
    for pattern in (
        _BOLD_TITLE_RE,
        _BOLD_COLON_INSIDE_RE,
        _BOLD_COLON_OUTSIDE_RE,
    ):
        match = pattern.match(line)
        if match:
            title = _clean_followup_title(match.group(1).strip())
            ask = match.group(2).strip()
            return (title, ask) if title and ask else None

    for separator in (" — ", " - ", ": "):
        if separator not in line:
            continue
        title, ask = line.split(separator, 1)
        title = _clean_followup_title(title.strip())
        ask = ask.strip()
        return (title, ask) if title and ask else None
    return None


def parse_followups_detailed(body: str) -> FollowupParseResult:
    """Extract follow-ups without losing non-sentinel input.

    Accepts the canonical em-dash separator, a spaced ASCII hyphen, a colon,
    and the common bold-colon Markdown variants. Blank lines and explicit
    no-follow-up sentinels are intentional absence. Every other unparseable or
    over-cap line is returned verbatim in ``rejected`` for durable filing.
    """
    match = re.search(r"^## Follow-ups\s*$", body, re.MULTILINE)
    if not match:
        return FollowupParseResult((), ())

    section_body = body[match.end():]
    end_match = re.search(r"^(?:## |---)", section_body, re.MULTILINE)
    if end_match:
        section_body = section_body[: end_match.start()]

    parsed: list[tuple[str, str]] = []
    rejected: list[str] = []
    overflow = 0
    for raw_line in section_body.splitlines():
        raw = raw_line.strip()
        if not raw:
            continue
        line = _LIST_MARKER_RE.sub("", raw, count=1).strip()
        if _NO_FOLLOWUP_SENTINEL_RE.fullmatch(line):
            continue

        item = _split_followup_line(line)
        if item is None:
            print(
                f"[actuation] followup: preserving malformed line in catch-all: {raw!r}",
                file=sys.stderr,
            )
            rejected.append(raw)
            continue
        if len(parsed) >= _FOLLOWUP_SOFT_CAP:
            overflow += 1
            rejected.append(raw)
            continue
        parsed.append(item)

    if overflow:
        print(
            f"[actuation] followup: follow-up cap ({_FOLLOWUP_SOFT_CAP}) exceeded; "
            f"preserving {overflow} overflow item(s) in catch-all",
            file=sys.stderr,
        )
    return FollowupParseResult(tuple(parsed), tuple(rejected))


def parse_followups(body: str) -> list[tuple[str, str]]:
    """Backward-compatible parsed-only view of ``parse_followups_detailed``."""
    return list(parse_followups_detailed(body).parsed)


def _escape_classifier_trigger(text: str) -> str:
    """Escape the auto-commit classifier trigger in researcher-authored text."""
    return text.replace(_CLASSIFIER_TRIGGER, _CLASSIFIER_TRIGGER_ESCAPED)


def _read_report_body(finding: Finding) -> str:
    """Read a watch report's file body. Returns '' on missing file or decode error."""
    report_path = REPO_ROOT / finding.report_relpath
    try:
        return report_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def build_followup_ticket_bodies(
    title: str,
    ask: str,
    finding: Finding,
    parent_id: str | None,
) -> tuple[str, str]:
    """Render ``(ask_body, context_body)`` for a follow-up ticket.

    The context body MUST contain ``finding.report_relpath`` verbatim so
    ``already_filed`` covers this ticket on subsequent runs (idempotency gate).
    """
    ask_body = (
        f"{title} — {ask} "
        f"(from watch report: {finding.report_relpath})"
    )
    parent_ref = parent_id if parent_id else "n/a"
    context_body = (
        f"Source: {finding.report_relpath}\n\n"
        f"Follow-up from watch report on topic `{finding.slug}`.\n\n"
        f"Parent headline ticket: {parent_ref}"
    )
    return ask_body, context_body


def file_followup(
    title: str,
    ask: str,
    finding: Finding,
    *,
    parent_id: str | None,
    ticket_bin: Path = TICKET_BIN,
    env: dict | None = None,
) -> tuple[str, str | None, str]:
    """Invoke ``ticket.py file`` for one follow-up item.

    Returns ``(outcome, ticket_id, detail)`` with the same semantics as
    ``file_finding``. Retries on transient git-push failure only.
    target=specialist; labels low-pri, triage, from-watch, source-topic-<slug>.

    Routing (T-1217, per orchestrator mailbox triage 2026-06-16): follow-ups go to
    **specialist** (the watch-loop owner), NOT orchestrator. A follow-up's TYPE
    (research/eval/landscape vs security vs Echo vs methodology) is content-
    dependent, not determined by its source topic — context-engineering spawns
    both research AND brief-shape methodology follow-ups; mcp-supply-chain spawns
    both security AND research-eval. No automated topic→owner rule is correct, so
    the watch-loop owner (specialist, who knows the topics + content) triages its own
    follow-ups and routes the domain ones (security→recovery, Echo/methodology→orchestrator)
    onward. Defaulting to orchestrator dumped that triage on HO and
    mis-routed the research majority (the recurrence this fixes).
    """
    if env is None:
        env = os.environ.copy() | {"PANTHEON_ACTOR": "specialist"}

    # Researcher prose is crossing into an auto-committed ticket. Apply the
    # classifier escape at this shared sink so both ordinary parsed follow-ups
    # and rejected-line catch-alls are safe by construction.
    title = _escape_classifier_trigger(title)
    ask = _escape_classifier_trigger(ask)
    ask_body, context_body = build_followup_ticket_bodies(title, ask, finding, parent_id)
    merged_labels = ["low-pri", "triage", "from-watch", f"source-topic-{finding.slug}"]
    argv = [
        sys.executable,
        str(ticket_bin),
        "file",
        "--requester", "specialist",
        "--target", "specialist",
        "--title", title[:120],
        "--labels", ",".join(merged_labels),
        "--ask", ask_body,
        "--context", context_body,
    ]
    if parent_id is not None:
        argv += ["--parent-ticket", parent_id]

    return _invoke_ticket_file(argv, f"file_followup {finding.slug}:{title[:40]}", env)


def file_unparsed_followups(
    lines: Sequence[str],
    finding: Finding,
    *,
    parent_id: str | None,
    ticket_bin: Path = TICKET_BIN,
    env: dict | None = None,
) -> tuple[str, str | None, str]:
    """File one durable recovery ticket containing every rejected raw line.

    Delegating to ``file_followup`` preserves the report-path idempotency key,
    ordinary retry/logging/routing behavior, and classifier escaping.
    """
    if not lines:
        return "skipped_dup", None, "no rejected follow-ups"
    ask = (
        "The watch follow-up parser could not safely split or individually "
        "file the following raw lines. Preserve and triage every line:\n\n"
        + "\n".join(f"- {line}" for line in lines)
    )
    return file_followup(
        f"Recover unparsed watch follow-ups: {finding.slug}",
        ask,
        finding,
        parent_id=parent_id,
        ticket_bin=ticket_bin,
        env=env,
    )


def file_finding(
    finding: Finding,
    policy: dict,
    *,
    ticket_bin: Path = TICKET_BIN,
    env: dict | None = None,
) -> tuple[str, str | None, str]:
    """Invoke `ticket.py file` with policy-derived argv. Returns
    `(outcome, ticket_id, detail)` where outcome ∈ {"filed", "errored", "skipped_dup"}.
    `detail` carries human-readable context plus a `retries=N` token for
    callers that want to count.

    Retry only on transient `git push` failures (origin contention). Other
    failures bail without retry — HERMES guard, scope guard, pull conflict
    are not transient and re-running won't help.
    """
    if env is None:
        env = os.environ.copy() | {"PANTHEON_ACTOR": "specialist"}

    ask, context = build_ticket_bodies(finding)
    title = finding.tldr[:120]
    merged_labels = (
        ["b11", "actuation", f"source-topic-{finding.slug}"]
        + list(policy.get("labels", []))
        + (["interactive-required"] if policy.get("interactive_required") else [])
    )
    argv = [
        sys.executable,
        str(ticket_bin),
        "file",
        "--requester", "specialist",
        "--target", policy["target"],
        "--title", title,
        "--labels", ",".join(merged_labels),
        "--ask", ask,
        "--context", context,
    ]
    return _invoke_ticket_file(argv, f"file_finding {finding.slug}", env)


def file_cross_reference(
    finding: Finding,
    primary_finding: Finding,
    policy: dict,
    *,
    ticket_bin: Path = TICKET_BIN,
    env: dict | None = None,
) -> tuple[str, str | None, str]:
    """T-2523: file a secondary finding that DECLARED `belongs_to_topic` as a
    cross-reference note rather than a peer actuation ticket.

    Called only when the declared primary topic also produced an `ok` finding
    in the same run, so the event is already being filed once (by the primary).
    This ticket exists to preserve the secondary topic's angle + its source
    report link WITHOUT charging triage a second full actuation. It is filed
    to the SAME target as a normal finding (so it lands in the same queue), but
    is labeled `cross-reference`/`duplicate-of-topic-<primary>` and its ask
    says up front "the primary lives in topic X; resolve as a no-op unless this
    topic's angle needs separate work" — the reviewer's cost is a glance, not a
    re-adjudication.

    Design note: this is deliberately a linked NOTE, not a suppression, because
    the two reports can carry different framing even for one event; dropping the
    secondary outright would lose that. Suppress-entirely is a future option if
    triage says the cross-ref itself is noise — flagged in the ticket.
    """
    if env is None:
        env = os.environ.copy() | {"PANTHEON_ACTOR": "specialist"}

    ask, _context = build_ticket_bodies(finding)
    ask = (
        f"CROSS-REFERENCE (T-2523): this event is declared to belong to topic "
        f"`{finding.belongs_to_topic}`, whose report `{primary_finding.report_relpath}` "
        f"files the primary actuation this run. Resolve as 'no action — see "
        f"primary in {finding.belongs_to_topic}' UNLESS this topic (`{finding.slug}`) "
        f"raises a genuinely different ask about the same event. " + ask
    )
    context = (
        f"Source (secondary): {finding.report_relpath}\n"
        f"Primary topic: {finding.belongs_to_topic} "
        f"({primary_finding.report_relpath})\n\n"
        f"TL;DR: {finding.tldr}\n\n"
        f"{finding.headline_excerpt}"
    )
    title = f"[xref → {finding.belongs_to_topic}] {finding.tldr[:100]}"
    merged_labels = (
        ["b11", "actuation", "cross-reference",
         f"source-topic-{finding.slug}",
         f"duplicate-of-topic-{finding.belongs_to_topic}"]
        + list(policy.get("labels", []))
    )
    argv = [
        sys.executable,
        str(ticket_bin),
        "file",
        "--requester", "specialist",
        "--target", policy["target"],
        "--title", title,
        "--labels", ",".join(merged_labels),
        "--ask", ask,
        "--context", context,
    ]
    return _invoke_ticket_file(argv, f"file_cross_reference {finding.slug}", env)


def _append_runner_log(line: str, *, log_path: Path | None = None) -> None:
    # Resolve at call time so tests can patch `tools.watch_runner.RUNNER_LOG`
    # and have the change take effect; default-argument binding happens at
    # function-definition time and would otherwise capture the original path.
    if log_path is None:
        log_path = RUNNER_LOG
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{_utc_iso_now()} {line}\n")
    except OSError:
        # Best-effort log; never raise out of the actuation path.
        pass


def log_actuation_summary(
    filed: int,
    surfaced: int,
    retried: int,
    errored: int,
    *,
    log_path: Path = RUNNER_LOG,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(
            f"{_utc_iso_now()} [specialist] actuation: "
            f"filed {filed} tickets, surfaced {surfaced} findings, "
            f"retried {retried}, errored {errored}\n"
        )


def write_actuation_ledger(
    today: date,
    filed_ids: list[str],
    date_iso: str,
    *,
    ledger_path: Path | None = None,
) -> Path | None:
    """Append one ledger entry summarizing the actuation. No-op if no tickets
    were filed (a watch cycle that surfaced zero actuables shouldn't add ledger
    noise — the runner log already records the surfaced/errored counts).

    Returns the resolved ledger path written to (or None on the no-op path),
    so the companion git commit stages exactly that dynamic path. The target
    is resolved via `current_write_target` (T-0148 Phase 2): the legacy
    `ledger/2026.jsonl` while it exists, else the current ISO-week shard.
    """
    if len(filed_ids) == 0:
        return None
    ts = _utc_iso_now()
    # T-1088: build the canonical ORG-001 entry via the shared _audit helper.
    # This now emits the full 10 fields (cost_usd/parent_id/supersedes=null) —
    # the prior inline entry omitted that provenance trio (8-field drift the
    # convergence closes). Readers tolerate the added null keys (all use .get()
    # or only read id/ts/action). `append_ledger_entries` owns the
    # resolve→ensure→append kernel and RETURNS the path it wrote.
    entry = build_ledger_entry(
        entry_id=f"{ts}-actuation",
        ts=ts,
        agent="specialist",
        action=f"actuation_{date_iso}",
        rationale=(
            f"Auto-filed {len(filed_ids)} ticket(s) from {date_iso} watch reports."
        ),
        justifies_via=["P1", "D1", "D3"],
        links=[{"type": "ticket", "ref": tid} for tid in filed_ids]
        + [{"type": "file", "ref": f"knowledge/watch/reports/{date_iso}/"}],
    )
    return append_ledger_entries(
        [entry], ledger_dir=REPO_ROOT / "ledger", shard=ledger_path
    )


def git_commit_ledger_entry(today: date, ledger_path: Path | None = None) -> tuple[bool, str]:
    """Narrow companion to git_commit_and_push for the ledger JSONL append.

    Order: HERMES message belt → ff-only pull → narrow stage → scope-readback
    assert → commit → push. No dir scan (JSONL is structured + this code
    constructs it; not researcher-authored). On any step failure: log + return
    (False, msg). Don't reset the index on push failure — lets a retry land
    it later.
    """
    # Stage the SAME dynamic target that write_actuation_ledger appended to
    # (T-0148 Phase 2): the legacy ledger/2026.jsonl while it exists, else the
    # current ISO-week shard ledger/<seg>/<seg>.jsonl. The scope-readback
    # assert below then expects this dynamic rel path, not a hardcoded one.
    if ledger_path is None:
        ledger_path = current_write_target(REPO_ROOT / "ledger", datetime.now(timezone.utc))
    rel = ledger_path.relative_to(REPO_ROOT).as_posix()
    short_body = f"Specialist actuation ledger entry for {today.isoformat()}."
    message = f"chore(ledger): actuation entry {today.isoformat()}\n\n{short_body}\n"

    # Delegates to the shared commit_scoped primitive (T-1088 convergence).
    # Behavior deltas vs the prior inline pipeline, all safe for this non-detached
    # REPO_ROOT push:
    #   - plain `git push origin main` → `push_with_retry(HEAD:main)`: strict superset
    #     (adds non-ff retry; identical target since HEAD is on main here);
    #   - `pull_repo_strict` → `pull_or_rebase_strict`: adds rebase-onto-origin fallback;
    #   - adds a post-stage HERMES scan of the staged JSONL (harmless — structured,
    #     code-constructed data; the prior "no dir scan" note was an optimization).
    # RUNNER_LOG-on-failure side-effect preserved by the wrap below.
    ok, detail = commit_scoped([rel], message, cwd=REPO_ROOT, push=True, sync=True)
    if not ok:
        _append_runner_log(f"[specialist] actuation: {detail}")
        return False, detail
    return True, f"pushed {rel}"


def actuate_phase(today: date, fires: list[Topic], statuses: dict[str, str]) -> None:
    """Top-level orchestrator for the post-digest actuation phase.

    Best-effort: any unexpected exception is logged to RUNNER_LOG and
    swallowed. Caller must not let this fail the watch cycle.
    """
    try:
        date_dir = REPORTS_ROOT / today.isoformat()
        findings = extract_findings(date_dir, statuses)
        surfaced = len(findings)
        filed_ids: list[str] = []
        retried = 0
        errored = 0
        tickets_root = REPO_ROOT / "tickets"
        # T-2523: index this run's findings by slug so a secondary report's
        # declared `belongs_to_topic` can be checked against topics that
        # ACTUALLY produced a finding this run. A declaration pointing at a
        # topic absent from the run (paused, not-due, or errored) is NOT
        # honored — we do not suppress against a primary that never filed, or
        # the event would silently vanish. In that case the secondary files
        # as a normal peer, which is the safe direction.
        findings_by_slug = {f.slug: f for f in findings}
        for finding in findings:
            policy = TOPIC_POLICY.get(finding.slug, _DEFAULT_POLICY)
            if already_filed(finding.report_relpath, tickets_root):
                continue
            # T-2523: if this finding declared it belongs to another topic AND
            # that topic is present in this run, file a cross-reference note
            # instead of a peer actuation ticket. The primary topic files the
            # real one. Absent-primary falls through to a normal file below.
            primary = (
                findings_by_slug.get(finding.belongs_to_topic)
                if finding.belongs_to_topic
                else None
            )
            if primary is not None:
                outcome, ticket_id, detail = file_cross_reference(
                    finding, primary, policy
                )
            else:
                outcome, ticket_id, detail = file_finding(finding, policy)
            if outcome == "filed":
                if ticket_id is not None:
                    filed_ids.append(ticket_id)
                # Detail carries `retries=N` token; bump retried if N>0.
                m = re.search(r"retries=(\d+)", detail)
                if m and int(m.group(1)) > 0:
                    retried += 1
            elif outcome == "errored":
                errored += 1

            # File follow-ups from the report's ## Follow-ups section.
            # Done in the same pass (inside the already_filed guard) so the
            # per-report idempotency key covers both headline and follow-ups.
            report_body = _read_report_body(finding)
            followups = parse_followups_detailed(report_body)
            for fu_title, fu_ask in followups.parsed:
                fu_outcome, fu_id, _fu_detail = file_followup(
                    fu_title,
                    fu_ask,
                    finding,
                    parent_id=ticket_id,
                )
                if fu_outcome == "filed":
                    if fu_id is not None:
                        filed_ids.append(fu_id)
                elif fu_outcome == "errored":
                    errored += 1

            # A stochastic researcher format must never be a data-loss gate.
            # One recovery ticket preserves every rejected/over-cap line; it
            # carries the same report-path key as normal follow-ups, so the
            # outer already_filed guard makes a rerun idempotent.
            if followups.rejected:
                recovery_outcome, recovery_id, _recovery_detail = (
                    file_unparsed_followups(
                        followups.rejected,
                        finding,
                        parent_id=ticket_id,
                    )
                )
                if recovery_outcome == "filed":
                    if recovery_id is not None:
                        filed_ids.append(recovery_id)
                elif recovery_outcome == "errored":
                    errored += 1

        log_actuation_summary(len(filed_ids), surfaced, retried, errored)

        if filed_ids:
            written_path = write_actuation_ledger(today, filed_ids, today.isoformat())
            ok, lmsg = git_commit_ledger_entry(today, written_path)
            if not ok:
                _append_runner_log(f"[specialist] actuation: ledger commit {lmsg}")
    except Exception as exc:
        _append_runner_log(f"[specialist] actuation: TOP-LEVEL EXCEPTION {exc!r}")
        return


def land_reports(
    today: date,
    *,
    force: bool = False,
    topic_filter: str | None = None,
    no_actuate: bool = False,
) -> int:
    """In-session post-dispatch landing (ORG-025 / T-0855).

    This is the second half of the migrated watch cycle. After Specialist's
    interactive session has dispatched the due topics via the Agent tool (one
    `researcher`-brief subagent per `--emit-due-briefs` record) and each
    subagent has written its report to `REPORTS_ROOT/<date>/<slug>.md`, this
    function lands them — this is what replaced the retired headless fire's
    post-dispatch state/commit/actuate tail, WITHOUT a per-fire /tmp worktree.

    Ground-truth is the disk: a report file that is landed-ok (exists, at least
    `REPORT_MIN_BYTES`, and has the template's frontmatter-headline +
    `## `-heading structure — T-1581) = `ok`; absent, too small, or
    unstructured = `fail` (the dispatch produced nothing usable). The day-index
    summary
    is `TL;DR: <report headline frontmatter>` so the actuation phase
    (extract_findings) picks ok rows up exactly as it does for the legacy path.

    Flow: per due topic → derive status from disk, append day-index row
    (idempotent), update_state → save_state → git_commit_and_push (cwd=None:
    pull-ff-only + narrow `git add` scope + HERMES guard + partial-stage guard,
    no worktree per the flagged P1 decision) → actuate_phase (best-effort).

    Respects --topic / --force the same way --emit-due-briefs / main() do, so the
    landed set matches the emitted set. Returns 1 if any topic failed or the
    commit/push failed (so a wrapping caller / journal flags it), else 0.
    """
    topic_warnings: list[str] = []
    topics = parse_topics(TOPICS_FILE, warnings=topic_warnings)
    state = load_state()
    for w in topic_warnings:
        print(f"[topic-parse] WARNING: {w}", file=sys.stderr)
    if topic_filter:
        topics = [t for t in topics if t.slug == topic_filter]
        if not topics:
            print(f"unknown topic: {topic_filter}", file=sys.stderr)
            return 2

    date_dir = REPORTS_ROOT / today.isoformat()
    write_topic_warnings(date_dir, topic_warnings)
    fires: list[Topic] = []
    statuses: dict[str, str] = {}
    failures = 0

    for topic in topics:
        if force or topic_filter:
            due = topic.cadence != "paused"
        else:
            due = topic.is_due(last_fire_date(state, topic.slug), today)
        if not due:
            continue

        report_path = date_dir / f"{topic.slug}.md"
        landed = report_landed_ok(report_path)
        if landed:
            status = "ok"
            headline = parse_report_headline(report_path)
            summary = f"TL;DR: {headline}" if headline else "TL;DR: (no headline frontmatter)"
            # T-2614: accumulate any brief_corrections this report found —
            # the "rides the same pipe as the defect" step. Best-effort:
            # accumulate_brief_corrections never raises.
            new_ids = accumulate_brief_corrections(state, topic.slug, report_path, today)
            if new_ids:
                print(f"  {topic.slug}: +{len(new_ids)} brief correction(s) accumulated")
        else:
            status = "fail"
            summary = "dispatch produced no usable report on disk"
            failures += 1

        fires.append(topic)
        statuses[topic.slug] = status
        if not day_index_has_row(date_dir, topic.slug):
            append_day_index_entry(date_dir, topic.slug, status, summary)
        update_state(state, topic.slug, today, status)
        print(f"  {topic.slug} → {status}")

    # T-2148: sweep for reports that reached disk outside the topics.md dispatch
    # list. Runs AFTER the topic loop so a due topic is indexed by its own path
    # (with its real status), and this only picks up what that path cannot see.
    adhoc = index_unindexed_reports(date_dir)
    for slug in adhoc:
        print(f"  {slug} → ok (ad-hoc, indexed by disk sweep)")

    if not fires and not adhoc:
        print(f"[{today.isoformat()}] no topics due to land")
        _append_runner_log(f"[{today.isoformat()}] land-commit: no topics due")
        return 0

    save_state(state)

    ok, gmsg = git_commit_and_push(today.isoformat(), fires, statuses)
    stream = sys.stdout if ok else sys.stderr
    print(f"[git] {gmsg}", file=stream)
    if not ok:
        # Reports on disk but not on origin = invisible state. Flag via exit.
        failures += 1

    if not no_actuate and ok:
        try:
            actuate_phase(today, fires, statuses)
        except Exception as exc:
            try:
                RUNNER_LOG.parent.mkdir(parents=True, exist_ok=True)
                with RUNNER_LOG.open("a", encoding="utf-8") as f:
                    f.write(
                        f"{_utc_iso_now()} [specialist] land-commit actuation: "
                        f"TOP-LEVEL EXCEPTION {exc!r}\n"
                    )
            except Exception:
                pass
    elif not no_actuate and not ok:
        # T-1739 GAP 1: reports on disk but not on origin (guard refusal, push
        # failure, or scope-guard refusal) = invisible state. Actuating here
        # files follow-up tickets off reports that never landed, producing the
        # split state the 2026-07-13 live repro hit (37 orphaned-report
        # tickets). Skip actuation; the operator repairs the land and re-runs
        # with --actuate-only (the designed manual recovery path) once the
        # reports are actually on origin.
        print(
            f"ERROR: skipping actuation — report land failed ({gmsg}); "
            "repair the land and re-run with --actuate-only.",
            file=sys.stderr,
        )

    return 1 if failures else 0


def file_watch_ticket(
    today: date,
    *,
    force: bool = False,
    ticket_bin: Path = TICKET_BIN,
    env: dict | None = None,
) -> int:
    """Migrated timer entrypoint (ORG-025 / T-0855): the daily timer runs THIS
    instead of `--commit-prod`.

    Computes which topics are due (same cadence/state logic as the legacy fire);
    if any are due, files ONE `target=specialist` "watch dispatch" ticket whose body
    lists the due slugs + the in-session dispatch recipe (emit → Agent-tool
    dispatch → land-commit). Specialist's existing inbox-consumer
    (pantheon-inbox-consumer@specialist.timer) delivers it into the interactive
    session, where Specialist does the actual researcher dispatch via the Agent tool.

    This process fires NO `claude -p` and NO researcher — it only files the
    handoff ticket. That is the whole point of the migration: the cron stops
    being a researcher-dispatcher and becomes a due-detector + ticket-filer,
    moving the (subscription-billed) dispatch into Specialist's interactive session.

    Idempotent per day: if a watch-dispatch ticket for `today` is still sitting
    unconsumed in tickets/inbox/, this is a no-op (avoids duplicate tickets if
    the timer double-fires or the consumer is slow). Returns 0 on success /
    nothing-due / already-filed; 1 on a filing failure (so the journal flags it).
    """
    records = compute_due_briefs(today, force=force)
    due_slugs = [r["slug"] for r in records]
    if not due_slugs:
        print(f"[{today.isoformat()}] no topics due; no watch ticket filed")
        _append_runner_log(f"[{today.isoformat()}] file-watch-ticket: none due")
        return 0

    # Idempotency: the marker line lives in the ticket context; scan the inbox
    # for it before filing again.
    marker = f"watch-dispatch-for: {today.isoformat()}"
    inbox = REPO_ROOT / "tickets" / "inbox"
    if inbox.exists():
        for p in inbox.glob("*.md"):
            try:
                if marker in p.read_text(encoding="utf-8"):
                    print(
                        f"[{today.isoformat()}] watch dispatch ticket already in inbox "
                        f"({p.name}); skipping"
                    )
                    return 0
            except OSError:
                continue

    title = f"Watch dispatch {today.isoformat()} — {len(due_slugs)} topic(s) due"
    slug_lines = "\n".join(f"- {s}" for s in due_slugs)
    ask = (
        f"{len(due_slugs)} watch topic(s) are due today ({today.isoformat()}). "
        f"Dispatch each in-session (ORG-025), then land:\n"
        f"1. `python3 tools/watch_runner.py --emit-due-briefs` → JSON list of "
        f"{{slug, brief, output_path}}.\n"
        f"2. For each record, dispatch a general-purpose subagent (Agent tool, "
        f"model sonnet) with prompt = the record's `brief`; the subagent writes "
        f"its report to `output_path`.\n"
        f"3. `python3 tools/watch_runner.py --land-commit` → lands the reports, "
        f"commits (narrow scope + HERMES guard), and actuates.\n"
        f"Due slugs:\n{slug_lines}"
    )
    context = (
        f"watch-dispatch-for: {today.isoformat()}\n\n"
        f"Filed by `watch_runner.py --file-watch-ticket` — the migrated timer "
        f"entrypoint (ORG-025 / T-0855). The timer no longer fires researchers "
        f"itself via headless `claude -p`; it hands the due-topic list to Specialist's "
        f"interactive session, which dispatches via the Agent tool. See "
        f"adr/ORG-025-specialist-orchestration-model.md."
    )
    if env is None:
        env = os.environ.copy() | {"PANTHEON_ACTOR": "specialist"}
    argv = [
        sys.executable,
        str(ticket_bin),
        "file",
        "--requester", "specialist",
        "--target", "specialist",
        "--title", title,
        "--labels", "watch,watch-dispatch,ORG-025",
        "--ask", ask,
        "--context", context,
    ]
    outcome, tid, detail = _invoke_ticket_file(
        argv, f"file_watch_ticket {today.isoformat()}", env
    )
    if outcome == "filed":
        print(
            f"[{today.isoformat()}] filed watch dispatch ticket "
            f"{tid or '(id?)'} for: {', '.join(due_slugs)}"
        )
        _append_runner_log(
            f"[specialist] file-watch-ticket: filed {tid} ({len(due_slugs)} due)"
        )
        return 0
    print(
        f"[{today.isoformat()}] FAILED to file watch dispatch ticket: {detail}",
        file=sys.stderr,
    )
    return 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Pantheon watch-loop driver. Default-safe contract (T-0261 per "
                    "sop/standard/auto-commit-cron-default-safe-contract.md): must pass "
                    "--land-commit, --file-watch-ticket, --emit-due-briefs, or "
                    "--actuate-only explicitly.",
    )
    p.add_argument(
        "--topic",
        help="restrict emit/land to one topic by slug regardless of due-state; "
             "with --clear-correction, the topic slug of the correction to clear",
    )
    p.add_argument("--force", action="store_true", help="ignore cadence; treat all topics as due")
    p.add_argument("--no-actuate", action="store_true", help="skip the post-digest actuation phase")
    p.add_argument(
        "--actuate-only",
        action="store_true",
        help="run actuation against today's existing reports without dispatching anything; for manual smoke. Counts as safe-mode.",
    )
    p.add_argument(
        "--emit-due-briefs",
        action="store_true",
        help="ORG-025/T-0855 in-session dispatch: print due topics as JSON "
             "[{slug,cadence,output_path,brief}] for Specialist to dispatch via the Agent "
             "tool. Read-only (no dispatch / state / commit); counts as safe-mode. "
             "Respects --topic / --force.",
    )
    p.add_argument(
        "--land-commit",
        action="store_true",
        help="ORG-025/T-0855 in-session dispatch: land reports Specialist's subagents "
             "already wrote to REPORTS_ROOT/<date>/ — derive ok/fail from disk, append "
             "day-index rows, update state, commit+push (narrow scope, no worktree), "
             "then actuate. Prod-mutating mode. "
             "Respects --topic / --force / --no-actuate.",
    )
    p.add_argument(
        "--file-watch-ticket",
        action="store_true",
        help="ORG-025/T-0855 MIGRATED TIMER ENTRYPOINT: compute due topics; if any, "
             "file ONE target=specialist 'watch dispatch' ticket (due slugs + dispatch recipe) "
             "for Specialist's inbox-consumer to deliver in-session. Fires NO claude -p / NO "
             "researcher. Idempotent per day. Prod-mutating mode. Respects --force.",
    )
    p.add_argument(
        "--clear-correction",
        action="store_true",
        help="T-2614: remove one un-cleared brief_corrections entry (requires "
             "--topic and --id). Run after folding the correction into "
             "knowledge/watch/topics.md by hand. Local state.json mutation only "
             "(no git commit/push). Mutually exclusive with the other modes.",
    )
    p.add_argument(
        "--id",
        help="correction id to clear (with --clear-correction; T-2614)",
    )
    p.add_argument(
        "--note",
        help="optional note recorded to the runner log when clearing a "
             "correction (with --clear-correction; T-2614)",
    )
    return p.parse_args(argv)


def _enforce_default_safe(args: argparse.Namespace) -> int | None:
    """Refuse to run if neither a prod nor a safe mode is explicitly chosen.

    Default-safe contract (T-0261 per the auto-commit-cron SOP): avoid silent
    prod-target. Caller must opt in via a prod mode (--land-commit or
    --file-watch-ticket, the ORG-025 in-session halves), OR opt out via a safe
    mode (--actuate-only / --emit-due-briefs). Mixing a prod mode with a
    safe-mode flag — or both prod modes — is ambiguous → refuse.

    --clear-correction (T-2614) is a fifth, distinct mode: it mutates only
    the local, non-git-tracked state.json (no origin commit/push), so it
    doesn't fit the prod-vs-safe dichotomy above — it's checked first and is
    mutually exclusive with all four dispatch-pipeline modes, requiring both
    --topic and --id when set.

    Returns exit code (64) if the invocation should be rejected; None otherwise.
    """
    safe_explicit = (
        args.actuate_only
        or args.emit_due_briefs
    )
    # Prod modes mutate origin (commit/push of reports or tickets). Exactly one
    # must be chosen for a prod run; --land-commit and --file-watch-ticket are
    # the ORG-025 in-session halves. They are mutually exclusive (each is a
    # distinct operation).
    prod_modes = [
        name for name, on in (
            ("--land-commit", args.land_commit),
            ("--file-watch-ticket", args.file_watch_ticket),
        ) if on
    ]
    prod_explicit = bool(prod_modes)

    if args.clear_correction:
        if prod_explicit or safe_explicit:
            print(
                "ERROR: --clear-correction is mutually exclusive with the "
                "other modes (--land-commit / --file-watch-ticket / "
                "--actuate-only / --emit-due-briefs).",
                file=sys.stderr,
            )
            return 64
        if not args.topic or not args.id:
            print(
                "ERROR: --clear-correction requires both --topic <slug> and "
                "--id <correction-id>.",
                file=sys.stderr,
            )
            return 64
        return None

    if len(prod_modes) > 1:
        print(
            f"ERROR: prod modes are mutually exclusive; pass exactly one of "
            f"{', '.join(prod_modes)}.",
            file=sys.stderr,
        )
        return 64
    if prod_explicit and safe_explicit:
        print(
            "ERROR: prod mode (--land-commit / --file-watch-ticket) conflicts "
            "with safe-mode flag (--actuate-only / --emit-due-briefs).",
            file=sys.stderr,
        )
        return 64
    if not prod_explicit and not safe_explicit:
        print(
            "ERROR: refusing to run without explicit mode. Pass one of:\n"
            "  --file-watch-ticket  file a target=specialist watch-dispatch ticket if topics due (ORG-025 timer)\n"
            "  --land-commit        land in-session-dispatched reports + commit + push (ORG-025)\n"
            "  --emit-due-briefs    print due topics as JSON for in-session dispatch; no side effects\n"
            "  --actuate-only       re-run actuation against today's reports; no dispatch, no commit\n"
            "  --clear-correction   remove one un-cleared brief_corrections entry (needs --topic + --id; T-2614)\n"
            "Per sop/standard/auto-commit-cron-default-safe-contract.md (T-0261).",
            file=sys.stderr,
        )
        return 64
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Default-safe gate (T-0261).
    refusal = _enforce_default_safe(args)
    if refusal is not None:
        return refusal

    # --clear-correction (T-2614): local state.json-only operator ack/clear
    # path. No `today` needed, no origin mutation — handled before the
    # dispatch-pipeline branches below.
    if args.clear_correction:
        state = load_state()
        found = clear_correction(state, args.topic, args.id)
        if not found:
            print(
                f"no un-cleared brief_correction found for topic={args.topic!r} "
                f"id={args.id!r}",
                file=sys.stderr,
            )
            return 1
        save_state(state)
        note_suffix = f" note={args.note}" if args.note else ""
        _append_runner_log(
            f"[operator] cleared brief_correction {args.topic}:{args.id}{note_suffix}"
        )
        print(f"cleared brief_correction {args.topic}:{args.id}")
        return 0

    today = datetime.now(timezone.utc).date()

    # --emit-due-briefs (ORG-025/T-0855): read-only. Print due topics as JSON for
    # the in-session dispatch path. No HO-window check (not dispatching), no state
    # mutation, no commit. Respects --topic / --force for manual/forced emission.
    if args.emit_due_briefs:
        records = compute_due_briefs(
            today, force=args.force, topic_filter=args.topic or None
        )
        print(json.dumps(records, indent=2))
        return 0

    # --actuate-only: skip the entire dispatch flow and run actuation against
    # today's already-on-disk reports. For manual smoke / re-run after a partial
    # failure. Doesn't touch state, doesn't commit reports, doesn't redispatch.
    if args.actuate_only:
        actuate_phase(today, [], {})
        return 0

    # --land-commit (ORG-025/T-0855): land reports the in-session subagents
    # already wrote. No dispatch, no HO-window check (landing consumes no
    # researcher tokens), no worktree (narrow-add + guards in git_commit_and_push
    # instead — the flagged P1 decision). This is the second half of the migrated
    # cycle; --emit-due-briefs is the first half.
    if args.land_commit:
        return land_reports(
            today,
            force=args.force,
            topic_filter=args.topic or None,
            no_actuate=args.no_actuate,
        )

    # --file-watch-ticket (ORG-025/T-0855): the migrated TIMER entrypoint. File a
    # target=specialist watch-dispatch ticket if topics are due; fire no claude -p / no
    # researcher. Specialist's inbox-consumer delivers it into the interactive session.
    if args.file_watch_ticket:
        return file_watch_ticket(today, force=args.force)

    # Unreachable: _enforce_default_safe guarantees exactly one of the four
    # live modes above is set, and each returns. This belt keeps main() honest
    # if the gate ever drifts (it would surface as a loud non-zero rather than
    # an implicit None).
    print("ERROR: no live mode selected (gate drift?)", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
