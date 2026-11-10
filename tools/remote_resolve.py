"""tools/remote_resolve.py — T-1996: manifest-resolved git remotes (ORG-046).

Resolves a logical repo name to a reachable git remote through the TRUSTED
manifest (ORG-046 Decision §1), with bounded read-only self-heal (§2-§3) and
a security-hardened trust boundary (§4 / §7 codex-hardening):

  - a rename-redirect is NEVER authority — only a signal to re-read the
    trusted manifest (finding 4, security-critical).
  - auth-to-host binding (`allowed_hosts`) is enforced BEFORE any subprocess
    call — a manifest entry can't pair a real credential with an arbitrary
    URL (finding 6).
  - reachability = `git ls-remote <url>` (finding 10) — not DNS/ping.
  - only remotes carrying `pantheon-managed=true` are ever mutated; an
    unmanaged local remote gets a diagnostic refusal, never a silent
    clobber (finding 2).
  - self-heal repoints LOCAL git config only on a CONFIRMED PERMANENT
    identity change; transient/auth failures fall back for the current
    operation but never persist (finding 3).
  - a repo-local flock serializes any local git-config mutation
    (finding 8).

Wiring this resolver into an actual harness code path (git_operations,
config-sync, etc.) is OUT OF SCOPE here — that is T-1998. This module is a
standalone, independently-testable primitive; writes/push failover and
primary<->mirror sync policy are explicitly deferred to T-1998 per ORG-046
Decision §2.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECS = 10
DEFAULT_REMOTE_NAME = "origin"
LOCK_FILENAME = "pantheon-remote-resolve.lock"
MANAGED_MARKER_SUFFIX = "pantheon-managed"

# Injectable git-op runner. Mirrors the tools/provision_repos.py convention:
# (argv, cwd, timeout) -> CompletedProcess. Tests inject a fake; production
# callers may pass None to get subprocess.run.
GitRunner = Callable[[list[str], Path | None, int], subprocess.CompletedProcess]


# ---------------------------------------------------------------------------
# Data model (ORG-046 Decision §1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthRef:
    """One `auth_refs` entry: a named credential bound to allowed hosts.

    `allowed_hosts` is host-level defense-in-depth against a tampered
    manifest repointing this credential at an attacker host (ORG-046 §4);
    repo-scoping itself rides on the credential's own forge-side scope, not
    this binding — see ORG-046 §1.
    """

    name: str
    allowed_hosts: tuple[str, ...]


@dataclass(frozen=True)
class RemoteEntry:
    """One remote line in a repo's ordered `remotes:` list."""

    role: str  # "primary" | "mirror" (schema invariant: exactly one primary)
    url: str
    auth_ref: str  # key into Manifest.auth_refs
    # ORG-101 §2 (T-4690): True only on a `mirror` that accepts pushes (a regular,
    # converted Forgejo repo rather than a pull-mirror). Default False = read-only,
    # so the push tooling does nothing until the steward enables it per repo.
    writable: bool = False


@dataclass(frozen=True)
class RepoManifestEntry:
    """A single repo's manifest entry: its ordered remotes list.

    Preference = list order (ORG-046 §1) — the resolver does not re-sort by
    role; the manifest producer is trusted to have ordered it.
    """

    name: str
    remotes: tuple[RemoteEntry, ...]
    # ORG-081 §7 (T-5113): the ratified commit an external component's contract is resolved at.
    # A full 40-hex sha or None; a tag or branch moves, so it is not a pin.
    revision: str | None = None


@dataclass(frozen=True)
class Manifest:
    """The trusted manifest, already parsed (see load_manifest()).

    `signature_verified` is checked at resolve_remote()'s boundary, not only
    at load time — a hand-constructed or stale Manifest gets the same gate
    (defense-in-depth; D11/P4: verify at the consuming surface).
    """

    repos: dict[str, RepoManifestEntry]
    auth_refs: dict[str, AuthRef]
    signature_verified: bool = False


@dataclass(frozen=True)
class ResolvedRemote:
    """The result of a resolve_remote() call."""

    url: str
    auth_ref: str | None
    host_binding: str
    is_healed: bool
    healed_from: str | None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RemoteResolveError(Exception):
    """Base class for all resolver errors."""


