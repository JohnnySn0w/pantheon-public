"""provision_repos.py — T-0761 component 4: clone-on-demand repo provisioning.

Called by ticket.py:cmd_claim after a successful claim. Clones (or fetches)
repos declared in the claimed ticket's `repos:` field.

Design constraints (settled in planning + HO approval 2026-06-14):
  - NEVER raises — all errors surface as warning strings in the return list.
  - SYNCHRONOUS with per-repo timeout; failure is best-effort, claim unaffected.
  - Trusts the declared `repos:` field (ticket_lint's gate already validated
    grants before the ticket could carry those entries; no re-implementation of
    grant resolution needed here — see comment at _should_skip).
  - ro  → HTTPS clone  (PAT-backed; no deploy-key required)
  - rw  → SSH alias clone (git@github-<repo>:example-org/<repo>.git; deploy key
    installed by HO out-of-band; this code only USES the alias)
  - always_available repos (pantheon-config) → skipped (already present)
  - Repo not found in repos.yaml `repos:` section → skipped with warning
    (can't build a safe URL; should not happen if lint ran)
  - Existing repo dir → `git fetch` (updates refs without touching the working
    tree; never fails on a dirty tree; provisioning = AVAILABLE, not forced-advance)
  - Absent repo dir  → `git clone`
  - Re-claiming the same ticket → idempotent (fetch on already-present dir)
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ProvisionReconcileResult:
    ok: bool
    detail: str
    action: str
    update_policy: str


ScopedProvisionAdapter = Callable[[str, Path], ProvisionReconcileResult]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_ORG = "example-org"

# SSH alias format mirrors the pattern established in T-0399 (2026-06-06):
# ~/.ssh/config Host alias `github-<repo>` → IdentityFile <deploy-key>.
# HO installs the alias+key; this code consumes the alias string.
SSH_ALIAS_TEMPLATE = "git@github-{repo}:{org}/{repo}.git"

HTTPS_TEMPLATE = "https://github.com/{org}/{repo}.git"

# Per-repo subprocess timeout (seconds). Caps worst-case latency added to claim.
# Injectable via the timeout param on provision_repos for tests/overrides.
DEFAULT_CLONE_TIMEOUT_SECS: int = 30

# Repos in `always_available` (repos.yaml) that are already present and should
# not be cloned on demand (pantheon-config is the current sole member).
_ALWAYS_AVAILABLE_REPOS: frozenset[str] = frozenset({"pantheon-config"})

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def provision_repos(
    ticket_fields: dict,
    *,
    repo_root: Path,
    git_runner: Callable[[list[str], Path | None, int], subprocess.CompletedProcess] | None = None,
    clone_base: Path | None = None,
    timeout: int = DEFAULT_CLONE_TIMEOUT_SECS,
    reconcile_adapter: ScopedProvisionAdapter | None = None,
) -> list[str]:
    """Provision repos declared in ticket_fields['repos'] for the claiming agent.

    Returns a list of warning strings (empty = all good). Never raises.

    Args:
        ticket_fields: parsed front-matter dict from the claimed ticket.
        repo_root:     root of the pantheon-config repo (used to locate repos.yaml).
        git_runner:    injectable callable (argv, cwd, timeout) -> CompletedProcess.
                       Defaults to the real subprocess.run. Tests inject a fake.
        clone_base:    directory under which repos are cloned. Defaults to
                       Path.home() (the OS user's home dir — correct for each
                       agent's execution context without reading instance.yaml).
        timeout:       per-repo subprocess timeout in seconds.
    """
    repos_field = ticket_fields.get("repos")
    if not repos_field:
        return []  # no repos declared — fast no-op

    if git_runner is None:
        git_runner = _default_git_runner

    if clone_base is None:
        clone_base = Path.home()

    # Load registry to identify always_available and validate repo names.
    registry = _load_registry(repo_root)

    entries = _parse_repos_entries(repos_field)
    warnings: list[str] = []

    for repo_name, access in entries:
        skip_reason = _should_skip(repo_name, registry)
        if reconcile_adapter is not None:
            # The adapter owns exact declarative checkout selection, manifest
            # resolution and scoped authorization. It is warning-only so a claim
            # remains best effort. No ordinary caller supplies it yet.
            if skip_reason and repo_name not in _ALWAYS_AVAILABLE_REPOS:
                warnings.append(f"{repo_name}: skipped — {skip_reason}")
                continue
            try:
                result = reconcile_adapter(repo_name, clone_base / repo_name)
            except Exception as exc:  # noqa: BLE001 — preserve never-raise contract
                warnings.append(f"{repo_name}: reconcile refused: unexpected reconciler error: {exc}")
                continue
            if type(result) is not ProvisionReconcileResult:
                warnings.append(f"{repo_name}: reconcile refused: adapter result is not structured")
                continue
            if not result.ok:
                warnings.append(f"{repo_name}: reconcile refused: {result.detail}")
                continue
            if result.update_policy != "availability-only":
                warnings.append(
                    f"{repo_name}: reconcile refused: update_policy {result.update_policy} is not availability-only"
                )
                continue
            if result.action not in {"clone", "noop"}:
                warnings.append(
                    f"{repo_name}: reconcile refused: action {result.action} exceeds availability-only"
                )
                continue
            if repo_name in _ALWAYS_AVAILABLE_REPOS and result.action != "noop":
                warnings.append(
                    f"{repo_name}: reconcile refused: always_available repo permits structured noop only, not {result.action}"
                )
            continue
        if skip_reason:
            warnings.append(f"{repo_name}: skipped — {skip_reason}")
            continue

        remote_url = _build_remote_url(repo_name, access)
        target_dir = clone_base / repo_name

        w = _provision_one(repo_name, remote_url, target_dir, git_runner, timeout)
        if w:
            warnings.append(w)

    return warnings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_repos_entries(repos_field: object) -> list[tuple[str, str]]:
    """Parse repos: field items into (name, access) tuples.

    Each item is `name` or `name:access`. Non-list or malformed entries are
    silently skipped (they would have been caught by ticket_lint's gate).
    """
    if not isinstance(repos_field, list):
        return []
    result: list[tuple[str, str]] = []
    for item in repos_field:
        name, _sep, access = str(item).partition(":")
        access = access or "ro"
        if name:
            result.append((name, access))
    return result


def _build_remote_url(repo_name: str, access: str) -> str:
    """Return the remote URL for a repo.

    ro  → HTTPS (PAT-backed; works without a deploy key)
    rw  → SSH alias (git@github-<repo>:example-org/<repo>.git)
    Any other access value falls back to HTTPS (defensive; lint should have caught it).
    """
    if access == "rw":
        return SSH_ALIAS_TEMPLATE.format(repo=repo_name, org=GITHUB_ORG)
    return HTTPS_TEMPLATE.format(repo=repo_name, org=GITHUB_ORG)


def _load_registry(repo_root: Path) -> dict | None:
    """Load repos.yaml, return parsed dict or None on any failure (fail-open).

    Thin wrapper around :func:`tools.repo_registry.load_repo_registry` (T-1136).
    """
    from tools.repo_registry import load_repo_registry

    return load_repo_registry(repo_root)


def _should_skip(repo_name: str, registry: dict | None) -> str | None:
    """Return a skip-reason string if this repo should be skipped, else None.

    Skips:
      1. always_available repos (pantheon-config) — already present, no clone needed.
      2. Repos not found in the registry's `repos:` section — can't build a safe URL;
         this should not happen if ticket_lint ran, but we skip defensively with a
         warning rather than attempting a clone against an unknown repo name.
         NOTE: no grant re-validation here. ticket_lint's gate (T-0761 component 1-3)
         validated grants before the ticket could carry these entries. Trusting the
         declared field avoids re-implementing persona→role mapping (which has no live
         instance.yaml reader yet). Cite: HO approval 2026-06-14.

    If registry is None (absent/unparseable), skip rule 2 only — fail-open means
    we attempt to provision repos we can build URLs for (always_available still skips).
    """
    if repo_name in _ALWAYS_AVAILABLE_REPOS:
        return "always_available (already present)"

    if registry is not None:
        repo_entries = registry.get("repos") or []
        known_names = {
            e.get("name")
            for e in repo_entries
            if isinstance(e, dict) and e.get("name")
        }
        if repo_name not in known_names:
            return f"unknown repo (not in repos.yaml registry); lint gate may not have run"

    return None


def _provision_one(
    repo_name: str,
    remote_url: str,
    target_dir: Path,
    git_runner: Callable[[list[str], Path | None, int], subprocess.CompletedProcess],
    timeout: int,
) -> str | None:
    """Clone (if absent) or fetch (if present) a single repo.

    Returns None on success, a warning string on any failure (timeout, non-zero
    returncode, or any exception).
    """
    try:
        if target_dir.exists():
            # Repo already present — fetch to update refs without touching the
            # working tree. `git fetch` never fails on a dirty tree; it makes
            # the repo AVAILABLE (refs current) without forcing advancement.
            # Justified by HO clarification 2026-06-14 (fetch over pull --ff-only).
            res = git_runner(["git", "fetch"], target_dir, timeout)
        else:
            # Clone into parent dir; git creates the repo-named subdir.
            res = git_runner(["git", "clone", remote_url, str(target_dir)], None, timeout)

        if res.returncode != 0:
            err = (res.stderr or "").strip() or (res.stdout or "").strip() or "non-zero exit"
            return f"{repo_name}: git operation failed (rc={res.returncode}): {err}"
        return None

    except subprocess.TimeoutExpired:
        return f"{repo_name}: git operation timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001 — best-effort, surface as warning
        return f"{repo_name}: unexpected error during provisioning: {exc}"


def _default_git_runner(
    argv: list[str],
    cwd: Path | None,
    timeout: int,
) -> subprocess.CompletedProcess:
    """Real subprocess.run wrapper matching the git_runner signature."""
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
