#!/usr/bin/env python3
"""lease_audit — detective check for repairs taken without the repair lease (T-4884 B4b).

WHY THIS EXISTS. Recovery (claude-fallback) and the recovery seat
(codex-fallback) hold mutual full direct sudo (the operator's ratification, TG 11705).
So the codex-recovery broker's repair lease (tools/codex_recovery.py, design note
§4.3) cannot be ENFORCED for a direct-sudo repair: it is honor-system for both
seats. The operator asked for bypasses to be VISIBLE instead (design note §4.4). This
tool reads the two records that exist anyway and reports, as enum-only findings,
every repair either seat took against a fleet seat without holding that target's
lease at that moment. Purpose is visibility and learning (each finding is a
candidate SOP fix: "this repair path should take the lease"), not blame.

SOURCES
  1. The sudo journal: `journalctl _COMM=sudo -o json`. sudo logs every run as
     `<actor> : PWD=... ; USER=<target> ; COMMAND=<argv>`; long argv spill into
     `<actor> : (command continued) ...` records with the same _PID, which are
     re-joined. sudo quotes args that hold spaces ('...'), backslash-escapes `'`
     and `\\`, and syslog-escapes control bytes as #0oo; decode_sudo_command()
     undoes exactly that and refuses anything else. A denied run ("command not
     allowed ; ...") never executed and is skipped. Reading the system journal
     needs group systemd-journal: the unit wraps the run in `sg systemd-journal`,
     and a positive control (`journalctl --system -n 1` must return a record)
     makes an unreadable journal a JOURNAL_UNREADABLE finding (exit 2), never a
     quiet "no repairs".
  2. The broker's readable lease-event export
     (/var/lib/codex-recovery-public/lease-events.jsonl, root:pantheon-health
     0640; tools/codex_recovery.py LeaseExportSink). The root-only audit log is
     unreadable to both seats by design, so it is NOT read here. Each export line
     carries ts_wall and expires_at_wall: an interval runs from an acquire's
     ts_wall to its expires_at_wall, a renew extends it, a release ends it at the
     release's ts_wall. A missing or unreadable export means the lease state is
     UNKNOWN: one LEASE_STATE_UNKNOWN finding per run, and no REPAIR_WITHOUT_LEASE
     (unknown is not "no lease"). The export is best-effort on the broker side,
     so a lost line can only over-flag (a held lease looks absent), never hide a
     bypass.

CLASSIFIER. A whitelist over argv tokens (never a substring search). The sudo argv
is decoded, wrappers (VAR=val, env, exec, command, nohup, setsid, timeout) are
stripped, and one `bash|sh|dash -c <script>` level (two at most) is split into
simple commands with shlex and classified the same way:
  KILL_SESSION    tmux kill-session (or kill-server)
  CONTROL_KEYS    tmux send-keys whose keys are ALL control keys (C-c, Escape,
                  Enter, C-u, arrows, ...), or a single modal choice digit 1-9
                  (optionally + Enter); never with -l/-H (literal/hex text)
  UNIT_RESTART    systemctl --user restart|try-restart|reload-or-restart|stop|kill|
                  reset-failed of a pantheon-* / claude-telegram* / codex-* unit
  RESTART_SCRIPT  self_restart.sh, handoff_and_restart.sh, pantheon-seat-relaunch.sh
Everything else is not a repair. `send-keys -l <text>` and any send-keys that
types text are INTERVENTION: counted, never flagged. A command the parser cannot
split (unbalanced quotes, over-long, nested too deep) is OPAQUE: counted.

SCOPE. Actors: claude-fallback and codex-fallback only. Targets: the fleet
accounts (agent_role_map.fleet_accounts()) plus those two seats, minus the actor's
own account. Target accounts map to broker target keys via the manifest
(/etc/codex-recovery/manifest.json, structural parse); with no manifest the key
is the account name (the sample manifest's convention). A repair on an account
that is not a manifest target cannot be leased at all: it is counted
(repairs_unleasable), not flagged.

ROUTING. A finding by codex-fallback -> MAIL_RECOVERY; by
claude-fallback -> MAIL_RECOVERY_SEAT; the same (actor, target, class) >= 3 times in
24h -> PAGE_OPERATOR (once per key per 24h). Mails per key are rate-limited by a
cooldown. LEASE_STATE_UNKNOWN is logged, not mailed (expected until the broker is
installed, B11). Each mail line names its recipient persona (`to=`), resolved from
instance.yaml by the OTHER seat's account (mail_recipient()); the recovery seat's
persona is recovery_watcher.recovery_seat_persona(), so a rename is data-only.

INERTNESS CONTRACT (design §8, same as tools/recovery_watcher.py). Dry-run remains
the default. Live senders exist only behind make_live_senders(), which only live
mode calls and only when an action is due. Dry-run side effects are exactly: the
journalctl read, stdout lines and the --state-file. A failed live delivery is loud
and never earns a delivery cooldown or finding-dedupe stamp. The cadence retries
at most twice, then holds that route for 24h before allowing another bounded
attempt window; the transition to the hold exits non-zero once instead of sending
or paging every five minutes forever.

UNTRUSTED INPUT. Nothing from COMMAND text, PWD or any export-log string is ever
printed or stored. Output carries only enum members, ints, and account names that
matched the actor/fleet allowlists.
"""
from __future__ import annotations

