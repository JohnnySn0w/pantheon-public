#!/usr/bin/env python3
"""T-5451: read-only report of the codex-flip policy per seat (T-5446).

Intent: before a policy-flip merge reaches live seats, show exactly what each seat
with a manifest fallback will do. Pure reads of instance.yaml and the bridge install
files; never runs a probe, a controller, systemctl, tmux, claude or codex.

Weekly usage is NOT read here: the only live source is the claude quota probe, which
spawns a session and writes state. Pass the numbers in instead:

    python3 tools/flip_policy_report.py --usage specialist=96.5,orchestrator=94 [--manifest PATH]

A seat's `planned_transition_on_next_tick` is true iff quota_check is supported and
remaining% (100 - usage) is <= the policy threshold (weekly_remaining_percent).
Comms readiness is read as the invoking unix user; another account's home may be
unreadable ("unreadable: ..."), in which case run it as that seat.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import persona_bringup  # noqa: E402


def _comms_readiness(manifest_path: Path, persona: str) -> str:
    from tools import codex_telegram_bridge, persona_installer

    try:
        manifest = persona_installer.Manifest.load(manifest_path, strict_flip_keys=False)
        bridge = codex_telegram_bridge.binding_from_manifest(manifest, persona)
    except codex_telegram_bridge.BridgeError:
        return "n/a (no codex telegram bridge entry)"
    try:
        checks = codex_telegram_bridge.receiver_readiness(
            bridge, managed_marker=persona_installer.MANAGED_MARKER)
    except OSError as exc:
        return f"unreadable: {exc}"
    failed = [f"{c.name}: {c.detail}" for c in checks if not c.ok]
    return "ready" if not failed else "NOT READY: " + "; ".join(failed)


def build_report(
    manifest_path: Path,
    *,
    usage: Mapping[str, float] | None = None,
    personas: list[str] | None = None,
) -> list[dict[str, Any]]:
    usage = usage or {}
    if personas is None:
        from tools import persona_installer

        roles = persona_installer.Manifest.load(manifest_path, strict_flip_keys=False).roles
        personas = sorted({r["persona"] for r in roles.values() if isinstance(r, dict) and r.get("persona")})
    rows: list[dict[str, Any]] = []
    for persona in personas:
        binding = persona_bringup.load_binding(manifest_path, persona)
        if not binding.controller.policy.fallbacks:
            rows.append({"persona": persona, "fallback": None, "note": "no fallback: flip is a structural no-op"})
            continue
        supported, reason = persona_bringup.quota_check_supported(binding, manifest_path=manifest_path)
        threshold = binding.controller.policy.weekly_remaining_percent
        used = usage.get(persona)
        rows.append({
            "persona": persona,
            "fallback": list(binding.controller.policy.fallbacks),
            "quota_check_supported": supported,
            "quota_check_reason": reason,
            "mid_session_cap_swap": binding.controller.mid_session_cap_swap_enabled,
            "fallback_return": binding.controller.fallback_return_enabled,
            "comms_receiver": _comms_readiness(manifest_path, persona),
            "weekly_used_percent": used,
            "weekly_remaining_threshold_percent": threshold,
            "planned_transition_on_next_tick": (
                None if used is None else bool(supported and (100.0 - used) <= threshold)),
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", type=Path, default=persona_bringup.DEFAULT_MANIFEST)
    ap.add_argument("--usage", default="", help="persona=used_percent[,persona=used_percent...]")
    args = ap.parse_args(argv)
    usage = {k: float(v) for k, v in (item.split("=", 1) for item in args.usage.split(",") if item)}
    print(json.dumps(build_report(args.manifest, usage=usage), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
