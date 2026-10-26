#!/usr/bin/env python3
"""Provision + verify the context-fill-monitor per-agent enablement drop-ins (T-1214).

The per-agent monitor legs — api-health / staleness / model-outage detection — are
armed via LOCAL systemd drop-ins under Recovery's
  ~/.config/systemd/user/pantheon-context-fill-monitor@<agent>.service.d/<name>.conf
Each is a one-line `Environment=PANTHEON_<X>_ENABLED=1`. None were repo-tracked, so a
Recovery re-provision would silently LOSE every enable (detection goes dark with NO
signal — the exact silent-disarm hazard T-1214 names).

This tool makes the enablement REPRODUCIBLE. The canonical drop-in set is the
`DROPINS` spec below, each leg over its own agent subset (default `AGENTS`; this
module IS the provisioning manifest):
  --provision : recreate the full drop-in set idempotently + `systemctl --user
                daemon-reload`. A re-provision can re-run this to re-arm every leg.
  --verify    : confirm every expected leg is armed; exit non-zero on ANY missing.
                Content-tolerant — it checks for the `Environment=...=1` token, so
                per-agent history comments in the live files don't trip it. Wired
                into verify_startup.sh as a soft guard so re-provision drift WARNs
                instead of going dark.

Mirrors feedback_reprovision_all_agents_after_unit_fix: installed units are copies;
a tracked source-of-truth + a verify gate is what keeps the copies from drifting
silently dark. Default-safe (T-0261): refuses to run with no mode flag.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Iterator, NamedTuple

UNIT = "pantheon-context-fill-monitor"

# The agents whose monitor legs Recovery arms (Recovery monitors the fleet from its own
# ~/.config). Keep in sync with the per-agent @<agent> instances actually run.
AGENTS = ("orchestrator", "specialist", "recovery")


class Leg(NamedTuple):
    """One enablement leg: its env var, canonical one-line note, and the agents it
    is armed for. The default is the full `AGENTS` set; a leg mid-rollout (T-3314
    repo-audit, HO-first) names a narrower subset."""

    envvar: str
    note: str
    agents: tuple[str, ...] = AGENTS


# Each enablement leg: drop-in filename -> Leg. Content is uniform across the
# agents a leg covers (same env var per detector); the rollout history lives in
# the ticket/ledger, not in per-agent comment divergence.
DROPINS: dict[str, Leg] = {
    "enable.conf": Leg(
        "PANTHEON_API_HEALTH_ENABLED",
        "alive-but-erroring api-health watchdog (sustained api-error -> restart); "
        "fleet-wide after the T-1126 gap-2 peer-coordinated first-fire (2026-06-16).",
    ),
    "staleness.conf": Leg(
        "PANTHEON_STALENESS_ENABLED",
        "wedge/staleness detection (a confirmed-wedged pane -> restart); per-agent "
        "cutover T-0857 (Recovery first-fire 2026-06-06, then Specialist/Orchestrator).",
    ),
    "model-outage.conf": Leg(
        "PANTHEON_MODEL_OUTAGE_ENABLED",
        "model-outage detector (sustained 404 model_not_found -> ALERT to Recovery, never "
        "a force-restart); fleet-wide after the T-1126 Recovery-first cutover (2026-06-16).",
    ),
    "repo-audit.conf": Leg(
        "PANTHEON_REPO_AUDIT_ENABLED",
        "T-0761 component 5 present-but-undeclared repo audit (alert-once, never a "
        "restart); HO-first single-seat validation per T-3314, widen to specialist/recovery "
        "after a week clean.",
        agents=("orchestrator",),
    ),
}


def expected_legs() -> Iterator[tuple[str, str, Leg]]:
    """Yield every expected (agent, drop-in name, leg), honouring each leg's subset."""
    for agent in AGENTS:
        for name, leg in DROPINS.items():
            if agent in leg.agents:
                yield agent, name, leg

DEFAULT_CONFIG_ROOT = Path.home() / ".config" / "systemd" / "user"


def dropin_body(envvar: str, note: str) -> str:
    """Canonical content for one enablement drop-in."""
    return (
        "[Service]\n"
        f"# {note}\n"
        "# Managed by tools/provision_context_fill_monitor_dropins.py (T-1214):\n"
        "# re-create with --provision, check with --verify. Remove this file +\n"
        "# `systemctl --user daemon-reload` to disarm.\n"
        f"Environment={envvar}=1\n"
    )


def dropin_path(config_root: Path, agent: str, name: str) -> Path:
    return config_root / f"{UNIT}@{agent}.service.d" / name


def expected_token(envvar: str) -> str:
    return f"Environment={envvar}=1"


def provision(config_root: Path = DEFAULT_CONFIG_ROOT, *, daemon_reload: bool = True) -> list[str]:
    """(Re)create every drop-in idempotently. Returns the paths actually written."""
    written: list[str] = []
    for agent, name, leg in expected_legs():
        path = dropin_path(config_root, agent, name)
        body = dropin_body(leg.envvar, leg.note)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text() != body:
            path.write_text(body)
            written.append(str(path))
    if daemon_reload and written:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    return written


def verify(config_root: Path = DEFAULT_CONFIG_ROOT) -> list[str]:
    """Return the MISSING enablement legs (empty list = all armed).

    Content-tolerant: a leg counts as armed iff its drop-in file exists AND
    contains the `Environment=<X>=1` token, regardless of surrounding comments.
    """
    missing: list[str] = []
    for agent, name, leg in expected_legs():
        path = dropin_path(config_root, agent, name)
        if not path.exists() or expected_token(leg.envvar) not in path.read_text():
            missing.append(f"{agent}/{name} ({leg.envvar})")
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--provision", action="store_true",
        help="(re)create the drop-in set idempotently + daemon-reload, then verify",
    )
    mode.add_argument(
        "--verify", action="store_true",
        help="check every expected leg is armed; exit 1 on any missing (no writes)",
    )
    parser.add_argument(
        "--config-root", type=Path, default=DEFAULT_CONFIG_ROOT,
        help="systemd user config root (override for tests)",
    )
    parser.add_argument(
        "--no-reload", action="store_true",
        help="skip `systemctl --user daemon-reload` (with --provision)",
    )
    args = parser.parse_args(argv)

    total = sum(1 for _ in expected_legs())

    if not (args.provision or args.verify):
        print(
            "ERROR: refusing to run with no mode flag. Pass --provision (recreate the "
            "drop-ins) or --verify (check they are armed).",
            file=sys.stderr,
        )
        return 64

    if args.provision:
        written = provision(args.config_root, daemon_reload=not args.no_reload)
        if written:
            for w in written:
                print(f"[provision] wrote {w}")
            tail = "" if args.no_reload else " + daemon-reload"
            print(f"[provision] {len(written)} drop-in(s) (re)created{tail}")
        else:
            print("[provision] all drop-ins already current (no-op)")
        missing = verify(args.config_root)
        if missing:
            print(f"[provision] WARNING: still missing after provision: {missing}", file=sys.stderr)
            return 1
        print(f"[provision] verified {total} enablement leg(s) armed ({', '.join(AGENTS)})")
        return 0

    # --verify
    missing = verify(args.config_root)
    if missing:
        print(
            f"[verify] DISARMED context-fill-monitor leg(s) {len(missing)}/{total}: {missing}. "
            f"Re-arm with: python3 tools/provision_context_fill_monitor_dropins.py --provision",
            file=sys.stderr,
        )
        return 1
    print(f"[verify] all {total} context-fill-monitor enablement leg(s) armed ({', '.join(AGENTS)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