import argparse
import enum
import fnmatch
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import agent_role_map  # noqa: E402
from tools.codex_recovery import (  # noqa: E402
    DEFAULT_LEASE_EXPORT_PATH,
    DEFAULT_MANIFEST_PATH,
    MAX_MANIFEST_BYTES,
    ManifestValidationError,
    parse_manifest_bytes,
)
from tools.health_export import atomic_write_json  # noqa: E402
from tools.recovery_watcher import (  # noqa: E402
    PAGE_TAG,
    RECOVERY_SEAT_ACCOUNT,
    RECOVERY_PERSONA,
    recovery_seat_persona,
)

DEFAULT_STATE_FILE = Path.home() / ".local/state/pantheon/lease_audit.json"
DEFAULT_SINCE = "-15min"

RECOVERY_ACCOUNT = "claude-fallback"
ACTOR_ACCOUNTS = (RECOVERY_ACCOUNT, RECOVERY_SEAT_ACCOUNT)

MAX_COMMAND_CHARS = 64 * 1024
MAX_SHELL_DEPTH = 2
MAX_EXPORT_BYTES = 64 * 1024 * 1024
MAX_EXPORT_LINE = 4096
JOURNAL_TIMEOUT_S = 120

PAGE_THRESHOLD = 3
PAGE_WINDOW_S = 24 * 3600
PAGE_COOLDOWN_S = 24 * 3600
MAIL_COOLDOWN_S = 3600
SUBMIT_PAIR_WINDOW_S = 30
REPORTED_TTL_S = 48 * 3600
MAX_STATE_ENTRIES = 5000
DELIVERY_MAX_ATTEMPTS = 2
DELIVERY_HOLD_S = 24 * 3600


class RepairClass(enum.Enum):
    KILL_SESSION = "KILL_SESSION"
    CONTROL_KEYS = "CONTROL_KEYS"
    UNIT_RESTART = "UNIT_RESTART"
    RESTART_SCRIPT = "RESTART_SCRIPT"


class Kind(enum.Enum):
    REPAIR = "REPAIR"
    INTERVENTION = "INTERVENTION"
    OPAQUE = "OPAQUE"
    OTHER = "OTHER"


class FindingClass(enum.Enum):
    REPAIR_WITHOUT_LEASE = "REPAIR_WITHOUT_LEASE"
    LEASE_STATE_UNKNOWN = "LEASE_STATE_UNKNOWN"
    JOURNAL_UNREADABLE = "JOURNAL_UNREADABLE"


class Action(enum.Enum):
    MAIL_RECOVERY = "MAIL_RECOVERY"
    MAIL_RECOVERY_SEAT = "MAIL_RECOVERY_SEAT"
    PAGE_OPERATOR = "PAGE_OPERATOR"


ACTOR_ROUTE = {RECOVERY_SEAT_ACCOUNT: Action.MAIL_RECOVERY, RECOVERY_ACCOUNT: Action.MAIL_RECOVERY_SEAT}


def mail_recipient(action: Action) -> str:
    """The persona a MAIL_* action goes to, from the manifest by account: Recovery's
    mapping for claude-fallback (else "recovery"), and recovery_seat_persona() for the
    recovery seat (else its neutral placeholder). Never a hard-coded seat name."""
    if action is Action.MAIL_RECOVERY_SEAT:
        return recovery_seat_persona()
    if action is Action.MAIL_RECOVERY:
        try:
            persona = agent_role_map.account_persona(RECOVERY_ACCOUNT)
        except Exception:  # noqa: BLE001
            persona = None
        return persona if isinstance(persona, str) and persona else RECOVERY_PERSONA
    raise ValueError(f"not a mail action: {action.value}")


@dataclass(frozen=True)
class Classification:
    repairs: frozenset = frozenset()
    intervention: bool = False
    opaque: bool = False
    # True when every repair here is a send-keys of submit keys only (Enter):
    # the second half of a text nudge, not a repair on its own (see run()).
    submit_only: bool = False

    @property
    def kind(self) -> Kind:
        if self.repairs and not (self.submit_only and self.intervention):
            return Kind.REPAIR
        if self.intervention:
            return Kind.INTERVENTION
        if self.opaque:
            return Kind.OPAQUE
        return Kind.OTHER

    def merge(self, other: "Classification") -> "Classification":
        repairs = self.repairs | other.repairs
        submit_only = bool(repairs) and (self.submit_only or not self.repairs) \
            and (other.submit_only or not other.repairs)
        return Classification(repairs, self.intervention or other.intervention,
                              self.opaque or other.opaque, submit_only)


OTHER = Classification()
OPAQUE = Classification(opaque=True)
INTERVENTION = Classification(intervention=True)


def _repair(cls: RepairClass) -> Classification:
    return Classification(repairs=frozenset({cls}))


# ------------------------------------------------------------- sudo decoding
_SYSLOG_ESC_RE = re.compile(r"#(0[0-3][0-7])")


