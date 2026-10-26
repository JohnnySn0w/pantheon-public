"""tools/read_failover.py — T-1998 phase 2: ORG-046 READ failover for the pull path.

Wires the T-1996 resolver (tools/remote_resolve.py) into the fleet's fail-open
pull (`_git_utils.pull_repo_or_warn` — every seat's inbox_consumer pull, plus the
digest/grooming crons). Two operations, both no-ops unless ARMED:

  - refresh_pin_if_armed : after a SUCCESSFUL `git fetch origin main`, re-pin
      repos.yaml from the just-fetched origin/main blob (establish_pin,
      do_fetch=False). This is the "verified-config-pull" leg of T-3723 §8.1:
      the pin only ever comes from bytes fetched off the authenticated origin,
      never from the working tree and never from the mirror.
  - fetch_main_via_mirror : after a FAILED origin fetch, resolve pantheon-config
      through the pin-verified manifest; if the resolver fails over to the mirror
      (primary unreachable per `git ls-remote`), fetch the mirror's main into a
      scratch ref and fast-forward refs/remotes/origin/main to it.

Safety properties (ORG-046 §2/§3/§4):
  - READ only. Nothing here pushes, and nothing persists a remote change: the
    mirror url is used for this one fetch (ephemeral fallback, §2).
  - origin/main only ever moves FORWARD. A mirror behind origin/main (normal:
    the Forgejo pull-mirror syncs every 24h) is a no-op; a mirror that DIVERGES
    from origin/main is refused, since a real pull-mirror can't diverge from its
    source (forked or tampered mirror).
  - The ref update is compare-and-swap on the old value, so a concurrent
    successful origin fetch is never clobbered.
  - No git url rewriting: if git config (url.<base>.insteadOf) would rewrite
    any of the repo's manifest urls, the failover refuses before the resolver's
    credential-bearing probes run, because host binding is checked on the
    manifest string and a rewrite would bypass it.
  - Fail-closed on trust: no pin / pin mismatch / unverified manifest means the
    resolver refuses and there is no failover. Callers are fail-open on
    reachability: any failure here is reported and the stale clone is used,
    exactly as before this module existed.

ARMED = env PANTHEON_READ_FAILOVER=1 AND a manifest pin exists
(tools/manifest_pin.py bootstrap). The env var is the explicit per-unit cutover
switch; tests and interactive shells never set it, so the default everywhere is
the pre-T-1998 behaviour.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:  # package import (tools.read_failover) and script-dir import (read_failover)
    from tools.remote_resolve import (
        GitRunner,
        _default_git_runner,
        default_pin_path,
        establish_pin,
        load_production_manifest,
        resolve_remote,
    )
except ImportError:  # pragma: no cover — script-dir import path
    from remote_resolve import (  # type: ignore[no-redef]
        GitRunner,
        _default_git_runner,
        default_pin_path,
        establish_pin,
        load_production_manifest,
        resolve_remote,
    )

ENV_SWITCH = "PANTHEON_READ_FAILOVER"
REPO_NAME = "pantheon-config"
TRACKING_REF = "refs/remotes/origin/main"
SCRATCH_REF = "refs/pantheon/failover/main"
DEFAULT_TIMEOUT_SECS = 30


@dataclass(frozen=True)
class FailoverResult:
    """Outcome of fetch_main_via_mirror. `ok` means refs/remotes/origin/main is
    now at least as fresh as the mirror (advanced, or already ahead/equal)."""

    ok: bool
    detail: str


def armed(pin_path: Path | None = None) -> bool:
    """True iff the env switch is on AND a manifest pin exists."""
    if os.environ.get(ENV_SWITCH) != "1":
        return False
    return (pin_path or default_pin_path()).exists()


def refresh_pin_if_armed(repo_root: Path, *, pin_path: Path | None = None) -> str | None:
    """Re-pin repos.yaml from origin/main after a successful origin fetch.

    Returns the pin written, or None when not armed. Raises what establish_pin
    raises (ManifestIntegrityError); the caller decides how loud to be."""
    if not armed(pin_path):
        return None
    return establish_pin(
        repo_root=repo_root,
        pin_path=pin_path or default_pin_path(),
        ref="origin/main",
        do_fetch=False,
    )


def _rev(run: GitRunner, repo_root: Path, ref: str, timeout: int) -> str | None:
    res = run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo_root, timeout)
    sha = res.stdout.strip() if res.returncode == 0 else ""
    return sha or None


def _is_ancestor(run: GitRunner, repo_root: Path, a: str, b: str, timeout: int) -> bool:
    return run(["git", "merge-base", "--is-ancestor", a, b], repo_root, timeout).returncode == 0


def _rewritten_url(run: GitRunner, repo_root: Path, url: str, timeout: int) -> str | None:
    """What git would actually contact for `url` if config rewrites it, else None.

    resolve_remote checks host binding on the manifest STRING, but git applies
    url.<base>.insteadOf (repo/global/system config) after that, so a poisoned
    config could send the probe's or the fetch's credential to another host
    (codex T-1998 review, High). `git ls-remote --get-url` is local-only: it
    prints the url after rewriting and contacts nothing."""
    res = run(["git", "ls-remote", "--get-url", url], repo_root, timeout)
    effective = res.stdout.strip() if res.returncode == 0 else ""
    if effective == url:
        return None
    return effective or "<unresolvable>"


def fetch_main_via_mirror(
    repo_root: Path,
    *,
    pin_path: Path | None = None,
    git_runner: GitRunner | None = None,
    manifest_loader=load_production_manifest,
    resolver=resolve_remote,
    timeout: int = DEFAULT_TIMEOUT_SECS,
) -> FailoverResult:
    """ORG-046 §2 read failover for `git fetch origin main`. See module docstring.

    `manifest_loader` / `resolver` / `git_runner` are the hermetic-test seams."""
    if not armed(pin_path):
        return FailoverResult(False, f"not armed ({ENV_SWITCH}=1 + manifest pin required)")
    run = git_runner or _default_git_runner

    try:
        manifest = manifest_loader(repo_root=repo_root, pin_path=pin_path)
        if not manifest.signature_verified:
            return FailoverResult(False, "resolver refused: manifest does not verify against the pin")
        entry = manifest.repos.get(REPO_NAME)
        for remote in entry.remotes if entry else ():
            rewritten = _rewritten_url(run, repo_root, remote.url, timeout)
            if rewritten is not None:
                return FailoverResult(
                    False,
                    f"git config rewrites manifest url {remote.url} -> {rewritten} (insteadOf); refusing",
                )
        resolved = resolver(REPO_NAME, manifest=manifest, git_dir=repo_root, git_runner=run)
    except Exception as exc:  # noqa: BLE001 — any resolver/manifest failure = no failover
        return FailoverResult(False, f"resolver refused: {type(exc).__name__}: {exc}")
    if not resolved.is_healed:
        # The primary answers ls-remote, so the fetch failure was not a
        # reachability failure (ORG-046 §2 fails over on reachability only).
        return FailoverResult(False, "primary is reachable; fetch failure is not a reachability failure")

    try:
        fetch = run(
            ["git", "fetch", "--no-tags", "--quiet", resolved.url, f"+refs/heads/main:{SCRATCH_REF}"],
            repo_root,
            timeout,
        )
        if fetch.returncode != 0:
            detail = (fetch.stderr or fetch.stdout).strip().splitlines()[-1:] or [""]
            return FailoverResult(False, f"mirror fetch failed ({resolved.host_binding}): {detail[0]}")

        new = _rev(run, repo_root, SCRATCH_REF, timeout)
        if new is None:
            return FailoverResult(False, "mirror fetch left no scratch ref")
        old = _rev(run, repo_root, TRACKING_REF, timeout)

        if old == new:
            return FailoverResult(True, f"mirror {resolved.host_binding} at origin/main already ({new[:9]})")
        if old is not None and _is_ancestor(run, repo_root, new, old, timeout):
            return FailoverResult(True, f"mirror {resolved.host_binding} is behind origin/main; nothing newer")
        if old is not None and not _is_ancestor(run, repo_root, old, new, timeout):
            return FailoverResult(
                False,
                f"mirror {resolved.host_binding} main {new[:9]} DIVERGES from origin/main {old[:9]}; refusing",
            )

        # Fast-forward (or first population), compare-and-swap on the old value.
        # An empty old value means "must not exist yet".
        upd = run(
            ["git", "update-ref", "-m", f"read-failover: {resolved.host_binding}", TRACKING_REF, new, old or ""],
            repo_root,
            timeout,
        )
        if upd.returncode != 0:
            detail = (upd.stderr or upd.stdout).strip().splitlines()[-1:] or [""]
            return FailoverResult(False, f"origin/main update refused: {detail[0]}")
        span = f"{old[:9]}..{new[:9]}" if old else f"(new) {new[:9]}"
        return FailoverResult(True, f"advanced origin/main {span} from mirror {resolved.host_binding}")
    except subprocess.TimeoutExpired as exc:
        return FailoverResult(False, f"mirror step timed out: {exc.cmd!r}")
    except OSError as exc:
        return FailoverResult(False, f"mirror step failed: {exc!r}")
