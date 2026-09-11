"""run_in_sandbox() -- the harness's real-host systemd-run --user transport.

This is deliberately NOT the production launcher
(tools/germinate/planner_sandbox/launcher.py, tranche D). It proves the
systemd-run --user transport mechanism against a real host and gives every
tranche (especially D's canary suite) a typed RunResult to build on. See
T-3290 DECISIONS for the callable-transport and default-properties
boundary.

"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .exceptions import HarnessUnavailable

#: Conservative default confinement the harness's own self-test relies on.
#: Each tranche (esp. D) supplies its OWN full C5 enforced-properties list
#: via the `properties` kwarg -- this default is deliberately NOT the full
#: profile from ORG-080 Component contract (that profile needs the vendored
#: seccomp wheel + BindReadOnlyPaths interpreter-closure enumeration, which
#: are tranche D's bootstrap responsibility, not the harness's).
DEFAULT_PROPERTIES: tuple[str, ...] = (
    "NoNewPrivileges=yes",
    "PrivateTmp=yes",
)

_PYTHON = "/usr/bin/python3"


@dataclass(frozen=True)
class RunResult:
    """Outcome of one ``run_in_sandbox()`` invocation.

    ``returncode`` is systemd-run's own raw exit code under ``--wait``: 0
    on clean exit, the unit's own exit code on non-zero clean exit. On a
    signal-kill, ``systemd-run`` does **not** follow the shell's
    128+signal convention -- empirically verified (T-3290 Phase 2 test 4,
    real systemd 255 on this host) to return a flat 255 regardless of
    which signal killed the unit. Signal-kill evidence (which signal,
    "Finished with result: signal") is recoverable only from ``stderr``
    text, not from ``returncode`` arithmetic. Callers needing a
    killed-vs-exited distinction must inspect ``stderr``.
    """

    returncode: int
    stdout: str
    stderr: str
    unit_name: str


def probe_systemd_user() -> None:
    """Raise HarnessUnavailable unless systemd-user is usable on this host.

    Probe: ``systemctl --user is-system-running``. Only the exact stdout
    ``running`` (exit 0) is accepted -- ``degraded`` and every other state
    are treated as unavailable (fail-closed; a degraded manager could be
    masking an unrelated failure, and a false SKIP is cheaper here than a
    false PASS against a primitive that isn't fully healthy).
    """

    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessUnavailable(
            "systemd-user", f"probe failed to run: {exc}"
        ) from exc

    state = proc.stdout.strip()
    if proc.returncode != 0 or state != "running":
        raise HarnessUnavailable(
            "systemd-user",
            f"systemctl --user is-system-running reported state={state!r} "
            f"exit={proc.returncode}",
        )


def run_in_sandbox(
    source: str,
    *,
    scratch_dir: Path,
    properties: Sequence[str] = DEFAULT_PROPERTIES,
    timeout: float = 10.0,
) -> RunResult:
    """Run ``source`` (a self-contained Python script) under systemd-run --user.

    Writes ``source`` to ``<scratch_dir>/probe.py`` (mode 0644), then
    invokes it via a transient systemd-run --user unit with
    ``--wait --pipe --collect`` (unit stdio piped straight through --
    empirically verified on the real host to isolate systemd-run's own
    status chatter onto its OWN stderr, never mixing into the unit's
    stdout). ``-u`` (unbuffered) avoids flush-timing ambiguity between
    "clean exit, empty stdout" and "killed before flushing."
    """

    probe_path = scratch_dir / "probe.py"
    probe_path.write_text(source, encoding="utf-8")
    probe_path.chmod(0o644)

    unit_name = f"t3290-harness-{uuid.uuid4().hex[:12]}"
    cmd = [
        "systemd-run",
        "--user",
        "--wait",
        "--pipe",
        "--collect",
        f"--unit={unit_name}",
    ]
    for prop in properties:
        cmd.append(f"--property={prop}")
    cmd += ["--", _PYTHON, "-u", str(probe_path)]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessUnavailable(
            "systemd-user",
            f"run_in_sandbox: systemd-run invocation failed: {exc}",
        ) from exc

    return RunResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        unit_name=unit_name,
    )
