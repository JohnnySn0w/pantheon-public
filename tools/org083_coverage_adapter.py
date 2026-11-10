#!/usr/bin/env python3
"""org083_coverage_adapter.py -- pantheon-config's ORG-083 Python coverage adapter (T-5971).

    python3 tools/org083_coverage_adapter.py [--rev REV] [--workers N] [--out-dir DIR] [--venv DIR]
                                             [--baseline-run PRIOR_OUT]

Adapter id `pantheon-config.coveragepy-ctrace-arc.v1`: coverage.py 7.17.0, C tracer, branch
(arc) measurement, `patch = subprocess`, absolute paths, run directly as
`python -m coverage run -m pytest tools/` inside a dedicated venv.  It implements the build
contract of knowledge/episodic/2026-11-01-t5825-pantheon-config-org083-adapter-proposal/
proposal.md section 12 (ADAPTER-PC-BUILD) and nothing else: it collects evidence, writes
candidate record bodies under a fresh <OUT>, and claims no baseline, threshold, ratchet or
maturity.  pantheon-config stays inventory_only.

HARD BOUNDARIES.
  * coverage.py lives ONLY in the adapter venv.  This module never imports it; it drives it as
    `<VENV>/bin/python -m coverage ...` subprocesses and reads its data files with stdlib
    sqlite3 (schema version asserted, refusal R7a on an unknown one).
  * The adapter writes only into a fresh <OUT> outside the repository, measures a fresh detached
    worktree, and removes that worktree in `finally` (RA-5, RA-6).
  * Exit codes: 0 admissible, 1 refused (refusal.json lists every fired predicate), 2 setup error.

SECTIONS: 1 constants - 2 canonical forms and fingerprints - 3 static map, resolved map,
comparator - 4 parsers - 5 predicate evaluators (R1..R17) - 6 environment, venv, worktree,
measured run - 7 artifacts - 8 pipeline and CLI.
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import gzip
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import soak_leg  # noqa: E402
from tools.check_suite_outcome_floor import InputError, count  # noqa: E402
from tools.repository_evidence import COMPARABILITY_CHECKED_FIELDS  # noqa: E402
from tools.soak_leg import SetupError  # noqa: E402

Runner = Callable[..., "subprocess.CompletedProcess"]

# ===========================================================================
# 1. Constants (proposal sections 1, 4, 5, 6, 7).
# ===========================================================================

ADAPTER_ID = "pantheon-config.coveragepy-ctrace-arc.v1"
TOOL_NAME = "coverage.py"
TOOL_VERSION = "7.17.0"
# Per-interpreter binary wheels (proposal section 1).  The pure-Python wheel is forbidden.
COVERAGE_WHEELS = {
    "cp311": "8e8113d3852e9377421a61de1917df27b3abb25404a4ecf69f9e64612c4345b5",
    "cp312": "85932227e9dd38e62cd29c17fefdfecfb919eead6af1e3259522d03cf85de983",
}
SDIST_SHA256 = "cc010a0a7fbdd9bb8bdfd0d3f04a5151275860213cbd7e2b876b29778489c712"  # provenance only

ASSETS_DIR = Path(__file__).resolve().parent / "org083_coverage_adapter_assets"
CONFIG_NAME = "coveragerc"
LOCK_NAME = "requirements.lock"
STATIC_MAP_NAME = "semantic-map.pantheon-config.coveragepy-ctrace-arc.v1.json"
# Pinned digests: editing the committed config or the static map without a new adapter id is a
# refusal (R16a / R17a), not a silent series change.
CONFIG_SHA256 = "04935e06b932c08942c32dd2ade5b08bb81ee6f04f7fdb7a1ce6ec1cedd57a64"
STATIC_MAP_SHA256 = "03a4246076bd4d9242fdf8525e22bd94a577c1a357a22471c6ad52d194754097"

INCLUDE_SELECTORS = ("tools",)
GENERATED_SOURCE_IDS = ("generated-none",)
# Proposal section 4, anchored with a leading */ (unanchored patterns are cwd-relative).
OMIT_TEST_CODE = ("*/tools/test_*.py", "*/tools/**/test_*.py", "*/tools/**/tests/*",
                  "*/tools/conftest.py", "*/tools/**/conftest.py")
OMIT_TEST_DATA = ("*/tools/fixtures/*", "*/tools/testdata/*")
OMIT_PATTERNS = OMIT_TEST_CODE + OMIT_TEST_DATA
# Tracked in-scope files that coverage.py source discovery cannot see even with
# `[report] include_namespace_packages = True` (Q3 outcome: with the option set in [report] the
# F4 files ARE discovered, so at the build revision this list is EMPTY; names containing any of
# `.#~!$@%^&*()+=,` before the extension would be undiscoverable).  The e2e honesty test
# regenerates it from a no-op measurement; R10b/c/d keep it honest at run time.  Editing it
# changes scope_fingerprint (a reset, by design).
UNDISCOVERABLE_PATHS: tuple[str, ...] = ()
DISCOVERY_RULE = "coveragepy-7.17.0 source-dir discovery; relative_files=false; include_namespace_packages=true"
UNDISCOVERABLE_REASON = ("coverage.py source discovery cannot see unexecuted non-importable files "
                         "(hyphenated names, non-identifier directories, and files in directories "
                         "without __init__.py), so they enter the denominator only if some "
                         "process executes them; the tool-native denominator is kept")
UNDISCOVERABLE_RE_EVAL = ("re-evaluate when coverage.py discovery changes, when a listed file is "
                          "renamed to an importable name, or at the next adapter id bump")


def exclusion_records(undiscoverable: Sequence[str] | None = None) -> list[dict]:
    """The ExclusionRecord declarations (proposal section 4): none is a silent omit."""
    paths = list(UNDISCOVERABLE_PATHS if undiscoverable is None else undiscoverable)
    return [
        {"exclusion_id": "excl-test-code", "patterns": list(OMIT_TEST_CODE),
         "reason": "Test code is the instrument, not the measured product; including it would "
                   "inflate both numerator and denominator."},
        {"exclusion_id": "excl-test-data", "patterns": list(OMIT_TEST_DATA),
         "reason": "Fixture inputs; the patterns keep a future fixture .py out of the "
                   "denominator unrecorded."},
        {"exclusion_id": "excl-tool-undiscoverable", "patterns": [], "paths": sorted(paths),
         "reason": UNDISCOVERABLE_REASON, "re_evaluation_trigger": UNDISCOVERABLE_RE_EVAL},
    ]


EXCLUSION_IDS = ("excl-test-code", "excl-test-data", "excl-tool-undiscoverable")

# Environment (proposal section 6, reconciled with section 12 item 3: soak_leg's stripping is
# reused, so the unset set is the UNION of section 6's seven names, soak_leg.STRIPPED_ENV and
# the git repo-redirect variables asked of git at run time).
ENV_SET = {"PANTHEON_DISABLE_LIVE_TELEGRAM": "1", "CI": "true"}
SECTION6_UNSET = ("PYTEST_ADDOPTS", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV",
                  "COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG", "COVERAGE_RCFILE")
REDIRECT_RULE = "git-redirect-vars: `git rev-parse --local-env-vars` plus GIT_NAMESPACE (soak_leg.repo_redirect_env)"
NARROWING_FLAGS = frozenset({"-k", "-m", "--deselect", "--ignore", "--ignore-glob", "--lf",
                             "--last-failed", "--sw", "--stepwise", "--collect-only", "--co"})
DEFAULT_PATHS = ("tools/",)
DEFAULT_WORKERS = 4
DEFAULT_TIMEOUT = 4 * 3600
DEFAULT_VENV = Path.home() / ".cache" / "pantheon" / "org083-adapter-venv"
DEFAULT_OUT_ROOT = Path.home() / ".cache" / "pantheon" / "org083-adapter"
ADAPTER_MARKER = ".org083-adapter"
START_SLACK_SECONDS = 2.0
SUPPORTED_DATA_SCHEMA = 7  # coverage 7.17.0's SQLite data-file schema
EXIT_OK, EXIT_REFUSED, EXIT_SETUP = 0, 1, 2

MANIFEST_SCHEMA = "org083.adapter-run-manifest.v1"
INPUTS_SCHEMA = "org083.adapter-measurement-inputs.v1"
DIMENSIONS = {"line": ("statement", "covered_lines", "num_statements"),
              "branch": ("branch_destination", "covered_branches", "num_branches")}


@dataclass(frozen=True)
class Fire:
    """One fired refusal predicate (section 10a id), its reason and the triggering evidence."""
    id: str
    reason: str
    evidence: str = ""


class Refusal(Exception):
    """The run must be refused: carries every fired predicate known at that point."""

    def __init__(self, fires: Sequence[Fire]):
        super().__init__("; ".join(f"{f.id}: {f.reason}" for f in fires))
        self.fires = list(fires)


class Interrupted(Exception):
    """SIGTERM arrived mid-run (R15a)."""


@dataclass
class CoveragePin:
    version: str = TOOL_VERSION
    wheels: Mapping[str, str] = field(default_factory=lambda: dict(COVERAGE_WHEELS))


@dataclass
class Seams:
    """Internal-entry test seams.  Keyword-only on `run`; NOT reachable from the CLI argv."""
    assets_dir: Path | None = None
    pinned_config_sha: str | None = None
    pinned_map_sha: str | None = None
    coverage_pin: CoveragePin | None = None
    undiscoverable_paths: Sequence[str] | None = None
    disabled: frozenset[str] = frozenset()  # predicate ids / sub-ids whose evaluation is skipped
    pre_start: Callable[[Path, Path], None] | None = None  # (data_dir, out_dir) before the R14a check
    now: Callable[[], datetime] | None = None
    run_nonce: str | None = None  # per-run id uniqueness component; None = a real random value
    runner_label: str | None = None

    def off(self, ident: str) -> bool:
        return ident in self.disabled


# ===========================================================================
# 2. Canonical forms and fingerprints (proposal sections 7, 7b).
# ===========================================================================


def _nfc(obj: Any) -> Any:
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, Mapping):
        return {_nfc(k): _nfc(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_nfc(v) for v in obj]
    if isinstance(obj, float):
        raise ValueError("floats never enter a fingerprint")
    return obj


def canonical_bytes(obj: Any) -> bytes:
    """json.dumps(sort_keys, minimal separators, ensure_ascii False) of the NFC-normalized object."""
    return json.dumps(_nfc(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def has_control_char(text: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" for ch in text)


def normalize_set(items: Iterable[str]) -> list[str]:
    """A sorted list of unique NFC strings.  Duplicates are an error, not silently merged."""
    seen: list[str] = []
    for item in items:
        seen.append(unicodedata.normalize("NFC", item))
    if len(set(seen)) != len(seen):
        dup = sorted({s for s in seen if seen.count(s) > 1})
        raise Refusal([Fire("R10e", f"duplicate entries in a canonical set: {dup[:5]}", "scope builder input")])
    return sorted(seen)


def placeholderize(text: str, mapping: Mapping[str, str]) -> str:
    """Replace machine roots with <ROOT>/<OUT>/<VENV>/<CFG> placeholders, longest path first."""
    for path in sorted(mapping, key=len, reverse=True):
        if path:
            text = text.replace(path, mapping[path])
    return text


def glob_to_regex(pattern: str) -> str:
    """Port of coverage.py's file-glob tokenizer (coverage/files.py), so the in-scope path set
    is computed with the SAME omit semantics without importing coverage."""
    pattern = pattern.replace("\\", "/")
    if "/" not in pattern:
        pattern = f"**/{pattern}"
    tokens = [
        (r"\*\*\*+", None), (r"[^/]+\*\*+", None), (r"\*\*+[^/]+", None), (r"\*\*/\*\*", None),
        (r"^\*+/", r"(.*[/\\\\])?"), (r"/\*+$", r"[/\\\\].*"), (r"\*\*/", r"(.*[/\\\\])?"),
        (r"/", r"[/\\\\]"), (r"\*", r"[^/\\\\]*"), (r"\?", r"[^/\\\\]"),
        (r"\[.*?\]", r"\g<0>"), (r"[a-zA-Z0-9_-]+", r"\g<0>"), (r"[\[\]]", None),
        (r"[\s\S]", r"\\\g<0>"),
    ]
    compiled = [(re.compile(rx), sub) for rx, sub in tokens]
    out: list[str] = []
    pos = 0
    while pos < len(pattern):
        for rx, sub in compiled:
            m = rx.match(pattern, pos=pos)
            if m:
                if sub is None:
                    raise ValueError(f"file pattern can't include {m[0]!r}")
                out.append(m.expand(sub))
                pos = m.end()
                break
    return "".join(out)


def omit_matcher(patterns: Sequence[str]) -> Callable[[str], bool]:
    rx = re.compile("(?:" + "|".join(glob_to_regex(p) for p in patterns) + r")\Z")
    return lambda path: rx.match(path) is not None


def command_fingerprint(argv: Sequence[str], *, mapping: Mapping[str, str], env_set: Mapping[str, str],
                        env_unset: Iterable[str], python3_identity: str, workers: int) -> str:
    """Apparatus identity of the measured command (section 7): the placeholderized argv, the env
    allowlist values, the exact sorted union of unset NAMES plus the redirect rule name, the
    PATH-resolved python3 identity and -n.  No node-id set, no host path."""
    return digest({
        "argv": [placeholderize(a, mapping) for a in argv],
        "env_set": dict(sorted(env_set.items())),
        "env_unset_names": normalize_set(env_unset),
        "env_unset_rule": REDIRECT_RULE,
        "python3": placeholderize(python3_identity, mapping),
        "workers": int(workers),
    })


def config_digest(config_sha256: str, ci_pins: Iterable[str], pin: CoveragePin | None = None) -> str:
    """Static inputs only: config bytes digest plus the exact pins.  No resolved map, no tool output."""
    pin = pin or CoveragePin()
    return digest({
        "coverage_config_bytes_sha256": config_sha256,
        "pins": {"coverage": {"version": pin.version, "wheel_sha256": dict(sorted(pin.wheels.items()))},
                 "ci": normalize_set(p.lower() for p in ci_pins)},
    })


def runner_fingerprint(*, implementation: str, python_version: str, platform_text: str, glibc: str,
                       label: str, cpus: int, core: str, ctracer: bool, freeze_digest: str) -> str:
    return digest({"implementation": implementation, "python": python_version, "platform": platform_text,
                   "glibc": glibc, "runner": label, "cpus": int(cpus), "core": core, "ctracer": bool(ctracer),
                   "pip_freeze_sha256": freeze_digest})


def pip_freeze_digest(freeze_text: str) -> str:
    lines = sorted({ln.strip().lower() for ln in freeze_text.splitlines() if "==" in ln})
    return sha256_bytes("\n".join(lines).encode("utf-8"))


def build_in_scope(tracked: Iterable[str], root: Path, *, omit: Sequence[str] = OMIT_PATTERNS) -> tuple[list[str], list[Fire]]:
    """The sorted repo-relative POSIX in-scope .py set (tracked tools/ files matching no omit
    pattern) and the predicates fired while building it: a duplicate (R10e), a control
    character (R9d), a symlink alias or out-of-root target (R9c / R9a).  The realpath is used and
    an alias is not double-counted."""
    fires: list[Fire] = []
    matcher = omit_matcher(omit)
    entries = [unicodedata.normalize("NFC", t) for t in tracked]
    if len(set(entries)) != len(entries):
        fires.append(Fire("R10e", "the in-scope path set contains a duplicate entry",
                          str(sorted({e for e in entries if entries.count(e) > 1})[:5])))
    real_root = os.path.realpath(root)
    selected: dict[str, str] = {}
    for rel in sorted(set(entries)):
        if has_control_char(rel):
            fires.append(Fire("R9d", "a tracked path contains a control character", repr(rel)))
            continue
        if not rel.startswith("tools/") or not rel.endswith(".py"):
            continue
        absolute = os.path.join(real_root, rel)
        if matcher(absolute):
            continue
        real = os.path.realpath(absolute)
        if real != os.path.normpath(absolute):
            fires.append(Fire("R9c", "a tracked in-scope path is a symlink alias", rel))
        if not real.startswith(real_root + os.sep):
            fires.append(Fire("R9a", "a tracked in-scope path resolves outside the root", rel))
            continue
        canon = Path(real).relative_to(real_root).as_posix()
        if not canon.startswith("tools/") or matcher(os.path.join(real_root, canon)):
            continue
        selected.setdefault(canon, rel)
    return sorted(selected), fires


def scope_fingerprint(*, in_scope_paths: Iterable[str], unreported_paths: Iterable[str],
                      exclusion_regexes: Iterable[str], discovery_rule: str = DISCOVERY_RULE,
                      omit_patterns: Sequence[str] = OMIT_PATTERNS,
                      exclusion_ids: Sequence[str] = EXCLUSION_IDS,
                      include_selectors: Sequence[str] = INCLUDE_SELECTORS) -> str:
    """Section 7: the rules and the path SET only.  No blob shas, contents or pragma inventory."""
    paths = list(in_scope_paths)
    for p in paths:
        if has_control_char(p):
            raise Refusal([Fire("R9d", "a path contains a control character", repr(p))])
    return digest({
        "include_selectors": normalize_set(include_selectors),
        "omit_patterns": normalize_set(omit_patterns),
        "exclusion_record_ids": normalize_set(exclusion_ids),
        "exclusion_regexes": normalize_set(exclusion_regexes),
        "discovery_rule": discovery_rule,
        "in_scope_paths": normalize_set(paths),
        "unreported_scope_paths": normalize_set(unreported_paths),
    })


# ===========================================================================
# 3. Static semantic map, resolved map, comparator (proposal section 7a).
# ===========================================================================


def load_static_map(assets_dir: Path, pinned_sha: str, *, seams: Seams | None = None) -> tuple[dict | None, list[Fire]]:
    """The static semantic-source map plus R17a (digest != pin) and R17b (key set !=
    COMPARABILITY_CHECKED_FIELDS, imported never copied) fires."""
    seams = seams or Seams()
    fires: list[Fire] = []
    path = Path(assets_dir) / STATIC_MAP_NAME
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, [Fire("R17a", f"static semantic map unreadable: {exc}", str(path))]
    if not seams.off("R17a") and sha256_bytes(raw) != pinned_sha:
        fires.append(Fire("R17a", "static semantic map digest differs from the digest pinned in the adapter source",
                          f"{path}: {sha256_bytes(raw)} != {pinned_sha}"))
    try:
        data = json.loads(raw.decode("utf-8"))
        fields = data["fields"]
    except (ValueError, KeyError, TypeError) as exc:
        return None, fires + [Fire("R17b", f"static semantic map is not a map with `fields`: {exc}", str(path))]
    if not seams.off("R17b") and set(fields) != set(COMPARABILITY_CHECKED_FIELDS):
        missing = sorted(set(COMPARABILITY_CHECKED_FIELDS) - set(fields))
        extra = sorted(set(fields) - set(COMPARABILITY_CHECKED_FIELDS))
        fires.append(Fire("R17b", f"map key set differs from COMPARABILITY_CHECKED_FIELDS (missing {missing}, extra {extra})",
                          str(path)))
    return data, fires


def build_resolved_map(static_map: Mapping, static_sha: str, instances: Mapping[str, Any],
                       observations: Mapping[str, Any]) -> dict:
    """semantic-map.resolved.json: `compared` holds exactly the 16 keys (declaration + instance),
    `observations` holds what is NOT compared."""
    fields = static_map["fields"]
    compared = {key: {"declaration": fields[key]["statement"], "instance": instances[key]}
                for key in COMPARABILITY_CHECKED_FIELDS}
    return {"schema": "org083.adapter-resolved-map.v1", "adapter_id": ADAPTER_ID,
            "static_map_sha256": static_sha, "compared": compared, "observations": dict(observations)}


@dataclass
class CompareResult:
    changed_fields: list[str]
    verdict: str  # comparable | reset_required | refused
    reasons: list[str]


def _retrieve_side(manifest_id: str, retrieve: Callable[[str], bytes],
                   records: Mapping[str, Mapping], *, seams: Seams) -> tuple[dict | None, str | None]:
    """The side's resolved map, with every digest verified against the manifest AND the root
    ArtifactRecord.  (None, reason) on any failure."""
    try:
        manifest_bytes = retrieve(manifest_id)
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        members = {m["name"]: m for m in manifest["members"]}
        member = members["semantic-map.resolved.json"]
        map_bytes = retrieve(member["artifact_id"])
    except (KeyError, ValueError, OSError, TypeError) as exc:
        return None, f"{manifest_id}: manifest or member unreadable: {exc}"
    if not seams.off("SM7.manifest") and sha256_bytes(map_bytes) != member.get("sha256"):
        return None, f"{manifest_id}: resolved-map bytes differ from the manifest digest"
    if not seams.off("SM7.record"):
        rec = records.get(member["artifact_id"])
        if rec is None or sha256_bytes(map_bytes) != rec.get("digest"):
            return None, f"{manifest_id}: resolved-map bytes differ from its ArtifactRecord digest"
    try:
        return json.loads(map_bytes.decode("utf-8")), None
    except ValueError as exc:
        return None, f"{manifest_id}: resolved map unparseable: {exc}"


def compare(baseline_manifest_id: str, current_manifest_id: str, *, retrieve: Callable[[str], bytes],
            records: Mapping[str, Mapping], seams: Seams | None = None) -> CompareResult:
    """Derive changed_fields and the verdict from two retained manifests (section 7a.3)."""
    seams = seams or Seams()
    maps: list[dict] = []
    for mid in (baseline_manifest_id, current_manifest_id):
        resolved, reason = _retrieve_side(mid, retrieve, records, seams=seams)
        if resolved is None:
            return CompareResult([], "refused", [reason or "unreadable"])
        maps.append(resolved)
    expected = set(COMPARABILITY_CHECKED_FIELDS)
    if not seams.off("SM5"):
        for resolved in maps:
            if set(resolved.get("compared", {})) != expected:
                return CompareResult([], "refused", ["a resolved map's compared key set differs from COMPARABILITY_CHECKED_FIELDS"])
    base_c, cur_c = maps[0].get("compared", {}), maps[1].get("compared", {})
    changed = [key for key in COMPARABILITY_CHECKED_FIELDS
               if canonical_bytes(base_c.get(key)) != canonical_bytes(cur_c.get(key))]
    return CompareResult(changed, "comparable" if not changed else "reset_required", [])


# ===========================================================================
# 4. Parsers.
# ===========================================================================


def parse_kv_report(text: str) -> dict[str, str]:
    """`coverage debug sys|config` layout: 31-column right-aligned key, `: `, value; continuation
    lines are indented to the value column."""
    out: dict[str, str] = {}
    last: str | None = None
    for line in text.splitlines():
        if len(line) >= 33 and line[31:33] == ": " and line[:31].strip() and not line.startswith("-- "):
            last = line[:31].strip()
            out.setdefault(last, line[33:])
        elif last is not None and line.startswith(" " * 33):
            out[last] = out[last] + "\n" + line[33:]
        elif not line.strip():
            last = None
    return out


@dataclass
class CoverageObservation:
    version: str
    ctracer: bool
    core: str
    python_version: str
    implementation: str
    platform_text: str
    glibc: str
    stable_sys: dict
    stable_config: dict


_SYS_KEYS = ("coverage_version", "core", "python", "platform", "implementation", "gil_enabled")
_CONFIG_DROP = {"data_file", "config_file", "config_files_attempted", "config_files_read", "command_line",
                "json_output", "html_dir", "lcov_output", "xml_output"}


def parse_observation(sys_text: str, config_text: str) -> CoverageObservation:
    """Effective coverage version, CTracer availability and core.  `debug sys` shows `core:
    -none-` before a run, so the resolved `core` of `debug config` is the effective one."""
    s, c = parse_kv_report(sys_text), parse_kv_report(config_text)
    core = s.get("core", "-none-")
    if core in ("-none-", ""):
        core = c.get("core", "-none-")
    py = s.get("python", "")
    py_version = py.split(" ", 1)[0]
    plat = s.get("platform", "")
    glibc = plat.rsplit("glibc", 1)[1] if "glibc" in plat else "?"
    return CoverageObservation(
        version=s.get("coverage_version", ""), ctracer=s.get("CTracer", "").startswith("available"), core=core,
        python_version=py_version, implementation=s.get("implementation", ""), platform_text=plat, glibc=glibc,
        stable_sys={k: s.get(k, "") for k in _SYS_KEYS} | {"CTracer": s.get("CTracer", "").split(" ", 1)[0]},
        stable_config={k: v for k, v in sorted(c.items()) if k not in _CONFIG_DROP})


_RESULT_LINE = re.compile(r"^(Combined data file|Skipping duplicate data|Couldn't combine data file)\s+(?P<path>\S+?)(?::|\s|$)")


@dataclass
class CombineReport:
    results: dict[str, str]  # basename -> combined | duplicate | error
    errored: list[tuple[str, str]]  # (basename, reason)
    other_skips: list[str]
    mixed: bool
    summary: str


def parse_combine(rc: int, stdout: str, stderr: str) -> CombineReport:
    """`coverage combine --debug=combine` output (stdout and stderr), format captured from 7.17.0."""
    text = (stdout or "") + "\n" + (stderr or "")
    results: dict[str, str] = {}
    errored: list[tuple[str, str]] = []
    other_skips: list[str] = []
    summary = ""
    for line in text.splitlines():
        m = re.match(r"^Combined data file (\S+)", line)
        if m:
            results[os.path.basename(m.group(1))] = "combined"
            continue
        m = re.match(r"^Skipping duplicate data (\S+)", line)
        if m:
            results[os.path.basename(m.group(1))] = "duplicate"
            continue
        m = re.match(r"^Couldn't combine data file (\S+?): (.*)$", line)
        if m:
            base = os.path.basename(m.group(1))
            results[base] = "error"
            errored.append((base, m.group(2)))
            continue
        if re.match(r"^Skipping(?! duplicate data)\b", line) or line.startswith("No data to combine"):
            other_skips.append(line.strip())
        if re.match(r"^Combined \d+ files?", line):
            summary = line.strip()
    mixed = bool(re.search(r"Can't combine (statement|branch) coverage data with (statement|branch) data", text))
    return CombineReport(results, errored, other_skips, mixed, summary)


_WARNING = re.compile(r"CoverageWarning: (?P<msg>.*?) \((?P<slug>[a-z-]+)\)")
REFUSED_WARNING_SLUGS = ("no-data-collected", "module-not-measured", "couldnt-parse", "include-ignored")


def parse_warnings(text: str) -> list[tuple[str, str]]:
    """(slug, message) for every CoverageWarning in a captured stderr/log text."""
    return [(m.group("slug"), m.group("msg")) for m in _WARNING.finditer(text or "")]


def warning_names_in_scope_module(message: str) -> bool:
    """module-not-measured names a module (`tools.x`) or a path (`tools/x.py`, `<ROOT>/tools/...`)."""
    return bool(re.search(r"(?:^|[\s'\"/])tools(?:[./]|\b)", message))


@dataclass
class DataFileInfo:
    name: str
    size: int
    sha256: str
    mtime: float
    readable: bool
    reason: str = ""
    schema: int | None = None
    has_arcs: bool = False
    measured_files: int = 0
    executed_rows: int = 0
    controller: bool = False
    argv0: str = ""


def read_data_file(path: Path) -> DataFileInfo:
    """Inspect one coverage data file with stdlib sqlite3 (read-only).  Anything unreadable or of
    an unknown schema version is reported unreadable with its reason (R7a)."""
    st = path.stat() if path.exists() else None
    info = DataFileInfo(name=path.name, size=st.st_size if st else 0, sha256="", mtime=st.st_mtime if st else 0.0,
                        readable=False)
    try:
        info.sha256 = sha256_file(path)
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            schema = con.execute("select version from coverage_schema").fetchone()
            info.schema = schema[0] if schema else None
            if info.schema != SUPPORTED_DATA_SCHEMA:
                info.reason = f"unsupported coverage data schema version {info.schema!r}"
                return info
            meta = dict(con.execute("select key, value from meta").fetchall())
            info.has_arcs = meta.get("has_arcs") in ("1", "True", "true")
            try:
                argv = eval_literal_list(meta.get("sys_argv", "[]"))
            except ValueError:
                argv = []
            info.argv0 = argv[0] if argv else ""
            info.controller = info.argv0.replace("\\", "/").endswith("/pytest/__main__.py")
            info.measured_files = con.execute("select count(*) from file").fetchone()[0]
            info.executed_rows = (con.execute("select count(*) from arc").fetchone()[0]
                                  + con.execute("select count(*) from line_bits").fetchone()[0])
            info.readable = True
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        info.reason = str(exc)
    return info


def eval_literal_list(text: str) -> list[str]:
    """`sys_argv` is stored as a Python list repr; parse it without eval."""
    import ast
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError) as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(value, list):
        raise ValueError("not a list")
    return [str(v) for v in value]


# ===========================================================================
# 5. Predicate evaluators (section 10a rows).  Each returns a list of Fire.
# ===========================================================================


def eval_args(paths: Sequence[str], extra_args: Sequence[str], *, seams: Seams) -> list[Fire]:
    fires: list[Fire] = []
    if tuple(paths) != DEFAULT_PATHS and not seams.off("R13a"):
        fires.append(Fire("R13a", "explicit subset (--paths): scope=subset is never admissible as a repository measurement",
                          " ".join(paths)))
    if not seams.off("R13b"):
        narrowing = [a for a in extra_args if a.split("=", 1)[0] in NARROWING_FLAGS
                     or (a.startswith("no:") and a != "no:cacheprovider")]
        if narrowing:
            fires.append(Fire("R13b", "implicit test-selection narrowing in the pytest argv", " ".join(narrowing)))
    return fires


def eval_environment(environ: Mapping[str, str], *, seams: Seams) -> list[Fire]:
    if seams.off("R16b"):
        return []
    bad = sorted(k for k in environ if k.startswith("COVERAGE_"))
    return [Fire("R16b", "a COVERAGE_* variable is set outside the adapter", ",".join(bad))] if bad else []


def eval_config(config_path: Path, pinned_sha: str, *, seams: Seams) -> list[Fire]:
    if seams.off("R16a"):
        return []
    try:
        actual = sha256_file(config_path)
    except OSError as exc:
        return [Fire("R16a", f"adapter coverage config unreadable: {exc}", str(config_path))]
    if actual != pinned_sha:
        return [Fire("R16a", "config bytes differ from the committed (pinned) adapter config",
                     f"{config_path}: {actual} != {pinned_sha}")]
    return []


def eval_start_state(status_lines: Sequence[str], head: str, requested: str, *, seams: Seams) -> list[Fire]:
    fires: list[Fire] = []
    untracked = [ln for ln in status_lines if ln.startswith("??")]
    modified = [ln for ln in status_lines if ln and not ln.startswith("??")]
    if untracked and not seams.off("R4a"):
        fires.append(Fire("R4a", "untracked file(s) in the worktree", "; ".join(untracked[:5])))
    if modified and not seams.off("R4b"):
        fires.append(Fire("R4b", "modified tracked file(s) in the worktree", "; ".join(modified[:5])))
    if head != requested and not seams.off("R4c"):
        fires.append(Fire("R4c", "measured revision differs from the requested revision", f"HEAD {head} != {requested}"))
    return fires


def eval_data_dir_start(data_dir: Path, *, seams: Seams) -> list[Fire]:
    if seams.off("R14a"):
        return []
    left = sorted(p.name for p in data_dir.iterdir()) if data_dir.exists() else []
    return [Fire("R14a", "the data directory is non-empty at start", ",".join(left[:5]))] if left else []


def eval_coverage_observation(obs: CoverageObservation, pin: CoveragePin, *, seams: Seams) -> list[Fire]:
    fires: list[Fire] = []
    if obs.version != pin.version and not seams.off("R5a"):
        fires.append(Fire("R5a", f"coverage version {obs.version!r} differs from the pin {pin.version!r}", "debug sys"))
    if not obs.ctracer and not seams.off("R5b"):
        fires.append(Fire("R5b", "the C tracer is unavailable (a pure-Python wheel?)", "debug sys CTracer"))
    if obs.core != "ctrace" and not seams.off("R5c"):
        fires.append(Fire("R5c", f"the effective core is {obs.core!r}, not ctrace", "debug sys / debug config core"))
    return fires


def eval_contradictions(static_map: Mapping, *, observed_core: str | None = None, has_arcs: bool | None = None,
                        argv_extra: Sequence[str] = (), seams: Seams) -> list[Fire]:
    """R17c: an observed value that contradicts a static declaration (section 7a.4)."""
    fires: list[Fire] = []
    facts = {k: v.get("facts", {}) for k, v in static_map["fields"].items()}
    bs, ts = facts.get("branch_semantics", {}), facts.get("test_selection", {})
    if observed_core is not None and not seams.off("R17c.core") and observed_core != bs.get("core"):
        fires.append(Fire("R17c", f"branch semantics declare core {bs.get('core')!r} but {observed_core!r} was observed",
                          "branch_semantics"))
    if has_arcs is not None and not seams.off("R17c.arcs") and bool(has_arcs) != bool(bs.get("arcs")):
        fires.append(Fire("R17c", f"branch semantics declare arcs={bs.get('arcs')} but has_arcs={has_arcs} was observed",
                          "branch_semantics"))
    narrowed = any(a.split("=", 1)[0] in NARROWING_FLAGS for a in argv_extra)
    if not seams.off("R17c.selection") and narrowed != bool(ts.get("narrowing_flags")):
        fires.append(Fire("R17c", "the test_selection declaration (no narrowing flags) contradicts the argv", "test_selection"))
    return fires


def eval_junit(junit: Path, pytest_rc: int, start: float, *, seams: Seams) -> tuple[list[Fire], dict | None]:
    """R1a-c, R2a-d, R14c, R15c over this run's junit.  Returns (fires, counts-or-None)."""
    fires: list[Fire] = []
    if pytest_rc != 0 and not seams.off("R1a"):
        fires.append(Fire("R1a", f"pytest exited {pytest_rc}", "pytest rc"))
    if pytest_rc == 2 and not seams.off("R15c"):
        fires.append(Fire("R15c", "pytest was interrupted (rc 2): partial data", "pytest rc"))
    if not junit.exists():
        if not seams.off("R2a"):
            fires.append(Fire("R2a", "the junit report is missing", str(junit)))
        return fires, None
    if junit.stat().st_mtime < start - START_SLACK_SECONDS and not seams.off("R14c"):
        fires.append(Fire("R14c", "the junit report is older than the run start (stale)", str(junit)))
    try:
        counts = count(junit)
    except InputError as exc:
        message = str(exc)
        if "holds no testcases" in message:
            if not seams.off("R2d"):
                fires.append(Fire("R2d", "the junit report holds zero testcases", message))
        elif not seams.off("R2b"):
            fires.append(Fire("R2b", "the junit report is unreadable or malformed", message))
        return fires, None
    try:
        root = ET.parse(str(junit)).getroot()
    except ET.ParseError:
        root = None
    if root is not None:
        failures = sum(1 for c in root.iter("testcase") if c.find("failure") is not None)
        errors = sum(1 for c in root.iter("testcase") if c.find("error") is not None)
        if failures and not seams.off("R1b"):
            fires.append(Fire("R1b", f"{failures} junit testcase(s) failed", str(junit)))
        if errors and not seams.off("R1c"):
            fires.append(Fire("R1c", f"{errors} junit testcase(s) errored", str(junit)))
    if counts["executed"] == 0 and not seams.off("R2c"):
        fires.append(Fire("R2c", "zero tests executed (everything skipped)", str(junit)))
    return fires, counts


