"""Hermes render target and per-seat launch preparation (T-5005, T-4924 H1).

Implements the ``HarnessRenderTarget`` protocol (``render_target.py``) for the
Hermes Agent harness, plus ``prepare_hermes_launch``, which carries out plan §2.2
step 2 under the D-37 failure policy:

* integrity (a source hash mismatch, uncaptured defaults, a safety-default
  contradiction, a seat dir inside a repo) fails closed;
* a transient write fault launches only if the on-disk per-seat files are
  byte-equal to the in-memory render, and otherwise fails closed.

INERT in H1: nothing in render.py, persona_installer or runtime_controller
calls this module yet (that wiring is H2).

Byte safety (pantheon-config CLAUDE.md, plan §1): the rendered project context
is installed under its native lowercase name ``.hermes.md`` only. This module
never builds the uppercase variant.
"""

from __future__ import annotations

import codecs
import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

import yaml

from tools.harness_adapter.render_target import ArtifactKind, SkillEntry
from tools.harness_adapter.types import HarnessLaunchConfig


SEAT_DIRNAME = "seat"
HERMES_HOME_DIRNAME = ".hermes"
STANDING_FILENAME = ".hermes.md"
IDENTITY_FILENAME = "SOUL.md"
RUNTIME_CONFIG_FILENAME = "config.yaml"
MANAGED_MARKER_FILENAME = ".managed"
# [H0] H0 showed ``$HERMES_HOME/.managed`` (or ``HERMES_MANAGED``) marks a
# package-manager-owned install; it is NOT a config lock. Whether the seat still
# wants the marker (and its exact effect) is open for H2.
MANAGED_MARKER_BYTES = b""
# T-5011 PR-D (artisan ruling on .managed): source-read at the pin, the marker
# makes is_managed() true, which refuses Hermes's own config saves, `hermes
# config set`, .env writes and `hermes update` (hermes_cli/config.py:2460,
# 2726, 3166, 3584; main.py:2326). It also SKIPS Hermes's 0600 chmod of secret
# files (config.py:501), so prepare_hermes_launch tightens .env itself. The seat
# owns $HERMES_HOME and can delete the marker: a convenience lock, not a
# boundary; making it root-owned is H5a.
#: tools/skills_sync.py:72,396-410: with this file in $HERMES_HOME, Hermes seeds
#: only its essential skill instead of the 54 bundled ones; ours come from the
#: external dir (artisan ruling 3).
NO_BUNDLED_SKILLS_MARKER_FILENAME = ".no-bundled-skills"
NO_BUNDLED_SKILLS_MARKER_BYTES = b""
#: The seat's provider key file; Hermes skips its own chmod under .managed.
ENV_FILENAME = ".env"
ENV_FILE_MODE = 0o600
OP_ENV_FILENAME = ".op.env"
MAX_ENV_FILE_BYTES = 64 * 1024
BINDINGS_RELDIR = PurePath(".local", "state", "pantheon", "hermes-render")
BINDINGS_FILENAME = "bindings.json"
# Hermes's managed scope (H0, ``hermes_cli/managed_scope.py`` ``get_managed_dir()``):
# ``$HERMES_MANAGED_DIR`` if non-empty and the dir exists, else ``/etc/hermes`` if
# it exists, else none. A well-formed overlay wins per leaf and unlisted leaves
# fall through to the user config; a MALFORMED one fails open (the user config
# applies). The overlay is a convenience lock, not a boundary.
HOST_OVERLAY_PATH = Path("/etc/hermes/config.yaml")
#: The env var that repoints the managed scope. Every hermes launch drops it
#: (``env -u`` semantics), but that removes only an INHERITED value. It does
#: NOT stop a seat repointing the scope: Hermes's ``load_hermes_dotenv``
#: (``hermes_cli/env_loader.py``) loads ``$HERMES_HOME/.env`` into os.environ
#: with override=True (then ``.op.env`` and the install-root ``.env``), and only
#: afterwards does ``_apply_managed_env`` call ``get_managed_dir()``, which
#: reads ``HERMES_MANAGED_DIR`` from os.environ unfiltered. A seat that appends
#: ``HERMES_MANAGED_DIR=~/x`` to its seat-writable ``.env`` and writes
#: ``~/x/config.yaml`` overrides the locks per leaf while config.yaml stays
#: clean. See ``check_env_file_controls`` and the H2/H3 must below.
MANAGED_DIR_ENV = "HERMES_MANAGED_DIR"
LAUNCH_ENV_UNSET: tuple[str, ...] = (MANAGED_DIR_ENV,)
#: Keys that must never appear in a dotenv file Hermes loads, because each one
#: changes where config, the overlay, the home or the standing rules are read
#: from (pinned source e06df9c2, read not run):
#:
#: * ``HERMES_MANAGED_DIR``: repoints the managed scope (``managed_scope.py``).
#: * ``HERMES_MANAGED``: the package-manager-owned install marker
#:   (``hermes_constants.py``), never a seat's choice.
#: * ``HERMES_HOME``: ``get_hermes_home()`` re-reads it on every call, so a
#:   value loaded from ``.env`` repoints ``config.yaml`` and the home.
#: * ``HERMES_IGNORE_USER_CONFIG``: "=1 skips the user config entirely"
#:   (``cli_config_load.py``), dropping the merged locks.
#: * ``HERMES_IGNORE_RULES``: suppresses context files (``agent/system_prompt.py``),
#:   so ``.hermes.md`` (the pinned rules) would not load.
#:
#: Lock-defeat keys, which do not belong in a seat-writable .env either:
#:
#: * ``HERMES_YOLO_MODE``: skips the recoverable approval layer
#:   (``tools/approval.py`` ~:967, ~:1044). It is moot today because
#:   approvals.mode is off, but it must not become a seat's switch.
#: * ``HERMES_ACCEPT_HOOKS``: auto-approves unseen shell hooks
#:   (``agent/shell_hooks.py`` ~:624).
#:
#: Excluded: HERMES_CONFIG, HERMES_ENV and HERMES_PROFILE are on Hermes's own
#: env-name denylist (``hermes_cli/config.py`` ~:120-133) but nothing reads
#: them as a config path at the pin. Revisit them on a pin bump.
FORBIDDEN_ENV_FILE_KEYS: frozenset[str] = frozenset(
    {
        MANAGED_DIR_ENV,
        "HERMES_MANAGED",
        "HERMES_HOME",
        "HERMES_IGNORE_USER_CONFIG",
        "HERMES_IGNORE_RULES",
        "HERMES_YOLO_MODE",
        "HERMES_ACCEPT_HOOKS",
    }
)
#: The finding ``check_env_file_controls`` returns when it cannot decode a file
#: the way Hermes would (fail closed: an unreadable file is never "clean").
ENV_FILE_UNDECODABLE = "<undecodable>"
ENV_FILE_UNREADABLE = "<unreadable>"
ENV_FILE_SYMLINK = "<symlink>"
ENV_FILE_NOT_REGULAR = "<not-regular>"
ENV_FILE_OVERSIZE = "<oversize>"
INSTALL_ROOT_UNRESOLVED = "<install-root-unresolved>"
DOTENV_PATH_CLASSES = ("harness-env", "harness-op-env", "install-root-env")
# H2/H3 MUST (MED-1, T-5005 r1): pre-launch validation and the drift watcher
# both run check_env_file_controls over every dotenv Hermes loads:
# $HERMES_HOME/.env, $HERMES_HOME/.op.env and the install-root .env. Any hit
# fails launch closed and is exposed as config drift. H5a still decides which
# files become root-owned as the better steady state; it does not replace this
# check.
#: The stderr text Hermes prints when it skips a malformed overlay ("managed
#: scope: failed to parse <path> ... IGNORING this managed file. Admin policy
#: from this file is NOT being applied."). The H3 drift watcher keys on it; no
#: watcher exists in H1.
MANAGED_OVERLAY_IGNORED_STDERR = "IGNORING this managed file"
SKILLS_ROOT = Path("/opt/pantheon/hermes-skills")
SKILL_CATEGORY = "pantheon"
SOP_SKILL_PREFIX = "pantheon-sop-"
IDENTITY_HEADING = "## Identity"
DEFAULTS_SCHEMA = "pantheon-hermes-defaults/v1"
CAPTURED_PROVENANCE = "h0-captured"
BINDINGS_SCHEMA = "pantheon-hermes-render-bindings/v1"
# "merged" (the default since H0): the locked defaults are rendered INTO the
# per-seat config.yaml AND emitted for the host overlay as an extra layer,
# because the overlay fails open on a malformed file (H0 Specialist must 3).
# "overlay": the locks go to HOST_OVERLAY_PATH only (the pre-H0 plan shape);
# kept for comparison, never the live default.
DEFAULTS_PLACEMENT = "merged"
_PLACEMENTS = ("overlay", "merged")
_GENERATED_HEADER = (
    "# GENERATED by tools/harness_adapter/hermes_render.py (T-4924). Do not hand-edit:\n"
    "# it is re-rendered at every launch and drift-checked.\n"
)
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_AUTO_PROVIDER = "auto"
DEEPSEEK_PROXY_BASE_URL = "https://deepseek-proxy.invalid:8448/v1"


