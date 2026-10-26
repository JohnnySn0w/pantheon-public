#!/usr/bin/env python3
"""Pantheon typed-link traversal surface — read-only index/query over existing data.

Realizes the `tools/links.py` design locked in `notes/dei-foundational-wip/links.md`
(Link Type taxonomy, T-0669; build ticket T-1087). It is a thin READ-ONLY CLI/lib
over data that already lives in ticket frontmatter, transition logs, commit messages,
and `architecture/adr/`. It stores NO new authoritative state: every answer is derived live from
those sources (grep + frontmatter reads + `git log --grep`). A materialized index
(`rebuild-index`) is an optional regeneratable snapshot for external tooling — reads
never depend on it, so it can never go silently stale (grep is always the source of
truth). No graph DB; if traversal depth >1 ever becomes a real need, THAT is the
trigger to revisit infra (explicitly deferred per links.md locked scope).

Two layers (per links.md §"Queryable surface"):
  1. Direct reads (no index): parent_ticket / depends_on / ratifiable_files come
     straight from frontmatter (via a lenient local reader); commit↔ticket from
     `git log --grep`; actor↔ticket by parsing `[<actor>]` from the transition log.
  2. Derived edges (live-scanned each call): actor→tickets (touched-by),
     ORG-NNN→tickets (governed_by / adr), msg_NNNN→files (cites). `rebuild-index`
     materializes the full edge set to a JSONL snapshot.

Link Types realized (links.md taxonomy):
  parent_ticket  Ticket→Ticket  KEEP      frontmatter            parents/children
  depends_on     Ticket→Ticket  KEEP      frontmatter            deps/blocks
  ratifies       Ticket→File    KEEP      frontmatter            ratifies
  fixes/touches  Commit→Ticket  PROMOTE   git log --grep         commits
  actor          Agent→Ticket   PROMOTE   transition-log [actor] actors / touched-by
  governed_by    Ticket→ADR     PROMOTE   ORG-NNN label↔architecture/adr/     adr
  cites          Ticket→Message PROMOTE-LITE  msg_NNNN token     cites

CLI:
    links.py neighbors  T-0669       # all typed edges touching a ticket, grouped
    links.py parents    T-0669       # parent_ticket (child→parent)
    links.py children   T-0669       # reverse parent_ticket
    links.py deps       T-0669       # depends_on (blocked→blocker)
    links.py blocks     T-0669       # reverse depends_on
    links.py ratifies   T-0669       # ratifiable_files
    links.py commits    T-0669       # commits referencing it (git log --grep)
    links.py actors     T-0669       # agents that transitioned it (from logs)
    links.py adr        ORG-022      # tickets governed_by an ADR (+ reverse: the file)
    links.py touched-by recovery         # tickets an agent has acted on (alias-aware)
    links.py cites      msg_1540     # tickets/notes citing a message (index)
    links.py rebuild-index           # materialize the derived edge snapshot
    links.py --json <subcommand> ... # machine-readable output for any subcommand

Read-only: no write path anywhere except `rebuild-index`, which writes only the
regeneratable snapshot under ~/.local/state/pantheon/ (never the repo).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
TICKETS_ROOT = REPO_ROOT / "tickets"
ADR_ROOT = REPO_ROOT / "architecture" / "adr"
NOTES_ROOT = REPO_ROOT / "notes"
TICKET_STATES = ("inbox", "active", "blocked", "done")

INDEX_PATH = Path.home() / ".local" / "state" / "pantheon" / "links.index.jsonl"

TICKET_ID_RE = re.compile(r"^T-\d{3,}$")
ORG_ID_RE = re.compile(r"^ORG-\d{2,}$")
MSG_ID_RE = re.compile(r"^msg_\d+$")

# Token shapes used when live-scanning bodies/labels.
_ORG_TOKEN_RE = re.compile(r"\bORG-\d{2,}\b")
_MSG_TOKEN_RE = re.compile(r"\bmsg_\d+\b")
# Transition-log actor: a bullet line `- <ts> [<actor>] ...`. Actor is the first
# bracketed token on a log bullet. Tolerant of leading whitespace.
_ACTOR_LINE_RE = re.compile(r"^\s*-\s+\S+\s+\[([a-zA-Z0-9_-]+)\]")


# ---------------------------------------------------------------------------
# Lenient frontmatter reader (read-only; never raises on a malformed ticket).
#
# Deliberately NOT tools.ticket.parse_ticket: that enforces lifecycle-grade
# required fields and RAISES on any partial/malformed file. A read-only
# traversal surface must tolerate one bad ticket without crashing every query,
# and must not require fields it doesn't read. This extracts only the fields
# links.py traverses (parent_ticket, depends_on, ratifiable_files, labels) plus
# the body, and returns empty for anything missing.
# ---------------------------------------------------------------------------


class _Doc:
    __slots__ = ("fields", "body")

    def __init__(self, fields: dict, body: str):
        self.fields = fields
        self.body = body


def _parse_scalar_or_list(raw: str):
    """Parse a frontmatter value: `[a, b]` → list[str]; `[]` → []; else the
    stripped scalar string (quotes stripped).

    Flow-style only (`[a, b]`), matching every real Pantheon ticket on the fields
    links.py reads (parent_ticket/depends_on/ratifiable_files/labels). Block-style
    YAML lists and inline `# comments` are NOT interpreted — out of scope for this
    lenient reader, and no live ticket uses them on these fields.
    """
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
    return raw.strip("'\"")


def read_doc(path: Path) -> _Doc:
    """Read a markdown file's `---`-delimited frontmatter + body, leniently.

    Never raises on malformed/absent frontmatter — returns empty fields + the
    whole text as body in that case.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _Doc({}, "")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return _Doc({}, text)
    fields: dict = {}
    body_start = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body_start = i + 1
            break
        line = lines[i].rstrip("\n").rstrip("\r")
        if ":" in line and not line.lstrip().startswith("#"):
            key, _, val = line.partition(":")
            key = key.strip()
            if key:
                fields[key] = _parse_scalar_or_list(val)
    if body_start is None:  # unterminated frontmatter — treat all as body
        return _Doc({}, text)
    return _Doc(fields, "".join(lines[body_start:]))