class ManifestIntegrityError(RemoteResolveError):
    """The manifest failed signature/integrity verification (ORG-046 §4, Q2)."""


class HostBindingViolation(RemoteResolveError):
    """A url/auth pair's host is outside the auth ref's allowed_hosts (finding 6)."""


class UnmanagedRemoteError(RemoteResolveError):
    """A repoint is warranted but the local remote lacks pantheon-managed=true.

    Diagnostic-only, per ORG-046 §3: "fails with a diagnostic rather than
    clobbering it" — never a silent data-loss of local config.
    """


class NoReachableRemoteError(RemoteResolveError):
    """Every candidate remote failed reachability within the bounded retry.

    False-RED discipline (ORG-046 §3): a genuinely-gone remote must error,
    never be papered over.
    """


# ---------------------------------------------------------------------------
# Signature verification boundary (Q2: signing-as-default; no crypto here)
# ---------------------------------------------------------------------------


class SignatureVerifier(Protocol):
    """Injectable-at-the-boundary verification callable.

    Production implementation: `PinnedHashVerifier` (ORG-046 Q2 pinned-hash,
    T-3723). Tests may still mock this Protocol directly.
    """

    def __call__(self, path: Path, content: bytes) -> bool: ...


_PIN_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PinnedHashVerifier:
    """Production SignatureVerifier (ORG-046 §4, Q2 pinned-hash; T-3723).

    **INTEGRITY-only, NOT authenticity** (T-3723 §8.2 / codex ORG-041 #4):
    proves `repos.yaml`'s bytes are unchanged since the last authenticated-origin
    fetch (the pin). It does NOT prove authorship/authorization — *who* authored
    or approved the manifest is gated by the forge repo ACLs, not here.
    Author-authenticity would require signed-commits/detached-sigs + key custody
    (a deferred, larger mechanism), never this integrity check.

    The pin is a 64-hex SHA-256 in an **out-of-repo** store, written ONLY by the
    verified-config-pull entrypoint from bytes fetched off the authenticated
    origin — never from on-disk bytes (T-3723 §8.1 bootstrap rule: no TOFU path).

    `__call__` is **OFFLINE by contract** (codex ORG-041 #3): it reads the local
    pin and compares — it MUST NOT fetch, contact the origin, or check remote
    refs, or it would re-couple manifest verification to the very primary-remote
    reachability the resolver exists to fail over. **Fail-closed**: a missing /
    unreadable / malformed pin, or any hash mismatch, returns False (never
    raises — `resolve_remote()` is the single refusal point).
    """

    def __init__(self, pin_path: Path) -> None:
        self._pin_path = pin_path

    def __call__(self, path: Path, content: bytes) -> bool:
        expected = self._read_pin()
        if expected is None:
            return False
        actual = hashlib.sha256(content).hexdigest()
        # Constant-time compare (both are fixed-length lowercase hex).
        return hmac.compare_digest(actual, expected)

    def _read_pin(self) -> str | None:
        """The pinned 64-hex SHA-256, or None if absent/unreadable/malformed.

        Anything that is not exactly one lowercase 64-hex token (empty,
        truncated, non-hex, a directory, permission error) → None → verify
        returns False. Never trusts a partially-parsed pin."""
        try:
            raw = self._pin_path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return None
        return raw if _PIN_SHA256_RE.match(raw) else None


# ---------------------------------------------------------------------------
# Pin-writer (verified-config-pull) — establishes/updates the pin the verifier
# reads. THE bootstrap trust anchor (T-3723 §8.1, closes codex ORG-041 #2 TOFU).
# ---------------------------------------------------------------------------

# argv -> (returncode, stdout_bytes). Distinct from GitRunner (text-mode)
# because the manifest blob must be hashed as the RAW BYTES load_manifest reads
# back with path.read_bytes() — a text decode/re-encode round-trip is not
# guaranteed byte-identical.
PinWriterRunner = Callable[[list[str]], "tuple[int, bytes]"]


def _default_pin_writer_runner(argv: list[str]) -> "tuple[int, bytes]":
    proc = subprocess.run(argv, capture_output=True)  # bytes stdout (no text=)
    return proc.returncode, proc.stdout


