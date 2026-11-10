"""T-4533 / T-4643 step 4: launch env that names a controller-launched claude seat's fleet-usage seat.

Since T-4643 the fleet-usage mod loads as the MANAGED org mod (/etc/claude-code/managed-settings.json
+ the root-owned /opt/pantheon/claude-plugins marketplace), never through ``CLAUDE_CODE_PLUGIN_DIRS``
(managed settings lock out a same-name sideload). The mod writes nothing without an explicit
``PANTHEON_FLEET_USAGE_SEAT`` (T-4643 step 0), so this helper injects that one key, naming the
persona (not the unix user, e.g. suborch1/suborch2 for mason/artisan).

Hand-wired seats (orchestrator, specialist, recovery) set the key in ``~/.claude/settings.json`` ``env``; a key the
seat sets there is never injected, so the seat's own value stays authoritative. Every failure
degrades to "inject nothing": the launch must never be blocked by this helper.

Used by ``runtime_controller._start_runtime`` and, via the CLI below, by
``pantheon-seat-relaunch.sh`` (one source for both launch paths).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SEAT_KEY = "PANTHEON_FLEET_USAGE_SEAT"


def _settings_env(account_home: Path) -> dict:
    try:
        data = json.loads((account_home / ".claude" / "settings.json").read_text())
        env = data.get("env") if isinstance(data, dict) else None
        return env if isinstance(env, dict) else {}
    except (OSError, ValueError):
        return {}


def fleet_usage_launch_env(
    persona: str, account_home: Path, *, clone_root: Path | None = None
) -> dict[str, str]:
    """Env to add to a claude seat's launch: the seat key alone (``{}`` when there is no persona
    or the seat's settings already set it). ``clone_root`` is accepted for caller compatibility
    and unused since T-4643 step 4 (the mod no longer loads from the seat's clone)."""
    del clone_root
    if not persona:
        return {}
    wanted = {SEAT_KEY: persona}
    already = _settings_env(Path(account_home))
    return {key: value for key, value in wanted.items() if key not in already}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--persona", required=True)
    parser.add_argument("--home", required=True, help="the seat account's home dir")
    parser.add_argument("--clone-root", default=None, help="override <home>/pantheon-config")
    args = parser.parse_args(argv)
    try:
        clone = Path(args.clone_root) if args.clone_root else None
        for key, value in fleet_usage_launch_env(args.persona, Path(args.home), clone_root=clone).items():
            print(f"{key}={value}")
    except Exception as exc:  # fail open: the respawn proceeds without the keys
        print(f"fleet_usage_env: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
