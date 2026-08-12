#!/usr/bin/env python3
"""tools/t2598_live_codex_login_check.py — gated live codex login-screen
proof over a synthetic-seeded hermetic CODEX_HOME (T-2598).

Upgrades ``codex_hermetic_home``'s pure-fixture unit tier to a genuine
"a real codex binary accepts the synthetic auth.json" proof: seeds a
disposable CODEX_HOME via :func:`seed_hermetic_codex_home` and runs
``codex login status`` against it, asserting the output reports logged-in.

Mirrors ``tools/t2582_live_telegram_accept.py``'s gate/skip shape (an
explicit opt-in env var, absent by default, missing it raises
:class:`AcceptanceSkipped` rather than silently no-op'ing) — same
name/behavior, deliberately NOT imported from that module: each live leg's
``AcceptanceSkipped`` is scoped to its own small domain (T-2582 is telegram
sends; T-2382's is the codex/claude/swap/forced-cap leg family) and a
shared base would couple unrelated legs for no benefit.

SEND-ISOLATION: the always-run suite (``python -m pytest tools/ -q``) never
sets ``T2598_LIVE_CODEX_LOGIN_CHECK``, so :func:`require_live_gate` always
raises :class:`AcceptanceSkipped` there and no codex process is ever
spawned by the test tier.

The disposable CODEX_HOME root is created under ``~/.cache`` (NOT ``/tmp``)
via ``tempfile.mkdtemp`` — codex emits a cosmetic-but-avoidable startup
warning ("Refusing to create helper binaries under temporary dir /tmp") for
a /tmp-rooted CODEX_HOME (T-2598 transition log, 2026-08-05 finding); a
non-/tmp root sidesteps it for free. The seeded home is removed in a
``finally`` block regardless of outcome — it holds only fabricated fixture
data, but littering ``~/.cache`` with one disposable dir per gated run is
untidy.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.bringup_acceptance.codex_hermetic_home import seed_hermetic_codex_home
from tools.bringup_acceptance.types import HardStop

GATE_ENV = "T2598_LIVE_CODEX_LOGIN_CHECK"
_GATE_TRUE_VALUES = frozenset({"1", "true", "yes"})

DEFAULT_CACHE_ROOT = Path.home() / ".cache"
_DISPOSABLE_HOME_PREFIX = "t2598-codex-home-"


class AcceptanceSkipped(RuntimeError):
    """Raised when the live gate env var is not explicitly set."""


def require_live_gate(env: Mapping[str, str]) -> None:
    """Raise :class:`AcceptanceSkipped` unless ``GATE_ENV`` is explicitly truthy."""
    if env.get(GATE_ENV) not in _GATE_TRUE_VALUES:
        raise AcceptanceSkipped(
            f"missing explicit live gate {GATE_ENV}; justifies-via P1/P4: the "
            "codex live-login-check leg is operator-gated"
        )


def run_live_login_check(
    *, codex_executable: str, cache_root: Path = DEFAULT_CACHE_ROOT
) -> str:
    """Seed a disposable hermetic home, run ``codex login status`` against
    it, and return the captured stdout on success.

    Raises :class:`HardStop` if the output never reports a logged-in state.
    Always removes the disposable home before returning or raising.
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    disposable_root = Path(tempfile.mkdtemp(prefix=_DISPOSABLE_HOME_PREFIX, dir=str(cache_root)))
    codex_home = disposable_root / ".codex"
    try:
        seed_hermetic_codex_home(codex_home, disposable_root)
        result = subprocess.run(
            [codex_executable, "login", "status"],
            env={**os.environ, "CODEX_HOME": str(codex_home)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        if "Logged in" not in result.stdout:
            raise HardStop(
                "live codex-login-check: 'codex login status' did not report "
                f"logged-in (returncode={result.returncode} "
                f"stdout={result.stdout!r} stderr={result.stderr!r})"
            )
        return result.stdout
    finally:
        shutil.rmtree(disposable_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-executable", default=shutil.which("codex") or "codex")
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    args = parser.parse_args(argv)

    try:
        require_live_gate(os.environ)
    except AcceptanceSkipped as exc:
        print(f"SKIPPED: {exc}")
        return 0

    stdout = run_live_login_check(
        codex_executable=args.codex_executable, cache_root=args.cache_root
    )
    print(f"ACCEPTED: codex login status reported logged-in ({stdout.strip()!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
