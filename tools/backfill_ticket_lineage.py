#!/usr/bin/env python3
"""One-shot lineage backfill over ORPHAN done tickets (T-3331, follow-up of T-3328).

An orphan is a ticket that carries none of the lineage edges `ticket.py file`'s
T-3328 guard counts (parent_ticket, depends_on, serves, nearest_intent_ref,
project_id, implements, verifies, supersedes, informs, ratifies) and no
`orphan_reason`.

SCOPE: tickets/done/ only. Open tickets (inbox/active/blocked) belong to their
owners, and any write to them appends a transition-log line that resets
grooming staleness and the inbox consumer's auto_delivered backstop; open
orphans are listed in the report as "left for owners" and never touched.

PRIME RULE: never fabricate lineage; prefer precision over recall. An edge is
written only when the ticket's OWN text cites it in a parent sense:

  Tier A  (edge to an EXISTING ticket with a LOWER id), first hit wins:
          1. title-phrase  a strong phrase (STRONG_PHRASES) names T-X in the title
          2. title         the title cites T-X (any non-excluded mention)
          3. opening-*     a strong phrase names T-X in the FIRST SENTENCE of the
                           body, of `## Ask` or of `## Context`
          4. declared      a sentence anywhere opens with an explicit lineage
                           label ("Parent lineage: T-X", "Follow-up to T-X")
          Excluded mentions (never an edge, anywhere): possessive "T-X's",
          methodology/precedent senses ("discipline from", "-style", "parallel",
          "precedent", "mirrors", "like T-X", "sibling to"), quoted text (quotes,
          backticks, blockquotes), paths, negations. Weak phrases (bare
          from/post/after/during/per) never qualify outside the title.
  Guard   a tier-A candidate T-X whose own parent_ticket / depends_on /
          nearest_intent_ref already points at the child (inverse edge), or whose
          new edge would close a cycle in the combined parent graph (existing
          edges + every edge of this batch), is demoted to tier B.
  Tier B  everything else -> `orphan_reason: <mechanical justification>`.

Edge type (--edge, default parent_ticket): parent_ticket REPLACES the existing
`parent_ticket: null` line in place (ORG-021 requires the key on every ticket);
nearest_intent_ref inserts `nearest_intent_ref: ticket:T-X` at its
FRONT_MATTER_FIELD_ORDER position. `project_id` is NEVER written (ORG-054:
null = explicit root).

Writes are minimal text surgery (one replaced-or-inserted frontmatter line + one
appended transition-log note), verified per ticket by re-parse, canonical
position, exact reversal to the original bytes and the ORG-021 registry check.
The note carries no repo-sha stamp (the published-parent sha would predate this
tool); its provenance is the "lineage backfill (T-3331, <edge>)" text.

One-shot tool, not cron (auto-commit contract does not apply), but it refuses
to run without an explicit mode: --dry-run (report only) or --apply.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import ticket as tk  # noqa: E402
from tools.ticket_purpose import PurposeError, parse_intent_ref  # noqa: E402

TICKET_DIRS = ("inbox", "active", "blocked", "done")  # every dir whose ids exist
BACKFILL_DIRS = ("done",)  # the only dir this tool writes
OPEN_DIRS = ("inbox", "active", "blocked")  # orphans here are left for owners
EDGE_TYPES = ("parent_ticket", "nearest_intent_ref")
DEFAULT_EDGE = "parent_ticket"
# Exactly the field set of ticket.py cmd_file's `has_lineage_edge` (T-3328).
LINEAGE_FIELDS = (
    "parent_ticket", "depends_on", "serves", "nearest_intent_ref", "project_id",
    "implements", "verifies", "supersedes", "informs", "ratifies",
)
TRANSITION_HEADER = tk._TRANSITION_HEADER  # "## Transition log"

# Any textual ticket id (not glued to a preceding word char). Every match of an
# existing lower id counts as a MENTION (so tier B never claims "no reference"
# when there is one); only mentions passing the exclusions can become an edge.
REF_RE = re.compile(r"(?<![A-Za-z0-9_])T-(\d{4,})(?![0-9])")
_TID_RE = re.compile(r"T-\d{4,}")

# ---- exclusions (apply everywhere, title included) --------------------------
# Preceding text that embeds the id in a path or identifier ("tickets/active/
# T-0204.md", "/tmp/echo-T-1377"); "post-T-X" is handled as a weak phrase.
_PATH_BEFORE = re.compile(r"(?:[A-Za-z_.~]/|[A-Za-z0-9]-)$")
_POST_BEFORE = re.compile(r"\bpost-$", re.I)
_PATH_AFTER = re.compile(r"^(?:/(?!T-\d)|\.md\b|\.(?:py|json|txt|ya?ml)\b)")
_POSSESSIVE_AFTER = re.compile(r"^['’]s?\b")
# Analogy / methodology / precedent senses following the id.
_ANALOGY_AFTER = re.compile(
    r"^(?:-(?:style|like|shaped?|era|pattern|class|precedent|parallel)\b"
    r"|[\s,:]+(?:[\w/-]+[\s,]+){0,2}?(?:precedent|pattern|style|class|convention|"
    r"analog\w*|sibling|sister|parallel|twin|template|methodology|playbook|recipe|discipline)\b)",
    re.I)
# Analogy / methodology / precedent senses right before the id.
_ANALOGY_BEFORE = re.compile(
    r"(?:\b(?:like|unlike|similar\s+to|same\s+as|the\s+way|mirror\w*|e\.g\.,?|i\.e\.,?|cf\.?|"
    r"see(?:\s+also)?|such\s+as|including|same\s+(?:class|shape|bug|pattern|kind|family)\s+as|"
    r"analog(?:ue)?\s+(?:of|to)|counterpart\s+(?:of|to)|(?:sibling|sister|twin)(?:s)?(?:\s+(?:of|to|with))?|"
    r"(?:deferred\s+|queued\s+|sequenced\s+|parked\s+)?behind|gated\s+on|blocked\s+(?:on|by)|depends?\s+on|"
    r"waiting\s+(?:on|for)|awaiting|until|unblocks?|blocks|prereq(?:uisite)?\s+(?:of|for)|"
    r"parallel(?:s|ing)?(?:\s+(?:of|to|with))?|precedent(?:s)?(?:\s+(?:of|from|in|set\s+by))?|"
    r"as\s+in|as\s+with|modell?ed\s+on|renumbered(?:\s+from)?)"
    r"|\b(?:discipline|pattern|methodology|approach|lesson|template|recipe|playbook|technique|"
    r"practice|convention|style|shape|model|precedent|rule|idea|trick|framing)s?"
    r"\s+(?:\w+\s+)?(?:from|of|in|set\s+by|established\s+(?:by|in)|used\s+in)"
    r"|\b(?:quoted?|quoting|text|wording|verbatim|excerpt|cited)\s+(?:\w+\s+)?(?:from|of|in))"
    r"\s+(?:the\s+)?$", re.I)
# A negated / contrastive mention right before the id -> never an edge.
_NEGATED_BEFORE = re.compile(
    r"\b(?:not|never|unlike|unrelated\s+to|distinct\s+from|different\s+from|separate\s+from|"
    r"independent\s+of|rather\s+than|instead\s+of|vs\.?|versus|nothing\s+to\s+do\s+with)"
    r"\s+(?:\w+\s+)?(?:the\s+)?$", re.I)

# ---- strong (parent-sense) phrases, matched on the text right before the id --
STRONG_PHRASES: tuple[tuple[str, re.Pattern], ...] = (
    ("follow-up", re.compile(r"\bfollow[- ]?(?:ups?|on)\s+(?:from|to|of|on|for)\s+(?:the\s+)?(?:ticket\s+)?$", re.I)),
    ("filed-from", re.compile(r"\bfiled\s+(?:from|during|out\s+of)\s+(?:the\s+)?$", re.I)),
    ("split-from", re.compile(r"\b(?:split|carved|spun|broken)[- ](?:out|off)?\s*(?:from|of)\s+(?:the\s+)?$", re.I)),
    ("child-of", re.compile(r"\bchild\s+(?:ticket\s+)?of\s+(?:the\s+)?$", re.I)),
    # prose "Parent: T-X" / "parent lineage: T-X" only -- never the frontmatter
    # literal `parent_ticket: T-X` or the flag `--parent-ticket T-X`.
    ("parent", re.compile(r"(?<![-_])\bparent(?:[ ](?:ticket|lineage|arc))?\s*(?:[:(]|\bis\b)?\s*$", re.I)),
    ("surfaced-in", re.compile(r"\b(?:surfaced|found|discovered|spotted|caught|exposed|uncovered)"
                               r"\s+(?:\w+\s+)?(?:in|by|during|from|via|on|(?:while\s+)?(?:resolving|verifying|staging|reviewing|working\s+on))\s+(?:the\s+)?$", re.I)),
    ("while-working", re.compile(r"\bwhile\s+(?:diagnosing|investigating|working(?:\s+on)?|debugging|"
                                 r"implementing|reviewing|verifying|fixing|landing|smoking|triaging)"
                                 r"\s+(?:the\s+)?$", re.I)),
    ("deferred-from", re.compile(r"\b(?:deferred|originat\w*|spawned|descoped|punted)\s+(?:from|of|in|by)\s+(?:the\s+)?$", re.I)),
    ("prompted-by", re.compile(r"\b(?:prompted|triggered|motivated)\s+by\s+(?:\w+\s+)?(?:the\s+)?$", re.I)),
    ("closes", re.compile(r"\bcloses\s+(?:out\s+)?$", re.I)),
    ("successor-to", re.compile(r"\bsuccessor\s+(?:to|of)\s+(?:the\s+)?$", re.I)),
    ("captured-from", re.compile(r"\b(?:captured|flagged|carried|lifted|raised)\s+(?:\w+\s+)?(?:from|in|during|by)\s+(?:the\s+)?$", re.I)),
)
# "from the T-X cycle/arc/review" (also "in/during the", "(T-X thread)").
_ARC_BEFORE = re.compile(r"(?:\b(?:from|during|in|of)\s+(?:the\s+)?|\(\s*)$", re.I)
_ARC_AFTER = re.compile(r"^\s+(?:cycle|arc|review|thread|session|incident|investigation|diagnosis|audit)\b", re.I)
# "T-X follow-up" (title or opening sentence only, like every strong phrase).
_FOLLOWUP_AFTER = re.compile(r"^\W{0,2}\s*follow[- ]?(?:ups?|on)\b", re.I)
# Explicit lineage label opening a sentence anywhere ("Parent lineage: T-X.").
_DECLARED_LABEL = re.compile(
    r"(?:^|(?<=[.!?;])\s+|\n)[ \t]*(?:[-*>]\s+)?(?:\*\*)?"
    r"(?:parent(?:\s+(?:ticket|lineage))?|child\s+of|follow[- ]?up\s+(?:of|to|from)|"
    r"spun\s+out\s+of|split\s+from|carved\s+out\s+of)(?:\*\*)?\s*:?\s*(?:\*\*)?\s*(?:the\s+)?$", re.I)

# Quoted regions: refs inside are quoted text, never citations.
_QUOTE_RES = (
    re.compile(r'"[^"\n]{0,600}"'),
    re.compile("“[^”\n]{0,600}”"),
    re.compile("‘[^’\n]{0,600}’(?![A-Za-z])"),
    re.compile(r"(?<![\w])'[^'\n]{1,600}'(?![\w])"),
    re.compile(r"`[^`\n]{0,600}`"),
    re.compile(r"^[ \t]*>[^\n]*", re.M),
)

# Sections whose first sentence is an "opening" (besides the body's first).
_SECTION_FIRST_LINE = re.compile(r"^## (Ask|Context)[ \t]*\n(?:[ \t]*\n)*([^\n]*)", re.M)
_SENTENCE_END = re.compile(r"(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\bcf)(?<!\betc)[.!?](?=\s+[A-Z(*\[`\"'“]|\s*$)")

# Auto-generated regions excluded from the scan.
_BRIEF_MARKER = re.compile(r"^--- BRIEF\b", re.M)  # pasted dispatch brief (s3star runner)
_AUTOGEN_REGION = re.compile(r"AUTOGEN-START.*?AUTOGEN-END", re.S)
_RUNNER_SECTION = re.compile(
    r"^##+ [^\n]*(?:runner-narrated|auto-generated)[^\n]*\n.*?(?=^##+ |\Z)", re.S | re.M | re.I
)

# Tier B (orphan_reason) text, mechanical. Guard clause by `created` vs the
# T-3328 guard landing (76a86a4b31, PR #381 merged 2026-08-28T15:05:24Z).
GUARD_LANDED = "2026-08-28T15:05:24Z"
_REASON_PRE = "historical root filed before the T-3328 file-time guard"
_REASON_POST = "filed without a lineage edge despite the T-3328 file-time guard"
_REASON_NONE = "T-3331 backfill found no earlier-ticket reference in its title or body"
_REASON_INCIDENTAL = (
    "T-3331 backfill found only incidental earlier-ticket mentions (none in a "
    "parent-like phrase, the title or the opening text), so inferred no edge"
)
_REASON_GUARD = (
    "T-3331 backfill found only a candidate parent that already points back at "
    "this ticket (inverse or cyclic edge), so inferred no edge"
)
DETAIL_B = {
    "none": "lineage backfill (T-3331): orphan_reason set (no earlier-ticket reference)",
    "incidental": "lineage backfill (T-3331): orphan_reason set (only incidental earlier-ticket mentions)",
    "guard-inverse": "lineage backfill (T-3331): orphan_reason set (candidate parent points back: inverse edge)",
    "guard-cycle": "lineage backfill (T-3331): orphan_reason set (candidate parent would close a cycle)",
}


def orphan_reason_text(created: str, kind: str) -> str:
    head = _REASON_POST if created > GUARD_LANDED else _REASON_PRE
    tail = {"none": _REASON_NONE, "incidental": _REASON_INCIDENTAL}.get(kind, _REASON_GUARD)
    return f"{head}; {tail}"


SPAN_MAX = 60


class BackfillError(Exception):
    pass


@dataclass
class Decision:
    tid: str
    path: Path
    state_dir: str
    tier: str  # "A" | "B"
    rule: str  # tier A rule | tier B "none" / "incidental" / "guard-inverse" / "guard-cycle"
    target: str | None = None
    span: str | None = None
    field_name: str = ""
    value: str = ""
    detail: str = ""
    guard_target: str | None = None  # tier B guard hits: the demoted candidate
    created: str = ""


@dataclass
class ScanResult:
    total: int = 0
    edge: str = DEFAULT_EDGE
    unparseable: list = field(default_factory=list)  # [(relpath, error)]
    skipped_has_edge: int = 0
    skipped_orphan_reason: int = 0
    skipped_other: list = field(default_factory=list)  # [(tid, reason)]
    left_for_owners: list = field(default_factory=list)  # [Decision] open orphans, never written
    decisions: list = field(default_factory=list)


def _tid_num(tid: str) -> int:
    return int(tid.split("-", 1)[1])


def is_orphan(fields: dict) -> bool:
    if any(fields.get(k) not in (None, "", [], "null") for k in LINEAGE_FIELDS):
        return False
    return not fields.get("orphan_reason")


def scannable_text(body: str) -> str:
    """Title + body up to `## Transition log`, minus auto-generated regions."""
    lines = body.split("\n")
    cut = len(lines)
    for i, ln in enumerate(lines):
        if ln.rstrip() == TRANSITION_HEADER:
            cut = i
            break
    text = "\n".join(lines[:cut])
    m = _BRIEF_MARKER.search(text)
    if m:
        text = text[: m.start()]
    text = _AUTOGEN_REGION.sub("", text)
    text = _RUNNER_SECTION.sub("", text)
    return text


def _span(text: str, start: int, end: int) -> str:
    """<=SPAN_MAX-char quote containing text[start:end], clipped to the citing
    line, whitespace collapsed, `'` replaced by U+2019 so the quote delimits."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    lo = max(line_start, start - 30)
    hi = min(line_end, end + 30)
    raw = " ".join(text[lo:hi].split())
    ref = " ".join(text[start:end].split())
    pos = raw.find(ref)
    if len(raw) <= SPAN_MAX:
        out = raw
    else:
        before = max(0, pos - (SPAN_MAX - len(ref)) // 2)
        out = raw[before : before + SPAN_MAX]
        if ref not in out:  # defensive; ref is always <= SPAN_MAX
            out = raw[pos : pos + SPAN_MAX]
    return out.replace("'", "’").strip()


def _first_sentence(text: str, a: int, b: int) -> tuple[int, int]:
    m = _SENTENCE_END.search(text, a, b)
    return (a, m.end() if m else b)


def _opening_spans(text: str, title_end: int) -> list[tuple[str, int, int]]:
    """(label, start, end) of the first sentence of the body and of the first
    line of `## Ask` / `## Context`."""
    out: list[tuple[str, int, int]] = []
    for m in re.finditer(r"[^\n]*", text[title_end:]):
        ln = m.group(0)
        if ln.strip() and not ln.lstrip().startswith("#"):
            a = title_end + m.start()
            out.append(("opening-body", *_first_sentence(text, a, title_end + m.end())))
            break
    for sm in _SECTION_FIRST_LINE.finditer(text):
        if sm.group(2).strip() and not sm.group(2).lstrip().startswith("#"):
            label = "opening-" + sm.group(1).lower()
            out.append((label, *_first_sentence(text, sm.start(2), sm.end(2))))
    return out


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for rx in _QUOTE_RES:
        spans += [(m.start(), m.end()) for m in rx.finditer(text)]
    return spans


def _excluded(text: str, m: re.Match, quoted: list[tuple[int, int]]) -> bool:
    """True for a mention that can never be an edge (path, possessive,
    analogy/methodology/precedent, quoted, negated)."""
    s, e = m.start(), m.end()
    ctx = text[max(0, s - 6) : s]
    if _PATH_BEFORE.search(ctx) and not _POST_BEFORE.search(ctx):
        return True
    after = text[e : e + 40]
    if _PATH_AFTER.match(after) or _POSSESSIVE_AFTER.match(after) or _ANALOGY_AFTER.match(after):
        return True
    pre = text[max(0, s - 60) : s]
    if _NEGATED_BEFORE.search(pre) or _ANALOGY_BEFORE.search(pre):
        return True
    return any(a < s and e <= b for a, b in quoted)


def _strong(text: str, m: re.Match) -> tuple[str, int] | None:
    """(phrase label, span start) when a strong phrase names this mention."""
    s, e = m.start(), m.end()
    pre = text[max(0, s - 60) : s]
    for label, pat in STRONG_PHRASES:
        pm = pat.search(pre)
        if pm:
            return label, s - (len(pre) - pm.start())
    am = _ARC_BEFORE.search(pre)
    if am and _ARC_AFTER.match(text[e : e + 20]):
        return "from-arc", s - (len(pre) - am.start())
    if _FOLLOWUP_AFTER.match(text[e : e + 20]):
        return "ref-follow-up", s
    return None


def choose_reference(tid: str, body: str, existing: set[str]):
    """Return (target, rule, span), or (None, "incidental"|"none", None). Pure.

    Precedence (first hit wins): a strong phrase in the title; any non-excluded
    title mention; a strong phrase in an opening sentence (body / Ask /
    Context), text order; an explicit sentence-opening lineage label anywhere.
    """
    text = scannable_text(body)
    own = _tid_num(tid)
    title = re.search(r"^# [^\n]*", text, re.M)
    t_start, t_end = (title.start(), title.end()) if title else (0, 0)
    openings = _opening_spans(text, t_end if title else 0)
    quoted = _quoted_spans(text)
    saw_mention = False
    title_strong = title_plain = opening = declared = None
    for m in REF_RE.finditer(text):
        ref = f"T-{m.group(1)}"
        if int(m.group(1)) >= own or ref not in existing:
            continue  # self, higher id, or non-existent
        saw_mention = True
        if _excluded(text, m, quoted):
            continue
        in_title = title is not None and t_start <= m.start() < t_end
        if in_title:
            st = _strong(text, m)
            if st and title_strong is None:
                title_strong = (ref, "title-phrase", _span(text, st[1], m.end()))
            if title_plain is None:
                title_plain = (ref, "title", _span(text, m.start(), m.end()))
            continue
        if opening is None:
            for label, a, b in openings:
                if a <= m.start() < b:
                    st = _strong(text, m)
                    if st:
                        opening = (ref, label, _span(text, st[1], m.end()))
                    break
        if declared is None:
            pre = text[max(0, m.start() - 40) : m.start()]
            dm = _DECLARED_LABEL.search(pre)
            if dm:
                declared = (ref, "declared", _span(text, m.start() - (len(pre) - dm.start()), m.end()))
    for hit in (title_strong, title_plain, opening, declared):
        if hit is not None:
            return hit
    return (None, "incidental" if saw_mention else "none", None)


def _existing_ids(tickets_root: Path) -> set[str]:
    ids: set[str] = set()
    for d in TICKET_DIRS:
        for p in (tickets_root / d).glob("T-*.md"):
            ids.add(p.stem)
    return ids


def _ids_in(value) -> list[str]:
    if value in (None, "", "null"):
        return []
    vals = value if isinstance(value, list) else [value]
    out = []
    for v in vals:
        out += _TID_RE.findall(str(v))
    return out


class LineageGraph:
    """Existing parent-like edges (child -> parents) plus this batch's new
    edges; answers "does T-X already point at the child" and "would child -> T-X
    close a cycle"."""

    def __init__(self) -> None:
        self.parents: dict[str, set[str]] = {}
        self.backrefs: dict[str, set[str]] = {}  # tid -> ids its parent/deps/intent name

    def add_ticket(self, tid: str, fields: dict, edge: str) -> None:
        pt = _ids_in(fields.get("parent_ticket"))[:1]
        nir = fields.get("nearest_intent_ref")
        nir_ids = _TID_RE.findall(str(nir)) if str(nir or "").startswith("ticket:") else []
        self.backrefs[tid] = set(pt) | set(_ids_in(fields.get("depends_on"))) | set(nir_ids)
        graph_ids = set(pt) | (set(nir_ids) if edge == "nearest_intent_ref" else set())
        if graph_ids:
            self.parents.setdefault(tid, set()).update(graph_ids)

    def check(self, child: str, target: str) -> str | None:
        """None if child -> target is safe, else "guard-inverse"/"guard-cycle"."""
        if child in self.backrefs.get(target, set()):
            return "guard-inverse"
        seen, stack = set(), [target]
        while stack:
            cur = stack.pop()
            if cur == child:
                return "guard-cycle"
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self.parents.get(cur, ()))
        return None

    def add_edge(self, child: str, target: str) -> None:
        self.parents.setdefault(child, set()).add(target)


