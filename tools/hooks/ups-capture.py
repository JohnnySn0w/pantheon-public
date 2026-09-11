#!/usr/bin/env python3
"""UserPromptSubmit CAPTURE hook (T-3241) — observe the real Claude Code hook contract.

WHY THIS EXISTS. ORG-073 §4 says the Concordance build MUST verify the harness
hook contract — native stdin fields, additionalContext output shape, stdout/stderr
rules, timeout behaviour + its actual value, exit-code semantics, fail-open
behaviour, hook trust/allowlisting, session-ID stability across turns, and whether
multiple UserPromptSubmit hooks compose or overwrite — and that documented defaults
are not evidence. A subagent cannot produce that observation (subagent prompts do
not fire UserPromptSubmit), so this hook is wired into a REAL interactive session
by the Orchestrator and simply records what arrives. The encoder that follows
(T-3242) is then written against these captures, not against documentation.

WHAT IT DOES (default mode):
  * reads stdin (bounded — see STDIN_DEADLINE_SECONDS), stores the bytes VERBATIM;
  * snapshots the environment (secret-named VALUES redacted, names + lengths kept);
  * records wall clock + monotonic clock + boot id + pid/ppid/cwd/argv;
  * writes ONE FILE PER FIRE, atomically, under the capture directory;
  * emits NOTHING on stdout, NOTHING on stderr;
  * exits 0 on every path. Any internal failure is swallowed and, where the record
    can still be written, listed under "errors" in that fire's capture file.

CAPTURE DIRECTORY: ~/.claude/hooks/.ups_capture/ (sibling of recall-feedback's
.recall_seen state dir), overridable with $UPS_CAPTURE_DIR (tests). Dir 0700,
files 0600. Hard cap MAX_CAPTURE_FILES: at the cap the hook writes nothing and
still exits 0 (a forgotten hook must not grow disk without bound).

FILENAME (lexical order == fire order):
  ups_<UTC YYYYMMDDTHHMMSS.ffffffZ>_<monotonic_ns, 20 digits>_<sid8>[_<label>]_<pid>.json
  sid8 = first 8 chars of the payload's `session_id` iff the payload parsed to a
  dict with a string `session_id`, else "nosid". A convenience for `ls`; the
  verbatim record inside the file is authoritative.

RECORD (one JSON object):
  capture_schema, ticket, hook_basename, label,
  clock{utc, wall_ns, monotonic_ns, boot_id},
  process{pid, ppid, uid, cwd, argv, python},
  stdin{bytes, utf8, raw, raw_b64, json, json_error, read_hit_deadline},
  env{...}, env_redacted_names[...],
  emit{enabled, reason, until, shape, stdout},
  elapsed_ms, errors[...]
  `stdin.raw` is the verbatim decoded text (or `raw_b64` if not UTF-8);
  `stdin.json` is a parsed COPY for jq convenience and is NOT authoritative.

SESSION-ID STABILITY ACROSS TURNS is answerable from the files alone via four
independent keys per file: payload `session_id` (inside stdin.raw, verbatim),
env CLAUDE_CODE_SESSION_ID (verified present on this seat's hook-adjacent
processes, 2026-08-25), process.ppid (the harness process — constant within a
session regardless of payload naming), and clock.boot_id + monotonic_ns for
strict ordering (clock.utc / wall_ns for cross-boot, human correlation).

OPT-IN EMIT MODE (the compose-vs-overwrite probe) — OFF BY DEFAULT:
  UPS_CAPTURE_EMIT_UNTIL=<ISO-8601 UTC instant, e.g. 2026-08-26T02:00:00Z>
    Emits iff the value parses (timezone REQUIRED), now < until, and
    until - now <= EMIT_MAX_WINDOW_SECONDS (24 h). Unset / empty / unparseable /
    past / too-far-ahead => OFF, with the reason recorded in that fire's
    capture under emit.reason. An expiry is a clock; a boolean is a memory —
    this cannot be left on by accident.
  UPS_CAPTURE_EMIT_SHAPE=json|plain   (default json)
    json  -> {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                     "additionalContext": "<marker>"}}
    plain -> the bare marker line on stdout
    Both are candidate shapes the harness may honour on this event; the live
    run measures which. The exact emitted text is stored in emit.stdout so the
    transcript can be reconciled against disk.
  Marker (one line, no newline inside):
    UPS-CAPTURE-PROBE label=<label|none> capture=<filename|none> mono=<monotonic_ns> -- T-3241 compose-vs-overwrite probe; ignore.

!!! THE COMPOSE-VS-OVERWRITE ANSWER REQUIRES TWO REGISTRATIONS OF THIS SCRIPT !!!
The seat's only other UserPromptSubmit hook (hook-handler.js, agent-monitor)
POSTs the event to a local dashboard and writes NOTHING to stdout (read at
source 2026-08-25). A lone emitter cannot distinguish "compose" from
"overwrite". Wire this script TWICE in the UserPromptSubmit hooks array with
distinct labels, e.g.
    {"type": "command", "command": "/home/claude/.claude/hooks/ups-capture.py --label A"}
    {"type": "command", "command": "/home/claude/.claude/hooks/ups-capture.py --label B"}
and set UPS_CAPTURE_EMIT_UNTIL for the time-boxed session. Two markers visible
to the model => hooks COMPOSE; one => they OVERWRITE (the capture files say
which label's file was written and what each emitted). `--label` is sanitised
to [A-Za-z0-9_-] (max 32) and is never fatal; each command individually
passes context_fill_monitor's allowlist check (one script-like token, no
shell metacharacters; an env-assignment prefix such as
`UPS_CAPTURE_EMIT_UNTIL=... /path/ups-capture.py` is also accepted).

WIRING (the interactive step, done by the Orchestrator, NOT by this
file): symlink into ~/.claude/hooks/ (see tools/hooks/README.md), add to
~/.claude/settings.json as a UserPromptSubmit hook, AND — FIRST — make sure
this basename is in HOOK_ALLOWLIST["orchestrator"] in tools/context_fill_monitor.py
on origin/main (T-1947 ordering: the T-3204 checker re-reads the allowlist
from origin/main, and the T-1102 pre-commit guard refuses commits from a seat
whose wired hooks are not allowlisted).

Fail-open contract: `main()` never lets an exception reach the harness; the
`__main__` guard additionally catches BaseException and exits via os._exit(0)
after a best-effort flush, so even a broken pipe at interpreter shutdown cannot
turn the exit code non-zero.
"""

