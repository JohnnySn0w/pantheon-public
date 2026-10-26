#!/usr/bin/env python3
"""seat_launch_config.py — the model/effort a seat's role DECLARES in instance.yaml (T-4085).

pantheon-seat-relaunch.sh respawns a not-cut-over seat (mason/artisan) by replaying
its CAPTURED live argv, so an instance.yaml model change (T-4067: opus-4-8 -> 5-5)
never reached those seats: a relaunch was a context refresh, not a config reload.
This resolves the same keys the claude harness adapter reads for a controller-
launched seat (harness_adapter/claude.py build_interactive_launch:
role.claude.model -> --model, role.claude.native.effort -> --effort), so the
relaunch can re-derive them from config instead of from the running process.

CLI (consumed by pantheon-seat-relaunch.sh):

    seat_launch_config.py --persona mason [--instance PATH]

prints exactly two lines, ``model=<m>`` and ``effort=<e>`` (value empty when the
role declares nothing). Exit 0 on a clean read; exit 2 when the config or role
cannot be resolved, or a declared value is not a plain CLI token — the caller
then keeps the captured argv (fail-safe: never respawn a guessed command).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TOOLS))

from agent_role_map import load_instance_document, resolve_instance_role  # noqa: E402

# A declared value is spliced into the respawn argv, so accept only a plain
# CLI token (model ids like claude-opus-5-5 / claude-opus-5-5[1m], efforts
# like xhigh). Anything else is a config bug, not something to launch.
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]*$")


class LaunchConfigError(Exception):
    pass


def role_launch_overrides(persona: str, instance_path: Path) -> dict[str, str | None]:
    """Return {"model": m|None, "effort": e|None} declared by *persona*'s role.

    None means the role declares nothing for that key (keep the captured value).
    Raises LaunchConfigError when the file, role, or a declared value is unusable.
    """
    try:
        import yaml
    except ImportError as exc:
        raise LaunchConfigError(f"pyyaml unavailable: {exc}") from exc
    try:
        data = load_instance_document(instance_path)  # T-4279: cached parse
    except (OSError, yaml.YAMLError) as exc:
        raise LaunchConfigError(f"cannot read {instance_path}: {exc}") from exc
    runtime = data.get("harness_runtime") if isinstance(data, dict) else None
    roles = runtime.get("roles") if isinstance(runtime, dict) else None
    if not isinstance(roles, dict):
        raise LaunchConfigError(f"{instance_path}: no harness_runtime.roles mapping")
    role_key = resolve_instance_role(persona)
    role = roles.get(role_key)
    if not isinstance(role, dict):
        raise LaunchConfigError(f"{instance_path}: no role '{role_key}' for persona '{persona}'")
    claude = role.get("claude")
    if claude is None:
        return {"model": None, "effort": None}
    if not isinstance(claude, dict):
        raise LaunchConfigError(f"role '{role_key}'.claude is not a mapping")
    native = claude.get("native")
    out = {
        "model": claude.get("model"),
        "effort": native.get("effort") if isinstance(native, dict) else None,
    }
    for key, value in out.items():
        if value is None:
            continue
        if not isinstance(value, str) or not _TOKEN.match(value):
            raise LaunchConfigError(f"role '{role_key}' {key}={value!r} is not a plain CLI token")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--persona", required=True)
    p.add_argument("--instance", type=Path, default=_TOOLS.parent / "instance.yaml")
    args = p.parse_args(argv)
    try:
        cfg = role_launch_overrides(args.persona, args.instance)
    except LaunchConfigError as exc:
        print(f"[seat_launch_config] {exc}", file=sys.stderr)
        return 2
    print(f"model={cfg['model'] or ''}")
    print(f"effort={cfg['effort'] or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
