"""`germinate {plan,apply,verify,rollback,status}` (ORG-080 §1), simulated backend only.

Every verb requires ``--simulated-host-root DIR``: there is no default and no
other backend to choose, so no invocation can reach the real host. Output is
JSON lines (transcript events, then a result line). Exit 0 on success, 1 on a
typed refusal, 2 on a usage error.

``germinate sim init`` is a simulation harness verb, not an ORG-080 lifecycle
verb: it creates the simulated host (sentinel) and simulates postboot so an
end-to-end CLI run needs no Python.

Plan inputs come only from declared sources: ``--set KEY=VALUE`` is checked
against the contract's ``desired_schema`` (ORG-080 §1), and ``--from-rev`` /
``--to-rev`` feed the update leg's ``source_rev_from`` / ``source_rev_to``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LEGS = ("install", "configure", "update", "remove", "repair")


def _repo_root(args: argparse.Namespace) -> Path:
    from tools.germinate.registry.cli import REPO_ROOT

    return Path(args.repo_root).resolve() if args.repo_root else REPO_ROOT


def _scope(args: argparse.Namespace) -> tuple[str, str | None]:
    if getattr(args, "seat", None):
        return f"seat-{args.seat}", None
    return "system", getattr(args, "system", None)


def _engine(args: argparse.Namespace):
    from tools.germinate.engine.executor import Engine

    return Engine.from_checkout(
        Path(args.simulated_host_root),
        repo_root=_repo_root(args),
        registry_path=Path(args.registry) if args.registry else None,
        instance=args.instance,
        echo=sys.stdout,
    )


def _inputs(args: argparse.Namespace) -> dict:
    from tools.germinate.engine.executor import EngineRefusal

    desired = {}
    for item in getattr(args, "set", None) or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise EngineRefusal("inputs-refused", f"--set expects KEY=VALUE, got {item!r}")
        if key in desired:
            raise EngineRefusal("inputs-refused", f"--set {key} given twice")
        desired[key] = value
    revs = (getattr(args, "from_rev", None), getattr(args, "to_rev", None))
    if (revs[0] is None) != (revs[1] is None):
        raise EngineRefusal("inputs-refused", "--from-rev and --to-rev go together")
    return {"desired": desired or None, "source_revs": revs if revs[0] is not None else None}


def _emit(result: object) -> None:
    sys.stdout.write(json.dumps({"result": result}, sort_keys=True) + "\n")


def _run(fn, args: argparse.Namespace) -> int:
    from tools.germinate.engine.backend import SimRootRefused
    from tools.germinate.engine.executor import EngineRefusal
    from tools.germinate.engine.run_state import RunStateError

    try:
        return fn(_engine(args), args)
    except SimRootRefused as exc:
        print(f"refused: simulated-host-root: {exc}", file=sys.stderr)
        return 1
    except EngineRefusal as exc:
        print(f"refused: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except RunStateError as exc:
        print(f"refused: run-state-refused: {exc}", file=sys.stderr)
        return 1


def _sim_init(args: argparse.Namespace) -> int:
    from tools.germinate.engine.backend import SimRootRefused, SimulatedHostBackend

    try:
        backend = SimulatedHostBackend(Path(args.simulated_host_root))
        backend.simulate_postboot()
    except SimRootRefused as exc:
        print(f"refused: simulated-host-root: {exc}", file=sys.stderr)
        return 1
    _emit({"simulated_host_root": str(backend.root), "postboot": "simulated"})
    return 0


def _plan(engine, args) -> int:
    scope, system = _scope(args)
    merged = engine.plan(args.leg, scope, system, **_inputs(args))
    _emit({"plan_digest": merged.digest, "nodes": [n.component for n in merged.nodes]})
    return 0


def _apply(engine, args) -> int:
    scope, system = _scope(args)
    if args.accept_gates is None:
        accept = None
    elif args.accept_gates == "":
        accept = frozenset()
    else:
        accept = frozenset(g for g in args.accept_gates.split(",") if g)
    grants = None
    if getattr(args, "grant_file", None):
        import os

        from tools.germinate.engine.executor import EngineRefusal
        from tools.germinate.engine.grants import GrantRefused, read_grant_file

        try:
            grants = read_grant_file(Path(args.grant_file), os.getuid())
        except GrantRefused as exc:
            raise EngineRefusal(exc.code, exc.message) from None
    outcomes = engine.apply(
        args.leg, scope, system, plan_digest=args.plan_digest, accept_gates=accept, grants=grants, **_inputs(args)
    )
    _emit([o.__dict__ for o in outcomes])
    return 0


def _verify(engine, args) -> int:
    scope, system = _scope(args)
    results = engine.verify(scope, system)
    _emit(results)
    return 0 if all(r["ok"] for r in results) else 1


def _rollback(engine, args) -> int:
    _emit(engine.rollback(args.run).__dict__)
    return 0


def _status(engine, args) -> int:
    _emit(engine.status())
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--simulated-host-root",
            required=True,
            metavar="DIR",
            help="the simulated host (created if absent; must carry the SIMULATED-HOST sentinel otherwise)",
        )
        p.add_argument("--repo-root", help="checkout root holding registry/ and contract modules (default: this repo)")
        p.add_argument("--registry", help="registry file (default: <repo-root>/registry/systems.yaml)")
        p.add_argument("--instance", default="sim", help="instance id (default: sim)")

    def scope(p: argparse.ArgumentParser, *, required: bool) -> None:
        group = p.add_mutually_exclusive_group(required=required)
        group.add_argument("--all", action="store_true", help="every component with a pinned contract")
        group.add_argument("--system", metavar="ID", help="one registry component (dependencies are not planned)")
        group.add_argument("--seat", metavar="ACCOUNT", help="seat-scoped leg for one account")

    def inputs(p: argparse.ArgumentParser) -> None:
        p.add_argument("--set", action="append", metavar="KEY=VALUE", help="a desired-state value the contract's desired_schema declares")
        p.add_argument("--from-rev", help="update leg: the source revision being left")
        p.add_argument("--to-rev", help="update leg: the source revision being adopted")

    p = subparsers.add_parser("plan", help="compute the merged plan and its digest (read-only)")
    p.add_argument("leg", choices=LEGS)
    scope(p, required=True)
    common(p)
    inputs(p)
    p.set_defaults(func=lambda a: _run(_plan, a))

    p = subparsers.add_parser("apply", help="execute an approved plan digest")
    p.add_argument("leg", choices=LEGS)
    scope(p, required=True)
    common(p)
    inputs(p)
    p.add_argument("--plan-digest", required=True, help="the digest `plan` printed; apply refuses any other plan")
    p.add_argument(
        "--accept-gates",
        nargs="?",
        const="",
        default=None,
        metavar="ID,...",
        help="answer operator-class gates (all, or the named ids); never independent-agent or reserved",
    )
    p.add_argument(
        "--grant-file",
        metavar="PATH",
        help="mode-0600 file of '<operation-id> <approval-handle>' lines: pre-issued independent-agent grants to consume "
        "(handles never go in argv)",
    )
    p.set_defaults(func=lambda a: _run(_apply, a))

    p = subparsers.add_parser("verify", help="read-only drift check of receipted postconditions")
    scope(p, required=False)
    common(p)
    p.set_defaults(func=lambda a: _run(_verify, a))

    p = subparsers.add_parser("rollback", help="receipt-anchored undo of one non-terminal run")
    p.add_argument("--run", required=True, metavar="RUN_ID")
    common(p)
    p.set_defaults(func=lambda a: _run(_rollback, a))

    p = subparsers.add_parser("status", help="registry coverage joined with run state (display-only)")
    common(p)
    p.set_defaults(func=lambda a: _run(_status, a))

    sim = subparsers.add_parser("sim", help="simulation harness (not a lifecycle verb)")
    sim_sub = sim.add_subparsers(dest="sim_cmd", required=True)
    p = sim_sub.add_parser("init", help="create a simulated host and simulate postboot")
    p.add_argument("--simulated-host-root", required=True, metavar="DIR")
    p.set_defaults(func=_sim_init)
