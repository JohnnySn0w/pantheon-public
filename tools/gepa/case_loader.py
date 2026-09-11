"""Loads the frozen `knowledge/eval/reviewer-v1/` case set (T-2809 Phase 2).

Read-only. Never writes to `knowledge/eval/reviewer-v1/**` (that directory is
ratification-gated per its own README's Goodhart guard) and never imports the
`gepa` package (see package docstring).

`load_cases(split=...)` is the split-scoping seam: a caller that only ever
calls `load_cases("train")` structurally cannot hand a holdout case to the
optimization loop. The adapter layer (`adapter.py`) additionally refuses at
evaluate()-time as defense in depth — this module's scoping is the first
line, not the only line.

Diff resolution (`resolve_diff`) is lazy and best-effort, exactly as the
eval-set README documents: "A Phase-2 adapter resolves the diff lazily via
`git show` against the pointer when it actually runs rollouts." Repo names
from a case file are looked up in a FIXED allowlist — never used to build an
arbitrary filesystem path — and any resolution failure (unknown repo, missing
checkout, dead sha, git failure) degrades to `None` (narrative-only), never
raises. A case file cannot make this module touch a path outside the
allowlist's values.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tools.ticket import REPO_ROOT

EVAL_SET_DIR = REPO_ROOT / "knowledge" / "eval" / "reviewer-v1"
CASES_DIR = EVAL_SET_DIR / "cases"
MANIFEST_PATH = EVAL_SET_DIR / "manifest.json"

VALID_SPLITS = ("train", "holdout")

# Fixed repo-name -> checkout-path allowlist. `None` is the "resolve to
# whichever repo this code is currently running from" sentinel (the
# pantheon-config case: at Phase-2 run time that's the canonical checkout,
# not necessarily this worktree). Every other entry is a concrete path on
# this box (T-2809 Phase-1b survey: 14 pantheon-config / 7 echo-prismatic /
# 1 pantheon-pwa / 1 pantheon-public cases carry a resolvable sha).
DEFAULT_REPO_ALLOWLIST: dict[str, Path | None] = {
    "pantheon-config": None,
    "echo-prismatic": Path("/home/claude/echo-prismatic"),
    "pantheon-pwa": Path("/home/claude/pantheon-pwa"),
    "pantheon-public": Path("/home/claude/pantheon-public"),
}


class CaseLoadError(Exception):
    """Raised when a committed case file or the manifest fails to parse.

    This is a systemic-failure signal (a malformed frozen artifact), not a
    per-case tolerated condition — unlike diff resolution, which degrades
    gracefully.
    """


@dataclass(frozen=True)
class DiffRef:
    repo: str | None
    sha: str | None


@dataclass(frozen=True)
class Finding:
    category: str
    description: str

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Finding":
        return cls(category=d["category"], description=d["description"])


@dataclass(frozen=True)
class ReviewMaterial:
    ask_summary: str
    diff_ref: DiffRef
    review_kind: str


@dataclass(frozen=True)
class Expected:
    verdict: str
    findings: tuple[Finding, ...]
    must_not_flag: tuple[Finding, ...]


@dataclass(frozen=True)
class Case:
    case_id: str
    case_class: str  # JSON field "class"; renamed to dodge the keyword
    split: str
    source_ticket: str
    source_path: str
    provenance: Mapping[str, Any]
    review_material: ReviewMaterial
    expected: Expected
    curation: Mapping[str, Any]

    @classmethod
    def from_json_dict(cls, d: Mapping[str, Any]) -> "Case":
        rm = d["review_material"]
        diff_ref_raw = rm.get("diff_ref") or {}
        exp = d["expected"]
        return cls(
            case_id=d["case_id"],
            case_class=d["class"],
            split=d["split"],
            source_ticket=d["source_ticket"],
            source_path=d["source_path"],
            provenance=d.get("provenance", {}),
            review_material=ReviewMaterial(
                ask_summary=rm["ask_summary"],
                diff_ref=DiffRef(repo=diff_ref_raw.get("repo"), sha=diff_ref_raw.get("sha")),
                review_kind=rm.get("review_kind", "diff"),
            ),
            expected=Expected(
                verdict=exp["verdict"],
                findings=tuple(Finding.from_dict(f) for f in exp.get("findings", [])),
                must_not_flag=tuple(Finding.from_dict(f) for f in exp.get("must_not_flag", [])),
            ),
            curation=d.get("curation", {}),
        )


def load_manifest(eval_set_dir: Path = EVAL_SET_DIR) -> dict:
    """Parse and return manifest.json. Raises CaseLoadError on any failure."""
    manifest_path = eval_set_dir / "manifest.json"
    try:
        with manifest_path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseLoadError(f"failed to load manifest at {manifest_path}: {exc}") from exc


def load_all_cases(eval_set_dir: Path = EVAL_SET_DIR) -> dict[str, Case]:
    """Parse every `cases/*.json` file. Raises CaseLoadError on any malformed file.

    The frozen eval set is a small (dozens), hand-curated, ratification-gated
    corpus — unlike tools/gepa_eval_extract.py's tolerant WARN-and-skip
    posture over a large uncurated ticket tree, a malformed case here means
    the committed artifact itself is broken and the caller should stop, not
    silently continue with a partial set.
    """
    cases_dir = eval_set_dir / "cases"
    if not cases_dir.is_dir():
        raise CaseLoadError(f"cases directory not found: {cases_dir}")
    cases: dict[str, Case] = {}
    for path in sorted(cases_dir.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as fh:
                raw = json.load(fh)
            case = Case.from_json_dict(raw)
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise CaseLoadError(f"failed to parse case file {path}: {exc}") from exc
        cases[case.case_id] = case
    return cases


def load_cases(split: str, *, eval_set_dir: Path = EVAL_SET_DIR) -> dict[str, Case]:
    """Return only the cases whose `split` matches. `split` must be 'train' or 'holdout'.

    This is the scoping seam: a caller building an optimization trainset that
    only ever calls `load_cases("train")` cannot hand a holdout case to the
    loop by construction.
    """
    if split not in VALID_SPLITS:
        raise ValueError(f"split must be one of {VALID_SPLITS}, got {split!r}")
    return {cid: case for cid, case in load_all_cases(eval_set_dir).items() if case.split == split}


def resolve_diff(
    case: Case,
    *,
    repo_allowlist: Mapping[str, Path | None] | None = None,
    self_repo_root: Path = REPO_ROOT,
    timeout: float = 10.0,
) -> str | None:
    """Return the diff text for `case`, or None (narrative-only fallback).

    None is returned — never raised — for: no diff_ref, no sha, repo not in
    the allowlist, the allowlist's checkout missing on this box, or `git
    show` itself failing (dead sha, corrupt object, etc). This function never
    builds a path from case-controlled input beyond looking it up in the
    fixed allowlist.
    """
    ref = case.review_material.diff_ref
    if ref.repo is None or ref.sha is None:
        return None

    allowlist = repo_allowlist if repo_allowlist is not None else DEFAULT_REPO_ALLOWLIST
    if ref.repo not in allowlist:
        return None

    repo_path = allowlist[ref.repo]
    if repo_path is None:
        repo_path = self_repo_root

    if not (repo_path / ".git").exists():
        return None

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "show", ref.sha],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None
    return result.stdout