def establish_pin(
    *,
    repo_root: Path,
    pin_path: Path,
    ref: str = "origin/main",
    manifest_rel: str = "repos.yaml",
    do_fetch: bool = True,
    runner: PinWriterRunner | None = None,
) -> str:
    """Write the manifest pin from bytes fetched off the AUTHENTICATED origin
    (T-3723 §8.1 — closes the bootstrap TOFU). **Never pins working-tree bytes.**

    Steps: (optionally) `git fetch <remote>` — authenticated by git's own
    transport (SSH host-key / HTTPS TLS); an unknown/failed host aborts pin-less.
    Then read the manifest blob AT `ref` via `git show <ref>:<manifest_rel>`
    (content-addressed, from the just-fetched authenticated object) as RAW BYTES,
    hash them, and atomically write the 64-hex SHA-256 to the out-of-repo pin
    store (0600). Any fetch/read failure RAISES ManifestIntegrityError and writes
    NO pin (fail-closed) — there is deliberately no code path that pins
    pre-existing on-disk bytes, so a tampered working tree can never seed a pin.

    The FIRST pin (bootstrap) is established at provisioning (operator-initiated,
    clean environment) by calling this from the authenticated origin at the
    provisioned ref — the operational call-site is phase-2 wiring, not this
    mechanism. This function is INTEGRITY provenance only, never authorship
    (codex ORG-041 #4)."""
    run = runner or _default_pin_writer_runner
    git = ["git", "-C", str(repo_root)]
    if do_fetch:
        remote = ref.split("/", 1)[0]
        rc, _ = run(git + ["fetch", "--quiet", remote])
        if rc != 0:
            raise ManifestIntegrityError(
                f"pin: fetch from authenticated origin {remote!r} failed (rc={rc}); no pin written"
            )
    rc, blob = run(git + ["show", f"{ref}:{manifest_rel}"])
    if rc != 0:
        raise ManifestIntegrityError(
            f"pin: reading {manifest_rel!r} at {ref!r} failed (rc={rc}); no pin written"
        )
    pin = hashlib.sha256(blob).hexdigest()
    _atomic_write_pin(pin_path, pin)
    return pin


def _atomic_write_pin(pin_path: Path, pin: str) -> None:
    """Write `pin` to `pin_path` atomically (temp + fsync + rename), 0600."""
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(pin_path.parent), prefix=".pin.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write((pin + "\n").encode("ascii"))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, pin_path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse_remote(repo_name: str, r: dict) -> RemoteEntry:
    """One remote line. `writable` (ORG-101 §2) must be a real bool and may only
    appear on a `mirror`: a primary is the write authority by definition, and a
    quoted "yes" must not silently read as truthy (hard-fail, ORG-046 §1)."""
    writable = r.get("writable", False)
    if not isinstance(writable, bool):
        raise ManifestIntegrityError(
            f"repo {repo_name!r} remote {r.get('url')!r}: writable must be a bool, got {writable!r}"
        )
    if writable and r["role"] != "mirror":
        raise ManifestIntegrityError(
            f"repo {repo_name!r} remote {r.get('url')!r}: writable is only valid on role: mirror"
        )
    return RemoteEntry(role=r["role"], url=r["url"], auth_ref=r["auth"], writable=writable)


_REVISION_PIN_RE = re.compile(r"^[0-9a-f]{40}$")


def _parse_revision(repo_name: str, entry: dict) -> str | None:
    if "revision" not in entry:
        return None
    rev = entry["revision"]
    if not (isinstance(rev, str) and _REVISION_PIN_RE.match(rev)):
        raise ManifestIntegrityError(f"repo {repo_name!r}: revision {rev!r} is not a 40-hex commit sha")
    return rev


