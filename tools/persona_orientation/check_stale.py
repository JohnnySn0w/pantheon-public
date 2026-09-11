"""T-3339 Phase 1 -- persona-orientation freshness detector.

Reads rulebook/persona-orientation/source-bindings.yaml (the renderer's
write-only stamp record) and, for every recorded source path, compares the
stamped `git_blob` to the blob currently at that path on HEAD. Any mismatch
means the source changed since the last `render.py` invocation and the
rendered artifacts may now be stale -- a ratified rulebook/SOP change should
always be followed by a manual re-render (RATIFY-POINT-2: manual trigger,
not automated, to avoid a merge-race rendering inconsistent orientation).

Exit codes:
    0 -- all sources fresh (or nothing to check)
    2 -- at least one source has drifted, or source-bindings.yaml is missing

CLI:
    check_stale.py            human-readable report
    check_stale.py --json     machine-readable report on stdout
    check_stale.py --threshold N   reserved for future automation (currently
                                    any drift beyond N sources exits 2; default
                                    N=0, i.e. any drift at all)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def load_bindings(root: Path) -> dict | None:
    path = root / "rulebook" / "persona-orientation" / "source-bindings.yaml"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def current_blob(root: Path, rel_path: str) -> str | None:
    try:
        return _git(root, "rev-parse", f"HEAD:{rel_path}")
    except subprocess.CalledProcessError:
        return None


def check(root: Path) -> dict:
    """Returns a report dict: {"status": "fresh"|"stale"|"missing-bindings",
    "drifted": [...], "checked": N}."""
    bindings = load_bindings(root)
    if bindings is None:
        return {"status": "missing-bindings", "drifted": [], "checked": 0}

    drifted = []
    for rel_path, stamp in bindings.items():
        blob_now = current_blob(root, rel_path)
        stamped_blob = stamp.get("git_blob")
        if blob_now is None:
            drifted.append({"path": rel_path, "reason": "source file no longer resolves at HEAD"})
        elif blob_now != stamped_blob:
            drifted.append({"path": rel_path, "reason": "blob changed", "stamped": stamped_blob, "current": blob_now})

    status = "stale" if drifted else "fresh"
    return {"status": status, "drifted": drifted, "checked": len(bindings)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--threshold", type=int, default=0, help="drift count above which exit 2 fires (default 0: any drift)")
    parser.add_argument("--repo-root", type=Path, default=None, help="override repo root (tests only; defaults to this file's repo)")
    args = parser.parse_args(argv)

    root = args.repo_root if args.repo_root is not None else repo_root()
    report = check(root)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        if report["status"] == "missing-bindings":
            print("No source-bindings.yaml found -- run `render.py` at least once first.", file=sys.stderr)
        elif report["status"] == "fresh":
            print(f"OK -- {report['checked']} source binding(s) fresh.")
        else:
            print(f"STALE -- {len(report['drifted'])} of {report['checked']} source binding(s) drifted:", file=sys.stderr)
            for item in report["drifted"]:
                print(f"  {item['path']}: {item['reason']}", file=sys.stderr)

    if report["status"] == "missing-bindings":
        return 2
    if len(report["drifted"]) > args.threshold:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
