#!/usr/bin/env python3
"""Validate public architecture diagrams as bounded derivatives of canon.

The public Excalidraw scenes are deliberately not a second architecture
schema.  Their manifest pins each canonical source, classifies every visible
text element, and preserves the graph's semantic IDs and bindings.  A canonical
edit therefore turns this check red until someone consciously refreshes and
reviews the public derivative.

Exit codes: 0 = all derivatives valid; 1 = one or more findings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "reports" / "PR" / "architecture" / "public-concepts.json"
TEXT_ACTIONS = frozenset({"translated", "kept", "removed"})


def load_manifest(path: Path) -> Mapping[str, object]:
    """Load a JSON derivation manifest."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest root must be an object")
    return value


def load_scene(path: Path) -> Mapping[str, object]:
    """Load an Excalidraw JSON scene."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("scene root must be an object")
    elements = value.get("elements")
    if not isinstance(elements, list):
        raise ValueError("scene must contain an elements array")
    return value


def _live_elements(scene: Mapping[str, object]) -> list[Mapping[str, object]]:
    elements = scene.get("elements", [])
    return [
        element
        for element in elements  # type: ignore[union-attr]
        if isinstance(element, dict) and not element.get("isDeleted", False)
    ]


def visible_text_by_id(scene: Mapping[str, object]) -> dict[str, str]:
    """Return every non-deleted text element keyed by stable element ID."""
    out: dict[str, str] = {}
    for element in _live_elements(scene):
        if element.get("type") != "text":
            continue
        element_id = element.get("id")
        text = element.get("text")
        if isinstance(element_id, str) and isinstance(text, str):
            out[element_id] = text
    return out


def arrow_bindings(
    scene: Mapping[str, object],
) -> dict[str, tuple[str | None, str | None]]:
    """Return semantic start/end element IDs for every live arrow."""
    out: dict[str, tuple[str | None, str | None]] = {}
    for element in _live_elements(scene):
        if element.get("type") != "arrow" or not isinstance(element.get("id"), str):
            continue
        start = element.get("startBinding")
        end = element.get("endBinding")
        start_id = start.get("elementId") if isinstance(start, dict) else None
        end_id = end.get("elementId") if isinstance(end, dict) else None
        out[str(element["id"])] = (
            start_id if isinstance(start_id, str) else None,
            end_id if isinstance(end_id, str) else None,
        )
    return out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path(spec: Mapping[str, object], key: str, repo_root: Path) -> Path:
    raw = spec.get(key)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{key} must be a non-empty repository-relative path")
    path = (repo_root / raw).resolve()
    try:
        path.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ValueError(f"{key} escapes repository root: {raw}") from exc
    return path


def validate_source_pin(
    spec: Mapping[str, object], repo_root: Path
) -> list[str]:
    """Require the canonical scene bytes to match the reviewed source pin."""
    try:
        source = _path(spec, "canonical", repo_root)
    except ValueError as exc:
        return [str(exc)]
    expected = spec.get("canonical_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        return ["canonical_sha256 must be 64 lowercase hexadecimal characters"]
    try:
        actual = _sha256(source)
    except OSError as exc:
        return [f"cannot read canonical source {source}: {exc}"]
    if actual != expected:
        return [
            f"canonical source drift: {source.relative_to(repo_root)} "
            f"expected {expected}, found {actual}"
        ]
    return []


def validate_text_classification(
    spec: Mapping[str, object], canonical: Mapping[str, object]
) -> list[str]:
    """Require every canonical visible-text ID to be classified exactly once."""
    findings: list[str] = []
    actual_ids = set(visible_text_by_id(canonical))
    raw = spec.get("text_classification")
    if not isinstance(raw, dict):
        return ["text_classification must be an object"]
    classified_ids = {key for key in raw if isinstance(key, str)}
    missing = sorted(actual_ids - classified_ids)
    extra = sorted(classified_ids - actual_ids)
    if missing:
        findings.append(f"unclassified canonical text IDs: {', '.join(missing)}")
    if extra:
        findings.append(f"classification references absent text IDs: {', '.join(extra)}")
    for element_id, action in raw.items():
        if not isinstance(element_id, str) or action not in TEXT_ACTIONS:
            findings.append(
                f"invalid text classification {element_id!r}: expected one of "
                f"{sorted(TEXT_ACTIONS)}"
            )
    return findings


def validate_public_vocabulary(
    spec: Mapping[str, object], public: Mapping[str, object]
) -> list[str]:
    """Check public text presence, Excalidraw text parity, and forbidden terms."""
    findings: list[str] = []
    public_text = visible_text_by_id(public)
    classifications = spec.get("text_classification", {})
    if not isinstance(classifications, dict):
        return ["text_classification must be an object"]

    for element_id, action in classifications.items():
        if not isinstance(element_id, str) or action not in TEXT_ACTIONS:
            continue
        if action == "removed":
            if element_id in public_text:
                findings.append(f"removed text ID remains public: {element_id}")
        elif element_id not in public_text:
            findings.append(f"public visible-text ID missing: {element_id}")

    for element in _live_elements(public):
        if element.get("type") != "text":
            continue
        element_id = str(element.get("id", "<missing-id>"))
        text = element.get("text")
        original = element.get("originalText")
        if not isinstance(text, str) or not isinstance(original, str):
            findings.append(f"public text element lacks string text/originalText: {element_id}")
        elif text != original:
            findings.append(f"public text/originalText diverge: {element_id}")

    combined = "\n".join(public_text.values())
    patterns = spec.get("forbidden_visible_patterns", [])
    if not isinstance(patterns, list):
        findings.append("forbidden_visible_patterns must be an array")
        return findings
    for pattern in patterns:
        if not isinstance(pattern, str):
            findings.append(f"forbidden pattern must be a string: {pattern!r}")
            continue
        try:
            match = re.search(pattern, combined, flags=re.IGNORECASE)
        except re.error as exc:
            findings.append(f"invalid forbidden pattern {pattern!r}: {exc}")
            continue
        if match:
            findings.append(
                f"forbidden internal vocabulary in public text: pattern {pattern!r} "
                f"matched {match.group(0)!r}"
            )
    return findings


def _bound_elements(element: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    raw = element.get("boundElements")
    if not isinstance(raw, list):
        return ()
    values: list[tuple[str, str]] = []
    for binding in raw:
        if not isinstance(binding, dict):
            continue
        element_id = binding.get("id")
        kind = binding.get("type")
        if isinstance(element_id, str) and isinstance(kind, str):
            values.append((element_id, kind))
    return tuple(sorted(values))


def validate_topology_parity(
    canonical: Mapping[str, object], public: Mapping[str, object],
    topology_exceptions: Sequence[str] = (),
) -> list[str]:
    """Preserve semantic element IDs, types, containers, and arrow endpoints."""
    findings: list[str] = []
    canonical_by_id = {
        str(element["id"]): element
        for element in _live_elements(canonical)
        if isinstance(element.get("id"), str)
    }
    public_by_id = {
        str(element["id"]): element
        for element in _live_elements(public)
        if isinstance(element.get("id"), str)
    }
    allowed = set(topology_exceptions)

    missing = sorted(set(canonical_by_id) - set(public_by_id) - allowed)
    added = sorted(set(public_by_id) - set(canonical_by_id) - allowed)
    if missing:
        findings.append(f"public scene missing semantic element IDs: {', '.join(missing)}")
    if added:
        findings.append(f"public scene added semantic element IDs: {', '.join(added)}")

    for element_id in sorted((set(canonical_by_id) & set(public_by_id)) - allowed):
        source = canonical_by_id[element_id]
        target = public_by_id[element_id]
        if source.get("type") != target.get("type"):
            findings.append(
                f"element type drift for {element_id}: "
                f"{source.get('type')!r} -> {target.get('type')!r}"
            )
        if source.get("containerId") != target.get("containerId"):
            findings.append(f"container binding drift for {element_id}")
        if _bound_elements(source) != _bound_elements(target):
            findings.append(f"boundElements drift for {element_id}")

    source_arrows = arrow_bindings(canonical)
    public_arrows = arrow_bindings(public)
    for arrow_id in sorted((set(source_arrows) | set(public_arrows)) - allowed):
        if source_arrows.get(arrow_id) != public_arrows.get(arrow_id):
            findings.append(
                f"arrow binding drift for {arrow_id}: "
                f"{source_arrows.get(arrow_id)!r} -> {public_arrows.get(arrow_id)!r}"
            )
    return findings


def _view_label(spec: Mapping[str, object], index: int) -> str:
    view_id = spec.get("id")
    return str(view_id) if isinstance(view_id, str) and view_id else f"view[{index}]"


def validate_all(manifest_path: Path, repo_root: Path) -> list[str]:
    """Validate every declared public architecture derivative."""
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"manifest load failed: {exc}"]

    views = manifest.get("views")
    if not isinstance(views, list) or not views:
        return ["manifest views must be a non-empty array"]
    global_patterns = manifest.get("forbidden_visible_patterns", [])
    if not isinstance(global_patterns, list) or not all(
        isinstance(value, str) for value in global_patterns
    ):
        return ["manifest forbidden_visible_patterns must be an array of strings"]

    findings: list[str] = []
    for index, raw_spec in enumerate(views):
        if not isinstance(raw_spec, dict):
            findings.append(f"view[{index}]: view spec must be an object")
            continue
        spec: dict[str, Any] = dict(raw_spec)
        local_patterns = spec.get("forbidden_visible_patterns", [])
        if not isinstance(local_patterns, list) or not all(
            isinstance(value, str) for value in local_patterns
        ):
            findings.append(
                f"{_view_label(spec, index)}: forbidden_visible_patterns must be "
                "an array of strings"
            )
            local_patterns = []
        spec["forbidden_visible_patterns"] = [
            *global_patterns,
            *local_patterns,
        ]
        label = _view_label(spec, index)
        for finding in validate_source_pin(spec, repo_root):
            findings.append(f"{label}: {finding}")

        try:
            canonical_path = _path(spec, "canonical", repo_root)
            public_path = _path(spec, "public", repo_root)
            canonical = load_scene(canonical_path)
            public = load_scene(public_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            findings.append(f"{label}: scene load failed: {exc}")
            continue

        for finding in validate_text_classification(spec, canonical):
            findings.append(f"{label}: {finding}")
        for finding in validate_public_vocabulary(spec, public):
            findings.append(f"{label}: {finding}")
        exceptions = spec.get("topology_exceptions", [])
        if not isinstance(exceptions, list) or not all(
            isinstance(value, str) for value in exceptions
        ):
            findings.append(f"{label}: topology_exceptions must be an array of IDs")
            exceptions = []
        for finding in validate_topology_parity(canonical, public, exceptions):
            findings.append(f"{label}: {finding}")
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST,
        help="derivation manifest (default: reports/PR/architecture/public-concepts.json)",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=REPO_ROOT,
        help="repository root used to resolve manifest paths",
    )
    args = parser.parse_args(argv)

    findings = validate_all(args.manifest, args.repo_root)
    if findings:
        print(f"public architecture derivative check failed ({len(findings)}):")
        for finding in findings:
            print(f"  - {finding}")
        return 1
    print("public architecture derivatives: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
