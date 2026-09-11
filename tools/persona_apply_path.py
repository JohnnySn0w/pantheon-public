"""Shared account-local environment for persona apply/preflight helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping


def controlled_apply_path(account_home: Path) -> str:
    """Return the deterministic PATH used for account-local harness apply."""
    return f"{account_home}/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"


def controlled_apply_environment(account_home: Path, uid: int) -> Mapping[str, str]:
    """Return the deterministic target-account apply/user-bus environment."""
    if uid < 0:
        raise ValueError("uid must be non-negative")
    runtime_dir = f"/run/user/{uid}"
    return {
        "PATH": controlled_apply_path(account_home),
        "XDG_RUNTIME_DIR": runtime_dir,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus",
    }