class HermesRenderError(RuntimeError):
    """A fail-closed render outcome. ``reason`` is an enum-like token that the
    watcher exports as ``config_render: failed`` evidence (plan §2.2)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dump_yaml(data: Mapping[str, Any]) -> bytes:
    body = yaml.safe_dump(
        _plain(data),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return (_GENERATED_HEADER + body).encode("utf-8")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _leaves(data: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Dotted-path leaves. Non-empty mappings recurse; anything else (lists and
    empty mappings included) is a leaf."""
    out: dict[tuple[str, ...], Any] = {}
    for key, value in data.items():
        path = prefix + (str(key),)
        if isinstance(value, Mapping) and value:
            out.update(_leaves(value, path))
        else:
            out[path] = _plain(value)
    return out


def _deep_merge(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {key: _plain(value) for key, value in base.items()}
    for key, value in extra.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping) and value:
            merged[key] = _deep_merge(current, value)
        elif key not in merged:
            merged[key] = _plain(value)
    return merged


def check_no_contradiction(per_seat: Mapping[str, Any], overlay: Mapping[str, Any]) -> None:
    """Every overlay leaf is a locked safety default (plan §2.1 precedence).

    A per-seat value at a locked path (or one that replaces a locked subtree)
    must equal it, or the render fails. Per-seat keys never override.
    """
    locked = _leaves(overlay)
    seat = _leaves(per_seat)
    for path, value in sorted(seat.items()):
        for depth in range(1, len(path)):
            if path[:depth] in locked:
                # Nested under a locked leaf, including an empty-mapping leaf
                # such as `mcp_servers: {}` (r1 MED-2).
                raise HermesRenderError(
                    "safety-default-contradiction",
                    f"per-seat {'.'.join(path)} is nested under locked {'.'.join(path[:depth])}",
                )
        if path in locked and locked[path] != value:
            raise HermesRenderError(
                "safety-default-contradiction",
                f"per-seat {'.'.join(path)}={value!r} contradicts locked {locked[path]!r}",
            )
        for locked_path in locked:
            if len(locked_path) > len(path) and locked_path[: len(path)] == path:
                raise HermesRenderError(
                    "safety-default-contradiction",
                    f"per-seat {'.'.join(path)} replaces locked subtree {'.'.join(locked_path)}",
                )


