#!/usr/bin/env python3
"""Lessons → hooks promotion pipeline — detector (T-0649), increment 2.

Design: `notes/2026-06-09-lessons-to-hooks-promotion-pipeline.md`.

Pantheon files lessons (`lessons/`), bug gotchas (`bugs/`) and feedback
memories (`~/.claude/.../memory/feedback_*.md`) as SOFT enforcement — they
preserve the *why*, but findings rarely get promoted into *committed*
deterministic guards (`ticket_lint`, the HERMES byte-guard, …) which are all
hand-built one-offs. The operator wants the promotion AUTOMATED so hardening
compounds (Telegram 3015).

It scans the three corpora, applies the high-precision promotability criteria
from the design note, and drafts a candidate guard sketch per survivor.

- **inc 2 (default): report-only.** Print the candidates; file nothing.
- **inc 3b (`--file`): file to the review surface.** Write each unledgered
  candidate as a markdown file under `promotion-candidates/` and record it in
  the dedup ledger. This is the recovery-GATE's filing half: it still NEVER commits
  a hook, files a ticket, touches git, or sends Telegram. The surface is a
  dedicated directory — NOT the ticket queue — because a bare target=recovery ticket
  is auto-fired by the inbox-consumer, defeating the human review gate; a
  directory structurally cannot auto-fire. Recovery batch-reviews it (the inc 4
  weekly cron commits the surface + digests the pending files), then
  ACCEPT→files a landing ticket / DECLINE→records the fingerprint declined.

Promotability — a candidate qualifies IFF (per design §"The P1 core"):
  1. Deterministic + mechanically checkable (a pure function over
     bytes/AST/path/git-state could decide it), AND
  2. Recurrence (≥2 occurrences / explicit "twice"/"recurred") OR explicit
     irreversibility (data loss, live-bridge kill, force-push, …), AND
  3. NOT a soft/contextual judgment (when-to-defer, autonomy, "verify before
     asserting" — these stay soft; a hook would be a false-precision tripwire).
An explicit self-tag ("this should be a pre-commit/hook/lint check") is the
strongest signal and qualifies on its own (still subject to the soft-veto).

High **precision over recall**: when in doubt, do not emit — a later sweep
re-sees a missed lesson, but a noisy gate gets ignored.

CLI:
    python3 tools/promote_lessons.py                  # human report, all corpora
    python3 tools/promote_lessons.py --json           # machine-readable
    python3 tools/promote_lessons.py --min-confidence high
    python3 tools/promote_lessons.py --lessons-dir ... --bugs-dir ... \
        --memory-dir ...                              # path overrides (tests)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LESSONS_DIR = REPO_ROOT / "lessons"
DEFAULT_BUGS_DIR = REPO_ROOT / "bugs"
DEFAULT_MEMORY_DIR = Path(
    "/home/claude-fallback/.claude/projects/-home-claude-fallback/memory"
)
# Recovery's promotion-candidate review surface (inc 3b). A dedicated directory —
# NOT the ticket queue — because a bare target=recovery ticket is auto-fired by the
# inbox-consumer, which would defeat the human review gate. This surface
# structurally cannot auto-fire; recovery batch-reviews it (digest from inc 4).
DEFAULT_CANDIDATES_DIR = REPO_ROOT / "promotion-candidates"

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

# --- Detection vocabularies -------------------------------------------------
# Kept deliberately small + auditable; precision lives here. Tune against the
# real corpus rather than growing these unboundedly.

# (1) Explicit self-tag — the entry literally asks to be promoted to a guard.
SELF_TAG_RE = re.compile(
    r"\b("
    r"pre-?commit"
    r"|lint(?:er|-rule)?"
    r"|\bhook\b"
    r"|promote .{0,30}\b(guard|check|hook|lint)"
    r"|(should|could) be a .{0,30}\b(guard|check|hook|lint|rule)"
    r"|mechanically[- ]check"
    r"|deterministic guard"
    r"|ticket_lint"
    r"|byte-?sequence guard"
    r")",
    re.IGNORECASE,
)

# (2a) Mechanical modality — an imperative invariant.
MODALITY_RE = re.compile(
    r"\b(never|must(?: not)?|always|don'?t|do not|cannot|can'?t|require[ds]?|"
    r"forbid(?:den)?|disallow(?:ed)?|reject)\b",
    re.IGNORECASE,
)

# (2b) Mechanical nouns — STRUCTURAL tokens a pure function can inspect.
# Deliberately tight: generic words (path/field/branch/import/json) appear in
# soft process lessons too and tank precision. A concrete quoted code token
# (see INLINE_CODE_RE) is the preferred mechanical signal; this list is the
# fallback for the unmistakably-structural words.
MECHANICAL_NOUN_RE = re.compile(
    r"\b(backtick|byte-?sequence|byte sequence|glob expansion|frontmatter|"
    r"front-matter|regex|syntax|file extension|filename|"
    r"trailing whitespace|force-?push|substitution|double-?quoted|"
    r"shebang|exit code|non-?zero)\b",
    re.IGNORECASE,
)

# A concrete quoted code token (`...`) containing an operator/flag/path/colon —
# real mechanical rules quote the exact thing they constrain; soft lessons don't.
INLINE_CODE_RE = re.compile(r"`[^`\n]*[/_.:=\-][^`\n]*`")

# (3) Recurrence markers — LEXICAL only. (Ticket-ref counting was dropped: most
# memories cite ≥2 tickets without being about a recurring mechanical bug.)
RECURRENCE_RE = re.compile(
    r"\b(twice|two times|recurr?ed|recurr?ence|3x|\dx|"
    r"multiple times|bit (?:me )?(?:twice|again)|"
    r"both times|hit (?:it )?(?:twice|again)|re-?hit)\b",
    re.IGNORECASE,
)

# (4) Irreversibility markers — never-want-to-hit-even-once.
IRREVERSIBLE_RE = re.compile(
    r"\b(data ?loss|lose data|destroy(?:ed|s)?|force-?push|"
    r"kill(?:s|ed)? the (?:bridge|session|bot|poller)|bridge-?kill|"
    r"overwrit(?:e|ten)|irrecoverab|irreversib|nukes?|wiped?|"
    r"deleted? .{0,20}(branch|history|worktree)|landmine|outage)\b",
    re.IGNORECASE,
)

# (3-veto) Soft/contextual judgment — these stay soft; presence vetoes.
SOFT_VETO_RE = re.compile(
    r"\b(when to defer|how to weigh|autonomy|judgment|judgement|"
    r"verify .{0,20}before asserting|instinct|bandwidth|on the merits|"
    r"don'?t over-?infer|surface vs|act[, ]+don'?t ask|time of day|"
    r"slow down|hold the bar|raise the bar|defer-with|conduct|"
    r"philosophy|intuition|theory|\blens\b|"
    r"check adr|retarget|originator|blind-?spot|coverage gap|"
    r"verification[- ]tier|\btier\b|\bmystery\b|playbook|runbook|"
    r"process|priorit|\bnag\b|don'?t conflate|reviewer step|"
    r"pre-?implementation review)",
    re.IGNORECASE,
)


@dataclass
class Candidate:
    source: str  # relative-ish path to the source entry
    corpus: str  # "lessons" | "bugs" | "memory"
    title: str
    invariant: str  # best-guess one-line rule extracted from the entry
    check_kind: str  # "pre-commit" | "ticket_lint-rule" | "CI"
    draft: str  # sketch of the guard, modeled on existing precedents
    rationale: str  # why it qualified
    confidence: str  # "high" | "medium"
    signals: dict = field(default_factory=dict)
    fingerprint: str = ""  # stable id (source + invariant) for the dedup ledger

    def __post_init__(self):
        if not self.fingerprint:
            self.fingerprint = _fingerprint(self.source, self.invariant)


def _fingerprint(source: str, invariant: str) -> str:
    """Stable candidate id: the source path + a hash of its invariant. Source
    alone is too coarse (an entry can yield a revised invariant); invariant
    alone is too fragile (re-worded lessons). The pair is what the ledger keys
    on so a re-sweep never re-files (or re-proposes a declined) candidate."""
    src = Path(source).name
    h = hashlib.sha256(invariant.strip().encode("utf-8")).hexdigest()[:12]
    return f"{src}:{h}"


# --- Scan -------------------------------------------------------------------


def _iter_entries(directory: Path, corpus: str) -> list[tuple[Path, str]]:
    """Return (path, text) for each promotable-source .md file, README excluded."""
    if not directory.is_dir():
        return []
    out: list[tuple[Path, str]] = []
    for p in sorted(directory.glob("*.md")):
        if p.name.upper() == "README.MD":
            continue
        try:
            out.append((p, p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return out


def _title_of(path: Path, text: str) -> str:
    """First markdown H1, else the filename stem."""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return path.stem


def _sentences(text: str) -> list[str]:
    """Crude sentence/line splitter good enough for invariant extraction."""
    # Split on sentence punctuation and newlines; drop code-fence noise lightly.
    chunks = re.split(r"(?<=[.!?])\s+|\n", text)
    return [c.strip() for c in chunks if c.strip()]


# --- Detect -----------------------------------------------------------------


def _is_mechanical_sentence(s: str) -> bool:
    """A sentence is mechanical iff it pairs an imperative modality with a
    concrete signal: a quoted code token (preferred) or a structural noun."""
    if not MODALITY_RE.search(s):
        return False
    return bool(INLINE_CODE_RE.search(s) or MECHANICAL_NOUN_RE.search(s))


def _best_invariant(text: str) -> str | None:
    """Pick the most rule-like sentence: modality + concrete mechanical signal."""
    best = None
    for s in _sentences(text):
        if _is_mechanical_sentence(s):
            # Prefer the shortest such sentence — rules are terse.
            if best is None or len(s) < len(best):
                best = s
    return best


def _classify_check_kind(text: str, invariant: str | None) -> str:
    blob = ((invariant or "") + " " + text).lower()
    if any(k in blob for k in ("ticket", "frontmatter", "front-matter", "state:", "depends_on")):
        return "ticket_lint-rule"
    if any(k in blob for k in ("server-side", "server side", "ci ", "github actions", "branch-protection", "branch protection")):
        return "CI"
    return "pre-commit"


def _draft_for(check_kind: str, invariant: str, source: str) -> str:
    """A sketch modeled on the existing hand-built guards. Not runnable code —
    a starting point a human/recovery refines before landing."""
    inv = invariant.replace("\n", " ").strip()
    if check_kind == "ticket_lint-rule":
        return (
            "# add to tools/ticket_lint.py rule set (model on existing rules):\n"
            "def _rule_<name>(ticket, text):\n"
            f"    # INVARIANT: {inv}\n"
            "    # return a LintFinding if violated, else None\n"
            "    ...\n"
        )
    if check_kind == "CI":
        return (
            "# add a required GitHub Actions check (model on echo server-side CI):\n"
            f"# INVARIANT: {inv}\n"
            "# - run a scanner over the diff; fail the job (non-zero) on violation\n"
            "# - wire as a branch-protection required check\n"
        )
    # pre-commit (default) — model on the HERMES byte-guard shape.
    return (
        "#!/usr/bin/env python3\n"
        "# pre-commit guard (model on tools/_git_utils.py _hermes_scan_*):\n"
        f"# INVARIANT: {inv}\n"
        f"# SOURCE: {source}\n"
        "# scan staged content; print the violation + exit non-zero to block.\n"
        "import sys\n"
        "def scan(text: str) -> str | None:\n"
        "    ...  # return a violation message, or None if clean\n"
    )


def detect(path: Path, text: str, corpus: str) -> Candidate | None:
    """Apply the high-precision promotability criteria. Return a Candidate or None."""
    # (3) Soft veto first — cheapest and most decisive.
    if SOFT_VETO_RE.search(text):
        return None

    self_tag = bool(SELF_TAG_RE.search(text))
    invariant = _best_invariant(text)
    mechanical = invariant is not None
    recurrence = bool(RECURRENCE_RE.search(text))
    irreversible = bool(IRREVERSIBLE_RE.search(text))
    distinct_tickets = set(re.findall(r"\bT-\d{3,4}\b", text))

    # A mechanical invariant is REQUIRED to draft against — no exceptions, even
    # for a self-tagged entry (a self-tag with no quotable rule is too vague).
    if invariant is None:
        return None

    # Qualification (per design §"The P1 core", tuned for precision in inc.2):
    #   (A) explicit self-tag asking to become a guard, OR
    #   (B) mechanical invariant AND (lexical recurrence OR irreversibility).
    qualifies = self_tag or recurrence or irreversible
    if not qualifies:
        return None

    # Confidence.
    if self_tag or (recurrence and irreversible):
        confidence = "high"
    else:
        confidence = "medium"

    check_kind = _classify_check_kind(text, invariant)
    title = _title_of(path, text)
    source = str(path)
    reasons = []
    if self_tag:
        reasons.append("explicit self-tag (asks to be a guard)")
    if mechanical:
        reasons.append("mechanically-checkable invariant present")
    if recurrence:
        reasons.append("lexical recurrence marker")
    if irreversible:
        reasons.append("explicit irreversibility")

    return Candidate(
        source=source,
        corpus=corpus,
        title=title,
        invariant=invariant.replace("\n", " ").strip()[:300],
        check_kind=check_kind,
        draft=_draft_for(check_kind, invariant, source),
        rationale="; ".join(reasons),
        confidence=confidence,
        signals={
            "self_tag": self_tag,
            "mechanical": mechanical,
            "recurrence": recurrence,
            "irreversible": irreversible,
            "distinct_tickets": sorted(distinct_tickets),
        },
    )


def scan_all(
    lessons_dir: Path,
    bugs_dir: Path,
    memory_dir: Path,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for directory, corpus in (
        (lessons_dir, "lessons"),
        (bugs_dir, "bugs"),
        (memory_dir, "memory"),
    ):
        for path, text in _iter_entries(directory, corpus):
            if corpus == "memory" and not path.name.startswith("feedback_"):
                continue
            c = detect(path, text, corpus)
            if c is not None:
                candidates.append(c)
    # Strongest first.
    candidates.sort(key=lambda c: (-CONFIDENCE_ORDER[c.confidence], c.corpus, c.source))
    return candidates


# --- Dedup ledger -----------------------------------------------------------
# The ledger records every candidate we have already acted on so a re-sweep is
# idempotent. Statuses: "filed" (a ticket exists), "landed" (a guard was
# committed), "declined" (recovery judged it should stay soft — a decline is itself
# signal that it is not promotable, so we never re-propose it). Any recorded
# fingerprint is skipped on the next sweep regardless of status.
#
# T-2827 (P4 traceability): entries also carry `filed_at` / `decided_at` —
# ISO-8601 UTC decision timestamps. Both are ADDITIVE and OPTIONAL: entries
# written before T-2827 simply lack them (never fabricated — see load_ledger/
# set_status), and both readers below tolerate absence. Shaped so a future
# ORG-065 lineage-graph ingestion has a real "when" to query, not just a
# "what" — no graph code lands here, this is just the durable substrate for it.

LEDGER_STATUSES = ("filed", "landed", "declined")


def _now_stamp(now: datetime | None = None) -> str:
    """ISO-8601 UTC, `Z`-suffixed (matches the convention used elsewhere in
    this repo, e.g. tools/_git_utils.py, tools/activity_digest.py). `now` is
    an injectable clock (default the real clock) so callers/tests can pin a
    deterministic timestamp — same shape as tools/_audit.py's `now` param."""
    dt = now if now is not None else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_ledger(path: Path) -> dict:
    """Return {fingerprint: {status, ...}}. Missing/empty/corrupt → {} (the
    pipeline must degrade to 'propose everything', never crash on a bad ledger)."""
    if not path or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def filter_unledgered(candidates: list[Candidate], ledger: dict) -> list[Candidate]:
    """Drop candidates whose fingerprint is already in the ledger (any status)."""
    return [c for c in candidates if c.fingerprint not in ledger]