# ---------------------------------------------------------------------------
# Agent alias groups (persona ↔ account/role actor-strings).
#
# Transition-log actors are MIXED: persona names (recovery/orchestrator/specialist/codex), Unix
# account / role names (claude/claude-fallback/orchestrator/claude-research/
# specialist), and special non-agent actors (grooming/auto-unblock/operator/…).
# `touched-by <agent>` expands a query to its whole group so "recovery" finds
# `[recovery]` AND `[claude-fallback]` lines, etc. Unknown tokens match only
# themselves (so `touched-by grooming` still works literally).
#
# DERIVED (T-2256), and it was returning WRONG ANSWERS before it was. The list
# was hand-carried and stopped at the pre-ORG-042 three, so the ORG-042/ORG-043
# seats had no group at all — and alias_group() falls back to self, silently.
# Measured 2026-07-29 on the live tree: `touched-by archivist` matched only
# `[archivist]` lines and missed all 80 `[exocortex]` ones; `touched-by artisan`
# missed 10 `[suborch2]` ones. Not a latent risk — wrong output, and the gap grew
# with every transition log anyone wrote. Deriving from the persona registry plus
# the instance.yaml account bindings means a new seat is grouped at registration.
# ---------------------------------------------------------------------------

# Actor strings that are NOT derivable from the registries: a legacy free-form
# role word, and non-agent actors that appear as transition-log actors. These are
# additions to the derived groups, never a substitute for them.
_ALIAS_EXTRAS: dict[str, set[str]] = {"orchestrator": {"orchestrator"}}
_NON_AGENT_ACTOR_GROUPS: list[set[str]] = [{"operator"}]


