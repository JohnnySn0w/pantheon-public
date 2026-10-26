#!/usr/bin/env python3
"""ace_curate.py — ACE-style itemized store + deterministic curator for HANDOFF.md.

T-2819 PoC (knowledge/episodic/2026-10-22-ace-handoff-poc/, kept there as the
historical record), moved into tools/ and hardened by T-5348 per the T-2819
HO rulings (a) and (b). ORG-074 element E2. It mechanizes the *Curator* half of ACE
(Agentic Context Engineering, arXiv 2510.04618): the context is held as
itemized bullets with stable ids and counters, and every change arrives as
an explicit delta that a deterministic, non-LLM merge applies. The
*Reflector* (the agent that writes the delta) is out of this file on purpose.

What it does:
  parse   HANDOFF.md -> bullet store (JSON). Every block (heading, list item,
          paragraph, fenced code) becomes one item with a stable id. An
          unmodified store renders back to the input byte for byte.
  apply   store + delta -> new store. Ops: ADD, ADD_SECTION(heading, body?),
          UPDATE(id, reason), RETIRE(id, reason), TAG(id, helpful/harmful).
          All-or-nothing: a malformed delta is rejected before anything changes.
  render  store -> proposed HANDOFF markdown (retired items are omitted).
          --topic pins the durable (locked-pattern) sections first; the
          default render keeps document order and round-trips byte-exact.
  propose apply + render + review (op summary with reasons, warnings, diff).
  reconcile  store + hand-edited HANDOFF -> a delta (ADD/ADD_SECTION/UPDATE/
          RETIRE, every op reason "hand-edit, needs review"). It only writes
          the delta; apply/propose (and their strict guards) run on it later.

Invariants and guards (enforced in code, covered by test_ace_curate.py):
  * no silent drop: every item active in the old store is active in the new
    store unless THIS delta RETIREs it with a non-empty reason (post-condition;
    a violation raises). Retired items are tombstoned, never deleted.
  * UPDATE needs a reason, and is checked like a partial retire: it is flagged
    when the new text is under UPDATE_SHRINK_RATIO of the old, or when it loses
    a ticket id, a path, or a `code/command` span the old text carried.
  * last open mention: with an open-ticket set, a RETIRE/UPDATE that removes
    the last active mention of an open ticket is flagged.
  * terminal ids (T-2819 ruling a): with a done-ticket set (done_tickets, or
    tickets_root/done/ read at check time), an UPDATE may drop a ticket id
    whose ticket is done; update_token_loss then lists only the other lost
    tokens, and the exempt drops are listed in the review. Open and unknown
    ids stay strict (an id that is both open and done is treated as open).
  * strict mode (strict=True / --strict) turns every flag above into a
    rejection; it requires an open-ticket set (possibly empty) so it cannot
    silently run half-armed.
  * locked regions (ORG-074 2026-10-09 amendment, gate 1): items in a locked
    section (default: headings matching Basics / Rules / Lessons / Gotchas) and items
    whose text starts with RULE / Lesson / "Rules to remember" are locked.
    UPDATE or RETIRE of a locked item is rejected, always. ADD into a locked
    section is allowed (grow, never silently rewrite). Lock patterns are
    configurable (--locks FILE).
  * locked-section replace (T-2819 ruling b): ADD_SECTION with a body whose
    heading equals a locked section's heading after case-folding and
    stripping one trailing parenthetical ("Basics (unchanged)") REPLACES that
    section's body, latest wins: the old items are tombstoned with a reason
    and the review logs old and new byte counts. The heading item keeps its
    text and id. Any other heading is a new section. A locked section is
    never removed by omission: no body (or an identical one) is a no-op.
  * dedup: an ADD near-identical to an active item (normalized-token Jaccard
    >= --dedup-threshold, default 0.8) is not added; the match is recorded
    and the matched item's ``seen`` counter goes up.
  * writes, two layers (ORG-058 human gate: this tool only proposes):
    (1) allowlist: an explicit root only, i.e. --scratch-root DIR (the
        scratch_roots argument), propose's --out-dir, or the Chronicle root
        (tools/chronicle.py chronicle_root(), resolved at call time) where
        the companions live. This tool's own directory is not a root;
    (2) denylist, applied even inside the allowlist: no path through a
        ledger/ rulebook/ architecture/ notes/ sop/ memory/ tickets/ tools/
        directory, and no file named CLAUDE.md, README.md, HANDOFF.md or
        MEMORY.md.

No LLM, no embeddings, no network. Standard library only.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

STORE_VERSION = 1
DELTA_VERSION = 1
DEFAULT_DEDUP_THRESHOLD = 0.8
UPDATE_SHRINK_RATIO = 0.5

VALID_OPS = ("ADD", "ADD_SECTION", "UPDATE", "RETIRE", "TAG")

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*\S)[ \t]*$")
_HEADING_LINE_RE = re.compile(r"(?m)^#{1,6}[ \t]+\S")
_LIST_START_RE = re.compile(r"^(?:[-*+]|\d+[.)])[ \t]+")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(```|~~~)")
_TICKET_RE = re.compile(r"\bT-\d+\b")
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
_PATH_RE = re.compile(r"(?:~|\.{1,2})?/[\w.@+-]+(?:/[\w.@*+{}-]+)+|\b[\w-]+\.(?:py|sh|md|json|rs|ya?ml|toml|log|txt)\b")
_CODE_RE = re.compile(r"`[^`\n]+`")

DEFAULT_LOCKS = {
    # anchored: a section ABOUT basics/rules/lessons, not any heading that mentions "rule"
    "sections": [r"(?i)^(?:basics|rules?|lessons?|gotchas)\b"],
    "items": [r"(?i)^(?:(?:[-*+]|\d+[.)])[ \t]+)?(?:\*\*)?(?:RULE|Lesson|Rules to remember)\b"],
}

PKG_DIR = Path(__file__).resolve().parent
_FORBIDDEN_BASENAMES = {"handoff.md", "memory.md", "claude.md", "readme.md"}
_FORBIDDEN_DIRS = {"ledger", "rulebook", "architecture", "notes", "sop", "memory", "tickets", "tools"}
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
RECONCILE_REASON = "hand-edit, needs review"
RECONCILE_PAIR_JACCARD = 0.5
REPLACE_REASON = "locked-section replace (T-2819 ruling b): restated by delta {did}"
ADVISORY_KINDS = frozenset({"locked_replace_advisory"})  # shown in review/warnings, never rejected


class DeltaError(ValueError):
    """A delta is malformed or cannot apply; nothing was changed."""


class InvariantError(RuntimeError):
    """A post-condition of the merge failed (a bug, never a user error)."""


# ------------------------------------------------------------------ helpers
def _sha(s: str, n: int = 10) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:n]


def tokens(text: str) -> set[str]:
    """Normalized token set for dedup: lowercase, alnum runs (keeping T-1234,
    file.py, a_b joined), tokens of one character dropped."""
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) > 1}


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


def load_bearing_tokens(text: str) -> set[str]:
    """Ticket ids, paths and `code` spans: what an UPDATE must not drop unnoticed."""
    return set(_TICKET_RE.findall(text)) | set(_PATH_RE.findall(text)) | set(_CODE_RE.findall(text))


def _strip_heading(raw: str) -> str:
    m = _HEADING_RE.match(raw.strip("\n"))
    return m.group(2) if m else raw.strip()


def store_digest(store: dict) -> str:
    """Digest of the active content (ids + text, in order) — what a delta's
    base_digest pins so a delta cannot silently apply to a different store."""
    h = hashlib.sha256()
    for it in store["items"]:
        if it["status"] == "active":
            h.update(it["id"].encode())
            h.update(b"\0")
            h.update(it["text"].encode())
            h.update(b"\1")
    return h.hexdigest()[:16]


def _norm_heading(stem: str) -> str:
    """Ruling (b) comparison key: strip ONE trailing parenthetical, collapse
    whitespace, case-fold. A heading that is only a parenthetical keeps it."""
    s = " ".join(stem.split())
    cut = _TRAILING_PAREN_RE.sub("", s, count=1)
    return (cut or s).casefold()


def load_done_tickets(tickets_root: str | Path) -> set[str]:
    """Ids of terminal tickets: the T-NNNN.md files in ``tickets_root/done/``,
    read now (ruling a: 'in tickets/done at check time')."""
    done = Path(tickets_root) / "done"
    if not done.is_dir():
        raise DeltaError(f"tickets root {tickets_root} has no done/ directory")
    return {p.stem for p in done.glob("T-*.md") if re.fullmatch(r"T-\d+", p.stem)}


def _chronicle_root() -> Path | None:
    """The Chronicle root per tools/chronicle.py (env read at call time), or
    None if that module cannot be imported (fail closed: one root fewer)."""
    try:
        try:
            from tools import chronicle  # type: ignore
        except ImportError:
            if str(PKG_DIR) not in sys.path:
                sys.path.insert(0, str(PKG_DIR))
            import chronicle  # type: ignore
        return Path(chronicle.chronicle_root()).resolve()
    except Exception:  # noqa: BLE001 - any import/env problem means no extra root
        return None


def check_output_path(path: str | Path, scratch_roots: list[str | Path] | None = None) -> Path:
    """Layer 1, allowlist: an explicit root (scratch roots, which the CLI
    builds from --scratch-root and propose's --out-dir) or the Chronicle root.
    Layer 2, denylist (also inside the allowlist): canonical dirs and file names."""
    p = Path(path).resolve()
    roots = [Path(r).resolve() for r in (scratch_roots or [])]
    chron = _chronicle_root()
    if chron is not None:
        roots.append(chron)
    if not any(root == p or root in p.parents for root in roots):
        raise PermissionError(f"refusing to write outside the given scratch roots and the Chronicle root: {p}")
    if p.name.lower() in _FORBIDDEN_BASENAMES:
        raise PermissionError(f"refusing to write canonical-looking file {p.name}: this tool only proposes")
    bad = [part for part in p.parts[:-1] if part.lower() in _FORBIDDEN_DIRS]
    if bad:
        raise PermissionError(f"refusing to write under canonical dir {bad[0]!r}/: {p}")
    if chron is not None and (chron == p or chron in p.parents) and p.exists():
        # r2 (L3): Chronicle files are never overwritten (same as chronicle.py companions)
        raise PermissionError(f"refusing to overwrite an existing file under the Chronicle root: {p}")
    return p


# -------------------------------------------------------------------- parse
def _split_blocks(text: str) -> list[tuple[str, str]]:
    """Split markdown into (kind, raw) blocks. Concatenating every raw gives
    back the input exactly. Kinds: heading, list, para, code, blank."""
    lines = text.splitlines(keepends=True)
    blocks: list[list] = []  # [kind, raw]
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        bare = line.rstrip("\n")
        if not bare.strip():
            if blocks:
                blocks[-1][1] += line
            else:
                blocks.append(["blank", line])
            i += 1
            continue
        if _FENCE_RE.match(bare):
            fence = _FENCE_RE.match(bare).group(1)
            raw = line
            i += 1
            while i < n:
                raw += lines[i]
                if lines[i].strip().startswith(fence):
                    i += 1
                    break
                i += 1
            blocks.append(["code", raw])
            continue
        if _HEADING_RE.match(bare):
            blocks.append(["heading", line])
            i += 1
            continue
        kind = "list" if _LIST_START_RE.match(bare) else "para"
        raw = line
        i += 1
        while i < n:
            nxt = lines[i].rstrip("\n")
            if not nxt.strip() or _HEADING_RE.match(nxt) or _FENCE_RE.match(nxt):
                break
            if _LIST_START_RE.match(nxt):  # a new top-level item (indent 0)
                break
            raw += lines[i]
            i += 1
        blocks.append([kind, raw])
    return [(k, r) for k, r in blocks]


def _assign_ids(bases: list[str]) -> list[str]:
    """Disambiguate duplicate base ids counting from the END of the document,
    so that prepending a block (the dominant HANDOFF edit) never shifts the id
    of an older duplicate. The last occurrence keeps the bare base id."""
    out = [""] * len(bases)
    seen: dict[str, int] = {}
    for k in range(len(bases) - 1, -1, -1):
        b = bases[k]
        c = seen.get(b, 0) + 1
        seen[b] = c
        out[k] = b if c == 1 else f"{b}-{c}"
    return out


def parse(text: str, *, source: str = "parse") -> dict:
    """Parse a HANDOFF into a store. Ids are content-derived and stable:
    parsing the same text twice gives the same ids, and a block that survives
    verbatim into a later snapshot keeps its id (same section, same text),
    including when a duplicate heading or block is prepended above it."""
    blocks = _split_blocks(text)
    bodies = [raw.rstrip() for _, raw in blocks]
    head_idx = [k for k, (kind, _) in enumerate(blocks) if kind == "heading"]
    head_ids = _assign_ids(["s-" + _sha(_strip_heading(bodies[k])) for k in head_idx])
    sid_of = dict(zip(head_idx, head_ids))
    sections, cur = [], "s-root"
    for k, (kind, _) in enumerate(blocks):
        if kind == "heading":
            cur = sid_of[k]
        sections.append(cur)
    bullet_idx = [k for k, (kind, _) in enumerate(blocks) if kind != "heading"]
    bullet_ids = _assign_ids(["b-" + _sha(sections[k] + "\0" + bodies[k]) for k in bullet_idx])
    id_of = dict(sid_of)
    id_of.update(zip(bullet_idx, bullet_ids))

    items: list[dict] = []
    for k, (kind, raw) in enumerate(blocks):
        body = bodies[k]
        item = {
            "id": id_of[k],
            "kind": kind,
            "section": sections[k],
            "text": body,
            "trail": raw[len(body):],
            "status": "active",
            "origin": source,
            "helpful": 0,
            "harmful": 0,
            "seen": 1,
            "tickets": sorted(set(_TICKET_RE.findall(body))),
            "history": [],
        }
        if kind == "heading":
            item["level"] = len(_HEADING_RE.match(body).group(1))
        items.append(item)
    return {"store_version": STORE_VERSION, "items": items, "deltas_applied": []}


# ------------------------------------------------------------------- render
def _render_items(items: list[dict], joins: set[int] = frozenset()) -> str:
    out: list[str] = []
    prev_touched = False
    for k, it in enumerate(items):
        touched = it["origin"] != "parse" or bool(it["history"])
        if out and it["kind"] == "heading" and (touched or prev_touched or k in joins):
            if not out[-1].endswith("\n\n"):
                out[-1] = out[-1].rstrip("\n") + "\n\n"
        out.append(it["text"] + it["trail"])
        prev_touched = touched
    return "".join(out)


def _topic_order(items: list[dict], lk: dict) -> list[list[dict]]:
    """Units in topic order: the preamble (items before the first heading),
    then every durable unit (a heading matching a section lock pattern plus
    the deeper-level headings nested under it), then everything else, each
    group in its existing order."""
    chunks: list[list[dict]] = []
    for it in items:
        if it["kind"] == "heading" or not chunks:
            chunks.append([])
        chunks[-1].append(it)
    pre = [chunks.pop(0)] if chunks and chunks[0][0]["kind"] != "heading" else []
    pinned: list[list[dict]] = []
    rest: list[list[dict]] = []
    k = 0
    while k < len(chunks):
        head = chunks[k][0]
        if any(p.search(_strip_heading(head["text"])) for p in lk["sections"]):
            unit = list(chunks[k])
            k += 1
            while k < len(chunks) and chunks[k][0].get("level", 1) > head.get("level", 1):
                unit += chunks[k]
                k += 1
            pinned.append(unit)
        else:
            rest.append(chunks[k])
            k += 1
    return pre + pinned + rest


def render(store: dict, *, topic: bool = False, locks: dict | None = None) -> str:
    """Render active items in order. Parsed, untouched items reproduce their
    original bytes. A heading that follows a new or edited item gets a blank
    line in front if it would otherwise be glued to the previous block.

    topic=True (T-5348) pins durable sections first: those whose heading
    matches a section lock pattern (default Basics / Rules / Lessons /
    Gotchas, case-insensitive), each with its nested deeper headings; the
    rest follow in their existing order. Deterministic; when nothing moves the
    output equals the default render. Where units are re-joined a blank line
    separates them, and the document keeps its original trailing newlines."""
    items = [it for it in store["items"] if it["status"] == "active"]
    default = _render_items(items)
    if not topic:
        return default
    units = _topic_order(items, compile_locks(locks))
    order = [it for u in units for it in u]
    if [it["id"] for it in order] == [it["id"] for it in items]:
        return default
    pos = {it["id"]: n for n, it in enumerate(items)}
    joins = {n for n in range(1, len(order)) if pos[order[n]["id"]] != pos[order[n - 1]["id"]] + 1}
    out = _render_items(order, joins)
    return out.rstrip("\n") + default[len(default.rstrip("\n")):]


# -------------------------------------------------------------------- locks
def compile_locks(spec: dict | None) -> dict:
    spec = DEFAULT_LOCKS if spec is None else spec
    return {"sections": [re.compile(p) for p in spec.get("sections", [])],
            "items": [re.compile(p) for p in spec.get("items", [])]}


def in_locked_region(store: dict, it: dict, locks: dict) -> bool:
    if any(p.search(it["text"]) for p in locks["items"]):
        return True
    heads = {x["id"]: x for x in store["items"] if x["kind"] == "heading"}
    sec = heads.get(it["section"])
    return bool(sec and any(p.search(_strip_heading(sec["text"])) for p in locks["sections"]))


# -------------------------------------------------------------------- delta
def _find_section(store: dict, ref: str, lk: dict | None = None) -> dict:
    """Resolve a section by id or by exact heading text (with or without
    leading #s). Exactly one active match is required. Only if the exact
    match finds nothing, a LOCKED section is matched by the ruling-(b) key."""
    heads = [it for it in store["items"] if it["kind"] == "heading" and it["status"] == "active"]
    by_id = [h for h in heads if h["id"] == ref]
    if by_id:
        return by_id[0]
    want = _strip_heading(ref) if ref.lstrip().startswith("#") else ref.strip()
    matches = [h for h in heads if _strip_heading(h["text"]) == want]
    if not matches:
        # T-5348: fall back to the ruling-(b) key, so an ADD naming a restated
        # heading ('Basics (unchanged)') lands in the section it was merged into;
        # scoped to locked sections (r2 L2), which is all ruling (b) covers
        lk = compile_locks(None) if lk is None else lk
        key = _norm_heading(want)
        matches = [h for h in heads if any(p.search(_strip_heading(h["text"])) for p in lk["sections"])
                   and _norm_heading(_strip_heading(h["text"])) == key]
    if len(matches) != 1:
        raise DeltaError(f"section {ref!r} matches {len(matches)} active headings (need exactly 1)")
    return matches[0]


def _item(store: dict, iid: str) -> dict:
    for it in store["items"]:
        if it["id"] == iid:
            return it
    raise DeltaError(f"unknown id {iid!r}")


def validate_delta(delta: dict) -> None:
    """Shape check only (no store needed). Raises DeltaError."""
    if not isinstance(delta, dict):
        raise DeltaError("delta must be a JSON object")
    if delta.get("delta_version") != DELTA_VERSION:
        raise DeltaError(f"delta_version must be {DELTA_VERSION}")
    if not isinstance(delta.get("delta_id"), str) or not delta["delta_id"].strip():
        raise DeltaError("delta_id (non-empty string) is required")
    ops = delta.get("ops")
    if not isinstance(ops, list) or not ops:
        raise DeltaError("ops must be a non-empty list")
    for n, op in enumerate(ops):
        where = f"ops[{n}]"
        if not isinstance(op, dict):
            raise DeltaError(f"{where}: must be an object")
        kind = op.get("op")
        if kind not in VALID_OPS:
            raise DeltaError(f"{where}: unknown op {kind!r} (valid: {', '.join(VALID_OPS)})")

        def need_str(field: str) -> None:
            v = op.get(field)
            if not isinstance(v, str) or not v.strip():
                raise DeltaError(f"{where} {kind}: '{field}' must be a non-empty string")

        if kind == "ADD":
            need_str("section")
            need_str("text")
            if _HEADING_LINE_RE.search(op["text"]):
                raise DeltaError(f"{where} ADD: text contains a heading line; use ADD_SECTION")
        elif kind == "ADD_SECTION":
            need_str("heading")
            if not _HEADING_RE.match(op["heading"].strip()) or "\n" in op["heading"].strip():
                raise DeltaError(f"{where} ADD_SECTION: heading must be one line starting with 1-6 '#' and a space")
            if op.get("position", "top") not in ("top", "bottom"):
                raise DeltaError(f"{where} ADD_SECTION: position must be 'top' or 'bottom'")
            if "body" in op:
                need_str("body")
                if _HEADING_LINE_RE.search(op["body"]):
                    raise DeltaError(f"{where} ADD_SECTION: body contains a heading line; "
                                     "restate one section per ADD_SECTION")
        elif kind == "UPDATE":
            need_str("id")
            need_str("text")
            need_str("reason")
        elif kind == "RETIRE":
            need_str("id")
            need_str("reason")
        elif kind == "TAG":
            need_str("id")
            for f in ("helpful", "harmful"):
                v = op.get(f, 0)
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    raise DeltaError(f"{where} TAG: '{f}' must be a non-negative int")
            if not op.get("helpful") and not op.get("harmful"):
                raise DeltaError(f"{where} TAG: needs helpful or harmful > 0")


def _new_item(kind: str, text: str, section: str, delta_id: str, *, level: int | None = None,
              id_seed: str) -> dict:
    it = {
        "id": ("s-" if kind == "heading" else "b-") + _sha(id_seed),
        "kind": kind,
        "section": section,
        "text": text,
        "trail": "\n",
        "status": "active",
        "origin": f"delta:{delta_id}",
        "helpful": 0,
        "harmful": 0,
        "seen": 1,
        "tickets": sorted(set(_TICKET_RE.findall(text))),
        "history": [],
    }
    if level is not None:
        it["level"] = level
    return it


def _unique_id(store: dict, iid: str) -> str:
    ids = {it["id"] for it in store["items"]}
    if iid not in ids:
        return iid
    k = 2
    while f"{iid}-{k}" in ids:
        k += 1
    return f"{iid}-{k}"


def _nested_live_headings(store: dict, head: dict) -> list[str]:
    """Active headings nested under `head` (deeper level, before the next
    heading at the same or a shallower level)."""
    items = store["items"]
    k = next(i for i, x in enumerate(items) if x["id"] == head["id"])
    out = []
    for x in items[k + 1:]:
        if x["kind"] != "heading":
            continue
        if x.get("level", 1) <= head.get("level", 1):
            break
        if x["status"] == "active":
            out.append(x["id"])
    return out


def _section_body_items(store: dict, head: dict) -> list[dict]:
    """Active non-heading items directly in ``head``'s section, in order."""
    return [x for x in store["items"] if x["section"] == head["id"] and x["kind"] != "heading"
            and x["status"] == "active"]


def _body_bytes(text: str) -> int:
    """Byte count of a section body as content: trailing blank lines (the
    separator before the next heading) are not counted."""
    t = text.rstrip()
    return len((t + "\n").encode("utf-8")) if t else 0


def _body_blocks(body: str) -> list[tuple[str, str, str]]:
    """(kind, text, trail) for every non-blank block of a section body."""
    return [(k, raw.rstrip(), raw[len(raw.rstrip()):]) for k, raw in _split_blocks(body) if raw.strip()]


def apply_delta(store: dict, delta: dict, *, dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD,
                ts: str | None = None, open_tickets: set[str] | None = None,
                strict: bool = False, locks: dict | None = None,
                done_tickets: set[str] | None = None,
                tickets_root: str | Path | None = None) -> tuple[dict, dict]:
    """Apply a delta deterministically. Returns (new_store, report). The input
    store is not modified. Any error raises DeltaError and nothing applies.

    open_tickets: ids of tickets still open (enables the last-open-mention
    guard). strict: every guard flag rejects instead of warning; requires
    open_tickets (an empty set is fine). locks: lock-pattern spec (default
    DEFAULT_LOCKS); UPDATE/RETIRE of a locked item is always rejected.
    done_tickets / tickets_root (ruling a): the terminal-ticket set, given
    directly or read from tickets_root/done/ now; an UPDATE that drops a
    done id is not an update_token_loss. The curator stays pure apart from
    that one explicit read: ticket state is an input, not a lookup."""
    if strict and open_tickets is None:
        raise DeltaError("strict mode needs an open-ticket set (pass an empty one if none are open)")
    if done_tickets is None and tickets_root is not None:
        done_tickets = load_done_tickets(tickets_root)
    exempt_ids = set(done_tickets or ()) - set(open_tickets or ())
    validate_delta(delta)
    if delta.get("base_digest") and delta["base_digest"] != store_digest(store):
        raise DeltaError(f"base_digest {delta['base_digest']} does not match store {store_digest(store)}")
    if delta["delta_id"] in store.get("deltas_applied", []):
        raise DeltaError(f"delta {delta['delta_id']} was already applied to this store")
    lk = compile_locks(locks)

    new = copy.deepcopy(store)
    did = delta["delta_id"]
    stamp = ts or delta.get("ts") or ""
    report = {"delta_id": did, "added": [], "added_sections": [], "updated": [], "retired": [],
              "tagged": [], "deduped": [], "warnings": [], "merged_sections": [],
              "replaced_sections": [], "exempt_terminal": []}
    retired_here: set[str] = set()
    # T-5348 r2 (M1): an ADD never dedups against an item this same delta
    # RETIREs, wherever the RETIRE sits in the op order; otherwise moved
    # content is skipped as a duplicate and then tombstoned, ending up nowhere
    to_retire = {op.get("id") for op in delta["ops"] if op.get("op") == "RETIRE"}

    def add_item(n: int, sec: dict, text: str, as_list: bool, evidence=None) -> None:
        text = text.rstrip()
        best_id, best = None, 0.0
        for it in new["items"]:
            if it["status"] != "active" or it["kind"] == "heading" or it["id"] in to_retire:
                continue
            s = jaccard(text, it["text"])
            if s > best:
                best_id, best = it["id"], s
        if best_id is not None and best >= dedup_threshold:
            _item(new, best_id)["seen"] += 1
            report["deduped"].append({"op": n, "matched": best_id, "jaccard": round(best, 3),
                                      "text": text[:120]})
            return
        kind_of = "list" if _LIST_START_RE.match(text) else "para"
        if kind_of == "para" and as_list and "\n" not in text:
            text = "- " + text
            kind_of = "list"
        it = _new_item(kind_of, text, sec["id"], did, id_seed=sec["id"] + "\0" + text)
        it["id"] = _unique_id(new, it["id"])
        if evidence:
            it["evidence"] = evidence
        last = max(k for k, x in enumerate(new["items"]) if x["section"] == sec["id"])
        prev = new["items"][last]
        if prev["kind"] != "heading" and prev["trail"].count("\n") > 1:
            it["trail"], prev["trail"] = prev["trail"], "\n"
        new["items"].insert(last + 1, it)
        report["added"].append({"id": it["id"], "section": sec["id"], "text": text[:120]})

    def replace_locked_section(n: int, head: dict, heading: str, body: str) -> None:
        """Ruling (b): the restated body replaces the locked section's body."""
        old_items = _section_body_items(new, head)
        old_text = "".join(x["text"] + x["trail"] for x in old_items)
        if old_text.rstrip() == body.rstrip():
            report["merged_sections"].append({"op": n, "id": head["id"], "heading": heading})
            return
        reason = REPLACE_REASON.format(did=did)
        for x in old_items:
            x["status"] = "retired"
            x["retired"] = {"delta": did, "ts": stamp, "reason": reason, "superseded_by": head["id"]}
            retired_here.add(x["id"])
            report["retired"].append({"id": x["id"], "reason": reason, "tickets": x["tickets"],
                                      "text": x["text"][:120]})
        blocks = _body_blocks(body)
        last = max(k for k, x in enumerate(new["items"]) if x["section"] == head["id"])
        tail = old_items[-1]["trail"] if old_items else "\n"
        new_ids = []
        for j, (bkind, text, trail) in enumerate(blocks):
            it = _new_item(bkind, text, head["id"], did, id_seed=head["id"] + "\0" + text)
            it["id"] = _unique_id(new, it["id"])
            # keep the body's own blank lines between blocks; the last block takes
            # the old body's separator to whatever follows the section
            it["trail"] = (trail or "\n") if j < len(blocks) - 1 else (tail if "\n" in tail else "\n")
            new["items"].insert(last + 1 + j, it)
            new_ids.append(it["id"])
        old_b, new_b = _body_bytes(old_text), _body_bytes(body)
        report["replaced_sections"].append({
            "op": n, "id": head["id"], "heading": head["text"], "restated_as": heading,
            "old_bytes": old_b, "new_bytes": new_b,
            "old_items": [x["id"] for x in old_items], "new_items": new_ids})
        # T-5348 r2 (L1): advisory only (ADVISORY_KINDS never reject, even under
        # strict); ruling (b) itself stays latest-wins
        dropped = sorted(set(_TICKET_RE.findall(old_text)) - set(_TICKET_RE.findall(body)) - exempt_ids)
        shrunk = new_b < UPDATE_SHRINK_RATIO * old_b
        if dropped or shrunk:
            w = {"kind": "locked_replace_advisory", "id": head["id"], "dropped_tickets": dropped,
                 "old_bytes": old_b, "new_bytes": new_b}
            if shrunk:
                w["ratio"] = round(new_b / old_b, 3)
            report["warnings"].append(w)

    for n, op in enumerate(delta["ops"]):
        kind = op["op"]
        where = f"ops[{n}] {kind}"
        if kind in ("UPDATE", "RETIRE"):
            target = _item(new, op["id"])
            if in_locked_region(new, target, lk):
                raise DeltaError(f"{where}: {op['id']} is in a locked region (ORG-074 gate 1); "
                                 "changing it needs ratification, not a delta")
        if kind == "ADD_SECTION":
            heading = op["heading"].strip()
            stem = _strip_heading(heading)
            body = op.get("body")
            heads = [x for x in new["items"] if x["kind"] == "heading" and x["status"] == "active"]
            locked_match = [x for x in heads
                            if any(p.search(_strip_heading(x["text"])) for p in lk["sections"])
                            and _norm_heading(_strip_heading(x["text"])) == _norm_heading(stem)]
            if len(locked_match) > 1:
                raise DeltaError(f"{where}: {heading!r} matches {len(locked_match)} locked sections "
                                 f"{[x['id'] for x in locked_match]}; cannot tell which to replace")
            if locked_match:
                if body is None:  # re-emitted heading, no restatement: merge = no-op
                    report["merged_sections"].append({"op": n, "id": locked_match[0]["id"], "heading": heading})
                else:
                    replace_locked_section(n, locked_match[0], heading, body)
                continue
            existing = [x for x in heads if _strip_heading(x["text"]) == stem]
            if existing:
                # a rewrite re-emitting a heading we already hold: merge = no-op;
                # later ADDs (and this op's body, as ADDs) land in that section
                report["merged_sections"].append({"op": n, "id": existing[0]["id"], "heading": heading})
                sec = existing[0]
            else:
                sec = _new_item("heading", heading, "", did, level=len(_HEADING_RE.match(heading).group(1)),
                                id_seed=stem)
                sec["id"] = _unique_id(new, sec["id"])
                sec["section"] = sec["id"]
                if op.get("position", "top") == "top":
                    idx = next((k for k, x in enumerate(new["items"]) if x["kind"] == "heading"),
                               len(new["items"]))
                    new["items"].insert(idx, sec)
                else:
                    new["items"].append(sec)
                report["added_sections"].append({"id": sec["id"], "heading": heading})
            for _bkind, text, _trail in _body_blocks(body or ""):
                add_item(n, sec, text, as_list=False)
        elif kind == "ADD":
            add_item(n, _find_section(new, op["section"], lk), op["text"], op.get("as_list", True),
                     op.get("evidence"))
        elif kind == "UPDATE":
            it = target
            if it["status"] != "active":
                raise DeltaError(f"{where}: {op['id']} is {it['status']}, not active")
            text = op["text"].rstrip()
            if it["kind"] == "heading":
                if not _HEADING_RE.match(text) or "\n" in text:
                    raise DeltaError(f"{where}: a heading UPDATE must stay one markdown heading line")
            elif _HEADING_LINE_RE.search(text):
                raise DeltaError(f"{where}: text contains a heading line; use ADD_SECTION")
            if it["kind"] == "list" and not _LIST_START_RE.match(text):
                text = _LIST_START_RE.match(it["text"]).group(0) + text
            old = it["text"]
            if len(text) < UPDATE_SHRINK_RATIO * len(old):
                report["warnings"].append({"kind": "update_shrink", "id": it["id"],
                                           "ratio": round(len(text) / len(old), 3)})
            lost = sorted(load_bearing_tokens(old) - load_bearing_tokens(text))
            exempt = [t for t in lost if t in exempt_ids]  # ruling (a): terminal ids may go
            if exempt:
                report["exempt_terminal"].append({"id": it["id"], "dropped": exempt})
                lost = [t for t in lost if t not in exempt_ids]
            if lost:
                w = {"kind": "update_token_loss", "id": it["id"], "lost": lost}
                if open_tickets is not None:  # context for the reviewer; the flag stands either way
                    w["lost_open_tickets"] = [t for t in lost if t in open_tickets]
                report["warnings"].append(w)
            it["history"].append({"delta": did, "ts": stamp, "old_text": old, "reason": op["reason"].strip()})
            it["text"] = text
            it["tickets"] = sorted(set(_TICKET_RE.findall(text)))
            report["updated"].append({"id": it["id"], "reason": op["reason"].strip(), "text": text[:120]})
        elif kind == "RETIRE":
            it = target
            if it["status"] != "active":
                raise DeltaError(f"{where}: {op['id']} is already {it['status']}")
            if it["kind"] == "heading":
                live_kids = [x["id"] for x in new["items"]
                             if x["section"] == it["id"] and x["kind"] != "heading" and x["status"] == "active"]
                live_kids += _nested_live_headings(new, it)
                if live_kids:
                    raise DeltaError(f"{where}: section {it['id']} still has active items {live_kids}; "
                                     "retire them first (no orphaned bullets or subsections)")
            it["status"] = "retired"
            it["retired"] = {"delta": did, "ts": stamp, "reason": op["reason"].strip(),
                             "superseded_by": op.get("superseded_by")}
            retired_here.add(it["id"])
            report["retired"].append({"id": it["id"], "reason": op["reason"].strip(),
                                      "tickets": it["tickets"], "text": it["text"][:120]})
        elif kind == "TAG":
            it = _item(new, op["id"])
            if it["status"] != "active":
                raise DeltaError(f"{where}: {op['id']} is {it['status']}; TAG needs an active item")
            it["helpful"] += op.get("helpful", 0)
            it["harmful"] += op.get("harmful", 0)
            report["tagged"].append({"id": it["id"], "helpful": op.get("helpful", 0),
                                     "harmful": op.get("harmful", 0)})

    new.setdefault("deltas_applied", []).append(did)
    check_no_silent_drop(store, new, retired_here)

    if open_tickets:
        by_id = {it["id"]: it for it in new["items"]}

        def mentioned(s: dict) -> set[str]:
            out: set[str] = set()
            for it in s["items"]:
                if it["status"] == "active":
                    out |= set(_TICKET_RE.findall(it["text"]))
            return out
        # an open ticket mentioned before (or added by this delta) but by no active item after
        added_here: set[str] = set()
        for r in report["added"] + report["added_sections"]:
            added_here |= set(_TICKET_RE.findall(by_id[r["id"]]["text"]))
        for r in report["updated"]:
            for h in by_id[r["id"]]["history"]:
                if h["delta"] == did:
                    added_here |= set(_TICKET_RE.findall(h["old_text"]))
        before = (mentioned(store) | added_here) & set(open_tickets)
        for t in sorted(before - mentioned(new)):
            culprits = [r["id"] for r in report["retired"] if t in by_id[r["id"]]["tickets"]]
            culprits += [r["id"] for r in report["updated"]
                         if any(h["delta"] == did and t in _TICKET_RE.findall(h["old_text"])
                                for h in by_id[r["id"]]["history"])]
            report["warnings"].append({"kind": "last_open_mention", "ticket": t, "by": culprits})
    blocking = [w for w in report["warnings"] if w["kind"] not in ADVISORY_KINDS]
    if strict and blocking:
        kinds = sorted({w["kind"] for w in blocking})
        raise DeltaError(f"strict mode: delta raised guard flags {kinds}: {blocking}")
    report["base_digest"] = store_digest(store)
    report["new_digest"] = store_digest(new)
    return new, report


def check_no_silent_drop(old: dict, new: dict, retired_here: set[str]) -> None:
    """Every item active in OLD must be active in NEW, or retired by this
    delta with a reason. Raises InvariantError otherwise."""
    new_by_id = {it["id"]: it for it in new["items"]}
    for it in old["items"]:
        if it["status"] != "active":
            continue
        cur = new_by_id.get(it["id"])
        if cur is None:
            raise InvariantError(f"silent drop: {it['id']} vanished from the store")
        if cur["status"] == "active":
            continue
        if it["id"] not in retired_here or not cur.get("retired", {}).get("reason"):
            raise InvariantError(f"silent drop: {it['id']} became {cur['status']} without a RETIRE+reason")


# ---------------------------------------------------------------- reconcile
def _pair(cands: list[dict], want: dict, used: set[str]) -> dict | None:
    """Best unmatched store item for an edited item: shares a ticket id, or
    normalized-token Jaccard >= RECONCILE_PAIR_JACCARD; ties keep store order."""
    best, key = None, None
    wt = set(_TICKET_RE.findall(want["text"]))
    for c in cands:
        if c["id"] in used:
            continue
        shared = bool(wt & set(_TICKET_RE.findall(c["text"])))
        j = jaccard(c["text"], want["text"])
        if not shared and j < RECONCILE_PAIR_JACCARD:
            continue
        k = (shared, j)
        if key is None or k > key:
            best, key = c, k
    return best


def reconcile(store: dict, text: str, *, delta_id: str | None = None,
              locks: dict | None = None) -> tuple[dict, dict]:
    """Diff a hand-edited HANDOFF against the store; return (delta, notes).

    The delta turns the hand edit into ADD / ADD_SECTION / UPDATE / RETIRE
    ops, each with reason RECONCILE_REASON, pinned to the store by
    base_digest. Nothing is applied: the delta goes through apply/propose
    later, with their guards (strict included). Matching is by content
    (section + text), so a store that has moved on through deltas reconciles
    cleanly against its own render. Locked content is never retired here:
    a locked section missing from the edit is kept (noted); a locked section
    whose body was edited becomes an ADD_SECTION restatement (ruling b replace
    path); a locked item outside a locked section that was removed or edited
    is kept and noted. ``ops`` is empty when the two are in sync."""
    lk = compile_locks(locks)
    did = delta_id or "reconcile-" + _sha(store_digest(store) + "\0" + text)
    notes: dict = {"locked": [], "skipped_preamble": []}
    ops_sections, ops_add, ops_update, ops_retire_items, ops_retire_heads = [], [], [], [], []

    def op(**kw) -> dict:
        kw["reason"] = RECONCILE_REASON
        return kw

    def sec_is_durable(h: dict) -> bool:
        return any(p.search(_strip_heading(h["text"])) for p in lk["sections"])

    s_items = [it for it in store["items"] if it["status"] == "active"]
    s_heads = [it for it in s_items if it["kind"] == "heading"]
    e_items = parse(text, source="reconcile")["items"]
    e_heads = [it for it in e_items if it["kind"] == "heading"]

    # 1. headings: exact text first (document order), then locked by ruling-(b) key
    smap: dict[str, str] = {"s-root": "s-root"}  # edited section id -> store section id
    used: set[str] = set()
    for eh in e_heads:
        m = next((sh for sh in s_heads if sh["id"] not in used and sh["text"] == eh["text"]), None)
        if m:
            smap[eh["id"]] = m["id"]
            used.add(m["id"])
    for eh in e_heads:
        if eh["id"] in smap:
            continue
        key = _norm_heading(_strip_heading(eh["text"]))
        m = [sh for sh in s_heads if sh["id"] not in used and sec_is_durable(sh)
             and _norm_heading(_strip_heading(sh["text"])) == key]
        if len(m) == 1:
            smap[eh["id"]] = m[0]["id"]
            used.add(m[0]["id"])

    # 2. locked sections: replace-path restatement, or kept when omitted
    locked_secs = {sh["id"] for sh in s_heads if sec_is_durable(sh)}
    rev = {}
    for e_id, s_id in smap.items():
        rev.setdefault(s_id, e_id)
    for sh in s_heads:
        if sh["id"] not in locked_secs:
            continue
        if sh["id"] not in rev:
            notes["locked"].append(f"{sh['id']} {sh['text']!r}: locked section absent from the hand edit; "
                                   "kept (never removed by omission)")
            continue
        eh = next(x for x in e_heads if x["id"] == rev[sh["id"]])
        s_body = "".join(x["text"] + x["trail"] for x in _section_body_items(store, sh))
        e_body = "".join(x["text"] + x["trail"] for x in e_items if x["section"] == eh["id"]
                         and x["kind"] != "heading")
        if s_body.rstrip() != e_body.rstrip():
            if e_body.strip():
                ops_sections.append(op(op="ADD_SECTION", heading=eh["text"], body=e_body.rstrip() + "\n"))
            else:
                notes["locked"].append(f"{sh['id']} {sh['text']!r}: body emptied by the hand edit; kept "
                                       "(never removed by omission)")

    # 3. everything else, content-matched within the mapped section
    e_locked_secs = {e for e, s in smap.items() if s in locked_secs}
    s_rest = [it for it in s_items if it["kind"] != "heading" and it["section"] not in locked_secs]
    e_rest = [it for it in e_items if it["kind"] != "heading" and it["section"] not in e_locked_secs]
    s_left = list(s_rest)
    e_left = []
    for ei in e_rest:
        sec = smap.get(ei["section"])
        m = next((si for si in s_left if si["section"] == sec and si["text"] == ei["text"]), None)
        if m:
            s_left.remove(m)
        else:
            e_left.append(ei)

    new_heads = [eh for eh in e_heads if eh["id"] not in smap]
    gone_heads = [sh for sh in s_heads if sh["id"] not in used]
    # a renamed (unlocked) heading: pair it, so its section keeps its items
    paired_used: set[str] = set()
    for eh in list(new_heads):
        m = _pair(gone_heads, eh, paired_used)
        if m is None:
            continue
        paired_used.add(m["id"])
        smap[eh["id"]] = m["id"]
        new_heads.remove(eh)
        ops_update.append(op(op="UPDATE", id=m["id"], text=eh["text"]))
    gone_heads = [sh for sh in gone_heads if sh["id"] not in paired_used]

    first_kept = next((k for k, x in enumerate(e_items) if x["kind"] == "heading" and x["id"] in smap), None)
    top = [eh for eh in new_heads if first_kept is not None and e_items.index(eh) < first_kept]
    for eh in reversed(top):  # each 'top' insert lands above the previous one
        ops_sections.append(op(op="ADD_SECTION", heading=eh["text"], position="top"))
    for eh in new_heads:
        if eh not in top:
            ops_sections.append(op(op="ADD_SECTION", heading=eh["text"], position="bottom"))
    new_head_stems = {eh["id"]: _strip_heading(eh["text"]) for eh in new_heads}

    s_used: set[str] = set()
    for ei in e_left:
        sec = smap.get(ei["section"])
        cands = [si for si in s_left if si["section"] == sec] if sec else []
        m = _pair(cands, ei, s_used)
        if m is not None and in_locked_region(store, m, lk):
            s_used.add(m["id"])
            notes["locked"].append(f"{m['id']}: locked item hand-edited; kept as is, the edit is not carried "
                                   "(changing it needs ratification)")
            continue
        if m is not None:
            s_used.add(m["id"])
            ops_update.append(op(op="UPDATE", id=m["id"], text=ei["text"]))
            continue
        if ei["section"] == "s-root":
            notes["skipped_preamble"].append(ei["text"][:120])
            continue
        ref = sec if sec else new_head_stems.get(ei["section"])
        ops_add.append(op(op="ADD", section=ref, text=ei["text"], as_list=False))
    for si in s_left:
        if si["id"] in s_used:
            continue
        if in_locked_region(store, si, lk):
            notes["locked"].append(f"{si['id']}: locked item absent from the hand edit; kept "
                                   "(never removed by omission)")
            continue
        ops_retire_items.append(op(op="RETIRE", id=si["id"]))
    retiring = {o["id"] for o in ops_retire_items}
    for sh in sorted(gone_heads, key=lambda h: -h.get("level", 1)):
        if sh["id"] in locked_secs:
            continue  # noted in step 2
        kids = [x["id"] for x in _section_body_items(store, sh)] + _nested_live_headings(store, sh)
        alive = [x for x in kids if x not in retiring]
        if alive:
            # apply refuses to orphan them; the store cannot express a move
            notes["locked"].append(f"{sh['id']} {sh['text']!r}: heading removed by hand but {len(alive)} "
                                   "item(s) or subsection(s) under it are kept; heading kept")
            continue
        retiring.add(sh["id"])
        ops_retire_heads.append(op(op="RETIRE", id=sh["id"]))

    delta = {"delta_version": DELTA_VERSION, "delta_id": did, "base_digest": store_digest(store),
             # r2 (M1): item RETIREs before ADD/UPDATE, so a moved item is not deduped
             # against its own soon-retired original; heading RETIREs stay last
             "ops": ops_sections + ops_retire_items + ops_add + ops_update + ops_retire_heads}
    return delta, notes


# ------------------------------------------------------------------- review
def review_markdown(old: dict, new: dict, report: dict) -> str:
    """Human-review document: what the delta does, with reasons and guard
    flags, then the unified diff of the rendered HANDOFF."""
    lines = [f"# ACE curation proposal — delta `{report['delta_id']}`", "",
             f"Base store digest `{report['base_digest']}` -> proposed `{report['new_digest']}`. "
             "Nothing has been written to any HANDOFF; accept by copying the proposed render yourself.", ""]

    def sect(title: str, rows: list[str]) -> None:
        lines.append(f"## {title} ({len(rows)})")
        lines.extend(rows or ["- (none)"])
        lines.append("")

    sect("GUARD FLAGS (rejections under --strict; *_advisory kinds never reject)", [f"- {json.dumps(w, ensure_ascii=False)}"
                                                       for w in report.get("warnings", [])])
    sect("Re-emitted sections merged (no-op)", [f"- `{r['id']}` {r['heading']}"
                                                 for r in report.get("merged_sections", [])])
    sect("Locked sections replaced (ruling b, latest wins)",
         [f"- `{r['id']}` {r['heading']} restated as {r['restated_as']!r}: "
          f"old {r['old_bytes']} B -> new {r['new_bytes']} B "
          f"({len(r['old_items'])} items tombstoned, {len(r['new_items'])} added)"
          for r in report.get("replaced_sections", [])])
    sect("Terminal ids dropped (ruling a, exempt from update_token_loss)",
         [f"- `{r['id']}` dropped {', '.join(r['dropped'])}" for r in report.get("exempt_terminal", [])])
    sect("Added sections", [f"- `{r['id']}` {r['heading']}" for r in report["added_sections"]])
    sect("Added bullets", [f"- `{r['id']}` in `{r['section']}`: {r['text']}" for r in report["added"]])
    sect("Updated bullets", [f"- `{r['id']}` (REASON: {r['reason']}): {r['text']}" for r in report["updated"]])
    sect("Retired bullets", [f"- `{r['id']}` — REASON: {r['reason']} — was: {r['text']}"
                             for r in report["retired"]])
    sect("Deduplicated ADDs (not added)", [f"- ops[{r['op']}] ~ `{r['matched']}` (J={r['jaccard']}): {r['text']}"
                                           for r in report["deduped"]])
    sect("Counter tags", [f"- `{r['id']}` +helpful {r['helpful']} +harmful {r['harmful']}"
                          for r in report["tagged"]])
    diff = difflib.unified_diff(render(old).splitlines(keepends=True), render(new).splitlines(keepends=True),
                                fromfile="current", tofile="proposed")
    lines += ["## Rendered diff", "", "```diff", "".join(diff).rstrip("\n"), "```", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------- CLI
def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise DeltaError(f"{path}: not valid JSON ({exc})") from exc


def _dump(obj: dict, path: str, roots: list[str] | None = None) -> None:
    p = check_output_path(path, roots)
    p.write_text(json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_text(text: str, path: str, roots: list[str] | None = None) -> None:
    check_output_path(path, roots).write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse", help="HANDOFF.md -> store JSON")
    p.add_argument("handoff")
    p.add_argument("--out", required=True)

    def guard_args(x: argparse.ArgumentParser) -> None:
        x.add_argument("--dedup-threshold", type=float, default=DEFAULT_DEDUP_THRESHOLD)
        x.add_argument("--open-tickets", help="file with one open ticket id per line (enables the last-mention guard)")
        x.add_argument("--strict", action="store_true", help="reject (not warn) on any guard flag; needs --open-tickets")
        x.add_argument("--locks", help="JSON file {sections: [regex], items: [regex]} (default: Basics/Rules/Lessons)")
        x.add_argument("--done-tickets", help="file with one done ticket id per line (ruling a: an UPDATE may "
                                              "drop these ids)")
        x.add_argument("--tickets-root", help="tickets directory; its done/ is read now as the done set")

    a = sub.add_parser("apply", help="store + delta -> new store")
    a.add_argument("--store", required=True)
    a.add_argument("--delta", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--report")
    guard_args(a)
    r = sub.add_parser("render", help="store -> proposed markdown")
    r.add_argument("--store", required=True)
    r.add_argument("--out", required=True)
    pr = sub.add_parser("propose", help="apply + render + review")
    pr.add_argument("--store", required=True)
    pr.add_argument("--delta", required=True)
    pr.add_argument("--out-dir", required=True, help="explicit output dir (an allowed write root)")
    guard_args(pr)
    for x in (r, pr):
        x.add_argument("--topic", action="store_true",
                       help="pin durable (locked-pattern) sections first; default keeps document order")
    rc = sub.add_parser("reconcile", help="store + hand-edited HANDOFF -> delta for review (never applied)")
    rc.add_argument("--store", required=True)
    rc.add_argument("--handoff", required=True)
    rc.add_argument("--out", required=True)
    rc.add_argument("--delta-id")
    rc.add_argument("--locks", help="JSON file {sections: [regex], items: [regex]}")
    for x in (p, a, r, pr, rc):
        x.add_argument("--scratch-root", action="append", default=[],
                       help="directory outputs may go to (besides the Chronicle root); repeatable")
    args = ap.parse_args(argv)
    R = list(args.scratch_root)

    def kw() -> dict:
        f, d = args.open_tickets, args.done_tickets
        return {"dedup_threshold": args.dedup_threshold,
                "open_tickets": set(Path(f).read_text(encoding="utf-8").split()) if f else None,
                "strict": args.strict,
                "locks": _load(args.locks) if args.locks else None,
                "done_tickets": set(Path(d).read_text(encoding="utf-8").split()) if d else None,
                "tickets_root": args.tickets_root}

    try:
        if args.cmd == "parse":
            _dump(parse(Path(args.handoff).read_text(encoding="utf-8")), args.out, R)
        elif args.cmd == "apply":
            new, rep = apply_delta(_load(args.store), _load(args.delta), **kw())
            _dump(new, args.out, R)
            if args.report:
                _dump(rep, args.report, R)
        elif args.cmd == "render":
            _write_text(render(_load(args.store), topic=args.topic), args.out, R)
        elif args.cmd == "propose":
            old = _load(args.store)
            k = kw()
            new, rep = apply_delta(old, _load(args.delta), **k)
            od = Path(args.out_dir)
            R.append(od)
            _dump(new, str(od / "proposed-store.json"), R)
            _write_text(render(new, topic=args.topic, locks=k["locks"]), str(od / "proposed-handoff.md"), R)
            _write_text(review_markdown(old, new, rep), str(od / "review.md"), R)
        elif args.cmd == "reconcile":
            d, notes = reconcile(_load(args.store), Path(args.handoff).read_text(encoding="utf-8"),
                                 delta_id=args.delta_id, locks=_load(args.locks) if args.locks else None)
            _dump(d, args.out, R)
            for n in notes["locked"]:
                print(f"ace_curate: reconcile: {n}", file=sys.stderr)
            if notes["skipped_preamble"]:
                print(f"ace_curate: reconcile: {len(notes['skipped_preamble'])} new preamble block(s) "
                      "not carried (no section to ADD into)", file=sys.stderr)
            print(f"ace_curate: reconcile: {len(d['ops'])} op(s) for review; nothing applied", file=sys.stderr)
    except (DeltaError, PermissionError) as exc:
        print(f"ace_curate: REJECTED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
