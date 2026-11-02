"""tools/harness_idle.py — is a seat idle? Read the harness's own session log (T-5793).

keep_going (and, in a later PR, activity_monitor's P1.b poke) must nudge a parked seat
on ANY harness. The old gate scraped the pane for Claude Code's sentinels, so every
codex pane classified None and a codex seat was skipped forever (specialist 2026-11-01:
every 15-min tick 14:45Z-17:01Z logged "idle state inconclusive"). The harness already
writes a timestamped record of every turn boundary; this module reads it.

  codex  ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (the live session keeps
         appending under its START date). Turn start = event_msg task_started; end =
         task_complete or turn_aborted.
  claude ~/.claude/projects/<slug>/<session>.jsonl (top level only: subagent
         transcripts live in nested dirs). Turn start = a `user` entry carrying
         turnOrigin; end = system/turn_duration, or a "[Request interrupted by user"
         entry (no turn_duration is written on an interrupt).

Rule: IDLE iff the last boundary is a turn END, no background work is open, and no
activity for `quiet_s`. Everything else is WORKING (in a turn, or ended too recently),
BG_WAIT (the turn ended but a background agent or shell it launched is still out), or
UNKNOWN (no log, stale log, unreadable, schema drift). Callers nudge only on IDLE.

Claude background trap: turn_duration.pendingBackgroundAgentCount counts AGENTS only.
Background SHELLS are tracked as an open set: started = toolUseResult.backgroundTaskId,
finished = a task-notification naming that <task-id>, either as a user entry (turn
start) or, when it lands mid-turn, as an attachment {type: queued_command, prompt:
"<task-notification>..."}. The set is built by a separate marker scan over the last
BG_SCAN_BYTES of the file (only lines containing a marker substring are parsed), so a
long shell launched before the classification window still counts (T-5793 r1 MED-1).
A shell stays open however old it is (a cold pre-push runs 60+ min); the only release
is the quiet clock: BG_WAIT whose quiet time reaches `bg_max_s` reads IDLE, so a lost
notification cannot park the seat forever (T-5793 r2).

Quiet clock: newest `timestamp` among user/assistant/system entries (Claude) or any
record (codex). Never file mtime: Claude rewrites untimestamped metadata (ai-title,
last-prompt, mode, ...) while idle (T-5050; same set as health_export).

The screen is NOT the idle signal here; pane_inject_safe() is only the pre-inject
safety gate (modal/dialog/busy check) a caller runs after the log says IDLE.

Stdlib only, and nothing here raises: every failure is an UNKNOWN reading with a
reason. File access goes through a `reader` (LocalReader by default) whose method
shapes match health_export.SudoReader, so a cross-account caller can pass that.
"""
from __future__ import annotations

import dataclasses
import enum
import importlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

#: keep_going's quiet window. Below its ~15-min timer tick, so a seat that parked just
#: after one tick is caught on the next (design T-5793 §arc decisions).
DEFAULT_QUIET_S = 600
#: BG_WAIT past this reads IDLE. Anchored to activity_monitor.P1A_IDLE_THRESHOLD_SECONDS
#: (3600 s, its first-fire idle delay): background work that has been silent that long
#: is treated the way P1.a treats an idle seat.
DEFAULT_BG_MAX_S = 3600

#: First read of a log is this many tail bytes; widened once to WIDE_TAIL_BYTES when the
#: window holds no turn boundary at all (Claude api-request-blob lines can be large).
TAIL_BYTES = 256 * 1024
WIDE_TAIL_BYTES = 4 * 1024 * 1024
#: Background-shell marker scan window (start/finish markers only; see _bg_markers).
BG_SCAN_BYTES = 16 * 1024 * 1024
_BG_NEEDLES = (b"backgroundTaskId", b"task-id")
#: session_meta (codex line 1) carries base_instructions; ~20 KB on cli 0.147.0.
HEAD_BYTES = 64 * 1024
#: Codex: how many newest rollouts to inspect for the top-level (non-subagent) one.
CODEX_CANDIDATES = 16
#: Claude: newest transcripts by mtime to compare by activity timestamp. An idle
#: metadata rewrite can bump an OLD session's mtime (health_export.ACTIVITY_CANDIDATES).
CLAUDE_CANDIDATES = 3

