#!/usr/bin/env python3
"""Carry a failed memex unit on LXC 203 to archivist without anyone having to look.

T-4598. Before this, a failed `--user` unit on 203 (a stranded drop, a refusal, a failed
leg, a backup push failure, a stale-stuck page since T-4462 FU-A) was seen only when
archivist happened to read `systemctl --user --failed` at session start: policy, not
structure. This VPS-side check reads the failed set over ssh and mails archivist, the
owner of those units.

Two legs, one ssh read:

  - the FAILED SET: units still failed at poll time (`list-units --state=failed`);
  - the FAILURE EVENTS (T-5602): every unit failure systemd recorded since the last check,
    read from the structured journal entry it writes when a unit fails (MESSAGE_ID
    d9b373ed55a64feb8242e02dbe79a49c, fields USER_UNIT + UNIT_RESULT). A unit that fails
    and is healed by a later run before the 6h poll leaves the failed set empty, so without
    this leg it reached nobody (the 2026-10-26 14:47Z supernote-ingest refusal).

The domain failure goes to the OWNER; only a broken check reaches the fleet:

    0  no failed unit and no new failure event; OR they were mailed to archivist; OR the
       failed set was already mailed within the dedupe window and no event is new. The unit
       stays green: the mail IS the alert, and an unread mail is the fleet mail-staleness
       monitor's job, not Recovery's.
    1  failed units or new failure events exist and the mail could NOT be delivered (or
       --no-mail report mode); OR the state file could NOT be persisted (a check that
       cannot keep its cursor/dedupe re-mails forever or reopens the 7h gap). The unit
       fails -> OnFailure= -> Recovery.
    3  203 unreachable, the ssh read timed out, or its output did not parse. The unit
       fails -> OnFailure= -> Recovery. FAIL CLOSED: an answer this check cannot read is
       never taken as "nothing failed".

Off the tailnet (T-5388, via tools/tailnet_self.py, T-5385): when the ssh READ fails and THIS
host's own tailscale definitely reads not-Running, the failure says nothing about 203. The
check reports it to the shared "swarm off tailnet" alert (one recovery mail per outage) and exits 0.
An unknown tailscale state never suppresses, and neither does a parse failure: ssh succeeded,
so the tailnet carried it. Every successful read re-arms the next outage's alert.

Proof that systemctl and journalctl ran (T-4598 REBOUND-1, T-5602): rc 0 + empty stdout is
also what an authorized_keys command=, a remote rc that runs `exit 0`, or a fresh user
manager would return. So the ONE ssh invocation prints list-units, then a
`__MEMEX_FUC_JOURNAL` marker line, then the journalctl lines, then an end sentinel carrying
list-units' rc, journalctl's rc, and the `is-active` state of a canary unit that must be
running in a healthy memex user manager (default supernote-ingest.path). The LAST non-empty
line must be exactly `__MEMEX_FUC_END rc=0 jrc=0 canary=active`; a missing sentinel, lines
after it, rc != 0, jrc != 0, a canary that is not active, or a marker that is missing or
repeated is exit 3. So is any journal line that is not a JSON object with a well-formed
USER_UNIT, a UNIT_RESULT word, and a digits-only __REALTIME_TIMESTAMP.

Event cursor: the state key `events_seen_until` (int microseconds) is the newest event
timestamp already mailed. The remote `--since` is that cursor's second (journalctl's
--since is inclusive, so events are re-filtered locally: only ts > cursor is new). With no
cursor (no state, an old state file, a corrupt key, or one in the future) --since is
now - 7h, just over the 6h cadence, so a first install does not mail ancient history; that
queried floor then becomes the cursor (never moving it backwards past a newer event) once
the run needs no mail or its mail succeeded, so a later outage longer than 7h resumes from
it instead of skipping what it missed. The cursor advances only after a successful send
or on a run with nothing to send; a failed send writes nothing, so the next run re-reads
the same events, and --no-mail never writes. Each event is mailed once.

Dedupe (state file, default ~/.local/state/pantheon/memex-failed-unit-check.json):
mail when the failed set differs from the last-mailed set, or >= 20h have passed since
the last mail (a daily reminder), or any failure event is new; the failed set and the new
events go in ONE mail. An empty failed set clears the failed-set dedupe, so a recurrence
mails again, but keeps the event cursor. State is written only after a successful send,
atomically. A corrupt or unreadable state file (or key) is treated as "never mailed" (it
can only cause an extra mail, never a suppressed one). A state that cannot be WRITTEN is
exit 1, even after a delivered mail.

Content-blind: only the FIRST token of each `list-units` line is used, and only when it
is a well-formed unit name; of each journal line only USER_UNIT, UNIT_RESULT and the
timestamp, each charset-checked. The mail and the log carry unit names, result words,
times and fixed text, never journal messages, descriptions, raw remote lines, or remote
stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PREFIX = "memex-failed-unit-check"
DEFAULT_HOST = "memex203"
HOST_ENV = "MEMEX_CHECK_HOST"
DEFAULT_CANARY = "supernote-ingest.path"
CANARY_ENV = "MEMEX_CHECK_CANARY"
DEFAULT_STATE = Path.home() / ".local" / "state" / "pantheon" / "memex-failed-unit-check.json"
SSH_TIMEOUT_SECONDS = 90
REMIND_AFTER = timedelta(hours=20)
# No event cursor: look back just over the 6h timer cadence, never into ancient history.
FIRST_LOOKBACK = timedelta(hours=7)
# A cursor further ahead than this is not trusted: it would suppress every event forever.
CURSOR_FUTURE_SLACK = timedelta(days=1)
SUBJECT_NAMES_MAX = 200
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

SENTINEL = "__MEMEX_FUC_END"
JOURNAL_MARKER = "__MEMEX_FUC_JOURNAL"
END_OK = f"{SENTINEL} rc=0 jrc=0 canary=active"
# systemd's "unit failed" catalog entry (sd-messages.h SD_MESSAGE_UNIT_FAILED).
UNIT_FAILED_MESSAGE_ID = "d9b373ed55a64feb8242e02dbe79a49c"
# Charset-bounded captures: whatever 203 sends, only digits / [a-z-] reach the log.
_END = re.compile(rf"^{SENTINEL} rc=(\d{{1,3}}) jrc=(\d{{1,3}}) canary=([a-z-]{{0,20}})$")
_UNIT = re.compile(
    r"^[A-Za-z0-9:_.@\\-]+\.(service|timer|path|socket|mount|target|scope|slice"
    r"|automount|swap|device)$"
)
_RESULT = re.compile(r"^[a-z-]{1,32}$")
_REALTIME = re.compile(r"^\d{1,18}$")
# An ssh destination, never an option: a value starting with '-' would be read by ssh
# as a flag (e.g. -oProxyCommand=...), so the host is validated before it reaches argv.
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")

# (timestamp in microseconds since the epoch, unit name, result word)
Event = tuple[int, str, str]


class CheckUnreachable(Exception):
    """The failed set could not be read: ssh failed, timed out, or the output did not parse."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_plain_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _epoch_us(dt: datetime) -> int:
    return (dt - _EPOCH) // timedelta(microseconds=1)