_EXPORT_RE = re.compile(r"^export\s+")
_FORBIDDEN_WORD_RES = {
    key: re.compile(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])") for key in FORBIDDEN_ENV_FILE_KEYS
}


def _decode_env_like_hermes(raw: bytes) -> str | None:
    """Decode dotenv bytes the way Hermes does before python-dotenv parses them.

    This mirrors ``_sanitize_env_file_if_needed`` in ``hermes_cli/env_loader.py``
    ~:384-452 (pin e06df9c2):

    * a UTF-32 BOM: Hermes leaves the file untouched, so what python-dotenv
      then reads is not modelled here. Return None (fail closed).
    * a UTF-16 BOM (LE/BE): decode as UTF-16 (Hermes rewrites it as UTF-8).
    * otherwise decode as utf-8-sig, which strips a UTF-8 BOM. Hermes uses
      errors=replace; here an undecodable byte returns None (fail closed).

    Then strip every NUL, as Hermes does before parsing.
    """
    if raw.startswith(codecs.BOM_UTF32_LE) or raw.startswith(codecs.BOM_UTF32_BE):
        return None  # checked before UTF-16: the UTF-32 LE BOM starts with the UTF-16 LE BOM
    try:
        if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
            text = raw.decode("utf-16")
        else:
            text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return text.replace("\x00", "")


