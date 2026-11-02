#!/usr/bin/env python3
"""Secret-free, fail-closed fleet MCP allowlist audit (T-4679).

Runtime authority is tools/mcp_allowlist.yaml.  The scanner reports only seat,
opaque source id, category, server name, and fixed reason codes; command lines,
arguments, environment values, raw paths, and parser errors never reach output.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.mcp_allowlist_registry import (  # noqa: E402
    Registry as Authority,
    RegistryError,
    RegistryServer as AuthorityServer,
    SCHEMA,
    parse_registry,
)

PROBE_SCHEMA = "pantheon-mcp-allowlist-audit-probe/v1"
DEFAULT_MAX_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
PROBE_TIMEOUT_EXIT = 124
PROBE_INPUT_EXIT = 125
PROBE_MAX_HORIZON_SECONDS = 30.0
PROBE_MIN_TIMEOUT_SECONDS = 0.25
MAX_MANAGED_DROPINS = 64
MAX_RUNTIME_PROCESSES = 256
EXIT = {"CLEAN": 0, "DRIFT": 2, "UNKNOWN": 3}
_MISSING = object()

PROBE_BOOTSTRAP = """import builtins, math, os, signal, sys, time
def _probe_alarm(_signum, _frame):
    os._exit(124)
try:
    _deadline = float(sys.argv[1])
    _cap = int(sys.argv[2])
except (IndexError, TypeError, ValueError):
    os._exit(125)
_remaining = _deadline - time.monotonic()
if not math.isfinite(_deadline) or _cap != 1048576 or _remaining <= 0:
    os._exit(124 if _remaining <= 0 else 125)
if _remaining > 30.0:
    os._exit(125)
os.chdir('/')
signal.signal(signal.SIGALRM, _probe_alarm)
signal.setitimer(signal.ITIMER_REAL, _remaining)
_source = sys.stdin.buffer.read(_cap + 1)
if len(_source) > _cap:
    os._exit(125)
_globals = {'__name__': '__main__', '__file__': '<stdin>', '__builtins__': builtins.__dict__}
exec(compile(_source, '<stdin>', 'exec'), _globals, _globals)
"""


class AuditError(Exception):
    """An input could not be safely or completely audited."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProbeDeadlines:
    child: float
    relay: float
    hard: float


def _probe_deadlines(timeout_seconds: float, *, now: float | None = None) -> ProbeDeadlines:
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not PROBE_MIN_TIMEOUT_SECONDS <= float(timeout_seconds) <= PROBE_MAX_HORIZON_SECONDS):
        raise AuditError("PROBE_TIMEOUT_INVALID")
    start = time.monotonic() if now is None else now
    timeout = float(timeout_seconds)
    gap = min(0.5, timeout * 0.2)
    hard = start + timeout
    relay = hard - gap
    child = relay - gap
    if not start < child < relay < hard:
        raise AuditError("PROBE_TIMEOUT_INVALID")
    return ProbeDeadlines(child, relay, hard)


def _probe_python_command(child_deadline: float) -> list[str]:
    now = time.monotonic()
    if (isinstance(child_deadline, bool) or not isinstance(child_deadline, (int, float))
            or not math.isfinite(float(child_deadline))
            or not 0 < float(child_deadline) - now <= PROBE_MAX_HORIZON_SECONDS):
        raise AuditError("PROBE_TIMEOUT_INVALID")
    rendered_deadline = f"{float(child_deadline):.9f}"
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", rendered_deadline):
        raise AuditError("PROBE_TIMEOUT_INVALID")
    return ["/usr/bin/python3", "-I", "-c", PROBE_BOOTSTRAP,
            rendered_deadline, str(DEFAULT_MAX_BYTES)]


