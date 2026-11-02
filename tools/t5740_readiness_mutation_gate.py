#!/usr/bin/env python3
"""Exact selectable Amendment-J mutations for T-5740 readiness.

Run from the repository root on a clean tree:
  python3 tools/t5740_readiness_mutation_gate.py --list
  python3 tools/t5740_readiness_mutation_gate.py --run mode-gate
  python3 tools/t5740_readiness_mutation_gate.py --run-all

Each mutation must make its named regression fail. Source bytes are restored in a
finally block and verified before exit.
"""
from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Mutation:
    path: str
    old: str
    new: str
    test: str


MUTATIONS = {
    "mode-gate": Mutation(
        "tools/runtime_controller.py", "    if structural_enabled:\n",
        "    if False and structural_enabled:\n",
        "tools.test_codex_controller_appserver.ControllerAppServerLaunchTests."
        "test_structural_readiness_never_captures_or_mutates_remote_tui",
    ),
    "nonce-freshness": Mutation(
        "tools/harness_adapter/codex.py", '            and doc["launch_nonce"] == nonce\n',
        '            and bool(doc["launch_nonce"])\n',
        "tools.test_codex_controller_appserver.ControllerAppServerLaunchTests."
        "test_current_generation_exact_owner_only_receipt_is_required",
    ),
    "heartbeat-proof": Mutation(
        "tools/codex_seat_delivery.py",
        "        loaded = loaded_threads_from_heartbeat(res)\n",
        "        if self.readiness_publisher is not None and not self._readiness_published:\n"
        "            self.readiness_publisher(self.client.thread_id)\n"
        "            self._readiness_published = True\n"
        "        loaded = loaded_threads_from_heartbeat(res)\n",
        "tools.test_codex_seat_delivery.HeartbeatTests."
        "test_readiness_publication_requires_exact_loaded_thread_heartbeat",
    ),
    "stale-clearing": Mutation(
        "tools/harness_adapter/codex.py", "            receipt_path.unlink(missing_ok=True)\n",
        "            pass  # MUTATION: retain stale receipt\n",
        "tools.test_codex_controller_appserver.ControllerAppServerLaunchTests."
        "test_new_launch_nonce_rejects_prior_generation_receipt",
    ),
}


def run_one(name: str) -> None:
    mutation = MUTATIONS[name]
    path = ROOT / mutation.path
    original = path.read_bytes()
    text = original.decode()
    if text.count(mutation.old) != 1:
        raise SystemExit(f"{name}: source anchor count is {text.count(mutation.old)}, expected 1")
    try:
        path.write_text(text.replace(mutation.old, mutation.new), encoding="utf-8")
        result = subprocess.run(
            ["python3", "-m", "unittest", mutation.test], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if result.returncode == 0:
            raise SystemExit(f"{name}: SURVIVED\n{result.stdout}")
        print(f"{name}: KILLED (test rc={result.returncode})")
    finally:
        path.write_bytes(original)
        if path.read_bytes() != original:
            raise SystemExit(f"{name}: source restoration failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--run", choices=sorted(MUTATIONS))
    group.add_argument("--run-all", action="store_true")
    args = parser.parse_args()
    if args.list:
        for name, mutation in MUTATIONS.items():
            print(f"{name}: {mutation.path} -> {mutation.test}")
        return 0
    for name in MUTATIONS if args.run_all else (args.run,):
        run_one(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