_CLAUDE_ACTIVITY_TYPES = frozenset({"user", "assistant", "system"})
_INTERRUPT_MARK = "[Request interrupted by user"
_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
_CODEX_START = frozenset({"task_started"})
_CODEX_END = frozenset({"task_complete", "turn_aborted"})
# Codex busy footer: "◦ Working (2m 47s • esc to interrupt)". Bottom region only, so a
# quoted sentinel in scrollback does not count (same reason as inbox_consumer T-0716).
_BUSY_SENTINEL = "esc to interrupt"
_BOTTOM_LINES = 6

CLAUDE = "claude-code"
CODEX = "codex"


class Verdict(enum.Enum):
    IDLE = "idle"
    WORKING = "working"
    BG_WAIT = "bg_wait"
    UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class IdleReading:
    verdict: Verdict
    harness: str | None = None          # submit profile: "claude-code" | "codex" | ...
    log_path: str | None = None
    session_id: str | None = None
    last_boundary: str | None = None    # e.g. task_complete, turn_duration, interrupt, turn_start
    last_event_ts: float | None = None  # epoch of the newest activity record
    quiet_s: float | None = None        # now - last_event_ts
    open_bg: int = 0                    # open background agents + shells (Claude)
    reason: str = ""


def _unknown(reason: str, **kw: Any) -> IdleReading:
    return IdleReading(Verdict.UNKNOWN, reason=reason, **kw)


# ------------------------------------------------------------------------------ helpers
def _sibling(name: str):
    """Import a tools/ module under either identity (package `tools.X` or bare `X`),
    the dual-import shape keep_going uses for mailbox/seat_state."""
    try:
        return importlib.import_module(f"tools.{name}")
    except ImportError:
        return importlib.import_module(name)


def _parse_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def parse_jsonl(text: str, *, truncated: bool) -> list[dict]:
    """JSONL text -> dict records. `truncated` drops the (possibly cut) first line."""
    lines = text.split("\n")
    if truncated and lines:
        lines = lines[1:]
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _fmt_s(seconds: float) -> str:
    return f"{int(seconds)}s"


def _notified_ids(rec: dict) -> list[str]:
    """<task-id>s a record reports as finished: a user entry's task-notification text,
    or a mid-turn attachment {type: queued_command, prompt: "<task-notification>..."}."""
    kind = rec.get("type")
    if kind == "user":
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        text = _text_of(msg.get("content"))
    elif kind == "attachment":
        att = rec.get("attachment") if isinstance(rec.get("attachment"), dict) else {}
        if att.get("type") != "queued_command":
            return []
        text = _text_of(att.get("prompt"))
    else:
        return []
    return _TASK_ID_RE.findall(text) if "<task-notification>" in text else []


def _started_shell(rec: dict) -> tuple[str, float | None] | None:
    """(backgroundTaskId, start epoch) when a user tool_result launched a background shell."""
    if rec.get("type") != "user":
        return None
    tur = rec.get("toolUseResult")
    if isinstance(tur, dict) and isinstance(tur.get("backgroundTaskId"), str):
        return tur["backgroundTaskId"], _parse_ts(rec.get("timestamp"))
    return None


def bg_markers(records: Iterable[dict]) -> tuple[dict[str, float | None], set[str]]:
    """(started shell id -> start epoch, finished ids) over `records`. Pure."""
    started: dict[str, float | None] = {}
    notified: set[str] = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        hit = _started_shell(rec)
        if hit is not None:
            started.setdefault(hit[0], hit[1])
        notified.update(_notified_ids(rec))
    return started, notified


def _bg_markers(reader, account: str, path: str) -> tuple[dict[str, float | None], set[str]] | None:
    """The wide marker scan: start/finish markers over the last BG_SCAN_BYTES. None when
    the reader cannot scan (the caller then uses the classification window alone)."""
    grep = getattr(reader, "grep_tail", None)
    if grep is None:
        return None
    lines = grep(account, path, BG_SCAN_BYTES, _BG_NEEDLES)
    if lines is None:
        return None
    # One record per returned line, with or without a trailing newline (a `tail -c | grep -F`
    # reader may strip them; joining with "" would glue records into unparseable text).
    return bg_markers(parse_jsonl("\n".join(ln.rstrip("\r\n") for ln in lines), truncated=False))


