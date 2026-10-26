"""ORG-081 D-10 lint: the machine-readable ADR <-> registry link.

Every ``architecture/adr/ORG-*.md`` carries EITHER ``registers_components:
[<id>, ...]`` (each id a ``kind: component`` registry row) OR
``no_registry_row_waiver: <string>``, never both.

Two modes, wired like ``tools/adr_scoping_lint.py``:

* tree mode (state check): every ORG ADR that carries either field must carry
  it well-formed, and every registered id must resolve against the registry.
  An ADR carrying neither is not flagged: existing ADRs are grandfathered.
* diff mode (creation check): a newly-ADDED ORG ADR must carry exactly one of
  the two fields (D-10 "landing" = newly added, per the T-5073 plan reading).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from tools.adr_reeval_walk import _split_frontmatter
from tools.germinate.registry import schema as S
from tools.germinate.registry.load import RegistryLoadError, loads
from tools.germinate.registry.validate import ADR_DIR_REL, ERROR, Finding
from tools.hermes_diff_scan import iter_added_lines

ORG_ADR_PATH_RE = re.compile(r"^architecture/adr/(ORG-\d+)[^/]*\.md$")
REGISTERS = "registers_components"
WAIVER = "no_registry_row_waiver"



def component_ids(registry_doc: Mapping) -> set[str]:
    return {
        row["id"]
        for row in registry_doc.get("systems") or []
        if isinstance(row, dict) and row.get("kind") == S.KIND_COMPONENT and isinstance(row.get("id"), str)
    }


def check_frontmatter(
    adr_id: str,
    where: str,
    fm: Mapping[str, Any],
    ids: set[str],
    *,
    require_field: bool,
) -> list[Finding]:
    findings: list[Finding] = []
    has_reg, has_waiver = REGISTERS in fm, WAIVER in fm
    if has_reg and has_waiver:
        findings.append(Finding(ERROR, where, f"{REGISTERS} and {WAIVER} are mutually exclusive (ORG-081 D-10)"))
    if not has_reg and not has_waiver:
        if require_field:
            findings.append(
                Finding(ERROR, where, f"a new ORG ADR must carry {REGISTERS}: [...] or {WAIVER}: <reason> (ORG-081 D-10)")
            )
        return findings
    if has_waiver:
        waiver = fm[WAIVER]
        if not (isinstance(waiver, str) and waiver.strip()):
            findings.append(Finding(ERROR, where, f"{WAIVER} must be a non-empty string"))
    if has_reg:
        value = fm[REGISTERS]
        if not isinstance(value, list):
            findings.append(Finding(ERROR, where, f"{REGISTERS} must be a list (use [] for none), got {value!r}"))
            return findings
        for item in value:
            if not isinstance(item, str):
                findings.append(Finding(ERROR, where, f"{REGISTERS} entry {item!r} is not a string id"))
            elif item not in ids:
                findings.append(
                    Finding(ERROR, where, f"{REGISTERS} names {item!r}, which has no kind: component registry row")
                )
    return findings


def _parse(where: str, text: str) -> tuple[dict | None, Finding | None]:
    fm_block, _body = _split_frontmatter(text)
    if not fm_block.strip():
        return {}, None
    try:
        data = loads(fm_block, source=where) or {}
    except RegistryLoadError as exc:
        return None, Finding(ERROR, where, f"frontmatter is not strict YAML ({exc})")
    if not isinstance(data, dict):
        return None, Finding(ERROR, where, "frontmatter is not a mapping")
    return data, None


def scan_tree(repo_root: Path, registry_doc: Mapping) -> list[Finding]:
    ids = component_ids(registry_doc)
    findings: list[Finding] = []
    for path in sorted((repo_root / ADR_DIR_REL).glob("ORG-*.md")):
        rel = f"{ADR_DIR_REL}/{path.name}"
        m = ORG_ADR_PATH_RE.match(rel)
        if not m:
            continue
        fm, problem = _parse(rel, path.read_text(encoding="utf-8"))
        if problem:
            findings.append(problem)
            continue
        findings += check_frontmatter(m.group(1), rel, fm, ids, require_field=False)
    return findings


def scan_diff(diff_text: "str | bytes", registry_doc: Mapping) -> list[Finding]:
    if isinstance(diff_text, str):
        diff_text = diff_text.encode("utf-8", "surrogateescape")
    files: dict[str, list[bytes]] = {}
    for ev in iter_added_lines(diff_text):
        if ev.is_new_file and ev.path is not None and ORG_ADR_PATH_RE.match(ev.path):
            text = ev.text[:-1] if ev.text.endswith(b"\r") else ev.text
            files.setdefault(ev.path, []).append(text)
    ids = component_ids(registry_doc)
    findings: list[Finding] = []
    for path, lines in files.items():
        fm, problem = _parse(path, b"\n".join(lines).decode("utf-8", "replace"))
        if problem:
            findings.append(problem)
            continue
        adr_id = ORG_ADR_PATH_RE.match(path).group(1)
        findings += check_frontmatter(adr_id, path, fm, ids, require_field=True)
    return findings