def decode_sudo_command(text: str) -> list[str] | None:
    """Decode sudo's COMMAND= rendering into argv, or None if it isn't one.

    sudo joins argv with spaces, single-quotes an arg holding a space, and
    backslash-escapes `\\` and `'`; syslog renders control bytes as #0oo."""
    if not isinstance(text, str) or len(text) > MAX_COMMAND_CHARS:
        return None
    text = _SYSLOG_ESC_RE.sub(lambda m: chr(int(m.group(1), 8)), text)
    argv: list[str] = []
    cur: list[str] = []
    have = False
    quoted = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            if i + 1 >= n:
                return None
            cur.append(text[i + 1])
            have = True
            i += 2
            continue
        if ch == "'":
            quoted = not quoted
            have = True
        elif ch == " " and not quoted:
            if have:
                argv.append("".join(cur))
            cur, have = [], False
        else:
            cur.append(ch)
            have = True
        i += 1
    if quoted:
        return None
    if have:
        argv.append("".join(cur))
    return argv


_ACTOR_RE = r"[a-z_][a-z0-9_-]{0,31}"
# sudo right-aligns a short actor name with spaces ("   codex : ...").
_HEAD_RE = re.compile(rf"^ *({_ACTOR_RE}) : (.*)$", re.S)
_CONT_RE = re.compile(rf"^ *({_ACTOR_RE}) : \(command continued\) (.*)$", re.S)
_DENIED_RE = re.compile(rf"^ *{_ACTOR_RE} : (?![A-Z]+=)[^;]* ; ")
_FIELD_RE = re.compile(r"^([A-Z]+)=(.*)$", re.S)


@dataclass(frozen=True)
class SudoHead:
    actor: str
    user: str
    command: str


def parse_sudo_head(message: str) -> SudoHead | None:
    """Strictly parse `<actor> : KEY=v ; ... ; COMMAND=<cmd>`; None otherwise
    (including denied runs, whose first field is a reason, not KEY=)."""
    m = _HEAD_RE.match(message)
    if not m or _CONT_RE.match(message):
        return None
    rest = m.group(2)
    if rest.startswith("COMMAND="):
        header, command = "", rest[len("COMMAND="):]
    else:
        idx = rest.find(" ; COMMAND=")
        if idx < 0:
            return None
        header, command = rest[:idx], rest[idx + len(" ; COMMAND="):]
    fields = {}
    if header:
        for part in header.split(" ; "):
            fm = _FIELD_RE.match(part)
            if not fm:
                return None
            fields[fm.group(1)] = fm.group(2)
    user = fields.get("USER")
    if not user or not re.fullmatch(_ACTOR_RE, user):
        return None
    return SudoHead(actor=m.group(1), user=user, command=command)


# ------------------------------------------------------------- classifier
SHELLS = frozenset({"bash", "sh", "dash"})
RESTART_SCRIPTS = frozenset({"self_restart.sh", "handoff_and_restart.sh", "pantheon-seat-relaunch.sh"})
SHELL_KEYWORDS = frozenset({"{", "}", "!", "do", "then", "else", "elif", "if", "while", "until", "time"})
UNIT_PATTERNS = ("pantheon-*", "claude-telegram*", "codex-*")
SYSTEMCTL_REPAIR_VERBS = frozenset({"restart", "try-restart", "reload-or-restart",
                                    "try-reload-or-restart", "stop", "kill", "reset-failed"})
SYSTEMCTL_ARG_OPTS = frozenset({"-p", "-t", "-s", "-H", "-M", "-n", "-o", "--property", "--type",
                                "--signal", "--host", "--machine", "--lines", "--output", "--state",
                                "--kill-whom", "--kill-value", "--job-mode", "--root", "--what",
                                "--timestamp", "--when", "--message", "--image", "--drop-in",
                                "--preset-mode"})
NAMED_CONTROL_KEYS = frozenset(k.lower() for k in (
    "Enter", "Escape", "Tab", "BTab", "BSpace", "Up", "Down", "Left", "Right", "Home", "End",
    "PPage", "NPage", "PageUp", "PageDown", "PgUp", "PgDn", "KPEnter", "IC", "DC", "Insert", "Delete",
))
SUBMIT_KEYS = frozenset({"enter", "c-m", "^m", "kpenter"})
_MODAL_DIGIT_RE = re.compile(r"[1-9]")
_CTRL_KEY_RE = re.compile(r"^(?:[Cc]-|\^)[^\s]$")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
TMUX_GLOBAL_ARG_OPTS = frozenset({"-f", "-L", "-S", "-T"})
SEND_KEYS_ARG_FLAGS = frozenset("cNt")