def _build_alias_groups() -> list[set[str]]:
    """One group per registered persona: {persona, audit role-id, OS account} plus
    any non-derivable extras, then the non-agent actor groups. Identity entries
    collapse naturally (`recovery`'s audit role-id IS "recovery"; codex's account IS
    "codex"), so a singleton group is a correct answer, not a missing one."""
    from tools.agent_role_map import AGENT_ROLE_MAP, account_persona_map, resolve_role

    persona_accounts: dict[str, str] = {
        persona: account for account, persona in account_persona_map().items()
    }
    groups: list[set[str]] = []
    for persona in sorted(AGENT_ROLE_MAP):
        group = {persona, resolve_role(persona)} | _ALIAS_EXTRAS.get(persona, set())
        account = persona_accounts.get(persona)
        if account:
            group.add(account)
        groups.append(group)
    return groups + [set(g) for g in _NON_AGENT_ACTOR_GROUPS]


_ALIAS_GROUPS: list[set[str]] = _build_alias_groups()


def alias_group(agent: str) -> set[str]:
    """Return the full set of actor-strings equivalent to `agent`.

    Unknown agents (special actors like `grooming`, `auto-unblock`, or a future
    agent not yet mapped) match only themselves — never silently empty.
    """
    for group in _ALIAS_GROUPS:
        if agent in group:
            return set(group)
    return {agent}


# ---------------------------------------------------------------------------
# Ticket iteration + frontmatter reads (Layer 1: direct).
# ---------------------------------------------------------------------------


def iter_ticket_paths(tickets_root: Path = TICKETS_ROOT) -> list[Path]:
    """Yield every `T-NNNN.md` under each ticket state dir, id-sorted by stem."""
    out: list[Path] = []
    for state in TICKET_STATES:
        d = tickets_root / state
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file() and p.suffix == ".md" and p.stem.startswith("T-"):
                out.append(p)
    return sorted(out, key=lambda p: p.stem)


def find_ticket_path(tid: str, tickets_root: Path = TICKETS_ROOT) -> Path | None:
    """Locate the file for a ticket id across state dirs (first match)."""
    for state in TICKET_STATES:
        cand = tickets_root / state / f"{tid}.md"
        if cand.is_file():
            return cand
    return None


def _as_list(val) -> list[str]:
    """Normalize a frontmatter field to a list of strings (it may be a str, list,
    or None depending on how the YAML-ish value parsed)."""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v) for v in val if str(v).strip()]
    s = str(val).strip()
    return [s] if s else []


def get_parents(tid: str, tickets_root: Path = TICKETS_ROOT) -> list[str]:
    p = find_ticket_path(tid, tickets_root)
    if p is None:
        return []
    raw = read_doc(p).fields.get("parent_ticket")
    return [v for v in _as_list(raw) if TICKET_ID_RE.match(v)]


def get_children(tid: str, tickets_root: Path = TICKETS_ROOT) -> list[str]:
    out: list[str] = []
    for path in iter_ticket_paths(tickets_root):
        parents = [v for v in _as_list(read_doc(path).fields.get("parent_ticket"))]
        if tid in parents:
            out.append(path.stem)
    return sorted(out)


def get_deps(tid: str, tickets_root: Path = TICKETS_ROOT) -> list[str]:
    p = find_ticket_path(tid, tickets_root)
    if p is None:
        return []
    raw = read_doc(p).fields.get("depends_on")
    return [v for v in _as_list(raw) if TICKET_ID_RE.match(v)]


def get_blocks(tid: str, tickets_root: Path = TICKETS_ROOT) -> list[str]:
    out: list[str] = []
    for path in iter_ticket_paths(tickets_root):
        deps = _as_list(read_doc(path).fields.get("depends_on"))
        if tid in deps:
            out.append(path.stem)
    return sorted(out)


def get_ratifies(tid: str, tickets_root: Path = TICKETS_ROOT) -> list[str]:
    p = find_ticket_path(tid, tickets_root)
    if p is None:
        return []
    return _as_list(read_doc(p).fields.get("ratifiable_files"))


