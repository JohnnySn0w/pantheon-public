"""Verify a built MCP adapter artifact under a ROOT (T-2915/T-2916, gate 7).

``--adapter`` selects the build profile (``host`` default, ``approval``);
both profiles are verified against the one shared ``requirements.lock``.

Checks, each reported by name:

- ``dist_set``: the venv's installed distributions are exactly the
  ``requirements.lock`` set (names and versions), nothing more;
- ``dist_metadata``: every ``*.dist-info`` in the venv has a readable
  ``METADATA`` (T-4858: one without it was a traceback, now a named
  failure; such a dist cannot be counted toward ``dist_set``);
- ``manifest``: every file/symlink re-hashes to BUILD-MANIFEST.json and
  nothing is missing or extra;
- ``modes``: no file or directory is group- or other-writable;
- ``owner`` (only with ``--expect-owner``): every path is owned by that
  uid — fixture roots cannot prove root ownership; T-2920's install
  receipt runs this with ``--expect-owner 0``;
- ``isolated_import``: the launcher's ``--self-check --no-bus`` runs with
  a hostile environment (PYTHONPATH and a user site both carrying a fake
  ``mcp``) and still reports the pinned runtime ``ready`` — the runtime
  imports without any per-seat installation and ignores seat-controlled
  import paths.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_runtime  # noqa: E402

_LOCK_LINE = re.compile(r"^([A-Za-z0-9._-]+)==([^\s\\]+)")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_set() -> set[tuple[str, str]]:
    pins = set()
    for line in build_runtime.REQUIREMENTS_LOCK.read_text(encoding="utf-8").splitlines():
        match = _LOCK_LINE.match(line)
        if match:
            pins.add((_normalize(match.group(1)), match.group(2)))
    return pins


_HOST = build_runtime.HOST_PROFILE


def _dist_infos(root: Path, profile: build_runtime.AdapterProfile) -> list[Path]:
    return sorted((root / profile.prefix / "venv").glob("lib/python*/site-packages/*.dist-info"))


def unreadable_metadata(root: Path, profile: build_runtime.AdapterProfile = _HOST) -> list[str]:
    """The dist-info dirs (relative to root) whose METADATA is missing or unreadable."""
    bad = []
    for dist_info in _dist_infos(root, profile):
        try:
            (dist_info / "METADATA").read_text(encoding="utf-8", errors="replace")
        except OSError:
            bad.append(dist_info.relative_to(root).as_posix())
    return bad


def installed_set(root: Path, profile: build_runtime.AdapterProfile = _HOST) -> set[tuple[str, str]]:
    found = set()
    for dist_info in _dist_infos(root, profile):
        try:
            metadata = (dist_info / "METADATA").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # reported by the dist_metadata check, not a traceback
        name = re.search(r"^Name: (.+)$", metadata, re.M)
        version = re.search(r"^Version: (.+)$", metadata, re.M)
        if name and version:
            found.add((_normalize(name.group(1).strip()), version.group(1).strip()))
    return found


def check_manifest(root: Path, profile: build_runtime.AdapterProfile = _HOST) -> list[str]:
    manifest_path = root / profile.prefix / build_runtime.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recorded = manifest.get("files", {})
    actual = build_runtime.manifest_entries(root, profile)
    problems = []
    for rel in sorted(set(recorded) | set(actual)):
        if recorded.get(rel) != actual.get(rel):
            problems.append(rel)
    return problems


def check_modes(root: Path, profile: build_runtime.AdapterProfile = _HOST) -> list[str]:
    bad = []
    for base in (root / profile.prefix, root / profile.launcher):
        for path in [base, *(base.rglob("*") if base.is_dir() else [])]:
            if path.is_symlink():
                continue
            if path.stat().st_mode & 0o022:
                bad.append(path.relative_to(root).as_posix())
    return bad


def check_owner(root: Path, uid: int, profile: build_runtime.AdapterProfile = _HOST) -> list[str]:
    bad = []
    for base in (root / profile.prefix, root / profile.launcher):
        for path in [base, *(base.rglob("*") if base.is_dir() else [])]:
            if os.lstat(path).st_uid != uid:
                bad.append(path.relative_to(root).as_posix())
    return bad


def check_isolated_import(root: Path, profile: build_runtime.AdapterProfile = _HOST) -> tuple[bool, str]:
    venv_python = root / profile.prefix / "venv" / "bin" / "python3"
    launcher = root / profile.launcher
    with tempfile.TemporaryDirectory() as poison:
        poison_path = Path(poison)
        for site in (
            poison_path / "pythonpath",
            poison_path / "home" / ".local" / "lib" / "python3.12" / "site-packages",
        ):
            fake = site / "mcp"
            fake.mkdir(parents=True)
            (fake / "__init__.py").write_text("raise ImportError('poisoned mcp imported')\n", encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(poison_path / "home"),
            "PYTHONPATH": str(poison_path / "pythonpath"),
            "PYTHONUSERBASE": str(poison_path / "home" / ".local"),
        }
        result = subprocess.run(
            [str(venv_python), "-I", str(launcher), "--self-check", "--no-bus"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=poison,
        )
    if result.returncode != 0:
        return False, (result.stderr or result.stdout)[-500:]
    try:
        report = json.loads(result.stdout)
    except ValueError:
        return False, "self-check output is not JSON"
    if report.get("Subcomponents", {}).get("runtime") != "ready":
        return False, "runtime not ready: " + json.dumps(report.get("Reasons", {}))
    return True, report["Revisions"]["runtime"]


def verify(
    root: Path,
    expect_owner: int | None = None,
    profile: build_runtime.AdapterProfile = _HOST,
) -> dict:
    root = root.resolve()
    checks: dict[str, dict] = {}
    expected, installed = lock_set(), installed_set(root, profile)
    checks["dist_set"] = {
        "ok": expected == installed and bool(expected),
        "missing": sorted(f"{n}=={v}" for n, v in expected - installed),
        "extra": sorted(f"{n}=={v}" for n, v in installed - expected),
        "count": len(installed),
    }
    no_metadata = unreadable_metadata(root, profile)
    checks["dist_metadata"] = {"ok": not no_metadata, "missing_metadata": no_metadata[:20]}
    # Run the launcher FIRST so the manifest check below also proves the
    # runtime wrote nothing into its own tree while running.
    ok, detail = check_isolated_import(root, profile)
    checks["isolated_import"] = {"ok": ok, "detail": detail}
    problems = check_manifest(root, profile)
    checks["manifest"] = {"ok": not problems, "mismatched": problems[:20]}
    bad_modes = check_modes(root, profile)
    checks["modes"] = {"ok": not bad_modes, "writable": bad_modes[:20]}
    if expect_owner is not None:
        bad_owner = check_owner(root, expect_owner, profile)
        checks["owner"] = {"ok": not bad_owner, "wrong_owner": bad_owner[:20]}
    return {"ok": all(check["ok"] for check in checks.values()), "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--expect-owner", type=int, default=None)
    parser.add_argument("--adapter", choices=sorted(build_runtime.PROFILES), default=_HOST.name)
    args = parser.parse_args(argv)
    report = verify(args.root, args.expect_owner, build_runtime.PROFILES[args.adapter])
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