from __future__ import annotations

import base64
import json
import os
import re
import select
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, TextIO

CAPTURE_SCHEMA = "ups-capture/1"
TICKET = "T-3241"
HOOK_BASENAME = "ups-capture.py"

CAPTURE_DIR_ENV = "UPS_CAPTURE_DIR"
EMIT_UNTIL_ENV = "UPS_CAPTURE_EMIT_UNTIL"
EMIT_SHAPE_ENV = "UPS_CAPTURE_EMIT_SHAPE"
EMIT_SHAPES = ("json", "plain")
EMIT_MAX_WINDOW_SECONDS = 24 * 3600

# A harness that hands us stdin but never closes it must not convert "well under
# one second" into "hook timeout": read with a deadline, record if it fired.
STDIN_DEADLINE_SECONDS = 0.75
STDIN_CHUNK_BYTES = 65536

# Disk bound for a hook nobody is watching. At the cap: write nothing, exit 0.
MAX_CAPTURE_FILES = 2000

MARKER_PREFIX = "UPS-CAPTURE-PROBE"
HOOK_EVENT_NAME = "UserPromptSubmit"

_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")

# Env var NAMES whose VALUES never reach disk. Over-inclusive on purpose (a
# non-secret with a secret-shaped name loses only its value; name + length stay).
_SECRET_NAME_RE = re.compile(
    r"TOKEN|SECRET|PASSW|PASSPHRASE|CREDENTIAL|API_?KEY|PRIVATE|_KEY$|^KEY$|AUTH",
    re.IGNORECASE,
)
# A secret value this long or longer is also scrubbed wherever it appears in
# OTHER env values (e.g. embedded in a URL under an innocent name).
MIN_SECRET_VALUE_LEN = 8

