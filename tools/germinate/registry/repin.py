"""`germinate registry repin <id> [--check]`: recompute a component's pins in cascade (T-5477).

A handlers edit moves three pins, in order:

1. the contract's ``handlers_digest`` (``engine/handlers_digest.py``, over
   ``<id>_handlers.py`` and its component-local helpers);
2. the contract's own ``contract_digest`` (``registry/contract_digest.py``, the
   sole oracle; it covers step 1's field);
3. the registry row's ``contract.digest`` in ``registry/systems.yaml``.

Each value is rewritten in place on its own line (comments and layout survive).
``--check`` writes nothing and reports drift (exit 1), so CI and a developer can
ask "is this component pinned?" with one command. This tool edits the working
tree only; it never commits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from tools.germinate.engine import handlers_digest as HD
from tools.germinate.engine.contract import ContractError, contained_module_path, handlers_path_for
from tools.germinate.registry import schema as S
from tools.germinate.registry.contract_digest import ContractDigestError, digest_document
from tools.germinate.registry.load import RegistryLoadError, loads

_FIELD_LINE = r"^(?P<indent>[ ]+){name}:[ ]*(?P<value>\S+)[ ]*$"


class RepinError(ValueError):
    """The component cannot be re-pinned (not found, unpinned row, unparseable, unpinnable)."""


@dataclass
class RepinResult:
    component: str
    contract_path: Path
    registry_path: Path
    drift: list[str] = field(default_factory=list)
    contract_text: str = ""
    registry_text: str = ""

    def write(self) -> None:
        """Write both texts (the contract first: a crash between leaves only the registry stale, which validate reds)."""

        self.contract_path.write_text(self.contract_text, encoding="utf-8")
        self.registry_path.write_text(self.registry_text, encoding="utf-8")


def _field(text: str, name: str, where: str) -> re.Match[str] | None:
    found = list(re.finditer(_FIELD_LINE.format(name=re.escape(name)), text, flags=re.MULTILINE))
    if len(found) > 1:
        raise RepinError(f"{where}: more than one `{name}:` line")
    return found[0] if found else None


def _set(text: str, match: re.Match[str], value: str) -> str:
    return text[: match.start("value")] + value + text[match.end("value") :]


def _row(doc: Any, cid: str) -> Mapping[str, Any]:
    rows = [r for r in (doc.get("systems") or []) if isinstance(r, dict)] if isinstance(doc, dict) else []
    for row in rows:
        if row.get("id") == cid:
            contract = row.get("contract")
            if row.get("kind") != S.KIND_COMPONENT or not isinstance(contract, dict) or contract.get("repo") != S.INTERNAL_REPO:
                raise RepinError(f"{cid}: not an internal component row")
            if not S.DIGEST_RE.match(str(contract.get("digest"))):
                raise RepinError(f"{cid}: the registry row is {contract.get('digest')!r}; repin only maintains pinned rows")
            return row
    raise RepinError(f"{cid}: no such registry row")


def repin(repo_root: Path, registry_path: Path, cid: str) -> RepinResult:
    """Compute the re-pinned contract and registry texts (nothing is written here)."""

    registry_text = registry_path.read_text(encoding="utf-8")
    try:
        row = _row(loads(registry_text, str(registry_path)), cid)
        module_path = contained_module_path(repo_root, row["contract"]["module"])
    except (RegistryLoadError, ContractError) as exc:
        raise RepinError(str(exc)) from None
    where = str(row["contract"]["module"])
    try:
        text = module_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RepinError(f"{where}: unreadable: {exc}") from None
    result = RepinResult(cid, module_path, registry_path)

    handlers = handlers_path_for(module_path)
    current = _field(text, "handlers_digest", where)
    if handlers.exists() or handlers.is_symlink():
        try:
            want = HD.compute(handlers).digest
        except HD.HandlersDigestError as exc:
            raise RepinError(f"{handlers.name}: {exc}") from None
        if current is None:
            anchor = _field(text, "contract_digest", where)
            if anchor is None:
                raise RepinError(f"{where}: no `contract_digest:` line to anchor handlers_digest under")
            line_end = text.index("\n", anchor.end()) + 1 if "\n" in text[anchor.end() :] else len(text)
            text = text[:line_end] + f"{anchor.group('indent')}handlers_digest: {want}\n" + text[line_end:]
            result.drift.append(f"handlers_digest: (missing) -> {want}")
        elif current.group("value") != want:
            result.drift.append(f"handlers_digest: {current.group('value')} -> {want}")
            text = _set(text, current, want)
    elif current is not None:
        raise RepinError(f"{where}: declares handlers_digest but {handlers.name} does not exist; remove the line by hand")

    anchor = _field(text, "contract_digest", where)
    if anchor is None:
        raise RepinError(f"{where}: no `contract_digest:` line")
    try:
        contract_digest = digest_document(loads(text, where), source=where)
    except (RegistryLoadError, ContractDigestError) as exc:
        raise RepinError(str(exc)) from None
    if anchor.group("value") != contract_digest:
        result.drift.append(f"contract_digest: {anchor.group('value')} -> {contract_digest}")
        text = _set(text, anchor, contract_digest)
    result.contract_text = text

    old = row["contract"]["digest"]
    pattern = re.compile(
        r"(module:[ ]*" + re.escape(row["contract"]["module"]) + r"[ ]*\n[ ]+digest:[ ]*)" + re.escape(old) + r"(?=[ ]*$)",
        flags=re.MULTILINE,
    )
    hits = pattern.findall(registry_text)
    if len(hits) != 1:
        raise RepinError(f"{registry_path.name}: cannot locate {cid}'s `module:` + `digest:` lines uniquely")
    if old != contract_digest:
        result.drift.append(f"registry {cid}.contract.digest: {old} -> {contract_digest}")
        registry_text = pattern.sub(lambda m: m.group(1) + contract_digest, registry_text)
    result.registry_text = registry_text
    return result


def pinned_components(registry_path: Path) -> list[str]:
    """Every internal, pinned component row id (the default set for ``repin --check``)."""

    doc = loads(registry_path.read_text(encoding="utf-8"), str(registry_path))
    rows = [r for r in (doc.get("systems") or []) if isinstance(r, dict)] if isinstance(doc, dict) else []
    return sorted(
        str(r.get("id"))
        for r in rows
        if r.get("kind") == S.KIND_COMPONENT
        and isinstance(r.get("contract"), dict)
        and r["contract"].get("repo") == S.INTERNAL_REPO
        and S.DIGEST_RE.match(str(r["contract"].get("digest")))
    )