def load_manifest(path: Path, *, signature_verifier: SignatureVerifier) -> Manifest:
    """Parse an ORG-046 §1-shaped manifest YAML file into a Manifest.

    Calls signature_verifier(path, raw_bytes) and records the verdict on
    Manifest.signature_verified. Does NOT raise on a False verdict here —
    resolve_remote() is the actual refusal point, so a Manifest built here
    and one hand-constructed by a caller get an identical gate.
    """
    raw = path.read_bytes()
    verified = bool(signature_verifier(path, raw))
    data = yaml.safe_load(raw) or {}

    auth_refs: dict[str, AuthRef] = {}
    for name, entry in (data.get("auth_refs") or {}).items():
        hosts = tuple(entry.get("allowed_hosts") or ())
        auth_refs[name] = AuthRef(name=name, allowed_hosts=hosts)

    repos: dict[str, RepoManifestEntry] = {}
    for repo_entry in data.get("repos") or []:
        remotes = tuple(_parse_remote(repo_entry["name"], r) for r in (repo_entry.get("remotes") or []))
        repos[repo_entry["name"]] = RepoManifestEntry(
            name=repo_entry["name"], remotes=remotes, revision=_parse_revision(repo_entry["name"], repo_entry)
        )

    return Manifest(repos=repos, auth_refs=auth_refs, signature_verified=verified)


# ---------------------------------------------------------------------------
# Pin store + production wiring (T-3764) — the out-of-repo pin location and
# the production 'build_config' factory that loads the manifest under the REAL
# PinnedHashVerifier. The bootstrap/refresh LIFECYCLE that populates the pin
# lives in tools/manifest_pin.py; wiring load_production_manifest() into the
# harness git paths is T-1998 phase-2 (supervised). Until a pin exists the
# verifier fail-closes, so resolve_remote stays inert — the safe default.
# ---------------------------------------------------------------------------

MANIFEST_REL = "repos.yaml"
PIN_FILENAME = "repos.yaml.pin"


def _state_dir() -> Path:
    """Out-of-repo state root. Matches the fleet ~/.local/state/pantheon
    convention (inbox_consumer, context_fill_hold, activity_monitor, ...);
    the PANTHEON_STATE_DIR override is the hermetic-test seam."""
    env = os.environ.get("PANTHEON_STATE_DIR")
    return Path(env) if env else Path.home() / ".local" / "state" / "pantheon"


def default_pin_path() -> Path:
    """Canonical out-of-repo manifest pin store (T-3764): 0600, written only by
    the pin-writer (establish_pin), read by PinnedHashVerifier. It is
    deliberately OUTSIDE the repo (ORG-046 §3 root-of-trust: a pin living in the
    same repo as repos.yaml would share the manifest's own trust domain, so a
    repo-write tamper could edit both)."""
    return _state_dir() / PIN_FILENAME


def default_repo_root() -> Path:
    """The pantheon-config clone this tools/ tree lives in — the manifest whose
    pin this seat verifies. Uses the dominant fleet convention
    (Path(__file__).resolve().parents[1]); the tool pins the clone it runs
    from, so a provisioning-time bootstrap pins that seat's fresh clone."""
    return Path(__file__).resolve().parents[1]


def load_production_manifest(
    *, repo_root: Path | None = None, pin_path: Path | None = None
) -> Manifest:
    """Production 'build_config' path (ORG-046 §4, T-3764): load
    <repo_root>/repos.yaml under the REAL PinnedHashVerifier(pin_path).

    This is what makes resolve_remote non-inert — but ONLY once a pin exists
    (established via tools/manifest_pin.py bootstrap/refresh); with no/mismatched
    pin the verifier fail-closes and resolve_remote refuses. Wiring THIS factory
    into the actual harness git paths is T-1998 phase-2 (supervised cutover)."""
    root = repo_root if repo_root is not None else default_repo_root()
    pin = pin_path if pin_path is not None else default_pin_path()
    return load_manifest(root / MANIFEST_REL, signature_verifier=PinnedHashVerifier(pin))


# ---------------------------------------------------------------------------
# Host binding (finding 6) — checked BEFORE any subprocess call
# ---------------------------------------------------------------------------


def _host_of(url: str) -> str:
    """Extract a host-ish string from a git remote URL.

    Handles scheme:// forms (https://host/org/repo.git,
    ssh://host/org/repo.git) and scp-like SSH (git@host:org/repo.git, where
    `host` may be an ssh-config alias rather than a real hostname).
    """
    if "://" in url:
        after_scheme = url.split("://", 1)[1]
        host_part = after_scheme.split("/", 1)[0]
        if "@" in host_part:
            host_part = host_part.split("@", 1)[1]
        return host_part.split(":", 1)[0]
    if "@" in url and ":" in url:
        after_at = url.split("@", 1)[1]
        return after_at.split(":", 1)[0]
    return url