# ------------------------------------------------------------------------------ reader
class LocalReader:
    """Direct filesystem reads as the calling account. Same method shapes as
    health_export.SudoReader (plus read_text_head); every method returns None on any
    failure. `account` is accepted for shape parity and ignored."""

    def newest_files(self, account: str | None, root: str, min_depth: int, max_depth: int,
                     suffix: str, n: int) -> list[tuple[float, str]] | None:
        del account
        rows: list[tuple[float, str]] = []

        def walk(path: str, depth: int) -> None:
            try:
                entries = list(os.scandir(path))
            except OSError:
                return
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if depth + 1 < max_depth:
                            walk(entry.path, depth + 1)
                    elif (entry.is_file(follow_symlinks=False) and depth + 1 >= min_depth
                          and entry.name.endswith(suffix)):
                        rows.append((entry.stat(follow_symlinks=False).st_mtime, entry.path))
                except OSError:
                    continue

        if not os.path.isdir(root):
            return None
        walk(root, 0)
        return sorted(rows, reverse=True)[:max(0, int(n))]

    def read_text_tail(self, account: str | None, path: str, max_bytes: int) -> str | None:
        del account
        try:
            with open(path, "rb") as handle:
                size = handle.seek(0, 2)
                handle.seek(max(0, size - int(max_bytes)))
                return handle.read(int(max_bytes)).decode("utf-8", errors="replace")
        except OSError:
            return None

    def read_text_head(self, account: str | None, path: str, max_bytes: int) -> str | None:
        del account
        try:
            with open(path, "rb") as handle:
                return handle.read(int(max_bytes)).decode("utf-8", errors="replace")
        except OSError:
            return None

    def grep_tail(self, account: str | None, path: str, max_bytes: int,
                  needles: tuple[bytes, ...]) -> list[str] | None:
        """Complete lines within the last `max_bytes` that contain any of `needles`
        (a byte-substring prefilter; nothing else is decoded). A cross-account reader
        can implement it as `tail -c N | grep -F`."""
        del account
        try:
            with open(path, "rb") as handle:
                size = handle.seek(0, 2)
                start = max(0, size - int(max_bytes))
                handle.seek(start)
                block = handle.read(int(max_bytes))
        except OSError:
            return None
        first = 0
        if start:
            cut = block.find(b"\n")  # drop the cut first line
            first = len(block) if cut < 0 else cut + 1
        spans: set[tuple[int, int]] = set()
        for needle in needles:
            if not needle:
                continue
            pos = block.find(needle, first)
            while pos >= 0:
                lo = max(block.rfind(b"\n", first, pos) + 1, first)
                hi = block.find(b"\n", pos)
                hi = len(block) if hi < 0 else hi + 1
                spans.add((lo, hi))
                pos = block.find(needle, hi)
        return [block[lo:hi].decode("utf-8", errors="replace") for lo, hi in sorted(spans)]


def _read_tail(reader, account: str, path: str, max_bytes: int) -> list[dict] | None:
    text = reader.read_text_tail(account, path, max_bytes)
    if text is None:
        return None
    return parse_jsonl(text, truncated=len(text.encode("utf-8", errors="replace")) >= max_bytes)


# --------------------------------------------------------------------------- classifiers
def classify_claude(records: Iterable[dict], now: float, *, quiet_s: float = DEFAULT_QUIET_S,
                    bg_max_s: float = DEFAULT_BG_MAX_S,
                    markers: tuple[dict[str, float | None], set[str]] | None = None) -> IdleReading:
    """Pure: Claude Code transcript records -> reading (harness/log/session unset).
    `markers` (bg_markers over a wider scan) is merged with the window's own."""
    try:
        return _classify_claude(list(records), float(now), quiet_s, bg_max_s, markers)
    except Exception as exc:  # noqa: BLE001 — never raise
        return _unknown(f"claude classifier error: {type(exc).__name__}: {exc}", harness=CLAUDE)


