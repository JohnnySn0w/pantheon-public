"""install_test_target() -- a minimal, throwaway systemd --user .target unit.

Stands in for the postboot-completion substrate primitive ORG-080 names
(``pantheon-germinate.target``, documentary per Architectural-tier scope
carve). This fixture is deliberately user-scope (no root) and randomly
named (never the literal production name) -- see T-3290 DECISIONS. Tranche
C owns the production substrate-primitive design decision; this fixture
only proves install/start/teardown mechanics are reachable without root.

"""

from __future__ import annotations

import contextlib
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .exceptions import HarnessUnavailable

_USER_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

_UNIT_TEMPLATE = """[Unit]
Description=T-3290 harness throwaway test target ({unit_name})
"""


@dataclass(frozen=True)
class TestTarget:
    unit_name: str
    unit_path: Path


def _systemctl_user(
    *args: str, timeout: float = 10.0
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessUnavailable(
            "systemd-user", f"systemctl --user {' '.join(args)} failed: {exc}"
        ) from exc


@contextlib.contextmanager
def install_test_target() -> Iterator[TestTarget]:
    """Install, start, and tear down a randomly-named throwaway .target unit.

    User-scope only (``~/.config/systemd/user/``) -- no root required. The
    unit name always carries a random suffix so it can never collide with a
    real ``pantheon-germinate.target`` install on the same account.
    """

    unit_name = f"germinate-harness-test-{uuid.uuid4().hex[:12]}.target"
    unit_path = _USER_UNIT_DIR / unit_name
    _USER_UNIT_DIR.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(
        _UNIT_TEMPLATE.format(unit_name=unit_name), encoding="utf-8"
    )

    reload_proc = _systemctl_user("daemon-reload")
    if reload_proc.returncode != 0:
        unit_path.unlink(missing_ok=True)
        raise HarnessUnavailable(
            "systemd-user", f"daemon-reload failed: {reload_proc.stderr.strip()}"
        )

    start_proc = _systemctl_user("start", unit_name)
    if start_proc.returncode != 0:
        unit_path.unlink(missing_ok=True)
        _systemctl_user("daemon-reload")
        raise HarnessUnavailable(
            "systemd-user", f"start {unit_name} failed: {start_proc.stderr.strip()}"
        )

    try:
        yield TestTarget(unit_name=unit_name, unit_path=unit_path)
    finally:
        _systemctl_user("stop", unit_name)
        unit_path.unlink(missing_ok=True)
        _systemctl_user("daemon-reload")
