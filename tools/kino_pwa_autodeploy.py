#!/usr/bin/env python3
"""kino-pwa auto-redeploy poller (T-0316, the "kino-pull-cron variant").

Detects when `pantheon-pwa` `origin/main` has advanced beyond what is currently
deployed to the live kino server and runs the redeploy procedure automatically,
replacing the manual post-merge redeploy (SOP `sop/standard/redeploy-pantheon-pwa.md`).

Designed to run as a systemd timer on the Recovery (`claude-fallback`) account with a
~5-minute cadence. Recovery owns the kino-pwa repair-sudo path; the build + rsync touch
`claude`-owned dirs (wrapped in `sudo -u claude`), and the `kino.service` restart /
is-active use Recovery's NOPASSWD sudo tokens (`systemctl restart kino`,
`systemctl is-active kino`).

Mechanism (one fire):
  1. Check the feature flag (PANTHEON_PWA_AUTODEPLOY_ENABLED=1). Disabled → log one
     line and exit 0. Mirrors restart_drain's in-script flag (auditable in the
     journal, unlike a systemd ConditionEnvironment= which would silently skip).
  2. Acquire a non-blocking flock so two timer fires can't deploy concurrently.
  3. fetch origin/main on the source checkout; read `git rev-parse origin/main`.
  4. Compare against the recorded last-deployed SHA in the state file. If equal,
     nothing to do → exit 0 (idempotent: the state file gates re-deploy).
  5. ff-only advance the source checkout to origin/main (`git pull --ff-only`); a
     non-fast-forward (divergence) ABORTS — never `git reset --hard`.
  6. Build at source (`bun run build:web`) → smoke (`bun run smoke:build`). The
     smoke is the GATE: only if it passes do we proceed. A failed build/smoke must
     NEVER touch /opt or restart kino — log to stderr, exit non-zero, leave the
     state file unchanged.
  7. rsync source → deployment copy (NO --delete, per the SOP; exclude deps/vcs).
  8. `sudo systemctl restart kino`; then `sudo systemctl is-active kino` until
     active or timeout.
  9. Only after the whole sequence verifies → write the new SHA to the state file.

Safety posture (this restarts a production service):
  - Default-OFF kill-switch (step 1).
  - Smoke-gated, fail-soft ordering (step 6): a broken bundle never reaches /opt
    and never leaves kino restarted on bad code.
  - Idempotent via the state file (steps 4 + 9): only deploy on SHA change; only
    advance the recorded SHA after a fully-verified deploy.
  - No force / no destructive flags: ff-only source advance, rsync without --delete.
  - Bounded + structured logging: every external call has a subprocess timeout;
    log lines mirror restart_drain's `[<name> ts=...]` style for journal capture.
  - Concurrency guard: a flock (mirrors handoff_and_restart.sh's lockfile pattern).

Environment overrides:
  PANTHEON_PWA_AUTODEPLOY_ENABLED — must be "1" to enable; any other value
                                    (including absent) is treated as disabled.
  PANTHEON_STATE_DIR              — override state dir (default ~/.local/state/pantheon).
  PANTHEON_PWA_AUTODEPLOY_LOCKFILE — override the flock path (default
                                    /tmp/kino_pwa_autodeploy.lock).

Exit codes:
  0  — clean (disabled, up-to-date, or a fully-verified deploy).
  1  — generic / unexpected failure.
  2  — could not acquire the lock (another fire is in progress).
  3  — git fetch / rev-parse failed (could not determine origin/main).
  4  — source advance (ff-only pull) failed or non-fast-forward (divergence).
  5  — build failed.
  6  — smoke GATE failed (build artifacts missing) — /opt untouched, kino untouched.
  7  — rsync failed.
  8  — kino restart failed.
  9  — kino is-active verification failed after restart.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

# Make `from tools.X import Y` work whether invoked as `python3 tools/foo.py`
# or `python3 -m tools.foo`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Constants — production layout (verified 2026-05-21, per the redeploy SOP)
# ---------------------------------------------------------------------------

# Source checkout (claude-owned); build happens here.
SOURCE_DIR = "/home/claude/pantheon-pwa"
# Deployment copy (claude-owned); kino's WorkingDirectory.
DEPLOY_DIR = "/opt/pantheon-pwa"
# Account that owns SOURCE_DIR + DEPLOY_DIR — Recovery sudo's into it for build+rsync.
DEPLOY_USER = "claude"
# bun is NOT on the default PATH; export it explicitly inside the sudo'd shell.
BUN_PATH = "/home/claude/.bun/bin"
# kino is a System unit → restart needs root. Recovery has NOPASSWD for the bare
# `kino` form (NOT `kino.service`). Tokens: `systemctl restart kino`,
# `systemctl is-active kino`.
KINO_UNIT = "kino"

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
_STATE_FILENAME = "kino_pwa_autodeploy_last_sha.txt"
_DEFAULT_LOCKFILE = "/tmp/kino_pwa_autodeploy.lock"

# Subprocess timeouts (seconds). The build is the long pole (bun install + build).
GIT_TIMEOUT = 60
BUILD_TIMEOUT = 600
SMOKE_TIMEOUT = 120
RSYNC_TIMEOUT = 300
RESTART_TIMEOUT = 60
ISACTIVE_TIMEOUT = 60
ISACTIVE_POLL_INTERVAL = 2

# Exit codes (see module docstring).
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_LOCK = 2
EXIT_FETCH = 3
EXIT_ADVANCE = 4
EXIT_BUILD = 5
EXIT_SMOKE = 6
EXIT_RSYNC = 7
EXIT_RESTART = 8
EXIT_ISACTIVE = 9


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    """Emit a structured log line to stdout (captured by the systemd journal)."""
    ts = int(time.time())
    print(f"[kino_pwa_autodeploy ts={ts}] {msg}", flush=True)


def _err(msg: str) -> None:
    """Emit a structured ERROR line to stderr."""
    ts = int(time.time())
    print(f"[kino_pwa_autodeploy ts={ts}] ERROR: {msg}", file=sys.stderr, flush=True)


def _state_dir() -> Path:
    """Return the state dir, honouring PANTHEON_STATE_DIR env override."""
    env = os.environ.get("PANTHEON_STATE_DIR")
    return Path(env) if env else _DEFAULT_STATE_DIR


def _state_file() -> Path:
    """Path to the last-deployed-SHA state file."""
    return _state_dir() / _STATE_FILENAME


def _lockfile() -> str:
    """Flock path, honouring PANTHEON_PWA_AUTODEPLOY_LOCKFILE env override."""
    return os.environ.get("PANTHEON_PWA_AUTODEPLOY_LOCKFILE") or _DEFAULT_LOCKFILE


def _is_enabled() -> bool:
    """Return True iff PANTHEON_PWA_AUTODEPLOY_ENABLED=1."""
    return os.environ.get("PANTHEON_PWA_AUTODEPLOY_ENABLED") == "1"


def _read_last_sha() -> str | None:
    """Read the recorded last-deployed SHA; None if no state file / unreadable."""
    path = _state_file()
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except (OSError, ValueError):
        return None


def _write_last_sha(sha: str) -> None:
    """Record the last-deployed SHA atomically (write temp + rename)."""
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _state_file()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(sha + "\n", encoding="utf-8")
    tmp.replace(path)


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Thin subprocess.run wrapper: capture, text, never raises on non-zero exit.

    Raises subprocess.TimeoutExpired / OSError on launch failure (callers handle).
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _sudo_claude(inner: str) -> list[str]:
    """Build the argv for running `inner` (a shell snippet) as the deploy user.

    Build + rsync touch claude-owned dirs, so Recovery sudo's into `claude`. The bun
    PATH export is prepended for build/smoke steps that need it. Single-purpose
    helper so every claude-side call goes through the same sudo shape.
    """
    return ["sudo", "-nu", DEPLOY_USER, "bash", "-c", inner]


def _last_line(result: subprocess.CompletedProcess) -> str:
    """Best-effort short failure detail from a completed process."""
    text = (result.stderr or result.stdout or "").strip()
    if not text:
        return ""
    return text.splitlines()[-1]


# ---------------------------------------------------------------------------
# Pipeline steps. Each returns (ok, detail); the orchestrator maps to exit codes.
# ---------------------------------------------------------------------------


def fetch_origin_sha() -> tuple[bool, str]:
    """fetch origin/main on the source checkout and return its tip SHA.

    Returns (True, sha) or (False, error_detail). git ops run on the claude-owned
    checkout → wrapped with sudo -u claude.
    """
    fetch_inner = f"git -C {SOURCE_DIR} fetch origin main"
    try:
        res = _run(_sudo_claude(fetch_inner), GIT_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"git fetch errored: {exc!r}"
    if res.returncode != 0:
        return False, f"git fetch failed (rc={res.returncode}): {_last_line(res)}"

    rev_inner = f"git -C {SOURCE_DIR} rev-parse origin/main"
    try:
        res = _run(_sudo_claude(rev_inner), GIT_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"git rev-parse errored: {exc!r}"
    if res.returncode != 0:
        return False, f"git rev-parse failed (rc={res.returncode}): {_last_line(res)}"
    sha = res.stdout.strip()
    if not sha:
        return False, "git rev-parse returned empty SHA"
    return True, sha


def advance_source() -> tuple[bool, str]:
    """ff-only advance the source checkout to origin/main.

    Uses `git pull --ff-only origin main`. A non-fast-forward (the source checkout
    has diverged from origin/main) ABORTS — we never `git reset --hard`. Returns
    (True, "") or (False, error_detail).
    """
    inner = f"git -C {SOURCE_DIR} pull --ff-only origin main"
    try:
        res = _run(_sudo_claude(inner), GIT_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"git pull --ff-only errored: {exc!r}"
    if res.returncode != 0:
        return False, (
            f"git pull --ff-only failed (rc={res.returncode}) — possible divergence; "
            f"NOT force-resetting: {_last_line(res)}"
        )
    return True, ""


def build() -> tuple[bool, str]:
    """Build at source: `bun run build:web` with the bun PATH exported."""
    inner = f'export PATH={BUN_PATH}:$PATH; cd {SOURCE_DIR} && bun run build:web'
    try:
        res = _run(_sudo_claude(inner), BUILD_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"build errored: {exc!r}"
    if res.returncode != 0:
        return False, f"build failed (rc={res.returncode}): {_last_line(res)}"
    return True, ""


def smoke() -> tuple[bool, str]:
    """Smoke GATE: `bun run smoke:build` — asserts build/web/{index.html,
    manifest.webmanifest, sw.js}. If this fails the caller must NOT rsync/restart.
    """
    inner = f'export PATH={BUN_PATH}:$PATH; cd {SOURCE_DIR} && bun run smoke:build'
    try:
        res = _run(_sudo_claude(inner), SMOKE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"smoke errored: {exc!r}"
    if res.returncode != 0:
        return False, f"smoke:build failed (rc={res.returncode}): {_last_line(res)}"
    return True, ""


def rsync_to_deploy() -> tuple[bool, str]:
    """rsync source → deployment copy. NO --delete (per the SOP); exclude deps/vcs.

    Runs as claude (both dirs are claude-owned). Trailing slash on the source so
    contents (not the dir itself) are mirrored, matching the SOP command exactly.
    """
    # T-4027: --chmod=g-w. The source checkout's files are claude:pantheon-shared
    # rw-rw-r--, and `-a` copied that group-write into /opt, which let any
    # pantheon-shared seat edit the code kino executes (src/daemon/main.ts) and
    # the served bundles. Dirs too: nothing but this rsync (as the owner, claude)
    # writes /opt, so no group needs write there.
    inner = (
        "rsync -a --chmod=g-w "
        "--exclude node_modules --exclude .git --exclude web/node_modules "
        f"{SOURCE_DIR}/ {DEPLOY_DIR}/"
    )
    try:
        res = _run(_sudo_claude(inner), RSYNC_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"rsync errored: {exc!r}"
    if res.returncode != 0:
        return False, f"rsync failed (rc={res.returncode}): {_last_line(res)}"
    return True, ""


def restart_kino() -> tuple[bool, str]:
    """`sudo systemctl restart kino` (bare `kino` form — Recovery's NOPASSWD token)."""
    cmd = ["sudo", "systemctl", "restart", KINO_UNIT]
    try:
        res = _run(cmd, RESTART_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"systemctl restart errored: {exc!r}"
    if res.returncode != 0:
        return False, f"systemctl restart kino failed (rc={res.returncode}): {_last_line(res)}"
    return True, ""


def verify_active() -> tuple[bool, str]:
    """Poll `sudo systemctl is-active kino` until 'active' or timeout."""
    cmd = ["sudo", "systemctl", "is-active", KINO_UNIT]
    deadline = time.time() + ISACTIVE_TIMEOUT
    last_state = "unknown"
    while time.time() < deadline:
        try:
            res = _run(cmd, RESTART_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f"systemctl is-active errored: {exc!r}"
        last_state = (res.stdout or "").strip() or "unknown"
        if last_state == "active":
            return True, "active"
        if last_state == "failed":
            return False, "kino is-active: failed"
        time.sleep(ISACTIVE_POLL_INTERVAL)
    return False, f"is-active timeout after {ISACTIVE_TIMEOUT}s; last state: {last_state}"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _deploy(target_sha: str) -> int:
    """Run the smoke-gated deploy pipeline for target_sha. Returns an exit code.

    Ordering is fail-soft + smoke-gated: build → smoke (GATE) → rsync → restart →
    verify. A build/smoke failure returns BEFORE rsync/restart, so a broken bundle
    never reaches /opt and kino is never restarted on bad code. The state file is
    advanced ONLY after verify passes.
    """
    # 1. Advance the source checkout to the target tip (ff-only; abort on divergence).
    ok, detail = advance_source()
    if not ok:
        _err(f"source advance failed: {detail}")
        return EXIT_ADVANCE
    _log("source checkout advanced to origin/main (ff-only)")

    # 2. Build.
    ok, detail = build()
    if not ok:
        _err(f"build failed; NOT touching {DEPLOY_DIR}, NOT restarting kino: {detail}")
        return EXIT_BUILD
    _log("build:web succeeded")

    # 3. Smoke GATE — the bright line before any /opt or kino mutation.
    ok, detail = smoke()
    if not ok:
        _err(
            f"smoke:build GATE failed; ABORTING before rsync/restart "
            f"(live PWA + kino untouched, state file unchanged): {detail}"
        )
        return EXIT_SMOKE
    _log("smoke:build GATE passed — proceeding to rsync + restart")

    # 4. rsync to the deployment copy.
    ok, detail = rsync_to_deploy()
    if not ok:
        _err(f"rsync failed; NOT restarting kino, state file unchanged: {detail}")
        return EXIT_RSYNC
    _log(f"rsync to {DEPLOY_DIR} succeeded")

    # 5. Restart kino.
    ok, detail = restart_kino()
    if not ok:
        _err(f"kino restart failed; state file unchanged: {detail}")
        return EXIT_RESTART
    _log("kino restart issued")

    # 6. Verify the unit is active.
    ok, detail = verify_active()
    if not ok:
        _err(f"kino is-active verification failed; state file unchanged: {detail}")
        return EXIT_ISACTIVE
    _log("kino is-active: active")

    # 7. Fully verified → advance the state file. Only now.
    _write_last_sha(target_sha)
    _log(f"deploy verified end-to-end; recorded last-deployed SHA {target_sha}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run one poll/deploy cycle. See module docstring for exit codes."""

    if not _is_enabled():
        _log("autodeploy disabled; PANTHEON_PWA_AUTODEPLOY_ENABLED not set to '1'; exiting")
        return EXIT_OK

    # Concurrency guard: non-blocking flock so two timer fires can't deploy
    # concurrently. Mirrors handoff_and_restart.sh — hold the fd for the whole
    # process lifetime (implicit release on exit). Keep a module-level reference
    # so the fd is not garbage-collected (which would release the lock).
    lockfile = _lockfile()
    try:
        lock_fd = open(lockfile, "w")  # noqa: SIM115 — held for process lifetime
    except OSError as exc:
        _err(f"could not open lockfile {lockfile}: {exc!r}")
        return EXIT_GENERIC
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _err(f"lock {lockfile} held by another instance; another fire is in progress")
        lock_fd.close()
        return EXIT_LOCK
    # Keep the fd alive for the rest of main(); released when the process exits.
    main._lock_fd = lock_fd  # type: ignore[attr-defined]

    # Determine origin/main's current tip.
    ok, sha_or_detail = fetch_origin_sha()
    if not ok:
        _err(f"could not determine origin/main SHA: {sha_or_detail}")
        return EXIT_FETCH
    origin_sha = sha_or_detail

    # Idempotency gate: only deploy when origin/main has advanced beyond the
    # recorded last-deployed SHA.
    last_sha = _read_last_sha()
    if last_sha == origin_sha:
        _log(f"origin/main ({origin_sha}) matches last-deployed SHA; nothing to do")
        return EXIT_OK

    if last_sha is None:
        _log(
            f"no recorded last-deployed SHA; origin/main is {origin_sha}; "
            f"treating as a deploy-needed first run"
        )
    else:
        _log(f"origin/main advanced: {last_sha} -> {origin_sha}; deploying")

    return _deploy(origin_sha)


if __name__ == "__main__":
    sys.exit(main())