def get_actors(tid: str, tickets_root: Path = TICKETS_ROOT) -> list[str]:
    """Distinct transition-log actors for a ticket, in first-seen order."""
    p = find_ticket_path(tid, tickets_root)
    if p is None:
        return []
    seen: list[str] = []
    for line in read_doc(p).body.splitlines():
        m = _ACTOR_LINE_RE.match(line)
        if m:
            actor = m.group(1)
            if actor not in seen:
                seen.append(actor)
    return seen


def get_org_labels(tid: str, tickets_root: Path = TICKETS_ROOT) -> list[str]:
    """ORG-NNN governance labels on a ticket (governed_by ADRs)."""
    p = find_ticket_path(tid, tickets_root)
    if p is None:
        return []
    labels = _as_list(read_doc(p).fields.get("labels"))
    return sorted({lbl for lbl in labels if ORG_ID_RE.match(lbl)})


# ---------------------------------------------------------------------------
# commit↔ticket (Layer 1: git log --grep). Trailer `Pantheon-Ticket: T-NNNN` is
# the first-class form; until backfill (separate ticket) lands, inline `T-NNNN`
# mentions are the live signal — `git log --grep` matches BOTH.
# ---------------------------------------------------------------------------


def get_commits(tid: str, repo_root: Path = REPO_ROOT, limit: int = 50) -> list[tuple[str, str]]:
    """Return (sha, subject) for commits whose message references `tid`.

    Digit-boundary anchored so `T-108` does not match `T-1087` and `T-1087`
    does not match `T-10870`. Read-only; never invokes a write subcommand.
    """
    pat = rf"(^|[^A-Za-z0-9-]){re.escape(tid)}([^0-9]|$)"
    try:
        res = subprocess.run(
            ["git", "log", "-E", f"--grep={pat}", "--format=%h\t%s"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return []
    if res.returncode != 0:
        return []
    out: list[tuple[str, str]] = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition("\t")
        out.append((sha, subject))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Derived reverse edges (Layer 2: live-scan). Always correct; no cache dep.
# ---------------------------------------------------------------------------


def get_touched_by(agent: str, tickets_root: Path = TICKETS_ROOT) -> list[str]:
    """Tickets whose transition log carries an actor in `agent`'s alias group."""
    group = alias_group(agent)
    out: list[str] = []
    for path in iter_ticket_paths(tickets_root):
        body = read_doc(path).body
        actors = {
            m.group(1)
            for line in body.splitlines()
            if (m := _ACTOR_LINE_RE.match(line))
        }
        if actors & group:
            out.append(path.stem)
    return sorted(out)


def get_adr_tickets(org_id: str, tickets_root: Path = TICKETS_ROOT) -> list[str]:
    """Tickets carrying the `org_id` governance label (forward: ticket→ADR)."""
    out: list[str] = []
    for path in iter_ticket_paths(tickets_root):
        labels = _as_list(read_doc(path).fields.get("labels"))
        if org_id in labels:
            out.append(path.stem)
    return sorted(out)


def find_adr_file(org_id: str, adr_root: Path = ADR_ROOT, repo_root: Path = REPO_ROOT) -> str | None:
    """Reverse: the `architecture/adr/ORG-NNN-*.md` file for an ADR id, if present."""
    if not adr_root.is_dir():
        return None

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(repo_root))
        except ValueError:
            return str(p)

    for p in sorted(adr_root.glob(f"{org_id}-*.md")):
        return _rel(p)
    # Also tolerate an exact `ORG-NNN.md`.
    exact = adr_root / f"{org_id}.md"
    return _rel(exact) if exact.is_file() else None


def get_cites(
    msg_id: str,
    tickets_root: Path = TICKETS_ROOT,
    notes_root: Path = NOTES_ROOT,
    repo_root: Path = REPO_ROOT,
) -> list[str]:
    """Repo-relative files (tickets + notes) citing a `msg_NNNN` token.

    Index-only per links.md: a Telegram message is external/unresolvable, so this
    locates citations, it does not dereference content.
    """
    token_re = re.compile(rf"\b{re.escape(msg_id)}\b")
    out: list[str] = []
    roots = [r for r in (tickets_root, notes_root) if r.is_dir()]
    for root in roots:
        for p in sorted(root.rglob("*.md")):
            try:
                if token_re.search(p.read_text(encoding="utf-8")):
                    try:
                        out.append(str(p.relative_to(repo_root)))
                    except ValueError:
                        out.append(str(p))
            except (OSError, UnicodeDecodeError):
                continue
    return out


# ---------------------------------------------------------------------------
# rebuild-index — materialize a regeneratable snapshot (non-authoritative).
# ---------------------------------------------------------------------------


def build_index_records(tickets_root: Path = TICKETS_ROOT) -> list[dict]:
    """Materialize every directly-derivable edge as a flat {type, from, to} record.

    Snapshot only — for external tooling / inspection. Reads never consult it;
    it can never make a query wrong, only faster (and even that is optional).
    """
    records: list[dict] = []
    for path in iter_ticket_paths(tickets_root):
        t = read_doc(path)
        tid = path.stem
        for parent in [v for v in _as_list(t.fields.get("parent_ticket")) if TICKET_ID_RE.match(v)]:
            records.append({"type": "parent_ticket", "from": tid, "to": parent})
        for dep in [v for v in _as_list(t.fields.get("depends_on")) if TICKET_ID_RE.match(v)]:
            records.append({"type": "depends_on", "from": tid, "to": dep})
        for f in _as_list(t.fields.get("ratifiable_files")):
            records.append({"type": "ratifies", "from": tid, "to": f})
        for lbl in _as_list(t.fields.get("labels")):
            if ORG_ID_RE.match(lbl):
                records.append({"type": "governed_by", "from": tid, "to": lbl})
        seen_actor: set[str] = set()
        for line in t.body.splitlines():
            m = _ACTOR_LINE_RE.match(line)
            if m and m.group(1) not in seen_actor:
                seen_actor.add(m.group(1))
                records.append({"type": "actor", "from": m.group(1), "to": tid})
        for tok in sorted(set(_MSG_TOKEN_RE.findall(t.body))):
            records.append({"type": "cites", "from": tid, "to": tok})
    return records


def rebuild_index(index_path: Path = INDEX_PATH, tickets_root: Path = TICKETS_ROOT) -> int:
    """Write the materialized edge snapshot. Returns the record count."""
    records = build_index_records(tickets_root)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = index_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    os.replace(tmp, index_path)
    return len(records)


# ---------------------------------------------------------------------------
# neighbors — aggregate all typed edges touching one ticket.
# ---------------------------------------------------------------------------


def neighbors(tid: str, tickets_root: Path = TICKETS_ROOT, repo_root: Path = REPO_ROOT) -> dict:
    """All typed edges touching a ticket, grouped by link type."""
    commits = get_commits(tid, repo_root)
    return {
        "ticket": tid,
        "parents": get_parents(tid, tickets_root),
        "children": get_children(tid, tickets_root),
        "depends_on": get_deps(tid, tickets_root),
        "blocks": get_blocks(tid, tickets_root),
        "ratifies": get_ratifies(tid, tickets_root),
        "governed_by": get_org_labels(tid, tickets_root),
        "actors": get_actors(tid, tickets_root),
        "commits": [{"sha": s, "subject": subj} for s, subj in commits],
    }


# ---------------------------------------------------------------------------
# CLI rendering.
# ---------------------------------------------------------------------------


def _print_list(label: str, items: list[str]) -> None:
    if not items:
        print(f"{label}: —")
        return
    print(f"{label}:")
    for it in items:
        print(f"  {it}")


def _render_neighbors_text(data: dict) -> None:
    print(f"# Links for {data['ticket']}")
    _print_list("parents (parent_ticket)", data["parents"])
    _print_list("children", data["children"])
    _print_list("depends_on (blockers)", data["depends_on"])
    _print_list("blocks (dependents)", data["blocks"])
    _print_list("ratifies (files)", data["ratifies"])
    _print_list("governed_by (ADRs)", data["governed_by"])
    _print_list("actors", data["actors"])
    if data["commits"]:
        print("commits:")
        for c in data["commits"]:
            print(f"  {c['sha']} {c['subject']}")
    else:
        print("commits: —")


def _validate_id(value: str, kind: str, pattern: re.Pattern) -> str:
    if not pattern.match(value):
        print(f"error: {value!r} is not a valid {kind} id", file=sys.stderr)
        raise SystemExit(2)
    return value


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="links.py",
        description="Read-only typed-link traversal over Pantheon data (T-1087 / links.md).",
    )
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("neighbors", "parents", "children", "deps", "blocks", "ratifies", "commits", "actors"):
        sp = sub.add_parser(name)
        sp.add_argument("ticket")
    sp_adr = sub.add_parser("adr")
    sp_adr.add_argument("org")
    sp_tb = sub.add_parser("touched-by")
    sp_tb.add_argument("agent")
    sp_cites = sub.add_parser("cites")
    sp_cites.add_argument("msg")
    sub.add_parser("rebuild-index")

    args = p.parse_args(argv)

    def emit(obj, render_text) -> int:
        if args.json:
            print(json.dumps(obj, indent=2, sort_keys=True))
        else:
            render_text(obj)
        return 0

    if args.cmd == "neighbors":
        tid = _validate_id(args.ticket, "ticket", TICKET_ID_RE)
        return emit(neighbors(tid), _render_neighbors_text)

    if args.cmd in ("parents", "children", "deps", "blocks", "ratifies", "actors"):
        tid = _validate_id(args.ticket, "ticket", TICKET_ID_RE)
        fn = {
            "parents": get_parents,
            "children": get_children,
            "deps": get_deps,
            "blocks": get_blocks,
            "ratifies": get_ratifies,
            "actors": get_actors,
        }[args.cmd]
        items = fn(tid)
        return emit(
            {"ticket": tid, args.cmd: items},
            lambda o: _print_list(f"{args.cmd} {tid}", items),
        )

    if args.cmd == "commits":
        tid = _validate_id(args.ticket, "ticket", TICKET_ID_RE)
        commits = get_commits(tid)

        def _render_commits(_o) -> None:
            if not commits:
                print(f"commits {tid}: —")
                return
            print(f"commits {tid}:")
            for sha, subj in commits:
                print(f"  {sha} {subj}")

        return emit(
            {"ticket": tid, "commits": [{"sha": s, "subject": j} for s, j in commits]},
            _render_commits,
        )

    if args.cmd == "adr":
        org = _validate_id(args.org, "ADR", ORG_ID_RE)
        tickets = get_adr_tickets(org)
        adr_file = find_adr_file(org)
        return emit(
            {"adr": org, "file": adr_file, "tickets": tickets},
            lambda o: (
                print(f"# ADR {org}"),
                print(f"file: {adr_file or '—'}"),
                _print_list("tickets governed_by", tickets),
            ),
        )

    if args.cmd == "touched-by":
        tickets = get_touched_by(args.agent)
        return emit(
            {"agent": args.agent, "group": sorted(alias_group(args.agent)), "tickets": tickets},
            lambda o: _print_list(f"touched-by {args.agent}", tickets),
        )

    if args.cmd == "cites":
        msg = _validate_id(args.msg, "message", MSG_ID_RE)
        files = get_cites(msg)
        return emit(
            {"msg": msg, "files": files},
            lambda o: _print_list(f"cites {msg}", files),
        )

    if args.cmd == "rebuild-index":
        n = rebuild_index()
        print(f"wrote {n} edge record(s) to {INDEX_PATH}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