def _read_bounded_regular(path: Path, cap: int) -> tuple[bytes | None, str]:
    """Read one regular file without following its final symlink.

    The status is ``ok``, ``absent``, ``symlink``, ``not-regular``,
    ``oversize`` or ``unreadable``. Reads are capped even when a hostile file's
    reported size is stale or zero.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, "absent"
    except OSError as exc:
        return None, "symlink" if exc.errno == errno.ELOOP else "unreadable"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not-regular"
        if st.st_size > cap:
            return None, "oversize"
        chunks: list[bytes] = []
        remaining = cap + 1
        while remaining:
            try:
                chunk = os.read(fd, remaining)
            except InterruptedError:
                continue
            except OSError:
                return None, "unreadable"
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        return (None, "oversize") if len(raw) > cap else (raw, "ok")
    finally:
        os.close(fd)


_ENV_READ_FAILURE = {
    "symlink": ENV_FILE_SYMLINK,
    "not-regular": ENV_FILE_NOT_REGULAR,
    "oversize": ENV_FILE_OVERSIZE,
    "unreadable": ENV_FILE_UNREADABLE,
}


def check_env_file_controls(path: Path) -> list[str]:
    """The ``FORBIDDEN_ENV_FILE_KEYS`` a dotenv file sets, sorted; ``[]`` if none
    (or if the file does not exist, since then Hermes loads nothing from it).
    ``[ENV_FILE_UNDECODABLE]`` if it cannot be decoded as Hermes would.

    Pure and read-only. The pre-launch gate and H3 drift watcher both call it.
    Fail-closed matching, where a false positive is acceptable and a miss is not:

    * each non-comment line, after an optional ``export`` and whitespace, has a
      key token (the text before the first ``=``, or the whole line); the token
      is flagged if it equals a forbidden key with or without surrounding single
      or double quotes;
    * as a backstop, any non-comment line whose text before the first ``=``
      contains a forbidden key as a whole word is flagged too.

    A key counts whatever its value (an empty value is still a seat-written control).
    """
    raw, status = _read_bounded_regular(path, MAX_ENV_FILE_BYTES)
    if status == "absent":
        return []
    if status != "ok" or raw is None:
        return [_ENV_READ_FAILURE[status]]
    text = _decode_env_like_hermes(raw)
    if text is None:
        return [ENV_FILE_UNDECODABLE]
    found: set[str] = set()
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        body = _EXPORT_RE.sub("", stripped)
        head = body.split("=", 1)[0]
        token = head.strip()
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
            token = token[1:-1].strip()
        if token in FORBIDDEN_ENV_FILE_KEYS:
            found.add(token)
        for key, pattern in _FORBIDDEN_WORD_RES.items():
            if pattern.search(head):
                found.add(key)
    return sorted(found)


def hermes_install_root(executable: Path) -> Path:
    """Resolve the pinned editable install's source root from its venv metadata.

    v0.22.0's ``hermes`` console script imports ``hermes_cli.main``. That module
    defines its project root as the parent of the installed ``hermes_cli``
    package and loads ``PROJECT_ROOT/.env``. The H0 install used ``uv sync
    --frozen``, which records that editable source root in
    ``hermes_agent-*.dist-info/direct_url.json``. Follow that recorded package
    placement instead of guessing an executable parent.
    """
    try:
        exe = executable.resolve(strict=True)
    except OSError as exc:
        raise HermesRenderError("install-root-unresolved", type(exc).__name__) from None
    if exe.name != "hermes" or exe.parent.name != "bin":
        raise HermesRenderError("install-root-unresolved", "executable-layout")
    venv = exe.parent.parent
    cfg_raw, cfg_status = _read_bounded_regular(venv / "pyvenv.cfg", 16 * 1024)
    if cfg_status != "ok" or cfg_raw is None:
        raise HermesRenderError("install-root-unresolved", f"pyvenv-{cfg_status}")
    try:
        cfg = cfg_raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HermesRenderError("install-root-unresolved", "pyvenv-undecodable") from None
    version = re.search(r"^version_info\s*=\s*(\d+)\.(\d+)(?:\.\d+)?\s*$", cfg, re.MULTILINE)
    if version is None:
        raise HermesRenderError("install-root-unresolved", "pyvenv-version")
    site = venv / "lib" / f"python{version.group(1)}.{version.group(2)}" / "site-packages"
    metadata: list[Path] = []
    try:
        with os.scandir(site) as entries:
            for index, entry in enumerate(entries):
                if index >= 512:
                    raise HermesRenderError("install-root-unresolved", "site-packages-oversize")
                if (entry.name.startswith("hermes_agent-") and entry.name.endswith(".dist-info")
                        and entry.is_dir(follow_symlinks=False)):
                    metadata.append(Path(entry.path) / "direct_url.json")
    except OSError as exc:
        raise HermesRenderError("install-root-unresolved", type(exc).__name__) from None
    if len(metadata) != 1:
        raise HermesRenderError("install-root-unresolved", "package-metadata")
    direct_raw, direct_status = _read_bounded_regular(metadata[0], 4096)
    if direct_status != "ok" or direct_raw is None:
        raise HermesRenderError("install-root-unresolved", f"direct-url-{direct_status}")
    try:
        direct = json.loads(direct_raw.decode("utf-8"))
        parsed = urllib.parse.urlparse(direct["url"])
        editable = direct["dir_info"]["editable"] is True
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise HermesRenderError("install-root-unresolved", "direct-url-invalid") from None
    if not editable or parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        raise HermesRenderError("install-root-unresolved", "direct-url-layout")
    root = Path(urllib.request.url2pathname(urllib.parse.unquote(parsed.path)))
    if not root.is_absolute():
        raise HermesRenderError("install-root-unresolved", "direct-url-relative")
    for marker in (root / "hermes_cli" / "_startup_fast.py", root / "pyproject.toml"):
        try:
            mode = marker.lstat().st_mode
        except OSError as exc:
            raise HermesRenderError("install-root-unresolved", type(exc).__name__) from None
        if not stat.S_ISREG(mode):
            raise HermesRenderError("install-root-unresolved", "package-layout")
    return root


def dotenv_control_findings(
    account_home: Path,
    executable: Path,
    *,
    install_root_resolver: Callable[[Path], Path] = hermes_install_root,
) -> dict[str, tuple[str, ...]]:
    """Typed, value-free findings for exactly the three dotenv paths v0.22.0 loads."""
    paths: list[tuple[str, Path]] = [
        ("harness-env", account_home / HERMES_HOME_DIRNAME / ENV_FILENAME),
        ("harness-op-env", account_home / HERMES_HOME_DIRNAME / OP_ENV_FILENAME),
    ]
    try:
        install_root = install_root_resolver(executable)
    except (HermesRenderError, OSError, ValueError):
        paths.append(("install-root-env", Path(".")))
        unresolved = True
    else:
        paths.append(("install-root-env", install_root / ENV_FILENAME))
        unresolved = False
    result: dict[str, tuple[str, ...]] = {}
    for path_class, path in paths:
        if path_class == "install-root-env" and unresolved:
            result[path_class] = (INSTALL_ROOT_UNRESOLVED,)
            continue
        raw = check_env_file_controls(path)
        normalized = tuple(
            finding if finding.startswith("<") else "<forbidden-control>"
            for finding in raw
        )
        result[path_class] = tuple(sorted(set(normalized)))
    return result


def load_defaults(path: Path) -> dict[str, Any]:
    """Parse and shape-check ``harness/hermes/config.defaults.yaml``."""
    return read_defaults(path)[0]


def read_defaults(path: Path) -> tuple[dict[str, Any], str]:
    """Read the defaults file ONCE; return (parsed, sha256 of those same bytes)."""
    raw = _read_bytes(path)
    if raw is None:
        raise HermesRenderError("defaults-unreadable", "OSError")
    return parse_defaults(raw), _sha256(raw)


def parse_defaults(raw: bytes) -> dict[str, Any]:
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise HermesRenderError("defaults-unreadable", type(exc).__name__) from None
    if not isinstance(data, dict) or data.get("schema") != DEFAULTS_SCHEMA:
        raise HermesRenderError("defaults-schema", "unsupported defaults schema")
    overlay = data.get("overlay")
    slots = data.get("auxiliary_slots")
    if not isinstance(overlay, dict):
        raise HermesRenderError("defaults-schema", "overlay must be a mapping")
    if not isinstance(slots, list) or not all(isinstance(s, str) and s for s in slots):
        raise HermesRenderError("defaults-schema", "auxiliary_slots must be a list of names")
    if len(set(slots)) != len(slots):
        raise HermesRenderError("defaults-schema", "auxiliary_slots has duplicates")
    platform = overlay.get("platform_toolsets")
    cli = platform.get("cli") if isinstance(platform, dict) else None
    if not isinstance(cli, list) or not cli or not all(isinstance(t, str) and t for t in cli):
        # T-5011 PR-D: no lock means Hermes's widest default; refuse it.
        raise HermesRenderError("defaults-schema", "overlay.platform_toolsets.cli must be a non-empty list")
    if "toolsets" in overlay:
        raise HermesRenderError("defaults-schema", "overlay.toolsets does not select CLI tools; lock platform_toolsets.cli")
    deny = overlay.get("approvals", {}).get("deny") if isinstance(overlay.get("approvals"), dict) else None
    if deny is not None and (not isinstance(deny, list) or not all(isinstance(g, str) and g.strip() for g in deny)):
        # approvals.deny: case-insensitive fnmatch globs over the normalized
        # command, applied before approvals.mode off (tools/approval_floors.py:23-42).
        raise HermesRenderError("defaults-schema", "overlay.approvals.deny must be a list of non-empty glob strings")
    if not isinstance(data.get("provenance"), str):
        raise HermesRenderError("defaults-schema", "provenance must be a string")
    return data


def extract_identity(source_text: str, heading: str = IDENTITY_HEADING) -> str:
    """The SOUL.md extract: the ``## Identity`` heading-region (plan D-24)."""
    # Lazy import: render.py will import render_target in H2; importing render.py
    # at module load here would then be a cycle.
    from tools.persona_orientation.render import extract_heading_region

    try:
        region = extract_heading_region(source_text, heading)
    except ValueError:
        raise HermesRenderError("identity-heading-missing", heading) from None
    return region + "\n"


