"""`germinate registry repin <id> [--check]`: recompute a component's pins in cascade (T-5477).

An edit moves up to four pins, in order:

1. the contract's ``invariant_list_digest`` (``engine/invariant_digest.py``, over
   the ``<id>.invariants.yaml`` beside the contract; only when the contract
   already declares the line, never added; ``invariant_list_id`` is never
   rewritten, a mismatch is a refusal), and with it every
   ``invariant_list_ref: <id>@<digest>`` line (``legs.verify``, ``emitter``;
   ORG-081 D-11(b) requires them to equal the binding; every such line is examined, a ref of any other form
   (odd value, wrong id) is a refusal, a trailing comment is kept). The parsed
   contract is then cross-checked: every ``invariant_list_ref`` key the engine
   could read under ``legs.verify`` / ``emitter`` must be one the line scan
   examined, else an unsupported YAML form (``key :``, quoted key, flow style,
   list item) is a refusal (T-6075);
2. the contract's ``handlers_digest`` (``engine/handlers_digest.py``, over
   ``<id>_handlers.py`` and its component-local helpers);
3. the contract's own ``contract_digest`` (``registry/contract_digest.py``, the
   sole oracle; it covers steps 1 and 2's fields);
4. the registry row's ``contract.digest`` in ``registry/systems.yaml``.

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
from tools.germinate.engine import invariant_digest as ID
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
    refs_seen: int = 0  # invariant_list_ref key lines examined (a guard: none may be skipped)

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


_REF_LINE = re.compile(r"^(?P<indent>[ ]*)invariant_list_ref:(?P<rest>[^\n]*)$", flags=re.MULTILINE)
# What may follow the key: optional matching quotes round the value, then an optional `  # comment`.
_REF_REST = re.compile(r"^[ ]*(?P<q>['\"]?)(?P<value>[^\s'\"#]+)(?P=q)(?:[ ]+#.*)?[ ]*$")


def _ref_label(text: str, match: re.Match[str]) -> str:
    """Dotted key path of the block holding a ref line (``legs.verify``, ``emitter``), from indentation."""

    lines = text[: match.start()].split("\n")
    indent, parts = len(match.group("indent")), []
    for line in reversed(lines):
        key = re.match(r"^([ ]*)([A-Za-z_][\w-]*):[ ]*(?:#.*)?$", line)
        if key and len(key.group(1)) < indent:
            parts.append(key.group(2))
            indent = len(key.group(1))
            if indent == 0:
                break
    return ".".join(reversed(parts[:-1] if parts and parts[-1] == "component" else parts)) or "?"


def _rewrite_refs(text: str, list_id: str, want: str, result: "RepinResult", where: str) -> str:
    """Rewrite every ``invariant_list_ref`` to ``<list_id>@<want>``; a ref of another form is a refusal."""

    expected = f"{list_id}@{want}"
    prefix = f"{list_id}@"
    first = len(result.drift)
    for match in reversed(list(_REF_LINE.finditer(text))):
        label = _ref_label(text, match)
        result.refs_seen += 1
        strict = _REF_REST.match(match.group("rest"))
        if strict is None:
            raise RepinError(f"{where}: {label}.invariant_list_ref {match.group('rest').strip()!r} is not a plain `<id>@sha256:<64 hex>` value")
        value = strict.group("value")
        if not (value.startswith(prefix) and S.DIGEST_RE.match(value[len(prefix) :])):
            raise RepinError(f"{where}: {label}.invariant_list_ref {value!r} is not of the form {prefix}sha256:<64 hex>")
        if value != expected:
            result.drift.append(f"{label}.invariant_list_ref: {value} -> {expected}")
            lo, hi = match.start("rest") + strict.start("value"), match.start("rest") + strict.end("value")
            text = text[:lo] + expected + text[hi:]  # quotes and any trailing comment stay verbatim
    result.drift[first:] = reversed(result.drift[first:])  # file order (the scan runs last-to-first)
    return text


def _count_ref_keys(node: Any) -> int:
    """``invariant_list_ref`` keys anywhere inside ``node`` (dicts and lists, recursively)."""

    if isinstance(node, dict):
        return sum((k == "invariant_list_ref") + _count_ref_keys(v) for k, v in node.items())
    if isinstance(node, list):
        return sum(_count_ref_keys(v) for v in node)
    return 0


def _check_refs_all_seen(text: str, result: "RepinResult", where: str) -> None:
    """Refuse when the parsed contract holds a ref the line scan did not examine (T-6075).

    The engine reads ``legs.verify.invariant_list_ref`` and ``emitter.invariant_list_ref``.
    Keys are counted anywhere inside those two blocks, deliberately including a list-item
    block (``verify: [- invariant_list_ref: x]``): the engine would not read it as a ref, but
    such a contract is wrong under either reading, so a loud refusal is the fail-closed outcome.
    A parse failure is also a refusal.
    """

    try:
        doc = loads(text, where)
    except RegistryLoadError as exc:
        raise RepinError(str(exc)) from None
    body = doc.get("component") if isinstance(doc, dict) else None
    if not isinstance(body, dict):
        raise RepinError(f"{where}: no `component:` mapping to check invariant_list_ref keys in")
    legs = body.get("legs")
    verify = legs.get("verify") if isinstance(legs, dict) else None
    parsed = _count_ref_keys(verify) + _count_ref_keys(body.get("emitter"))
    if parsed != result.refs_seen:
        raise RepinError(
            f"{where}: {parsed} refs in the parsed contract, {result.refs_seen} examined by the line scan; "
            "an unsupported YAML form, rewrite it in block style"
        )


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

    declared = _field(text, "invariant_list_digest", where)
    if declared is not None:
        list_id_line = _field(text, "invariant_list_id", where)
        if list_id_line is None:
            raise RepinError(f"{where}: invariant_list_digest declared without invariant_list_id")
        try:
            want_list, list_id = ID.compute(module_path)
        except ID.InvariantDigestError as exc:
            raise RepinError(str(exc)) from None
        if str(list_id) != list_id_line.group("value").strip("'\""):
            raise RepinError(
                f"{where}: invariant_list_id {list_id_line.group('value')!r} != the list file's id {list_id!r}; "
                "an id change is a human decision"
            )
        if declared.group("value") != want_list:
            result.drift.append(f"invariant_list_digest: {declared.group('value')} -> {want_list}")
            text = _set(text, declared, want_list)
        text = _rewrite_refs(text, str(list_id), want_list, result, where)
        _check_refs_all_seen(text, result, where)

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