def _edge_value(edge: str, target: str) -> str:
    return target if edge == "parent_ticket" else f"ticket:{target}"


def scan(tickets_root: Path, edge: str = DEFAULT_EDGE) -> ScanResult:
    if edge not in EDGE_TYPES:
        raise BackfillError(f"unknown edge type {edge!r}")
    res = ScanResult(edge=edge)
    existing = _existing_ids(tickets_root)
    graph = LineageGraph()
    candidates = []  # (tid, path, dir, fields, body)
    # Non-ticket files under tickets/ (e.g. plans/) are reported unparseable,
    # never touched.
    for p in sorted(tickets_root.glob("*/T-*.md")):
        res.total += 1
        try:
            fields, body = tk.parse_front_matter(p.read_text(encoding="utf-8"))
        except Exception as exc:  # report-only; any parse failure = skip
            res.unparseable.append((str(p.relative_to(tickets_root.parent)), f"{type(exc).__name__}: {exc}"))
            continue
        if p.parent.name not in TICKET_DIRS:
            res.skipped_other.append((p.stem, f"not in {TICKET_DIRS}"))
            continue
        graph.add_ticket(str(fields.get("id") or p.stem), fields, edge)
        if not is_orphan(fields):
            if fields.get("orphan_reason"):
                res.skipped_orphan_reason += 1
            else:
                res.skipped_has_edge += 1
            continue
        tid = str(fields.get("id") or p.stem)
        if tid != p.stem or not re.fullmatch(r"T-\d{4,}", tid):
            res.skipped_other.append((p.stem, f"id/filename mismatch ({tid!r})"))
            continue
        if TRANSITION_HEADER not in body:
            res.skipped_other.append((tid, "no transition-log header"))
            continue
        candidates.append((tid, p, p.parent.name, fields, body))
    # Decide in id order so the batch-wide cycle guard is deterministic.
    for tid, p, d, fields, body in sorted(candidates, key=lambda c: _tid_num(c[0])):
        created = str(fields.get("created") or "")
        target, rule, span = choose_reference(tid, body, existing)
        guard = None
        if target is not None:
            guard = graph.check(tid, target)
        if target is not None and guard is None:
            value = _edge_value(edge, target)
            if edge == "nearest_intent_ref":
                try:
                    parse_intent_ref(value)  # ticket.py's own nearest_intent_ref validator
                except PurposeError as exc:
                    res.skipped_other.append((tid, f"invalid intent ref {value}: {exc}"))
                    continue
            dec = Decision(
                tid=tid, path=p, state_dir=d, tier="A", rule=rule, target=target, span=span,
                field_name=edge, value=value, created=created,
                detail=f"lineage backfill (T-3331, {edge}): {edge} {target} from '{span}'",
            )
            if d in BACKFILL_DIRS:
                graph.add_edge(tid, target)  # only written edges join the graph
        else:
            kind = guard or rule
            dec = Decision(
                tid=tid, path=p, state_dir=d, tier="B", rule=kind, field_name="orphan_reason",
                value=orphan_reason_text(created, kind), detail=DETAIL_B[kind],
                guard_target=target if guard else None, created=created,
            )
        (res.decisions if d in BACKFILL_DIRS else res.left_for_owners).append(dec)
    return res


