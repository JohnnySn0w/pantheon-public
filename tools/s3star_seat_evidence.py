#!/usr/bin/env python3
"""s3star_seat_evidence.py — Recovery-captured per-seat evidence for the S3* auditor (T-4928).

WHY. The S3* audit session runs as `codex`. It used to read each seat (other
accounts' `systemctl --user` state, transcript files) through codex's
passwordless sudo. The operator removed that grant (TG 11733, T-4884 B12), and an
outside reviewer should not hold standing cross-account read anyway. The
runner's own evidence bundle (s3star_audit_runner.py) is captured as `claude`
without sudo, so it can only see claude's own user manager and says so in its
Limits section.

WHAT. Recovery already holds sudo into every fleet account. This captures, AS EACH
account in instance.yaml (seat_exec.seat_argv; sudo -n, never prompting):
  units-<acct>.txt        systemctl --user list-units --all --no-pager --plain
  timers-<acct>.txt       systemctl --user list-timers --all --no-pager
  failed-<acct>.txt       systemctl --user list-units --failed --no-pager --plain
  transcripts-<acct>.txt  INVENTORY ONLY of session transcripts modified in the
                          last --days: path, bytes, mtime. Never their content:
                          transcripts carry operator conversation and secrets
                          pasted into sessions, and this bundle sits in a
                          group-readable workspace.
into <workspace>/seat-evidence/<UTC date>/ (files 0444, dir setgid 2770) with a
MANIFEST.md (command, status, sha256 per artifact) and moves the `latest`
symlink to it. A capture that fails is recorded as FAILED with its exit code,
never dropped: absence in the bundle must read as absence-of-channel.

Exit 0 when every capture succeeded, 1 when any failed (bundle still written),
2 when the bundle could not be written at all.

Run as Recovery with the mailbox group (the workspace is group-owned):
    sg pantheon-mailbox -c 'python3 tools/s3star_seat_evidence.py'
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pwd
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.seat_exec import seat_argv  # noqa: E402

WORKSPACE_ROOT = Path("/var/lib/pantheon/s3star-workspace")
SUBDIR = "seat-evidence"
DEFAULT_DAYS = 8
TIMEOUT_S = 60
TRANSCRIPT_ROOTS = ("~/.claude/projects", "~/.codex/sessions")


@dataclass
class Capture:
    name: str
    account: str
    command: str
    status: str
    body: str


def _user_env(account: str, argv: list[str]) -> list[str]:
    """systemctl --user needs the target's XDG_RUNTIME_DIR, resolved as that user."""
    uid = pwd.getpwnam(account).pw_uid
    return ["env", f"XDG_RUNTIME_DIR=/run/user/{uid}", *argv]


def capture_commands(account: str, days: int) -> list[tuple[str, str, list[str]]]:
    """(artifact name, human-readable command, argv to run AS `account`)."""
    roots = " ".join(TRANSCRIPT_ROOTS)
    find = (f"for r in {roots}; do [ -d \"$r\" ] && find \"$r\" -type f -name '*.jsonl' "
            f"-mtime -{int(days)} -printf '%p\\t%s\\t%TY-%Tm-%TdT%TH:%TMZ\\n'; done; true")
    return [
        (f"units-{account}", "systemctl --user list-units --all --no-pager --plain",
         _user_env(account, ["systemctl", "--user", "list-units", "--all", "--no-pager", "--plain"])),
        (f"timers-{account}", "systemctl --user list-timers --all --no-pager",
         _user_env(account, ["systemctl", "--user", "list-timers", "--all", "--no-pager"])),
        (f"failed-{account}", "systemctl --user list-units --failed --no-pager --plain",
         _user_env(account, ["systemctl", "--user", "list-units", "--failed", "--no-pager", "--plain"])),
        (f"transcripts-{account}",
         f"inventory (path, bytes, mtime; NO content) of *.jsonl under {roots} modified in the last {days}d",
         ["env", "TZ=UTC", "bash", "-c", find]),
    ]


