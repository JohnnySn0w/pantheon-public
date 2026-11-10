#!/usr/bin/env python3
"""mailbox_integrity_audit — flag mailbox files that did not come through mailbox.send (T-4958).

WHY. mailbox.py validates the sender, builds a digested envelope, writes tmp/ then
renames into new/, and logs to the sender's ledger. But the hub's ACLs deliberately
let several fleet accounts write into peer inboxes (T-4902), so a direct filesystem
write bypasses all of it. Nothing looked at the result. This does, READ-ONLY.

WHAT IS CHECKED, per message file in <hub>/<inbox>/new (all) and cur (last --days):
  name          filename is `<ts_ns 20d>.<pid>.<seq 6d>.<sender>[:2,<flags>]`
  envelope      one JSON object; parses
  id            envelope id == filename (without the maildir flag suffix)
  sender        envelope sender matches mailbox._SENDER_RE and the filename's sender
  severity      in mailbox._VALID_SEVERITIES
  ts            envelope ts_ns == the filename's ts_ns
  digest        present, and equals mailbox._payload_digest() recomputed from the
                envelope (a record with NO digest is reported as legacy, not an anomaly)
  owner         when the sender resolves to a fleet persona or account, the file's
                owner is that persona's account. Tool senders (disk-warn, worker
                identities, ...) cannot be attributed and are counted, not flagged.
  mode          no world bits (mode & 0o007 == 0); mailbox.send writes 0660

It never moves, edits or deletes a message. Exit 0 clean, 1 anomalies found,
2 the hub could not be read (never reported as "clean").

Run with the mailbox group (hub reads need it):
    sg pantheon-mailbox -c 'python3 tools/mailbox_integrity_audit.py [--days 7] [--json]'
The hub root is traverse-only (0710). A seat that can sudo as the hub owner (recovery) lists
it that way. Any other mailbox-group seat (watchman) audits the registry-known inboxes,
which is accepted only when the hub's subdirectory count proves they are all of them;
otherwise it exits 2 (T-6142).
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
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import mailbox  # noqa: E402

NAME_RE = re.compile(
    r"^(?P<base>(?P<ts>\d{20})\.(?P<pid>\d+)\.(?P<seq>\d{6})\.(?P<sender>[^/:]+))"
    r"(?::2,[A-Za-z]*)?$")
DEFAULT_DAYS = 7.0


@dataclass
class Report:
    scanned: int = 0
    anomalies: list[tuple[str, str, str]] = field(default_factory=list)  # (path, class, detail)
    legacy_no_digest: int = 0
    unattributed: dict[str, int] = field(default_factory=dict)          # "sender@owner" -> n


def _owner(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def expected_account(sender: str, persona_accounts: dict[str, str],
                     accounts: set[str]) -> str | None:
    """The account a sender's mail must be owned by, or None if unattributable."""
    if sender in persona_accounts:
        return persona_accounts[sender]
    if sender in accounts:
        return sender
    return None


def audit_file(path: Path, report: Report, *, persona_accounts: dict[str, str],
               accounts: set[str]) -> None:
    report.scanned += 1

    def flag(cls: str, detail: str = "") -> None:
        report.anomalies.append((str(path), cls, detail))

    m = NAME_RE.match(path.name)
    if not m:
        flag("bad-name")
        return
    try:
        st = os.lstat(path)
    except OSError as exc:
        flag("unreadable", type(exc).__name__)
        return
    if not stat.S_ISREG(st.st_mode):
        flag("not-regular-file")
        return
    if st.st_mode & 0o007:
        flag("world-bits", oct(stat.S_IMODE(st.st_mode)))
    owner = _owner(st.st_uid)
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        flag("bad-envelope", type(exc).__name__)
        return
    if not isinstance(rec, dict):
        flag("bad-envelope", "not an object")
        return
    sender = rec.get("sender")
    if not isinstance(sender, str) or not mailbox._SENDER_RE.fullmatch(sender):
        flag("bad-sender", "invalid")
        return
    if sender != m["sender"]:
        flag("sender-mismatch", f"envelope {sender} vs name {m['sender']}")
    if rec.get("id") != m["base"]:
        flag("id-mismatch")
    if rec.get("severity", "normal") not in mailbox._VALID_SEVERITIES:
        flag("bad-severity", str(rec.get("severity"))[:20])
    if rec.get("ts_ns") != int(m["ts"]):
        flag("ts-mismatch")
    digest = rec.get("digest")
    if digest is None:
        report.legacy_no_digest += 1
    else:
        want = mailbox._payload_digest(sender, rec.get("subject"), rec.get("payload"),
                                       rec.get("severity", "normal"), rec.get("sender_session"))
        if digest != want:
            flag("digest-mismatch")
    acct = expected_account(sender, persona_accounts, accounts)
    if acct is None:
        key = f"{sender}@{owner}"
        report.unattributed[key] = report.unattributed.get(key, 0) + 1
    elif owner != acct:
        flag("owner-mismatch", f"sender {sender} expects {acct}, file owned by {owner}")


# Inboxes that are not a persona (T-6142; mail_staleness_monitor's CANDIDATE_INBOXES
# lesson: a list of names cannot contain the box nobody named, so st_nlink checks it).
NON_PERSONA_INBOXES = ("specialist-a2a",)


def candidate_inboxes() -> list[str]:
    """Every inbox name we know without listing the hub: each registry persona plus the
    non-persona boxes."""
    from tools import agent_role_map as arm
    return sorted(set(arm.account_persona_map().values()) | set(NON_PERSONA_INBOXES))