def _frontmatter_bounds(lines: list[str]) -> int:
    if lines[0] != "---":
        raise BackfillError("missing opening ---")
    close = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
    if close is None:
        raise BackfillError("missing closing ---")
    return close


def _insert_frontmatter_line(text: str, key: str, value: str) -> tuple[str, int]:
    """Insert `key: value` at the canonical FRONT_MATTER_FIELD_ORDER position.

    Returns (new_text, inserted_line_index). Text surgery only: the new line
    goes immediately before the first existing KNOWN key that sorts after
    `key`, else just before the closing `---`.
    """
    order = tk.FRONT_MATTER_FIELD_ORDER
    k_idx = order.index(key)
    lines = text.split("\n")
    close = _frontmatter_bounds(lines)
    at = close
    for i in range(1, close):
        m = re.match(r"^([a-z_][a-z0-9_]*):", lines[i])
        if not m:
            continue
        if m.group(1) == key:
            raise BackfillError(f"{key} already present")
        if m.group(1) in order and order.index(m.group(1)) > k_idx:
            at = i
            break
    lines.insert(at, f"{key}: {value}")
    return "\n".join(lines), at


def _replace_null_line(text: str, key: str, value: str) -> tuple[str, int] | None:
    """Replace the frontmatter line `key: null` in place; None if absent.
    Refuses a non-null existing value (never overwrite an edge)."""
    lines = text.split("\n")
    close = _frontmatter_bounds(lines)
    for i in range(1, close):
        if re.match(rf"^{re.escape(key)}:", lines[i]):
            if lines[i] != f"{key}: null":
                raise BackfillError(f"{key} already set: {lines[i]!r}")
            lines[i] = f"{key}: {value}"
            return "\n".join(lines), i
    return None


