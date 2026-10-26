"""Vanish-tolerant recursive tree listing for live-tree scans (T-4450).

``sorted(root.rglob(pattern))`` over a live checkout can raise
``FileNotFoundError`` from INSIDE the generator when a directory disappears
between being listed in its parent and being scanned itself (py3.11 pathlib
swallows only ``PermissionError``). Under pytest-xdist that happens whenever a
sibling test removes a temp dir it created inside the tree, so a born-green
live-tree guard goes red for reasons unrelated to what it checks. A
per-entry ``try/except`` cannot help: the raise comes from the iterator.

``walk_paths`` is a drop-in for ``sorted(root.rglob(pattern))``: same set of
paths (directories and files, dotfiles included, symlinked directories
listed but not descended into), same sort order, but an entry that vanishes
mid-walk is skipped instead of aborting the walk. On a stable tree the
result is identical to ``sorted(root.rglob(pattern))``.
"""

from __future__ import annotations

import os
from fnmatch import fnmatchcase
from pathlib import Path

# A directory that vanished (FileNotFoundError), was replaced by a file
# (NotADirectoryError), or is unreadable (PermissionError, which rglob also
# swallows) is skipped. Any other OSError still propagates.
_TOLERATED = (FileNotFoundError, NotADirectoryError, PermissionError)


def _onerror(exc: OSError) -> None:
    if not isinstance(exc, _TOLERATED):
        raise exc


def walk_paths(root: Path, pattern: str = "*") -> list[Path]:
    """Return ``sorted(root.rglob(pattern))``, tolerating mid-walk vanishes.

    ``pattern`` is a single-component glob matched against each entry's name
    (case-sensitive, as pathlib is on POSIX)."""
    root = Path(root)
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=_onerror, followlinks=False):
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            if fnmatchcase(name, pattern):
                out.append(base / name)
    return sorted(out)