def record_ledger(path: Path, candidates: list[Candidate], status: str,
                  *, now: datetime | None = None) -> dict:
    """Merge candidates into the ledger under `status` and persist. Returns the
    updated ledger. Existing entries are not downgraded blindly — a 'landed' or
    'declined' fingerprint is left as-is (re-recording as 'filed' would be a
    regression); only genuinely new fingerprints are added.

    A newly-added entry gets `filed_at` (T-2827) IFF `status == "filed"` — the
    field means "when this candidate was first filed for review"; stamping it
    under any other status would make it lie about what happened (P4)."""
    if status not in LEDGER_STATUSES:
        raise ValueError(f"status must be one of {LEDGER_STATUSES}, got {status!r}")
    ledger = load_ledger(path)
    for c in candidates:
        if c.fingerprint in ledger:
            continue
        entry = {
            "status": status,
            "source": c.source,
            "title": c.title,
            "check_kind": c.check_kind,
        }
        if status == "filed":
            entry["filed_at"] = _now_stamp(now)
        ledger[c.fingerprint] = entry
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")
    return ledger


# --- File to the review surface (inc 3b) ------------------------------------


def render_candidate_md(c: Candidate) -> str:
    """One promotion candidate as a reviewable markdown file. The fingerprint is
    in the frontmatter so the ACCEPT/DECLINE step can find it in the ledger."""
    draft_block = "\n".join(c.draft.rstrip().splitlines())
    return (
        "---\n"
        f"fingerprint: {c.fingerprint}\n"
        "status: pending-review\n"
        f"source: {c.source}\n"
        f"corpus: {c.corpus}\n"
        f"check_kind: {c.check_kind}\n"
        f"confidence: {c.confidence}\n"
        "---\n\n"
        f"# Promotion candidate: {c.title}\n\n"
        f"**Invariant:** {c.invariant}\n\n"
        f"**Why promotable:** {c.rationale}\n\n"
        "**Proposed guard** (draft, modeled on the existing precedents —\n"
        "`ticket_lint` rule / the byte-sequence guard / a pre-commit hook):\n\n"
        "```\n"
        f"{draft_block}\n"
        "```\n\n"
        "## Recovery review — this is a review surface, NOT an auto-fired ticket\n"
        "- **ACCEPT** → file an action ticket to land this guard (the real, tested\n"
        "  work), record this fingerprint `landed` in the ledger, and delete this file.\n"
        "- **DECLINE** → record this fingerprint `declined` in the ledger (so it is\n"
        "  never re-proposed) and delete this file. The decline says: keep it soft.\n"
    )