def _base(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def _strip_wrappers(argv: list[str]) -> list[str]:
    i, n = 0, len(argv)
    while i < n:
        tok = argv[i]
        name = _base(tok)
        if _ASSIGN_RE.match(tok) or tok in SHELL_KEYWORDS:
            i += 1
        elif name == "env":
            i += 1
            while i < n and (argv[i].startswith("-") or _ASSIGN_RE.match(argv[i])):
                i += 2 if argv[i] in ("-u", "-C", "-S") else 1
        elif name in ("exec", "command", "nohup", "setsid"):
            i += 1
            while i < n and argv[i].startswith("-"):
                i += 2 if argv[i] == "-a" else 1
        elif name == "timeout":
            i += 1
            while i < n and argv[i].startswith("-"):
                i += 2 if argv[i] in ("-s", "-k") else 1
            i += 1  # the duration
        else:
            break
    return argv[i:]


def is_control_key(tok: str) -> bool:
    return tok.lower() in NAMED_CONTROL_KEYS or bool(_CTRL_KEY_RE.match(tok))


def _classify_send_keys(args: list[str]) -> Classification:
    flags: set[str] = set()
    i, n = 0, len(args)
    while i < n:
        tok = args[i]
        if tok == "--":
            i += 1
            break
        if not tok.startswith("-") or len(tok) < 2:
            break
        letters = tok[1:]
        j = 0
        while j < len(letters):
            f = letters[j]
            flags.add(f)
            if f in SEND_KEYS_ARG_FLAGS:
                if j == len(letters) - 1:
                    i += 1  # its argument is the next token
                break
            j += 1
        i += 1
    keys = args[i:]
    if "l" in flags or "H" in flags or "X" in flags:
        return INTERVENTION
    if not keys:
        return OTHER
    if _MODAL_DIGIT_RE.fullmatch(keys[0]) and (len(keys) == 1 or (len(keys) == 2 and keys[1].lower() in SUBMIT_KEYS)):
        # A single 1-9 (optionally + Enter) picks a modal/dialog option: a repair.
        return _repair(RepairClass.CONTROL_KEYS)
    if all(k.lower() in SUBMIT_KEYS for k in keys):
        return Classification(repairs=frozenset({RepairClass.CONTROL_KEYS}), submit_only=True)
    if all(is_control_key(k) for k in keys):
        return _repair(RepairClass.CONTROL_KEYS)
    return INTERVENTION


def _classify_tmux(args: list[str]) -> Classification:
    i, n = 0, len(args)
    while i < n and args[i].startswith("-"):
        if args[i] == "-c":
            return OPAQUE  # `tmux -c <shell-command>`
        i += 2 if args[i] in TMUX_GLOBAL_ARG_OPTS else 1
    result = OTHER
    cmd: list[str] = []
    for tok in args[i:] + [";"]:
        if tok != ";":
            cmd.append(tok)
            continue
        if cmd:
            name, rest = cmd[0], cmd[1:]
            if name == "kill-server" or (len(name) >= 8 and "kill-session".startswith(name)):
                result = result.merge(_repair(RepairClass.KILL_SESSION))
            elif name == "send" or (len(name) >= 6 and "send-keys".startswith(name)):
                result = result.merge(_classify_send_keys(rest))
        cmd = []
    return result


def _classify_systemctl(args: list[str]) -> Classification:
    if "--user" not in args:
        return OTHER
    positional: list[str] = []
    i, n = 0, len(args)
    while i < n:
        tok = args[i]
        if tok.startswith("-"):
            i += 2 if tok in SYSTEMCTL_ARG_OPTS else 1
            continue
        positional.append(tok)
        i += 1
    if not positional or positional[0] not in SYSTEMCTL_REPAIR_VERBS:
        return OTHER
    verb, units = positional[0], positional[1:]
    if verb == "reset-failed" and not units:
        return _repair(RepairClass.UNIT_RESTART)
    if any(fnmatch.fnmatchcase(u, p) for u in units for p in UNIT_PATTERNS):
        return _repair(RepairClass.UNIT_RESTART)
    return OTHER


_SHELL_PUNCT = ";&|()<>\n"


def split_shell(script: str) -> list[list[str]] | None:
    """Split a shell script into simple-command argv lists (redirections dropped)."""
    lex = shlex.shlex(script, posix=True, punctuation_chars=_SHELL_PUNCT)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        return None
    segments: list[list[str]] = []
    cur: list[str] = []
    skip_next = False
    for tok in tokens:
        is_punct = bool(tok) and all(c in _SHELL_PUNCT for c in tok)
        if skip_next and not is_punct:
            skip_next = False
            continue
        skip_next = False
        if is_punct and ("<" in tok or ">" in tok):
            if cur and cur[-1].isdigit():
                cur.pop()
            skip_next = True
            continue
        if is_punct:
            if cur:
                segments.append(cur)
            cur = []
            continue
        cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def _shell_payload(args: list[str]) -> tuple[str | None, str | None]:
    """For `bash [opts] -c script` return (script, None); for `bash file` (None, file)."""
    i, n = 0, len(args)
    while i < n:
        tok = args[i]
        if tok in ("-o", "+o", "-O", "+O"):
            i += 2
            continue
        if tok.startswith("-") and len(tok) > 1 and not tok.startswith("--"):
            if "c" in tok[1:]:
                return (args[i + 1] if i + 1 < n else None), None
            i += 1
            continue
        if tok.startswith("--"):
            i += 1
            continue
        return None, tok
    return None, None


def classify_argv(argv: list[str], depth: int = 0) -> Classification:
    argv = _strip_wrappers(argv)
    if not argv:
        return OTHER
    name = _base(argv[0])
    if name in RESTART_SCRIPTS:
        return _repair(RepairClass.RESTART_SCRIPT)
    if name in SHELLS:
        script, script_file = _shell_payload(argv[1:])
        if script_file is not None:
            return _repair(RepairClass.RESTART_SCRIPT) if _base(script_file) in RESTART_SCRIPTS else OTHER
        if script is None:
            return OTHER
        if depth >= MAX_SHELL_DEPTH or len(script) > MAX_COMMAND_CHARS:
            return OPAQUE
        segments = split_shell(script)
        if segments is None:
            return OPAQUE
        result = OTHER
        for seg in segments:
            result = result.merge(classify_argv(seg, depth + 1))
        return result
    if name == "tmux":
        return _classify_tmux(argv[1:])
    if name == "systemctl":
        return _classify_systemctl(argv[1:])
    return OTHER


def classify_command(command: str) -> Classification:
    argv = decode_sudo_command(command)
    if argv is None:
        return OPAQUE
    return classify_argv(argv)


# ------------------------------------------------------------- journal
@dataclass(frozen=True)
class SudoEvent:
    event_id: str
    ts_epoch: int
    actor: str
    user: str
    command: str


class JournalUnreadable(Exception):
    pass


def _since_ok(since: str) -> bool:
    return bool(re.fullmatch(r"-\d{1,6}(?:s|min|m|h|d)|@\d{1,12}", since))


class JournalctlSource:
    """Default journal port. The running account must be in systemd-journal."""

    def __init__(self, runner: Callable = subprocess.run):
        self._run = runner

    def _lines(self, argv: list[str]) -> list[str]:
        try:
            res = self._run(argv, capture_output=True, text=True, timeout=JOURNAL_TIMEOUT_S,
                            errors="replace")
        except (OSError, subprocess.SubprocessError) as exc:
            raise JournalUnreadable(exc.__class__.__name__) from exc
        if res.returncode != 0:
            raise JournalUnreadable(f"rc={res.returncode}")
        return res.stdout.splitlines()

    def readable(self) -> bool:
        return bool(self._lines(["journalctl", "--system", "-n", "1", "-o", "json", "--no-pager", "-q"]))

    def records(self, since: str) -> Iterable[dict]:
        for line in self._lines(["journalctl", "_COMM=sudo", f"--since={since}", "-o", "json",
                                 "--output-fields=MESSAGE,_PID", "--no-pager", "-q"]):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


def sudo_events(records: Iterable[dict], counts: dict) -> list[SudoEvent]:
    """Reassemble head + `(command continued)` records into SudoEvents."""
    heads: list[dict] = []
    last_by_pid: dict[str, int] = {}
    for rec in records:
        msg, pid, ts = rec.get("MESSAGE"), rec.get("_PID"), rec.get("__REALTIME_TIMESTAMP")
        if not isinstance(msg, str) or not isinstance(pid, str) or not isinstance(ts, str) \
                or not ts.isdigit():
            counts["unparseable"] = counts.get("unparseable", 0) + 1
            continue
        cont = _CONT_RE.match(msg)
        if cont:
            idx = last_by_pid.get(pid)
            if idx is not None and heads[idx]["actor"] == cont.group(1) \
                    and heads[idx]["len"] <= MAX_COMMAND_CHARS:
                heads[idx]["parts"].append(cont.group(2))
                heads[idx]["len"] += len(cont.group(2)) + 1
            continue
        if " ; COMMAND=" not in msg and " : COMMAND=" not in msg:
            continue  # pam session lines and the like
        head = parse_sudo_head(msg)
        if head is None:
            k = "denied" if _DENIED_RE.match(msg) else "unparseable"  # denied: never ran
            counts[k] = counts.get(k, 0) + 1
            continue
        heads.append({"actor": head.actor, "user": head.user, "parts": [head.command],
                      "len": len(head.command), "pid": pid, "ts": ts})
        last_by_pid[pid] = len(heads) - 1
    return [SudoEvent(event_id=f"{h['ts']}:{h['pid']}", ts_epoch=int(h["ts"]) // 1_000_000,
                      actor=h["actor"], user=h["user"], command=" ".join(h["parts"]))
            for h in heads]


# ------------------------------------------------------------- lease record
def _int_field(rec: dict, key: str) -> int | None:
    v = rec.get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _read_bounded(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_EXPORT_BYTES:
            return None
        chunks, total = [], 0
        while total <= MAX_EXPORT_BYTES:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            chunks.append(b)
            total += len(b)
        return None if total > MAX_EXPORT_BYTES else b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def load_lease_intervals(path: Path) -> dict[tuple[str, str], list[list[int]]] | None:
    """(holder_account, target_key) -> [[start, end), ...] in wall-clock epoch
    seconds (a release-ended interval includes the release second, T-5048) from the broker's readable lease-event export, or None when the
    export is missing/unreadable (UNKNOWN). Each export line carries ts_wall
    and expires_at_wall, so no clock inference is needed."""
    raw = _read_bounded(path)
    if raw is None:
        return None
    intervals: dict[tuple[str, str], list[list[int]]] = {}
    open_by_target: dict[str, tuple[str, list[int]]] = {}
    for line in raw.split(b"\n"):
        if not line or len(line) > MAX_EXPORT_LINE:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        op, user, target = rec.get("op"), rec.get("holder_user"), rec.get("target")
        ts, expires = _int_field(rec, "ts_wall"), _int_field(rec, "expires_at_wall")
        if not isinstance(user, str) or not isinstance(target, str) or ts is None or expires is None:
            continue
        held = open_by_target.get(target)
        if op in ("acquire", "renew"):
            if held and held[0] == user and held[1][1] > ts:
                held[1][1] = max(held[1][1], expires)  # renew of the open interval
            else:
                iv = [ts, expires]
                intervals.setdefault((user, target), []).append(iv)
                open_by_target[target] = (user, iv)
        elif op == "release":
            if held and held[0] == user:
                # T-5048: both clocks are whole seconds, so a repair stamped in the release's
                # own second may have preceded it; read that second as held (end = ts + 1).
                # Only a release is widened, never past the lease's own expiry, and an expiry
                # end stays exclusive: B13 attempt 5 sent Escape at 1792088204 and released at
                # 1792088204, and the half-open [s, ts) flagged it REPAIR_WITHOUT_LEASE.
                held[1][1] = max(held[1][0], min(held[1][1], ts + 1))
            open_by_target.pop(target, None)
        elif op == "expire_observed":
            if held and held[0] == user:
                held[1][1] = min(held[1][1], expires)
            open_by_target.pop(target, None)
    return intervals


def lease_held(intervals: dict, holder: str, target_key: str, ts: int) -> bool:
    return any(s <= ts < e for s, e in intervals.get((holder, target_key), ()))


def load_target_keys(manifest_path: Path) -> dict[str, str] | None:
    """account -> broker target key, from the manifest (structural parse), or
    None when no manifest is readable (keys are then the account names)."""
    raw = _read_bounded(manifest_path)
    if raw is None or len(raw) > MAX_MANIFEST_BYTES:
        return None
    try:
        manifest = parse_manifest_bytes(raw, pwnam=None)
    except ManifestValidationError:
        return None
    return {spec.account: key for key, spec in manifest.targets.items()}


# ------------------------------------------------------------- state
def _fresh_state() -> dict:
    return {"reported": {}, "seen": {}, "history": [], "mailed": {}, "paged": {},
            "delivery_failures": {}, "delivery_blocked": {}}


def load_state(path: Path) -> dict:
    state = _fresh_state()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if not isinstance(doc, dict):
        return state
    for key in ("reported", "seen", "mailed", "paged", "delivery_blocked"):
        v = doc.get(key)
        if isinstance(v, dict):
            state[key] = {k: t for k, t in v.items()
                          if isinstance(k, str) and isinstance(t, int) and not isinstance(t, bool)}
    failures = doc.get("delivery_failures")
    if isinstance(failures, dict):
        state["delivery_failures"] = {
            k: [v[0], v[1]] for k, v in failures.items()
            if isinstance(k, str) and isinstance(v, list) and len(v) == 2
            and all(isinstance(n, int) and not isinstance(n, bool) for n in v)
            and v[0] > 0
        }
    hist = doc.get("history")
    if isinstance(hist, list):
        state["history"] = [h for h in hist if isinstance(h, list) and len(h) == 2
                            and isinstance(h[0], str) and isinstance(h[1], int)]
    return state


def _prune(state: dict, now: int) -> dict:
    def newest(d: dict, ttl: int) -> dict:
        kept = sorted(((k, t) for k, t in d.items() if now - t < ttl), key=lambda kv: kv[1])
        return dict(kept[-MAX_STATE_ENTRIES:])
    return {
        "reported": newest(state["reported"], REPORTED_TTL_S),
        "seen": newest(state["seen"], REPORTED_TTL_S),
        "mailed": newest(state["mailed"], MAIL_COOLDOWN_S),
        "paged": newest(state["paged"], PAGE_COOLDOWN_S),
        "delivery_failures": dict(sorted(
            ((k, v) for k, v in state["delivery_failures"].items()
             if now - v[1] < DELIVERY_HOLD_S), key=lambda kv: kv[1][1])[-MAX_STATE_ENTRIES:]),
        "delivery_blocked": newest(state["delivery_blocked"], DELIVERY_HOLD_S),
        "history": [h for h in state["history"] if now - h[1] < PAGE_WINDOW_S][-MAX_STATE_ENTRIES:],
    }


# ------------------------------------------------------------- live
class LiveSenders:
    """Mailbox + alert-bot side effects. Never built in dry-run."""

    def __init__(self, page, mail):
        self._page = page
        self._mail = mail

    def send(self, action: Action, payload):
        if action is Action.PAGE_OPERATOR:
            return self._page.send(payload)
        if action in (Action.MAIL_RECOVERY, Action.MAIL_RECOVERY_SEAT):
            return self._mail.send(payload)
        raise ValueError(f"unsupported live action: {action.value}")


def make_live_senders() -> LiveSenders:
    """The ONLY constructor of live side-effect objects. Called only in live mode."""
    from tools.recovery_watcher import MailSender, PageSender
    return LiveSenders(PageSender(), MailSender())


def action_payload(action: Action, *, actor: str, target: str, repair_class: RepairClass,
                   ts_epoch: int, count_24h: int):
    """Render an enum/allowlist-only payload. Journal COMMAND/PWD never enter it."""
    common = {
        "finding": FindingClass.REPAIR_WITHOUT_LEASE.value,
        "actor_account": actor,
        "target_account": target,
        "repair_class": repair_class.value,
        "ts_epoch": ts_epoch,
        "count_24h": count_24h,
    }
    if action is Action.PAGE_OPERATOR:
        lines = [PAGE_TAG, "LEASE_AUDIT escalation"]
        lines.extend(f"{key}: {value}" for key, value in common.items())
        return "\n".join(lines)
    recipient = mail_recipient(action)
    sender = agent_role_map.account_persona(os.environ.get("USER", "")) or "lease-audit"
    return {
        "recipient": f"{agent_role_map.mailbox_hub()}/{recipient}",
        "sender": sender,
        "subject": f"lease audit: {FindingClass.REPAIR_WITHOUT_LEASE.value}",
        "severity": "urgent",
        "body": common,
    }


# ------------------------------------------------------------- run
def _finding(fc: FindingClass, **fields) -> str:
    return json.dumps({"finding": fc.value, **fields}, sort_keys=True, separators=(",", ":"))


def run(*, since: str, state_file: Path, lease_events: Path, manifest: Path,
        source=None, fleet: Iterable[str] | None = None, now: int | None = None,
        out=None, live: bool = False, senders: LiveSenders | None = None) -> int:
    out = sys.stdout if out is None else out
    now = int(time.time()) if now is None else int(now)
    source = JournalctlSource() if source is None else source
    fleet_set = frozenset(agent_role_map.fleet_accounts() if fleet is None else fleet) | frozenset(ACTOR_ACCOUNTS)

    def log(line: str) -> None:
        print(f"lease_audit: {line}", file=out)

    counts = {"unparseable": 0, "denied": 0}
    try:
        if not source.readable():
            raise JournalUnreadable("positive control returned no record")
        events = sudo_events(source.records(since), counts)
    except JournalUnreadable:
        print(_finding(FindingClass.JOURNAL_UNREADABLE), file=out)
        log(f"summary mode={'live' if live else 'dry-run'} journal=unreadable "
            "(run under `sg systemd-journal`)")
        return 2

    intervals = load_lease_intervals(lease_events)
    target_keys = load_target_keys(manifest)
    state = load_state(state_file)
    tally = {k: 0 for k in ("actor_events", "repairs", "interventions", "submits_paired", "opaque",
                            "other", "repairs_unleasable", "repairs_unadjudicated",
                            "repairs_other_actors", "findings", "new_findings", "suppressed", "pages")}
    by_class = {c: 0 for c in RepairClass}
    pending_findings: list[tuple[str, str, RepairClass, int, str, bool]] = []
    last_text: dict[tuple[str, str], int] = {}

    for ev in events:
        own = ev.actor == ev.user
        if ev.user not in fleet_set or own:
            continue
        cls = classify_command(ev.command)
        if ev.actor not in ACTOR_ACCOUNTS:
            tally["repairs_other_actors"] += cls.kind is Kind.REPAIR
            continue
        tally["actor_events"] += 1
        kind = cls.kind
        pair = (ev.actor, ev.user)
        if kind is Kind.REPAIR and cls.submit_only and pair in last_text \
                and 0 <= ev.ts_epoch - last_text[pair] <= SUBMIT_PAIR_WINDOW_S:
            # The Enter that submits a text nudge sent just before (the two-step
            # send_keys_submit shape) belongs to that intervention. A lone Enter
            # with no text before it stays a CONTROL_KEYS repair.
            kind = Kind.INTERVENTION
            tally["submits_paired"] += 1
        if cls.intervention:
            last_text[pair] = ev.ts_epoch
        if kind is Kind.INTERVENTION:
            tally["interventions"] += 1
        elif kind is Kind.OPAQUE:
            tally["opaque"] += 1
        elif kind is Kind.OTHER:
            tally["other"] += 1
        if kind is not Kind.REPAIR:
            continue
        tally["repairs"] += 1
        key = ev.user if target_keys is None else target_keys.get(ev.user)
        for rc in sorted(cls.repairs, key=lambda c: c.value):
            by_class[rc] += 1
            if key is None:
                tally["repairs_unleasable"] += 1
                continue
            if intervals is None:
                tally["repairs_unadjudicated"] += 1
                continue
            # The broker names the holder from getpwuid(SUDO_UID), so the
            # export's holder_user is already uid-anchored: match on it.
            if lease_held(intervals, ev.actor, key, ev.ts_epoch):
                continue
            tally["findings"] += 1
            fid = f"{ev.event_id}:{rc.value}"
            if fid not in state["reported"]:  # overlapping windows: report each repair once
                first_seen = fid not in state["seen"]
                if first_seen:
                    print(_finding(FindingClass.REPAIR_WITHOUT_LEASE, actor_account=ev.actor,
                                   target_account=ev.user, repair_class=rc.value,
                                   ts_epoch=ev.ts_epoch), file=out)
                    state["seen"][fid] = now
                    pkey = f"{ev.actor}|{ev.user}|{rc.value}"
                    state["history"].append([pkey, ev.ts_epoch])
                pending_findings.append((ev.actor, ev.user, rc, ev.ts_epoch, fid, first_seen))

    if intervals is None:
        print(_finding(FindingClass.LEASE_STATE_UNKNOWN), file=out)

    live_senders = senders
    attempted_delivery_keys: set[str] = set()
    rc_send = 0

    def dispatch(action: Action, payload, delivery_key: str) -> bool:
        """One bounded delivery attempt per route key per cadence fire."""
        nonlocal live_senders, rc_send
        blocked_at = state["delivery_blocked"].get(delivery_key)
        if blocked_at is not None and now - blocked_at < DELIVERY_HOLD_S:
            log(f"suppressed_delivery_blocked action={action.value} hold_s={DELIVERY_HOLD_S}")
            return False
        if blocked_at is not None:
            state["delivery_blocked"].pop(delivery_key, None)
            state["delivery_failures"].pop(delivery_key, None)
        if delivery_key in attempted_delivery_keys:
            return False
        attempted_delivery_keys.add(delivery_key)
        if live_senders is None:
            live_senders = make_live_senders()
        try:
            result = live_senders.send(action, payload)
            if result is False:
                raise RuntimeError("unconfirmed")
        except Exception as exc:  # noqa: BLE001 - sender failure is enum-only and fail-loud
            prior = state["delivery_failures"].get(delivery_key, [0, 0])[0]
            attempts = prior + 1
            state["delivery_failures"][delivery_key] = [attempts, now]
            rc_send = 4
            if attempts >= DELIVERY_MAX_ATTEMPTS:
                state["delivery_blocked"][delivery_key] = now
                log(f"delivery_blocked action={action.value} attempts={attempts} "
                    f"hold_s={DELIVERY_HOLD_S} error={exc.__class__.__name__}")
            else:
                log(f"send_failed action={action.value} attempt={attempts}/"
                    f"{DELIVERY_MAX_ATTEMPTS} error={exc.__class__.__name__}")
            return False
        state["delivery_failures"].pop(delivery_key, None)
        state["delivery_blocked"].pop(delivery_key, None)
        return True

    for actor, target, rc, ts, fid, first_seen in pending_findings:
        tally["new_findings"] += int(first_seen)
        pkey = f"{actor}|{target}|{rc.value}"
        tag = f"actor={actor} target={target} class={rc.value}"
        recent = sum(1 for k, t in state["history"] if k == pkey and now - t < PAGE_WINDOW_S)
        mail_action = ACTOR_ROUTE[actor]
        all_done = True
        if now - state["mailed"].get(pkey, -MAIL_COOLDOWN_S) >= MAIL_COOLDOWN_S:
            if live:
                log(f"act action={mail_action.value} to={mail_recipient(mail_action)} "
                    f"finding=REPAIR_WITHOUT_LEASE {tag}")
                payload = action_payload(mail_action, actor=actor, target=target,
                                         repair_class=rc, ts_epoch=ts, count_24h=recent)
                if dispatch(mail_action, payload, f"{mail_action.value}|{pkey}"):
                    state["mailed"][pkey] = now
                else:
                    all_done = False
            else:
                state["mailed"][pkey] = now
                log(f"would_act action={mail_action.value} to={mail_recipient(mail_action)} "
                    f"finding=REPAIR_WITHOUT_LEASE {tag}")
        else:
            tally["suppressed"] += 1
            log(f"suppressed_cooldown action={mail_action.value} "
                f"to={mail_recipient(mail_action)} {tag}")
        page_due = (recent >= PAGE_THRESHOLD
                    and now - state["paged"].get(pkey, -PAGE_COOLDOWN_S) >= PAGE_COOLDOWN_S)
        if page_due:
            tally["pages"] += 1
            if live:
                log(f"act action={Action.PAGE_OPERATOR.value} finding=REPAIR_WITHOUT_LEASE "
                    f"{tag} count_24h={recent}")
                payload = action_payload(Action.PAGE_OPERATOR, actor=actor, target=target,
                                         repair_class=rc, ts_epoch=ts, count_24h=recent)
                if dispatch(Action.PAGE_OPERATOR, payload, f"{Action.PAGE_OPERATOR.value}|{pkey}"):
                    state["paged"][pkey] = now
                else:
                    all_done = False
            else:
                state["paged"][pkey] = now
                log(f"would_act action={Action.PAGE_OPERATOR.value} finding=REPAIR_WITHOUT_LEASE "
                    f"{tag} count_24h={recent}")
        if all_done:
            state["reported"][fid] = now

    classes = ",".join(f"{c.value}:{by_class[c]}" for c in RepairClass)
    log(f"summary mode={'live' if live else 'dry-run'} since={since} sudo_events={len(events)} "
        f"unparseable={counts['unparseable']} denied={counts['denied']} lease_state={'unknown' if intervals is None else 'known'} "
        f"targets={'manifest' if target_keys is not None else 'account-names'} "
        + " ".join(f"{k}={v}" for k, v in tally.items()) + f" repairs_by_class={classes}")

    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(state_file.parent, state_file.name,
                          {"schema_version": 2, "updated_at": now, **_prune(state, now)})
    except OSError as exc:
        log(f"error state_file_write_failed {exc.__class__.__name__}")
        return 2
    return rc_send


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--since", default=DEFAULT_SINCE,
                    help="journal window: -<N>{s,min,h,d} or @<epoch> (default %(default)s)")
    ap.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    ap.add_argument("--lease-events", type=Path, default=DEFAULT_LEASE_EXPORT_PATH)
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print would-act lines only (default)")
    mode.add_argument("--live", action="store_true",
                      help="send enum-only mailbox/page actions for new findings")
    argv = list(sys.argv[1:] if argv is None else argv)
    # `--since -2h`: argparse reads a dash-led value as an option; glue it on.
    for i in range(len(argv) - 1):
        if argv[i] == "--since":
            argv[i:i + 2] = [f"--since={argv[i + 1]}"]
            break
    args = ap.parse_args(argv)
    if not _since_ok(args.since):
        print("lease_audit: --since must be -<N>{s,min,m,h,d} or @<epoch>", file=sys.stderr)
        return 64
    return run(since=args.since, state_file=args.state_file, lease_events=args.lease_events,
               manifest=args.manifest, live=args.live)


if __name__ == "__main__":
    sys.exit(main())
