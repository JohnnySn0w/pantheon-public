"""T-5496: refuse a ticket close that would turn `registry validate` red.

registry/systems.yaml cells (legs / legacy_coverage / adoption_gaps) cite the OPEN
ticket that closes a gap; the validator errors once that ticket is in done/. This
module applies the validator's own predicate (`gap_cells_citing`) at close time,
and, given a successor ticket, re-cites those cells with a minimal textual edit
(no YAML round-trip) and re-renders registry/system-coverage.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from tools.germinate.registry import render
from tools.germinate.registry.load import RegistryLoadError, loads
from tools.germinate.registry.validate import REGISTRY_REL, gap_cells_citing


class RecitationError(Exception):
    """The successor re-cite/re-render could not be planned safely."""


@dataclass
class RegistryEdit:
    cells: list[str]
    registry_path: Path
    new_registry_text: str | None = None
    rendered_path: Path | None = None
    new_rendered_text: str | None = None


def _scalar_spans(text: str, cells: list[str], ticket: str) -> list[tuple[int, int]]:
    """(start, end) character spans of the `ticket:` scalar for each `rid.block.key` cell."""
    root = yaml.compose(text, Loader=yaml.SafeLoader)

    def get(node, key):
        if not isinstance(node, yaml.MappingNode):
            return None
        for k, v in node.value:
            if getattr(k, "value", None) == key:
                return v
        return None

    spans = []
    systems = get(root, "systems")
    for where in cells:
        rid, block, key = where.split(".", 2)
        for row in systems.value if isinstance(systems, yaml.SequenceNode) else []:
            idn = get(row, "id")
            if idn is None or idn.value != rid:
                continue
            node = get(get(get(row, block), key), "ticket")
            if node is not None and node.value == ticket:
                spans.append((node.start_mark.index, node.end_mark.index))
    if len(spans) != len(cells):
        raise ValueError(f"could not locate every cell citing {ticket} in the registry text")
    return spans


def _recite(text: str, spans: list[tuple[int, int]], successor: str) -> str:
    for start, end in sorted(spans, reverse=True):
        quote = text[start] if text[start] in "'\"" else ""
        text = text[:start] + quote + successor + quote + text[end:]
    return text


def plan_registry_edit(repo_root: Path, tid: str, successor: str | None) -> RegistryEdit | None:
    """None when `tid` owns no gap. Otherwise the cells; with `successor`, also the new file texts.

    An absent or unparseable registry is not this gate's business (the validator reports it): None.
    """
    reg = repo_root / REGISTRY_REL
    try:
        text = reg.read_text(encoding="utf-8")
        doc = loads(text, str(reg))
    except (OSError, RegistryLoadError):
        return None
    if not isinstance(doc, dict):
        return None
    cells = gap_cells_citing(doc, tid)
    if not cells:
        return None
    edit = RegistryEdit(cells=cells, registry_path=reg)
    if successor is not None:
        try:
            edit.new_registry_text = _recite(text, _scalar_spans(text, cells, tid), successor)
            edit.rendered_path = repo_root / render.RENDERED_REL
            edit.new_rendered_text = render.render_matrix(loads(edit.new_registry_text, str(reg)))
        except Exception as exc:  # noqa: BLE001 - every planning failure is one refusal
            raise RecitationError(str(exc)) from exc
    return edit


def refusal_message(verb: str, tid: str, cells: list[str]) -> str:
    listing = "\n".join(f"  - {c}" for c in cells)
    return (
        f"{verb} refused: registry/systems.yaml still cites {tid} as the owner of an open gap "
        f"in:\n{listing}\nClosing it would turn `registry validate` red. Re-cite those cells to "
        f"an open ticket first, or pass --successor T-NNNN to do it in this commit."
    )