def render_edit(text: str, d: Decision, entry: str) -> str:
    """Pure: the new file text for decision `d`, verified byte-preserving."""
    old_fields, old_body = tk.parse_front_matter(text)
    if not text.endswith("\n"):
        raise BackfillError("file does not end with newline")
    replaced = None
    if d.field_name == "parent_ticket":
        replaced = _replace_null_line(text, d.field_name, d.value)
    if replaced is not None:
        new_text, at = replaced
        mode = "replace"
    else:
        new_text, at = _insert_frontmatter_line(text, d.field_name, d.value)
        mode = "insert"
    new_text = new_text + entry + "\n"
    # Verify 1: ticket.py's parser sees exactly old fields + the one new value.
    new_fields, new_body = tk.parse_front_matter(new_text)
    expect = dict(old_fields)
    expect[d.field_name] = d.value
    if new_fields != expect:
        raise BackfillError("re-parse mismatch")
    # Verify 2: canonical position = where serialize_front_matter would put it
    # (checked whenever the original known keys were already in canonical order).
    def _canon(keys):
        known = [k for k in keys if k in tk.FRONT_MATTER_FIELD_ORDER]
        return known == sorted(known, key=tk.FRONT_MATTER_FIELD_ORDER.index)
    if _canon(old_fields) and not _canon(new_fields):
        raise BackfillError("edit broke canonical order")
    # Verify 3: undoing the one frontmatter line + the appended entry
    # reproduces the original exactly.
    lines = new_text.split("\n")
    if mode == "replace":
        lines[at] = f"{d.field_name}: null"
    else:
        del lines[at]
    reverted = "\n".join(lines)
    if not reverted.endswith(entry + "\n") or reverted[: -len(entry + "\n")] != text:
        raise BackfillError("byte-preservation check failed")
    if new_body != old_body + entry + "\n":
        raise BackfillError("body delta is not exactly one appended entry")
    # ORG-021 registry check, same choke point every ticket writer uses.
    tk._check_frontmatter_registry(new_fields, tk._fm_snapshot(old_fields))
    return new_text