_LABEL_RE = re.compile(r"[^A-Za-z0-9_-]")
_SID_RE = re.compile(r"[^A-Za-z0-9-]")


def _clean(s: str) -> str:
    """Make a surrogateescape'd string (os.environ / argv) safely JSON-encodable."""
    return s.encode("utf-8", "replace").decode("utf-8", "replace")


def read_stdin_bounded(fd: int | None, deadline_s: float = STDIN_DEADLINE_SECONDS) -> tuple[bytes, bool]:
    """Read fd to EOF or until deadline_s elapses. Returns (data, hit_deadline).

    fd None (stdin closed at startup) -> (b"", False). Never raises.
    """
    if fd is None:
        return b"", False
    data = bytearray()
    hit_deadline = False
    deadline = time.monotonic() + deadline_s
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                hit_deadline = True
                break
            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                hit_deadline = True
                break
            chunk = os.read(fd, STDIN_CHUNK_BYTES)
            if not chunk:
                break  # EOF
            data.extend(chunk)
    except (OSError, ValueError):
        pass  # partial data is still data; the record notes what we got
    return bytes(data), hit_deadline


def parse_label(argv: list[str]) -> str | None:
    """`--label X` / `--label=X` from argv, sanitised to [A-Za-z0-9_-] (max 32); else None."""
    try:
        raw: str | None = None
        for i, tok in enumerate(argv):
            if tok == "--label":
                if i + 1 < len(argv):
                    raw = argv[i + 1]
                break
            if tok.startswith("--label="):
                raw = tok.split("=", 1)[1]
                break
        if raw is None:
            return None
        label = _LABEL_RE.sub("", _clean(raw))[:32]
        return label or None
    except Exception:
        return None  # a bad label must never cost us the capture


def env_snapshot(environ: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """Copy of environ with secret-named values replaced by "<redacted len=N>" and
    any such value (>= MIN_SECRET_VALUE_LEN chars) scrubbed out of other values.
    Returns (snapshot, sorted names whose values were touched)."""
    snapshot: dict[str, str] = {}
    touched: set[str] = set()
    secret_values: list[str] = []
    for name, value in environ.items():
        cname, cvalue = _clean(str(name)), _clean(str(value))
        if _SECRET_NAME_RE.search(cname):
            snapshot[cname] = f"<redacted len={len(cvalue)}>"
            touched.add(cname)
            if len(cvalue) >= MIN_SECRET_VALUE_LEN:
                secret_values.append(cvalue)
        else:
            snapshot[cname] = cvalue
    # Second pass: a secret VALUE embedded in some other value (URL, prose)
    # under an innocent name must not reach disk either.
    if secret_values:
        for name, value in list(snapshot.items()):
            if name in touched:
                continue  # already a placeholder
            scrubbed = value
            for sv in secret_values:
                if sv in scrubbed:
                    scrubbed = scrubbed.replace(sv, f"<redacted len={len(sv)}>")
                    touched.add(name)
            snapshot[name] = scrubbed
    return snapshot, sorted(touched)


def emit_decision(environ: Mapping[str, str], now: datetime) -> dict:
    """{"enabled": bool, "reason": str, "until": str|None, "shape": str|None}.

    Enabled iff UPS_CAPTURE_EMIT_UNTIL parses as a tz-aware ISO-8601 instant,
    now < until, until - now <= EMIT_MAX_WINDOW_SECONDS, and the shape is known.
    """
    raw_until = environ.get(EMIT_UNTIL_ENV) or ""
    shape = environ.get(EMIT_SHAPE_ENV) or "json"
    off = {"enabled": False, "reason": "", "until": raw_until or None, "shape": None}
    if not raw_until:
        off["reason"] = f"{EMIT_UNTIL_ENV} unset"
        return off
    try:
        until = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
    except ValueError:
        off["reason"] = f"{EMIT_UNTIL_ENV} unparseable: {raw_until!r}"
        return off
    if until.tzinfo is None:
        off["reason"] = f"{EMIT_UNTIL_ENV} lacks a timezone (must be an instant): {raw_until!r}"
        return off
    if now >= until:
        off["reason"] = f"{EMIT_UNTIL_ENV} already past (expired probe window): {raw_until!r}"
        return off
    if (until - now).total_seconds() > EMIT_MAX_WINDOW_SECONDS:
        off["reason"] = (
            f"{EMIT_UNTIL_ENV} more than {EMIT_MAX_WINDOW_SECONDS}s ahead "
            f"(refusing an effectively-permanent emit): {raw_until!r}"
        )
        return off
    if shape not in EMIT_SHAPES:
        off["reason"] = f"{EMIT_SHAPE_ENV} unknown shape: {shape!r}"
        return off
    return {"enabled": True, "reason": "within emit window", "until": raw_until, "shape": shape}


def session_short_id(parsed: object) -> str:
    """First 8 chars of a dict payload's string `session_id`, else "nosid"."""
    try:
        if isinstance(parsed, dict):
            sid = parsed.get("session_id")
            if isinstance(sid, str):
                short = _SID_RE.sub("", sid)[:8]
                if short:
                    return short
    except Exception:
        pass
    return "nosid"


def capture_filename(utc_compact: str, monotonic_ns: int, sid8: str, label: str | None, pid: int) -> str:
    parts = ["ups", utc_compact, f"{monotonic_ns:020d}", sid8]
    if label:
        parts.append(label)
    parts.append(str(pid))
    return "_".join(parts) + ".json"


def build_marker(label: str | None, capture_name: str | None, monotonic_ns: int) -> str:
    return (
        f"{MARKER_PREFIX} label={label or 'none'} capture={capture_name or 'none'} "
        f"mono={monotonic_ns} -- {TICKET} compose-vs-overwrite probe; ignore."
    )


def render_emit(marker: str, shape: str) -> str:
    """The exact text written to stdout for the given shape (newline-terminated)."""
    if shape == "plain":
        return marker + "\n"
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": HOOK_EVENT_NAME,
            "additionalContext": marker,
        }
    }) + "\n"