def eval_shell(checker: Path, rc: int | None, *, whole_tree: bool, seams: Seams) -> list[Fire]:
    """R3a: the checker failed.  R3b: it could not be run on a whole-tree run."""
    fires: list[Fire] = []
    if not whole_tree:
        return fires
    if rc is None:
        if not seams.off("R3b"):
            fires.append(Fire("R3b", "the shell-suite outcome checker was not run on a whole-tree run", str(checker)))
    elif rc != 0 and not seams.off("R3a"):
        fires.append(Fire("R3a", f"check_shell_suite_outcomes exited {rc}", str(checker)))
    return fires


def eval_data_files(infos: Sequence[DataFileInfo], workers: int, start: float, *, seams: Seams) -> list[Fire]:
    fires: list[Fire] = []
    readable = [i for i in infos if i.readable]
    if len(infos) < 1 + workers and not seams.off("R6a"):
        fires.append(Fire("R6a", f"{len(infos)} parallel data file(s), fewer than 1 + N = {1 + workers}", "data dir"))
    if not any(i.controller for i in readable) and not seams.off("R6d"):
        fires.append(Fire("R6d", "no controller data file (pytest main process) among the data files", "data dir"))
    non_controller_full = [i for i in readable if not i.controller and i.measured_files > 0]
    if len(non_controller_full) < workers and not seams.off("R6b"):
        fires.append(Fire("R6b", f"only {len(non_controller_full)} non-controller data file(s) hold measured files, "
                                 f"need at least N = {workers}", "data dir"))
    if sum(i.executed_rows for i in readable) == 0 and not seams.off("R6c"):
        fires.append(Fire("R6c", "zero executed lines or arcs across all data files", "data dir"))
    old = [i.name for i in infos if i.mtime < start - START_SLACK_SECONDS]
    if old and not seams.off("R14b"):
        fires.append(Fire("R14b", "data file(s) older than the run start (stale)", ",".join(old[:5])))
    if readable and not seams.off("R5d") and not all(i.has_arcs for i in readable):
        fires.append(Fire("R5d", "a data file has no arcs (statement-only data)", ",".join(i.name for i in readable if not i.has_arcs)))
    return fires


