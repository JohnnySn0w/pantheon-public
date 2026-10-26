#!/usr/bin/env python3
"""codex_worker_guard_rollout.py: verify or apply the codex worker-guard block upgrade on ONE home (T-4395).

The T-4395 / T-4397 part A block edit (python3 -I, timeout 30) changes the guard's codex
currentHash, so every codex home needs its block rewritten AND re-trusted. The adapter does both
in one atomic write (tools.harness_adapter.codex.ensure_worker_guard_hook), automatically on the
next codex launch once the seat's clone is pulled. This is the ops surface for sequencing and
verifying that per home. Run it AS the seat account:

    python3 tools/codex_worker_guard_rollout.py status   # read-only verdict
    python3 tools/codex_worker_guard_rollout.py apply    # upgrade/trust now, then the verdict

Prints one JSON object. Exit 0: ON DISK the block is current, enabled and trusted under a
matching hash (ok). Exit 1: not ok. Exit 3: could not measure (the app-server read failed or
timed out), or config.toml is unparseable. status.live_sessions is the LIVE view and never
changes the exit code: each running codex session of this home, and whether it started before
config.toml's last write (so it still runs its startup snapshot of hooks + trust until restarted).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.harness_adapter.codex import (  # noqa: E402
    ensure_worker_guard_hook,
    worker_guard_hook_status,
)
from tools.harness_adapter.provider_quota import read_codex_app_server_hooks  # noqa: E402

EXIT_OK, EXIT_NOT_OK, EXIT_UNMEASURABLE = 0, 1, 3


def main(argv: list[str] | None = None, *, read_hooks=read_codex_app_server_hooks) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("action", choices=("status", "apply"))
    p.add_argument("--account-home", type=Path, default=Path.home())
    p.add_argument("--codex-home", type=Path, default=None, help="default: <account-home>/.codex")
    p.add_argument("--codex-bin", type=Path, default=Path("/usr/bin/codex"))
    args = p.parse_args(argv)
    codex_home = args.codex_home or args.account_home / ".codex"
    report: dict = {"action": args.action}
    try:
        if args.action == "apply":
            # trust_marker=False: the ops surface always asks codex, never the T-4428 launch cache.
            report["changed"] = ensure_worker_guard_hook(
                codex_home, args.account_home, executable=args.codex_bin, read_hooks=read_hooks,
                trust_marker=False)
        report["status"] = worker_guard_hook_status(
            codex_home, args.account_home, executable=args.codex_bin, read_hooks=read_hooks)
    except Exception as exc:  # noqa: BLE001 - unparseable config etc.: not a verdict
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(report, indent=1))
        return EXIT_UNMEASURABLE
    print(json.dumps(report, indent=1))
    status = report["status"]
    if not status["measurable"]:
        return EXIT_UNMEASURABLE
    return EXIT_OK if status["ok"] else EXIT_NOT_OK


if __name__ == "__main__":
    raise SystemExit(main())