def _candidate_filename(c: Candidate) -> str:
    """Filesystem-safe name from the source stem + the fingerprint hash. The raw
    fingerprint carries a ':' (illegal-ish in filenames), so derive instead."""
    stem = Path(c.source).stem
    digest = c.fingerprint.split(":")[-1]
    return f"{stem}__{digest}.md"


def file_candidates(candidates: list[Candidate], surface_dir: Path,
                    ledger_path: Path) -> list[Path]:
    """Write each not-yet-ledgered candidate to the review surface and record it
    'filed' in the ledger. The recovery-gate's filing half: it NEVER commits a hook
    and NEVER files a ticket (a bare target=recovery ticket auto-fires; this surface
    cannot). Idempotent — a fingerprint already filed/landed/declined is skipped,
    so a re-sweep is a no-op. Returns the paths written this call.

    Requires a ledger: idempotency is the whole point of the surface (without it a
    re-sweep would re-write declined candidates recovery already rejected)."""
    ledger = load_ledger(ledger_path)
    fresh = filter_unledgered(candidates, ledger)
    if fresh:
        surface_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for c in fresh:
        fpath = surface_dir / _candidate_filename(c)
        fpath.write_text(render_candidate_md(c), encoding="utf-8")
        written.append(fpath)
    record_ledger(ledger_path, fresh, "filed")
    return written