def format_entry(ts: str, actor: str, detail: str) -> str:
    """ticket.py's transition-note shape (`- ts [actor] note; detail`) WITHOUT
    the published-parent sha stamp: that sha would predate this tool's own
    commit, so provenance is carried by the T-3331 text instead."""
    return f"- {ts} [{actor}] note; {detail}"


def apply(res: ScanResult, ts: str, actor: str) -> list[str]:
    """Render EVERY edit first (any verification failure aborts before a single
    write), then write. Only res.decisions (done/) are ever written."""
    rendered = []
    for d in res.decisions:
        if d.state_dir not in BACKFILL_DIRS:  # defensive: open tickets are never written
            raise BackfillError(f"{d.tid}: refusing to write a {d.state_dir} ticket")
        text = d.path.read_text(encoding="utf-8")
        rendered.append((d, render_edit(text, d, format_entry(ts, actor, d.detail))))
    for d, new_text in rendered:
        d.path.write_text(new_text, encoding="utf-8")
    return [d.tid for d, _ in rendered]


def _row(d: Decision) -> dict:
    return {"tid": d.tid, "dir": d.state_dir, "tier": d.tier, "rule": d.rule,
            "target": d.target, "span": d.span, "field": d.field_name, "value": d.value,
            "guard_target": d.guard_target}