class HermesRenderTarget:
    """The hermes ``HarnessRenderTarget`` (plan §2.4)."""

    harness = "hermes"
    in_all = False  # opt-in: `--harness all` stays claude+codex (plan §2.5 gap 1)

    def __init__(self, *, placement: str = DEFAULTS_PLACEMENT) -> None:
        if placement not in _PLACEMENTS:
            raise ValueError(f"unknown defaults placement {placement!r}")
        self.placement = placement

    def harness_home(self, account_home: Path) -> Path:
        return account_home / HERMES_HOME_DIRNAME

    def seat_dir(self, account_home: Path) -> Path:
        return account_home / SEAT_DIRNAME

    def install_path(self, kind: ArtifactKind, *, account_home: Path) -> Path:
        if kind == "project-context":
            return self.seat_dir(account_home) / STANDING_FILENAME
        if kind == "identity":
            return self.harness_home(account_home) / IDENTITY_FILENAME
        if kind == "runtime-config":
            return self.harness_home(account_home) / RUNTIME_CONFIG_FILENAME
        if kind == "skill-root":
            return SKILLS_ROOT  # root-owned; the pin subdirectory is chosen by the root render
        raise ValueError(f"unknown artifact kind {kind!r}")

    def skill_frontmatter(self, entry: SkillEntry) -> Mapping[str, Any]:
        return {"name": SOP_SKILL_PREFIX + entry["slug"], "description": entry["description"]}

    def skill_relpath(self, slug: str) -> PurePath:
        if not _SLUG_RE.match(slug):
            raise ValueError(f"invalid skill slug {slug!r}")
        return PurePath(SKILL_CATEGORY, SOP_SKILL_PREFIX + slug, "SKILL.md")

    def render_standing(self, source_text: str, *, persona: str) -> bytes:
        """Verbatim source bytes (the codex standing-instructions shape, plan D-3),
        so the drift check is byte-equality against the pinned source."""
        del persona
        return source_text.encode("utf-8")

    def runtime_config_data(
        self,
        profile: HarnessLaunchConfig,
        defaults: Mapping[str, Any],
        *,
        account_home: Path,
    ) -> dict[str, Any]:
        if profile.harness != self.harness:
            raise HermesRenderError("profile-harness-mismatch", profile.harness)
        native = profile.native
        provider = native.get("provider")
        model = profile.model
        if not isinstance(provider, str) or not provider or provider == _AUTO_PROVIDER:
            raise HermesRenderError("profile-provider", "native.provider must name a provider")
        if not isinstance(model, str) or not model:
            raise HermesRenderError("profile-model", "the profile must set a model")
        model_block: dict[str, Any] = {"default": model, "provider": provider}
        base_url = native.get("base_url")
        if base_url is not None:
            if not isinstance(base_url, str) or not base_url:
                raise HermesRenderError("profile-base-url", "native.base_url must be a string")
            model_block["base_url"] = base_url
        config: dict[str, Any] = {
            "model": model_block,
            "terminal": {
                "backend": native.get("terminal_backend", "local"),
                "cwd": str(self.seat_dir(account_home)),
            },
        }
        if "approvals" in native:
            config["approvals"] = native["approvals"]
        if "busy_input_mode" in native:
            config["display"] = {"busy_input_mode": native["busy_input_mode"]}
        aux_pin: dict[str, Any] = {"provider": provider, "model": model}
        if base_url is not None:
            aux_pin["base_url"] = base_url
        config["auxiliary"] = {slot: dict(aux_pin) for slot in defaults["auxiliary_slots"]}
        config["fallback_providers"] = []  # plan D-6: the HC owns fallback
        config["mcp_servers"] = {}  # plan D-35: no MCP
        overlay = defaults["overlay"]
        if "telemetry" in overlay:
            # The consent-modal seed must sit in the seat config.yaml itself in
            # every placement: Hermes rewrites that file when the modal is
            # answered (H0 probe b).
            config["telemetry"] = _plain(overlay["telemetry"])
        # T-5011 PR-D: the CLI tool lock sits in the seat config.yaml in every
        # placement, like the consent seed: if the overlay is skipped (it fails
        # open) an absent platform_toolsets.cli means the whole hermes-cli bundle.
        config["platform_toolsets"] = _plain(overlay["platform_toolsets"])
        check_no_contradiction(config, overlay)
        if self.placement == "merged":
            config = _deep_merge(config, overlay)
        _check_auxiliary_pinned(config, defaults["auxiliary_slots"])
        _check_cli_toolsets(config, overlay)
        return config

    def render_runtime_config(
        self,
        profile: HarnessLaunchConfig,
        defaults: Mapping[str, Any],
        *,
        account_home: Path,
    ) -> bytes:
        return _dump_yaml(self.runtime_config_data(profile, defaults, account_home=account_home))

    def forbidden_targets(self, *, account_home: Path) -> tuple[Path, ...]:
        del account_home
        return ()  # plan D-4: no byte machinery beyond the kept commit guard

    def host_defaults(self, defaults: Mapping[str, Any]) -> tuple[Path, bytes] | None:
        """The host overlay, in BOTH placements: under "merged" it is an extra
        layer over locks the seat config already carries."""
        return HOST_OVERLAY_PATH, _dump_yaml(defaults["overlay"])

    def bindings_dir(self, *, repo_root: Path, account_home: Path) -> Path:
        del repo_root  # the seat's checkout is read-only (plan §2.5 gap 2)
        return account_home / BINDINGS_RELDIR


