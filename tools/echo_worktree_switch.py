#!/usr/bin/env python3
"""echo_worktree_switch.py — stop a shared cargo target from handing one echo
worktree another worktree's build of a workspace crate (T-5152).

THE FAILURE. Every echo-prismatic worktree builds into the shared
/var/lib/pantheon/echo-target. Cargo hashes a path (workspace) package by its
path RELATIVE to the workspace root, so /tmp/echo-T-5119 and /tmp/echo-T-4797
get the SAME unit hashes for echo-core, and freshness is decided by mtime: a
worktree whose sources are OLDER than the last build of that unit is judged
fresh and silently gets the other branch's artifact. Seen twice on 2026-10-18
(clippy errors on fields that exist only on another branch); touching the
crate roots fixed it.

THE FIX. The target remembers which workspace built last
(<target>/.echo-last-workspace). When a different workspace builds, this
touches the source root of every workspace target (lib.rs, main.rs, tests,
benches, examples; never build.rs), so cargo rebuilds this worktree's own
crates from its own source. Registry dependencies are untouched and stay
shared. The cost is a rebuild of the workspace crates on each switch, which
correctness needs anyway; the same worktree building again pays nothing.

LIMITS. Only builds that go through tools/echo-cargo.sh update the record. A
bare `cargo` from another worktree leaves it stale, so the next wrapped build
from the recorded worktree will not touch (run this with --force, or touch the
crate roots by hand). Fail-open: any error is reported and exit is 0, because
this runs in front of a build and must never block it.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

MARKER = ".echo-last-workspace"


def workspace_sources(metadata: dict) -> list[Path]:
    """Source roots of every non-build-script target of every workspace member."""
    members = set(metadata.get("workspace_members", []))
    root = Path(metadata["workspace_root"])
    out: list[Path] = []
    for pkg in metadata.get("packages", []):
        if pkg.get("id") not in members:
            continue
        for t in pkg.get("targets", []):
            if "custom-build" in t.get("kind", []):
                continue  # touching build.rs would re-run the build script
            src = Path(t["src_path"])
            if src.is_relative_to(root):
                out.append(src)
    return sorted(set(out))


def read_metadata(cargo: str, cwd: Path) -> dict:
    r = subprocess.run([cargo, "metadata", "--no-deps", "--format-version", "1"],
                       cwd=cwd, capture_output=True, text=True, check=True, timeout=120)
    return json.loads(r.stdout)


def switch(target: Path, metadata: dict, *, force: bool = False) -> tuple[str, int]:
    """(previous workspace or '', files touched). Touches only on a change of workspace."""
    root = os.path.realpath(metadata["workspace_root"])
    marker = target / MARKER
    try:
        prev = marker.read_text().strip()
    except OSError:
        prev = ""
    touched = 0
    if force or prev != root:
        for src in workspace_sources(metadata):
            try:
                os.utime(src, None)
                touched += 1
            except OSError as exc:
                print(f"[echo_worktree_switch] cannot touch {src}: {exc}", file=sys.stderr)
        tmp = target / f"{MARKER}.{os.getpid()}"
        tmp.write_text(root + "\n")
        os.replace(tmp, marker)
    return prev, touched


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--target", type=Path, required=True, help="the shared cargo target dir")
    p.add_argument("--cargo", default="cargo", help="cargo binary used for `cargo metadata`")
    p.add_argument("--force", action="store_true", help="touch even if this workspace built last")
    a = p.parse_args(argv)
    try:
        meta = read_metadata(a.cargo, Path.cwd())
        prev, touched = switch(a.target, meta, force=a.force)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"[echo_worktree_switch] skipped: {exc}", file=sys.stderr)
        return 0
    if touched:
        print(f"[echo_worktree_switch] workspace changed ({prev or 'none recorded'} -> "
              f"{meta['workspace_root']}): touched {touched} crate root(s) so cargo rebuilds "
              f"this worktree's own crates", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