def run_capture(account: str, name: str, command: str, argv: list[str], *,
                run: Callable = subprocess.run) -> Capture:
    try:
        r = run(seat_argv(account, argv), capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return Capture(name, account, command, f"FAILED ({type(exc).__name__})", "")
    if r.returncode != 0:
        first = (r.stderr or "").strip().splitlines()[:1]
        return Capture(name, account, command, f"FAILED (exit {r.returncode})",
                       first[0][:200] if first else "")
    return Capture(name, account, command, "OK", r.stdout)


def collect(accounts: list[str], *, days: int = DEFAULT_DAYS,
            run: Callable = subprocess.run) -> list[Capture]:
    out = []
    for acct in accounts:
        try:
            cmds = capture_commands(acct, days)
        except KeyError:
            out.append(Capture(f"account-{acct}", acct, "getpwnam", "FAILED (no such account)", ""))
            continue
        for name, command, argv in cmds:
            out.append(run_capture(acct, name, command, argv, run=run))
    return out


def manifest(captures: list[Capture], *, generated_at: str, days: int, captured_by: str) -> str:
    rows = []
    for c in captures:
        digest = hashlib.sha256(c.body.encode("utf-8")).hexdigest()
        rows.append(f"| `{c.name}.txt` | {c.account} | {c.command} | {c.status} | `{digest}` |")
    failed = [c for c in captures if c.status != "OK"]
    return (
        f"---\ngenerated_at: {generated_at}\ncaptured_by: {captured_by}\n"
        f"kind: s3star-seat-evidence-manifest\ntranscript_window_days: {days}\n---\n\n"
        "# S3* per-seat evidence (Recovery-captured, T-4928)\n\n"
        "CAVEAT: captured by Recovery (the head system orchestrator) as each account, outside any "
        "audit sandbox. Like the runner's bundle it is narrated evidence, not the auditor's own "
        "observation; cite it as Recovery-captured. Recovery is itself an audited seat, so treat "
        "Recovery's own rows with the same independence caution.\n\n"
        "| file | account | command | status | sha256 |\n|---|---|---|---|---|\n"
        + "\n".join(rows) + "\n\n## Limits\n\n"
        "- Transcript CONTENT is deliberately not captured, only an inventory. If an audit "
        "question needs a transcript excerpt, file it as a request to Recovery with the path and "
        "line range; Recovery returns a reviewed excerpt.\n"
        "- Point-in-time: every file reflects `generated_at`.\n"
        f"- Failed captures: {len(failed)}"
        + (" (" + ", ".join(f"{c.name}: {c.status}" for c in failed) + ")" if failed else "")
        + ". A FAILED row is absence-of-channel, not absence-of-thing.\n"
    )


def write_bundle(captures: list[Capture], root: Path, *, now: datetime, days: int,
                 captured_by: str) -> Path:
    """Write <root>/seat-evidence/<date>/ atomically (temp dir, then rename) and repoint
    `latest`. Returns the bundle dir. A same-day re-run replaces that day's bundle."""
    base = root / SUBDIR
    base.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d")
    tmp = Path(tempfile.mkdtemp(prefix=f".{stamp}.", dir=base))
    for c in captures:
        p = tmp / f"{c.name}.txt"
        p.write_text(c.body if c.status == "OK" else f"{c.status}\n{c.body}\n", encoding="utf-8")
        os.chmod(p, 0o444)
    m = tmp / "MANIFEST.md"
    m.write_text(manifest(captures, generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          days=days, captured_by=captured_by), encoding="utf-8")
    os.chmod(m, 0o444)
    os.chmod(tmp, 0o2770)
    final = base / stamp
    if final.exists():
        old = base / f".{stamp}.replaced.{os.getpid()}"
        os.rename(final, old)
        os.rename(tmp, final)
        for p in old.iterdir():
            os.chmod(p, 0o644)
            p.unlink()
        old.rmdir()
    else:
        os.rename(tmp, final)
    link_tmp = base / f".latest.{os.getpid()}"
    os.symlink(stamp, link_tmp)
    os.replace(link_tmp, base / "latest")
    return final


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", type=Path, default=WORKSPACE_ROOT)
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--dry-run", action="store_true", help="capture and print the manifest; write nothing")
    a = ap.parse_args(argv)
    from tools import agent_role_map as arm
    accounts = list(arm.fleet_accounts())
    captures = collect(accounts, days=a.days)
    now = datetime.now(timezone.utc)
    me = pwd.getpwuid(os.getuid()).pw_name
    if a.dry_run:
        print(manifest(captures, generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"), days=a.days,
                       captured_by=me))
    else:
        try:
            out = write_bundle(captures, a.workspace, now=now, days=a.days, captured_by=me)
        except OSError as exc:
            print(f"s3star_seat_evidence: cannot write bundle under {a.workspace}: {exc} "
                  "(run under `sg pantheon-mailbox`)", file=sys.stderr)
            return 2
        print(f"s3star_seat_evidence: wrote {out} ({len(captures)} captures)")
    failed = [c for c in captures if c.status != "OK"]
    for c in failed:
        print(f"s3star_seat_evidence: {c.name}: {c.status} {c.body}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
