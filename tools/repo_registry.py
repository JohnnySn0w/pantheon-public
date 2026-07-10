"""repo_registry.py — shared repos.yaml loader (T-1136).

Single public function: load_repo_registry(root) -> dict | None.
All three prior readers (ticket_lint, provision_repos, context_fill_monitor)
delegate here; this is the one implementation.
"""

from __future__ import annotations

from pathlib import Path


def load_repo_registry(root: Path) -> dict | None:
    """Load ``<root>/repos.yaml``, return the parsed mapping or None.

    Fail-open by design: any failure (file absent, PyYAML unavailable, parse
    error, non-dict content) returns None rather than raising.  Callers must
    treat None as "registry unavailable — skip validation / audit."

    Args:
        root: Directory that contains (or should contain) ``repos.yaml``.

    Returns:
        The parsed YAML mapping, or None on any failure.
    """
    reg_path = root / "repos.yaml"
    if not reg_path.exists():
        return None
    try:
        import yaml  # available (6.x); lazy + guarded so absence fails-open

        data = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None
