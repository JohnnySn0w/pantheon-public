#!/usr/bin/env python3
"""ADR / policy / SOP re-evaluation trigger walk (T-2527, design T-2526; T-4556).

Surfaces ADRs' re-evaluation triggers so they actually fire instead of becoming
invisible permanent rules (the ORG-002 failure mode). Home is the weekly-review
generation pass — this module is called by tools/weekly_ticket_summary.py, and
is also runnable standalone (`python3 tools/adr_reeval_walk.py --dry-run`).

Two tiers, per the accepted design (the operator TG 6798 "Auto file"):

  * DETERMINISTIC triggers (calendar past-due; a named ticket resolved; an
    instrumented metric crossed) -> AUTO-FILE a target=role:engineering_orchestrator re-eval ticket,
    DEDUPED against open re-eval tickets so a weekly re-run never re-files.
  * FUZZY / event / prose triggers (cannot be machine-decided as "fired") ->
    SURFACE-ONLY in an "ADR / policy / SOP re-eval watch" section of the weekly review,
    keyword-flagged against the week's activity.

Triggers are read from a structured `re_evaluate:` frontmatter key AND the prose
`## Re-evaluate when` (/ Revisit / Sunset) SECTION bullets (both the bold-id form
`- **ORG-NNN:Rk** - ...` and plain `- ...` bullets). NOTE: free inline prose
OUTSIDE such a section is NOT walked. An inline keyword matcher was dropped for
precision (it dredged ~40 false positives from ADRs that merely DISCUSS re-eval),
so an ADR that states its trigger only inline — no `## Re-evaluate when` section
and no structured key — is currently UNCAUGHT. T-2530 tracks backfilling a
structured `re_evaluate:` key onto those. The structured key is the forward-going,
machine-checkable form (seeded on ORG-062:R1-R4 as the reference example).

Policy and SOP roots (T-4556): `rulebook/policies/` and `sop/` (recursive) are
also scanned, so a trigger declared in a governance doc (e.g. autonomy.md's dead-
bridge trigger, which fired unseen for ~4 months) reaches the watch section. Two
declaration shapes are read for these roots ONLY (ADR parsing is unchanged, so
ADR trigger ids stay stable): the `## Re-evaluate when` section (bullets, or plain
lines when the section has none) and `**Re-evaluate when:** a; b; c.` lines
(split on `;`). A METADATA header line (before the first `##` heading) is dropped
when the doc also has a non-empty section (it is a compressed copy); a mid-body
line (after the first `##`) scopes a subsection and is always kept; a pointer
("see ... section") yields nothing. Every non-ADR trigger is SURFACE-ONLY — no
auto-filing from policy/SOP prose. Ids are path-derived: `policy:<name>:S<n>|H<n>`
and `sop:<subdir>/<name>:S<n>|H<n>` (S = section, H = header line); the `adr_id`
field holds the doc label (`policy:<name>` / `sop:<subdir>/<name>`).
`grand_program.scan_source_obligations` stays ADR-only.

The dedup guard mirrors tools/mirror_drift_sweep.existing_open_refresh_ticket
(the proven idempotent-auto-file pattern; T-1994 / T-0838 directive analog).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import grand_project_program as grand_program

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = REPO_ROOT / "architecture" / "adr"
TICKETS_ROOT = REPO_ROOT / "tickets"
# Governance-doc roots scanned (surface-only) alongside ADRs — T-4556.
POLICY_DIR_REL = Path("rulebook") / "policies"
SOP_DIR_REL = Path("sop")
TICKET_BIN = REPO_ROOT / "tools" / "ticket.py"

# Label vocabulary — the dedup key lives in labels so open-ticket scanning finds it.
REEVAL_LABEL = "adr-reeval"

# Deterministic trigger types (machine-decidable as fired). Everything else is surface-only.
DETERMINISTIC_TYPES = frozenset({"calendar", "metric", "ticket"})

# The re-eval SECTION header — "## Re-evaluate when" / "## Revisit when" /
# "## Sunset ...". This is the deliberate declaration surface; a section with no
# bullets (e.g. ORG-010's schema description) yields no triggers, by design.
_REEVAL_SECTION_RE = re.compile(
    r"^#{2,}\s*(?:Re-?evaluate|Revisit|Sunset)\b[^\n]*\n(?P<body>.*?)(?=^#{1,}\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
# A section bullet leading with a bold trigger id, e.g. "- **ORG-062:R1** - PMB-1 ...".
_ID_BULLET_RE = re.compile(
    r"^\s*[-*]\s*\*\*(?P<tid>[A-Z]{2,}-\d+:R\d+)\*\*\s*[—:-]?\s*(?P<text>.+?)\s*$",
    re.MULTILINE,
)
# A plain section bullet without a bold id, e.g. "- Manual-block usage emerges ...".
_PLAIN_BULLET_RE = re.compile(r"^\s*[-*]\s+(?P<text>\S.+?)\s*$", re.MULTILINE)

# Structured "event" that IS machine-checkable: "resolves:T-1234".
_TICKET_EVENT_RE = re.compile(r"^resolves:\s*(T-\d+)\s*$", re.IGNORECASE)
# Structured metric expr, e.g. "fp_mark_count >= 500".
_METRIC_EXPR_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<op>>=|<=|>|<|==)\s*(?P<num>-?\d+(?:\.\d+)?)\s*$"
)


@dataclasses.dataclass(frozen=True)
class Trigger:
    """One re-evaluation trigger extracted from an ADR."""
    adr_id: str            # e.g. "ORG-062"
    trigger_id: str        # e.g. "ORG-062:R1" (stable dedup key)
    ttype: str             # calendar | metric | ticket | event | prose
    when: str              # raw condition text (ISO date / metric expr / event / prose)
    fires: str             # "auto-ticket" | "surface"
    source: str            # "structured" | "prose"
    text: str              # human-readable description

    @property
    def dedup_key(self) -> str:
        return self.trigger_id


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block, body). Empty frontmatter if none."""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            fm = text[4:end]
            body = text[end + 4:]
            return fm, body
    return "", text