def _classify_claude(records: list[dict], now: float, quiet_s: float, bg_max_s: float,
                     markers) -> IdleReading:
    last_start = last_end = last_assistant = -1
    end_kind: str | None = None
    saw_turn_duration = False
    pending_agents = 0
    started, notified = bg_markers(records)
    if markers is not None:
        for sid, ts0 in markers[0].items():
            started.setdefault(sid, ts0)
        notified |= markers[1]
    newest: float | None = None
    for i, rec in enumerate(records):
        kind = rec.get("type")
        if kind not in _CLAUDE_ACTIVITY_TYPES:
            continue  # metadata / attachments / api-request blobs: not activity
        ts = _parse_ts(rec.get("timestamp"))
        if ts is not None and (newest is None or ts > newest):
            newest = ts
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        if kind == "system":
            if rec.get("subtype") == "turn_duration":
                saw_turn_duration = True
                last_end, end_kind = i, "turn_duration"
                try:
                    pending_agents = max(0, int(rec.get("pendingBackgroundAgentCount") or 0))
                except (TypeError, ValueError):
                    pending_agents = 0
        elif kind == "assistant":
            last_assistant = i
        else:  # user
            text = _text_of(msg.get("content"))
            if "turnOrigin" in rec:
                last_start = i
            elif _INTERRUPT_MARK in text:
                last_end, end_kind = i, "interrupt"
    if not saw_turn_duration:
        return _unknown("no system/turn_duration in the log window (schema-drift guard)",
                        harness=CLAUDE, last_event_ts=newest)
    if newest is None:
        return _unknown("no timestamped activity in the log window", harness=CLAUDE)
    quiet = max(0.0, now - newest)
    base = dict(harness=CLAUDE, last_event_ts=newest, quiet_s=quiet)
    if last_start > last_end:
        return IdleReading(Verdict.WORKING, last_boundary="turn_start",
                           reason=f"turn in progress (last activity {_fmt_s(quiet)} ago)", **base)
    if last_assistant > last_end:
        return IdleReading(Verdict.WORKING, last_boundary=end_kind,
                           reason="assistant activity after the last turn end (mid-tool)", **base)
    # No age filter: an old shell may still be running (r2). The quiet-clock release
    # below is the only bound, which errs toward not nudging.
    open_shells = {sid for sid in started if sid not in notified}
    open_bg = len(open_shells) + pending_agents
    if open_bg:
        if quiet >= bg_max_s:
            return IdleReading(Verdict.IDLE, last_boundary=end_kind, open_bg=open_bg,
                               reason=f"turn ended; {open_bg} background task(s) still open but "
                                      f"silent {_fmt_s(quiet)} >= {_fmt_s(bg_max_s)} — treating as idle",
                               **base)
        return IdleReading(Verdict.BG_WAIT, last_boundary=end_kind, open_bg=open_bg,
                           reason=f"turn ended; waiting on {open_bg} background task(s) "
                                  f"({pending_agents} agent(s), {open_bg - pending_agents} shell(s))",
                           **base)
    if quiet < quiet_s:
        return IdleReading(Verdict.WORKING, last_boundary=end_kind,
                           reason=f"turn ended {_fmt_s(quiet)} ago (< quiet {_fmt_s(quiet_s)})", **base)
    return IdleReading(Verdict.IDLE, last_boundary=end_kind,
                       reason=f"turn ended ({end_kind}); quiet {_fmt_s(quiet)}", **base)


def classify_codex(records: Iterable[dict], now: float, *, quiet_s: float = DEFAULT_QUIET_S) -> IdleReading:
    """Pure: codex rollout records -> reading (harness/log/session unset)."""
    try:
        return _classify_codex(list(records), float(now), quiet_s)
    except Exception as exc:  # noqa: BLE001 — never raise
        return _unknown(f"codex classifier error: {type(exc).__name__}: {exc}", harness=CODEX)