_UNREADABLE_REASON = re.compile(r"unable to open database file|SQLITE_CANTOPEN|Permission denied|readonly", re.I)


def eval_combine(rc: int, report: CombineReport, *, seams: Seams) -> list[Fire]:
    fires: list[Fire] = []
    if report.mixed and not seams.off("R8"):
        fires.append(Fire("R8", "cannot combine statement coverage data with branch data", report.summary or "combine"))
    if (report.errored or re.search(r"\d+ files? errored", report.summary)) and not seams.off("R7a"):
        fires.append(Fire("R7a", "combine reported an error or an unreadable data file",
                          "; ".join(f"{b}: {r}" for b, r in report.errored[:3]) or report.summary))
    if not seams.off("R7b"):
        unreadable = [(b, r) for b, r in report.errored if _UNREADABLE_REASON.search(r)]
        if unreadable:
            fires.append(Fire("R7b", "an unreadable data file was skipped by combine",
                              "; ".join(f"{b}: {r}" for b, r in unreadable[:3])))
    if report.other_skips and not seams.off("R7c"):
        fires.append(Fire("R7c", "combine skipped a file for a reason other than an identical-hash duplicate",
                          "; ".join(report.other_skips[:3])))
    if rc != 0 and not (report.mixed or report.errored) and not seams.off("R7a"):
        fires.append(Fire("R7a", f"combine exited {rc}", "combine"))
    return fires


