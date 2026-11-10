"""codex_shell_box.py: wire the shell-box PreToolUse hook into a codex config.toml (T-5480).

Intent: box every codex-runtime shell child in a per-command memory-bounded systemd scope, as
claude seats already are, so a codex fallback seat cannot OOM the host (ORG-104). Codex has no
config key that wraps the shell; the mechanism is a PreToolUse hook (matcher ``Bash``) that
rewrites the command (tools/oom/pantheon-codex-shell-box-hook.py).

This module is PURE TEXT: it takes a config.toml's text and returns the new text. It reuses the
worker-guard's trust machinery (tools.harness_adapter.codex) so the hook definition AND its
``[hooks.state."<key>"] trusted_hash`` land in one write (codex skips an untrusted hook, i.e.
fails open). It is deliberately NOT called from ``prepare_codex_launch``: wiring a live seat is a
separate, operator-run step (the installer op ``harness.codex-shell-box``).

The hook block is APPENDED, so existing hooks (the worker-guard) keep their position-based keys
and trust. For the same reason ``remove`` refuses unless our group is the LAST PreToolUse group:
removing an earlier one would shift later groups' keys and silently untrust them.
"""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

from tools.harness_adapter import codex as _codex

HOOK_RELATIVE = Path("pantheon-config") / "tools" / "oom" / "pantheon-codex-shell-box-hook.py"
BOX_RELATIVE = Path("pantheon-config") / "tools" / "oom" / "pantheon-shell-box.sh"
MATCHER = "Bash"
# The hook is a pure string rewrite; a codex hook timeout fails OPEN (command runs unboxed).
TIMEOUT_S = 10
_MARK = "# PreToolUse shell-box (T-5480)"

_BLOCK = _MARK + """: every codex Bash command runs through
# pantheon-shell-box.sh (per-command systemd scope, MemoryMax, oom_score_adj 0). Seeded by the
# persona installer op harness.codex-shell-box; fails open (unboxed) on any hook problem.
[[hooks.PreToolUse]]
matcher = "Bash"

[[hooks.PreToolUse.hooks]]
type = "command"
command = {command}
timeout = 10
"""


class WiringRefused(Exception):
    """The config cannot be wired/unwired safely; the file must be left untouched."""


def hook_script(account_home: Path) -> Path:
    return Path(account_home) / HOOK_RELATIVE


def box_script(account_home: Path) -> Path:
    return Path(account_home) / BOX_RELATIVE


def hook_command(account_home: Path) -> str:
    """Mirrors the worker-guard's invocation: an isolated interpreter (python3 -I)."""
    return f"/usr/bin/python3 -I {hook_script(account_home)}"


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _parse(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text) if text.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise WiringRefused(f"config.toml is not valid TOML ({exc}); refusing to touch it") from exc


def _groups(parsed: dict[str, Any]) -> list[Any]:
    groups = (parsed.get("hooks") or {}).get("PreToolUse") or []
    return groups if isinstance(groups, list) else []


def _definition_ok(parsed: dict[str, Any], pos: tuple[int, int]) -> bool:
    group, hook = _codex._guard_hook(parsed, pos)
    return (set(group) == {"matcher", "hooks"} and group["matcher"] == MATCHER
            and len(group["hooks"]) == 1
            and set(hook) == {"type", "command", "timeout"}
            and hook["type"] == "command" and hook["timeout"] == TIMEOUT_S)


def _single_position(parsed: dict[str, Any], command: str) -> tuple[int, int] | None:
    positions = _codex._guard_positions(parsed, command)
    if len(positions) > 1:
        raise WiringRefused(f"{len(positions)} shell-box hooks in config.toml; refusing an ambiguous config")
    return positions[0] if positions else None


