"""The handlers-digest oracle (T-5477): pin a component's executor code, verified at discover.

``components.discover()`` imports each component's ``<id>_handlers.py`` and runs
it with seat privilege. ``contract_digest`` covers only the contract YAML, so
this module is the one place the engine, ``load_contract`` and the registry
validator recompute ``handlers_digest`` (a ``component:`` contract field, so
``contract_digest`` and the registry row's pin cover it in turn).

What is digested: the entry file plus every component-local helper it imports,
transitively. A component-local helper is a module ``tools.germinate.components.<name>``,
found by an AST walk of the WHOLE tree, so a function-level (lazy) import counts.
Shared ``tools.*`` modules are out of scope: they are not component-registration
code and have their own review.

The AST walk only sees statically spelled imports, so the same walk refuses the
common dynamic-load forms (``REFUSED_MODULES`` and ``REFUSED_NAMES`` below; the
bare package or an ancestor of it; relative and star imports). The refusal list
lives next to the walk so the two stay in step. It is not exhaustive: a
deliberately obfuscated load (e.g. a ``getattr`` chain to a loader) is left to
the reviewer of the pinned diff, which every handler change must carry.

Scope of the guarantee (T-5506): the digest is verified when ``discover`` runs,
and the entry AND every helper then execute from those exact bytes
(``HandlersDigest.sources``; ``components.py`` resolves component-local imports to
them), so a post-discover edit, ``.pyc`` shadowing, or a ``--repo-root`` checkout
other than the one on ``sys.path`` cannot substitute helper code. Shared ``tools.*``
modules remain the accepted shared-module residual. The spelling
``import tools.germinate.components.<x>`` is refused: it would bind the name ``tools``.

Canonical form (``pantheon-handlers-digest/v1``): the raw bytes of each file (no
newline normalization) are hashed; the lines ``<sha256 hex>  <path relative to the
components directory>`` are sorted by path, prefixed with the version line, and
``sha256:<hex>`` of that text is the digest. File names are bound, so a rename
or an added helper changes it.

Every refusal is a ``HandlersDigestError`` (fail-closed).
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

VERSION = "pantheon-handlers-digest/v1"
COMPONENT_PACKAGE = "tools.germinate.components"
MAX_BYTES = 1 << 20

#: Modules a pinned file may not import: each can load or run code the AST walk cannot see.
REFUSED_MODULES = frozenset({"importlib", "runpy", "builtins", "pkgutil", "zipimport", "marshal"})
#: Bare names a pinned file may not use, for the same reason.
REFUSED_NAMES = frozenset({"__import__", "exec", "eval", "compile", "__builtins__"})


class HandlersDigestError(ValueError):
    """The handlers module (or a helper it imports) cannot be pinned."""


@dataclass(frozen=True)
class HandlersDigest:
    digest: str
    entry_source: bytes
    files: tuple[str, ...]
    sources: Mapping[str, bytes] = field(default_factory=dict, compare=False)


def _read(path: Path, label: str) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise HandlersDigestError(f"{label}: not a regular file (symlinks are refused)")
        data = path.read_bytes()
    except OSError as exc:
        raise HandlersDigestError(f"{label}: unreadable: {exc}") from None
    if len(data) > MAX_BYTES:
        raise HandlersDigestError(f"{label}: larger than {MAX_BYTES} bytes")
    return data


def _is_package_or_ancestor(name: str) -> bool:
    """``tools``, ``tools.germinate`` or ``tools.germinate.components``: a handle that reaches helpers by attribute."""

    return COMPONENT_PACKAGE == name or COMPONENT_PACKAGE.startswith(name + ".")


def _bare_package(label: str, lineno: int, name: str) -> str:
    return f"{label}:{lineno}: imports the bare {name!r} package; import the helper module by name ({COMPONENT_PACKAGE}.<name>)"


def _local_imports(data: bytes, label: str) -> set[str]:
    """Component-local module names this file imports; refuses what the walk cannot follow."""

    try:
        tree = ast.parse(data, filename=label)
    except (SyntaxError, ValueError) as exc:
        raise HandlersDigestError(f"{label}: does not parse: {exc}") from None
    prefix = COMPONENT_PACKAGE + "."
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in REFUSED_MODULES:
                    raise HandlersDigestError(f"{label}:{node.lineno}: imports {alias.name!r}, which the handlers pin refuses")
                if _is_package_or_ancestor(alias.name):
                    raise HandlersDigestError(_bare_package(label, node.lineno, alias.name))
                if alias.name.startswith(prefix):
                    raise HandlersDigestError(
                        f"{label}:{node.lineno}: `import {alias.name}` binds the name 'tools'; use `from {COMPONENT_PACKAGE} import <name>`"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise HandlersDigestError(f"{label}:{node.lineno}: relative import; use the absolute {COMPONENT_PACKAGE} path")
            module = node.module or ""
            if module.split(".")[0] in REFUSED_MODULES:
                raise HandlersDigestError(f"{label}:{node.lineno}: imports from {module!r}, which the handlers pin refuses")
            for alias in node.names:  # `from tools.germinate import components [as C]` is the bare package too
                if module != COMPONENT_PACKAGE and alias.name != "*" and _is_package_or_ancestor(f"{module}.{alias.name}"):
                    raise HandlersDigestError(_bare_package(label, node.lineno, f"{module}.{alias.name}"))
            if module == COMPONENT_PACKAGE:
                for alias in node.names:
                    if alias.name == "*":
                        raise HandlersDigestError(f"{label}:{node.lineno}: star import from {COMPONENT_PACKAGE}")
                    found.add(alias.name)
            elif module.startswith(prefix):
                found.add(module[len(prefix):].split(".")[0])
        elif isinstance(node, ast.Name) and node.id in REFUSED_NAMES:
            raise HandlersDigestError(f"{label}:{node.lineno}: uses {node.id!r}, which the handlers pin refuses")
    return found


def compute(handlers_path: Path) -> HandlersDigest:
    """The sole oracle: digest of ``handlers_path`` plus its component-local import closure."""

    handlers_path = Path(handlers_path)
    directory = handlers_path.parent
    entry_source = _read(handlers_path, handlers_path.name)
    sources = {handlers_path.name: entry_source}
    pending = sorted(_local_imports(entry_source, handlers_path.name))
    while pending:
        name = pending.pop()
        rel = f"{name}.py"
        if rel in sources:
            continue
        if not name.isidentifier() or (directory / name).is_dir():
            raise HandlersDigestError(f"{COMPONENT_PACKAGE}.{name}: not a single helper module under {directory.name}/")
        data = _read(directory / rel, rel)
        sources[rel] = data
        pending.extend(sorted(_local_imports(data, rel) - {n.removesuffix('.py') for n in sources}))
    lines = [VERSION] + [f"{hashlib.sha256(sources[rel]).hexdigest()}  {rel}" for rel in sorted(sources)]
    digest = "sha256:" + hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()
    return HandlersDigest(digest=digest, entry_source=entry_source, files=tuple(sorted(sources)), sources=dict(sources))