def build_report(res: ScanResult, sample_n: int = 40, seed: int = 3331) -> dict:
    a = [d for d in res.decisions if d.tier == "A"]
    b = [d for d in res.decisions if d.tier == "B"]
    count = lambda ds: dict(sorted({r: sum(1 for d in ds if d.rule == r) for r in {d.rule for d in ds}}.items()))
    rng = random.Random(seed)
    sample = sorted(rng.sample(a, min(sample_n, len(a))), key=lambda d: _tid_num(d.tid))
    return {
        "ticket": "T-3331",
        "edge": res.edge,
        "total_files": res.total,
        "unparseable": [{"path": p, "error": e} for p, e in res.unparseable],
        "skipped_has_edge": res.skipped_has_edge,
        "skipped_orphan_reason": res.skipped_orphan_reason,
        "skipped_other": [{"tid": t, "reason": r} for t, r in res.skipped_other],
        "orphans": len(res.decisions),
        "tier_a": len(a),
        "tier_a_by_rule": count(a),
        "tier_b": len(b),
        "tier_b_by_rule": count(b),
        "guard_hits": [_row(d) for d in b if d.rule.startswith("guard-")],
        "left_for_owners": [_row(d) for d in res.left_for_owners],
        "precedence": ("title-phrase > title > opening-{body,ask,context} strong phrase > declared "
                       "label; strong phrases: " + ", ".join(l for l, _ in STRONG_PHRASES)
                       + ", from-arc, ref-follow-up; excluded: path, possessive, analogy/"
                       "methodology/precedent, quoted, negated; inverse/cycle guard -> tier B"),
        "tier_a_sample_seed": seed,
        "tier_a_sample": [_row(d) for d in sample],
        "all": [_row(d) for d in res.decisions],
    }