def _event_time(ts_us: int) -> str:
    return (_EPOCH + timedelta(microseconds=ts_us)).strftime(_TS_FORMAT)


def remote_command(canary: str, since: int) -> str:
    """The single remote shell command: list-units, the journal marker, the unit-failed
    journal entries since `since` (epoch seconds), then the end sentinel carrying both rcs
    and the canary's is-active state. `canary` must already have passed the _UNIT check;
    `since` must be a non-negative int (it is formatted here, never passed through)."""
    if not _is_plain_int(since):
        raise TypeError("since must be an int")
    if since < 0:
        raise ValueError("since must be non-negative")
    return (
        "systemctl --user list-units --state=failed --no-legend --plain; r=$?; "
        f"printf '{JOURNAL_MARKER}\\n'; "
        "journalctl --user -o json --output-fields=USER_UNIT,UNIT_RESULT --no-pager "
        f"--since @{since:d} MESSAGE_ID={UNIT_FAILED_MESSAGE_ID}; j=$?; "
        f"printf '{SENTINEL} rc=%s jrc=%s canary=%s\\n' \"$r\" \"$j\" "
        f"\"$(systemctl --user is-active {shlex.quote(canary)})\""
    )


def fetch(host: str, canary: str = DEFAULT_CANARY, since: int = 0, runner=subprocess.run) -> str:
    """Return the raw remote stdout (list-units + marker + journal + end sentinel), or
    raise CheckUnreachable."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
            remote_command(canary, since)]
    try:
        r = runner(argv, capture_output=True, text=True, timeout=SSH_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise CheckUnreachable(f"ssh timed out after {SSH_TIMEOUT_SECONDS}s") from None
    except Exception as e:  # ssh missing, runner fault: still "could not read", never "clean"
        raise CheckUnreachable(f"ssh could not run ({e.__class__.__name__})") from None
    if r.returncode != 0:
        # Remote stderr is NOT echoed: it is uncontrolled text from 203 (content-blind).
        raise CheckUnreachable(f"ssh rc={r.returncode}")
    return r.stdout or ""


def _check_end(lines: list[str]) -> list[str]:
    """Verify the end sentinel (the proof systemctl and journalctl ran) and return the lines
    before it.

    Raises CheckUnreachable naming WHICH condition failed; no remote text is echoed beyond
    the charset-bounded rc / canary-state captures."""
    if not any(line.startswith(SENTINEL) for line in lines):
        raise CheckUnreachable("missing end sentinel (no proof systemctl ran)")
    last = lines[-1]
    if not last.startswith(SENTINEL):
        after = len(lines) - 1 - max(i for i, l in enumerate(lines) if l.startswith(SENTINEL))
        raise CheckUnreachable(f"{after} line(s) after the end sentinel")
    m = _END.fullmatch(last)
    if not m:
        raise CheckUnreachable("malformed end sentinel")
    problems = []
    if m.group(1) != "0":
        problems.append(f"list-units rc={m.group(1)}")
    if m.group(2) != "0":
        problems.append(f"journalctl rc={m.group(2)}")
    if m.group(3) != "active":
        problems.append(f"canary not active (state={m.group(3) or 'empty'})")
    if problems or last != END_OK:
        raise CheckUnreachable("; ".join(problems) or "malformed end sentinel")
    return lines[:-1]


def _parse_units(lines: list[str]) -> list[str]:
    names: set[str] = set()
    bad = 0
    for line in lines:
        if line[0] in "●*":  # the failed-state marker some systemd versions print
            line = line[1:].lstrip()
        token = line.split(None, 1)[0] if line else ""
        if _UNIT.fullmatch(token):
            names.add(token)
        else:
            bad += 1
    if bad:
        # The offending lines are not echoed (content-blind); the count is enough to act on.
        raise CheckUnreachable(f"unparseable: {bad} line(s) are not a unit name")
    return sorted(names)


def _parse_event(line: str) -> Event | None:
    """One journal line -> (ts_us, unit, result), or None when it is not a well-formed
    unit-failed entry. Only the three checked fields are carried; nothing else is kept."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    unit, result, ts = obj.get("USER_UNIT"), obj.get("UNIT_RESULT"), obj.get("__REALTIME_TIMESTAMP")
    if not (isinstance(unit, str) and _UNIT.fullmatch(unit)):
        return None
    if not (isinstance(result, str) and _RESULT.fullmatch(result)):
        return None
    if not (isinstance(ts, str) and _REALTIME.fullmatch(ts)):
        return None
    ts_us = int(ts)
    try:
        _event_time(ts_us)
    except OverflowError:  # beyond datetime's range: not a time this check can report
        return None
    return ts_us, unit, result