def eval_warnings(sources: Mapping[str, str], *, seams: Seams) -> tuple[list[Fire], list[dict]]:
    """R11a-e over EVERY process's captured stderr/log (sources maps a label to text; the labels
    that are not `controller` are children).  Returns (fires, all warnings recorded)."""
    fires: list[Fire] = []
    recorded: list[dict] = []
    for label, text in sources.items():
        for slug, message in parse_warnings(text):
            recorded.append({"source": label, "slug": slug, "message": message})
            if label != "controller" and seams.off("R11e"):
                continue
            if slug == "no-data-collected" and not seams.off("R11a"):
                fires.append(Fire("R11a", "coverage warned: no data collected", f"{label}: {message}"))
            elif slug == "module-not-measured" and not seams.off("R11b") and warning_names_in_scope_module(message):
                fires.append(Fire("R11b", "coverage warned: an in-scope module was not measured", f"{label}: {message}"))
            elif slug == "couldnt-parse" and not seams.off("R11c"):
                fires.append(Fire("R11c", "coverage could not parse a Python file", f"{label}: {message}"))
            elif slug == "include-ignored" and not seams.off("R11d"):
                fires.append(Fire("R11d", "coverage warned: --include is ignored", f"{label}: {message}"))
    return fires, recorded


def eval_totals(totals: Mapping, *, seams: Seams) -> list[Fire]:
    fires: list[Fire] = []
    for dim, (_unit, num_key, den_key) in DIMENSIONS.items():
        num, den = totals.get(num_key), totals.get(den_key)
        if not (type(num) is int and type(den) is int and num >= 0 and den >= 0):
            if not seams.off("R12c"):
                fires.append(Fire("R12c", f"{dim}: non-integer or negative counts", f"{num_key}={num!r} {den_key}={den!r}"))
            continue
        if den == 0 and not seams.off("R12a"):
            fires.append(Fire("R12a", f"{dim}: denominator is 0", f"{den_key}=0"))
        if num > den and not seams.off("R12b"):
            fires.append(Fire("R12b", f"{dim}: numerator exceeds denominator", f"{num_key}={num} > {den_key}={den}"))
    return fires