def _load_yaml(fm_block: str, source: str = "?") -> dict:
    try:
        import yaml
    except ModuleNotFoundError:  # pragma: no cover - yaml is present in-repo
        return {}
    try:
        data = yaml.safe_load(fm_block) or {}
    except Exception as exc:
        # NOT silent (T-2591): a swallowed parse error made 13 ADRs' frontmatter
        # (id / status / any structured re_evaluate: key) invisible to the walk
        # for weeks while it reported full coverage — a false safety net. WARN so
        # the drop is observable; the test_adr_frontmatter_all_parse lint is the
        # commit-time guard that keeps it from recurring.
        print(f"[adr_reeval_walk] WARNING: frontmatter of {source} did not parse "
              f"as YAML ({type(exc).__name__}); its keys are INVISIBLE to the walk. "
              f"Quote any title:/status: value containing a colon.", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _adr_id_from(fm: dict, path: Path) -> str:
    val = fm.get("id")
    if isinstance(val, str) and val.strip():
        return val.strip()
    # fall back to the filename prefix, e.g. ORG-062-....md -> ORG-062
    m = re.match(r"([A-Z]{2,}-\d+)", path.stem)
    return m.group(1) if m else path.stem


def classify_structured(entry: dict, adr_id: str) -> Trigger | None:
    """Turn one `re_evaluate:` list entry into a Trigger (or None if malformed)."""
    if not isinstance(entry, dict):
        return None
    rid = str(entry.get("id", "")).strip()
    trigger_id = rid if ":" in rid else f"{adr_id}:{rid}" if rid else ""
    if not trigger_id:
        return None
    ttype = str(entry.get("type", "")).strip().lower()
    when = str(entry.get("when", "")).strip()
    fires = str(entry.get("fires", "")).strip().lower()
    # 'event' with a resolves:T-NNNN condition is machine-checkable -> treat as ticket type.
    if ttype == "event" and _TICKET_EVENT_RE.match(when):
        ttype = "ticket"
    # Determinism gates the action: only deterministic types may auto-file.
    if fires not in ("auto-ticket", "surface"):
        fires = "auto-ticket" if ttype in DETERMINISTIC_TYPES else "surface"
    if fires == "auto-ticket" and ttype not in DETERMINISTIC_TYPES:
        fires = "surface"  # never auto-file a non-deterministic trigger
    return Trigger(
        adr_id=adr_id, trigger_id=trigger_id, ttype=ttype or "event",
        when=when, fires=fires, source="structured",
        text=str(entry.get("text", when) or when),
    )


def parse_adr(path: Path) -> list[Trigger]:
    """Extract every re-eval trigger from one ADR file.

    Structured `re_evaluate:` entries win over a prose bullet with the same
    trigger id (the prose stays as documentation; the structured entry is the
    machine-checkable source of truth).
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    fm_block, body = _split_frontmatter(text)
    fm = _load_yaml(fm_block, source=path.name)
    adr_id = _adr_id_from(fm, path)

    triggers: dict[str, Trigger] = {}

    # 1. Structured frontmatter key (authoritative).
    structured = fm.get("re_evaluate")
    if isinstance(structured, list):
        for entry in structured:
            t = classify_structured(entry, adr_id)
            if t is not None:
                triggers[t.trigger_id] = t

    # 2. Bullets in the `## Re-evaluate when` (/ Revisit / Sunset) section
    #    (surface-only; a structured entry with the same id wins). Two bullet
    #    styles: a bold trigger id (newer ADRs) and a plain bullet (older ADRs).
    #    A section with NO bullets (a schema description, e.g. ORG-010) yields
    #    nothing — deliberately, so meta-prose is not surfaced as a trigger.
    section = _REEVAL_SECTION_RE.search(body)
    if section:
        section_body = section.group("body")
        id_bullet_spans: list[tuple[int, int]] = []
        for m in _ID_BULLET_RE.finditer(section_body):
            id_bullet_spans.append(m.span())
            tid = m.group("tid")
            if tid not in triggers:
                triggers[tid] = Trigger(
                    adr_id=adr_id, trigger_id=tid, ttype="prose",
                    when=m.group("text").strip(), fires="surface", source="prose",
                    text=m.group("text").strip(),
                )
        # Plain bullets that weren't already captured as bold-id bullets.
        seq = 0
        for m in _PLAIN_BULLET_RE.finditer(section_body):
            if any(s <= m.start() < e for s, e in id_bullet_spans):
                continue  # already handled as a bold-id bullet
            seq += 1
            tid = f"{adr_id}:S{seq}"
            triggers[tid] = Trigger(
                adr_id=adr_id, trigger_id=tid, ttype="prose",
                when=m.group("text").strip(), fires="surface", source="prose",
                text=m.group("text").strip(),
            )

    return list(triggers.values())


def scan_adrs(adr_dir: Path = ADR_DIR) -> list[Trigger]:
    triggers: list[Trigger] = []
    for path in sorted(adr_dir.glob("*.md")):
        if path.name.upper().startswith("README"):
            continue
        triggers.extend(parse_adr(path))
    return triggers


# ---------------------------------------------------------------------------
# Policy / SOP docs (T-4556) — surface-only, header-line + section shapes
# ---------------------------------------------------------------------------

_HEADER_LINE_RE = re.compile(r"^\*\*Re-evaluate when:\*\*[ \t]*(?P<text>\S.*?)[ \t]*$", re.MULTILINE)
_FIRST_H2_RE = re.compile(r"^#{2,}\s", re.MULTILINE)
_POINTER_RE = re.compile(r"^see\b", re.IGNORECASE)


def _split_semicolons(text: str) -> list[str]:
    """Split on `;` outside parentheses/backticks; strip a trailing period."""
    parts: list[str] = []
    depth = 0
    in_tick = False
    cur: list[str] = []
    for ch in text:
        if ch == "`":
            in_tick = not in_tick
        elif not in_tick:
            if ch == "(":
                depth += 1
            elif ch == ")" and depth:
                depth -= 1
            elif ch == ";" and depth == 0:
                parts.append("".join(cur))
                cur = []
                continue
        cur.append(ch)
    parts.append("".join(cur))
    return [p.strip().rstrip(".").strip() for p in parts if p.strip().rstrip(".").strip()]


def _governance_label(path: Path, repo_root: Path) -> str:
    rel = path.relative_to(repo_root)
    if rel.parts[:2] == POLICY_DIR_REL.parts:
        return "policy:" + rel.relative_to(POLICY_DIR_REL).with_suffix("").as_posix()
    return "sop:" + rel.relative_to(SOP_DIR_REL).with_suffix("").as_posix()


def parse_governance_doc(path: Path, repo_root: Path = REPO_ROOT) -> list[Trigger]:
    """Extract surface-only re-eval triggers from one policy/SOP doc."""
    text = path.read_text(encoding="utf-8", errors="replace")
    label = _governance_label(path, repo_root)

    def mk(tid_suffix: str, when: str) -> Trigger:
        return Trigger(adr_id=label, trigger_id=f"{label}:{tid_suffix}", ttype="prose",
                       when=when, fires="surface", source="prose", text=when)

    # Section triggers: bullets, else plain non-empty lines.
    section_whens: list[str] = []
    m = _REEVAL_SECTION_RE.search(text)
    if m:
        body = m.group("body")
        bullets = [b.group("text").strip() for b in _PLAIN_BULLET_RE.finditer(body)]
        if bullets:
            section_whens = bullets
        else:
            section_whens = [ln.strip() for ln in body.splitlines()
                             if ln.strip() and not ln.lstrip().startswith("#")]

    h2 = _FIRST_H2_RE.search(text)
    first_h2 = h2.start() if h2 else len(text)
    header_whens: list[str] = []
    for hm in _HEADER_LINE_RE.finditer(text):
        in_meta = hm.start() < first_h2
        if in_meta and section_whens:
            continue  # metadata header is a compressed copy of the section
        raw = hm.group("text")
        if _POINTER_RE.match(raw):
            continue
        header_whens.extend(_split_semicolons(raw))

    out = [mk(f"S{i}", w) for i, w in enumerate(section_whens, 1)]
    out += [mk(f"H{i}", w) for i, w in enumerate(header_whens, 1)]
    return out


def scan_governance_docs(repo_root: Path = REPO_ROOT) -> list[Trigger]:
    triggers: list[Trigger] = []
    for rel in (POLICY_DIR_REL, SOP_DIR_REL):
        root = repo_root / rel
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            if path.name.upper().startswith("README"):
                continue
            triggers.extend(parse_governance_doc(path, repo_root))
    return triggers


# ---------------------------------------------------------------------------
# Deterministic evaluation
# ---------------------------------------------------------------------------

def _parse_iso_date(s: str) -> dt.date | None:
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(0))
    except ValueError:
        return None


def resolved_ticket_ids(tickets_root: Path = TICKETS_ROOT) -> set[str]:
    """Set of ticket ids currently in done/ (for `resolves:T-NNNN` triggers)."""
    done = tickets_root / "done"
    if not done.exists():
        return set()
    return {p.stem for p in done.glob("T-*.md")}


def trigger_fired(
    trigger: Trigger,
    *,
    now: dt.date,
    resolved: set[str],
    metrics: dict[str, float] | None = None,
) -> bool:
    """True iff a DETERMINISTIC trigger's condition is satisfied. Surface-only
    triggers always return False (they are never machine-decided as fired)."""
    if trigger.fires != "auto-ticket" or trigger.ttype not in DETERMINISTIC_TYPES:
        return False
    if trigger.ttype == "calendar":
        d = _parse_iso_date(trigger.when)
        return d is not None and d <= now
    if trigger.ttype == "ticket":
        m = _TICKET_EVENT_RE.match(trigger.when)
        return bool(m) and m.group(1).upper() in {r.upper() for r in resolved}
    if trigger.ttype == "metric":
        m = _METRIC_EXPR_RE.match(trigger.when)
        if not m or metrics is None:
            return False  # metric not instrumented -> surface, do not auto-fire
        name = m.group("name")
        if name not in metrics:
            return False
        val, num, op = metrics[name], float(m.group("num")), m.group("op")
        return {
            ">=": val >= num, "<=": val <= num, ">": val > num,
            "<": val < num, "==": val == num,
        }[op]
    return False


# ---------------------------------------------------------------------------
# Dedup + auto-file (mirrors mirror_drift_sweep.existing_open_refresh_ticket)
# ---------------------------------------------------------------------------

def existing_open_reeval_ticket(
    dedup_key: str, tickets_root: Path = TICKETS_ROOT
) -> str | None:
    """Id of an OPEN (inbox/active) re-eval ticket for `dedup_key`, else None.

    A ticket carrying both the `adr-reeval` label and the `reeval:<key>` label
    already covers this trigger — don't file a duplicate. This is the guard that
    makes a weekly re-run idempotent (the load-bearing correctness point).
    """
    from tools.ticket import TICKET_FILENAME_RE, TicketError, parse_ticket

    key_label = f"reeval:{dedup_key}"
    for state_dir in ("inbox", "active"):
        d = tickets_root / state_dir
        if not d.exists():
            continue
        for path in sorted(d.iterdir()):
            if not (path.is_file() and TICKET_FILENAME_RE.fullmatch(path.name)):
                continue
            try:
                ticket = parse_ticket(path)
            except TicketError:
                continue
            labels = ticket.fields.get("labels") or []
            if isinstance(labels, list) and REEVAL_LABEL in labels and key_label in labels:
                return str(ticket.fields.get("id", path.stem))
    return None


def handled_calendar_reeval_ticket(
    dedup_key: str, trigger_date: dt.date, tickets_root: Path = TICKETS_ROOT
) -> str | None:
    """Id of a DONE re-eval ticket for `dedup_key` created on/after `trigger_date`.

    T-4827: a calendar trigger stays past-due forever, so open-only dedup would
    refile it after every resolve. A review ticket filed on or after the date
    answered this firing; one filed before it answered an earlier one.
    """
    from tools.ticket import TICKET_FILENAME_RE, TicketError, parse_ticket

    key_label = f"reeval:{dedup_key}"
    d = tickets_root / "done"
    if not d.exists():
        return None
    for path in sorted(d.iterdir()):
        if not (path.is_file() and TICKET_FILENAME_RE.fullmatch(path.name)):
            continue
        try:
            ticket = parse_ticket(path)
        except TicketError:
            continue
        labels = ticket.fields.get("labels") or []
        if not (isinstance(labels, list) and REEVAL_LABEL in labels and key_label in labels):
            continue
        created = _parse_iso_date(str(ticket.fields.get("created", "")))
        if created is not None and created >= trigger_date:
            return str(ticket.fields.get("id", path.stem))
    return None


def file_reeval_ticket(
    trigger: Trigger, *, repo_root: Path = REPO_ROOT, push: bool = True
) -> str:
    """Shell out to tools/ticket.py to file a target=role:engineering_orchestrator re-eval ticket."""
    title = f"Re-evaluate {trigger.adr_id} ({trigger.trigger_id}): trigger fired"
    ask = (
        f"The re-evaluation trigger {trigger.trigger_id} on {trigger.adr_id} has "
        f"fired ({trigger.ttype}: {trigger.when}). Re-evaluate the ADR against "
        f"current reality and either reaffirm it (advancing/removing the trigger), "
        f"amend it, or supersede it — do not leave the fired trigger standing.\n\n"
        f"Trigger: {trigger.text}\n"
        f"Auto-filed by tools/adr_reeval_walk.py (T-2527) during the weekly-review walk."
    )
    labels = f"{REEVAL_LABEL},reeval:{trigger.trigger_id},adr,process"
    cmd = [
        sys.executable, str(repo_root / "tools" / "ticket.py"), "file",
        "--automated-filer", "adr_reeval_walk",  # T-5678: ORG-105 gate, non-interactive
        "--requester", "recovery",
        "--target", "role:engineering_orchestrator",
        "--title", title,
        "--labels", labels,
        "--project-id", "null",
        "--nearest-intent-ref", "maintenance:adr-reevaluation",
        "--ask", ask,
        "--serves", "ADR re-eval triggers fire instead of becoming invisible permanent rules (ORG-002)",
    ]
    if not push:
        cmd.append("--no-push")
    res = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise subprocess.CalledProcessError(res.returncode, cmd, output=res.stdout, stderr=res.stderr)
    m = re.search(r"\bfiled (T-\d+)", res.stdout)
    return m.group(1) if m else "T-????"


# ---------------------------------------------------------------------------
# Surface-only watch section
# ---------------------------------------------------------------------------

def _keyword_flag(trigger: Trigger, week_activity: str) -> bool:
    """Heuristic: does the trigger's condition intersect the week's activity?

    Flags a surface trigger whose salient words appear in the week's ticket /
    commit text, so the reviewer's eye goes to the ones plausibly in play.
    """
    if not week_activity:
        return False
    activity = week_activity.lower()
    words = {
        w for w in re.findall(r"[a-z0-9][a-z0-9_-]{3,}", trigger.when.lower())
        if w not in _STOPWORDS
    }
    return any(w in activity for w in words)


_STOPWORDS = frozenset({
    "when", "then", "with", "that", "this", "from", "into", "onto", "your",
    "will", "have", "been", "they", "their", "them", "which", "while", "under",
    "over", "some", "more", "than", "once", "only", "also", "does", "must",
    "first", "second", "third", "built", "fires", "comes", "online",
})


def build_watch_section(surface_triggers: list[Trigger], week_activity: str = "") -> str:
    """Render the 'ADR / policy / SOP re-eval watch' markdown section for the weekly review."""
    if not surface_triggers:
        return (
            "## ADR / policy / SOP re-eval watch\n\n"
            "_No prose/event re-evaluation triggers to surface this week._\n"
        )
    lines = [
        "## ADR / policy / SOP re-eval watch",
        "",
        "Surface-only re-evaluation triggers from ADRs, policies and SOPs (event/prose — not machine-decidable "
        "as fired). A ⚑ marks a trigger whose keywords intersect this week's "
        "activity, i.e. worth a look now. Deterministic triggers (calendar / "
        "resolved-ticket / instrumented-metric) auto-file their own re-eval "
        "tickets and are not listed here.",
        "",
        "| Doc | Trigger | Re-evaluate when | ⚑ |",
        "|---|---|---|---|",
    ]
    for t in sorted(surface_triggers, key=lambda x: x.trigger_id):
        flag = "⚑" if _keyword_flag(t, week_activity) else ""
        when = t.when.replace("|", "\\|")
        lines.append(f"| {t.adr_id} | {t.trigger_id} | {when} | {flag} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class WalkResult:
    watch_section: str
    filed: list[str]              # newly filed re-eval ticket ids
    deduped: list[str]            # trigger ids skipped (open ticket already exists)
    fired: list[str]              # all deterministic trigger ids that fired
    surface_count: int
    owned_status: str = "not-configured"
    owned_due: tuple[str, ...] = ()
    owned_filed: tuple[str, ...] = ()
    owned_existing: tuple[str, ...] = ()
    owned_findings: tuple[str, ...] = ()


def file_owned_review_ticket(
    args: tuple[str, ...], *, repo_root: Path = REPO_ROOT, push: bool = True
) -> str:
    """Execute one typed owned-obligation ticket proposal through ticket.py."""
    cmd = [sys.executable, str(repo_root / "tools" / "ticket.py"), *args]
    if not push:
        cmd.append("--no-push")
    result = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr
        )
    match = re.search(r"\bfiled (T-\d+)", result.stdout)
    return match.group(1) if match else "T-????"


def _owned_watch_section(
    obligations: tuple[grand_program.ReevaluationObligation, ...],
    report: grand_program.EvaluationReport,
) -> str:
    """Render owned metadata without copying normative ADR trigger prose."""
    surfaced = set(report.surfaced_obligation_ids)
    lines = [
        "## Grand Project owned re-evaluation watch",
        "",
        "Owned ORG-053–061 obligations are evaluator surfaces only. Review work is "
        "filed solely from new deterministic evidence; this table is not an architecture "
        "or stage-state mutation.",
        "",
        "| Obligation | Kind | State | Review target |",
        "|---|---|---|---|",
    ]
    for obligation in obligations:
        if obligation.obligation_id not in surfaced:
            continue
        lines.append(
            f"| {obligation.obligation_id} | {obligation.trigger_kind} | "
            f"{obligation.state} | {obligation.review_target} |"
        )
    lines.append("")
    return "\n".join(lines)


def run_walk(
    *,
    now: dt.date,
    week_activity: str = "",
    adr_dir: Path = ADR_DIR,
    governance_root: Path | None = None,
    tickets_root: Path = TICKETS_ROOT,
    metrics: dict[str, float] | None = None,
    metric_evidence_refs: dict[str, str] | None = None,
    dry_run: bool = False,
    push: bool = True,
    stage_index: Path | None = None,
    obligation_index: Path | None = None,
) -> WalkResult:
    """Walk all ADRs: auto-file fired deterministic triggers (deduped), and build
    the surface-only watch section."""
    triggers = scan_adrs(adr_dir)
    # Policy/SOP docs (T-4556): surface-only. Default root is the real repo only
    # when walking the real ADR dir, so fixture-ADR callers stay hermetic.
    if governance_root is None and adr_dir == ADR_DIR:
        governance_root = REPO_ROOT
    if governance_root is not None:
        triggers = triggers + scan_governance_docs(governance_root)
    resolved = resolved_ticket_ids(tickets_root)

    surface = [t for t in triggers if t.fires != "auto-ticket" or t.ttype not in DETERMINISTIC_TYPES]
    deterministic = [t for t in triggers if t not in surface]

    filed: list[str] = []
    deduped: list[str] = []
    fired: list[str] = []

    owned_status = "not-configured"
    owned_due: tuple[str, ...] = ()
    owned_filed: list[str] = []
    owned_existing: tuple[str, ...] = ()
    owned_findings: tuple[str, ...] = ()
    tickets_parent = tickets_root.parent
    stage_path = stage_index or tickets_parent / "tickets" / "grand-project-stage-evidence.yaml"
    obligation_path = obligation_index or tickets_parent / "tickets" / "grand-project-reevaluation-obligations.yaml"
    owned_watch = ""
    if stage_path.is_file() and obligation_path.is_file():
        try:
            stages = grand_program.load_stage_index(stage_path)
            obligations = grand_program.load_obligation_index(obligation_path)
            source = grand_program.scan_source_obligations(adr_dir)
            lint_findings = (
                grand_program.lint_obligation_completeness(source, obligations, tickets_root)
                + grand_program.lint_stage_evidence(stages, tickets_root)
            )
            if lint_findings:
                owned_status = "invalid"
                owned_findings = tuple(
                    f"{finding.code}:{finding.record_id}" for finding in lint_findings
                )
            else:
                evaluation = grand_program.evaluate_indexes(
                    grand_program.ProgramIndexes(stages=stages, obligations=obligations),
                    grand_program.EvaluationInputs(
                        now=dt.datetime.combine(now, dt.time.min, tzinfo=dt.timezone.utc),
                        resolved_ticket_ids=frozenset(resolved),
                        instrumented_metrics=metrics or {},
                        metric_evidence_refs=metric_evidence_refs or {},
                        weekly_activity_ref=f"observable:weekly/{now.isoformat()}",
                    ),
                    tickets_root,
                    max_new=1,
                )
                owned_status = "loaded"
                owned_due = evaluation.due_obligation_ids
                owned_existing = evaluation.existing_ticket_ids
                owned_findings = tuple(
                    f"{finding.code}:{finding.record_id}" for finding in evaluation.findings
                )
                owned_ids = {obligation.obligation_id for obligation in obligations}
                surface = [trigger for trigger in surface if trigger.trigger_id not in owned_ids]
                deterministic = [
                    trigger for trigger in deterministic if trigger.trigger_id not in owned_ids
                ]
                owned_watch = "\n" + _owned_watch_section(obligations, evaluation)
                for args in evaluation.proposed_ticket_args:
                    obligation_id = next(
                        label.split(":", 1)[1]
                        for value, label in zip(args, args[1:])
                        if value == "--labels" and "reeval:" in label
                    ).split(",", 1)[0]
                    if dry_run:
                        owned_filed.append(f"(dry-run) {obligation_id}")
                    else:
                        owned_filed.append(file_owned_review_ticket(args, push=push))
        except (OSError, ValueError, grand_program.ticket.TicketError) as exc:
            owned_status = "invalid"
            owned_findings = (f"index-load:{type(exc).__name__}:{exc}",)
    elif stage_path.exists() or obligation_path.exists():
        owned_status = "incomplete"
        owned_findings = ("index-pair-incomplete",)

    # Legacy behavior remains byte-for-byte for unowned ADRs. When the paired
    # owned indexes are clean, their IDs are removed from this legacy lane so
    # one trigger can never file through both authorities in the same walk.
    for trigger in deterministic:
        if not trigger_fired(trigger, now=now, resolved=resolved, metrics=metrics):
            continue
        fired.append(trigger.trigger_id)
        if existing_open_reeval_ticket(trigger.dedup_key, tickets_root) is not None:
            deduped.append(trigger.trigger_id)
            continue
        if trigger.ttype == "calendar":
            trigger_date = _parse_iso_date(trigger.when)
            if trigger_date is not None and handled_calendar_reeval_ticket(
                trigger.dedup_key, trigger_date, tickets_root
            ) is not None:
                deduped.append(trigger.trigger_id)
                continue
        if dry_run:
            filed.append(f"(dry-run) {trigger.trigger_id}")
            continue
        filed.append(file_reeval_ticket(trigger, push=push))

    # Preserve WalkResult.filed as the existing weekly caller's aggregate
    # newly-filed surface while retaining owned_filed for typed diagnostics.
    filed.extend(owned_filed)

    return WalkResult(
        watch_section=build_watch_section(surface, week_activity) + owned_watch,
        filed=filed, deduped=deduped, fired=fired, surface_count=len(surface),
        owned_status=owned_status,
        owned_due=owned_due,
        owned_filed=tuple(owned_filed),
        owned_existing=owned_existing,
        owned_findings=owned_findings,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true",
                   help="scan + report, but never file a ticket")
    p.add_argument("--no-push", action="store_true",
                   help="file locally but do not push (for testing)")
    p.add_argument("--today", type=dt.date.fromisoformat, default=None,
                   help="override 'now' (YYYY-MM-DD) for calendar evaluation")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = args.today or dt.date.today()
    result = run_walk(now=now, dry_run=args.dry_run, push=not args.no_push)
    print(f"[adr_reeval_walk] scanned ADRs + policies/SOPs; {len(result.fired)} deterministic trigger(s) fired, "
          f"{len(result.filed)} filed, {len(result.deduped)} deduped, "
          f"{result.surface_count} surface-only.")
    print(
        f"[adr_reeval_walk] owned program: {result.owned_status}; "
        f"{len(result.owned_due)} due, {len(result.owned_filed)} filed, "
        f"{len(result.owned_existing)} deduped, {len(result.owned_findings)} finding(s)."
    )
    if result.filed:
        print("  filed: " + ", ".join(result.filed))
    if result.deduped:
        print("  deduped (open ticket exists): " + ", ".join(result.deduped))
    print()
    print(result.watch_section)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