def _check_host_binding(manifest: Manifest, remote: RemoteEntry) -> str:
    """Return the bound host if remote's url/auth pair is allowed; else raise.

    Runs before any subprocess call for that candidate (finding 6).
    """
    auth = manifest.auth_refs.get(remote.auth_ref)
    if auth is None:
        raise HostBindingViolation(
            f"auth ref {remote.auth_ref!r} not found in manifest (url {remote.url!r})"
        )
    host = _host_of(remote.url)
    if host not in auth.allowed_hosts:
        raise HostBindingViolation(
            f"url {remote.url!r} host {host!r} not in allowed_hosts for "
            f"auth {remote.auth_ref!r}: {auth.allowed_hosts}"
        )
    return host


# ---------------------------------------------------------------------------
# Reachability (finding 10) — git ls-remote, not DNS/ping
# ---------------------------------------------------------------------------


def _default_git_runner(argv: list[str], cwd: Path | None, timeout: int) -> subprocess.CompletedProcess:
    """Real subprocess.run wrapper matching the GitRunner signature."""
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _probe_reachable(url: str, *, git_runner: GitRunner, cwd: Path | None, timeout: int) -> bool:
    """fetch-reachability = `git ls-remote <url>` returns rc==0.

    `git ls-remote` is itself the ref-emitting protocol — a non-garbage
    response is rc==0; a dead/denied/misconfigured remote is non-zero or a
    timeout. Deeper expected-refs content parsing is left to T-1998 (the
    actual operational wiring), per HO ratification 2026-09-12.
    """
    try:
        res = git_runner(["git", "ls-remote", url], cwd, timeout)
    except subprocess.TimeoutExpired:
        return False
    except Exception:  # noqa: BLE001 — any runner failure means unreachable
        return False
    return res.returncode == 0


# ---------------------------------------------------------------------------
# Managed-remote model (finding 2) — never clobber unmanaged local config
# ---------------------------------------------------------------------------


def managed_remote(
    git_dir: Path,
    remote_name: str,
    *,
    git_runner: GitRunner | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECS,
) -> bool:
    """True iff local git config marks remote.<name>.pantheon-managed=true.

    Absence of the key, a config-read failure, or any non-"true" value all
    mean NOT managed — the safe default that never clobbers a user-added,
    fork, or temp remote.
    """
    runner = git_runner or _default_git_runner
    key = f"remote.{remote_name}.{MANAGED_MARKER_SUFFIX}"
    try:
        res = runner(["git", "config", "--get", key], git_dir, timeout)
    except Exception:  # noqa: BLE001 — fail closed (not managed)
        return False
    if res.returncode != 0:
        return False
    return (res.stdout or "").strip().lower() == "true"


def _current_remote_url(
    git_dir: Path,
    remote_name: str,
    *,
    git_runner: GitRunner,
    timeout: int,
) -> str | None:
    """Best-effort read of the currently-configured remote URL, for
    `healed_from` reporting on the permanent-identity-change path. Never
    raises — a read failure just means healed_from is None."""
    try:
        res = git_runner(["git", "config", "--get", f"remote.{remote_name}.url"], git_dir, timeout)
    except Exception:  # noqa: BLE001
        return None
    if res.returncode != 0:
        return None
    return (res.stdout or "").strip() or None


def _repoint_local_remote(
    git_dir: Path,
    remote_name: str,
    new_url: str,
    *,
    git_runner: GitRunner,
    timeout: int,
) -> None:
    res = git_runner(["git", "config", f"remote.{remote_name}.url", new_url], git_dir, timeout)
    if res.returncode != 0:
        raise RemoteResolveError(
            f"failed to repoint remote {remote_name!r} to {new_url!r}: "
            f"rc={res.returncode} {(res.stderr or '').strip()}"
        )


# ---------------------------------------------------------------------------
# Repo-local lock (finding 8) — serializes local git-config mutation
# ---------------------------------------------------------------------------


def _lock_path_for(git_dir: Path) -> Path:
    """Lock file lives inside the repo's own .git dir — never /tmp. Keeps
    the lock lifecycle-bound to the repo and hidden from `ls` on the
    working tree, per standard git-tooling convention."""
    return git_dir / ".git" / LOCK_FILENAME


