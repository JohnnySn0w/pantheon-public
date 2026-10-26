"""`germinate` command root (ORG-080 §1).

Each area registers its own sub-command group here. The engine verbs plan,
apply, verify, rollback and status (T-5074, simulated backend only) register
from tools/germinate/engine/cli.py; resume and preflight come later.
"""

from __future__ import annotations

import argparse
import sys

from tools.germinate.engine import cli as engine_cli
from tools.germinate.registry import cli as registry_cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="germinate", description="Pantheon germination engine")
    sub = parser.add_subparsers(dest="command", required=True)
    registry_cli.register(sub)
    engine_cli.register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
