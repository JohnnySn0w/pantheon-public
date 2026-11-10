"""`germinate {plan,apply,verify,rollback,status}` (ORG-080 §1), simulated by default; live is dark.

Every verb requires ``--simulated-host-root DIR``: there is no default and no
other backend to choose, so no invocation can reach the real host. The one exception
(T-6167 PR-4) is ``apply install --seat ACCOUNT --live``: an injected ``live_install``
seam (the command root supplies it; engine/ never imports host_substrate) that refuses
while the launcher pins are off, before any engine, instance.yaml or runner is touched,
and is mutually exclusive with ``--simulated-host-root``. Output is
JSON lines (transcript events, then a result line). Exit 0 on success, 1 on a
typed refusal, 2 on a usage error.

``germinate sim init`` is a simulation harness verb, not an ORG-080 lifecycle
verb: it creates the simulated host (sentinel) and simulates postboot so an
end-to-end CLI run needs no Python.

Plan inputs come only from declared sources: ``--set KEY=VALUE`` is checked
against the contract's ``desired_schema`` (ORG-080 §1), and ``--from-rev`` /
``--to-rev`` feed the update leg's ``source_rev_from`` / ``source_rev_to``.

``plan --preflight`` (T-5860) prints a read-only GO/NO-GO receipt as the result
line (exit 0 GO, 1 NO-GO); save the output and hand it to ``apply
--preflight-receipt FILE`` (opt-in) to bind the apply to that receipt.
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
    from tools.germinate.engine.context import InvocationContext
    from tools.germinate.engine.executor import Engine

    return Engine.from_checkout(
        Path(args.simulated_host_root),
        repo_root=_repo_root(args),
        registry_path=Path(args.registry) if args.registry else None,
        instance=args.instance,
        echo=sys.stdout,
        context=InvocationContext(kind=args.context, account=args.invoking_account),
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


def _accept(args: argparse.Namespace) -> frozenset[str] | None:
    if args.accept_gates is None:
        return None
    if args.accept_gates == "":
        return frozenset()
    return frozenset(g for g in args.accept_gates.split(",") if g)


_PREFLIGHT_ONLY = ("accept_gates", "grant_file", "plan_digest", "receipt_ttl", "observation_tier")


def _wire_observation_tier(engine, args) -> None:
    """T-5921: ``--observation-tier live`` swaps in the live READ-ONLY reader, built by the
    factory the command root injected (tools/germinate/preflight_live.py: engine/ never
    imports it). The reader is dark per seat; a dark or missing reader refuses."""

    if getattr(args, "observation_tier", None) != "live":
        return
    from tools.germinate.engine.executor import EngineRefusal

    factory = getattr(args, "live_port_factory", None)
    if factory is None:
        raise EngineRefusal("observation-tier-refused", "no live preflight reader is wired into this command")
    try:
        engine.preflight_port = factory(getattr(args, "seat", None))
    except Exception as exc:  # noqa: BLE001 -- a dark or unbuildable reader is a typed refusal
        raise EngineRefusal("observation-tier-refused", f"{type(exc).__name__}: {exc}") from None


def _plan(engine, args) -> int:
    scope, system = _scope(args)
    if args.preflight:
        _wire_observation_tier(engine, args)
        receipt = engine.preflight(
            args.leg,
            scope,
            system,
            plan_digest=args.plan_digest,
            accept_gates=_accept(args),
            grant_file=Path(args.grant_file) if args.grant_file else None,
            ttl_seconds=args.receipt_ttl if args.receipt_ttl is not None else _default_ttl(),
            **_inputs(args),
        )
        _emit({"receipt": receipt})
        return 0 if receipt["verdict"] == "GO" else 1
    merged = engine.plan(args.leg, scope, system, **_inputs(args))
    _emit({"plan_digest": merged.digest, "nodes": [n.component for n in merged.nodes]})
    return 0


def _default_ttl() -> int:
    from tools.germinate.engine.preflight import DEFAULT_TTL_SECONDS

    return DEFAULT_TTL_SECONDS


def _apply(engine, args) -> int:
    scope, system = _scope(args)
    accept = _accept(args)
    receipt = None
    if getattr(args, "preflight_receipt", None):
        from tools.germinate.engine.executor import EngineRefusal
        from tools.germinate.engine.preflight import PreflightError, load_receipt_file

        try:
            receipt = load_receipt_file(Path(args.preflight_receipt))
        except PreflightError as exc:
            raise EngineRefusal(exc.code, exc.message) from None
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
        args.leg,
        scope,
        system,
        plan_digest=args.plan_digest,
        accept_gates=accept,
        grants=grants,
        preflight_receipt=receipt,
        **_inputs(args),
    )
    _emit([o.__dict__ for o in outcomes])
    return 0


def _plan_entry(args: argparse.Namespace) -> int:
    if not args.preflight:
        given = [name for name in _PREFLIGHT_ONLY if getattr(args, name) is not None]
        if given:
            flags = ", ".join("--" + name.replace("_", "-") for name in given)
            print(f"usage error: {flags} only apply with --preflight", file=sys.stderr)
            return 2
    return _run(_plan, args)


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


#: ``--live`` supports exactly this: the seat install leg with the plan digest. Every other
#: option has no meaning on the live path (the sub-run carries none of them) and is a usage error.
_LIVE_UNSUPPORTED = (
    ("set", None),
    ("from_rev", None),
    ("to_rev", None),
    ("accept_gates", None),
    ("grant_file", None),
    ("preflight_receipt", None),
    ("repo_root", None),
    ("registry", None),
    ("instance", "sim"),
    ("context", "operator"),
    ("invoking_account", None),
)


def _apply_entry(args: argparse.Namespace) -> int:
    live = bool(getattr(args, "live", False))
    if not live:
        if args.simulated_host_root is None:
            print("usage error: --simulated-host-root is required unless --live is given", file=sys.stderr)
            return 2
        return _run(_apply, args)
    # --live: classify the combination first (no I/O), then hand over to the injected seam.
    if args.simulated_host_root is not None:
        print("usage error: --live and --simulated-host-root are mutually exclusive", file=sys.stderr)
        return 2
    if args.leg != "install" or not getattr(args, "seat", None):
        print("usage error: --live is only for `apply install --seat ACCOUNT`", file=sys.stderr)
        return 2
    given = [name for name, default in _LIVE_UNSUPPORTED if getattr(args, name, default) != default]
    if given:
        flags = ", ".join("--" + name.replace("_", "-") for name in given)
        print(f"usage error: {flags} cannot be combined with --live", file=sys.stderr)
        return 2
    seam = getattr(args, "live_install", None)
    if seam is None:
        print("refused: live-not-wired: no live install seam is wired into this command", file=sys.stderr)
        return 1
    return int(seam(args))


def register(subparsers: argparse._SubParsersAction, *, live_port_factory=None, live_install=None) -> None:
    """``live_port_factory(seat) -> PreflightPort`` (T-5921) and ``live_install(args) -> int``
    (T-6167 PR-4) are injected by the command root, so engine/ never imports the live reader
    or the root launcher."""

    def common(p: argparse.ArgumentParser, *, sim_root_required: bool = True) -> None:
        p.add_argument(
            "--simulated-host-root",
            required=sim_root_required,
            metavar="DIR",
            help="the simulated host (created if absent; must carry the SIMULATED-HOST sentinel otherwise)",
        )
        p.add_argument("--repo-root", help="checkout root holding registry/ and contract modules (default: this repo)")
        p.add_argument("--registry", help="registry file (default: <repo-root>/registry/systems.yaml)")
        p.add_argument("--instance", default="sim", help="instance id (default: sim)")
        p.add_argument(
            "--context",
            choices=("operator", "scheduled", "daemon"),
            default="operator",
            help="invocation context (T-5243, ORG-080 A-3): scheduled/daemon refuse ops they are not admitted to run (default: operator)",
        )
        p.add_argument("--invoking-account", metavar="ACCOUNT", help="recorded in the transcript only; not checked")

    def scope(p: argparse.ArgumentParser, *, required: bool) -> None:
        group = p.add_mutually_exclusive_group(required=required)
        group.add_argument("--all", action="store_true", help="every component with a pinned contract")
        group.add_argument("--system", metavar="ID", help="one registry component (dependencies are not planned)")
        group.add_argument("--seat", metavar="ACCOUNT", help="seat-scoped leg for one account")

    def inputs(p: argparse.ArgumentParser) -> None:
        p.add_argument("--set", action="append", metavar="KEY=VALUE", help="a desired-state value the contract's desired_schema declares")
        p.add_argument("--from-rev", help="update leg: the source revision being left")
        p.add_argument("--to-rev", help="update leg: the source revision being adopted")

    def accept_gates(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--accept-gates",
            nargs="?",
            const="",
            default=None,
            metavar="ID,...",
            help="answer operator-class gates (all, or the named ids); never independent-agent or reserved",
        )

    def grant_file(p: argparse.ArgumentParser, help_text: str) -> None:
        p.add_argument("--grant-file", metavar="PATH", help=help_text)

    p = subparsers.add_parser("plan", help="compute the merged plan and its digest (read-only)")
    p.add_argument("leg", choices=LEGS)
    scope(p, required=True)
    common(p)
    inputs(p)
    p.add_argument(
        "--preflight",
        action="store_true",
        help="emit a read-only GO/NO-GO receipt (never mutates, claims or consumes a grant); exit 1 on NO-GO",
    )
    p.add_argument("--plan-digest", help="--preflight: the approved digest the receipt must match")
    accept_gates(p)
    grant_file(p, "--preflight: the grant file apply will use (presence is checked; nothing is bound)")
    p.add_argument("--receipt-ttl", type=int, metavar="SECONDS", help="--preflight: receipt lifetime (default 3600, 60..86400)")
    p.add_argument(
        "--observation-tier",
        choices=("simulated", "live"),
        help="--preflight: where observations come from (default simulated); live is the READ-ONLY reader, "
        "dark per seat, and needs --seat",
    )
    p.set_defaults(func=lambda a: _plan_entry(a), live_port_factory=live_port_factory)

    p = subparsers.add_parser("apply", help="execute an approved plan digest")
    p.add_argument("leg", choices=LEGS)
    scope(p, required=True)
    common(p, sim_root_required=False)  # required unless --live; enforced in _apply_entry
    p.add_argument(
        "--live",
        action="store_true",
        help="apply install --seat only: run the seat install through the root launcher (DARK: refuses while the "
        "launcher pins are off); mutually exclusive with --simulated-host-root",
    )
    inputs(p)
    p.add_argument("--plan-digest", required=True, help="the digest `plan` printed; apply refuses any other plan")
    accept_gates(p)
    grant_file(
        p,
        "mode-0600 file of '<operation-id> <approval-handle>' lines: pre-issued independent-agent grants to consume "
        "(handles never go in argv)",
    )
    p.add_argument(
        "--preflight-receipt",
        metavar="FILE",
        help="opt-in: the saved `plan --preflight` output; apply refuses unless it is a GO, unexpired receipt for this "
        "exact plan and the checkout has not moved",
    )
    p.set_defaults(func=lambda a: _apply_entry(a), live_install=live_install)

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


if __name__ == "__main__":  # T-5989: the module form runs the germinate verbs, never a silent exit 0
    from tools.germinate.cli import main as _germinate_main

    sys.exit(_germinate_main(sys.argv[1:]))