def _classify_codex(records: list[dict], now: float, quiet_s: float) -> IdleReading:
    last_start = last_end = -1
    end_kind: str | None = None
    newest: float | None = None
    for i, rec in enumerate(records):
        ts = _parse_ts(rec.get("timestamp"))
        if ts is not None and (newest is None or ts > newest):
            newest = ts
        if rec.get("type") != "event_msg":
            continue
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        ptype = payload.get("type")
        if ptype in _CODEX_START:
            last_start = i
        elif ptype in _CODEX_END:
            last_end, end_kind = i, ptype
    if last_start < 0 and last_end < 0:
        return _unknown("no task_started/task_complete/turn_aborted in the log window",
                        harness=CODEX, last_event_ts=newest)
    if newest is None:
        return _unknown("no timestamped records in the log window", harness=CODEX)
    quiet = max(0.0, now - newest)
    base = dict(harness=CODEX, last_event_ts=newest, quiet_s=quiet)
    if last_start > last_end:
        return IdleReading(Verdict.WORKING, last_boundary="task_started",
                           reason=f"turn in progress (last activity {_fmt_s(quiet)} ago)", **base)
    if quiet < quiet_s:
        return IdleReading(Verdict.WORKING, last_boundary=end_kind,
                           reason=f"turn ended {_fmt_s(quiet)} ago (< quiet {_fmt_s(quiet_s)})", **base)
    return IdleReading(Verdict.IDLE, last_boundary=end_kind,
                       reason=f"turn ended ({end_kind}); quiet {_fmt_s(quiet)}", **base)


# --------------------------------------------------------------------- harness resolving
def resolve_harness(agent: str, *, home: str | Path, default: str, account: str | None = None,
                    reader=None, state_root: Path | None = None,
                    persona_dir: Path | None = None) -> tuple[str, float | None, str]:
    """(submit profile, runtime started_at epoch or None, source) for `agent`'s LIVE runtime.

    Order: the runtime controller's runtime.json (authoritative; live_harness's schema
    check and profile map) -> the persona's active-runtime.json cache -> `default`.
    Never raises. Both files live in `persona_dir`, default <state_root>/<agent>, and
    `state_root` defaults to <home>/.local/state/pantheon/personas."""
    reader = reader or LocalReader()
    try:
        lh = _sibling("live_harness")
        if persona_dir is None:
            root = Path(state_root) if state_root is not None else lh.default_state_root(home)
            persona_dir = root / agent
        persona_dir = Path(persona_dir)
    except Exception:  # noqa: BLE001
        return default, None, "default (live_harness unavailable)"
    try:
        text = reader.read_text_head(account, str(persona_dir / "runtime-controller" / "runtime.json"),
                                     HEAD_BYTES)
        state = json.loads(text) if text else None
    except (ValueError, TypeError, OSError):
        state = None
    profile = lh.runtime_profile(state)
    if profile is not None:
        return profile, lh.runtime_started_at(state), "runtime.json"
    try:
        text = reader.read_text_head(account, str(persona_dir / "active-runtime.json"), HEAD_BYTES)
        cache = json.loads(text) if text else None
        runtime = cache.get("runtime") if isinstance(cache, dict) else None
    except (ValueError, TypeError, OSError, AttributeError):
        runtime = None
    prefix = str(runtime or "").split("/", 1)[0]
    cached = lh.profile_for(prefix)
    if cached is not None:
        return cached, None, "active-runtime.json"
    return default, None, "default"


# ------------------------------------------------------------------------- log locating
@dataclasses.dataclass(frozen=True)
class _Located:
    path: str
    mtime: float
    session_id: str
    records: list | None = None  # Claude: the tail already read while choosing


def _codex_meta_is_subthread(head: str | None) -> bool:
    """True when a rollout's session_meta marks a subagent or exec thread (load-bearing:
    during wait_agent a subagent rollout is newer and ends in task_complete). The three
    predicates of harness_adapter.integration._codex_top_level_rollouts, plus the root
    checks of codex_context_lifecycle (thread_source must be "user"; a dict `source`
    such as {"subagent": ...} is not a root thread)."""
    if not head:
        return False
    first = head.split("\n", 1)[0]
    try:
        meta = json.loads(first)
    except ValueError:
        meta = None
    if isinstance(meta, dict):
        payload = meta.get("payload") if isinstance(meta.get("payload"), dict) else {}
        thread_source = payload.get("thread_source")
        return meta.get("type") == "session_meta" and bool(
            payload.get("parent_thread_id") or payload.get("source") == "exec"
            or payload.get("originator") == "codex_exec"
            or isinstance(payload.get("source"), dict)
            or (thread_source is not None and thread_source != "user"))
    # Line 1 longer than the head read: textual match. The key patterns need an unescaped
    # `"key":`, which JSON-escaped prose inside base_instructions cannot produce, so they
    # scan the whole head. The bare "subagent" needle is broader, so it only scans the part
    # before base_instructions (id/source/thread_source precede it on cli 0.147.0).
    cut = head.find('"base_instructions"')
    prefix = head if cut < 0 else head[:cut]
    return bool(re.search(r'"parent_thread_id"\s*:\s*"[^"]', head)
                or re.search(r'"source"\s*:\s*"exec"', head)
                or re.search(r'"source"\s*:\s*\{', head)
                or re.search(r'"originator"\s*:\s*"codex_exec"', head)
                or re.search(r'"thread_source"\s*:\s*(?![\s"]|null)|"thread_source"\s*:\s*"(?!user")', head)
                or '"subagent"' in prefix)


