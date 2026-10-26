#!/usr/bin/env python3
"""seat_exec — the one own-account-safe way to build a per-seat cross-account argv.

THE CLASS this centralises (bugs/sudo-u-to-runners-own-account-fails-per-seat-cross-
account-tool-false-null.md; T-4032/T-4066): a fleet tool that prepends `sudo -n -u
<seat_account>` uniformly for EVERY seat FAILS on the one seat whose account == the
account the tool runs under — each agent's NOPASSWD sudo is scoped as OTHER agent-users
only, so `sudo -n -u <self>` is rejected, emits nothing on stdout, and exits non-zero,
byte-identical to a probe that legitimately found nothing. A tool keying off stdout then
fabricates a negative for its OWN seat. It has bitten four unrelated tools.

`seat_argv` is the fix in one place: run DIRECTLY (no sudo) when the target account is our
own, otherwise prefix `sudo -n -u <account>`. Adopt it instead of hand-rolling the sudo
prefix and the own-account case cannot regress. self_sudo_guard_lint flags the hand-rolled
form; this is what it points adopters at.

    from seat_exec import seat_argv
    argv = seat_argv(account, ["cat", path])                    # sudo -n -u <account> cat <path>, or direct
    argv = seat_argv(account, ["bash", "-c", inner])            # cross-account shell snippet
    argv = seat_argv(account, ["git", "-C", d, "log"], sudo=("sudo", "-S", "-k", "-u"))

The `sudo` prefix defaults to the non-interactive `("sudo", "-n", "-u")` (fail-fast, never
prompts) — the right default for unattended fleet tools. Override it (last element must be
`-u`, the account is appended after it) for a caller that needs a different sudo shape.
"""
from __future__ import annotations

import getpass
from typing import Sequence

#: Default cross-account prefix: non-interactive sudo, target user appended after `-u`.
DEFAULT_SUDO_PREFIX: tuple[str, ...] = ("sudo", "-n", "-u")


def is_own_account(account: str) -> bool:
    """True iff `account` is the account this process runs under (getpass.getuser()).
    The self-vs-other discrimination the whole class turns on, in one testable spot."""
    return account == getpass.getuser()


def seat_argv(account: str, argv: Sequence[str], *,
              sudo: Sequence[str] = DEFAULT_SUDO_PREFIX) -> list[str]:
    """Build the argv to run `argv` AS `account`.

    Own account -> `argv` verbatim (NO sudo: sudo-to-self is rejected, the false-null
    class). Every other account -> `[*sudo, account, *argv]` (default `sudo -n -u
    <account> …`). `argv` is any command list — the caller owns any `bash -c` wrapping."""
    if is_own_account(account):
        return list(argv)
    return [*sudo, account, *argv]