def classify(text: str, codex_home: Path, account_home: Path) -> tuple[str, str]:
    """(classification, detail), read-only, no codex call: ``absent``, ``untrusted`` (our block, no
    stored trusted_hash), ``matching`` (block exact + a stored hash; the hash itself is only checked
    against codex's by ``wire``), or ``conflicting`` (invalid TOML, ambiguous, or edited)."""
    command = hook_command(account_home)
    try:
        parsed = _parse(text)
        pos = _single_position(parsed, command)
    except WiringRefused as exc:
        return "conflicting", str(exc)
    if pos is None:
        return "absent", "shell-box hook block absent; will be appended with its trust"
    if not _definition_ok(parsed, pos):
        return "conflicting", ("the shell-box hook in config.toml was edited (matcher/timeout/keys differ); "
                               "refusing to overwrite it")
    if not _codex._stored_trusted_hash(parsed, _codex._guard_key(codex_home, pos)):
        return "untrusted", "shell-box hook block present without a trusted_hash; will add it"
    return "matching", "shell-box hook block already present and trusted"


def wire(text: str, codex_home: Path, account_home: Path, *, executable: Path,
         read_hooks, check_scripts: bool = True) -> tuple[str, str]:
    """(classification, new text). ``absent``: block + trust appended. ``untrusted``: the block is
    ours and current but has no trusted_hash; only the hash is added. ``matching``: nothing to do
    (text returned as is). Raises WiringRefused on anything it does not fully understand: invalid
    TOML, a missing/non-executable hook script or box (a hook pointing at a missing script runs
    on every Bash call), an edited definition, a stale hash, or an unreadable codex hash."""
    for path in ((hook_script(account_home), box_script(account_home)) if check_scripts else ()):
        if not _executable(path):
            raise WiringRefused(f"{path} is missing or not executable by this account; "
                                "pull or clone pantheon-config into the seat home first")
    command = hook_command(account_home)
    parsed = _parse(text)
    pos = _single_position(parsed, command)
    if pos is None:
        wired = (text.rstrip("\n") + "\n\n" if text.strip() else "") + _BLOCK.format(
            command=_codex._toml_scalar(command))
        cls = "absent"
        base = wired
    else:
        if not _definition_ok(parsed, pos):
            raise WiringRefused("the shell-box hook in config.toml was edited (matcher/timeout/keys differ); "
                                "refusing to overwrite it")
        cls, base = "untrusted", text
    wired_parsed = _parse(base)
    pos = _single_position(wired_parsed, command)
    assert pos is not None
    key = _codex._guard_key(codex_home, pos)
    definition = _codex._guard_definition_toml(wired_parsed, pos)
    digest = definition and _codex._precompute_guard_hash(
        definition, command, executable=executable, account_home=account_home, read_hooks=read_hooks)
    if not digest:
        raise WiringRefused("could not read codex's hash for the shell-box hook (codex hooks/list failed); "
                            "refusing to wire an untrusted hook")
    stored = _codex._stored_trusted_hash(wired_parsed, key)
    if stored == digest:
        return "matching", text
    if stored is not None:
        raise WiringRefused(f"trusted_hash for {key} is {stored}, codex computes {digest}; "
                            "refusing to overwrite a differing trust entry")
    trusted = _codex._set_trusted_hash(base, wired_parsed, key, digest)
    if trusted is None:
        raise WiringRefused("could not add trusted_hash as a provably minimal edit")
    return cls, trusted


def status(text: str, codex_home: Path, account_home: Path) -> dict[str, Any]:
    """Read-only, on-disk verdict (no codex call). ``problems`` is empty iff the block is present,
    exact (command, matcher, timeout), carries a stored trusted_hash, and the hook script and box
    are executable by the calling account."""
    command = hook_command(account_home)
    hook_ok, box_ok = _executable(hook_script(account_home)), _executable(box_script(account_home))
    out: dict[str, Any] = {
        "command": command, "block_present": False, "definition_ok": False, "key": None,
        "trusted_hash": None, "hook_executable": hook_ok, "box_executable": box_ok, "problems": [],
    }
    problems: list[str] = out["problems"]
    try:
        parsed = _parse(text)
        pos = _single_position(parsed, command)
    except WiringRefused as exc:
        problems.append(str(exc))
        return out
    if pos is None:
        problems.append("shell-box hook block absent from config.toml")
    else:
        out["block_present"] = True
        out["definition_ok"] = _definition_ok(parsed, pos)
        if not out["definition_ok"]:
            problems.append("shell-box hook block differs from the canonical definition (matcher/timeout/keys)")
        out["key"] = _codex._guard_key(codex_home, pos)
        out["trusted_hash"] = _codex._stored_trusted_hash(parsed, out["key"])
        if not out["trusted_hash"]:
            problems.append(f"no trusted_hash stored for {out['key']}: codex would skip the hook")
    if not hook_ok:
        problems.append(f"hook script missing or not executable: {hook_script(account_home)}")
    if not box_ok:
        problems.append(f"shell-box missing or not executable: {box_script(account_home)}")
    return out


