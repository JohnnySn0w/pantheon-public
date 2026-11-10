"""`germinate` command root (ORG-080 §1).

Each area registers its own sub-command group here. The engine verbs plan,
apply, verify, rollback and status (T-5074, simulated backend only) register
from tools/germinate/engine/cli.py, with `plan --preflight` (T-5860) for the
read-only GO/NO-GO receipt; resume comes later.
"""

from __future__ import annotations

import argparse
import sys

from tools.germinate.engine import cli as engine_cli
from tools.germinate.registry import cli as registry_cli


def _live_port(seat):
    """T-5921: the live READ-ONLY preflight reader, imported only when asked for (outside engine/)."""

    from tools.germinate.preflight_live import live_port_factory

    return live_port_factory(seat)


def _live_install(args):
    """T-6167 PR-4: the root-launcher install, imported only when ``--live`` is given (outside engine/).

    Loaded by name on purpose: live_run / root_launcher are ROOT-side code and must stay out of the
    rev-pinned framework copy's static import closure (the seat child reaches this module through
    engine/cli.py's ``__main__`` block); a test pins that closure's exact added set."""

    import importlib

    return importlib.import_module("tools.germinate.live_run").cli_install(args.seat, args.plan_digest)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="germinate", description="Pantheon germination engine")
    sub = parser.add_subparsers(dest="command", required=True)
    registry_cli.register(sub)
    engine_cli.register(sub, live_port_factory=_live_port, live_install=_live_install)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