def _check_cli_toolsets(config: Mapping[str, Any], overlay: Mapping[str, Any]) -> None:
    """Fail closed unless the rendered config carries ``platform_toolsets.cli``
    exactly as the locked defaults name it, and no top-level ``toolsets``
    (T-5011 PR-D): Hermes treats a missing CLI list as the widest bundle."""
    platform = config.get("platform_toolsets")
    rendered = platform.get("cli") if isinstance(platform, Mapping) else None
    locked = overlay["platform_toolsets"]["cli"]
    if rendered != locked:
        raise HermesRenderError("cli-toolsets", f"platform_toolsets.cli {rendered!r} is not the lock {locked!r}")
    if "toolsets" in config:
        raise HermesRenderError("cli-toolsets", "top-level toolsets must not be rendered")


def _check_auxiliary_pinned(config: Mapping[str, Any], slots: list[str]) -> None:
    auxiliary = config.get("auxiliary")
    if not isinstance(auxiliary, Mapping):
        raise HermesRenderError("auxiliary-unpinned", "no auxiliary block")
    for name, slot in auxiliary.items():
        if not isinstance(slot, Mapping):
            raise HermesRenderError("auxiliary-unpinned", str(name))
        provider = slot.get("provider")
        if provider == _AUTO_PROVIDER or (
            provider is None and slot.get("enabled") is not False
        ):
            raise HermesRenderError("auxiliary-unpinned", str(name))
    missing = [slot for slot in slots if slot not in auxiliary]
    if missing:
        raise HermesRenderError("auxiliary-unpinned", ",".join(missing))


