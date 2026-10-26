"""T-3339 Phase 1 -- persona-orientation freshness detector.

Reads rulebook/persona-orientation/source-bindings.yaml (the renderer's
write-only stamp record) and, for every recorded source path, compares the
stamped `git_blob` to the blob currently at that path on HEAD. Any mismatch
means the source changed since the last `render.py` invocation and the
rendered artifacts may now be stale -- a ratified rulebook/SOP change should
always be followed by a manual re-render (RATIFY-POINT-2: manual trigger,
not automated, to avoid a merge-race rendering inconsistent orientation).

Also covers the SOP-skill stamps (T-4465): tools/persona_orientation/
sop-skill-bindings.yaml, written by render.py from sop-skills.yaml, in the same
record format. check_sop_skills() is the SOP-skills-only slice that the CI test
(tools/test_persona_orientation_sop_skills.py) gates on.

Scope: REPO-level only. The stamps are committed, so once any one seat
re-renders and pushes, every other clone at that HEAD reads FRESH here even
if its own ~/.claude/skills/pantheon-* artifacts were never re-rendered
(T-3997). Per-seat artifact freshness is `render.py --check`, run as that
seat's user.

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


# Kept equal to render.SOP_SKILLS_MANIFEST / render.SOP_SKILL_BINDINGS (a test pins
# the pair); duplicated because this file also runs as a script, where importing
# render through the tools package is not available.
SOP_SKILLS_MANIFEST = "tools/persona_orientation/sop-skills.yaml"
SOP_SKILL_BINDINGS = "tools/persona_orientation/sop-skill-bindings.yaml"


def _drift(root: Path, bindings: dict) -> list[dict]:
    drifted = []
    for rel_path, stamp in bindings.items():
        blob_now = current_blob(root, rel_path)
        stamped_blob = stamp.get("git_blob")
        if blob_now is None:
            drifted.append({"path": rel_path, "reason": "source file no longer resolves at HEAD"})
        elif blob_now != stamped_blob:
            drifted.append({"path": rel_path, "reason": "blob changed", "stamped": stamped_blob, "current": blob_now})
    return drifted


def _render_module():
    try:
        from tools.persona_orientation import render
    except ImportError:  # run as a script: render.py is a sibling
        import render
    return render


def check_orientation_stamps(root: Path) -> list[dict]:
    """T-4743: drift entries where the stamp set and the manifest disagree, mirroring
    check_sop_skills' no-stamp / retired-entry checks. The expected set is exactly what
    render.py would stamp (every non-inline source the manifest + role-deltas pull in),
    so a section added to the manifest but never rendered fails closed here. No manifest
    in the repo -> nothing to check."""
    if not (root / "rulebook" / "persona-orientation" / "manifest.yaml").exists():
        return []
    bindings = load_bindings(root)
    if bindings is None:
        return []
    try:
        declared = set(_render_module().render_all(root)[1])
    except (OSError, KeyError, ValueError, subprocess.CalledProcessError) as exc:
        return [{"path": "rulebook/persona-orientation/manifest.yaml",
                 "reason": f"manifest does not render: {type(exc).__name__}: {exc}"}]
    drifted = []
    for rel_path in sorted(declared - set(bindings)):
        drifted.append({"path": rel_path, "reason": "no stamp recorded for this manifest entry (run render.py)"})
    for rel_path in sorted(set(bindings) - declared):
        drifted.append({"path": rel_path, "reason": "stamp for a source the manifest no longer names (run render.py)"})
    return drifted


def check_sop_skills(root: Path) -> dict:
    """Freshness of the SOP-skill stamps (T-4465) alone: {"status": "fresh"|"stale"|
    "missing-bindings"|"not-configured", "drifted": [...], "checked": N}. Beyond
    the blob comparison it fails closed on a manifest entry with no stamp (added,
    never rendered) and on a stamp whose SOP the manifest no longer names."""
    manifest_path = root / SOP_SKILLS_MANIFEST
    if not manifest_path.exists():
        return {"status": "not-configured", "drifted": [], "checked": 0}
    bindings_path = root / SOP_SKILL_BINDINGS
    if not bindings_path.exists():
        return {"status": "missing-bindings", "drifted": [], "checked": 0}
    with open(bindings_path, encoding="utf-8") as f:
        bindings = yaml.safe_load(f) or {}
    with open(manifest_path, encoding="utf-8") as f:
        declared = {e["sop"] for e in (yaml.safe_load(f) or {}).get("skills") or []}

    drifted = _drift(root, bindings)
    for rel_path in sorted(declared - set(bindings)):
        drifted.append({"path": rel_path, "reason": "no stamp recorded for this manifest entry (run render.py)"})
    for rel_path in sorted(set(bindings) - declared):
        drifted.append({"path": rel_path, "reason": "stamp for an SOP the manifest no longer names (run render.py)"})
    return {"status": "stale" if drifted else "fresh", "drifted": drifted, "checked": len(bindings)}


def check(root: Path) -> dict:
    """Returns a report dict: {"status": "fresh"|"stale"|"missing-bindings",
    "drifted": [...], "checked": N}. Covers the persona-orientation stamps and,
    when the repo has an SOP-skills manifest, the SOP-skill stamps."""
    bindings = load_bindings(root)
    if bindings is None:
        return {"status": "missing-bindings", "drifted": [], "checked": 0}

    drifted = _drift(root, bindings) + check_orientation_stamps(root)
    checked = len(bindings)

    sop = check_sop_skills(root)
    if sop["status"] == "missing-bindings":
        return {"status": "missing-bindings", "drifted": [], "checked": 0}
    drifted += sop["drifted"]
    checked += sop["checked"]

    status = "stale" if drifted else "fresh"
    return {"status": status, "drifted": drifted, "checked": checked}


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
            print("No source-bindings.yaml (or sop-skill-bindings.yaml, when an SOP-skills manifest exists) "
                  "found -- run `render.py` at least once first.", file=sys.stderr)
        elif report["status"] == "fresh":
            print(f"OK -- {report['checked']} source binding(s) fresh.")
            print("(repo-level stamps only; per-seat artifacts: render.py --check)")
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
