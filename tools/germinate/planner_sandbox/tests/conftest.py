"""pytest wrappers + typed skip-if-unavailable guards for T-3290 self-tests.

Per D5/D11: SKIP reasons must always name the concrete observed primitive
state, never a bare "skipped" -- tests that depend on systemd-user use the
``require_systemd_user`` fixture below rather than a raw ``pytest.skip()``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from tools.germinate.planner_sandbox.harness.exceptions import HarnessUnavailable
from tools.germinate.planner_sandbox.harness.sandbox_runner import probe_systemd_user


@pytest.fixture
def require_systemd_user() -> None:
    """Skip (typed reason) unless systemd-user is usable on this host."""

    try:
        probe_systemd_user()
    except HarnessUnavailable as exc:
        pytest.skip(reason=f"{exc.primitive} unavailable: {exc.reason}")


#: Operator escalation-password file, per the fleet-wide root.env
#: convention (never committed; mode 0600, owner-only). Read fresh on
#: every use, never cached module-wide -- see T-3292 DECISIONS for the
#: sudo_pw handling axes this fixture is held to. Env-var name and
#: override mechanism match the existing precedent in
#: ``tools/fleet_clone_currency_check.py`` (``PANTHEON_ROOT_ENV_PATH``)
#: rather than inventing a second convention for the same file -- so the
#: typed-SKIP path is independently reviewer-testable (e.g.
#: ``PANTHEON_ROOT_ENV_PATH=/nonexistent``) without touching the real
#: credential file.
_ROOT_ENV_PATH = Path(os.environ.get("PANTHEON_ROOT_ENV_PATH", "/home/claude/root.env"))

#: Generic, non-persona, universally-present unprivileged account used
#: as the second real uid for cross-uid admission tests. Deliberately
#: NOT another Pantheon persona's account (identity-boundary concern) --
#: `nobody` carries no authority and is a system-standard uid on any
#: Linux host.
PRIVILEGED_SWITCH_UID = 65534
PRIVILEGED_SWITCH_TARGET = "nobody"


def _read_sudo_pw() -> str:
    """Read the escalation password fresh from disk.

    Same idiom as ``tools/fleet_clone_currency_check.py``'s
    ``_read_sudo_password()`` (the closest existing precedent for
    root.env-backed ``sudo -S -k -u <account>``): ``.strip()``, with the
    caller appending the trailing newline ``sudo -S`` expects at the
    point of use. The returned string is never logged, never returned
    from a pytest fixture, and callers must drop their reference
    immediately after the single subprocess call that consumes it.
    """

    return _ROOT_ENV_PATH.read_text(encoding="utf-8").strip()


@pytest.fixture
def require_privileged_uid_switch() -> None:
    """Skip (typed reason) unless real cross-uid escalation is usable.

    Probes root.env presence FIRST (before any side-effecting action --
    reviewer verifies the SKIP path via an absent/unset root.env leaves
    no partial state), then performs one real, non-caching
    ``sudo -S -k -v`` credential check. Raises nothing on the happy
    path; the password is read-used-dropped within this function's own
    scope and never returned.
    """

    # T-3940 fix-4: guard is_file() itself, not just the subsequent read_text().
    # /home/claude/ on some CI runners is 0700 so the runner uid cannot stat()
    # entries in it — is_file() raises PermissionError before it can return
    # False. Same skip semantics for stat-refused, permission-refused-on-read,
    # and genuinely-absent — all three are "this uid cannot use root.env".
    try:
        present = _ROOT_ENV_PATH.is_file()
    except (PermissionError, OSError) as exc:
        pytest.skip(
            reason=f"privileged-uid-switch unavailable: cannot stat {_ROOT_ENV_PATH} ({exc})"
        )
    if not present:
        pytest.skip(
            reason=(
                "privileged-uid-switch unavailable: "
                f"{_ROOT_ENV_PATH} not present"
            )
        )

    try:
        sudo_pw = _read_sudo_pw()
    except (PermissionError, OSError) as exc:
        # T-3940 fix-3: file exists but current uid can't read it (typical CI-runner
        # posture — root.env is 0600 owned by a specific persona, other uids see it
        # but can't open). Functionally identical to "absent" for this fixture's
        # skip contract; the read-check inside the guard would otherwise raise
        # through pytest as an ERROR rather than a clean SKIP.
        pytest.skip(
            reason=f"privileged-uid-switch unavailable: cannot read {_ROOT_ENV_PATH} ({exc})"
        )
    try:
        proc = subprocess.run(
            ["sudo", "-S", "-k", "-v"],
            input=sudo_pw + "\n",
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(
            reason=f"privileged-uid-switch unavailable: sudo -v failed to run: {exc}"
        )
    finally:
        sudo_pw = None  # noqa: F841 -- drop reference promptly, never returned

    if proc.returncode != 0:
        pytest.skip(
            reason=(
                "privileged-uid-switch unavailable: sudo -v exited "
                f"{proc.returncode} (credential check failed)"
            )
        )

    # T-3753: also verify the switched-uid can actually IMPORT the
    # planner-sandbox module. On hosts where the operator's home is 0750
    # (default on some distros), `nobody` cannot traverse into it, so
    # PYTHONPATH=/home/<user>/pantheon-config yields ModuleNotFoundError
    # in the switched-uid shell -- the switch itself works but the source
    # tree is unreachable. Distinct primitive-state, distinct SKIP reason.
    sudo_pw2 = _read_sudo_pw()
    try:
        import_probe = subprocess.run(
            [
                "sudo", "-S", "-k", "-u", PRIVILEGED_SWITCH_TARGET,
                "env", f"PYTHONPATH={Path(__file__).resolve().parents[3].parent}",
                "python3", "-c", "import tools.germinate.planner_sandbox.admission",
            ],
            input=sudo_pw2 + "\n",
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(
            reason=f"privileged-uid-switch source-tree-unreachable probe failed: {exc}"
        )
    finally:
        sudo_pw2 = None  # noqa: F841

    if import_probe.returncode != 0:
        pytest.skip(
            reason=(
                "privileged-uid-switch source-tree-unreachable: "
                f"{PRIVILEGED_SWITCH_TARGET} cannot import the planner-sandbox "
                f"module (rc={import_probe.returncode}); likely operator-home "
                f"mode blocks traversal. stderr={import_probe.stderr.strip()[:200]!r}"
            )
        )


def run_as_privileged_switch_uid(
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    """Run ``args`` for real as ``PRIVILEGED_SWITCH_UID`` (``nobody``).

    A genuine ``setuid()``-mediated real-uid switch performed by the
    ``sudo`` binary itself (a privileged parent) -- NOT a mock, NOT the
    cosmetic ``unshare -U`` display-only uid (see T-3292 DECISIONS: that
    mechanism was empirically proven to leave the real kuid unchanged
    and was rejected on those grounds).

    The escalation password is read fresh from disk, piped via stdin
    only, and dropped immediately after this one call -- never logged,
    never exported to the child's environment, never returned. ``env``
    is passed to the target command via an explicit ``env K=V ...``
    prefix (NOT ``sudo --preserve-env``) because this sudoers
    configuration uses ``env_reset`` by default; wrapping with the
    ``env`` command sets the variable fresh inside the already-reset
    environment rather than depending on a preserve-env sudoers grant
    that may not be configured.

    Callers MUST assume ``require_privileged_uid_switch`` has already
    run and typed-skipped if unavailable -- this helper does not skip on
    its own.
    """

    env_prefix: list[str] = []
    if env:
        env_prefix = ["env", *(f"{key}={value}" for key, value in env.items())]

    cmd = ["sudo", "-S", "-k", "-u", PRIVILEGED_SWITCH_TARGET, *env_prefix, *args]

    sudo_pw = _read_sudo_pw()
    try:
        return subprocess.run(
            cmd, input=sudo_pw + "\n", capture_output=True, text=True, timeout=timeout
        )
    finally:
        sudo_pw = None  # noqa: F841 -- drop reference promptly, never returned
