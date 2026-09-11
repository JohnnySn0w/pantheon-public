"""Resolve manifest-declared persona brief paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class PersonaBriefError(ValueError):
    """The persona brief reference is not a safe manifest relative path."""


def validate_persona_brief_ref(value: Any, *, field: str = "persona_brief_ref") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PersonaBriefError(f"{field} must be a non-empty relative path")
    ref = value.strip()
    path = Path(ref)
    if path.is_absolute():
        raise PersonaBriefError(f"{field} must be account-home-relative, not absolute")
    if path.parts in {(), (".",)} or any(part in {"", ".", ".."} for part in path.parts):
        raise PersonaBriefError(f"{field} must not contain empty, current-directory, or parent-directory parts")
    return ref


def resolve_persona_brief_path(
    *,
    repo_root: Path,
    account_home: Path,
    persona: str,
    persona_brief_ref: Any = None,
) -> Path:
    ref = validate_persona_brief_ref(persona_brief_ref)
    if ref is None:
        return repo_root / "pinned-rules" / f"{persona}.md"
    return account_home / ref