def _codex_session_id(head: str | None, path: str) -> str:
    try:
        meta = json.loads((head or "").split("\n", 1)[0])
        sid = meta["payload"]["id"]
        if isinstance(sid, str) and sid:
            return sid
    except (ValueError, KeyError, TypeError):
        pass
    return Path(path).stem


def _claude_projects_dir(home: str | Path, cwd: str | Path | None) -> str | None:
    if cwd is None:
        return None
    try:
        slug = _sibling("harness_adapter.integration").claude_project_slug(Path(cwd))
    except Exception:  # noqa: BLE001 — optional heavy import
        return None
    return str(Path(home) / ".claude" / "projects" / slug)


def _locate(harness: str, account: str, home: str | Path, cwd: str | Path | None, *, reader,
            started_at: float | None, claude_projects_dir: str | None = None
            ) -> tuple[_Located | None, str]:
    """(located log or None, reason)."""
    if harness == CODEX:
        root = str(Path(home) / ".codex" / "sessions")
        rows = reader.newest_files(account, root, 4, 4, ".jsonl", CODEX_CANDIDATES)
        if not rows:
            return None, f"no codex rollouts under {root}"
        for mtime, path in rows:
            if not Path(path).name.startswith("rollout-"):
                continue
            if started_at is not None and mtime < started_at:
                return None, "newest top-level codex rollout predates the runtime start (stale)"
            head = reader.read_text_head(account, path, HEAD_BYTES)
            if head is None:
                continue
            if _codex_meta_is_subthread(head):
                continue
            return _Located(path, mtime, _codex_session_id(head, path)), "ok"
        return None, "no top-level codex rollout among the newest candidates"
    if harness == CLAUDE:
        pdir = claude_projects_dir or _claude_projects_dir(home, cwd)
        if not pdir:
            return None, "no Claude projects dir (no cwd/projects_dir)"
        rows = reader.newest_files(account, pdir, 1, 1, ".jsonl", CLAUDE_CANDIDATES)
        if not rows:
            return None, f"no Claude transcripts in {pdir}"
        if started_at is not None:
            rows = [r for r in rows if r[0] >= started_at]
            if not rows:
                return None, "newest Claude transcript predates the runtime start (stale)"
        best: _Located | None = None
        best_ts = float("-inf")
        for mtime, path in rows:
            records = _read_tail(reader, account, path, TAIL_BYTES)
            if records is None:
                continue
            ts = max((t for t in (_parse_ts(r.get("timestamp")) for r in records
                                  if r.get("type") in _CLAUDE_ACTIVITY_TYPES) if t is not None),
                     default=float("-inf"))
            if best is None or ts > best_ts:
                best, best_ts = _Located(path, mtime, Path(path).stem, records), ts
        if best is None:
            return None, "Claude transcripts unreadable"
        return best, "ok"
    return None, f"no idle classifier for harness {harness!r}"


def locate_log(harness: str, account: str, home: str | Path, cwd: str | Path | None, *,
               reader=None, started_at: float | None = None,
               claude_projects_dir: str | None = None) -> Path | None:
    """The live session log for `harness`, or None (missing, stale, unknown harness)."""
    try:
        found, _ = _locate(harness, account, home, cwd, reader=reader or LocalReader(),
                           started_at=started_at, claude_projects_dir=claude_projects_dir)
    except Exception:  # noqa: BLE001
        return None
    return Path(found.path) if found else None