def precomputed_hash(text: str, codex_home: Path, account_home: Path, *, executable: Path,
                     read_hooks) -> str | None:
    """codex's own hash for the block as it sits in ``text`` (via a scratch CODEX_HOME), or None
    when the block is absent/ambiguous or codex could not be read. For verification only."""
    command = hook_command(account_home)
    try:
        parsed = _parse(text)
        pos = _single_position(parsed, command)
    except WiringRefused:
        return None
    definition = _codex._guard_definition_toml(parsed, pos) if pos else None
    return definition and _codex._precompute_guard_hash(
        definition, command, executable=executable, account_home=account_home, read_hooks=read_hooks)


def _prune(doc: dict[str, Any]) -> dict[str, Any]:
    hooks = doc.get("hooks")
    if isinstance(hooks, dict):
        if hooks.get("state") == {}:
            del hooks["state"]
        if hooks.get("PreToolUse") == []:
            del hooks["PreToolUse"]
        if hooks == {}:
            del doc["hooks"]
    return doc


def remove(text: str, codex_home: Path, account_home: Path) -> str | None:
    """``text`` without OUR hook group and its trust entry, or None when the block is absent.
    Raises WiringRefused unless the result provably equals the parsed original minus exactly those
    two entries, and unless our group is the last PreToolUse group (see module docstring)."""
    command = hook_command(account_home)
    parsed = _parse(text)
    pos = _single_position(parsed, command)
    if pos is None:
        return None
    if pos != (len(_groups(parsed)) - 1, 0) or not _definition_ok(parsed, pos):
        raise WiringRefused("the shell-box hook is not the last, unmodified PreToolUse group; "
                            "not removing it (that would shift other hooks' trust keys)")
    key = _codex._guard_key(codex_home, pos)
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    cmd_lines = [i for i, line in enumerate(lines) if _codex._key_line(line, "command") == command]
    if len(cmd_lines) != 1:
        raise WiringRefused("cannot locate the shell-box hook lines exactly")
    _s, hook_end = _codex._table_bounds(lines, cmd_lines[0])
    headers = [i for i in range(cmd_lines[0], -1, -1) if lines[i].strip() == "[[hooks.PreToolUse]]"]
    if not headers:
        raise WiringRefused("cannot locate the shell-box hook group header")
    start = headers[0]
    while start > 0 and lines[start - 1].startswith("#") and (
            _MARK in lines[start - 1] or lines[start - 1].startswith("# pantheon-shell-box.sh")
            or lines[start - 1].startswith("# persona installer op")
            or lines[start - 1].startswith("# (")):
        start -= 1
    remove_ranges = [(start, hook_end)]
    header = f"[hooks.state.{_codex._toml_scalar(key)}]"
    state_headers = [i for i, line in enumerate(lines) if line.strip() == header]
    if len(state_headers) == 1:
        hs, he = _codex._table_bounds(lines, state_headers[0])
        remove_ranges.append((hs, he))
    keep = [line for i, line in enumerate(lines) if not any(a <= i < b for a, b in remove_ranges)]
    new = "".join(keep)
    # Trim the blank lines our appended block leaves at the end of the file only.
    if new.strip() == "":
        new = ""
    expected = copy.deepcopy(parsed)
    del expected["hooks"]["PreToolUse"][pos[0]]
    (expected.get("hooks", {}).get("state") or {}).pop(key, None)
    try:
        if _prune(tomllib.loads(new) if new.strip() else {}) != _prune(expected):
            raise WiringRefused("removal would not be a provably minimal edit; left untouched")
    except tomllib.TOMLDecodeError as exc:
        raise WiringRefused("removal produced invalid TOML; left untouched") from exc
    return new.rstrip("\n") + "\n" if new.strip() else ""
