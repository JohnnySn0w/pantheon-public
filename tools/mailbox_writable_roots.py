"""Resolve manifest-declared mailbox writable roots for harness launch.

This module is intentionally leaf-level so persona_bringup, persona_installer,
and command-sheet generation can share one parser without import cycles.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping


class MailboxWritableRootError(ValueError):
    pass


_PERSONA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_MAILDIR_SUBDIRS = ("tmp", "new", "cur")


@dataclass(frozen=True)
class MailboxWritableRootRef:
    kind: Literal["bound-mailbox", "persona-mailbox"]
    persona: str | None = None

    def logical_key(self) -> tuple[str, str]:
        return (self.kind, self.persona or "")

    def to_manifest(self) -> Mapping[str, str]:
        result: dict[str, str] = {"kind": self.kind}
        if self.persona is not None:
            result["persona"] = self.persona
        return result


@dataclass(frozen=True)
class MailboxWritableRoot:
    ref: MailboxWritableRootRef
    path: Path


def _rooted(root: Path, absolute: Path) -> Path:
    if not absolute.is_absolute():
        raise MailboxWritableRootError(f"mailbox root must be absolute: {absolute}")
    if root == Path("/"):
        return absolute
    return root / absolute.relative_to("/")


def _parse_persona(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _PERSONA_RE.fullmatch(value):
        raise MailboxWritableRootError(f"{where} must be a simple persona name")
    if value in {".", ".."} or "/" in value:
        raise MailboxWritableRootError(f"{where} must not escape the mailbox hub")
    return value


def role_harness_mailbox_root_refs(
    manifest: Mapping[str, Any], *, role: str, harness: str
) -> tuple[MailboxWritableRootRef, ...]:
    runtime = manifest.get("harness_runtime")
    if not isinstance(runtime, Mapping):
        return ()
    roles = runtime.get("roles")
    if not isinstance(roles, Mapping):
        return ()
    role_entry = roles.get(role)
    if not isinstance(role_entry, Mapping):
        return ()
    harness_entry = role_entry.get(harness)
    if not isinstance(harness_entry, Mapping):
        return ()
    raw_refs = harness_entry.get("mailbox_writable_roots", ())
    if raw_refs in (None, ()):
        return ()
    if not isinstance(raw_refs, list):
        raise MailboxWritableRootError(
            f"harness_runtime.roles.{role}.{harness}.mailbox_writable_roots must be a list"
        )
    refs: list[MailboxWritableRootRef] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw_refs):
        where = f"harness_runtime.roles.{role}.{harness}.mailbox_writable_roots[{index}]"
        if not isinstance(item, Mapping):
            raise MailboxWritableRootError(f"{where} must be a mapping")
        kind = item.get("kind")
        if kind == "bound-mailbox":
            if "persona" in item:
                raise MailboxWritableRootError(f"{where}.persona is not valid for bound-mailbox")
            ref = MailboxWritableRootRef("bound-mailbox")
        elif kind == "persona-mailbox":
            ref = MailboxWritableRootRef(
                "persona-mailbox",
                _parse_persona(item.get("persona"), f"{where}.persona"),
            )
        else:
            raise MailboxWritableRootError(f"{where}.kind is unsupported")
        key = ref.logical_key()
        if key in seen:
            raise MailboxWritableRootError(f"{where} duplicates a mailbox writable root ref")
        seen.add(key)
        refs.append(ref)
    return tuple(refs)


def _lexical_under(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _reject_symlink_components(path: Path, *, stop_at: Path) -> None:
    current = path.anchor and Path(path.anchor) or Path("/")
    for part in path.parts[1:]:
        current = current / part
        if not _lexical_under(stop_at, current) and current != stop_at:
            continue
        try:
            if os.path.islink(current):
                raise MailboxWritableRootError(f"mailbox path contains symlink component: {current}")
        except OSError as exc:
            raise MailboxWritableRootError(f"cannot inspect mailbox path component: {current}: {exc}") from exc


def resolve_mailbox_writable_roots(
    manifest: Mapping[str, Any],
    *,
    role: str,
    harness: str,
    bound_persona: str,
    bound_mailbox: Path,
    mailbox_hub: Path,
    root: Path = Path("/"),
    require_existing: bool = False,
) -> tuple[MailboxWritableRoot, ...]:
    refs = role_harness_mailbox_root_refs(manifest, role=role, harness=harness)
    if not refs:
        return ()
    rooted_hub = _rooted(root, mailbox_hub)
    if not rooted_hub.is_absolute():
        raise MailboxWritableRootError("mailbox hub must resolve to an absolute path")
    if require_existing:
        if os.path.islink(rooted_hub):
            raise MailboxWritableRootError("mailbox hub must not be a symlink")
        if not rooted_hub.is_dir():
            raise MailboxWritableRootError("mailbox hub must be an existing directory")
    roots: list[MailboxWritableRoot] = []
    seen_paths: set[Path] = set()
    for ref in refs:
        persona = bound_persona if ref.kind == "bound-mailbox" else ref.persona
        if persona is None:
            raise MailboxWritableRootError("mailbox writable root persona missing")
        target = _rooted(root, mailbox_hub / persona)
        if target != _rooted(root, bound_mailbox) and ref.kind == "bound-mailbox":
            raise MailboxWritableRootError("bound-mailbox does not match binding mailbox")
        if not _lexical_under(rooted_hub, target) or target == rooted_hub:
            raise MailboxWritableRootError("mailbox writable root escapes the mailbox hub")
        if require_existing:
            _reject_symlink_components(target, stop_at=rooted_hub)
            if not target.is_dir():
                raise MailboxWritableRootError("mailbox writable root must be an existing directory")
        if target in seen_paths:
            raise MailboxWritableRootError("mailbox writable root resolves to a duplicate path")
        seen_paths.add(target)
        roots.append(MailboxWritableRoot(ref=ref, path=target))
    return tuple(roots)


def verify_maildir_writable_root(path: Path) -> Mapping[str, str]:
    if not path.is_absolute():
        raise MailboxWritableRootError("mailbox writable root must be absolute")
    _reject_symlink_components(path, stop_at=Path(path.anchor))
    if os.path.islink(path):
        raise MailboxWritableRootError("mailbox writable root must not be a symlink")
    if not path.is_dir():
        raise MailboxWritableRootError("mailbox writable root must be an existing directory")
    checked = [path]
    for name in _MAILDIR_SUBDIRS:
        subdir = path / name
        if os.path.islink(subdir):
            raise MailboxWritableRootError(f"mailbox subdir {name} must not be a symlink")
        if not subdir.is_dir():
            raise MailboxWritableRootError(f"mailbox subdir {name} must be an existing directory")
        checked.append(subdir)
    for directory in checked:
        fd = None
        probe_name = None
        try:
            fd, probe_name = tempfile.mkstemp(prefix=".pantheon-rw-probe-", dir=directory)
            os.close(fd)
            fd = None
            os.unlink(probe_name)
        except OSError as exc:
            raise MailboxWritableRootError(f"mailbox directory is not writable: {directory}: {exc}") from exc
        finally:
            if fd is not None:
                os.close(fd)
            if probe_name is not None:
                try:
                    os.unlink(probe_name)
                except FileNotFoundError:
                    pass
    return {
        "root": str(path),
        "maildir_subdirs": ",".join(_MAILDIR_SUBDIRS),
        "transient_write_probe": "true",
    }
