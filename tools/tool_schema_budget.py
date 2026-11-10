"""Measure loaded MCP/tool-definition budget by Pantheon worker profile.

The measurement is intentionally read-only. It resolves worker profiles from
``instance.yaml`` and, when ``--probe`` is set, asks configured stdio MCP
servers for ``tools/list``. Token counts are tokenizer-free estimates based on
canonical JSON size; they are for thresholding and drift detection, not billing.

Usage:
    python3 -m tools.tool_schema_budget --manifest instance.yaml --probe
    python3 -m tools.tool_schema_budget --manifest instance.yaml --probe --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import selectors
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml


TIERS = (25_000, 50_000, 100_000)


@dataclass(frozen=True)
class ServerSpec:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    source: str = ""


@dataclass(frozen=True)
class ServerMeasurement:
    name: str
    source: str
    status: str
    tool_count: int
    schema_chars: int
    estimated_schema_tokens: int
    config_chars: int
    estimated_config_tokens: int
    error: str = ""


@dataclass(frozen=True)
class ProfileMeasurement:
    role: str
    persona: str
    account: str
    harness: str
    profile: str
    model: str | None
    tier: str
    tool_count: int
    schema_chars: int
    estimated_schema_tokens: int
    config_chars: int
    estimated_config_tokens: int
    servers: tuple[ServerMeasurement, ...]
    unmeasured_surfaces: tuple[str, ...] = ()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def estimate_tokens(text: str) -> int:
    """Cheap model-agnostic estimate for schema-budget tiering.

    Claude-token exactness would require a provider tokenizer/API call. For a
    local, no-spend budget tripwire we use the common four-characters-per-token
    approximation and round up so estimates err slightly high at small sizes.
    """

    if not text:
        return 0
    return math.ceil(len(text) / 4)


def tier_for(tokens: int) -> str:
    if tokens > 100_000:
        return ">100K"
    if tokens > 50_000:
        return ">50K"
    if tokens > 25_000:
        return ">25K"
    return "<=25K"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _server_specs_for_claude_persona(manifest: Mapping[str, Any], persona: str) -> tuple[ServerSpec, ...]:
    installer = _mapping(manifest.get("persona_installer"))
    catalog = _mapping(installer.get("claude_mcp_servers"))
    personas = _mapping(catalog.get("personas"))
    stanza = _mapping(personas.get(persona))
    servers = _mapping(stanza.get("servers"))
    specs: list[ServerSpec] = []
    for name, raw in sorted(servers.items()):
        server = _mapping(raw)
        command = server.get("command")
        if not isinstance(command, str) or not command:
            continue
        args_raw = server.get("args", [])
        args = tuple(str(item) for item in args_raw) if isinstance(args_raw, list) else ()
        env_raw = server.get("env", {})
        env = {str(k): str(v) for k, v in env_raw.items()} if isinstance(env_raw, Mapping) else {}
        specs.append(
            ServerSpec(
                name=str(name),
                command=command,
                args=args,
                env=env,
                source=f"persona_installer.claude_mcp_servers.personas.{persona}.servers.{name}",
            )
        )
    return tuple(specs)


def _configured_profile(manifest: Mapping[str, Any], role: str, harness: str, role_harness: Mapping[str, Any]) -> str:
    explicit = role_harness.get("profile")
    if isinstance(explicit, str) and explicit:
        return explicit
    runtime = _mapping(manifest.get("harness_runtime"))
    harnesses = _mapping(runtime.get("harnesses"))
    harness_block = _mapping(harnesses.get(harness))
    default = harness_block.get("default_profile")
    if isinstance(default, str) and default:
        return default
    return "unknown"


_UNMEASURED_HARNESSES = frozenset({"hermes"})


def iter_profile_specs(manifest: Mapping[str, Any]) -> Iterable[tuple[ProfileMeasurement, tuple[ServerSpec, ...]]]:
    roles = _mapping(manifest.get("roles"))
    runtime = _mapping(manifest.get("harness_runtime"))
    runtime_roles = _mapping(runtime.get("roles"))
    for role, role_info_raw in sorted(roles.items()):
        role_info = _mapping(role_info_raw)
        persona = str(role_info.get("persona") or role)
        account = str(role_info.get("account") or "")
        runtime_role = _mapping(runtime_roles.get(role))
        # T-5011 PR-B: say so, instead of skipping in silence, when a role
        # declares a harness this budget does not measure (hermes: no MCP).
        for other in sorted(set(runtime_role) & _UNMEASURED_HARNESSES):
            print(f"tool_schema_budget: {role}.{other} not measured (no MCP surface)", file=sys.stderr)
        for harness in ("claude", "codex"):
            role_harness = _mapping(runtime_role.get(harness))
            if not role_harness:
                continue
            profile = _configured_profile(manifest, str(role), harness, role_harness)
            model = role_harness.get("model")
            native = _mapping(role_harness.get("native"))
            unmeasured: list[str] = []
            if harness == "claude":
                specs = _server_specs_for_claude_persona(manifest, persona)
                channels = native.get("channels")
                if channels:
                    unmeasured.append(f"claude native channels: {channels}")
            else:
                specs = ()
                if account:
                    unmeasured.append(f"codex native config not in manifest: /home/{account}/.codex/config.toml")
            shell = ProfileMeasurement(
                role=str(role),
                persona=persona,
                account=account,
                harness=harness,
                profile=profile,
                model=str(model) if isinstance(model, str) else None,
                tier="<=25K",
                tool_count=0,
                schema_chars=0,
                estimated_schema_tokens=0,
                config_chars=0,
                estimated_config_tokens=0,
                servers=(),
                unmeasured_surfaces=tuple(unmeasured),
            )
            yield shell, specs


def _ask_jsonrpc(proc: subprocess.Popen[str], message: Mapping[str, Any], *, timeout: float) -> Mapping[str, Any] | None:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()
    if "id" not in message:
        return None
    target = message["id"]
    # T-5872: selectors (poll), not select.select() — the latter raises
    # ValueError for any fd >= FD_SETSIZE (1024).
    with selectors.PollSelector() as sel:
        sel.register(proc.stdout, selectors.EVENT_READ)
        while True:
            if not sel.select(timeout):
                raise TimeoutError(f"timed out waiting for response id={target}")
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError(f"server exited before response id={target}")
            reply = json.loads(line)
            if reply.get("id") == target:
                return reply


def probe_stdio_tools(spec: ServerSpec, *, cwd: Path, timeout: float) -> list[Mapping[str, Any]]:
    env = dict(os.environ)
    env.update(spec.env)
    proc = subprocess.Popen(
        [spec.command, *spec.args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd),
        env=env,
    )
    try:
        init = _ask_jsonrpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pantheon-tool-schema-budget", "version": "1"},
                },
            },
            timeout=timeout,
        )
        if init and "error" in init:
            raise RuntimeError(canonical_json(init["error"]))
        _ask_jsonrpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=timeout)
        listed = _ask_jsonrpc(
            proc,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            timeout=timeout,
        )
        if not listed:
            raise RuntimeError("tools/list returned no response")
        if "error" in listed:
            raise RuntimeError(canonical_json(listed["error"]))
        tools = _mapping(listed.get("result")).get("tools", [])
        if not isinstance(tools, list):
            raise RuntimeError("tools/list result.tools is not a list")
        return [item for item in tools if isinstance(item, Mapping)]
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()


def measure_server(spec: ServerSpec, *, probe: bool, cwd: Path, timeout: float) -> ServerMeasurement:
    config_text = canonical_json(
        {
            "name": spec.name,
            "command": spec.command,
            "args": list(spec.args),
            "env_keys": sorted(spec.env),
        }
    )
    if not probe:
        return ServerMeasurement(
            name=spec.name,
            source=spec.source,
            status="not_probed",
            tool_count=0,
            schema_chars=0,
            estimated_schema_tokens=0,
            config_chars=len(config_text),
            estimated_config_tokens=estimate_tokens(config_text),
        )
    try:
        tools = probe_stdio_tools(spec, cwd=cwd, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - measurement should report per-server failures.
        return ServerMeasurement(
            name=spec.name,
            source=spec.source,
            status="probe_failed",
            tool_count=0,
            schema_chars=0,
            estimated_schema_tokens=0,
            config_chars=len(config_text),
            estimated_config_tokens=estimate_tokens(config_text),
            error=f"{type(exc).__name__}: {exc}",
        )
    schema_text = canonical_json(tools)
    return ServerMeasurement(
        name=spec.name,
        source=spec.source,
        status="probed",
        tool_count=len(tools),
        schema_chars=len(schema_text),
        estimated_schema_tokens=estimate_tokens(schema_text),
        config_chars=len(config_text),
        estimated_config_tokens=estimate_tokens(config_text),
    )


def measure_profiles(
    manifest: Mapping[str, Any],
    *,
    probe: bool,
    cwd: Path,
    timeout: float,
    persona_filter: str | None = None,
    role_filter: str | None = None,
) -> tuple[ProfileMeasurement, ...]:
    results: list[ProfileMeasurement] = []
    for shell, specs in iter_profile_specs(manifest):
        if persona_filter and shell.persona != persona_filter:
            continue
        if role_filter and shell.role != role_filter:
            continue
        servers = tuple(measure_server(spec, probe=probe, cwd=cwd, timeout=timeout) for spec in specs)
        schema_chars = sum(server.schema_chars for server in servers)
        schema_tokens = sum(server.estimated_schema_tokens for server in servers)
        config_chars = sum(server.config_chars for server in servers)
        config_tokens = sum(server.estimated_config_tokens for server in servers)
        results.append(
            ProfileMeasurement(
                role=shell.role,
                persona=shell.persona,
                account=shell.account,
                harness=shell.harness,
                profile=shell.profile,
                model=shell.model,
                tier=tier_for(schema_tokens),
                tool_count=sum(server.tool_count for server in servers),
                schema_chars=schema_chars,
                estimated_schema_tokens=schema_tokens,
                config_chars=config_chars,
                estimated_config_tokens=config_tokens,
                servers=servers,
                unmeasured_surfaces=shell.unmeasured_surfaces,
            )
        )
    return tuple(results)


def load_manifest(path: Path) -> Mapping[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, Mapping):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


def as_jsonable(profile: ProfileMeasurement) -> dict[str, Any]:
    return {
        "role": profile.role,
        "persona": profile.persona,
        "account": profile.account,
        "harness": profile.harness,
        "profile": profile.profile,
        "model": profile.model,
        "tier": profile.tier,
        "tool_count": profile.tool_count,
        "schema_chars": profile.schema_chars,
        "estimated_schema_tokens": profile.estimated_schema_tokens,
        "config_chars": profile.config_chars,
        "estimated_config_tokens": profile.estimated_config_tokens,
        "unmeasured_surfaces": list(profile.unmeasured_surfaces),
        "servers": [
            {
                "name": server.name,
                "source": server.source,
                "status": server.status,
                "tool_count": server.tool_count,
                "schema_chars": server.schema_chars,
                "estimated_schema_tokens": server.estimated_schema_tokens,
                "config_chars": server.config_chars,
                "estimated_config_tokens": server.estimated_config_tokens,
                "error": server.error,
            }
            for server in profile.servers
        ],
    }


def render_table(profiles: Sequence[ProfileMeasurement]) -> str:
    lines = [
        "role persona harness/profile tools schema_tokens tier server_status unmeasured",
    ]
    for profile in profiles:
        statuses = ",".join(f"{server.name}:{server.status}" for server in profile.servers) or "-"
        unmeasured = "; ".join(profile.unmeasured_surfaces) or "-"
        lines.append(
            f"{profile.role} {profile.persona} {profile.harness}/{profile.profile} "
            f"{profile.tool_count} {profile.estimated_schema_tokens} {profile.tier} "
            f"{statuses} {unmeasured}"
        )
    return "\n".join(lines)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("instance.yaml"))
    parser.add_argument("--probe", action="store_true", help="run stdio MCP tools/list for configured servers")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--persona", help="limit to one persona")
    parser.add_argument("--role", help="limit to one instance role")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a compact table")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    manifest = load_manifest(args.manifest)
    profiles = measure_profiles(
        manifest,
        probe=args.probe,
        cwd=args.manifest.resolve().parent,
        timeout=args.timeout,
        persona_filter=args.persona,
        role_filter=args.role,
    )
    if args.json:
        payload = {
            "schema": "pantheon-tool-schema-budget/v1",
            "manifest": str(args.manifest),
            "probe": bool(args.probe),
            "tiers": list(TIERS),
            "profiles": [as_jsonable(profile) for profile in profiles],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_table(profiles))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