def capture_dir(environ: Mapping[str, str]) -> Path:
    override = environ.get(CAPTURE_DIR_ENV)
    if override:
        return Path(override)
    home = environ.get("HOME")
    base = Path(home) if home else Path.home()
    return base / ".claude" / "hooks" / ".ups_capture"


def write_capture(directory: Path, filename: str, record: dict, max_files: int = MAX_CAPTURE_FILES) -> Path | None:
    """Atomic write (tmp + os.replace), 0600, under a 0700 dir. Returns the final
    path, or None when the cap is reached. Raises on I/O failure (caller records)."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)  # mkdir mode is umask-masked; assert it outright
    existing = 0
    for _ in directory.glob("ups_*.json"):
        existing += 1
        if existing >= max_files:
            return None  # silent skip at the cap — unattended trustworthiness
    final = directory / filename
    tmp = directory / (filename + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False)
            fh.write("\n")
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.replace(tmp, final)
    return final


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    stdin_reader: Callable[[], tuple[bytes, bool]] | None = None,
    stdout: TextIO | None = None,
) -> dict:
    """The whole hook as one composable function. Returns the record it built.
    Every stage is individually fail-open; the record's "errors" lists what broke."""
    t0 = time.monotonic()
    errors: list[str] = []
    args = list(sys.argv) if argv is None else list(argv)
    env: Mapping[str, str] = os.environ if environ is None else environ
    out = sys.stdout if stdout is None else stdout

    label = parse_label(args)

    mono_ns = time.monotonic_ns()
    wall_ns = time.time_ns()
    now = datetime.now(timezone.utc)
    boot_id: str | None = None
    try:
        boot_id = _BOOT_ID_PATH.read_text(encoding="ascii").strip()
    except OSError:
        errors.append("boot_id unreadable")

    if stdin_reader is None:
        def stdin_reader() -> tuple[bytes, bool]:
            try:
                fd = sys.stdin.fileno()
            except Exception:  # stdin closed/absent at startup
                return b"", False
            return read_stdin_bounded(fd)
    try:
        data, hit_deadline = stdin_reader()
    except Exception as exc:
        data, hit_deadline = b"", False
        errors.append(f"stdin read failed: {exc!r}")

    raw: str | None
    raw_b64: str | None
    try:
        raw = data.decode("utf-8")
        utf8 = True
        raw_b64 = None
    except UnicodeDecodeError:
        raw = None
        utf8 = False
        raw_b64 = base64.b64encode(data).decode("ascii")

    parsed: object = None
    json_error: str | None = None
    if utf8 and raw is not None and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            json_error = str(exc)
    elif not utf8:
        json_error = "stdin is not UTF-8 (raw_b64 carries the verbatim bytes)"

    sid8 = session_short_id(parsed)

    try:
        env_snap, redacted_names = env_snapshot(env)
    except Exception as exc:
        env_snap, redacted_names = {}, []
        errors.append(f"env snapshot failed: {exc!r}")

    try:
        emit = emit_decision(env, now)
    except Exception as exc:
        emit = {"enabled": False, "reason": f"emit_decision failed: {exc!r}",
                "until": None, "shape": None}
        errors.append(f"emit_decision failed: {exc!r}")
    emit["stdout"] = None

    try:
        cwd: str | None = os.getcwd()
    except OSError:
        cwd = None

    filename: str | None = None
    try:
        utc_compact = now.strftime("%Y%m%dT%H%M%S.%fZ")
        filename = capture_filename(utc_compact, mono_ns, sid8, label, os.getpid())
    except Exception as exc:
        errors.append(f"capture_filename failed: {exc!r}")

    # The exact text we intend to emit is frozen into the record BEFORE the
    # write, so any record that reaches disk states its own stdout verbatim.
    if emit["enabled"]:
        emit["stdout"] = render_emit(build_marker(label, filename, mono_ns), emit["shape"])

    record = {
        "capture_schema": CAPTURE_SCHEMA,
        "ticket": TICKET,
        "hook_basename": HOOK_BASENAME,
        "label": label,
        "clock": {
            "utc": now.isoformat().replace("+00:00", "Z"),
            "wall_ns": wall_ns,
            "monotonic_ns": mono_ns,
            "boot_id": boot_id,
        },
        "process": {
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "uid": os.getuid(),
            "cwd": _clean(cwd) if cwd else None,
            "argv": [_clean(a) for a in args],
            "python": "%d.%d.%d" % sys.version_info[:3],
        },
        "stdin": {
            "bytes": len(data),
            "utf8": utf8,
            "raw": raw,
            "raw_b64": raw_b64,
            "json": parsed,
            "json_error": json_error,
            "read_hit_deadline": hit_deadline,
        },
        "env": env_snap,
        "env_redacted_names": redacted_names,
        "emit": emit,
        "elapsed_ms": None,
        "errors": errors,
    }
    record["elapsed_ms"] = round((time.monotonic() - t0) * 1000.0, 3)

    written: Path | None = None
    if filename is not None:
        try:
            written = write_capture(capture_dir(env), filename, record)
        except Exception as exc:
            errors.append(f"write_capture failed: {exc!r}")

    # Emit AFTER the write attempt: a persisted record implies a successful
    # write, so its stored emit.stdout matches what was actually emitted; when
    # no record persisted (cap / unwritable), the marker says capture=none.
    if emit["enabled"]:
        if written is not None:
            text = emit["stdout"]
        else:
            text = render_emit(build_marker(label, None, mono_ns), emit["shape"])
        try:
            out.write(text)
            out.flush()
        except Exception as exc:
            errors.append(f"emit write failed: {exc!r}")
        emit["stdout"] = text
    return record


def entry(exit_fn: Callable[[int], None] = os._exit) -> None:
    """Process entry: run main(), swallow ANYTHING, flush, exit 0 via exit_fn."""
    try:
        main()
    except BaseException:
        pass  # fail-open: a capture probe must never brick a session
    try:
        sys.stdout.flush()
    except BaseException:
        pass
    exit_fn(0)


if __name__ == "__main__":
    entry()
