"""tools/manifest_pin.py — ORG-046 manifest pin LIFECYCLE (T-3764).

The pin PRIMITIVES live in tools/remote_resolve.py (T-3723): `PinnedHashVerifier`
(reads the out-of-repo pin, offline, fail-closed) and `establish_pin` (writes the
pin from bytes fetched off the AUTHENTICATED origin — never from working-tree
bytes; T-3723 §8.1 closes the bootstrap TOFU). This module wires their
LIFECYCLE — the operator/harness entrypoints that populate and refresh the pin:

  - bootstrap : establish the FIRST pin at seat provisioning (operator-initiated,
                clean environment; §8.1). Refuses to clobber an existing pin
                unless --force.
  - refresh   : re-establish the pin from the authenticated origin after an
                AUTHORIZED manifest change (the pin leg of a verified config
                pull), so the pin tracks origin. Reports whether the working-tree
                repos.yaml now verifies against the new pin — the phase-2
                readiness signal (the working-tree pull itself is phase-2).
  - status    : report whether a pin exists and whether the current working-tree
                repos.yaml verifies against it.

Both bootstrap and refresh are the SAME underlying operation (§8.1: fetch
repos.yaml off the authenticated origin at an explicit ref, hash THOSE bytes,
atomically write the 0600 pin) — the split is intent + the clobber guard, not
mechanism. This module CREATES these entrypoints; wiring them into the live
harness git paths (inbox_consumer's pull, provision_repos, bringup §5) is
T-1998 phase-2, the supervised cutover. Until a pin exists PinnedHashVerifier
fail-closes, so resolve_remote stays inert — the safe default.

Note: the pin is a SHA-256 of a PUBLIC manifest (repos.yaml), not a secret — it
is safe to print. Its trust value is provenance (it came off the authenticated
origin), not confidentiality.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

try:  # package import (tools.manifest_pin) and script run (python3 tools/manifest_pin.py)
    from tools import remote_resolve
except ImportError:  # pragma: no cover - exercised only under direct script invocation
    import remote_resolve


# Exit codes for the CLI. 0 = did the thing; 2 = precondition refusal (bootstrap
# would clobber an existing pin); 1 = fetch/read failure (fail-closed, no pin).
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2


def _worktree_manifest_bytes(repo_root: Path) -> bytes | None:
    """Raw bytes of the working-tree repos.yaml, or None if absent/unreadable.
    This is what PinnedHashVerifier is asked to verify in production."""
    path = repo_root / remote_resolve.MANIFEST_REL
    try:
        return path.read_bytes()
    except OSError:
        return None


def _worktree_verifies(repo_root: Path, pin_path: Path) -> bool:
    """True iff the working-tree repos.yaml verifies against the pin — using the
    exact production verifier (no divergent hashing logic)."""
    raw = _worktree_manifest_bytes(repo_root)
    if raw is None:
        return False
    verifier = remote_resolve.PinnedHashVerifier(pin_path)
    return bool(verifier(repo_root / remote_resolve.MANIFEST_REL, raw))


def _read_pin_value(pin_path: Path) -> str | None:
    """The stored pin string (stripped), or None if absent/unreadable. For
    display + change-detection only; the authoritative validity check is the
    verifier, which re-validates format."""
    try:
        return pin_path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def bootstrap_pin(
    *,
    repo_root: Path | None = None,
    pin_path: Path | None = None,
    ref: str = "origin/main",
    force: bool = False,
    do_fetch: bool = True,
    runner=None,
) -> dict:
    """Establish the FIRST pin (§8.1 bootstrap). Refuses to overwrite an existing
    pin unless `force` (a first pin is a provisioning act; an existing pin should
    move via `refresh`). Always pins bytes fetched off the authenticated origin —
    a fetch/read failure raises ManifestIntegrityError and writes no pin."""
    repo_root = repo_root if repo_root is not None else remote_resolve.default_repo_root()
    pin_path = pin_path if pin_path is not None else remote_resolve.default_pin_path()

    # Clobber guard uses exists() (not is_file()) on purpose: refuse if ANYTHING
    # occupies the path — a file OR a directory — since we cannot safely establish
    # a pin over it without --force. (status() reports pin presence via is_file(),
    # a narrower "is there a valid pin file" question — the two differ by design.)
    if pin_path.exists() and not force:
        return {
            "action": "refused_exists",
            "pin_path": str(pin_path),
            "pin": _read_pin_value(pin_path),
            "reason": "a pin already exists; use `refresh` to update it, or pass --force",
        }

    pin = remote_resolve.establish_pin(
        repo_root=repo_root,
        pin_path=pin_path,
        ref=ref,
        do_fetch=do_fetch,
        runner=runner,
    )
    return {
        "action": "bootstrapped",
        "pin_path": str(pin_path),
        "pin": pin,
        "worktree_matches": _worktree_verifies(repo_root, pin_path),
    }


def refresh_pin(
    *,
    repo_root: Path | None = None,
    pin_path: Path | None = None,
    ref: str = "origin/main",
    do_fetch: bool = True,
    runner=None,
) -> dict:
    """Re-establish the pin from the authenticated origin (the pin leg of a
    verified config pull), so it tracks an AUTHORIZED manifest change. Idempotent
    — re-pinning identical origin bytes rewrites the same hash. `changed` says
    whether the pin moved; `worktree_matches` says whether the working-tree
    repos.yaml now verifies against the new pin (if False, a working-tree pull is
    still pending — that pull is T-1998 phase-2)."""
    repo_root = repo_root if repo_root is not None else remote_resolve.default_repo_root()
    pin_path = pin_path if pin_path is not None else remote_resolve.default_pin_path()

    prior = _read_pin_value(pin_path)
    pin = remote_resolve.establish_pin(
        repo_root=repo_root,
        pin_path=pin_path,
        ref=ref,
        do_fetch=do_fetch,
        runner=runner,
    )
    return {
        "action": "refreshed",
        "pin_path": str(pin_path),
        "pin": pin,
        "prior": prior,
        "changed": prior != pin,
        "worktree_matches": _worktree_verifies(repo_root, pin_path),
    }


def pin_status(
    *,
    repo_root: Path | None = None,
    pin_path: Path | None = None,
) -> dict:
    """Report the pin's presence + whether the working-tree repos.yaml verifies
    against it. `worktree_verifies=False` with a present pin means the working
    tree is stale/tampered relative to the pinned origin bytes (resolve_remote
    would refuse) — the phase-2 readiness signal."""
    repo_root = repo_root if repo_root is not None else remote_resolve.default_repo_root()
    pin_path = pin_path if pin_path is not None else remote_resolve.default_pin_path()

    pin_value = _read_pin_value(pin_path)
    worktree_raw = _worktree_manifest_bytes(repo_root)
    worktree_hash = hashlib.sha256(worktree_raw).hexdigest() if worktree_raw is not None else None
    return {
        "action": "status",
        "pin_path": str(pin_path),
        "pin_present": pin_path.is_file(),
        "pin": pin_value,
        "worktree_present": worktree_raw is not None,
        "worktree_hash": worktree_hash,
        "worktree_verifies": _worktree_verifies(repo_root, pin_path),
    }


def _emit(result: dict) -> None:
    """Human-readable one-liner to stdout (the machine-readable dict is the
    return value; callers/tests assert on that, not on this text)."""
    action = result.get("action")
    if action == "refused_exists":
        print(f"REFUSED: {result['reason']} ({result['pin_path']})")
    elif action == "bootstrapped":
        print(f"bootstrapped pin {result['pin']} -> {result['pin_path']} "
              f"(worktree_matches={result['worktree_matches']})")
    elif action == "refreshed":
        print(f"refreshed pin {result['pin']} -> {result['pin_path']} "
              f"(changed={result['changed']}, worktree_matches={result['worktree_matches']})")
    elif action == "status":
        print(f"pin_present={result['pin_present']} pin={result['pin']} "
              f"worktree_present={result['worktree_present']} "
              f"worktree_verifies={result['worktree_verifies']} ({result['pin_path']})")


def main(argv: list[str] | None = None, *, runner=None) -> int:
    parser = argparse.ArgumentParser(
        prog="manifest_pin",
        description="ORG-046 manifest pin lifecycle (T-3764): bootstrap / refresh / status.",
    )

    def _add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--repo-root", type=Path, default=None,
                        help="pantheon-config clone (default: the clone this tool lives in)")
        sp.add_argument("--pin-path", type=Path, default=None,
                        help="out-of-repo pin store (default: ~/.local/state/pantheon/repos.yaml.pin)")

    sub = parser.add_subparsers(dest="cmd", required=True)

    # NB: the CLI never exposes a "skip fetch" path — every pin is derived from a
    # FRESH authenticated fetch (§8.1 witness (c)). --ref should name an
    # authenticated remote-tracking ref (origin/*); pinning a bare local branch
    # would defeat the authenticated-provenance guarantee. (The do_fetch=False
    # seam stays on establish_pin for T-3723's own hermetic tests, not operators.)
    b = sub.add_parser("bootstrap", help="establish the FIRST pin at provisioning (§8.1)")
    _add_common(b)
    b.add_argument("--ref", default="origin/main",
                   help="authenticated remote-tracking ref to pin (default origin/main)")
    b.add_argument("--force", action="store_true", help="overwrite an existing pin (else refuse)")

    r = sub.add_parser("refresh", help="re-pin from the authenticated origin after an authorized change")
    _add_common(r)
    r.add_argument("--ref", default="origin/main",
                   help="authenticated remote-tracking ref to pin (default origin/main)")

    s = sub.add_parser("status", help="report pin presence + working-tree verification")
    _add_common(s)

    args = parser.parse_args(argv)

    try:
        if args.cmd == "bootstrap":
            result = bootstrap_pin(repo_root=args.repo_root, pin_path=args.pin_path,
                                   ref=args.ref, force=args.force, runner=runner)
        elif args.cmd == "refresh":
            result = refresh_pin(repo_root=args.repo_root, pin_path=args.pin_path,
                                 ref=args.ref, runner=runner)
        else:  # status
            result = pin_status(repo_root=args.repo_root, pin_path=args.pin_path)
    except remote_resolve.ManifestIntegrityError as e:
        print(f"ERROR (fail-closed, no pin written): {e}", file=sys.stderr)
        return EXIT_ERROR

    _emit(result)
    if result.get("action") == "refused_exists":
        return EXIT_REFUSED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