# ---------------------------------------------------------------------------- top level
def read_idle(agent: str, *, account: str, home: str | Path, cwd: str | Path | None,
              default_harness: str, reader=None, now: float | None = None,
              quiet_s: float = DEFAULT_QUIET_S, bg_max_s: float = DEFAULT_BG_MAX_S,
              claude_projects_dir: str | None = None, state_root: Path | None = None) -> IdleReading:
    """Resolve the live harness, find its session log, classify. Never raises.

    `claude_projects_dir` overrides the <home>/.claude/projects/<slug(cwd)> derivation
    (keep_going passes agent_role_map.live_session_projects_dir, its single source)."""
    try:
        return _read_idle(agent, account, home, cwd, default_harness, reader or LocalReader(),
                          time.time() if now is None else float(now), quiet_s, bg_max_s,
                          claude_projects_dir, state_root)
    except Exception as exc:  # noqa: BLE001 — never raise
        return _unknown(f"read_idle error: {type(exc).__name__}: {exc}")


def _read_idle(agent, account, home, cwd, default_harness, reader, now, quiet_s, bg_max_s,
               claude_projects_dir, state_root) -> IdleReading:
    harness, started_at, source = resolve_harness(
        agent, home=home, default=default_harness, account=account, reader=reader,
        state_root=state_root)
    if harness not in (CLAUDE, CODEX):
        return _unknown(f"no idle classifier for harness {harness!r} (from {source})", harness=harness)
    found, why = _locate(harness, account, home, cwd, reader=reader, started_at=started_at,
                         claude_projects_dir=claude_projects_dir)
    if found is None:
        return _unknown(f"{why} (harness {harness} from {source})", harness=harness)
    guard = "" if started_at is not None else "; no runtime started_at, mtime-only guard"
    records = found.records
    if records is None:
        records = _read_tail(reader, account, found.path, TAIL_BYTES)
    if records is None:
        return _unknown(f"log unreadable: {found.path}", harness=harness, log_path=found.path,
                        session_id=found.session_id)
    markers = _bg_markers(reader, account, found.path) if harness == CLAUDE else None
    classify = ((lambda recs: classify_codex(recs, now, quiet_s=quiet_s)) if harness == CODEX
                else (lambda recs: classify_claude(recs, now, quiet_s=quiet_s, bg_max_s=bg_max_s,
                                                   markers=markers)))
    reading = classify(records)
    if reading.verdict is Verdict.UNKNOWN and reading.last_boundary is None:
        wide = _read_tail(reader, account, found.path, WIDE_TAIL_BYTES)
        if wide is not None and len(wide) > len(records):
            reading = classify(wide)
    return dataclasses.replace(reading, harness=harness, log_path=found.path,
                               session_id=found.session_id, reason=reading.reason + guard)


# ------------------------------------------------------------------- pre-inject safety
def pane_inject_safe(pane_text: str | None, harness: str | None) -> bool:
    """May a nudge be pasted into this pane right now? The log already said IDLE; this
    only rules out a modal, dialog or a turn the log has not caught up with.

    claude: inbox_consumer.pane_idle_state(..., background_agents_busy=True) is True.
    codex:  ANSI-stripped capture is ready_for_submission, shows neither codex modal
            (the mailbox_wake.unknown_pane_deliverable codex branch), and its bottom
            region has no busy footer ("Working (… esc to interrupt)").
    Anything else, or any error: False."""
    if not pane_text or not pane_text.strip():
        return False
    try:
        if harness in (CLAUDE, "claude"):
            ic = _sibling("inbox_consumer")
            return ic.pane_idle_state(pane_text, background_agents_busy=True) is True
        if harness == CODEX:
            ic = _sibling("inbox_consumer")
            sv = _sibling("submit_verify")
            cm = _sibling("codex_modal")
            text = ic._ESCAPE_RE.sub("", pane_text)
            bottom = [ln for ln in text.splitlines() if ln.strip()][-_BOTTOM_LINES:]
            return (sv.ready_for_submission(text)
                    and not cm.is_codex_update_modal(text)
                    and not cm.is_codex_model_migration_modal(text)
                    and not any(_BUSY_SENTINEL in ln for ln in bottom))
    except Exception:  # noqa: BLE001
        return False
    return False
