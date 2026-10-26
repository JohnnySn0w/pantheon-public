#!/usr/bin/env python3
"""ace_carry.py — ACE phase 1: advisory carry-forward of dropped open items (T-5349).

T-2819 GO on phase 1 (knowledge/episodic/2026-10-22-ace-handoff-poc/ §§6.1, 7,
8 item 1) with the HO-ratified C3 expiry: N = 7. ORG-074 element E2, built on
tools/ace_curate.py (T-5348).

Each HANDOFF cycle (one run of ``run``) compares the previous HANDOFF (the
latest Chronicle snapshot, or --prev-file) with the new one:

  * carry:      a bullet of the previous text that the new text dropped and
                that names an OPEN ticket (tickets/inbox ∪ tickets/active,
                minus tickets/done) the new text no longer mentions is added
                to the carry, stamped with the cycle it was first carried.
  * re-confirm: a carried item whose ticket id is back in the seat's own new
                text leaves the carry silently.
  * retire:     a carried item whose ticket is now done leaves the carry with
                the reason "ticket done" (or "ticket no longer open" when its
                file is gone from inbox/active without reaching done).
  * expire:     an item still carried after N = 7 cycles (C3) is listed in
                expired-carry.md, surfaced ONCE to the HO by a mailbox send to
                orchestrator, and dropped from the carry. An item is never surfaced
                twice and an expired item is never carried again. A failed
                send stays pending and is retried on the next run; the send is
                suppressed under a test harness (live_send_suppressed()).

Retire list (T-5357, HO ruling b on T-5350): each cycle also lists the bullets
of the NEW HANDOFF (ace_curate blocks, never headings) whose ticket ids are ALL
terminal (in tickets/done at check time, the same reader as the carry). A
bullet with no ticket id is never listed. The list is advisory only: nothing
is removed, the seat still reads its unchanged HANDOFF.md, and the list is
never mailed or otherwise surfaced.

Re-mention metric (logged for T-5351, which judges option (a), substituting a
retired copy). Every bullet ever listed is TRACKED in carry-state.json under
"retire", keyed by its normalised text (norm_text: list marker stripped,
whitespace collapsed, case-folded). At each later cycle c (c > the cycle it
was first listed), a tracked bullet is counted as exactly one of:
  * still present: its normalised text is in HANDOFF(c) and was also in the
    previous processed HANDOFF. The seat simply never removed it. NOT a
    re-mention; counted as one still-present bullet-cycle.
  * re-mention, "reappeared": its normalised text is in HANDOFF(c) but was
    absent from the previous processed HANDOFF (it disappeared, then came
    back).
  * re-mention, "new_prose": it is not counted as reappeared, and a block of
    HANDOFF(c), headings included, that is NEW prose (its normalised text is
    not in the previous HANDOFF and is not itself a tracked bullet) names one
    of its ticket ids. Needs the previous HANDOFF text; with none, no
    new-prose re-mention is counted that cycle. Hits that come only from a
    heading are also counted as new_prose_heading.
  * otherwise nothing (absent and not named in new prose).
A tracked bullet scores at most one re-mention per cycle. The listing cycle
itself never scores. Each re-mention also gets ONE class, decided against the
previous processed HANDOFF as its retired copy looked at that cycle (the ids
it named, and the ids in blocks a retired copy keeps: headings and blocks with
any non-terminal id, both stored in state with that cycle's done set):
  * kept_block: an id of the bullet was still in a kept block, so option (a)
    would not have hidden it from the seat;
  * retired_only: its ids were in the previous HANDOFF only in retire-eligible
    blocks, so option (a) would have hidden them: the rate that bears on (a);
  * returned: none of its ids was in the previous HANDOFF at all (a true
    return);
  * unknown: no previous HANDOFF known (first cycle, no stored view). Logged: per bullet (rementions, reappeared, new_prose,
still_present_cycles and the class counts), totals, and a per-cycle history
(listed, listed_new, still_present, rementions, reappeared, class split). Re-mention rate for T-5351 =
totals.rementions / tracked bullets (and the share of tracked bullets with at
least one re-mention). A reopened ticket's bullet stays tracked: re-mentions
of it are real re-mentions of something the list once called retirable.
The retire sub-state is validated field by field, element types included; an
invalid one restarts alone, with one stderr line.

Outputs, ONLY as Chronicle companions (``<ts>-<uuid>.carry-forward.md``,
``.expired-carry.md``, ``.retire-list.md``, ``.carry-state.json`` beside the
seat's snapshots) or under an explicit --out-dir: carry-forward.md (a proposed
"Carried forward (advisory)" section with each item's age), expired-carry.md
(only when something expired or is pending a surface), retire-list.md (the
retire list plus the re-mention summary) and carry-state.json. It never
writes HANDOFF.md: every write passes ace_curate's two-layer path guard
(allowlist + canonical denylist). Chronicle companions are never overwritten.

Advisory only (ORG-058 human gate): nothing is written into HANDOFF.md and no
prompt changes. Per-seat opt-in: ``ace_carry_forward: true`` on the seat's
role binding in instance.yaml (artisan only); off seats get a silent no-op.
PANTHEON_ACE_CARRY=off is a kill switch, =on forces it (tests, replay).

Never-block contract (handoff_and_restart.sh calls this unattended): every
failure prints ONE stderr line and returns 0; nothing raises to the caller. A
retire-list failure (in its step or its render) skips only that cycle's retire
list and keeps the previous retire state (one stderr line); the carry, its
state and the HO send still run.

Standard library + tools/ace_curate.py, tools/chronicle.py, tools/mailbox.py.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tools import ace_curate as ac  # type: ignore
    from tools import chronicle  # type: ignore
except ImportError:  # imported top-level as `ace_carry` (tests)
    import ace_curate as ac  # type: ignore
    import chronicle  # type: ignore

EXPIRY_CYCLES = 7  # C3, ratified 2026-10-22 (T-2819 HO rulings)
STATE_VERSION = 1
SECTION_HEADING = "## Carried forward (advisory)"
OUT_NAMES = ("carry-forward.md", "expired-carry.md", "retire-list.md", "carry-state.json")
SENDER_NAME = "ace_carry"
SURFACED_KEEP = 1000  # bound on the once-only ledger kept in state
MAX_SEND_ATTEMPTS = 2  # review M1: the first send plus at most ONE resend after an unrecorded landing
MAIL_ITEM_CAP = 50  # review L4: items listed in one HO mail; the rest point at expired-carry.md
RETIRE_STATE_VERSION = 1  # state["retire"] (T-5357); an unreadable one restarts alone
RETIRE_TRACK_KEEP = 2000  # bound on tracked listed bullets (oldest-seen dropped first)
RETIRE_HISTORY_KEEP = 400  # per-cycle re-mention rows kept in state
RETIRE_RENDER_TOP = 20  # most re-mentioned bullets shown in retire-list.md
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TICKETS_ROOT = REPO_ROOT / "tickets"
DEFAULT_INSTANCE_YAML = REPO_ROOT / "instance.yaml"
_LIST_MARK_RE = re.compile(r"^(?:[-*+]|\d+[.)])[ \t]+")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fail(msg: str) -> int:
    print(f"ace_carry: skipped (non-fatal): {msg}"[:400].replace("\n", " "), file=sys.stderr)
    return 0


# ------------------------------------------------------------------- opt-in
def opted_in(agent: str | None, instance_yaml: str | Path | None = None) -> bool:
    """True when ``agent``'s role binding in instance.yaml carries
    ``ace_carry_forward: true``. PANTHEON_ACE_CARRY=off|on overrides. Any read
    or parse problem reads as off (default off)."""
    env = os.environ.get("PANTHEON_ACE_CARRY", "").strip().lower()
    if env in ("0", "off", "false", "no"):
        return False
    if env in ("1", "on", "true", "yes"):
        return True
    if not agent:
        return False
    try:
        try:
            from tools import agent_role_map as arm  # type: ignore
        except ImportError:
            import agent_role_map as arm  # type: ignore
        doc = arm.load_instance_document(Path(instance_yaml or DEFAULT_INSTANCE_YAML))
        roles = doc.get("roles") if isinstance(doc, dict) else None
        for binding in (roles or {}).values():
            if isinstance(binding, dict) and binding.get("persona") == agent:
                return binding.get("ace_carry_forward") is True
    except Exception:  # noqa: BLE001 - unreadable manifest == off
        return False
    return False


# ------------------------------------------------------------------ tickets
def _ids_in_dir(d: Path) -> set[str]:
    return {p.stem for p in d.glob("T-*.md") if re.fullmatch(r"T-\d+", p.stem)}


def _require_root(tickets_root: str | Path) -> Path:
    root = Path(tickets_root)
    if not root.is_dir():
        raise FileNotFoundError(f"tickets root {root} not found")
    return root


def done_ticket_set(tickets_root: str | Path) -> set[str]:
    return _ids_in_dir(_require_root(tickets_root) / "done")


def open_ticket_set(tickets_root: str | Path) -> set[str]:
    """inbox ∪ active, minus done (an id in both is treated as done)."""
    root = _require_root(tickets_root)
    if not (root / "active").is_dir() and not (root / "inbox").is_dir():
        raise FileNotFoundError(f"tickets root {root} has no inbox/ or active/")
    return (_ids_in_dir(root / "inbox") | _ids_in_dir(root / "active")) - _ids_in_dir(root / "done")


def ids_in(text: str) -> set[str]:
    return set(ac._TICKET_RE.findall(text or ""))


# -------------------------------------------------------------------- state
def new_state() -> dict:
    return {"state_version": STATE_VERSION, "expiry": EXPIRY_CYCLES, "cycle": 0, "items": [],
            "pending_surface": [], "surfaced": {}, "last_input": None, "updated": None}


def _valid_state(st: object) -> bool:
    if not isinstance(st, dict) or st.get("state_version") != STATE_VERSION:
        return False
    if not isinstance(st.get("cycle"), int) or not isinstance(st.get("surfaced"), dict):
        return False
    for name in ("items", "pending_surface"):
        rows = st.get(name)
        if not isinstance(rows, list):
            return False
        for r in rows:
            if not (isinstance(r, dict) and isinstance(r.get("key"), str) and isinstance(r.get("text"), str)
                    and isinstance(r.get("ids"), list) and isinstance(r.get("first_cycle"), int)):
                return False
    return True


def load_state(paths: list[Path]) -> tuple[dict, str | None]:
    """The first valid state among ``paths`` (newest first). Returns
    (state, note): note is set whenever an unreadable/malformed file was
    skipped, whether an older valid state was used or the carry starts fresh.
    The caller withholds the HO send for that cycle (review L1): the skipped
    newer file may have recorded a send the older one does not know about."""
    bad = None
    for p in paths:
        try:
            st = json.loads(Path(p).read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001 - corrupt state recovers
            bad = bad or f"{p.name}: {type(exc).__name__}"
            continue
        if _valid_state(st):
            return st, (f"skipped unreadable newer state ({bad}); using {p.name}" if bad else None)
        bad = bad or f"{p.name}: malformed state"
    return new_state(), (f"state unreadable ({bad}); starting fresh" if bad else None)


def age(state: dict, item: dict) -> int:
    """Cycles an item has been carried, counting the current one."""
    return state["cycle"] - item["first_cycle"] + 1


def _key(text: str) -> str:
    return hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]


def _digest(*texts: str) -> str:
    h = hashlib.sha1()
    for t in texts:
        h.update((t or "").encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


# ------------------------------------------------------------ state machine
def step(state: dict, prev_text: str | None, new_text: str, open_ids: set[str], done_ids: set[str],
         *, expiry: int = EXPIRY_CYCLES) -> tuple[dict, dict]:
    """Advance the carry by one HANDOFF cycle. Pure: returns (new_state,
    report); report lists carried_new / kept / reconfirmed / retired /
    expired (each a list of item dicts) and noop (same input as the last
    processed cycle: nothing changes and the cycle does not advance)."""
    st = copy.deepcopy(state)
    report = {"noop": False, "carried_new": [], "kept": [], "reconfirmed": [], "retired": [], "expired": []}
    sig = _digest(prev_text or "", new_text)
    if st.get("last_input") == sig:
        report["noop"] = True
        return st, report
    st["cycle"] += 1
    cyc = st["cycle"]
    open_ids = set(open_ids) - set(done_ids)
    new_ids = ids_in(new_text)

    kept: list[dict] = []
    for it in st["items"]:
        ids = set(it["ids"])
        reappeared = ids & new_ids
        missing = ids - new_ids
        still = missing & open_ids
        if not still:
            if reappeared:
                report["reconfirmed"].append(it)
            else:
                reason = "ticket done" if missing & done_ids else "ticket no longer open"
                report["retired"].append(dict(it, reason=reason, retired_cycle=cyc))
            continue
        it["ids"] = sorted(still)
        if cyc - it["first_cycle"] >= expiry:
            rec = dict(it, expired_cycle=cyc, expired_at=_now(), carried_cycles=cyc - it["first_cycle"])
            report["expired"].append(rec)
            st["pending_surface"].append(rec)
            continue
        kept.append(it)
        report["kept"].append(it)

    blocked = {it["key"] for it in kept} | set(st["surfaced"]) | {r["key"] for r in st["pending_surface"]}
    if prev_text:
        for it in ac.parse(prev_text)["items"]:
            if it["kind"] == "heading" or it["text"] in new_text:
                continue
            missing_open = (set(it["tickets"]) & open_ids) - new_ids
            k = _key(it["text"])
            if not missing_open or k in blocked:
                continue
            row = {"key": k, "ids": sorted(missing_open), "text": it["text"], "first_cycle": cyc,
                   "first_carried_at": _now()}
            kept.append(row)
            blocked.add(k)
            report["carried_new"].append(row)
    st["items"] = kept
    st["last_input"] = sig
    st["updated"] = _now()
    st["expiry"] = expiry
    return st, report


# ------------------------------------------------------------------ surface
def live_send_suppressed() -> bool:
    """Same contract as inbox_consumer._live_telegram_suppressed(): True under
    a test harness (unittest/pytest loaded, or PYTEST_CURRENT_TEST);
    PANTHEON_DISABLE_LIVE_TELEGRAM=1/0 forces it either way."""
    override = os.environ.get("PANTHEON_DISABLE_LIVE_TELEGRAM", "").strip().lower()
    if override in ("1", "true", "yes"):
        return True
    if override in ("0", "false", "no"):
        return False
    return "unittest" in sys.modules or "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _mailbox_module():
    try:
        from tools import mailbox  # type: ignore
    except ImportError:
        import mailbox  # type: ignore
    return mailbox


def _orchestrator_inbox() -> str:
    env = os.environ.get("PANTHEON_ORCHESTRATOR_MAILBOX")
    if env:
        return env
    try:
        from tools.agent_role_map import mailbox_hub  # type: ignore
    except ImportError:
        from agent_role_map import mailbox_hub  # type: ignore
    return f"{mailbox_hub()}/orchestrator"


def _in_test_context() -> bool:
    return "unittest" in sys.modules or "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _live_sender(inbox, payload, sender, **kw):
    """mailbox.send to orchestrator, or None (nothing sent) under a test harness."""
    if live_send_suppressed():
        if not _in_test_context():
            print("ace_carry: HO surface suppressed by PANTHEON_DISABLE_LIVE_TELEGRAM; nothing sent, "
                  "items stay pending", file=sys.stderr)
        return None
    return _mailbox_module().send(inbox, payload, sender, **kw)


def _short(text: str, n: int = 220) -> str:
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 1] + "…"


def batch_id(rows: list[dict]) -> str:
    """Stable id of a surface batch: the same pending set always gives the
    same id (and the same subject and payload), so a resend of a batch whose
    first send landed unrecorded is content-identical."""
    return hashlib.sha1("\n".join(sorted(r["key"] for r in rows)).encode("utf-8")).hexdigest()[:10]


def plan_surface(state: dict, closed: list | None = None) -> tuple[str | None, list[dict]]:
    """Pick the pending items to send now and COUNT the attempt on each (the
    count is persisted BEFORE the send, review M1). An item whose earlier
    attempts may have landed unrecorded (attempts >= MAX_SEND_ATTEMPTS) is
    closed as surfaced-unconfirmed instead of being sent again; those rows
    are appended to ``closed`` (listed in expired-carry.md). Mutates."""
    pending = [r for r in state["pending_surface"] if r["key"] not in state["surfaced"]]
    rows = []
    for r in pending:
        if r.get("attempts", 0) >= MAX_SEND_ATTEMPTS:
            state["surfaced"][r["key"]] = {"at": _now(), "ids": r["ids"], "msg": None, "unconfirmed": True,
                                           "batch": r.get("batch")}
            if closed is not None:
                closed.append(r)
            print(f"ace_carry: {', '.join(r['ids'])}: {r['attempts']} send attempts not confirmed; "
                  "closing as surfaced-unconfirmed, no further send", file=sys.stderr)
        else:
            rows.append(r)
    state["pending_surface"] = rows
    if not rows:
        return None, []
    bid = batch_id(rows)
    for r in rows:
        r["attempts"] = r.get("attempts", 0) + 1
        r["batch"] = bid
    return bid, rows


def _mail_text(state: dict, agent: str, rows: list[dict]) -> str:
    lines = [f"[ace-carry] {agent}: {len(rows)} carried HANDOFF item(s) expired after "
             f"{state.get('expiry', EXPIRY_CYCLES)} cycles without being re-confirmed or retired "
             "(T-2819 C3; advisory pilot T-5349). Each is dropped from the carry and surfaced once. "
             "Please re-home or close them:"]
    for r in rows[:MAIL_ITEM_CAP]:
        lines.append(f"- {', '.join(r['ids'])}: {_short(r['text'])} (carried since cycle {r['first_cycle']})")
    if len(rows) > MAIL_ITEM_CAP:
        lines.append(f"- ... and {len(rows) - MAIL_ITEM_CAP} more, see expired-carry.md")
    return "\n".join(lines)


def _pre_delivery(exc: Exception) -> bool:
    """True only for exceptions mailbox.send raises BEFORE the message is
    renamed into new/: argument validation (ValueError), a missing inbox
    (MailboxNotFound) and an unserializable payload (TypeError, json.dumps).
    Anything else (an OSError from the write, rename or the post-rename
    _fsync_dir(new/)) may follow a delivery, so it is NOT known to be safe."""
    if isinstance(exc, (ValueError, TypeError)):
        return True
    try:
        return isinstance(exc, _mailbox_module().MailboxNotFound)
    except Exception:  # noqa: BLE001
        return type(exc).__name__ == "MailboxNotFound"


def send_batch(state: dict, agent: str, bid: str, rows: list[dict], sender=None) -> tuple[str | None, bool]:
    """One mailbox message to orchestrator for the batch. Returns (mid, not_delivered):
    mid is the message id when the send returned one; not_delivered is True
    only when the message certainly did NOT land (suppressed, the inbox could
    not be resolved, or a pre-delivery exception, see _pre_delivery). Subject
    and payload carry no timestamp, so a resend of the same batch is
    content-identical and mailbox.send's pending dedupe can collapse it while
    the first copy is unconsumed (only when sender_session matches, T-2230;
    the attempt cap is the hard bound)."""
    try:
        inbox = _orchestrator_inbox()
    except Exception as exc:  # noqa: BLE001 - nothing sent
        print(f"ace_carry: cannot resolve orchestrator's inbox, retry next run: {type(exc).__name__}: {exc}"[:300],
              file=sys.stderr)
        return None, True
    try:
        mid = (sender or _live_sender)(inbox, _mail_text(state, agent, rows), SENDER_NAME,
                                       subject=f"ace-carry expired items ({agent}) [batch {bid}]",
                                       severity="normal")
    except Exception as exc:  # noqa: BLE001
        pre = _pre_delivery(exc)
        print(f"ace_carry: surface to orchestrator failed ({'not delivered' if pre else 'delivery unknown, attempt kept'}): "
              f"{type(exc).__name__}: {exc}"[:300], file=sys.stderr)
        return None, pre
    return (str(mid), False) if mid else (None, True)


def commit_surface(state: dict, rows: list[dict], mid: str | None, not_delivered: bool = True) -> int:
    """After the send: a landed batch moves to ``surfaced``. A send known NOT
    to have landed gives its attempt back and stays pending; an outcome that
    is unknown (an exception that may follow delivery) keeps the attempt, so
    the batch is resent at most once more and then closed. Mutates."""
    if not mid:
        if not_delivered:
            for r in rows:
                r["attempts"] = max(0, r.get("attempts", 1) - 1)
        return 0
    at = _now()
    keys = {r["key"] for r in rows}
    for r in rows:
        state["surfaced"][r["key"]] = {"at": at, "ids": r["ids"], "msg": mid, "batch": r.get("batch")}
    if len(state["surfaced"]) > SURFACED_KEEP:
        keep = sorted(state["surfaced"].items(), key=lambda kv: kv[1].get("at", ""))[-SURFACED_KEEP:]
        state["surfaced"] = dict(keep)
    state["pending_surface"] = [r for r in state["pending_surface"] if r["key"] not in keys]
    return len(rows)


def surface_expired(state: dict, agent: str, sender=None) -> int:
    """In-memory plan + send + commit (library use and replay). The CLI
    persists between the steps instead (see _run). Returns items surfaced."""
    bid, rows = plan_surface(state)
    if not rows:
        return 0
    return commit_surface(state, rows, *send_batch(state, agent, bid, rows, sender))


# ------------------------------------------------------------------- render
def _bullet(text: str, prefix: str) -> str:
    lines = text.splitlines() or [""]
    first = _LIST_MARK_RE.sub("", lines[0], count=1)
    rest = ["  " + ln.lstrip() if ln.strip() else "" for ln in lines[1:]]
    return "\n".join([f"- {prefix} {first}"] + rest)


def render_carry_forward(state: dict) -> str:
    n = state.get("expiry", EXPIRY_CYCLES)
    out = [SECTION_HEADING, "",
           f"_Advisory (ace_carry, T-5349): open-ticket items a HANDOFF rewrite dropped. Mention the "
           f"ticket again to re-confirm; an item not re-confirmed or retired within {n} cycles is "
           f"surfaced once to the HO and dropped. Cycle {state['cycle']}._", ""]
    if not state["items"]:
        out.append("_Nothing carried this cycle._")
    for it in state["items"]:
        out.append(_bullet(it["text"], f"[carried {age(state, it)}/{n} · {', '.join(it['ids'])}]"))
    return "\n".join(out) + "\n"


def render_expired(state: dict, expired_now: list[dict], closed_now: list[dict] = ()) -> str:
    out = ["## Expired carry (surfaced to the HO)", ""]
    for r in expired_now:
        status = "surfaced" if r["key"] in state["surfaced"] else "pending surface"
        out.append(_bullet(r["text"], f"[cycle {r['expired_cycle']}, carried {r['carried_cycles']} cycles · "
                                      f"{', '.join(r['ids'])} · {status}]"))
    for r in state["pending_surface"]:
        if r not in expired_now:
            out.append(_bullet(r["text"], f"[expired cycle {r.get('expired_cycle')} · {', '.join(r['ids'])} · "
                                          "pending surface]"))
    if closed_now:
        out += ["", "## Closed without a confirmed send", "",
                f"_These items reached {MAX_SEND_ATTEMPTS} send attempts whose delivery was never confirmed "
                "(the mail may or may not have reached orchestrator). They are not sent again; check the HO "
                "mailbox by batch id._", ""]
        for r in closed_now:
            out.append(_bullet(r["text"], f"[batch {r.get('batch')} · {r.get('attempts')} attempts · "
                                          f"{', '.join(r['ids'])} · surfaced-unconfirmed]"))
    return "\n".join(out) + "\n"


# ------------------------------------------------------- retire list (T-5357)
REMENTION_CLASSES = ("kept_block", "retired_only", "returned")
_TID_RE = re.compile(r"T-\d+")


def norm_text(text: str) -> str:
    """Re-mention match key: the leading list marker stripped, whitespace
    collapsed, case-folded. (ace_curate has no general-text normaliser; its
    _norm_heading also strips a trailing parenthetical, which would merge
    distinct bullets, so it is not reused here.)"""
    first, _, rest = (text or "").strip().partition("\n")
    return " ".join((_LIST_MARK_RE.sub("", first, count=1) + "\n" + rest).split()).casefold()


def _nkey(text: str) -> str:
    return hashlib.sha1(norm_text(text).encode("utf-8")).hexdigest()[:16]


def _all_items(text: str | None) -> list[dict]:
    """Every ace_curate block with text, headings included."""
    if not text:
        return []
    return [it for it in ac.parse(text)["items"] if it["text"].strip()]


def _body_items(text: str | None) -> list[dict]:
    return [it for it in _all_items(text) if it["kind"] != "heading"]


def retire_candidates(text: str, done_ids: set[str]) -> list[dict]:
    """HANDOFF bullets (ace_curate blocks, not headings) whose ticket ids are
    ALL terminal (in ``done_ids``). A bullet with no ticket id is never
    listed. One row per normalised text, in document order."""
    rows, seen = [], set()
    for it in _body_items(text):
        ids = set(it["tickets"])
        if not ids or not ids <= done_ids:
            continue
        k = _nkey(it["text"])
        if k in seen:
            continue
        seen.add(k)
        rows.append({"key": k, "ids": sorted(ids), "text": it["text"]})
    return rows


def id_views(items: list[dict], done_ids: set[str]) -> tuple[set[str], set[str]]:
    """(every id named in ``items``, the ids named in a block a retired copy
    would KEEP: a heading, or a block with at least one non-terminal id)."""
    every, kept = set(), set()
    for it in items:
        ids = set(it["tickets"])
        every |= ids
        if it["kind"] == "heading" or not ids <= done_ids:
            kept |= ids
    return every, kept


def remention_class(ids: list[str], prev_every: set[str] | None, prev_kept: set[str] | None) -> str:
    """Class of one re-mention, decided against the previous HANDOFF (as its
    retired copy would have looked at that cycle): returned (no id of the
    bullet was in the previous HANDOFF at all), kept_block (an id was in a
    block a retired copy keeps), retired_only (its ids were only in
    retire-eligible blocks: option (a) would have hidden them). "unknown"
    only when no previous HANDOFF is known."""
    if prev_every is None or prev_kept is None:
        return "unknown"
    s = set(ids)
    if not s & prev_every:
        return "returned"
    return "kept_block" if s & prev_kept else "retired_only"


def _zero_totals() -> dict:
    return {"cycles": 0, "listed": 0, "listed_new": 0, "still_present": 0, "rementions": 0, "reappeared": 0,
            "new_prose": 0, "new_prose_heading": 0, **{c: 0 for c in REMENTION_CLASSES}, "unknown": 0}


def new_retire_state() -> dict:
    return {"version": RETIRE_STATE_VERSION, "tracked": {}, "history": [], "totals": _zero_totals(),
            "prev_ids": None, "prev_kept_ids": None}


def _ids_list(v: object) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and _TID_RE.fullmatch(x) for x in v)


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


_TRACKED_INTS = ("first_listed_cycle", "last_seen_cycle", "rementions", "still_present_cycles")
_TRACKED_OPT_INTS = ("reappeared", "new_prose", "last_listed_cycle", "last_remention_cycle",
                     *REMENTION_CLASSES, "unknown")


def _valid_retire(rs: object) -> bool:
    """Every field retire_step and render_retire_list read, element types
    included (review r1 L1): a value that would raise later fails here, so
    the retire sub-state restarts once instead of failing every cycle."""
    if not isinstance(rs, dict) or rs.get("version") != RETIRE_STATE_VERSION:
        return False
    if not isinstance(rs.get("tracked"), dict) or not isinstance(rs.get("history"), list):
        return False
    if not all(isinstance(h, dict) for h in rs["history"]):
        return False
    tot = rs.get("totals")
    if not isinstance(tot, dict) or not all(_is_int(tot.get(k)) for k in _zero_totals()):
        return False
    for name in ("prev_ids", "prev_kept_ids"):
        if rs.get(name) is not None and not _ids_list(rs.get(name)):
            return False
    for k, t in rs["tracked"].items():
        if not (isinstance(k, str) and isinstance(t, dict) and _ids_list(t.get("ids"))
                and isinstance(t.get("text"), str) and isinstance(t.get("present"), bool)
                and all(_is_int(t.get(f)) for f in _TRACKED_INTS)
                and all(f not in t or _is_int(t[f]) for f in _TRACKED_OPT_INTS)):
            return False
    return True


def retire_step(state: dict, prev_text: str | None, new_text: str, done_ids: set[str]) -> tuple[dict, dict]:
    """Advance the retire list and its re-mention log by one cycle; call it
    right after step() on the same inputs (state["cycle"] is the current
    cycle). Pure apart from a one-line stderr note when state["retire"] is
    invalid (that sub-state alone restarts; the carry is untouched).
    Returns (new_state, report); see the module docstring for the metric."""
    st = copy.deepcopy(state)
    rs = st.get("retire")
    if rs is not None and not _valid_retire(rs):
        print("ace_carry: retire state unreadable; re-mention log restarts (carry unaffected)", file=sys.stderr)
        rs = None
    rs = rs if rs is not None else new_retire_state()
    cyc = st["cycle"]
    done_ids = set(done_ids)
    all_items = _all_items(new_text)
    items = [it for it in all_items if it["kind"] != "heading"]
    present = {_nkey(it["text"]) for it in items}
    prev_all = _all_items(prev_text)
    prev_keys = {_nkey(it["text"]) for it in prev_all} if prev_text else None
    # the previous HANDOFF's ids, as its retired copy looked then (stored with that cycle's done set);
    # without a stored view (first cycle after a reset), fall back to prev_text with today's done set
    if rs.get("prev_ids") is not None and rs.get("prev_kept_ids") is not None:
        prev_every, prev_kept = set(rs["prev_ids"]), set(rs["prev_kept_ids"])
    elif prev_text:
        prev_every, prev_kept = id_views(prev_all, done_ids)
    else:
        prev_every = prev_kept = None
    tracked = rs["tracked"]
    listed = retire_candidates(new_text, done_ids)
    rep = {"cycle": cyc, "listed": listed, "listed_new": 0, "still_present": 0, "rementions": []}

    def remention(k: str, t: dict, how: str, heading: bool = False) -> None:
        cls = remention_class(t["ids"], prev_every, prev_kept)
        t[how] = t.get(how, 0) + 1
        t[cls] = t.get(cls, 0) + 1
        rep["rementions"].append({"key": k, "ids": t["ids"], "how": how, "class": cls, "heading": heading})

    # 1. bullets listed in an EARLIER cycle: reappeared (re-mention) or still present (not one)
    hit: set[str] = set()
    for k, t in tracked.items():
        if t["first_listed_cycle"] >= cyc:
            continue
        if k in present:
            if t["present"]:
                t["still_present_cycles"] += 1
                rep["still_present"] += 1
            else:
                hit.add(k)
                remention(k, t, "reappeared")
    # 2. new prose (a block, heading included, whose normalised text was not in the previous
    #    HANDOFF and is not itself a tracked bullet) naming an earlier-listed id
    if prev_keys is not None:
        fresh_ids: set[str] = set()
        fresh_body_ids: set[str] = set()
        for it in all_items:
            k = _nkey(it["text"])
            if k not in prev_keys and k not in tracked:
                fresh_ids |= set(it["tickets"])
                if it["kind"] != "heading":
                    fresh_body_ids |= set(it["tickets"])
        for k, t in tracked.items():
            if t["first_listed_cycle"] < cyc and k not in hit and set(t["ids"]) & fresh_ids:
                hit.add(k)
                remention(k, t, "new_prose", heading=not set(t["ids"]) & fresh_body_ids)
    for k in hit:
        tracked[k]["rementions"] += 1
        tracked[k]["last_remention_cycle"] = cyc
    # 3. presence and the newly listed
    for k, t in tracked.items():
        t["present"] = k in present
        if t["present"]:
            t["last_seen_cycle"] = cyc
    for row in listed:
        t = tracked.get(row["key"])
        if t is None:
            tracked[row["key"]] = {"ids": row["ids"], "text": _short(row["text"], 160), "first_listed_cycle": cyc,
                                   "last_seen_cycle": cyc, "present": True, "rementions": 0,
                                   "still_present_cycles": 0, "reappeared": 0, "new_prose": 0}
            rep["listed_new"] += 1
        else:
            t["last_listed_cycle"] = cyc
    if len(tracked) > RETIRE_TRACK_KEEP:
        keep = sorted(tracked.items(), key=lambda kv: (kv[1]["last_seen_cycle"], kv[1]["first_listed_cycle"]))
        rs["tracked"] = tracked = dict(keep[-RETIRE_TRACK_KEEP:])

    res = rep["rementions"]
    n_app = sum(1 for r in res if r["how"] == "reappeared")
    split = {c: sum(1 for r in res if r["class"] == c) for c in (*REMENTION_CLASSES, "unknown")}
    tot = rs["totals"]
    tot["cycles"] += 1
    tot["listed"] += len(listed)
    tot["listed_new"] += rep["listed_new"]
    tot["still_present"] += rep["still_present"]
    tot["rementions"] += len(res)
    tot["reappeared"] += n_app
    tot["new_prose"] += len(res) - n_app
    tot["new_prose_heading"] += sum(1 for r in res if r["heading"])
    for c, n in split.items():
        tot[c] += n
    rep["split"] = split
    rs["history"] = (rs["history"] + [{"cycle": cyc, "listed": len(listed), "listed_new": rep["listed_new"],
                                       "still_present": rep["still_present"], "rementions": len(res),
                                       "reappeared": n_app, **split}])[-RETIRE_HISTORY_KEEP:]
    every, kept = id_views(all_items, done_ids)
    rs["prev_ids"], rs["prev_kept_ids"] = sorted(every), sorted(kept)
    st["retire"] = rs
    return st, rep


def _split_text(d: dict) -> str:
    s = ", ".join(f"{c} {d.get(c, 0)}" for c in REMENTION_CLASSES)
    return s + (f", unknown {d['unknown']}" if d.get("unknown") else "")


def render_retire_list(state: dict, rep: dict) -> str:
    rs = state.get("retire") or new_retire_state()
    tot = rs["totals"]
    out = ["## Retire list (advisory)", "",
           f"_Advisory (ace_carry, T-5357): bullets of this HANDOFF whose ticket ids are all terminal "
           f"(in tickets/done at check time). Nothing was removed: the seat's HANDOFF.md is unchanged and "
           f"is still what the seat reads. Cycle {state['cycle']}._", ""]
    if not rep["listed"]:
        out.append("_Nothing to retire this cycle._")
    for row in rep["listed"]:
        out.append(_bullet(row["text"], f"[{', '.join(row['ids'])}]"))
    n_app = sum(1 for r in rep["rementions"] if r["how"] == "reappeared")
    tracked = len(rs["tracked"])
    with_re = sum(1 for t in rs["tracked"].values() if t["rementions"])
    out += ["", "### Re-mentions (T-5351 metric)", "",
            f"- listed this cycle: {len(rep['listed'])} (new {rep['listed_new']}, still present from an "
            f"earlier list {rep['still_present']})",
            f"- re-mentions this cycle: {len(rep['rementions'])} (reappeared {n_app}, new prose "
            f"{len(rep['rementions']) - n_app}; {_split_text(rep.get('split', {}))})",
            f"- cumulative over {tot['cycles']} cycles: {tot['rementions']} re-mentions (reappeared "
            f"{tot['reappeared']}, new prose {tot['new_prose']} of which in a heading {tot['new_prose_heading']}; "
            f"{_split_text(tot)}), {tot['still_present']} still-present bullet-cycles; {with_re} of "
            f"{tracked} tracked bullets re-mentioned at least once"]
    top = sorted((t for t in rs["tracked"].values() if t["rementions"]),
                 key=lambda t: (-t["rementions"], t["first_listed_cycle"]))[:RETIRE_RENDER_TOP]
    for t in top:
        out.append(f"  - {t['rementions']}x [{', '.join(t['ids'])}] {_short(t['text'], 120)} "
                   f"(listed since cycle {t['first_listed_cycle']})")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------- writes
def guarded_path(path: str | Path, roots: list[str | Path] | None) -> Path:
    """ace_curate's two-layer guard (allowlist + canonical denylist; never a
    HANDOFF.md / README.md / CLAUDE.md / MEMORY.md)."""
    return ac.check_output_path(path, roots)


def _companion_dest(agent_dir: Path, ts: str, uuid: str, suffix: str) -> Path:
    day = agent_dir / ts[:10]
    stem = f"{chronicle._fs_safe_ts(ts)}-{uuid}.{suffix}"
    dest, n = day / stem, 2
    while dest.exists():
        dest, n = day / f"{stem}.{n}", n + 1
    return dest


def _state_companions(agent_dir: Path) -> list[Path]:
    if not agent_dir.is_dir():
        return []
    def order(p: Path) -> tuple[str, int]:
        stem, _, n = p.name.partition(".carry-state.json")
        return stem, int(n[1:]) if n[1:].isdigit() else 1  # write_companion_text's .2, .3 ... come later
    return sorted(agent_dir.glob("*/*.carry-state.json*"), key=order, reverse=True)


def _write_plain(dest: Path, text: str) -> None:
    tmp = dest.with_name(f".{dest.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dest)


# ---------------------------------------------------------------------- CLI
def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="advance the carry by one HANDOFF cycle (non-fatal, always exit 0)")
    r.add_argument("--handoff-file", required=True, help="the NEW HANDOFF (read only)")
    r.add_argument("--prev-file", help="the previous HANDOFF (default: the latest Chronicle snapshot)")
    r.add_argument("--agent", help="persona (default: PANTHEON_ACE_CARRY_AGENT, else from the handoff path)")
    r.add_argument("--tickets-root", default=str(DEFAULT_TICKETS_ROOT))
    r.add_argument("--out-dir", help="write outputs here instead of as Chronicle companions")
    r.add_argument("--state-file", help="read the carry state from here (default: out-dir or latest companion)")
    r.add_argument("--ts", help="companion stem timestamp (default: now)")
    r.add_argument("--uuid", default="ace-carry", help="companion stem uuid")
    r.add_argument("--instance-yaml", help="per-seat opt-in manifest (default: repo instance.yaml)")
    r.add_argument("--force", action="store_true", help="run even if the seat is not opted in")
    return ap


def main(argv: list[str] | None = None, *, sender=None) -> int:
    try:
        try:
            args = _parser().parse_args(argv)
        except SystemExit as exc:
            return 0 if exc.code in (0, None) else _fail("bad arguments")
        return _run(args, sender)
    except Exception as exc:  # noqa: BLE001 - never raise into handoff_and_restart.sh
        return _fail(f"{type(exc).__name__}: {exc}")


def _run(args: argparse.Namespace, sender) -> int:
    agent = args.agent or os.environ.get("PANTHEON_ACE_CARRY_AGENT") or \
        chronicle.agent_from_handoff_path(args.handoff_file)
    if not (args.force or opted_in(agent, args.instance_yaml)):
        return 0  # default off: silent no-op
    if not agent:
        return _fail(f"cannot resolve the agent from {args.handoff_file}; pass --agent")
    try:
        new_text = Path(args.handoff_file).read_text(encoding="utf-8")
    except OSError as exc:
        return _fail(f"cannot read handoff {args.handoff_file}: {exc}")
    try:
        open_ids, done_ids = open_ticket_set(args.tickets_root), done_ticket_set(args.tickets_root)
    except OSError as exc:
        return _fail(str(exc))
    if args.prev_file:
        try:
            prev_text = Path(args.prev_file).read_text(encoding="utf-8")
        except OSError as exc:
            return _fail(f"cannot read previous handoff {args.prev_file}: {exc}")
    else:
        prev_text = chronicle.latest_handoff_text(chronicle.agent_dir_name(agent), root=chronicle.chronicle_root())

    ts = args.ts or _now()
    if args.out_dir:
        out = Path(args.out_dir)
        roots: list | None = [out]
        dests = {n: out / n for n in OUT_NAMES}
        state_paths = [Path(args.state_file)] if args.state_file else [out / "carry-state.json"]
    else:
        agent_dir = chronicle.chronicle_root() / chronicle.agent_dir_name(agent)
        roots = None
        dests = {n: _companion_dest(agent_dir, ts, args.uuid, n) for n in OUT_NAMES}
        state_paths = [Path(args.state_file)] if args.state_file else _state_companions(agent_dir)
    try:
        for d in dests.values():
            guarded_path(d, roots)
    except PermissionError as exc:
        return _fail(str(exc))

    state, note = load_state(state_paths)
    if note:
        print(f"ace_carry: {note}", file=sys.stderr)
    state, rep = step(state, prev_text, new_text, open_ids, done_ids)
    if rep["noop"]:
        print(f"ace_carry: {agent}: same HANDOFF pair as the last processed cycle; nothing to do")
        return 0
    retire_rep, retire_md = None, None
    try:  # T-5357: advisory retire list; step AND render here, so a failure never reaches persist()
        r_state, r_rep = retire_step(state, prev_text, new_text, done_ids)
        r_md = render_retire_list(r_state, r_rep)
        state, retire_rep, retire_md = r_state, r_rep, r_md
    except Exception as exc:  # noqa: BLE001 - this cycle has no retire companion; carry and its state intact
        print(f"ace_carry: retire list skipped (non-fatal): {type(exc).__name__}: {exc}"[:400].replace("\n", " "),
              file=sys.stderr)
    bid, rows, closed = None, [], []
    if state["pending_surface"]:
        if note:  # review L1: a newer state was unreadable; it may hold a send we cannot see
            print("ace_carry: HO surface withheld this cycle (state recovered after an unreadable file)",
                  file=sys.stderr)
        else:
            bid, rows = plan_surface(state, closed)
    written: set[str] = set()

    def persist(names: tuple[str, ...]) -> None:
        texts = {"carry-state.json": json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                 "carry-forward.md": render_carry_forward(state)}
        if rep["expired"] or state["pending_surface"] or rows or closed:
            texts["expired-carry.md"] = render_expired(state, rep["expired"], closed)
        if retire_md is not None and "retire-list.md" in names:
            texts["retire-list.md"] = retire_md
        for name in names:  # carry-state.json first
            if name not in texts:
                continue
            if roots is None:
                chronicle.write_companion_text(chronicle.agent_dir_name(agent), texts[name], jsonl_uuid=args.uuid,
                                               ts=ts, suffix=name)
            else:
                _write_plain(dests[name], texts[name])
            written.add(name)

    # review M1, step 1: persist the state (attempt counted, items pending) BEFORE any send.
    try:
        persist(("carry-state.json", "carry-forward.md", "expired-carry.md", "retire-list.md"))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"state write failed, nothing sent: {type(exc).__name__}: {exc}")
    surfaced = 0
    if rows:
        mid, not_delivered = send_batch(state, agent, bid, rows, sender)  # step 2
        surfaced = commit_surface(state, rows, mid, not_delivered)
        try:  # step 3: record the outcome
            persist(("carry-state.json", "expired-carry.md"))
        except Exception as exc:  # noqa: BLE001
            return _fail(f"batch {bid} {'sent' if mid else 'not sent'} but the outcome was not recorded "
                         f"({type(exc).__name__}: {exc}); at most one resend of this batch follows")
    where = args.out_dir or f"chronicle/{chronicle.agent_dir_name(agent)}/{ts[:10]}"
    print(f"ace_carry: {agent} cycle {state['cycle']}: carrying {len(state['items'])} "
          f"(+{len(rep['carried_new'])} new), re-confirmed {len(rep['reconfirmed'])}, "
          f"retired {len(rep['retired'])}, expired {len(rep['expired'])}, surfaced {surfaced}, "
          f"pending {len(state['pending_surface'])}; "
          + (f"retire-listed {len(retire_rep['listed'])}, re-mentions {len(retire_rep['rementions'])} "
             f"({_split_text(retire_rep['split'])}); " if retire_rep is not None else "")
          + f"wrote {', '.join(sorted(written))} to {where} (advisory)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