def _parse_events(lines: list[str]) -> list[Event]:
    events: set[Event] = set()
    bad = 0
    for line in lines:
        ev = _parse_event(line)
        if ev is None:
            bad += 1
        else:
            events.add(ev)
    if bad:
        raise CheckUnreachable(f"unparseable: {bad} journal line(s) are not a unit-failed event")
    return sorted(events)


def parse_all(text: str) -> tuple[list[str], list[Event]]:
    """(failed unit names, failure events) from the remote output, each sorted and
    de-duplicated; CheckUnreachable on a missing or bad end sentinel, a missing or repeated
    journal marker, or any line that is not a unit name / unit-failed event."""
    lines = _check_end([raw.strip() for raw in text.splitlines() if raw.strip()])
    marks = [i for i, line in enumerate(lines) if line == JOURNAL_MARKER]
    if not marks:
        raise CheckUnreachable("missing journal marker (no proof journalctl ran)")
    if len(marks) > 1:
        raise CheckUnreachable(f"repeated journal marker ({len(marks)} markers)")
    i = marks[0]
    return _parse_units(lines[:i]), _parse_events(lines[i + 1:])


def parse(text: str) -> list[str]:
    """The failed unit names only (see parse_all)."""
    return parse_all(text)[0]


def load_state(path: Path, now: datetime) -> tuple[dict | None, int | None, list[str]]:
    """Return (dedupe, events_seen_until, problems).

    `dedupe` is {"failed", "mailed_at"} or None (missing, cleared, or corrupt);
    `events_seen_until` is the int cursor or None (missing, corrupt, or in the future).
    A missing file is (None, None, []); each bad part adds one problem string."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None, []
    except OSError as e:
        return None, None, [f"unreadable ({e.__class__.__name__}); treating as never mailed"]
    try:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise TypeError("state is not an object")
    except (ValueError, TypeError) as e:
        return None, None, [f"corrupt ({e.__class__.__name__}); treating as never mailed"]

    problems: list[str] = []
    dedupe = None
    # A cursor-only file (the failed-set dedupe cleared) carries neither key: not corrupt.
    if "failed" in obj or "mailed_at" in obj:
        try:
            failed = obj["failed"]
            if not isinstance(failed, list) or not all(isinstance(n, str) for n in failed):
                raise TypeError("failed is not a list of names")
            mailed_at = datetime.strptime(obj["mailed_at"], _TS_FORMAT).replace(
                tzinfo=timezone.utc)
            dedupe = {"failed": sorted(failed), "mailed_at": mailed_at}
        except (ValueError, KeyError, TypeError) as e:
            problems.append(f"corrupt ({e.__class__.__name__}); treating as never mailed")

    cursor = None
    if "events_seen_until" in obj:
        v = obj["events_seen_until"]
        if not (_is_plain_int(v) and v >= 0):
            problems.append("events_seen_until corrupt; treating as missing")
        elif v > _epoch_us(now + CURSOR_FUTURE_SLACK):
            problems.append("events_seen_until is in the future; treating as missing")
        else:
            cursor = v
    return dedupe, cursor, problems


def should_mail(failed: list[str], state: dict | None, now: datetime) -> bool:
    if not failed:
        return False
    if state is None or state["failed"] != failed:
        return True
    age = now - state["mailed_at"]
    # A mailed_at in the future is not trusted: it would suppress the same set forever.
    return age < timedelta(0) or age >= REMIND_AFTER


def write_state(path: Path, failed: list[str], now: datetime,
                events_seen_until: int | None = None) -> None:
    """Atomic: write a temp file beside the target, fsync, rename over it. An empty
    `failed` writes no failed-set dedupe (cleared); a None cursor writes no cursor.
    `now` is stamped as the failed set's mailed_at."""
    path.parent.mkdir(parents=True, exist_ok=True)
    obj: dict = {}
    if failed:
        obj["failed"] = failed
        obj["mailed_at"] = now.strftime(_TS_FORMAT)
    if events_seen_until is not None:
        obj["events_seen_until"] = events_seen_until
    body = json.dumps(obj) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_unmailed_state(path: Path, failed: list[str], mailed_at: datetime | None,
                          events_seen_until: int) -> bool:
    """A state update on a run that sent nothing: the failed-set dedupe is carried as-is
    (its mailed_at unchanged), or cleared when `failed` is empty; the cursor (always known
    here: the stored one or the queried floor) is kept. Returns False (and logs) when the
    state could not be persisted: the caller exits 1, because a check that cannot keep its
    cursor re-mails forever or reopens the 7h gap."""
    try:
        write_state(path, failed, mailed_at or _utcnow(), events_seen_until)
    except OSError as e:
        print(f"{PREFIX}: state write to {path} FAILED ({e!r}); state not persisted")
        return False
    return True


