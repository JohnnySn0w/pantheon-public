"""Strict YAML loading for the registry: safe_load semantics, duplicate keys refused.

Stock ``yaml.safe_load`` silently keeps the last of two equal mapping keys, which
would let a second ``status:`` in a cell override the first unseen. A closed
schema has to see both, so this loader raises on any duplicate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class RegistryLoadError(ValueError):
    """The file could not be read or parsed as strict YAML."""


class _StrictLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as exc:
            raise RegistryLoadError(f"unhashable mapping key at line {key_node.start_mark.line + 1}") from exc
        if key in mapping:
            raise RegistryLoadError(
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def loads(text: str, source: str = "<string>") -> Any:
    try:
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except RegistryLoadError as exc:
        raise RegistryLoadError(f"{source}: {exc}") from None
    except yaml.YAMLError as exc:
        raise RegistryLoadError(f"{source}: not valid YAML ({exc})") from None


def load_file(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryLoadError(f"{path}: cannot read ({exc.strerror})") from None
    return loads(text, source=str(path))