class _RepoLock:
    """Blocking flock over a per-repo lockfile.

    Deliberately minimal — no TTL, no staleness detection, no PID-reap.
    This guards one short critical section (a local git-config write), not
    daemon-wide single-holder coordination; tools/_lease.py solves that
    different problem and its process-reaping semantics would be the wrong
    (and riskier) shape here.
    """

    def __init__(self, lock_path: Path):
        self._lock_path = lock_path
        self._fd = None

    def __enter__(self) -> "_RepoLock":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self._lock_path, "a+")
        fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX)  # blocks until free
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._fd is not None:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None
        return False


# ---------------------------------------------------------------------------
# Candidate ordering
# ---------------------------------------------------------------------------


def _ordered_candidates(repo_entry: RepoManifestEntry, prefer: str | None) -> list[RemoteEntry]:
    """Preference = manifest list order (ORG-046 §1), unless `prefer` names
    a role to try first (e.g. `prefer="mirror"` to dry-run failover)."""
    remotes = list(repo_entry.remotes)
    if prefer is None:
        return remotes
    preferred = [r for r in remotes if r.role == prefer]
    rest = [r for r in remotes if r.role != prefer]
    return preferred + rest


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_remote(
    repo_name: str,
    *,
    manifest: Manifest,
    prefer: str | None = None,
    git_dir: Path | None = None,
    git_runner: GitRunner | None = None,
    redirect_hint: str | None = None,
    remote_name: str = DEFAULT_REMOTE_NAME,
    timeout: int = DEFAULT_TIMEOUT_SECS,
) -> ResolvedRemote:
    """Resolve `repo_name` to a reachable remote via the trusted `manifest`.

    Read-only failover (ORG-046 §2): tries the top-preference candidate,
    then at most ONE more (bounded self-heal), then surfaces
    NoReachableRemoteError. Never persists a local config change for this
    path (transient/auth class never persists — ORG-046 §3).

    `redirect_hint`, if given, is a URL surfaced by a prior git operation
    (e.g. a rename-redirect). It is NEVER trusted directly (finding 4) —
    it is cross-checked against `manifest`'s own remotes for this repo; only
    a match is treated as a confirmed PERMANENT identity change, and only
    then (and only if the local remote is pantheon-managed) does this
    function repoint local git config, under a repo-local lock.
    """
    if not manifest.signature_verified:
        raise ManifestIntegrityError(
            f"manifest is not signature-verified; refusing to resolve remotes for {repo_name!r}"
        )

    repo_entry = manifest.repos.get(repo_name)
    if repo_entry is None or not repo_entry.remotes:
        raise NoReachableRemoteError(f"no manifest entry / remotes for repo {repo_name!r}")

    runner = git_runner or _default_git_runner

    if redirect_hint is not None:
        healed = _try_permanent_identity_change(
            repo_entry,
            manifest,
            redirect_hint,
            git_dir=git_dir,
            git_runner=runner,
            remote_name=remote_name,
            timeout=timeout,
        )
        if healed is not None:
            return healed
        # Hint didn't match any manifest entry — discarded, not authoritative.
        # Fall through to ordinary reachability-based resolution.

    return _resolve_by_reachability(
        repo_name,
        repo_entry,
        manifest,
        prefer,
        git_dir=git_dir,
        git_runner=runner,
        timeout=timeout,
    )


def _try_permanent_identity_change(
    repo_entry: RepoManifestEntry,
    manifest: Manifest,
    redirect_hint: str,
    *,
    git_dir: Path | None,
    git_runner: GitRunner,
    remote_name: str,
    timeout: int,
) -> ResolvedRemote | None:
    """If `redirect_hint` matches a manifest remote for this repo, treat it
    as a CONFIRMED permanent identity change and repoint (managed-only,
    lock-guarded). Returns None if the hint does not match — caller falls
    back to ordinary reachability resolution."""
    matched = next((r for r in repo_entry.remotes if r.url == redirect_hint), None)
    if matched is None:
        return None

    _check_host_binding(manifest, matched)  # enforce binding on the confirmed target too

    if git_dir is None:
        raise RemoteResolveError(
            "git_dir is required to repoint a local remote on a confirmed identity change"
        )

    if not managed_remote(git_dir, remote_name, git_runner=git_runner, timeout=timeout):
        raise UnmanagedRemoteError(
            f"remote {remote_name!r} in {git_dir} is not pantheon-managed; "
            "refusing to auto-repoint on confirmed identity change"
        )

    prior_url = _current_remote_url(git_dir, remote_name, git_runner=git_runner, timeout=timeout)

    with _RepoLock(_lock_path_for(git_dir)):
        _repoint_local_remote(git_dir, remote_name, matched.url, git_runner=git_runner, timeout=timeout)

    return ResolvedRemote(
        url=matched.url,
        auth_ref=matched.auth_ref,
        host_binding=_host_of(matched.url),
        is_healed=True,
        healed_from=prior_url,
    )


