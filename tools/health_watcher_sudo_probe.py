#!/usr/bin/env python3
"""health_watcher_sudo_probe — READ-ONLY deploy-time check for the health watcher's sudo needs (T-5415 b).

WHY. tools/health_watcher.py starts one tap per fleet_personas() account as
`sudo -n -u <acct> env ... python3 -I -c <TAP_SRC> ...`. If recovery's sudo rules do not allow
that, the account's tap is refused and silently left to the 60s tick. Run this BEFORE
installing/restarting the watcher unit; it says which accounts would be refused.

HOW. For each account it asks sudo whether the watcher's exact tap command would be allowed,
using `sudo -n -l -u <acct> <tap argv>`: the `-l` (list) form with a command only CHECKS the
policy and prints the command path (exit 0) or exits 1 if not allowed. It never executes the
command, never prompts (`-n`, stdin=/dev/null), and never changes sudoers.

Per account: OK | MISSING (policy says no / password required) | UNKNOWN (timeout, sudo
absent, unknown user, unexpected rc). Exit 0 only if every account is OK, else 1.
Never run on import; manual/deploy use only.
"""
from __future__ import annotations

import os
import pwd
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import health_watcher as hw  # noqa: E402
from tools.health_export import SudoReader, fleet_personas  # noqa: E402

TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Result:
    account: str
    status: str        # OK | MISSING | UNKNOWN
    detail: str        # bounded class string only; never sudo output


def _self_account() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return ""


def _check_argv(account: str) -> list[str] | None:
    """The watcher's real tap argv with `-l` inserted after `-n`; None if unbuildable;
    [] if no sudo is needed (the account is ourselves)."""
    reader = SudoReader()
    reader.self_account = _self_account()
    spec = hw.TapSpec(f"session:{account}", "session", hw.hc.PC_INTERFACE, account=account)
    argv = hw.tap_argv(spec, reader)
    if not argv:
        return None
    if argv[:2] != ["sudo", "-n"]:
        return []
    return [*argv[:2], "-l", *argv[2:]]


def probe_account(account: str) -> Result:
    argv = _check_argv(account)
    if argv is None:
        return Result(account, "UNKNOWN", "no passwd entry / no tap argv")
    if not argv:
        return Result(account, "OK", "self account, no sudo needed")
    assert argv[:3] == ["sudo", "-n", "-l"]        # never a prompting or executing form
    try:
        out = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                             timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return Result(account, "UNKNOWN", "timeout")
    except (OSError, subprocess.SubprocessError, ValueError):
        return Result(account, "UNKNOWN", "sudo could not be run")
    if out.returncode == 0:
        return Result(account, "OK", "sudo -l allows the tap command")
    text = (out.stderr + out.stdout).lower()
    if "unknown user" in text or "unknown group" in text:
        return Result(account, "UNKNOWN", "sudo: unknown user")
    if out.returncode == 1:
        if "password is required" in text:
            return Result(account, "MISSING", "password required (not NOPASSWD)")
        return Result(account, "MISSING", "not allowed by sudo policy")
    return Result(account, "UNKNOWN", f"unexpected sudo rc={out.returncode}")


def probe_all(personas) -> list[Result]:
    accounts = sorted({a for _p, a, _t in personas})
    return [probe_account(a) for a in accounts]


def format_result(r: Result) -> str:
    return f"{r.status:<8} {r.account}  {r.detail}"


def main(argv: list[str] | None = None) -> int:
    personas = fleet_personas()
    if not personas:
        print("UNKNOWN  (no fleet_personas() accounts found)")
        return 1
    results = probe_all(personas)
    for r in results:
        print(format_result(r))
    return 0 if all(r.status == "OK" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