def reconcile_reported(keys: Iterable[str], root: Path, tracked_py: Sequence[str], in_scope: Sequence[str],
                       undiscoverable: Sequence[str], *, seams: Seams) -> tuple[list[Fire], list[str], list[str]]:
    """R9 (path discipline) and R10 (reported file set == tracked in-scope minus recorded
    undiscoverables, in both directions).  Returns (fires, reported rel paths, unreported rel paths)."""
    fires: list[Fire] = []
    real_root = os.path.realpath(root)
    tracked = set(tracked_py)
    canon: dict[str, list[str]] = {}
    for key in keys:
        if has_control_char(key):
            if not seams.off("R9d"):
                fires.append(Fire("R9d", "a measured path contains a control character", repr(key)))
            continue
        if os.path.isabs(key):
            lexical = os.path.normpath(key)
        else:
            lexical = os.path.normpath(os.path.join(real_root, key))
            rel_key = os.path.normpath(key)
            if not rel_key.startswith("tools" + os.sep) and f"tools/{rel_key}" in tracked:
                if not seams.off("R9b"):
                    fires.append(Fire("R9b", "the same source file appears under a relative alias key (relative_files corruption)", key))
                canon.setdefault(f"tools/{rel_key}", []).append(key)
                continue
        real = os.path.realpath(lexical)
        if real != lexical and not seams.off("R9c"):
            fires.append(Fire("R9c", "a measured path goes through a symlink", f"{key} -> {real}"))
        if not real.startswith(os.path.join(real_root, "tools") + os.sep):
            if not seams.off("R9a"):
                fires.append(Fire("R9a", "a measured path lies outside the absolute <ROOT>/tools", key))
            continue
        canon.setdefault(Path(real).relative_to(real_root).as_posix(), []).append(key)
    for rel, spellings in canon.items():
        if len(spellings) > 1 and not seams.off("R9b"):
            fires.append(Fire("R9b", "the same source file appears under two keys", f"{rel}: {spellings}"))
    reported = sorted(canon)
    expected = set(in_scope)
    record = set(undiscoverable)
    if not seams.off("R10a"):
        extra = [r for r in reported if r not in tracked]
        if extra:
            fires.append(Fire("R10a", "a reported file is not tracked (generated or untracked source)", ", ".join(extra[:5])))
    if not seams.off("R10b"):
        absent = sorted(expected - set(reported) - record)
        if absent:
            fires.append(Fire("R10b", "a tracked in-scope file is absent from the report with no exclusion record",
                              ", ".join(absent[:5])))
    if not seams.off("R10c"):
        stale = sorted(record & set(reported))
        if stale:
            fires.append(Fire("R10c", "the undiscoverable record lists a file that is actually reported", ", ".join(stale[:5])))
    if not seams.off("R10d"):
        ghost = sorted(r for r in record if r not in tracked)
        if ghost:
            fires.append(Fire("R10d", "the undiscoverable record names a path that is not tracked", ", ".join(ghost[:5])))
    unreported = sorted((expected & record) - set(reported))
    return fires, reported, unreported


# ===========================================================================
# 6. Environment, venv, worktree, measured run (proposal sections 5, 6, 11.5).
# ===========================================================================


def static_unset_names() -> list[str]:
    """Section 6's seven names UNIONED with soak_leg.STRIPPED_ENV (the section 12 item 3 reuse)."""
    return sorted(set(SECTION6_UNSET) | set(soak_leg.STRIPPED_ENV))


def env_unset_names(redirect: Iterable[str]) -> list[str]:
    """The exact sorted union of every name stripped from the measured environment: the static
    names plus the git repo-redirect variables asked of git at run time."""
    return sorted(set(static_unset_names()) | set(redirect))


def build_measured_env(base: Mapping[str, str], venv_dir: Path, data_file: Path, redirect: frozenset[str]) -> dict:
    """soak_leg.build_pytest_env (strip + telegram flag) minus section 6's remaining names, plus the allowlist."""
    env = soak_leg.build_pytest_env(dict(base), redirect=redirect)
    for name in SECTION6_UNSET:
        env.pop(name, None)
    env.update(ENV_SET)
    env["COVERAGE_FILE"] = str(data_file)
    env["PATH"] = f"{venv_dir}/bin:{base.get('PATH', '')}"
    return env


def resolve_python3(venv_dir: Path, base_path: str) -> str:
    """The PATH-resolved python3 identity with the venv bin/ first (as actions/setup-python does)."""
    found = shutil.which("python3", path=f"{venv_dir}/bin:{base_path}")
    return found or "unresolved"


def measured_argv(venv_dir: Path, cfg: Path, out: Path, workers: int, paths: Sequence[str], extra: Sequence[str]) -> list[str]:
    """Proposal section 6 step 2, minus the `env -u` prefix (applied to the environment instead)."""
    return [str(soak_leg.venv_python(venv_dir)), "-m", "coverage", "run", f"--rcfile={cfg}", "-m", "pytest",
            *paths, "-q", "-n", str(workers), "-p", "no:cacheprovider", "-o", "faulthandler_timeout=600",
            "-o", "junit_family=xunit1", f"--junitxml={out / 'pytest-tools.xml'}", f"--basetemp={out / 'basetemp'}",
            *extra]


def _adapter_marker_text(ci_pins: Sequence[str], lock_sha: str) -> str:
    return (f"python={soak_leg.python_tag()}\nci_pins_sha256={digest(normalize_set(ci_pins))}\n"
            f"coverage_lock_sha256={lock_sha}\n")