# --- Recovery review of a filed surface (inc 3b/4) ------------------------------


def set_status(ledger_path: Path, fingerprint: str, status: str,
               *, now: datetime | None = None) -> dict:
    """Transition a ledger entry to a TERMINAL review status (landed|declined).
    Unlike record_ledger (which only ADDS new fingerprints and never downgrades),
    this is the recovery-review verb: a 'filed' candidate becomes 'landed' or
    'declined'. Idempotent — re-declining a declined fingerprint is a no-op. A
    fingerprint not previously seen is recorded directly (a pre-emptive decline).

    `decided_at` (T-2827) is stamped on first assignment or on a genuine status
    change; a redundant identical re-call leaves it alone — decided_at records
    when the decision was actually MADE, and a no-op re-call is not a new
    decision (P4: the timestamp must not drift on repeated identical review
    verbs)."""
    if status not in ("landed", "declined"):
        raise ValueError(f"review status must be landed|declined, got {status!r}")
    ledger = load_ledger(ledger_path)
    entry = ledger.get(fingerprint)
    if entry is None:
        ledger[fingerprint] = {"status": status, "decided_at": _now_stamp(now)}
    else:
        if entry.get("status") != status:
            entry["decided_at"] = _now_stamp(now)
        entry["status"] = status
    ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True),
                           encoding="utf-8")
    return ledger