@dataclass(frozen=True)
class HermesRenderInputs:
    """What ``prepare_hermes_launch`` needs. In H1 a duck-typed
    ``binding.hermes_render`` carries it; the H2/H4 manifest wiring builds it
    from ``harness_standing_instructions`` (artisan ruling, T-5005)."""

    persona: str
    account_home: Path
    source_path: Path
    source_sha256: str
    source_commit: str
    defaults_path: Path
    identity_heading: str = IDENTITY_HEADING


@dataclass(frozen=True)
class HermesPrepareResult:
    status: str  # "rendered" | "on-disk-equal"
    outputs: Mapping[str, str]  # account-home-relative path -> sha256
    stamp_written: bool
    write_errors: tuple[str, ...] = field(default=())
    #: T-5011 PR-D: "absent" | "0600" | "tightened" | "foreign-owner" (left alone);
    #: a symlinked or non-regular .env raises instead.
    env_file_mode: str = "absent"


def tighten_env_file(path: Path, *, fchmod: Callable[[int, int], None] = os.fchmod) -> str:
    """Make the seat's own ``.env`` 0600 (T-5011 PR-D): under ``.managed`` Hermes
    skips its own chmod (config.py:501).

    The file is opened ``O_NOFOLLOW`` and every check and the chmod act on that
    one descriptor (fstat, fchmod), so nothing outside the seat can be touched
    and there is no lstat-then-chmod race (r1 MED). Results:

    * "absent": no file.
    * "foreign-owner": a regular file another uid owns (an H5a root-owned .env),
      left untouched.
    * "0600" / "tightened": the seat's own regular file, already or now 0600.

    Refused (HermesRenderError, the launch stops): a symlink
    (``env-file-symlink``) or any non-regular file (``env-file-not-regular``).
    Hermes loads whatever the path resolves to, so a seat-owned link would let
    the seat point its key file anywhere; the seat's .env must be a regular file
    (P1). A chmod failure on the seat's own file also fails closed
    (``env-file-mode``): the key would sit group/other-readable.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise HermesRenderError("env-file-symlink", path.name) from None
        raise HermesRenderError("env-file-mode", f"{path.name}:{type(exc).__name__}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HermesRenderError("env-file-not-regular", path.name)
        if st.st_uid != os.geteuid():
            return "foreign-owner"
        if stat.S_IMODE(st.st_mode) == ENV_FILE_MODE:
            return "0600"
        try:
            fchmod(fd, ENV_FILE_MODE)
        except OSError as exc:
            raise HermesRenderError("env-file-mode", f"{path.name}:{type(exc).__name__}") from None
        return "tightened"
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Temp file in the same directory, fsync, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Make the rename durable: fsync the directory that holds the new entry."""
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _inside_repo(path: Path) -> bool:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return True
    return False


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def render_seat_files(
    inputs: HermesRenderInputs,
    launch: HarnessLaunchConfig,
    *,
    target: HermesRenderTarget | None = None,
    defaults: Mapping[str, Any] | None = None,
) -> tuple[tuple[Path, bytes], ...]:
    """The per-seat files in write order (SOUL.md first, plan §2.1), rendered in
    memory from the pinned source. Raises ``HermesRenderError`` on any integrity
    failure; never writes. ``defaults`` lets a caller that already read the
    defaults bytes pass the parse of those same bytes."""
    target = target or HermesRenderTarget()
    if defaults is None:
        defaults = load_defaults(inputs.defaults_path)
    if defaults["provenance"] != CAPTURED_PROVENANCE:
        raise HermesRenderError(
            "defaults-not-captured",
            f"provenance {defaults['provenance']!r}; H0 must capture the pinned DEFAULT_CONFIG first",
        )
    if defaults.get("auxiliary_slots_complete") is not True:
        raise HermesRenderError(
            "defaults-not-captured",
            "auxiliary_slots_complete is not true; every pinned auxiliary slot must be listed",
        )
    source = _read_bytes(inputs.source_path)
    if source is None:
        raise HermesRenderError("source-missing", str(inputs.source_path))
    if _sha256(source) != inputs.source_sha256:
        raise HermesRenderError("source-hash-mismatch", str(inputs.source_path))
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        raise HermesRenderError("source-not-utf8", str(inputs.source_path)) from None
    home = inputs.account_home
    return (
        (
            target.install_path("identity", account_home=home),
            target.render_standing(extract_identity(text, inputs.identity_heading), persona=inputs.persona),
        ),
        (
            target.install_path("runtime-config", account_home=home),
            target.render_runtime_config(launch, defaults, account_home=home),
        ),
        (
            target.install_path("project-context", account_home=home),
            target.render_standing(text, persona=inputs.persona),
        ),
        (target.harness_home(home) / MANAGED_MARKER_FILENAME, MANAGED_MARKER_BYTES),
        (target.harness_home(home) / NO_BUNDLED_SKILLS_MARKER_FILENAME, NO_BUNDLED_SKILLS_MARKER_BYTES),
    )