def ensure_adapter_venv(venv_dir: Path, ci_pins: Sequence[str], lock_path: Path, *, runner: Runner) -> str:
    """CI's pins through soak_leg.ensure_venv (pip, as CI does), then a SECOND pip step with
    --require-hashes --no-deps --only-binary=:all: for coverage only (a single --require-hashes
    run would demand hashes for CI's pins).  The reuse marker is keyed on (Python minor, CI pin
    digest, coverage lock digest) under an flock."""
    venv_dir = Path(venv_dir)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_file = venv_dir.with_name(venv_dir.name + ".adapter.lock")
    with open(lock_file, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        action = soak_leg.ensure_venv(venv_dir, ci_pins, runner=runner)
        want = _adapter_marker_text(ci_pins, sha256_file(lock_path))
        marker = venv_dir / ADAPTER_MARKER
        try:
            if marker.read_text(encoding="utf-8") == want:
                return action
        except OSError:
            pass
        r = runner([str(soak_leg.venv_python(venv_dir)), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "--require-hashes", "--no-deps", "--only-binary=:all:", "-r", str(lock_path)],
                   capture_output=True, text=True)
        if r.returncode != 0:
            raise SetupError(f"hash-locked coverage install into {venv_dir} failed (rc {r.returncode}): "
                             f"{(r.stderr or '').strip()}")
        marker.write_text(want, encoding="utf-8")
        return "installed" if action == "reused" else action


def observe_coverage(venv_dir: Path, cfg: Path, root: Path, environ: Mapping[str, str], *, runner: Runner) -> CoverageObservation:
    """`coverage debug sys` + `debug config` revalidated on EVERY run (R5)."""
    env = dict(environ)
    env["COVERAGE_RCFILE"] = str(cfg)  # for these two read-only calls only (debug takes no --rcfile)
    outs = []
    for topic in ("sys", "config"):
        r = runner([str(soak_leg.venv_python(venv_dir)), "-m", "coverage", "debug", topic], cwd=str(root), env=env,
                   capture_output=True, text=True)
        if r.returncode != 0:
            raise SetupError(f"`coverage debug {topic}` failed in {venv_dir} (rc {r.returncode}): {(r.stderr or '').strip()}")
        outs.append(r.stdout or "")
    return parse_observation(outs[0], outs[1])


def git_out(root: Path, *args: str, runner: Runner) -> subprocess.CompletedProcess:
    return runner(["git", "-C", str(root), *args], capture_output=True, text=True)


def tracked_files(root: Path, *, runner: Runner) -> list[str]:
    r = git_out(root, "ls-files", "-z", runner=runner)
    if r.returncode != 0:
        raise SetupError(f"git ls-files failed in {root}: {(r.stderr or '').strip()}")
    return [p for p in (r.stdout or "").split("\0") if p]


def _on_sigterm(signum, frame):  # noqa: ARG001
    raise Interrupted("SIGTERM")


# ===========================================================================
# 7. Artifacts (proposal section 8).
# ===========================================================================


def dump_json(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def write_bytes(path: Path, data: bytes) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"sha256": sha256_bytes(data), "size": len(data)}


def refusal_payload(fires: Sequence[Fire]) -> dict:
    return {"schema": "org083.adapter-refusal.v1", "adapter_id": ADAPTER_ID, "admissible": False,
            "exit_code": EXIT_REFUSED,
            "refusals": [{"id": f.id, "reason": f.reason, "evidence": f.evidence} for f in fires]}


def write_refusal(out: Path, fires: Sequence[Fire]) -> None:
    """RA-2/3/4: refusal.json lists every fired predicate; no manifest, record body, numerator,
    denominator or value is published; diagnostics stay in <OUT>/diagnostics/ unreferenced."""
    ids = sorted({f.id for f in fires})
    write_bytes(out / "refusal.json", dump_json(refusal_payload(fires)))
    write_bytes(out / "measurement-inputs.json", dump_json(
        {"schema": INPUTS_SCHEMA, "adapter_id": ADAPTER_ID, "admissible": False, "refusal_codes": ids}))
    lines = ["# ORG-083 coverage adapter: REFUSED", "", f"Adapter id: `{ADAPTER_ID}`", "",
             "This run is not an admissible measurement. No coverage totals are published.", ""]
    lines += [f"- **{f.id}**: {f.reason}" + (f" (`{f.evidence}`)" if f.evidence else "") for f in fires]
    write_bytes(out / "coverage.md", ("\n".join(lines) + "\n").encode("utf-8"))


def load_baseline_run(prior: Path) -> tuple[dict, dict[str, Path], dict]:
    """(measurement-inputs, artifact_id -> path, records-by-artifact-id) of a prior admissible run."""
    prior = Path(prior)
    try:
        inputs = json.loads((prior / "measurement-inputs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SetupError(f"--baseline-run {prior}: measurement-inputs.json unreadable: {exc}") from exc
    if not inputs.get("admissible"):
        raise SetupError(f"--baseline-run {prior} is not an admissible run")
    records = {r["artifact_id"]: r for r in inputs.get("artifact_records", [])}
    paths = {aid: prior / r["storage_locator"]["path"] for aid, r in records.items()}
    return inputs, paths, records


# ===========================================================================
# 8. Pipeline and CLI.
# ===========================================================================

REPO_ROOT = Path(__file__).resolve().parent.parent


def default_out_dir() -> Path:
    return DEFAULT_OUT_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def prepare_out(out: Path, repo_root: Path, *, seams: Seams) -> None:
    """<OUT> must be fresh (RA-5) and outside the repository (the adapter never writes under it)."""
    real_out, real_repo = os.path.realpath(out), os.path.realpath(repo_root)
    if real_out == real_repo or real_out.startswith(real_repo + os.sep):
        raise SetupError(f"--out-dir {out} is inside the repository {repo_root}; the adapter never writes under it")
    if out.exists() and any(out.iterdir()):
        raise SetupError(f"--out-dir {out} is not empty: <OUT> must be fresh")
    out.mkdir(parents=True, exist_ok=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ORG-083 coverage adapter for pantheon-config (T-5971).")
    p.add_argument("--rev", default="HEAD", help="revision to measure in a fresh detached worktree (default HEAD)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"pytest-xdist -n (default {DEFAULT_WORKERS})")
    p.add_argument("--out-dir", type=Path, default=None, help=f"fresh run dir (default {DEFAULT_OUT_ROOT}/<UTC stamp>)")
    p.add_argument("--venv", type=Path, default=None, help=f"adapter venv (default {DEFAULT_VENV})")
    p.add_argument("--baseline-run", type=Path, default=None, help="a prior admissible <OUT> to derive changed_fields against")
    p.add_argument("--paths", nargs="+", default=None, help="subset run: ALWAYS refused (R13a), diagnostics only")
    p.add_argument("--pytest-arg", action="append", default=[], help="extra pytest arg (narrowing flags are refused, R13b)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds before the measured run is killed (R15b)")
    p.add_argument("--repository", default="pantheon-config")
    p.add_argument("--ci-workflow", type=Path, default=None, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.workers < 1:
        p.error("--workers must be >= 1")
    return args


def _status_lines(wt: Path, *, runner: Runner) -> list[str]:
    r = git_out(wt, "status", "--porcelain", runner=runner)
    return [ln for ln in (r.stdout or "").splitlines() if ln]


def _rev_parse(root: Path, rev: str, *, runner: Runner) -> str:
    r = git_out(root, "rev-parse", "--verify", f"{rev}^{{commit}}", runner=runner)
    sha = (r.stdout or "").strip()
    if r.returncode != 0 or not sha:
        raise SetupError(f"cannot resolve revision {rev!r} in {root}: {(r.stderr or '').strip()}")
    return sha


def _junit_texts(junit: Path) -> str:
    try:
        root = ET.parse(str(junit)).getroot()
    except (OSError, ET.ParseError):
        return ""
    return "\n".join((e.text or "") for tag in ("system-err", "system-out") for e in root.iter(tag))


def _excluded_inventory(files: Mapping[str, Mapping]) -> dict:
    per = {k: list(v.get("excluded_lines", [])) for k, v in sorted(files.items()) if v.get("excluded_lines")}
    return {"per_file": per, "total": sum(len(v) for v in per.values())}


@dataclass
class RunState:
    """Per-run facts shared by the pipeline phases."""
    args: argparse.Namespace
    out: Path
    venv: Path
    repo_root: Path
    environ: dict
    runner: Runner
    seams: Seams
    worktree: Path | None = None
    start: float = 0.0


def _refuse_if(fires: Sequence[Fire]) -> None:
    if fires:
        raise Refusal(fires)


def pipeline(st: RunState) -> int:
    """Phases: pre-checks -> worktree -> venv -> measured run -> outcome gates -> combine/report
    -> reconcile -> fingerprints -> resolved map -> manifest/inputs.  Raises Refusal carrying
    every predicate fired in the phase block that failed."""
    args, out, seams, runner = st.args, st.out, st.seams, st.runner
    assets = Path(seams.assets_dir) if seams.assets_dir else ASSETS_DIR
    cfg = assets / CONFIG_NAME
    lock = assets / LOCK_NAME
    pin = seams.coverage_pin or CoveragePin()
    paths = tuple(args.paths) if args.paths else DEFAULT_PATHS
    whole_tree = paths == DEFAULT_PATHS
    extra = list(args.pytest_arg)
    now = seams.now or (lambda: datetime.now(timezone.utc))
    diagnostics = out / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    data_dir = out / "data"
    data_dir.mkdir(exist_ok=True)

    # --- P0: arguments, environment, committed config, static map (no worktree needed) -------
    arg_fires = eval_args(paths, extra, seams=seams)
    subset_fires = [f for f in arg_fires if f.id == "R13a"]  # recorded; a subset still runs, for diagnostics
    p0 = [f for f in arg_fires if f.id != "R13a"]
    p0 += eval_environment(st.environ, seams=seams)
    p0 += eval_config(cfg, seams.pinned_config_sha or CONFIG_SHA256, seams=seams)
    static_map, map_fires = load_static_map(assets, seams.pinned_map_sha or STATIC_MAP_SHA256, seams=seams)
    p0 += map_fires
    _refuse_if(p0)
    assert static_map is not None
    static_sha = sha256_bytes((assets / STATIC_MAP_NAME).read_bytes())

    # --- P1: fresh detached worktree, clean start, empty data dir -----------------------------
    requested = _rev_parse(st.repo_root, args.rev, runner=runner)
    st.worktree = soak_leg.add_worktree(st.repo_root, out, args.rev, runner=runner)
    wt = st.worktree
    head = (git_out(wt, "rev-parse", "HEAD", runner=runner).stdout or "").strip()
    if seams.pre_start:
        seams.pre_start(data_dir, out)
    p1 = eval_start_state(_status_lines(wt, runner=runner), head, requested, seams=seams)
    p1 += eval_data_dir_start(data_dir, seams=seams)
    _refuse_if(p1)

    # --- P2: venv, effective coverage, apparatus facts ----------------------------------------
    ci_workflow = args.ci_workflow or wt / ".github" / "workflows" / "ci.yml"
    ci_pins = soak_leg.load_ci_pins(ci_workflow)
    ensure_adapter_venv(st.venv, ci_pins, lock, runner=runner)
    obs = observe_coverage(st.venv, cfg, wt, st.environ, runner=runner)
    p2 = eval_coverage_observation(obs, pin, seams=seams)
    p2 += eval_contradictions(static_map, observed_core=obs.core, argv_extra=extra, seams=seams)
    _refuse_if(p2)
    freeze = runner([str(soak_leg.venv_python(st.venv)), "-m", "pip", "freeze"], cwd=str(wt), capture_output=True, text=True)
    redirect = soak_leg.repo_redirect_env(runner=runner)
    unset_names = env_unset_names(redirect)
    env = build_measured_env(st.environ, st.venv, data_dir / ".coverage", redirect)
    argv = measured_argv(st.venv, cfg, out, args.workers, paths, extra)

    # --- P3: the measured run -----------------------------------------------------------------
    st.start = time.time()
    log_path = out / "pytest.log"
    try:
        with log_path.open("w", encoding="utf-8") as fh:
            r = runner(list(argv), cwd=str(wt), env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=args.timeout)
        pytest_rc = r.returncode
    except subprocess.TimeoutExpired as exc:
        raise Refusal([Fire("R15b", f"the measured run exceeded the {args.timeout}s timeout and was killed", str(exc))]) from exc

    # --- P4..P7: outcome gates, combine/report, reconcile (every fire collected) --------------
    post: list[Fire] = list(subset_fires)
    junit = out / "pytest-tools.xml"
    jf, counts = eval_junit(junit, pytest_rc, st.start, seams=seams)
    post += jf
    shell_rc: int | None = None
    checker = wt / "tools" / "check_shell_suite_outcomes.py"
    if junit.exists():
        if whole_tree and checker.exists():
            shell_rc = soak_leg.run_shell_outcomes(junit, out, runner=runner, repo_root=wt)
        post += eval_shell(checker, shell_rc, whole_tree=whole_tree, seams=seams)
    infos = [read_data_file(p) for p in sorted(data_dir.glob(".coverage.*"))]
    post += [Fire("R7a", f"data file unreadable: {i.reason}", i.name) for i in infos if not i.readable and not seams.off("R7a")]
    post += eval_data_files(infos, args.workers, st.start, seams=seams)
    cp = runner([str(soak_leg.venv_python(st.venv)), "-m", "coverage", "combine", f"--rcfile={cfg}", "--debug=combine", "--keep"],
                cwd=str(wt), env=env, capture_output=True, text=True)
    combine_text = (cp.stdout or "") + (cp.stderr or "")
    (out / "combine.log").write_text(combine_text, encoding="utf-8")
    creport = parse_combine(cp.returncode, cp.stdout or "", cp.stderr or "")
    post += eval_combine(cp.returncode, creport, seams=seams)
    combined = data_dir / ".coverage"
    combined_info = read_data_file(combined) if combined.exists() else None
    has_arcs = bool(combined_info and combined_info.has_arcs)
    raw_json = diagnostics / "coverage.raw.json"
    js = runner([str(soak_leg.venv_python(st.venv)), "-m", "coverage", "json", f"--rcfile={cfg}", "-o", str(raw_json)],
                cwd=str(wt), env=env, capture_output=True, text=True)
    md = runner([str(soak_leg.venv_python(st.venv)), "-m", "coverage", "report", f"--rcfile={cfg}", "--format=markdown"],
                cwd=str(wt), env=env, capture_output=True, text=True)
    cov: dict = {}
    if js.returncode == 0 and raw_json.exists():
        try:
            cov = json.loads(raw_json.read_text(encoding="utf-8"))
        except ValueError:
            cov = {}
    if not cov:
        stderr = (js.stderr or "").strip()
        if "No source for code" in stderr:
            post.append(Fire("R9b", "coverage json failed: relative alias keys (the F1 shape)", stderr[:300]))
        post.append(Fire("R12c", f"no usable coverage totals: `coverage json` failed (rc {js.returncode})", stderr[:300]))
    tracked_py = [p for p in tracked_files(wt, runner=runner) if p.endswith(".py")]
    undiscoverable = list(seams.undiscoverable_paths if seams.undiscoverable_paths is not None else UNDISCOVERABLE_PATHS)
    in_scope, scope_fires = build_in_scope(tracked_py, wt)
    post += scope_fires
    files = cov.get("files", {}) if cov else {}
    rf, reported, unreported = reconcile_reported(files.keys(), wt, tracked_py, in_scope, undiscoverable, seams=seams)
    post += rf
    if cov:
        post += eval_totals(cov.get("totals", {}), seams=seams)
        post += eval_contradictions(static_map, has_arcs=has_arcs, argv_extra=extra, seams=seams)
    sources = {"controller": log_path.read_text(encoding="utf-8", errors="replace"),
               "child:junit": _junit_texts(junit), "combine": combine_text, "json": js.stderr or "", "report": md.stderr or ""}
    wf, warnings = eval_warnings(sources, seams=seams)
    post += wf
    post += [f for f in eval_start_state(_status_lines(wt, runner=runner), head, requested, seams=seams)
             if f.id in ("R4a", "R4b")]
    accounting = {
        "schema": "org083.adapter-accounting.v1", "adapter_id": ADAPTER_ID, "pytest_rc": pytest_rc, "shell_outcomes_rc": shell_rc,
        "combine_rc": cp.returncode, "json_rc": js.returncode, "report_rc": md.returncode,
        "data_files": [{"name": i.name, "size": i.size, "sha256": i.sha256, "readable": i.readable, "reason": i.reason,
                        "controller": i.controller, "has_arcs": i.has_arcs, "measured_files": i.measured_files,
                        "combine_result": creport.results.get(i.name, "unknown"),
                        "pid_host_suffix": i.name[len(".coverage."):]} for i in infos],
        "combine_summary": creport.summary, "warnings": warnings,
        "junit": counts and {"executed": counts["executed"], "skipped": counts["skipped"], "failed": len(counts["failed"])},
    }
    write_bytes(diagnostics / "accounting.json", dump_json(accounting))
    _refuse_if(post)
    assert counts is not None and cov

    # --- P8: fingerprints, resolved map, manifest, inputs -------------------------------------
    clock = now()  # ONE reading: stamp and collected_at must not straddle a second boundary
    stamp = clock.strftime("%Y%m%dT%H%M%SZ")
    collected_at = clock.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Second-resolution stamps collide for two runs on one revision; the nonce makes every run's ids unique.
    stem = f"{head[:12]}-{stamp}-{seams.run_nonce or secrets.token_hex(4)}"
    label = seams.runner_label or f"local:{getpass.getuser()}"
    mapping = {str(wt): "<ROOT>", str(out): "<OUT>", str(st.venv): "<VENV>", str(cfg): "<CFG>"}
    regexes = [ln for key in ("exclude_list", "partial_list", "partial_always_list")
               for ln in obs.stable_config.get(key, "").splitlines() if ln and ln != "-none-"]
    fp_command = command_fingerprint(argv, mapping=mapping, env_set=ENV_SET, env_unset=unset_names,
                                     python3_identity=resolve_python3(st.venv, st.environ.get("PATH", "")),
                                     workers=args.workers)
    fp_config = config_digest(sha256_file(cfg), ci_pins, pin)
    fp_runner = runner_fingerprint(
        implementation=obs.implementation, python_version=obs.python_version, platform_text=obs.platform_text,
        glibc=obs.glibc, label=label, cpus=os.cpu_count() or 0, core=obs.core, ctracer=obs.ctracer,
        freeze_digest=pip_freeze_digest(freeze.stdout or ""))
    fp_scope = scope_fingerprint(in_scope_paths=in_scope, unreported_paths=unreported, exclusion_regexes=regexes)
    node_ids = sorted({f"{c.get('classname', '')}::{c.get('name', '')}" for c in ET.parse(str(junit)).getroot().iter("testcase")})
    instances = {
        "collector.adapter_id": ADAPTER_ID, "collector.tool_name": TOOL_NAME, "collector.tool_version": obs.version,
        "collector.command_fingerprint": fp_command, "collector.config_digest": fp_config,
        "collector.runner_fingerprint": fp_runner, "scope.include_selectors": list(INCLUDE_SELECTORS),
        "scope.exclusion_ids": list(EXCLUSION_IDS), "scope.generated_source_ids": list(GENERATED_SOURCE_IDS),
        "scope.scope_fingerprint": fp_scope, "dimension": sorted(DIMENSIONS),
        "unit": {d: v[0] for d, v in sorted(DIMENSIONS.items())},
        "denominator": {"line": "totals.num_statements", "branch": "totals.num_branches"},
        "branch_semantics": {"effective_core": obs.core, "has_arcs": has_arcs},
        "function_semantics": "deferred",
        "test_selection": {"argv_selection": list(paths), "extra_args": list(extra),
                           "narrowing_flags_present": any(a.split("=", 1)[0] in NARROWING_FLAGS for a in extra),
                           "env_allowlist": dict(sorted(ENV_SET.items()))},
    }
    observations = {
        "junit_node_id_count": len(node_ids), "junit_node_id_set_sha256": digest(node_ids), "has_arcs": has_arcs,
        "coverage_debug_sys_sha256": digest(obs.stable_sys), "coverage_debug_config_sha256": digest(obs.stable_config),
        "excluded_lines": _excluded_inventory(files), "parallel_data_file_count": len(infos),
        "env_unset_names": unset_names, "env_unset_rule": REDIRECT_RULE,
        "pytest_executed": counts["executed"], "pytest_skipped": counts["skipped"],
    }
    resolved = build_resolved_map(static_map, static_sha, instances, observations)

    def art_id(kind: str) -> str:
        return f"art-{stem}-{kind}"

    base_loaded = None
    if args.baseline_run:
        # Fail closed BEFORE anything is published to <OUT>: a shared id would make the comparator
        # resolve the baseline to this run's own bytes and report "no change" (T-5971).
        base_loaded = load_baseline_run(args.baseline_run)
        prior_ids = set(base_loaded[2]) | {m for r in base_loaded[0].get("records", [])
                                           for m in (r.get("measurement_id"), r.get("baseline", {}).get("measurement_id"))}
        mine = {art_id(k) for k in ("coverage", "map", "accounting", "manifest")} | {f"m-{d}-{stem}" for d in DIMENSIONS}
        if prior_ids & mine:
            raise SetupError(f"--baseline-run {args.baseline_run} shares ids with this run: {sorted(prior_ids & mine)}")

    artifacts = {
        "coverage": ("coverage.json.gz", gzip.compress(raw_json.read_bytes(), mtime=0), "application/gzip"),
        "map": ("semantic-map.resolved.json", dump_json(resolved), "application/json"),
        "accounting": ("accounting.json", dump_json(accounting), "application/json"),
    }
    members = []
    for kind, (name, data, _media) in artifacts.items():
        members.append({"name": name, "artifact_id": art_id(kind), **write_bytes(out / name, data)})
    raw_json.unlink()
    (diagnostics / "accounting.json").unlink()
    manifest = {"schema": MANIFEST_SCHEMA, "adapter_id": ADAPTER_ID, "revision": head, "members": members}
    manifest_meta = write_bytes(out / "run-manifest.json", dump_json(manifest))
    manifest_id = art_id("manifest")

    def record_for(name: str, aid: str, meta: Mapping, media: str) -> dict:
        return {"artifact_id": aid, "repository": args.repository, "revision": head, "artifact_class": "measurement",
                "created_at": collected_at, "producer": ADAPTER_ID, "tool": {"name": TOOL_NAME, "version": obs.version},
                "config_identity": fp_config, "storage_locator": {"kind": "repo_path", "path": name},
                "media_type": media, "digest_algorithm": "sha256", "digest": meta["sha256"], "size": meta["size"]}

    artifact_records = [record_for("run-manifest.json", manifest_id, manifest_meta, "application/json")]
    for member, (name, _data, media) in zip(members, artifacts.values()):
        artifact_records.append(record_for(name, member["artifact_id"], member, media))
    totals = cov["totals"]
    base_inputs = None
    verdict, changed = "comparable", []
    reason = "first measurement: self-baseline (no prior run supplied)"
    if args.baseline_run:
        base_inputs, base_paths, base_records = base_loaded
        here = {r["artifact_id"]: out / r["storage_locator"]["path"] for r in artifact_records}

        def retrieve(aid: str) -> bytes:
            return (here.get(aid) or base_paths[aid]).read_bytes()
        cmp_ = compare(base_inputs["manifest_artifact_id"], manifest_id, retrieve=retrieve,
                       records={**base_records, **{r["artifact_id"]: r for r in artifact_records}}, seams=seams)
        verdict, changed = cmp_.verdict, cmp_.changed_fields
        reason = "derived by the adapter comparator over the retained run manifests" if not cmp_.reasons else "; ".join(cmp_.reasons)
    records = []
    for dim, (unit, num_key, den_key) in DIMENSIONS.items():
        num, den = totals[num_key], totals[den_key]
        mid = f"m-{dim}-{stem}"
        base_rec = next((r for r in (base_inputs or {}).get("records", []) if r["dimension"] == dim), None)
        cur = {"measurement_id": mid, "numerator": num, "denominator": den, "value": num / den}
        baseline = {k: base_rec[k] for k in ("measurement_id", "numerator", "denominator", "value")} if base_rec else dict(cur)
        records.append({
            "measurement_id": mid, "repository": args.repository, "revision": head, "collected_at": collected_at,
            "dimension": dim, "unit": unit,
            "collector": {"adapter_id": ADAPTER_ID, "tool_name": TOOL_NAME, "tool_version": obs.version,
                          "command_fingerprint": fp_command, "config_digest": fp_config, "runner_fingerprint": fp_runner},
            "scope": {"include_selectors": list(INCLUDE_SELECTORS), "exclusion_ids": list(EXCLUSION_IDS),
                      "generated_source_ids": list(GENERATED_SOURCE_IDS), "scope_fingerprint": fp_scope},
            "numerator": num, "denominator": den, "value": num / den, "baseline": baseline, "current": cur,
            "threshold": {"state": "unset", "comparator": "gte", "value": None, "tolerance": 0.0},
            "ratchet": {"state": "unset", "direction": "nondecreasing", "proposed": None, "adopted": None, "tolerance": 0.0},
            "comparability": {"verdict": verdict, "baseline_measurement_id": baseline["measurement_id"],
                              "current_measurement_id": mid, "checked_fields": list(COMPARABILITY_CHECKED_FIELDS),
                              "changed_fields": list(changed), "reason": reason, "equivalence_decision": None,
                              "equivalence_artifact_id": None, "reset_authority_decision": None, "reset_artifact_id": None},
            "changed_code_record_id": None, "mutation_record_id": None, "flake_observation_ids": [],
            "artifact_id": manifest_id})
    inputs = {
        "schema": INPUTS_SCHEMA, "adapter_id": ADAPTER_ID, "admissible": True, "refusal_codes": [],
        "claims": "evidence only: no baseline, no threshold, no ratchet, no maturity change",
        "manifest_artifact_id": manifest_id, "revision": head, "records": records, "artifact_records": artifact_records,
        "fingerprints": {"command_fingerprint": fp_command, "config_digest": fp_config, "runner_fingerprint": fp_runner,
                         "scope_fingerprint": fp_scope},
        "scope_reconciliation": {"in_scope_path_count": len(in_scope), "reported_path_count": len(reported),
                                 "unreported_scope_paths": unreported, "exclusion_records": exclusion_records(undiscoverable)},
        "limitations": ["reachability, not effectiveness (ORG-083 section 1)",
                        "Python only: bash lines are not measured (unsupported)",
                        "patch = subprocess only: os._exit/exec*/SIGKILL/timeout-ended processes and env-replacing "
                        "subprocesses are not measured; reach is under-reported, never inflated",
                        "function, changed_code, mutation and flake dimensions are deferred"],
    }
    write_bytes(out / "measurement-inputs.json", dump_json(inputs))
    header = ["# ORG-083 coverage report (evidence only)", "", f"- revision: `{head}`", f"- adapter id: `{ADAPTER_ID}`",
              f"- dimensions: line (statement) {totals['covered_lines']}/{totals['num_statements']}; "
              f"branch (branch_destination) {totals['covered_branches']}/{totals['num_branches']}",
              "- limitations: reachability, not effectiveness; Python only; `patch = subprocess` only", "", md.stdout or ""]
    write_bytes(out / "coverage.md", ("\n".join(header) + "\n").encode("utf-8"))
    print(f"org083_coverage_adapter: ADMISSIBLE rev={head[:12]} line={totals['covered_lines']}/{totals['num_statements']} "
          f"branch={totals['covered_branches']}/{totals['num_branches']} out={out}")
    return EXIT_OK


def execute(argv: Sequence[str] | None = None, *, runner: Runner = subprocess.run, repo_root: Path | None = None,
            environ: Mapping[str, str] | None = None, seams: Seams | None = None) -> int:
    """The internal entry (the CLI is `main`).  `seams` is keyword-only and never built from argv."""
    args = parse_args(argv)
    seams = seams or Seams()
    repo_root = Path(repo_root or REPO_ROOT).resolve()
    out = Path(args.out_dir or default_out_dir()).resolve()
    venv = Path(args.venv or os.environ.get("PANTHEON_ORG083_VENV") or DEFAULT_VENV).resolve()
    st = RunState(args=args, out=out, venv=venv, repo_root=repo_root,
                  environ=dict(os.environ if environ is None else environ), runner=runner, seams=seams)
    try:
        prepare_out(out, repo_root, seams=seams)
    except (SetupError, OSError) as exc:
        print(f"org083_coverage_adapter: {exc}", file=sys.stderr)
        return EXIT_SETUP
    try:
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:  # not the main thread: no handler can be installed
        previous = None
    rc = EXIT_SETUP
    try:
        try:
            rc = pipeline(st)
        except Refusal as refusal:
            write_refusal(out, refusal.fires)
            print("org083_coverage_adapter: REFUSED " + ", ".join(sorted({f.id for f in refusal.fires})), file=sys.stderr)
            rc = EXIT_REFUSED
        except Interrupted:
            write_refusal(out, [Fire("R15a", "the adapter was signalled (SIGTERM) mid-run: partial data", "SIGTERM")])
            rc = EXIT_REFUSED
        except (SetupError, OSError, ValueError) as exc:
            print(f"org083_coverage_adapter: setup error: {exc}", file=sys.stderr)
            rc = EXIT_SETUP
    finally:
        if st.worktree is not None:
            soak_leg.remove_worktree(repo_root, st.worktree, runner=runner)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    return rc


def main(argv: Sequence[str] | None = None) -> int:
    return execute(argv)


if __name__ == "__main__":
    sys.exit(main())