def _truncate(joined: str) -> str:
    if len(joined) <= SUBJECT_NAMES_MAX:
        return joined
    return joined[: SUBJECT_NAMES_MAX - 3] + "..."


def _names_text(failed: list[str]) -> str:
    return _truncate(", ".join(failed))


def _events_text(events: list[Event]) -> str:
    return _truncate(", ".join(f"{unit} ({result})" for _, unit, result in events))


def _summary(failed: list[str], events: list[Event]) -> str:
    parts = []
    if failed:
        parts.append(f"{len(failed)} failed unit(s): {_names_text(failed)}")
    if events:
        parts.append(f"{len(events)} failure event(s) since last check: {_events_text(events)}")
    return "; ".join(parts)


def build_mail(failed: list[str], host: str, events: list[Event] = ()) -> tuple[str, str]:
    """(subject, payload): unit names, result words, event times and fixed text only,
    deterministic (no current timestamp), so the mailbox's dedupe_pending collapses an
    identical repeat still sitting unread."""
    events = sorted(events)
    subject = f"memex 203: {_summary(failed, events)}"
    lines: list[str] = []
    if failed:
        lines += [
            f"memex failed-unit check (T-4598): {len(failed)} --user unit(s) on LXC 203 are failed:",
            "",
            *[f"- {name}" for name in failed],
            "",
        ]
    if events:
        lines += [
            f"memex failed-unit check (T-5602): systemd recorded {len(events)} --user unit "
            "failure event(s) on LXC 203 since the last check:",
            "",
            *[f"- {_event_time(ts)} {unit} ({result})" for ts, unit, result in events],
            "",
            "this unit may already be healthy again: a later run can clear the failed state",
            "",
        ]
    inspect = list(failed) + sorted({unit for _, unit, _ in events} - set(failed))
    lines += [
        "Inspect each with:",
        *[f"  ssh {host} 'journalctl --user -u {name} -n 50'" for name in inspect],
        "",
        "Standing rule: broken service => ticket (file first, diagnose after).",
        "Re-mailed when the failed set changes, or daily while it persists; "
        "an empty set clears the dedupe.",
    ]
    if events:
        lines.append("Each failure event is mailed once.")
    return subject, "\n".join(lines)