def prepare_hermes_launch(
    inputs: HermesRenderInputs,
    launch: HarnessLaunchConfig,
    *,
    target: HermesRenderTarget | None = None,
    writer: Callable[[Path, bytes], None] = atomic_write,
    repo_probe: Callable[[Path], bool] | None = None,
    fchmod: Callable[[int, int], None] = os.fchmod,
    install_root_resolver: Callable[[Path], Path] | None = None,
) -> HermesPrepareResult:
    """Plan §2.2 step 2: verify, render, write atomically, mark, stamp.

    ``repo_probe`` (default: walk up looking for ``.git``) is injectable
    because a test host's temp root can itself sit inside a stray repo.
    """
    target = target or HermesRenderTarget()
    if (
        inputs.account_home.name == "deepseek-fallback"
        and launch.native.get("base_url") != DEEPSEEK_PROXY_BASE_URL
    ):
        raise HermesRenderError("egress-proxy-required", DEEPSEEK_PROXY_BASE_URL)
    seat = target.seat_dir(inputs.account_home)
    if (repo_probe or _inside_repo)(seat):
        raise HermesRenderError("seat-inside-repo", str(seat))
    env_findings = dotenv_control_findings(
        inputs.account_home,
        launch.executable,
        install_root_resolver=install_root_resolver or hermes_install_root,
    )
    refused = [
        f"{path_class}:{','.join(findings)}"
        for path_class, findings in env_findings.items()
        if findings
    ]
    if refused:
        raise HermesRenderError("env-file-control", ";".join(refused))
    defaults, defaults_sha256 = read_defaults(inputs.defaults_path)  # one read: parse and hash the same bytes
    files = render_seat_files(inputs, launch, target=target, defaults=defaults)
    errors: list[str] = []
    for path, data in files:
        try:
            writer(path, data)
        except OSError as exc:
            errors.append(f"{path.name}:{type(exc).__name__}")
    if errors:
        stale = [path for path, data in files if _read_bytes(path) != data]
        if stale:
            raise HermesRenderError(
                "transient-write-failure",
                ",".join(sorted(path.name for path in stale)),
            )
        status = "on-disk-equal"
    else:
        status = "rendered"
    outputs = {
        str(path.relative_to(inputs.account_home)): _sha256(data) for path, data in files
    }
    stamp = {
        "schema": BINDINGS_SCHEMA,
        "persona": inputs.persona,
        "source_commit": inputs.source_commit,
        "source_sha256": inputs.source_sha256,
        "defaults_sha256": defaults_sha256,
        "placement": target.placement,
        "outputs": dict(sorted(outputs.items())),
    }
    stamp_bytes = (json.dumps(stamp, indent=2, sort_keys=True) + "\n").encode("utf-8")
    env_mode = tighten_env_file(target.harness_home(inputs.account_home) / ENV_FILENAME, fchmod=fchmod)
    stamp_path = target.bindings_dir(repo_root=inputs.source_path.parent, account_home=inputs.account_home)
    try:
        writer(stamp_path / BINDINGS_FILENAME, stamp_bytes)
        stamp_written = True
    except OSError:
        # The stamp is informational and never trusted (plan §2.2 step 3); a
        # failed stamp write does not block a launch on verified bytes.
        stamp_written = False
    return HermesPrepareResult(
        status=status,
        outputs=outputs,
        stamp_written=stamp_written,
        write_errors=tuple(errors),
        env_file_mode=env_mode,
    )