@dataclass(frozen=True, order=True)
class Finding:
    classification: str
    seat: str
    source_id: str
    category: str
    server: str
    reason: str

    def safe_dict(self) -> dict[str, str]:
        return {
            "classification": self.classification,
            "seat": self.seat,
            "source_id": self.source_id,
            "category": self.category,
            "server": self.server,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ScanResult:
    verdict: str
    findings: tuple[Finding, ...]
    seats: tuple[str, ...] = ()
    discovered: int = 0

    @classmethod
    def unknown_global(cls, reason: str) -> "ScanResult":
        return cls(
            "UNKNOWN",
            (Finding("UNKNOWN", "fleet", "global", "global", "-", reason),),
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "schema": "pantheon-mcp-allowlist-audit/v1",
            "verdict": self.verdict,
            "seats": list(self.seats),
            "discovered": self.discovered,
            "findings": [item.safe_dict() for item in self.findings],
        }

    def to_json(self) -> str:
        return json.dumps(self.safe_dict(), sort_keys=True, separators=(",", ":"))

    def to_human(self) -> str:
        lines = [
            f"MCP allowlist audit: {self.verdict} "
            f"({len(self.seats)} seats, {self.discovered} configured servers)"
        ]
        for f in self.findings:
            lines.append(
                f"{f.classification} seat={f.seat} source={f.source_id} "
                f"category={f.category} server={f.server} reason={f.reason}"
            )
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class MonitorOutcome:
    alerted: bool
    delivered: bool
    reason: str


@dataclass(frozen=True)
class Seat:
    persona: str
    account: str
    harness: str
    home: Path
    repo: Path


def _plain_name(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is not None


def _authority_from_mapping(raw: Any) -> Authority:
    try:
        return parse_registry(raw)
    except RegistryError as exc:
        raise AuditError("AUTHORITY_INVALID") from exc


def load_authority(path: Path) -> Authority:
    try:
        raw = yaml.safe_load(_read_regular_file(path, DEFAULT_MAX_BYTES))
        return _authority_from_mapping(raw)
    except AuditError:
        raise
    except Exception as exc:
        raise AuditError("AUTHORITY_INVALID") from exc


def _authority_wire(authority: Authority) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "servers": {
            name: {"seats": sorted(server.seats), "profiles": list(server.profiles)}
            for name, server in sorted(authority.servers.items())
        },
        "fingerprints": {name: dict(value) for name, value in sorted(authority.fingerprints.items())},
    }


def authority_memberships(path: Path) -> dict[str, frozenset[str]]:
    return {name: server.seats for name, server in load_authority(path).servers.items()}


def _read_regular_file(
    path: Path, max_bytes: int = DEFAULT_MAX_BYTES, *, boundary: Path | None = None
) -> bytes:
    if boundary is not None and not _safe_descendant(path, boundary):
        raise AuditError("UNSAFE_FILE_PATH")
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(info.st_mode):
        raise AuditError("UNSAFE_FILE_TYPE")
    if info.st_size > max_bytes:
        raise AuditError("FILE_OVERSIZE")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > max_bytes:
            raise AuditError("UNSAFE_FILE_TYPE" if not stat.S_ISREG(opened.st_mode) else "FILE_OVERSIZE")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise AuditError("FILE_OVERSIZE")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _opaque(label: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    return f"{label}:{digest}"


def _load_yaml_manifest(path: Path, homes_root: Path) -> tuple[list[Seat], str]:
    try:
        raw = yaml.safe_load(_read_regular_file(path, DEFAULT_MAX_BYTES))
    except Exception as exc:
        raise AuditError("MANIFEST_INVALID") from exc
    if not isinstance(raw, Mapping) or not isinstance(raw.get("roles"), Mapping):
        raise AuditError("MANIFEST_INVALID")
    installer = raw.get("persona_installer")
    paths = installer.get("paths") if isinstance(installer, Mapping) else None
    repo_rel = paths.get("repo") if isinstance(paths, Mapping) else None
    if (
        not isinstance(repo_rel, str)
        or not repo_rel
        or Path(repo_rel).is_absolute()
        or not all(_plain_name(part) for part in Path(repo_rel).parts)
    ):
        raise AuditError("MANIFEST_INVALID")
    seats: list[Seat] = []
    seen_personas: set[str] = set()
    seen_accounts: set[str] = set()
    for item in raw["roles"].values():
        if not isinstance(item, Mapping) or "persona" not in item:
            continue
        persona, account, harness = item.get("persona"), item.get("account"), item.get("harness")
        if not (_plain_name(persona) and _plain_name(account) and harness in {"claude-code", "codex"}):
            raise AuditError("MANIFEST_INVALID")
        if persona in seen_personas or account in seen_accounts:
            raise AuditError("MANIFEST_INVALID")
        seen_personas.add(persona)
        seen_accounts.add(account)
        home = homes_root / account
        seats.append(Seat(persona, account, harness, home, home / repo_rel))
    if not seats:
        raise AuditError("MANIFEST_INVALID")
    return sorted(seats, key=lambda s: s.persona), repo_rel


def _safe_descendant(path: Path, root: Path) -> bool:
    if ".." in path.parts:
        return False
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError:
        return False
    current = root
    try:
        relative = path.absolute().relative_to(root.absolute())
        for part in relative.parts[:-1]:
            current = current / part
            if current.is_symlink():
                return False
    except OSError:
        return False
    return True


def _json_optional(
    path: Path, source_id: str, findings: list[Finding], seat: str,
    category: str, max_bytes: int, *, boundary: Path,
):
    try:
        data = _read_regular_file(path, max_bytes, boundary=boundary)
    except FileNotFoundError:
        return _MISSING
    except PermissionError:
        findings.append(Finding("UNKNOWN", seat, source_id, category, "-", "READ_FAILED"))
        return _MISSING
    except AuditError as exc:
        findings.append(Finding("UNKNOWN", seat, source_id, category, "-", exc.reason))
        return _MISSING
    except OSError:
        findings.append(Finding("UNKNOWN", seat, source_id, category, "-", "READ_FAILED"))
        return _MISSING
    try:
        return json.loads(data)
    except Exception:
        findings.append(Finding("UNKNOWN", seat, source_id, category, "-", "MALFORMED_JSON"))
        return _MISSING


def _toml_optional(
    path: Path, source_id: str, findings: list[Finding], seat: str,
    max_bytes: int, *, boundary: Path,
):
    try:
        data = _read_regular_file(path, max_bytes, boundary=boundary)
    except FileNotFoundError:
        return _MISSING
    except PermissionError:
        findings.append(Finding("UNKNOWN", seat, source_id, "codex-user", "-", "READ_FAILED"))
        return _MISSING
    except AuditError as exc:
        findings.append(Finding("UNKNOWN", seat, source_id, "codex-user", "-", exc.reason))
        return _MISSING
    except OSError:
        findings.append(Finding("UNKNOWN", seat, source_id, "codex-user", "-", "READ_FAILED"))
        return _MISSING
    try:
        return tomllib.loads(data.decode("utf-8"))
    except Exception:
        findings.append(Finding("UNKNOWN", seat, source_id, "codex-user", "-", "MALFORMED_TOML"))
        return _MISSING


def _stdio_shape(entry: Mapping[str, Any]) -> tuple[str, list[str], Mapping[str, Any]] | None:
    if "url" in entry or entry.get("type", "stdio") != "stdio":
        return None
    command, args, env = entry.get("command"), entry.get("args", []), entry.get("env", {})
    if not isinstance(command, str) or not isinstance(args, list) or not all(isinstance(v, str) for v in args):
        return None
    if not isinstance(env, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
        return None
    return command, args, env


def _fingerprint_matches(
    profile: Mapping[str, Any], entry: Mapping[str, Any], *, category: str, seat: Seat, plugin_key: str | None
) -> bool:
    if profile.get("category") != category:
        return False
    allowed_fields = {"type", "command", "args", "env"}
    if category == "codex":
        allowed_fields.add("tools")
    if not set(entry).issubset(allowed_fields):
        return False
    shape = _stdio_shape(entry)
    if shape is None:
        return False
    command, args, env = shape
    if env:
        return False
    expected_command = profile.get("command")
    if category == "plugin" and expected_command == "bun":
        commands = {"bun", str(seat.home / ".bun/bin/bun")}
        if command not in commands or plugin_key != profile.get("plugin"):
            return False
    elif command != expected_command:
        return False
    if "repo_script" in profile:
        script = str(seat.repo / str(profile["repo_script"]))
        tail = profile.get("argv_tail", [])
        return args == [script, *tail]
    if "home_script" in profile:
        return args == [str(seat.home / str(profile["home_script"]))]
    if "argv_shapes" in profile:
        return args in profile["argv_shapes"]
    return False


def _record_servers(
    raw: Any,
    *,
    seat: Seat,
    authority: Authority,
    source_id: str,
    category: str,
    findings: list[Finding],
    discovered: set[tuple[str, str]],
    plugin_key: str | None = None,
) -> None:
    if raw is None:
        return
    if not isinstance(raw, Mapping):
        findings.append(Finding("UNKNOWN", seat.persona, source_id, category, "-", "WRONG_MCP_SERVERS_TYPE"))
        return
    for name, entry in sorted(raw.items(), key=lambda pair: str(pair[0])):
        safe_name = name if _plain_name(name) else "<invalid>"
        if not _plain_name(name) or not isinstance(entry, Mapping):
            findings.append(Finding("UNKNOWN", seat.persona, source_id, category, safe_name, "MALFORMED_SERVER_ENTRY"))
            continue
        discovered.add((seat.persona, name))
        allowed = authority.servers.get(name)
        if allowed is None:
            findings.append(Finding("DRIFT", seat.persona, source_id, category, name, "UNALLOWLISTED_SERVER"))
            continue
        if seat.persona not in allowed.seats:
            findings.append(Finding("DRIFT", seat.persona, source_id, category, name, "SEAT_NOT_ADMITTED"))
            continue
        broad = "plugin" if category == "plugin" else "codex" if category == "codex-user" else "claude"
        if not any(
            _fingerprint_matches(authority.fingerprints[p], entry, category=broad, seat=seat, plugin_key=plugin_key)
            for p in allowed.profiles
        ):
            findings.append(Finding("DRIFT", seat.persona, source_id, category, name, "FINGERPRINT_MISMATCH"))


def _apply_settings(
    settings: Any, *, seat: Seat, authority: Authority, source_id: str,
    findings: list[Finding], discovered: set[tuple[str, str]], enabled: dict[str, bool],
) -> None:
    if not isinstance(settings, Mapping):
        findings.append(Finding("UNKNOWN", seat.persona, source_id, "claude-settings", "-", "WRONG_ROOT_TYPE"))
        return
    _record_servers(settings.get("mcpServers"), seat=seat, authority=authority,
                    source_id=source_id, category="claude-settings",
                    findings=findings, discovered=discovered)
    plugins = settings.get("enabledPlugins", {})
    if not isinstance(plugins, Mapping):
        findings.append(Finding("UNKNOWN", seat.persona, source_id, "claude-settings", "-", "WRONG_ENABLED_PLUGINS_TYPE"))
        return
    for key, value in plugins.items():
        if not isinstance(key, str) or not isinstance(value, bool):
            findings.append(Finding("UNKNOWN", seat.persona, source_id, "claude-settings", "-", "MALFORMED_ENABLED_PLUGIN"))
        else:
            enabled[key] = value


def _runtime_settings(
    seat: Seat, proc_root: Path, max_bytes: int, findings: list[Finding]
) -> list[tuple[str, Any]]:
    try:
        root_info = proc_root.lstat()
    except FileNotFoundError:
        return []
    except OSError:
        findings.append(Finding("UNKNOWN", seat.persona, "settings:runtime", "claude-settings", "-", "READ_FAILED"))
        return []
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        findings.append(Finding("UNKNOWN", seat.persona, "settings:runtime", "claude-settings", "-", "UNSAFE_FILE_TYPE"))
        return []
    found: list[tuple[str, Any]] = []
    relevant = 0
    try:
        pids = sorted((p for p in proc_root.iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
    except OSError:
        findings.append(Finding("UNKNOWN", seat.persona, "settings:runtime", "claude-settings", "-", "READ_FAILED"))
        return []
    for base in pids:
        try:
            same_principal = base.lstat().st_uid == os.getuid()
        except FileNotFoundError:
            continue
        except OSError:
            findings.append(Finding("UNKNOWN", seat.persona, "settings:runtime", "claude-settings", "-", "READ_FAILED"))
            continue
        if not same_principal:
            continue
        try:
            raw = _read_regular_file(base / "cmdline", max_bytes, boundary=base)
            argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        except FileNotFoundError:
            continue
        except AuditError as exc:
            findings.append(Finding("UNKNOWN", seat.persona, "settings:runtime", "claude-settings", "-", exc.reason))
            continue
        except (PermissionError, OSError):
            findings.append(Finding("UNKNOWN", seat.persona, "settings:runtime", "claude-settings", "-", "READ_FAILED"))
            continue
        is_claude = any(os.path.basename(a) == "claude" for a in argv[:2]) or any(
            a.endswith("@anthropic-ai/claude-code/cli.js") for a in argv[:3]
        )
        if not is_claude:
            continue
        relevant += 1
        if relevant > MAX_RUNTIME_PROCESSES:
            findings.append(Finding("UNKNOWN", seat.persona, "settings:runtime", "claude-settings", "-", "RUNTIME_SOURCE_LIMIT"))
            break
        for index, arg in enumerate(argv):
            value = argv[index + 1] if arg == "--settings" and index + 1 < len(argv) else (
                arg.split("=", 1)[1] if arg.startswith("--settings=") else None
            )
            if value is None:
                continue
            source_id = _opaque("settings-runtime", f"{base.name}:{index}")
            if len(value.encode("utf-8", "replace")) > max_bytes:
                findings.append(Finding("UNKNOWN", seat.persona, source_id, "claude-settings", "-", "FILE_OVERSIZE"))
                continue
            if value.lstrip().startswith("{"):
                try:
                    found.append((source_id, json.loads(value)))
                except Exception:
                    findings.append(Finding("UNKNOWN", seat.persona, source_id, "claude-settings", "-", "MALFORMED_JSON"))
                continue
            try:
                cwd = Path(os.readlink(base / "cwd"))
                expanded = value.replace("~", str(seat.home), 1) if value.startswith("~") else value
                candidate = Path(expanded) if Path(expanded).is_absolute() else cwd / expanded
                if not _safe_descendant(candidate, seat.home):
                    raise AuditError("UNSAFE_FILE_PATH")
                data = _read_regular_file(candidate, max_bytes, boundary=seat.home)
                found.append((source_id, json.loads(data)))
            except FileNotFoundError:
                findings.append(Finding("UNKNOWN", seat.persona, source_id, "claude-settings", "-", "READ_FAILED"))
            except AuditError as exc:
                findings.append(Finding("UNKNOWN", seat.persona, source_id, "claude-settings", "-", exc.reason))
            except json.JSONDecodeError:
                findings.append(Finding("UNKNOWN", seat.persona, source_id, "claude-settings", "-", "MALFORMED_JSON"))
            except (OSError, PermissionError):
                findings.append(Finding("UNKNOWN", seat.persona, source_id, "claude-settings", "-", "READ_FAILED"))
    return found


def _managed_settings(
    seat: Seat, managed_root: Path, max_bytes: int, findings: list[Finding]
) -> list[tuple[str, Any]]:
    try:
        root_info = managed_root.lstat()
    except FileNotFoundError:
        return []
    except OSError:
        findings.append(Finding("UNKNOWN", seat.persona, "settings:managed", "claude-settings", "-", "READ_FAILED"))
        return []
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        findings.append(Finding("UNKNOWN", seat.persona, "settings:managed", "claude-settings", "-", "UNSAFE_FILE_TYPE"))
        return []
    paths = [(managed_root / "managed-settings.json", "settings:managed")]
    dropins = managed_root / "managed-settings.d"
    try:
        info = dropins.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise AuditError("UNSAFE_FILE_TYPE")
        entries = sorted(p for p in dropins.iterdir() if p.suffix == ".json")
        if len(entries) > MAX_MANAGED_DROPINS:
            findings.append(Finding("UNKNOWN", seat.persona, "settings:managed-dropins", "claude-settings", "-", "MANAGED_SOURCE_LIMIT"))
            entries = entries[:MAX_MANAGED_DROPINS]
        paths.extend((path, _opaque("settings-managed", path.name)) for path in entries)
    except FileNotFoundError:
        pass
    except AuditError as exc:
        findings.append(Finding("UNKNOWN", seat.persona, "settings:managed-dropins", "claude-settings", "-", exc.reason))
    except OSError:
        findings.append(Finding("UNKNOWN", seat.persona, "settings:managed-dropins", "claude-settings", "-", "READ_FAILED"))
    result: list[tuple[str, Any]] = []
    for path, source_id in paths:
        value = _json_optional(path, source_id, findings, seat.persona, "claude-settings", max_bytes, boundary=managed_root)
        if value is not _MISSING:
            result.append((source_id, value))
    return result


def _scan_seat(
    seat: Seat, authority: Authority, max_bytes: int, *, managed_root: Path, proc_root: Path,
) -> tuple[list[Finding], set[tuple[str, str]]]:
    findings: list[Finding] = []
    discovered: set[tuple[str, str]] = set()
    try:
        home_info = seat.home.lstat()
        repo_info = seat.repo.lstat()
    except (FileNotFoundError, PermissionError, OSError):
        findings.append(Finding("UNKNOWN", seat.persona, "manifest-root", "global", "-", "MANIFEST_ROOT_UNAVAILABLE"))
        return findings, discovered
    if (
        not stat.S_ISDIR(home_info.st_mode)
        or not stat.S_ISDIR(repo_info.st_mode)
        or stat.S_ISLNK(home_info.st_mode)
        or stat.S_ISLNK(repo_info.st_mode)
        or not _safe_descendant(seat.repo / ".mcp.json", seat.home)
    ):
        findings.append(Finding("UNKNOWN", seat.persona, "manifest-root", "global", "-", "UNSAFE_MANIFEST_ROOT"))
        return findings, discovered
    project_roots: set[Path] = {seat.repo}
    user = _json_optional(seat.home / ".claude.json", "claude:user", findings, seat.persona, "claude-user", max_bytes, boundary=seat.home)
    if user is not _MISSING:
        if not isinstance(user, Mapping):
            findings.append(Finding("UNKNOWN", seat.persona, "claude:user", "claude-user", "-", "WRONG_ROOT_TYPE"))
        else:
            _record_servers(user.get("mcpServers"), seat=seat, authority=authority, source_id="claude:user", category="claude-user", findings=findings, discovered=discovered)
            projects = user.get("projects", {})
            if not isinstance(projects, Mapping):
                findings.append(Finding("UNKNOWN", seat.persona, "claude:projects", "claude-project", "-", "WRONG_PROJECTS_TYPE"))
            else:
                for raw_path, config in projects.items():
                    source_id = _opaque("claude-project", str(raw_path))
                    if not isinstance(config, Mapping):
                        findings.append(Finding("UNKNOWN", seat.persona, source_id, "claude-project", "-", "WRONG_PROJECT_CONFIG_TYPE"))
                    elif "mcpServers" in config:
                        _record_servers(config.get("mcpServers"), seat=seat, authority=authority, source_id=source_id, category="claude-project", findings=findings, discovered=discovered)
                    candidate = Path(raw_path) if isinstance(raw_path, str) else Path("/")
                    if (not isinstance(raw_path, str) or not candidate.is_absolute()
                            or not _safe_descendant(candidate / ".mcp.json", seat.home)):
                        findings.append(Finding("UNKNOWN", seat.persona, source_id, "claude-project", "-", "UNSAFE_PROJECT_PATH"))
                        continue
                    try:
                        info = candidate.lstat()
                    except FileNotFoundError:
                        continue
                    except (PermissionError, OSError):
                        findings.append(Finding("UNKNOWN", seat.persona, source_id, "claude-project", "-", "UNSAFE_PROJECT_PATH"))
                        continue
                    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                        findings.append(Finding("UNKNOWN", seat.persona, source_id, "claude-project", "-", "UNSAFE_PROJECT_PATH"))
                        continue
                    project_roots.add(candidate)

    for project_root in sorted(project_roots, key=str):
        project_source = _opaque("project-mcp", str(project_root))
        project = _json_optional(project_root / ".mcp.json", project_source, findings, seat.persona, "project-mcp", max_bytes, boundary=seat.home)
        if project is not _MISSING:
            if not isinstance(project, Mapping):
                findings.append(Finding("UNKNOWN", seat.persona, project_source, "project-mcp", "-", "WRONG_ROOT_TYPE"))
            else:
                _record_servers(project.get("mcpServers"), seat=seat, authority=authority, source_id=project_source, category="project-mcp", findings=findings, discovered=discovered)

    enabled: dict[str, bool] = {}
    settings_paths = (
        (seat.home / ".claude/settings.json", "settings:user"),
        (seat.repo / ".claude/settings.json", "settings:project"),
        (seat.repo / ".claude/settings.local.json", "settings:local"),
    )
    for path, source_id in settings_paths:
        settings = _json_optional(path, source_id, findings, seat.persona, "claude-settings", max_bytes, boundary=seat.home)
        if settings is _MISSING:
            continue
        _apply_settings(settings, seat=seat, authority=authority, source_id=source_id,
                        findings=findings, discovered=discovered, enabled=enabled)

    runtime_enabled: dict[str, bool] = {}
    for source_id, settings in _runtime_settings(seat, proc_root, max_bytes, findings):
        one: dict[str, bool] = {}
        _apply_settings(settings, seat=seat, authority=authority, source_id=source_id,
                        findings=findings, discovered=discovered, enabled=one)
        for key, value in one.items():
            runtime_enabled[key] = runtime_enabled.get(key, False) or value
    enabled.update(runtime_enabled)
    for source_id, settings in _managed_settings(seat, managed_root, max_bytes, findings):
        _apply_settings(settings, seat=seat, authority=authority, source_id=source_id,
                        findings=findings, discovered=discovered, enabled=enabled)

    active = {key for key, value in enabled.items() if value}
    if active:
        installed_path = seat.home / ".claude/plugins/installed_plugins.json"
        installed = _json_optional(installed_path, "plugins:installed", findings, seat.persona, "plugin", max_bytes, boundary=seat.home)
        if not isinstance(installed, Mapping) or not isinstance(installed.get("plugins"), Mapping):
            findings.append(Finding("UNKNOWN", seat.persona, "plugins:installed", "plugin", "-", "INSTALLED_REGISTRY_INVALID"))
        else:
            registry = installed["plugins"]
            for key in sorted(active):
                entries = registry.get(key)
                if entries is None:
                    findings.append(Finding("UNKNOWN", seat.persona, _opaque("plugin", key), "plugin", "-", "ENABLED_PLUGIN_NOT_INSTALLED"))
                    continue
                if not isinstance(entries, list):
                    entries = [entries]
                for index, item in enumerate(entries):
                    source_id = _opaque("plugin", f"{key}:{index}")
                    root_raw = item.get("installPath") if isinstance(item, Mapping) else None
                    root = Path(root_raw) if isinstance(root_raw, str) else Path("/")
                    plugin_base = seat.home / ".claude/plugins/cache"
                    if not _safe_descendant(root / ".mcp.json", plugin_base):
                        findings.append(Finding("UNKNOWN", seat.persona, source_id, "plugin", "-", "UNSAFE_PLUGIN_PATH"))
                        continue
                    manifest = _json_optional(root / ".claude-plugin/plugin.json", source_id, findings, seat.persona, "plugin", max_bytes, boundary=seat.home)
                    if not isinstance(manifest, Mapping):
                        findings.append(Finding("UNKNOWN", seat.persona, source_id, "plugin", "-", "PLUGIN_MANIFEST_INVALID"))
                        continue
                    mcp = _json_optional(root / ".mcp.json", source_id, findings, seat.persona, "plugin", max_bytes, boundary=seat.home)
                    if mcp is _MISSING:
                        continue
                    if not isinstance(mcp, Mapping):
                        findings.append(Finding("UNKNOWN", seat.persona, source_id, "plugin", "-", "WRONG_ROOT_TYPE"))
                        continue
                    _record_servers(mcp.get("mcpServers"), seat=seat, authority=authority, source_id=source_id, category="plugin", findings=findings, discovered=discovered, plugin_key=key)

    codex = _toml_optional(seat.home / ".codex/config.toml", "codex:user", findings, seat.persona, max_bytes, boundary=seat.home)
    if codex is not _MISSING:
        if not isinstance(codex, Mapping):
            findings.append(Finding("UNKNOWN", seat.persona, "codex:user", "codex-user", "-", "WRONG_ROOT_TYPE"))
        else:
            _record_servers(codex.get("mcp_servers"), seat=seat, authority=authority, source_id="codex:user", category="codex-user", findings=findings, discovered=discovered)
    return findings, discovered


def _finish(findings: Iterable[Finding], seats: Iterable[str], discovered: set[tuple[str, str]]) -> ScanResult:
    unique = tuple(sorted(set(findings)))
    verdict = "UNKNOWN" if any(f.classification == "UNKNOWN" for f in unique) else (
        "DRIFT" if any(f.classification == "DRIFT" for f in unique) else "CLEAN"
    )
    return ScanResult(verdict, unique, tuple(sorted(set(seats))), len(discovered))


def scan_fleet(
    *, authority_path: Path, manifest_path: Path, homes_root: Path,
    managed_root: Path = Path("/etc/claude-code"), proc_root: Path = Path("/proc"),
    max_bytes: int = DEFAULT_MAX_BYTES, only_seat: str | None = None,
) -> ScanResult:
    try:
        authority = load_authority(authority_path)
        seats, _ = _load_yaml_manifest(manifest_path, homes_root)
    except AuditError as exc:
        return ScanResult.unknown_global(exc.reason)
    if only_seat is not None:
        seats = [seat for seat in seats if seat.persona == only_seat]
        if not seats:
            return ScanResult.unknown_global("MANIFEST_SEAT_MISSING")
    findings: list[Finding] = []
    discovered: set[tuple[str, str]] = set()
    for seat in seats:
        seat_findings, seat_discovered = _scan_seat(
            seat, authority, max_bytes, managed_root=managed_root, proc_root=proc_root
        )
        findings.extend(seat_findings)
        discovered.update(seat_discovered)
    return _finish(findings, (seat.persona for seat in seats), discovered)


def _reader_unknown(seat: str, reason: str) -> ScanResult:
    return ScanResult("UNKNOWN", (Finding("UNKNOWN", seat, "seat-probe", "global", "-", reason),), (seat,))


def run_probe_command(
    command: Sequence[str], *, seat: str, timeout_seconds: float | None = None, input_text: str = "",
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES, account: str | None = None,
    cwd: Path | None = None, deadlines: ProbeDeadlines | None = None,
) -> ScanResult:
    try:
        if deadlines is None:
            if timeout_seconds is None:
                raise AuditError("PROBE_TIMEOUT_INVALID")
            deadlines = _probe_deadlines(timeout_seconds)
        elif (not deadlines.child < deadlines.relay < deadlines.hard
              or deadlines.hard - time.monotonic() > PROBE_MAX_HORIZON_SECONDS
              or deadlines.hard <= time.monotonic()):
            raise AuditError("PROBE_TIMEOUT_INVALID")
    except AuditError:
        return _reader_unknown(seat, "READER_TIMEOUT")
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    oversized = threading.Event()
    timeout_transition = False
    relay_sent = False
    proc: subprocess.Popen[bytes] | None = None
    threads: tuple[threading.Thread, ...] = ()
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=cwd, start_new_session=True)
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

        def relay_termination() -> None:
            nonlocal relay_sent
            if relay_sent or proc is None or proc.poll() is not None:
                return
            relay_sent = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        def read_capped(stream: Any, target: list[bytes]) -> None:
            data = stream.read(max_output_bytes + 1)
            target.append(data)
            if len(data) > max_output_bytes:
                oversized.set()

        def write_input() -> None:
            try:
                proc.stdin.write(input_text.encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass

        stdout_thread = threading.Thread(target=read_capped, args=(proc.stdout, stdout_parts), daemon=True)
        stderr_thread = threading.Thread(target=read_capped, args=(proc.stderr, stderr_parts), daemon=True)
        stdin_thread = threading.Thread(target=write_input, daemon=True)
        threads = (stdin_thread, stdout_thread, stderr_thread)
        stdout_thread.start()
        stderr_thread.start()
        stdin_thread.start()

        while proc.poll() is None:
            now = time.monotonic()
            if oversized.is_set():
                relay_termination()
            elif now >= deadlines.relay:
                timeout_transition = True
                relay_termination()
            if now >= deadlines.hard:
                timeout_transition = True
                break
            time.sleep(min(0.005, max(0.0, deadlines.hard - now)))

        if proc.poll() is None:
            relay_termination()
            try:
                proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=0.2)
        returncode = proc.wait()
        for thread in threads:
            thread.join(timeout=max(0.0, deadlines.hard - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            timeout_transition = True
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            for thread in threads:
                thread.join(timeout=0.1)
        if any(thread.is_alive() for thread in threads):
            return _reader_unknown(seat, "READER_TIMEOUT")
        stdout = b"".join(stdout_parts).decode("utf-8", "replace")
        stderr = b"".join(stderr_parts).decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return _reader_unknown(seat, "READER_TIMEOUT")
    except OSError:
        return _reader_unknown(seat, "READER_FAILED")
    finally:
        if proc is not None:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
    if oversized.is_set():
        return _reader_unknown(seat, "READER_OUTPUT_OVERSIZE")
    if timeout_transition or returncode == PROBE_TIMEOUT_EXIT:
        return _reader_unknown(seat, "READER_TIMEOUT")
    if stderr:
        return _reader_unknown(seat, "READER_STDERR_NONEMPTY")
    try:
        envelope = json.loads(stdout)
        if not isinstance(envelope, Mapping) or set(envelope) != {"schema", "account", "audit"}:
            raise ValueError
        if envelope["schema"] != PROBE_SCHEMA or envelope["account"] != (account or seat):
            return _reader_unknown(seat, "READER_PRINCIPAL_MISMATCH")
        raw = envelope["audit"]
        if not isinstance(raw, Mapping) or set(raw) != {"schema", "verdict", "seats", "discovered", "findings"}:
            raise ValueError
        if raw["schema"] != "pantheon-mcp-allowlist-audit/v1" or raw["seats"] != [seat]:
            raise ValueError
        if (not isinstance(raw["findings"], list)
                or type(raw["discovered"]) is not int or raw["discovered"] < 0):
            raise ValueError
        fields = {"classification", "seat", "source_id", "category", "server", "reason"}
        if any(not isinstance(item, Mapping) or set(item) != fields or item.get("seat") not in {seat, "fleet"}
               or not all(isinstance(v, str) for v in item.values())
               or item["classification"] not in {"DRIFT", "UNKNOWN"}
               or item["category"] not in {"global", "claude-user", "claude-project", "project-mcp", "claude-settings", "plugin", "codex-user"}
               or not re.fullmatch(r"[A-Za-z0-9:._<>-]{1,160}", item["source_id"])
               or not (item["server"] in {"-", "<invalid>"} or _plain_name(item["server"]))
               or not _plain_name(item["reason"])
               for item in raw["findings"]):
            raise ValueError
        findings = tuple(Finding(**item) for item in raw["findings"])
        result = ScanResult(str(raw["verdict"]), findings, tuple(raw["seats"]), int(raw["discovered"]))
    except Exception:
        return _reader_unknown(seat, "READER_OUTPUT_INVALID")
    expected_verdict = "UNKNOWN" if any(f.classification == "UNKNOWN" for f in result.findings) else (
        "DRIFT" if result.findings else "CLEAN"
    )
    if result.verdict not in EXIT or result.verdict != expected_verdict or returncode != EXIT[result.verdict]:
        return _reader_unknown(seat, "READER_OUTPUT_INVALID")
    return result


def _run_embedded_probe(spec: Any) -> int:
    actual_account = "unknown"
    try:
        if not isinstance(spec, Mapping) or spec.get("schema") != PROBE_SCHEMA:
            raise AuditError("PROBE_SPEC_INVALID")
        account = spec.get("account")
        actual_account = pwd.getpwuid(os.getuid()).pw_name
        if not _plain_name(account) or actual_account != account:
            raise AuditError("PRINCIPAL_MISMATCH")
        seat = Seat(str(spec["persona"]), account, str(spec["harness"]),
                    Path(str(spec["home"])), Path(str(spec["repo"])))
        authority = _authority_from_mapping(spec["authority"])
        max_bytes = int(spec["max_bytes"])
        seat_findings, seat_discovered = _scan_seat(
            seat, authority, max_bytes, managed_root=Path(str(spec["managed_root"])),
            proc_root=Path(str(spec["proc_root"])),
        )
        result = _finish(seat_findings, (seat.persona,), seat_discovered)
    except (AuditError, KeyError, TypeError, ValueError, OSError):
        result = ScanResult.unknown_global("PROBE_SPEC_INVALID")
        result = ScanResult(result.verdict, result.findings, (str(spec.get("persona", "fleet")),), 0) if isinstance(spec, Mapping) else result
    print(json.dumps({"schema": PROBE_SCHEMA, "account": actual_account,
                      "audit": result.safe_dict()}, sort_keys=True, separators=(",", ":")))
    return EXIT[result.verdict]


def build_probe_invocation(
    seat: Seat, *, authority: Authority, max_bytes: int, managed_root: Path, proc_root: Path,
    child_deadline: float,
) -> tuple[list[str], str]:
    from tools.seat_exec import seat_argv

    spec = {
        "schema": PROBE_SCHEMA, "persona": seat.persona, "account": seat.account,
        "harness": seat.harness, "home": str(seat.home), "repo": str(seat.repo),
        "authority": _authority_wire(authority), "max_bytes": max_bytes,
        "managed_root": str(managed_root), "proc_root": str(proc_root),
    }
    registry_path = Path(__file__).with_name("mcp_allowlist_registry.py")
    try:
        source = _read_regular_file(Path(__file__), DEFAULT_MAX_BYTES).decode("utf-8")
        registry_source = _read_regular_file(registry_path, DEFAULT_MAX_BYTES).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AuditError("PROBE_SOURCE_INVALID") from exc
    trailer = '\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
    if trailer not in source:
        raise AuditError("PROBE_SOURCE_INVALID")
    source = source.replace(trailer, "\n")
    bootstrap = (
        "import os, sys, types\n"
        "os.chdir('/')\n"
        "_registry_module = types.ModuleType('tools.mcp_allowlist_registry')\n"
        "sys.modules['tools.mcp_allowlist_registry'] = _registry_module\n"
        f"exec({registry_source!r}, _registry_module.__dict__)\n"
    )
    future_line = "from __future__ import annotations\n"
    if future_line not in source:
        raise AuditError("PROBE_SOURCE_INVALID")
    source = source.replace(future_line, future_line + bootstrap, 1)
    source += f"\nraise SystemExit(_run_embedded_probe({json.dumps(spec, sort_keys=True)}))\n"
    if len(source.encode("utf-8")) > DEFAULT_MAX_BYTES:
        raise AuditError("PROBE_SOURCE_INVALID")
    return seat_argv(seat.account, _probe_python_command(child_deadline)), source


def scan_fleet_via_probes(
    *, authority_path: Path, manifest_path: Path, homes_root: Path,
    managed_root: Path, proc_root: Path, max_bytes: int, timeout_seconds: float,
) -> ScanResult:
    try:
        authority = load_authority(authority_path)
        seats, _ = _load_yaml_manifest(manifest_path, homes_root)
    except AuditError as exc:
        return ScanResult.unknown_global(exc.reason)
    all_findings: list[Finding] = []
    discovered = 0
    for seat in seats:
        try:
            deadlines = _probe_deadlines(timeout_seconds)
            command, source = build_probe_invocation(
                seat, authority=authority, max_bytes=max_bytes,
                managed_root=managed_root, proc_root=proc_root,
                child_deadline=deadlines.child,
            )
        except (AuditError, OSError):
            result = _reader_unknown(seat.persona, "READER_FAILED")
        else:
            result = run_probe_command(command, seat=seat.persona, deadlines=deadlines,
                                       input_text=source, account=seat.account)
        all_findings.extend(result.findings)
        discovered += result.discovered
    merged = _finish(all_findings, (s.persona for s in seats), set())
    return ScanResult(merged.verdict, merged.findings, merged.seats, discovered)


def _result_digest(result: ScanResult) -> str:
    payload = json.dumps(
        {"verdict": result.verdict, "findings": [f.safe_dict() for f in result.findings]},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _atomic_write(path: Path, data: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _deliver_mailbox(result: ScanResult, mailbox_root: Path) -> None:
    from tools import mailbox

    payload = {
        "schema": "pantheon-mcp-allowlist-alert/v1",
        "audit": result.safe_dict(),
    }
    mailbox.send(mailbox_root / "orchestrator", payload, sender="recovery",
                 subject=f"MCP allowlist audit {result.verdict}")


def monitor_result(result: ScanResult, state_path: Path, mailbox_root: Path, *, dry_run: bool = False) -> MonitorOutcome:
    if result.verdict == "CLEAN":
        had_state = state_path.exists()
        if not dry_run:
            try:
                state_path.unlink()
            except FileNotFoundError:
                pass
        return MonitorOutcome(False, True, "RECOVERED" if had_state else "CLEAN")
    digest = _result_digest(result)
    previous = None
    try:
        raw = json.loads(_read_regular_file(state_path, DEFAULT_MAX_BYTES))
        previous = raw.get("digest") if isinstance(raw, Mapping) else None
    except FileNotFoundError:
        pass
    except Exception:
        previous = None
    if previous == digest:
        return MonitorOutcome(False, True, "UNCHANGED")
    if dry_run:
        return MonitorOutcome(True, False, "DRY_RUN")
    try:
        _deliver_mailbox(result, mailbox_root)
    except Exception:
        return MonitorOutcome(True, False, "MAILBOX_DELIVERY_FAILED")
    try:
        _atomic_write(state_path, json.dumps({"schema": "pantheon-mcp-allowlist-state/v1", "digest": digest}, sort_keys=True) + "\n")
    except OSError:
        return MonitorOutcome(True, False, "STATE_WRITE_FAILED")
    return MonitorOutcome(True, True, "ALERTED")


def install_units(repo_root: Path, home: Path, *, runner: Callable[[Sequence[str]], int]) -> tuple[str, ...]:
    unit_dir = home / ".config/systemd/user"
    state_dir = home / ".local/state/pantheon"
    try:
        home.mkdir(parents=True, exist_ok=True)
        current = home
        for part in (".local", "state", "pantheon"):
            current = current / part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise AuditError("UNSAFE_STATE_DIR")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(state_dir, flags)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISDIR(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                raise AuditError("UNSAFE_STATE_DIR")
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    except AuditError:
        raise
    except OSError as exc:
        raise AuditError("UNSAFE_STATE_DIR") from exc
    changed: list[str] = []
    for name in ("pantheon-mcp-allowlist-audit.service", "pantheon-mcp-allowlist-audit.timer"):
        source = repo_root / "tools/systemd" / name
        desired = _read_regular_file(source, DEFAULT_MAX_BYTES)
        target = unit_dir / name
        try:
            current = _read_regular_file(target, DEFAULT_MAX_BYTES)
        except FileNotFoundError:
            current = None
        if current != desired:
            _atomic_write(target, desired.decode("utf-8"), 0o644)
            changed.append(name)
    if changed:
        if runner(("systemctl", "--user", "daemon-reload")) != 0:
            raise AuditError("DAEMON_RELOAD_FAILED")
    return tuple(changed)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "monitor", "_probe-seat"):
        p = sub.add_parser(name)
        p.add_argument("--authority", type=Path, default=Path(__file__).resolve().with_name("mcp_allowlist.yaml"))
        p.add_argument("--manifest", type=Path, default=Path(__file__).resolve().parent.parent / "instance.yaml")
        p.add_argument("--homes-root", type=Path, default=Path("/home"))
        p.add_argument("--managed-root", type=Path, default=Path("/etc/claude-code"))
        p.add_argument("--proc-root", type=Path, default=Path("/proc"))
        p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
        if name != "_probe-seat":
            p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
            p.add_argument("--format", choices=("human", "json"), default="human")
        else:
            p.add_argument("--seat", required=True)
    monitor = sub.choices["monitor"]
    monitor.add_argument("--state", type=Path, default=Path.home() / ".local/state/pantheon/mcp-allowlist-audit.json")
    monitor.add_argument("--mailbox-root", type=Path, default=Path("/home/post-office/mailbox"))
    monitor.add_argument("--dry-run", action="store_true")
    install = sub.add_parser("install")
    install.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    install.add_argument("--home", type=Path, default=Path.home())
    return parser


def _run_systemctl(argv: Sequence[str]) -> int:
    return subprocess.run(argv, check=False).returncode


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "install":
        try:
            changed = install_units(args.repo_root, args.home, runner=_run_systemctl)
        except AuditError as exc:
            print(exc.reason, file=sys.stderr)
            return 1
        print(json.dumps({"changed": list(changed), "activation": "operator-managed"}, sort_keys=True))
        return 0
    if args.command == "_probe-seat":
        result = scan_fleet(authority_path=args.authority, manifest_path=args.manifest,
                            homes_root=args.homes_root, managed_root=args.managed_root,
                            proc_root=args.proc_root, max_bytes=args.max_bytes, only_seat=args.seat)
        print(result.to_json())
        return EXIT[result.verdict]
    if args.homes_root == Path("/home"):
        result = scan_fleet_via_probes(
            authority_path=args.authority, manifest_path=args.manifest, homes_root=args.homes_root,
            managed_root=args.managed_root, proc_root=args.proc_root,
            max_bytes=args.max_bytes, timeout_seconds=args.timeout,
        )
    else:
        result = scan_fleet(authority_path=args.authority, manifest_path=args.manifest,
                            homes_root=args.homes_root, managed_root=args.managed_root,
                            proc_root=args.proc_root, max_bytes=args.max_bytes)
    print(result.to_json() if args.format == "json" else result.to_human(), end="" if args.format == "human" else "\n")
    if args.command == "monitor":
        outcome = monitor_result(result, args.state, args.mailbox_root, dry_run=args.dry_run)
        if not outcome.delivered and not args.dry_run:
            print(outcome.reason, file=sys.stderr)
            return 1
        return 0
    return EXIT[result.verdict]


if __name__ == "__main__":
    raise SystemExit(main())