def _resolve_by_reachability(
    repo_name: str,
    repo_entry: RepoManifestEntry,
    manifest: Manifest,
    prefer: str | None,
    *,
    git_dir: Path | None,
    git_runner: GitRunner,
    timeout: int,
) -> ResolvedRemote:
    candidates = _ordered_candidates(repo_entry, prefer)
    top = candidates[0] if candidates else None

    attempted = 0
    for remote in candidates:
        _check_host_binding(manifest, remote)  # before any subprocess call
        attempted += 1
        if _probe_reachable(remote.url, git_runner=git_runner, cwd=git_dir, timeout=timeout):
            is_healed = remote is not top
            return ResolvedRemote(
                url=remote.url,
                auth_ref=remote.auth_ref,
                host_binding=_host_of(remote.url),
                is_healed=is_healed,
                healed_from=top.url if is_healed else None,
            )
        if attempted >= 2:
            # Bounded self-heal: at most one re-resolve+retry, then surface
            # (ORG-046 §3) — a genuinely-gone remote must error, never be
            # papered over past this point.
            break

    raise NoReachableRemoteError(
        f"no reachable remote for {repo_name!r} after {attempted} attempt(s)"
    )


# ---------------------------------------------------------------------------
# Write path (ORG-101 §2, T-4690): origin -> manifest primary -> writable mirror
# ---------------------------------------------------------------------------


def _origin_key(url: str) -> tuple[str, str] | None:
    """Normalize a git url to (host-class, 'owner/repo'), or None if unparseable.

    Seat clones reach GitHub through per-repo ssh host aliases (`github-<repo>`,
    ORG-046 §1), so a `github.com` host and a `github-*` alias are one host class.
    That is a tolerance, not alias resolution: it never guesses across host classes
    or across owner/repo, and anything unparseable (local paths included) is None."""
    url = (url or "").strip()
    if not url:
        return None
    if "://" in url:
        rest = url.split("://", 1)[1]
        host_part, _, path = rest.partition("/")
        host = host_part.rsplit("@", 1)[-1].split(":", 1)[0]
    elif ":" in url and not url.startswith(("/", ".")):
        left, _, path = url.partition(":")
        host = left.rsplit("@", 1)[-1]
    else:
        return None
    host = host.lower()
    if not host:
        return None
    segs = [s for s in path.strip("/").split("/") if s]
    if len(segs) != 2:
        return None
    repo = segs[1][:-4] if segs[1].endswith(".git") else segs[1]
    if not repo:
        return None
    host_class = "github" if host == "github.com" or host.startswith("github-") else host
    return host_class, f"{segs[0]}/{repo}".lower()


def writable_mirror_for(manifest: Manifest, origin_url: str) -> RemoteEntry | None:
    """The writable mirror for the repo whose manifest PRIMARY matches `origin_url`.

    Fail-closed everywhere: an unverified manifest, an origin that matches no repo
    (or more than one), no `writable: true` mirror, or a host-binding violation all
    return None, which means "no write redundancy" = today's behavior. Never guesses.
    Pure: no subprocess."""
    if not manifest.signature_verified:
        return None
    key = _origin_key(origin_url)
    if key is None:
        return None
    matches = [
        repo
        for repo in manifest.repos.values()
        if any(r.role == "primary" and _origin_key(r.url) == key for r in repo.remotes)
    ]
    if len(matches) != 1:
        return None
    for remote in matches[0].remotes:
        if remote.role == "mirror" and remote.writable:
            try:
                _check_host_binding(manifest, remote)
            except HostBindingViolation:
                return None
            return remote
    return None