def mail_archivist(subject: str, payload: str) -> None:
    from tools import agent_role_map, mailbox

    mailbox.send(
        str(Path(agent_role_map.mailbox_hub()) / "archivist"), payload, "archivist",
        subject=subject,
    )


def _default_tailnet():
    from tools import tailnet_self

    return tailnet_self


def main(argv: list[str] | None = None, *, runner=subprocess.run, clock=_utcnow,
         send=mail_archivist) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=None,
                    help=f"ssh host (default ${HOST_ENV}, else {DEFAULT_HOST})")
    ap.add_argument("--canary", default=None,
                    help=f"unit that must be active on the host (default ${CANARY_ENV}, "
                         f"else {DEFAULT_CANARY})")
    ap.add_argument("--state", type=Path, default=None,
                    help=f"dedupe state file (default {DEFAULT_STATE})")
    ap.add_argument("--no-mail", action="store_true",
                    help="report only: never mail, never touch the state; exit 1 if any unit "
                         "failed or any failure event is new")
    a = ap.parse_args(argv)
    host = a.host or os.environ.get(HOST_ENV) or DEFAULT_HOST
    canary = a.canary or os.environ.get(CANARY_ENV) or DEFAULT_CANARY
    state_path = a.state or DEFAULT_STATE

    if not _HOST.fullmatch(host):
        print(f"{PREFIX}: UNREACHABLE: invalid ssh host {host!r}")
        return 3
    if not _UNIT.fullmatch(canary):
        print(f"{PREFIX}: UNREACHABLE: invalid canary unit name {canary!r}")
        return 3

    now = clock()
    dedupe, cursor, problems = load_state(state_path, now)
    if cursor is None:
        since = int((now - FIRST_LOOKBACK).timestamp())
    else:
        since = cursor // 1_000_000

    tailnet = _default_tailnet()
    try:
        raw = fetch(host, canary, since, runner)
    except CheckUnreachable as e:
        off = tailnet.off_tailnet()
        if off:
            tailnet.report_off_tailnet(PREFIX, off, detail=f"host={host}: {e}")
            print(f"{PREFIX}: SUPPRESSED host={host}: this host is off the tailnet ({off}): {e}")
            return 0
        print(f"{PREFIX}: UNREACHABLE host={host}: {e}")
        return 3
    try:
        failed, events = parse_all(raw)
    except CheckUnreachable as e:
        print(f"{PREFIX}: UNREACHABLE host={host}: {e}")
        return 3
    tailnet.note_reachable()

    if problems:
        print(f"{PREFIX}: state file {state_path} " + "; ".join(problems))
    # --since is inclusive (and second-granular): only what is past the cursor is new.
    new = [ev for ev in events if cursor is None or ev[0] > cursor]
    # With no cursor, the floor this run actually queried becomes the cursor once the run
    # needs no mail (or its mail succeeded): a later outage longer than the lookback then
    # resumes from here instead of silently skipping the events it missed.
    floor = since * 1_000_000 if cursor is None else None

    if not failed and not new:
        if not a.no_mail:
            if cursor is not None and dedupe is None and not problems:
                pass  # a clean cursor-only file: nothing to change
            else:
                # Clear only the failed-set dedupe; the event cursor survives (or the floor
                # becomes it), or the next run would fall back to now-7h and re-mail an
                # event already mailed.
                if not _write_unmailed_state(state_path, [], None,
                                             cursor if floor is None else floor):
                    return 1
        print(f"{PREFIX}: ok host={host}: 0 failed units, 0 new failure events")
        return 0

    summary = _summary(failed, new)
    if a.no_mail:
        print(f"{PREFIX}: FAILED host={host}: {summary}; not mailed (--no-mail)")
        return 1

    if not new and not should_mail(failed, dedupe, now):
        if floor is not None:  # keep the dedupe (and its mailed_at) exactly; add the floor
            if not _write_unmailed_state(state_path, dedupe["failed"], dedupe["mailed_at"],
                                         floor):
                return 1
        print(f"{PREFIX}: FAILED host={host}: {summary}; "
              f"already mailed at {dedupe['mailed_at'].strftime(_TS_FORMAT)}, not re-mailed")
        return 0

    subject, payload = build_mail(failed, host, new)
    try:
        send(subject, payload)
    except Exception as e:  # undelivered: the unit fails and OnFailure= carries it to Recovery
        print(f"{PREFIX}: FAILED host={host}: {summary}; mail to archivist FAILED: {e!r}")
        return 1
    # Never backwards: the max of the new events and the cursor (or the queried floor).
    seen = max([ev[0] for ev in new] + [c for c in (cursor, floor) if c is not None])
    try:
        write_state(state_path, failed, now, seen)
    except OSError as e:
        # The mail was delivered, but a check that cannot persist its cursor/dedupe is
        # broken (it re-mails every run, or reopens the 7h gap): exit 1 -> OnFailure= -> Recovery.
        print(f"{PREFIX}: FAILED host={host}: {summary}; mailed archivist, but state write "
              f"to {state_path} FAILED ({e!r}); state not persisted")
        return 1
    print(f"{PREFIX}: FAILED host={host}: {summary}; mailed archivist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