def human_summary(rep: dict) -> str:
    out = [
        f"T-3331 lineage backfill (edge={rep['edge']}): {rep['total_files']} files, "
        f"{len(rep['unparseable'])} unparseable, {rep['orphans']} done orphans -> "
        f"tier A {rep['tier_a']}, tier B {rep['tier_b']}; {len(rep['left_for_owners'])} open orphans left for owners",
        f"tier A by rule: {rep['tier_a_by_rule']}",
        f"tier B by rule: {rep['tier_b_by_rule']}",
        f"skipped (other): {len(rep['skipped_other'])}",
        "", "guard hits (demoted to tier B):",
    ]
    out += [f"  {r['tid']} -x-> {r['guard_target']} [{r['rule']}]" for r in rep["guard_hits"]]
    out += ["", "open orphans left for owners (not written):"]
    out += [f"  {r['tid']} [{r['dir']}] would-be {r['tier']} {r['rule']} {r['target'] or ''}"
            for r in rep["left_for_owners"]]
    out += ["", f"tier A sample (seed {rep['tier_a_sample_seed']}):"]
    out += [f"  {r['tid']} -> {r['target']} [{r['rule']}] '{r['span']}'" for r in rep["tier_a_sample"]]
    out += ["", "unparseable:"] + [f"  {u['path']}: {u['error']}" for u in rep["unparseable"]]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="scan + write report; no ticket writes")
    mode.add_argument("--apply", action="store_true", help="write the edits into done/ ticket files (no git)")
    p.add_argument("--edge", choices=EDGE_TYPES, default=DEFAULT_EDGE,
                   help="tier-A edge field (default parent_ticket: replaces `parent_ticket: null` in place)")
    p.add_argument("--tickets-root", type=Path, default=_REPO_ROOT / "tickets")
    p.add_argument("--report", type=Path, default=None, help="report path (JSON; .txt summary alongside)")
    p.add_argument("--actor", default=None, help="transition-log actor (default: ticket.py _actor())")
    p.add_argument("--timestamp", default=None, help="transition-log ts (default: now, UTC)")
    p.add_argument("--seed", type=int, default=3331, help="tier-A sample seed")
    args = p.parse_args(argv)
    if not (args.dry_run or args.apply):
        print("ERROR: refusing to run without an explicit mode: pass --dry-run or --apply", file=sys.stderr)
        return 64
    if args.dry_run and args.report is None:
        print("ERROR: --dry-run requires --report <path>", file=sys.stderr)
        return 64
    res = scan(args.tickets_root, edge=args.edge)
    rep = build_report(res, seed=args.seed)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(rep, indent=1) + "\n", encoding="utf-8")
        args.report.with_suffix(".txt").write_text(human_summary(rep), encoding="utf-8")
    if args.apply:
        ts = args.timestamp or tk._now_iso()
        actor = args.actor or tk._actor()
        changed = apply(res, ts, actor)
        print(f"applied: {len(changed)} done tickets (A {rep['tier_a']}, B {rep['tier_b']}); "
              f"{len(rep['left_for_owners'])} open orphans left for owners")
    else:
        print(human_summary(rep).split("\n\n")[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