def _parse_frontmatter(text: str) -> dict:
    """Tiny `key: value` frontmatter parser for a candidate .md (no YAML dep)."""
    out: dict = {}
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    if end == -1:
        return out
    for line in text[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def _iter_candidate_files(surface_dir: Path):
    """Yield (path, frontmatter) for each PENDING candidate .md (README.md
    excluded). `surface_dir.glob("*.md")` is intentionally NON-recursive: it
    must NEVER become `**/*.md` (or similar), because `decided/` — the
    T-2827 archive subdir — lives under `surface_dir` and its files must not
    resurface as pending. That non-recursion is the whole structural guarantee
    the pending-list / dedup-filter rely on."""
    if not surface_dir.is_dir():
        return
    for p in sorted(surface_dir.glob("*.md")):
        if p.name == "README.md":
            continue
        yield p, _parse_frontmatter(p.read_text(encoding="utf-8"))


def find_candidate_file(surface_dir: Path, fingerprint: str) -> Path | None:
    """The candidate .md whose frontmatter fingerprint matches, or None."""
    for p, fm in _iter_candidate_files(surface_dir):
        if fm.get("fingerprint") == fingerprint:
            return p
    return None


def list_pending(surface_dir: Path) -> list[dict]:
    """Frontmatter of every pending candidate file, for recovery review / the digest."""
    return [fm for _p, fm in _iter_candidate_files(surface_dir)]


# --- Decided-candidate archive (T-2827 defect 2) ----------------------------
# land/decline used to DELETE the candidate .md — destroying the invariant,
# draft, rationale, and any decline reasoning regardless of outcome. Instead
# the reviewed file is stamped + MOVED into `decided/`, a subdir of the same
# surface. It ships for free in promote_lessons_cron.py's existing
# `commit_scoped(["promotion-candidates"], ...)` scope — verified at source
# that `git add -- promotion-candidates` (allow_prefix=True) recursively
# stages subdirs, so no cron change is needed.

DECIDED_SUBDIR = "decided"


def _rewrite_decision_frontmatter(text: str, status: str, decided_at: str) -> str:
    """Return `text` with its frontmatter `status:` flipped to the terminal
    outcome and `decided_at:` set — the candidate BODY (invariant/draft/
    rationale) is untouched. That preservation is the point of defect 2."""
    if not text.startswith("---"):
        # No frontmatter (shouldn't happen for a rendered candidate) — prepend
        # one rather than silently dropping the decision.
        return f"---\nstatus: {status}\ndecided_at: {decided_at}\n---\n\n{text}"
    end = text.find("\n---", 3)
    if end == -1:
        return text
    header, body = text[3:end], text[end + 4:]
    out_lines = []
    seen_status = seen_decided = False
    for line in header.splitlines():
        key = line.split(":", 1)[0].strip() if ":" in line else None
        if key == "status":
            out_lines.append(f"status: {status}")
            seen_status = True
        elif key == "decided_at":
            out_lines.append(f"decided_at: {decided_at}")
            seen_decided = True
        else:
            out_lines.append(line)
    if not seen_status:
        out_lines.append(f"status: {status}")
    if not seen_decided:
        out_lines.append(f"decided_at: {decided_at}")
    return "---" + "\n".join(out_lines) + "\n---" + body


def _archive_decided_candidate(surface_dir: Path, fpath: Path, status: str,
                               decided_at: str) -> Path:
    """Stamp the outcome into `fpath`'s frontmatter and move it into
    `decided/`, overwriting idempotently if a same-named file is already
    there (never errors on a pre-existing name)."""
    text = fpath.read_text(encoding="utf-8")
    new_text = _rewrite_decision_frontmatter(text, status, decided_at)
    decided_dir = surface_dir / DECIDED_SUBDIR
    decided_dir.mkdir(parents=True, exist_ok=True)
    dest = decided_dir / fpath.name
    dest.write_text(new_text, encoding="utf-8")
    fpath.unlink()
    return dest


def review_candidate(surface_dir: Path, ledger_path: Path, fingerprint: str,
                     decision: str, *, now: datetime | None = None) -> bool:
    """Apply a recovery decision: set the ledger status (landed|declined), stamp
    decided_at, and PRESERVE the candidate file by archiving it to `decided/`
    (T-2827 — never deleted). Returns True if a candidate file was archived.
    The ledger is updated regardless (so a decline sticks even if the file was
    already gone). `now` is resolved once and threaded into both the ledger
    write and the archived file's stamp so they never drift."""
    status = "landed" if decision == "land" else "declined"
    resolved_now = now if now is not None else datetime.now(timezone.utc)
    set_status(ledger_path, fingerprint, status, now=resolved_now)
    fpath = find_candidate_file(surface_dir, fingerprint)
    if fpath is not None:
        _archive_decided_candidate(surface_dir, fpath, status, _now_stamp(resolved_now))
        return True
    return False


# --- Report -----------------------------------------------------------------


def render_report(candidates: list[Candidate]) -> str:
    if not candidates:
        return "promote_lessons: 0 promotable candidates found (report-only)."
    lines = [
        f"promote_lessons: {len(candidates)} promotable candidate(s) "
        f"(report-only — nothing filed/committed).",
        "",
    ]
    for i, c in enumerate(candidates, 1):
        lines += [
            f"[{i}] {c.title}",
            f"    confidence : {c.confidence}",
            f"    corpus     : {c.corpus}",
            f"    source     : {c.source}",
            f"    check-kind : {c.check_kind}",
            f"    invariant  : {c.invariant}",
            f"    why        : {c.rationale}",
            "    draft:",
        ]
        lines += [f"      {ln}" for ln in c.draft.rstrip().splitlines()]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Lessons→hooks promotion detector (report-only).")
    ap.add_argument("--lessons-dir", type=Path, default=DEFAULT_LESSONS_DIR)
    ap.add_argument("--bugs-dir", type=Path, default=DEFAULT_BUGS_DIR)
    ap.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY_DIR)
    ap.add_argument("--min-confidence", choices=("medium", "high"), default="medium")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--ledger", type=Path, default=None,
                    help="dedup ledger (JSON); already-recorded candidates are skipped")
    ap.add_argument("--file", action="store_true",
                    help="file unledgered candidates to the review surface "
                         "(inc 3b); requires --ledger. NOT report-only.")
    ap.add_argument("--candidates-dir", type=Path, default=DEFAULT_CANDIDATES_DIR,
                    help="promotion-candidate review surface (with --file)")
    ap.add_argument("--list-pending", action="store_true",
                    help="list the candidates currently awaiting recovery review")
    ap.add_argument("--decline", metavar="FINGERPRINT", default=None,
                    help="recovery review: mark a candidate declined (keep it soft) "
                         "+ remove its file; requires --ledger")
    ap.add_argument("--land", metavar="FINGERPRINT", default=None,
                    help="recovery review: mark a candidate landed (guard committed) "
                         "+ remove its file; requires --ledger")
    args = ap.parse_args(argv)

    # --- recovery review verbs (operate on the filed surface, not the corpus) ---
    if args.list_pending:
        pending = list_pending(args.candidates_dir)
        if not pending:
            print("promote_lessons: 0 candidates pending review.")
            return 0
        print(f"promote_lessons: {len(pending)} candidate(s) pending review:")
        for fm in pending:
            print(f"  [{fm.get('confidence','?'):6}] {fm.get('fingerprint','?')}")
            print(f"           source: {fm.get('source','?')}")
        return 0
    if args.decline or args.land:
        if not args.ledger:
            ap.error("--decline/--land require --ledger")
        fp = args.decline or args.land
        decision = "decline" if args.decline else "land"
        removed = review_candidate(args.candidates_dir, args.ledger, fp, decision)
        print(f"promote_lessons: {decision}d {fp} "
              f"({'file archived to decided/' if removed else 'no file found; ledger updated'}).")
        return 0

    candidates = scan_all(args.lessons_dir, args.bugs_dir, args.memory_dir)
    threshold = CONFIDENCE_ORDER[args.min_confidence]
    candidates = [c for c in candidates if CONFIDENCE_ORDER[c.confidence] >= threshold]

    if args.file:
        if not args.ledger:
            ap.error("--file requires --ledger (idempotency depends on it)")
        written = file_candidates(candidates, args.candidates_dir, args.ledger)
        skipped = len(candidates) - len(written)
        print(f"promote_lessons: filed {len(written)} new candidate(s) to "
              f"{args.candidates_dir} ({skipped} already in ledger, skipped).")
        for p in written:
            print(f"  + {p.name}")
        return 0

    if args.ledger:
        before = len(candidates)
        candidates = filter_unledgered(candidates, load_ledger(args.ledger))
        skipped = before - len(candidates)
        if skipped and not args.json:
            print(f"(ledger: skipped {skipped} already-recorded candidate(s))\n")

    if args.json:
        print(json.dumps([asdict(c) for c in candidates], indent=2))
    else:
        print(render_report(candidates))
    return 0


if __name__ == "__main__":
    sys.exit(main())
