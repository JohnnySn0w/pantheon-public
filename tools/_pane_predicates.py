"""Pane-context predicate helpers for RECOVERY_AUTO_RESOLVE_ALLOWLIST rules.

This module provides pure utility functions used by AutoResolveRule predicates
in tools/inbox_consumer.py. Functions here are independently testable and have
no runtime side-effects (no tmux, no subprocess, no file I/O).

Convention mirrors tools/_git_utils.py: underscore-prefixed module name signals
internal-use helper; public surface is the functions, not the module name.

Predicate-helper design (per T-0479 HO design call #1):
  Each predicate in a rule knows what context token it needs (folder path,
  file path, URL, etc.) and has a co-located extractor here. Generic body-
  extraction is intentionally NOT provided — different prompt classes have
  heterogeneous shapes, and a generic extractor would be fragile across them.
  Per-sentinel-class extraction co-located with the predicate that needs it.

Dialog text shapes are grounded in the Specialist T-0473 catalog:
  knowledge/watch/reports/2026-05-20/claude-code-wedge-sentinels.md §3.1, §3.3
and verbatim source: TrustDialog.tsx, FileWritePermissionRequest.tsx,
FileEditPermissionRequest.tsx from the claude-code March-2026 leak mirror.

Public surface:
    path_in_canonical_pantheon_dirs(path) — is path under a canonical dir?
    path_in_agent_cwd(path, agent)        — is path within the agent's cwd?
    extract_trust_dialog_path(pane_text)  — folder path from trust dialog
    extract_file_dialog_path(pane_text)   — file path from create/edit/overwrite
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Canonical Pantheon directory set (per T-0479 ticket spec).
# ---------------------------------------------------------------------------

# Static canonical home dirs. Each entry is a path prefix; a path matches if it
# equals the prefix OR starts with "<prefix>/". Order doesn't matter for
# correctness, but keeps the list readable.
_CANONICAL_HOME_DIRS: tuple[str, ...] = (
    "/home/claude/pantheon-config",
    "/home/claude-research/pantheon-config",
    "/home/claude-fallback/pantheon-config",
    "/home/claude/echo-prismatic",
    "/home/claude/pantheon-pwa",
)

# /tmp/<name>-T-NNNN pattern: one or more path segments, then a hyphen, then
# T- followed by digits. The canonical worktree root is /tmp/<anything>-T-\d+.
# Example: /tmp/pantheon-config-T-0479, /tmp/echo-prismatic-T-0200
_TMP_WORKTREE_RE = re.compile(r"^/tmp/[^/]+-T-\d+(/.*)?$")

# Per-agent home dirs. Maps persona → list of home-dir prefixes that belong to
# that agent's cwd. /tmp/ worktrees are shared across agents (any agent may be
# dispatched into any /tmp worktree).
_AGENT_HOME_PREFIXES: dict[str, tuple[str, ...]] = {
    "orchestrator": (
        "/home/claude/pantheon-config",
        "/home/claude/echo-prismatic",
        "/home/claude/pantheon-pwa",
    ),
    "specialist": (
        "/home/claude-research/pantheon-config",
    ),
    "recovery": (
        "/home/claude-fallback/pantheon-config",
    ),
}

# ---------------------------------------------------------------------------
# Path predicates.
# ---------------------------------------------------------------------------


def path_in_canonical_pantheon_dirs(path: str) -> bool:
    """Return True iff `path` is under a canonical Pantheon directory.

    Canonical set (per T-0479):
    - /home/claude/pantheon-config (and subpaths)
    - /home/claude-research/pantheon-config (and subpaths)
    - /home/claude-fallback/pantheon-config (and subpaths)
    - /home/claude/echo-prismatic (and subpaths)
    - /home/claude/pantheon-pwa (and subpaths)
    - /tmp/<anything>-T-<digits> and subpaths (agent worktrees)

    Relative paths always return False (canonical paths are absolute).
    """
    if not path or not path.startswith("/"):
        return False

    # Check static canonical home dirs.
    for prefix in _CANONICAL_HOME_DIRS:
        if path == prefix or path.startswith(prefix + "/"):
            return True

    # Check /tmp/<name>-T-NNNN worktree pattern.
    if _TMP_WORKTREE_RE.match(path):
        return True

    return False


def path_in_agent_cwd(path: str, agent: str) -> bool:
    """Return True iff `path` is within the given agent's cwd.

    For /tmp/ worktrees: any canonical /tmp/<name>-T-NNNN path is considered
    in-cwd for all agents (agents work in assigned worktrees regardless of
    their home user).

    For home dirs: only the agent's own home-dir prefixes match.

    Returns False for unknown agent names (no configured prefixes).
    """
    if not path or not path.startswith("/"):
        return False

    # /tmp/ worktrees are in-cwd for any agent.
    if _TMP_WORKTREE_RE.match(path):
        return True

    # Check agent-specific home dirs.
    for prefix in _AGENT_HOME_PREFIXES.get(agent, ()):
        if path == prefix or path.startswith(prefix + "/"):
            return True

    return False


# ---------------------------------------------------------------------------
# Per-sentinel-class extractors.
# ---------------------------------------------------------------------------

# Trust dialog sentinels (Specialist catalog §3.1):
#   Current variant: "Do you trust the files in this folder?"
#   Legacy variant:  "Yes, I trust this folder" (used as sub-heading in some code paths)
# In both variants, the folder path appears on its own line after the question,
# before the option list (❯ 1. ...).
_TRUST_DIALOG_SENTINELS = (
    "Do you trust the files in this folder?",
    "Yes, I trust this folder",
)

# File operation dialog sentinels (Specialist catalog §3.3).
# Pattern: "Do you want to <verb> /path/to/file?"
_FILE_DIALOG_RE = re.compile(
    r"Do you want to (?:create|make this edit to|overwrite)\s+(/[^\?]+)\?",
    re.IGNORECASE,
)


def extract_trust_dialog_path(pane_text: str) -> str | None:
    """Extract the folder path from a workspace-trust dialog pane capture.

    Returns the path string if found, else None.

    Expected pane shape (TrustDialog.tsx, from T-0473 §3.1 catalog):
      Do you trust the files in this folder?
      /path/to/folder
      ❯ 1. Yes, I trust this folder
        2. No, exit Claude Code

    The folder path appears on a standalone line that starts with '/' after
    the trust-dialog sentinel line.
    """
    if not any(sentinel in pane_text for sentinel in _TRUST_DIALOG_SENTINELS):
        return None

    lines = pane_text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if any(sentinel in stripped for sentinel in _TRUST_DIALOG_SENTINELS):
            # The path is on the next non-empty line that starts with '/'.
            for subsequent in lines[i + 1:]:
                candidate = subsequent.strip()
                if candidate.startswith("/"):
                    return candidate
            break

    return None


def extract_file_dialog_path(pane_text: str) -> str | None:
    """Extract the file path from a create/edit/overwrite dialog pane capture.

    Returns the path string if found, else None.

    Expected pane shapes (FileWritePermissionRequest.tsx etc., T-0473 §3.3):
      Do you want to create /path/to/file.py?
      Do you want to make this edit to /path/to/file.py?
      Do you want to overwrite /path/to/file.py?

    The path is embedded in the question line itself, terminated by '?'.
    """
    match = _FILE_DIALOG_RE.search(pane_text)
    if match:
        return match.group(1).strip()
    return None
