#!/usr/bin/env python3
"""health_export — sanitized, enum-only fleet health summary per persona (T-4884 B2).

WHY THIS EXISTS. A future codex-primary recovery seat (account
codex-fallback) must detect a dead, capped or wedged operator WITHOUT reading
other accounts' raw state (transcripts, panes, controller evidence) and without
any free-form text that could carry instructions into an LLM prompt. Design:
notes/2026-10-12-t4884-codex-recovery-seat-design.md §4.1 (review #2, #3, #13).

So this exporter, running as Recovery (claude-fallback, which already has
`sudo -n -u <acct>` to fleet accounts and already hosts the fleet monitors),
reads those sources and publishes ONLY enums, ints, bounded ascii identifiers
and null, one file per persona:

    <out-dir>/<persona>.json     schema_version 1, keys = FIELDS
    <out-dir>/_exporter.json     {schema_version, exported_at, persona_count}

The heartbeat file's own staleness is a signal (Recovery's account or user manager
is broken), so it is written LAST, after every persona file.

CONTRACTS
  * Every cross-account read goes through ONE reader object (SudoReader in
    production; a fake in tests). argv arrays only, `sudo -n` (never prompts),
    bounded timeouts. A failed read yields None, which becomes a null field —
    never an exception, never a guessed value ("unknown must not stand in for no").
  * sanitize() is applied to every record right before it is written. It drops
    unknown keys, enforces enums and int ranges, rejects control characters and
    caps identifiers at 64 chars. Collection code already maps everything to
    enums; the sanitizer is the second, schema-level gate.
  * Writes are atomic: `.<name>.tmp` in the same dir, fsync, rename, mode 0640.
  * Exit 0 even when some personas could not be read (their fields are null).
    Non-zero only when the out-dir is unwritable.

NOT INSTALLED BY THIS STEP. The unit templates live in
tools/systemd/pantheon-health-export.{service,timer}. /run/pantheon-health is
provisioned by root via tmpfiles (B7/B11), not by this tool.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import agent_role_map  # noqa: E402
from tools.acp_bridge_backlog_watchdog import _parse_ts_ns  # noqa: E402  (mailbox id -> ts_ns)

SCHEMA_VERSION = 1
DEFAULT_OUT_DIR = Path("/run/pantheon-health")
HEARTBEAT_NAME = "_exporter.json"

# Ordered: the on-disk key order. Schema-stable: every key is always present.
FIELDS: tuple[str, ...] = (
    "schema_version",
    "persona",
    "account",
    "exported_at",
    "runtime",
    "controller_last_event",
    "controller_last_event_age_s",
    "activity_age_s",
    "mail_unread",
    "mail_oldest_unread_age_s",
    "consume_unit",
    "pane_state",
    "seat_state",
    "expected_down",
    # ORG-106 L4b leg 1 (T-5528): detail so kino can render a pause/sleep as expected.
    "seat_owner",
    "seat_reason",
    "seat_expires",
    "seat_indefinite",
    "seat_set_age_s",
    "seat_indefinite_stale",
)

CONTROLLER_EVENTS = frozenset({
    "runtime_viability_blocked",
    "transition_started",
    "runtime_replaced",
    "successor_prompted",
    "runtime_submission_accepted",
    "swap_abandoned",
    "other",
})
# Real event names written by tools/runtime_controller.py
# (_append_controller_evidence call sites) -> exported enum. Anything not listed
# is "other". Keyed on exact strings; no substring/prefix matching.
_EVENT_MAP: dict[str, str] = {
    "runtime_viability_blocked": "runtime_viability_blocked",
    "transition_started": "transition_started",
    "operator_transition_started": "transition_started",
    "runtime_replaced": "runtime_replaced",
    "successor_prompted": "successor_prompted",
    "runtime_submission_accepted": "runtime_submission_accepted",
    "planned_swap_abandoned": "swap_abandoned",
}

CONSUME_STATES = frozenset({"active", "inactive", "failed", "unknown"})
PANE_STATES = frozenset({"idle", "busy", "modal", "unknown"})
# ORG-106 L4a (T-5527): the seat-state record's state. An unread record is "active" (fail-open).
SEAT_STATES = frozenset({"active", "paused-intake", "deep-sleep", "halted"})  # T-5868

_IDENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RUNTIME_RE = re.compile(r"^[a-z0-9-]+/[a-z0-9-]+$")
_MAX_IDENT = 64
_MAX_AGE_S = 10 * 365 * 86400       # anything older is nonsense, not data
_MAX_EPOCH = 2 ** 40
_MAX_COUNT = 1_000_000

_TAIL_BYTES = 64 * 1024
_RUNTIME_JSON_BYTES = 16 * 1024
_TIMEOUT_S = 10


# --------------------------------------------------------------------- reader
class SudoReader:
    """The ONE cross-account read surface. Every method returns None on any failure.

    Reads of the caller's own account (and account=None, used for the
    group-readable mailbox hub) run without sudo. Everything else runs as
    `sudo -n -u <account> <argv...>` — argv arrays only, never a shell string,
    so no path or target is ever shell-interpreted.
    """

    def __init__(self, timeout: int = _TIMEOUT_S):
        self.timeout = timeout
        try:
            self.self_account = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            self.self_account = ""

    def _argv(self, account: str | None, argv: list[str], env: dict[str, str] | None = None) -> list[str]:
        envv = ["env", *(f"{k}={v}" for k, v in (env or {}).items())] if env else []
        if account is None or account == self.self_account:
            return [*envv, *argv]
        return ["sudo", "-n", "-u", account, *envv, *argv]

    def _run(self, account: str | None, argv: list[str], env: dict[str, str] | None = None) -> str | None:
        try:
            out = subprocess.run(self._argv(account, argv, env), capture_output=True,
                                 text=True, errors="replace", timeout=self.timeout,
                                 stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if out.returncode != 0:
            return None
        return out.stdout

    def read_text_tail(self, account: str, path: str, max_bytes: int) -> str | None:
        return self._run(account, ["tail", "-c", str(int(max_bytes)), "--", path])

    def read_text_head(self, account: str | None, path: str, max_bytes: int) -> str | None:
        """The first `max_bytes` of `path` (harness_idle reader seam: codex session_meta,
        runtime.json). argv-only `head -c N -- path`."""
        return self._run(account, ["head", "-c", str(max(0, int(max_bytes))), "--", path])

    def grep_tail(self, account: str | None, path: str, max_bytes: int,
                  needles: tuple[bytes, ...]) -> list[str] | None:
        """Complete lines within the last `max_bytes` of `path` that contain any of
        `needles` (harness_idle's background-shell marker scan, T-5793). One argv-only
        `tail -c N -- path` read; the substring prefilter runs here in Python, so no
        shell and no second process. The cut first line is dropped when the read filled
        the window. Lines are returned without trailing newlines (harness_idle accepts
        both shapes)."""
        cap = max(0, int(max_bytes))
        out = self._run(account, ["tail", "-c", str(cap), "--", path])
        if out is None:
            return None
        lines = out.split("\n")
        if lines and len(out.encode("utf-8", errors="replace")) >= cap:
            lines = lines[1:]
        wanted = [n.decode("utf-8", errors="replace") for n in needles if n]
        return [ln for ln in lines if ln and any(n in ln for n in wanted)]

    def newest_mtime(self, account: str, root: str, min_depth: int, max_depth: int,
                     suffix: str) -> float | None:
        out = self._run(account, ["find", root, "-mindepth", str(int(min_depth)),
                                  "-maxdepth", str(int(max_depth)), "-type", "f",
                                  "-name", f"*{suffix}", "-printf", r"%T@\n"])
        if out is None:
            return None
        best = None
        for line in out.splitlines():
            try:
                v = float(line)
            except ValueError:
                continue
            best = v if best is None or v > best else best
        return best

    def newest_files(self, account: str, root: str, min_depth: int, max_depth: int,
                     suffix: str, n: int) -> list[tuple[float, str]] | None:
        """The `n` most recently modified matching files as (mtime, path), newest first."""
        out = self._run(account, ["find", root, "-mindepth", str(int(min_depth)),
                                  "-maxdepth", str(int(max_depth)), "-type", "f",
                                  "-name", f"*{suffix}", "-printf", r"%T@ %p\n"])
        if out is None:
            return None
        rows = []
        for line in out.splitlines():
            m, sep, path = line.partition(" ")
            try:
                rows.append((float(m), path))
            except ValueError:
                continue
        return sorted((r for r in rows if r[1]), reverse=True)[:max(0, int(n))]

    def list_dir(self, account: str | None, path: str) -> list[str] | None:
        if account is None or account == self.self_account:
            try:
                return os.listdir(path)
            except OSError:
                return None
        out = self._run(account, ["ls", "-1A", "--", path])
        return None if out is None else out.splitlines()

    def systemctl_show(self, account: str, unit: str, props: tuple[str, ...]) -> dict[str, str] | None:
        try:
            uid = pwd.getpwnam(account).pw_uid
        except KeyError:
            return None
        out = self._run(account, ["systemctl", "--user", "show", "-p", ",".join(props), "--", unit],
                        env={"XDG_RUNTIME_DIR": f"/run/user/{uid}"})
        if out is None:
            return None
        res: dict[str, str] = {}
        for line in out.splitlines():
            k, sep, v = line.partition("=")
            if sep:
                res[k.strip()] = v.strip()
        return res

    def capture_pane(self, account: str, target: str) -> str | None:
        # -e: pane_idle_state needs the DIM attribute (T-4284), same as mail_staleness_monitor.
        return self._run(account, ["tmux", "capture-pane", "-p", "-e", "-t", target])


# ----------------------------------------------------------------- collectors
def _safe(fn, *args):
    """Run one collector; any exception from a reader or parser is a null field."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - a read error is null, never a crash
        return None


def _age(now: float, then: float | None) -> int | None:
    if then is None:
        return None
    return max(0, int(now - then))


def _persona_state_dir(account: str, persona: str) -> str:
    # Mirrors runtime_controller.DEFAULT_STATE_ROOT (~/.local/state/pantheon/personas)
    # resolved for ANOTHER account's home, which that module cannot do for us.
    return f"/home/{account}/.local/state/pantheon/personas/{persona}"


def _runtime(reader, account: str, persona: str) -> str | None:
    text = reader.read_text_tail(account, f"{_persona_state_dir(account, persona)}/active-runtime.json",
                                 _RUNTIME_JSON_BYTES)
    if not text:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    rt = doc.get("runtime") if isinstance(doc, dict) else None
    if isinstance(rt, str) and len(rt) <= _MAX_IDENT and _RUNTIME_RE.match(rt):
        return rt
    return None


_SEAT_REASON_MAX = 200
_SEAT_OWNER_MAX = 80
_ISO_Z_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

# Every seat_* key at its inactive value: the fail-open result and the "active" shape.
_SEAT_INACTIVE: dict = {
    "seat_state": "active", "expected_down": False,
    "seat_owner": None, "seat_reason": None, "seat_expires": None,
    "seat_indefinite": False, "seat_set_age_s": None, "seat_indefinite_stale": False,
}


def _seat_owner(v: object) -> str | None:
    """Printable, no whitespace, 1-80 (harness.py --owner shape). Anything else is null."""
    if (isinstance(v, str) and 1 <= len(v) <= _SEAT_OWNER_MAX and v.isprintable()
            and not any(c.isspace() for c in v)):
        return v
    return None


def _seat_reason(v: object) -> str | None:
    """Free text for kino: printable only, control chars stripped, capped. NOT HTML-escaped
    (escaping is the renderer's job). Empty after stripping is null."""
    if not isinstance(v, str):
        return None
    out = "".join(c for c in v if c.isprintable())[:_SEAT_REASON_MAX]
    return out or None


def _seat_expires(v: object) -> str | None:
    return v if isinstance(v, str) and _ISO_Z_RE.fullmatch(v) else None


def _seat_stale(indefinite: bool, age: int | None) -> bool:
    from tools import seat_state

    return bool(indefinite and age is not None and age >= seat_state.INDEFINITE_FLAG_AGE.total_seconds())


def _seat_state(reader, account: str, persona: str, now: float) -> dict:
    """The seat_* keys from the seat's ORG-106 seat-state record, read through the SAME
    cross-account reader as every other field here (no new sudo surface, one read).
    FAIL-OPEN: a missing, unreadable, malformed or expired record is the inactive shape."""
    from tools import seat_state

    out = dict(_SEAT_INACTIVE)
    text = reader.read_text_tail(account, f"{_persona_state_dir(account, persona)}/overrides/seat-state.json",
                                 _RUNTIME_JSON_BYTES)
    if not text:
        return out
    try:
        doc = json.loads(text)
        c = seat_state.parse_record(doc, now=datetime.fromtimestamp(now, timezone.utc))
        if not c.active or c.state not in SEAT_STATES:
            return out
        # validate_record (inside parse_record) guaranteed set_ts / indefinite are well-formed.
        set_ts = seat_state._parse_ts(doc["set_ts"])
        indefinite = doc["indefinite"] is True
        age = max(0, min(int(now - set_ts.timestamp()), _MAX_AGE_S))  # clamp, don't null
        out.update(
            seat_state=c.state, expected_down=bool(c.expected_down),
            seat_owner=_seat_owner(c.owner), seat_reason=_seat_reason(c.reason),
            seat_expires=None if indefinite else _seat_expires(c.expires),
            seat_indefinite=indefinite, seat_set_age_s=age,
            seat_indefinite_stale=_seat_stale(indefinite, age),
        )
    except Exception:  # noqa: BLE001
        return dict(_SEAT_INACTIVE)
    return out


def _map_event(raw: object) -> str | None:
    """Exact-match lookup into the fixed enum. A non-string event is unknown (None);
    any string not in _EVENT_MAP — including hostile ones — is "other"."""
    if not isinstance(raw, str):
        return None
    return _EVENT_MAP.get(raw, "other")


def _parse_iso_epoch(raw: object) -> float | None:
    if not isinstance(raw, str) or len(raw) > 40:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _controller(reader, account: str, persona: str, now: float) -> tuple[str | None, int | None]:
    path = f"{_persona_state_dir(account, persona)}/runtime-controller/controller-evidence.jsonl"
    text = reader.read_text_tail(account, path, _TAIL_BYTES)
    if not text:
        return None, None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None, None
    try:
        rec = json.loads(lines[-1])
    except ValueError:
        return None, None  # torn/partial last line: unknown, not "other"
    if not isinstance(rec, dict):
        return None, None
    return _map_event(rec.get("event")), _age(now, _parse_iso_epoch(rec.get("recorded_at")))


# T-5050: activity is the newest TIMESTAMPED transcript entry, not the file's mtime. Claude Code
# rewrites a transcript's untimestamped metadata lines (last-prompt, ai-title, mode,
# permission-mode, ...) in place while the seat is idle: on 2026-10-22 specialist's and archivist's
# transcripts had mtimes 11-26 min old but newest entries ~5h old, and in B13 attempt 6 one such
# rewrite reset recovery's 30-min stale clock (RECOVERY_UNHEALTHY at T+57, not ~T+35).
ACTIVITY_TAIL_BYTES = 256 * 1024
ACTIVITY_CANDIDATES = 3  # the newest few files by mtime; an idle rewrite can reorder them
_CLAUDE_ACTIVITY_TYPES = frozenset({"user", "assistant", "system"})


def _newest_entry_epoch(text: str, *, claude: bool) -> float | None:
    """Max `timestamp` among the tail's complete JSONL entries (the first line may be cut)."""
    best = None
    for line in text.split("\n")[1:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or (claude and rec.get("type") not in _CLAUDE_ACTIVITY_TYPES):
            continue
        ts = _parse_iso_epoch(rec.get("timestamp"))
        if ts is not None and (best is None or ts > best):
            best = ts
    return best


def _activity_age(reader, account: str, now: float) -> int | None:
    home = f"/home/{account}"
    newest = None
    # Claude: ~/.claude/projects/*/*.jsonl ; codex: ~/.codex/sessions/**/*.jsonl
    for root, lo, hi, claude in ((f"{home}/.claude/projects", 2, 2, True),
                                 (f"{home}/.codex/sessions", 1, 6, False)):
        files = _safe(lambda: reader.newest_files(account, root, lo, hi, ".jsonl", ACTIVITY_CANDIDATES))
        if not isinstance(files, list):
            # Listing failed (or a reader without newest_files): the pre-T-5050 mtime reading.
            m = _safe(reader.newest_mtime, account, root, lo, hi, ".jsonl")
            if isinstance(m, (int, float)) and not isinstance(m, bool):
                newest = m if newest is None or m > newest else newest
            continue
        for mtime, path in files:
            if not isinstance(mtime, (int, float)) or isinstance(mtime, bool):
                continue
            tail = _safe(reader.read_text_tail, account, path, ACTIVITY_TAIL_BYTES)
            entry = _safe(lambda: _newest_entry_epoch(tail, claude=claude)) if isinstance(tail, str) else None
            # An entry cannot postdate its file's last write, so mtime caps it. No readable
            # timestamped entry falls back to mtime, the pre-T-5050 reading: a fresher-looking
            # value, never a null that would drop the signal.
            t = min(entry, mtime) if isinstance(entry, float) else mtime
            newest = t if newest is None or t > newest else newest
    return _age(now, newest)


def _mail(reader, hub: str, persona: str, now: float) -> tuple[int | None, int | None]:
    names = reader.list_dir(None, f"{hub}/{persona}/new")  # hub is group-readable: no sudo
    if names is None:
        return None, None
    msgs = [n for n in names if isinstance(n, str) and n and not n.startswith(".")]
    stamps = [ts for ts in (_parse_ts_ns(n) for n in msgs) if ts is not None]
    oldest = _age(now, min(stamps) / 1e9) if stamps else None
    return len(msgs), oldest


def _consume_unit(reader, account: str, persona: str) -> str:
    unit = agent_role_map.control_plane_consume_service(persona)
    props = reader.systemctl_show(account, unit, ("LoadState", "ActiveState", "Result"))
    if not isinstance(props, dict):
        return "unknown"
    if props.get("LoadState") != "loaded":
        return "unknown"
    state = props.get("ActiveState")
    if state == "failed":
        return "failed"
    if state in ("active", "activating", "reloading", "deactivating"):
        return "active"
    if state == "inactive":
        return "inactive"
    return "unknown"


def classify_pane(text: str | None) -> str:
    """Map a captured pane to idle|busy|modal|unknown via inbox_consumer's classifier.

    pane_idle_state is the canonical tristate (True idle / False generating / None
    unknown-or-modal); _pane_blocks_injection is its positive modal witness, used only
    to split None into modal vs unknown. Imported lazily, as mail_staleness_monitor
    does, so a broken import degrades to "unknown" rather than killing the export.
    """
    if not text:
        return "unknown"
    try:
        from tools import inbox_consumer as ic
    except Exception:  # noqa: BLE001
        return "unknown"
    try:
        state = ic.pane_idle_state(text)
        if state is True:
            return "idle"
        if state is False:
            return "busy"
        reason = ic._pane_blocks_injection(text)
        if isinstance(reason, str) and reason.startswith("modal footer"):
            return "modal"
        return _classify_codex_pane(text)
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


# T-5032: codex TUI (any codex-harness seat). Only the live bottom region
# is judged, so quoted/old scrollback cannot fake a state.
_CODEX_BOTTOM_LINES = 6
_CODEX_BUSY_RE = re.compile(r"\bWorking\b.*esc to interrupt|esc to interrupt", re.IGNORECASE)
# Model footer: "<model>[ <effort|default>] \u00b7 <cwd>" (effort suffix optional).
_CODEX_FOOTER_RE = re.compile(r"^\s*[\w.\-]+(?:\s+\w+)?\s+\u00b7\s+\S")
_CODEX_COMPOSER_RE = re.compile(r"^\s*\u203a(?:\s|$)")


def _classify_codex_pane(text: str) -> str:
    """idle|busy|modal|unknown for a codex pane; unknown on anything unrecognised.

    Modal detection is delegated to codex_modal_monitor (single source) and outranks
    busy/idle. Busy = codex's in-progress line in the bottom region. Idle = a `\u203a`
    composer line (any ghost/placeholder text) in the bottom region plus the model
    footer as the last non-blank line, with no busy line."""
    try:
        from tools import codex_modal_monitor as cmm
        if cmm.classify_codex_pane(text):
            return "modal"
    except Exception:  # noqa: BLE001
        return "unknown"
    # The production capture is `capture-pane -e` (SudoReader.capture_pane), so the
    # footer/composer carry SGR colour codes; match on the plain text.
    try:
        from tools.inbox_consumer import _ESCAPE_RE
        plain = _ESCAPE_RE.sub("", text)
    except Exception:  # noqa: BLE001
        return "unknown"
    lines = [ln for ln in plain.splitlines() if ln.strip()]
    bottom = lines[-_CODEX_BOTTOM_LINES:]
    if any(_CODEX_BUSY_RE.search(ln) for ln in bottom):
        return "busy"
    if (bottom and _CODEX_FOOTER_RE.match(bottom[-1])
            and any(_CODEX_COMPOSER_RE.match(ln) for ln in bottom[:-1])):
        return "idle"
    return "unknown"


def collect_persona(reader, persona: str, account: str, *, now: float, hub: str,
                    pane_target: str | None) -> dict:
    """One persona's raw record (already enum-mapped). Never raises."""
    ctl = _safe(_controller, reader, account, persona, now) or (None, None)
    mail = _safe(_mail, reader, hub, persona, now) or (None, None)
    pane_text = _safe(reader.capture_pane, account, pane_target) if pane_target else None
    seat = _safe(_seat_state, reader, account, persona, now) or dict(_SEAT_INACTIVE)
    rec = {
        "schema_version": SCHEMA_VERSION,
        "persona": persona,
        "account": account,
        "exported_at": int(now),
        "runtime": _safe(_runtime, reader, account, persona),
        "controller_last_event": ctl[0],
        "controller_last_event_age_s": ctl[1],
        "activity_age_s": _safe(_activity_age, reader, account, now),
        "mail_unread": mail[0],
        "mail_oldest_unread_age_s": mail[1],
        "consume_unit": _safe(_consume_unit, reader, account, persona) or "unknown",
        "pane_state": classify_pane(pane_text if isinstance(pane_text, str) else None),
        **{k: seat[k] for k in _SEAT_INACTIVE},
    }
    del pane_text  # never stored, never written
    return rec


# ------------------------------------------------------------------ sanitizer
def _has_ctrl(s: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in s)


def _ident(v: object, pattern: re.Pattern = _IDENT_RE) -> str | None:
    if (isinstance(v, str) and len(v) <= _MAX_IDENT and v.isascii()
            and not _has_ctrl(v) and pattern.match(v)):
        return v
    return None


def _bounded_int(v: object, hi: int) -> int | None:
    # bool is an int subclass; True is not a count.
    if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= hi:
        return v
    return None


def _sanitize_seat_detail(record: dict, seat_st: str) -> dict:
    """Consume-side validation of the six L4b seat_* keys. An active seat carries none of
    them; a missing key (older exporter) is null/false; stale is recomputed, never trusted."""
    if seat_st == "active":
        return {k: v for k, v in _SEAT_INACTIVE.items() if k not in ("seat_state", "expected_down")}
    indefinite = record.get("seat_indefinite") is True
    age = _bounded_int(record.get("seat_set_age_s"), _MAX_AGE_S)
    return {
        "seat_owner": _seat_owner(record.get("seat_owner")),
        "seat_reason": _seat_reason(record.get("seat_reason")),
        "seat_expires": None if indefinite else _seat_expires(record.get("seat_expires")),
        "seat_indefinite": indefinite,
        "seat_set_age_s": age,
        "seat_indefinite_stale": _seat_stale(indefinite, age),
    }


def sanitize(record: object) -> dict | None:
    """Enforce schema v1 on a record. Returns a NEW dict with exactly FIELDS, or None.

    None only when the record cannot be addressed safely (persona/account not a valid
    identifier, wrong schema_version, not a dict) — the persona name becomes a file
    name, so it is never coerced. Every other field degrades to null (or "unknown" /
    "other" for the enums) rather than passing an unexpected value through.
    """
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
        return None
    persona, account = _ident(record.get("persona")), _ident(record.get("account"))
    if persona is None or account is None:
        return None
    exported_at = _bounded_int(record.get("exported_at"), _MAX_EPOCH)
    if exported_at is None:
        return None
    event = record.get("controller_last_event")
    consume = record.get("consume_unit")
    pane = record.get("pane_state")
    seat_st = (record["seat_state"] if isinstance(record.get("seat_state"), str)
               and record["seat_state"] in SEAT_STATES else "active")
    seat_extra = _sanitize_seat_detail(record, seat_st)
    return {
        "schema_version": SCHEMA_VERSION,
        "persona": persona,
        "account": account,
        "exported_at": exported_at,
        "runtime": _ident(record.get("runtime"), _RUNTIME_RE),
        "controller_last_event": (None if event is None
                                  else event if isinstance(event, str) and event in CONTROLLER_EVENTS
                                  else "other"),
        "controller_last_event_age_s": _bounded_int(record.get("controller_last_event_age_s"), _MAX_AGE_S),
        "activity_age_s": _bounded_int(record.get("activity_age_s"), _MAX_AGE_S),
        "mail_unread": _bounded_int(record.get("mail_unread"), _MAX_COUNT),
        "mail_oldest_unread_age_s": _bounded_int(record.get("mail_oldest_unread_age_s"), _MAX_AGE_S),
        "consume_unit": consume if isinstance(consume, str) and consume in CONSUME_STATES else "unknown",
        "pane_state": pane if isinstance(pane, str) and pane in PANE_STATES else "unknown",
        "seat_state": seat_st,
        # kino keys its "asleep" glance on expected_down: only a deep-sleep seat can be down.
        "expected_down": (record.get("expected_down") is True and seat_st == "deep-sleep"),
        **seat_extra,
    }


# --------------------------------------------------------------------- output
def atomic_write_json(out_dir: Path, name: str, doc: dict, mode: int = 0o640) -> None:
    """Write doc to out_dir/name atomically: .<name>.tmp, fsync, rename, mode 0640.

    `mode` (T-5395) lets the fleet-health collector publish its 0644 aggregate through
    this same writer; the default keeps every health_export caller at 0640."""
    final = out_dir / name
    tmp = out_dir / f".{name}.tmp"
    data = (json.dumps(doc, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        try:
            os.fchmod(fd, mode)  # umask must not widen or narrow the contract
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, final)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    try:
        dfd = os.open(out_dir, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass  # rename already landed; dir fsync is durability, not correctness


_PRUNE_MAX_BYTES = 64 * 1024
_RECORD_CORE_KEYS = frozenset({"schema_version", "persona", "account", "exported_at"})


def _exporter_owned_record(path: Path) -> bool:
    """True only for a file THIS exporter wrote as a persona record (T-5836).

    A regular file (never a symlink), owned by this uid, whose stem is a valid
    persona identifier and whose content is a schema-v1 record naming that same
    persona. _exporter.json (leading underscore), fleet-health.json (no
    `persona`) and any foreign JSON therefore never qualify."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > _PRUNE_MAX_BYTES:
        return False
    stem = path.name[: -len(".json")]
    if not _IDENT_RE.match(stem):
        return False
    try:
        doc = json.loads(path.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return False
    return (isinstance(doc, dict) and _RECORD_CORE_KEYS <= set(doc)
            and doc.get("schema_version") == SCHEMA_VERSION and doc.get("persona") == stem)


def prune_retired_records(out_dir: Path, current: set[str]) -> list[str]:
    """Remove exporter-owned records whose persona left the manifest (T-5836).

    health_export overwrites current records but never pruned retired filenames,
    so a retired seat's record persisted forever and recovery_watcher counted it as
    an invalid (unknown_persona) record. Fail-safe: an EMPTY current set prunes
    nothing (a manifest read failure must never wipe the directory), and more
    candidates than half the current fleet is treated as a fault and skipped.
    Returns the pruned persona names."""
    if not current:
        return []
    try:
        names = sorted(os.listdir(out_dir))
    except OSError:
        return []
    candidates = [n[: -len(".json")] for n in names
                  if n.endswith(".json") and not n.startswith((".", "_"))
                  and n[: -len(".json")] not in current
                  and _exporter_owned_record(out_dir / n)]
    if len(candidates) > max(1, len(current) // 2):
        print(f"health_export: refusing to prune {len(candidates)} records (more than half the fleet)",
              file=sys.stderr)
        return []
    pruned = []
    for persona in candidates:
        try:
            (out_dir / f"{persona}.json").unlink()
            pruned.append(persona)
        except OSError:
            pass
    if pruned:
        print(f"health_export: pruned retired persona records: {', '.join(pruned)}", file=sys.stderr)
    return pruned


def fleet_personas() -> list[tuple[str, str, str | None]]:
    """(persona, account, pane_target) for every manifest persona bound to an account.

    Source: agent_role_map.account_persona_map() (instance.yaml `roles:`, which
    excludes `host_services:` such as ci-runner). Pane targets come from
    mailbox_wake.AGENT_PANE, the substrate map mail_staleness_monitor also uses.
    """
    try:
        from tools import mailbox_wake
        panes = {p: cfg.get("pane_target") for p, cfg in mailbox_wake.AGENT_PANE.items()}
    except Exception:  # noqa: BLE001 - no pane map -> pane_state unknown
        panes = {}
    out = []
    for account, persona in agent_role_map.account_persona_map().items():
        if _IDENT_RE.match(persona) and _IDENT_RE.match(account):
            target = panes.get(persona)
            out.append((persona, account, target if isinstance(target, str) and target else None))
    return sorted(out)


def run(personas, *, reader, out_dir: Path, now: float | None = None, hub: str | None = None,
        dry_run: bool = False) -> int:
    now = time.time() if now is None else now
    hub = agent_role_map.mailbox_hub() if hub is None else hub
    personas = list(personas)
    records: dict[str, dict] = {}
    for persona, account, target in personas:
        rec = sanitize(collect_persona(reader, persona, account, now=now, hub=hub, pane_target=target))
        if rec is None:
            print("health_export: dropped a persona record that failed the schema", file=sys.stderr)
            continue
        records[rec["persona"]] = rec
    heartbeat = {"schema_version": SCHEMA_VERSION, "exported_at": int(now),
                 "persona_count": len(records)}
    if dry_run:
        print(json.dumps({"_exporter": heartbeat, "personas": records}, indent=2, ensure_ascii=True))
        return 0
    if not out_dir.is_dir() or not os.access(out_dir, os.W_OK | os.X_OK):
        print(f"health_export: out-dir not writable: {out_dir}", file=sys.stderr)
        return 2
    try:
        for persona, rec in records.items():
            atomic_write_json(out_dir, f"{persona}.json", rec)
        # T-5836: prune only when EVERY manifest persona produced a record this run, so
        # a partial collection can never read as "retired".
        if records and len(records) == len(personas):
            try:
                prune_retired_records(out_dir, set(records))
            except Exception:  # noqa: BLE001 - pruning is hygiene; never fail the export
                pass
        atomic_write_json(out_dir, HEARTBEAT_NAME, heartbeat)  # LAST: its age is the signal
    except OSError as exc:
        print(f"health_export: write failed in {out_dir}: {exc.__class__.__name__}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--dry-run", action="store_true", help="print JSON to stdout, write nothing")
    args = ap.parse_args(argv)
    return run(fleet_personas(), reader=SudoReader(), out_dir=args.out_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