def list_inboxes(hub: Path, *, run=subprocess.run, candidates=None) -> list[str]:
    """Inbox names under the hub. The hub root is traverse-only for the mailbox group
    (mode 710), so when a direct listing is refused, list it AS the hub owner
    (seat_exec.seat_argv, the mailbox_traffic_report pattern). A recovery seat that may
    not sudo as the owner (watchman, T-6142) falls back to probing the known inbox names
    by traverse, and that result is used ONLY when the hub's st_nlink proves it is every
    subdirectory. Raises OSError otherwise: an unlistable or partly-seen hub is never
    reported as a clean one."""
    try:
        names = os.listdir(hub)
    except PermissionError:
        from tools.mailbox_traffic_report import hub_owner
        from tools.seat_exec import seat_argv
        try:
            r = run(seat_argv(hub_owner(str(hub)), ["ls", "-1A", str(hub)]),
                    capture_output=True, text=True, timeout=60)
            owner_rc = r.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            r, owner_rc = None, f"error ({exc})"
        if r is not None and owner_rc == 0:
            names = r.stdout.split()
        else:
            names = _probe_known_inboxes(hub, candidate_inboxes() if candidates is None
                                         else candidates, owner_rc)
    return sorted(n for n in names if not n.startswith("."))


def _is_real_dir(p: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(p).st_mode)
    except FileNotFoundError:
        return False


def _probe_known_inboxes(hub: Path, candidates, owner_rc) -> list[str]:
    # lstat, not is_dir(): a symlinked name adds nothing to st_nlink, so it could stand in
    # for an unnamed real inbox and make a partial view pass the completeness check
    found = [n for n in candidates if _is_real_dir(hub / n)]
    nlink = os.stat(hub).st_nlink
    if nlink < 2:  # a filesystem that does not count subdirs (btrfs): unverifiable
        raise PermissionError(f"listing {hub} as its owner exited {owner_rc}, and its "
                              f"subdirectory count is unverifiable (st_nlink {nlink})")
    expected = nlink - 2
    if len(found) < expected:
        raise PermissionError(
            f"listing {hub} as its owner exited {owner_rc}; probed {len(found)} known "
            f"inbox(es) but the hub has {expected} subdirectories, {expected - len(found)} "
            "NOT auditable from this seat (run as recovery or the hub owner)")
    return found


def iter_messages(hub: Path, *, days: float, now: float, run=subprocess.run):
    """(path) for every message in new/, and in cur/ within `days`. Raises OSError if
    the hub itself cannot be listed."""
    cutoff_ns = int((now - days * 86400) * 1e9)
    for name in list_inboxes(hub, run=run):
        inbox = hub / name
        if not inbox.is_dir():
            continue
        for sub in ("new", "cur"):
            d = inbox / sub
            try:
                names = sorted(os.listdir(d))
            except FileNotFoundError:
                continue
            for name in names:
                if name.startswith("."):
                    continue
                if sub == "cur":
                    m = NAME_RE.match(name)
                    if m and int(m["ts"]) < cutoff_ns:
                        continue
                yield d / name


def audit(hub: Path, *, days: float = DEFAULT_DAYS, now: float | None = None,
          persona_accounts: dict[str, str] | None = None,
          accounts: set[str] | None = None) -> Report:
    if persona_accounts is None or accounts is None:
        from tools import agent_role_map as arm
        ap = arm.account_persona_map()
        persona_accounts = {p: a for a, p in ap.items()} if persona_accounts is None else persona_accounts
        accounts = set(arm.fleet_accounts()) if accounts is None else accounts
    report = Report()
    for path in iter_messages(hub, days=days, now=time.time() if now is None else now):
        audit_file(path, report, persona_accounts=persona_accounts, accounts=accounts)
    return report


def render(report: Report, hub: Path, days: float) -> str:
    lines = [f"mailbox integrity audit: {hub} (new/ all, cur/ last {days:g}d): "
             f"{report.scanned} message(s), {len(report.anomalies)} anomaly(ies), "
             f"{report.legacy_no_digest} legacy without digest"]
    for p, cls, detail in report.anomalies:
        lines.append(f"  ANOMALY {cls} {p}" + (f" ({detail})" if detail else ""))
    if report.unattributed:
        lines.append("  unattributed tool senders (owner not checked): " + ", ".join(
            f"{k} x{v}" for k, v in sorted(report.unattributed.items())))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hub", type=Path, default=None, help="mailbox hub (default: instance.yaml's)")
    ap.add_argument("--days", type=float, default=DEFAULT_DAYS, help="cur/ look-back (default 7)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.hub is None:
        from tools import agent_role_map as arm
        a.hub = Path(arm.mailbox_hub())
    try:
        report = audit(a.hub, days=a.days)
    except OSError as exc:
        print(f"mailbox_integrity_audit: cannot read hub {a.hub}: {exc} "
              "(run under `sg pantheon-mailbox`)", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps({"hub": str(a.hub), "days": a.days, "scanned": report.scanned,
                          "anomalies": [{"path": p, "class": c, "detail": d}
                                        for p, c, d in report.anomalies],
                          "legacy_no_digest": report.legacy_no_digest,
                          "unattributed": report.unattributed}, indent=1))
    else:
        print(render(report, a.hub, a.days))
    return 1 if report.anomalies else 0


if __name__ == "__main__":
    raise SystemExit(main())
