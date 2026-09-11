#!/usr/bin/env python3
"""tools/harness.py — manual-override CLI for the T-2953 harness controller (T-2967 BT4).

Command surface + ledger schema per notes/2026-08-18-t2953-phase1-spec.md
§Manual-override CLI (the ratify addition, TG 7935, item #3):

    harness <persona> status
    harness <persona> restart [--runtime <name>] [--clear-cooldown] --reason "<text>"
    harness <persona> clear-cooldown <runtime>|--all --reason "<text>"
    harness <persona> hold <runtime> --reason "<text>"
    harness <persona> clear-hold <runtime> --reason "<text>"

Global options: --dry-run, --operator <id> (usable before or after the
subcommand). --reason is required on every mutating verb (all but `status`);
enforced by a post-parse validator (MissingReasonError), not argparse
`required=True`, so `tools.harness_telegram` can reuse the identical parser
and turn a missing reason into a follow-up prompt instead of a hard refusal.

`--runtime` on `restart` kills live tmux for the persona and (with
`--clear-cooldown`) clears that runtime's cooldown so it becomes selectable —
it does NOT force the controller's manifest-order selection; a
manifest-earlier viable runtime still wins the next cycle. See T-2967 plan
"Open question — restart --runtime" (disposition: Orchestrator/HO, 2026-08-18,
Option 1 — scope conservatively, no new durable-state schema).

Every mutating verb lands exactly one ledger entry (5-field schema: id, ts,
agent, action, summary, links — the ticket's literal "exact schema from
spec", not tools/_audit.py's aspirational 10-field ORG-001 shape) via
tools.ledger_shard.append_entry. `--dry-run` performs zero writes — no state
file, no ledger entry — and prints what would have happened instead.

Coordination note: independent of tools/runtime_controller.py (BT1, T-2966,
in flight on a parallel branch) — this CLI reads/writes the same durable
JSON files by path + schema (tools/harness_state.py), never by importing
runtime_controller internals.
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import harness_state as hs  # noqa: E402
from tools.ledger_shard import append_entry, existing_entry_ids  # noqa: E402

LEDGER_DIR = _REPO_ROOT / "ledger"

MUTATING_COMMANDS = frozenset({"restart", "clear-cooldown", "hold", "clear-hold"})


class HarnessCliError(RuntimeError):
    """Raised for CLI-level failures (arg parsing/validation)."""


class MissingReasonError(HarnessCliError):
    def __init__(self, command: str):
        super().__init__(f"--reason is required for '{command}'")
        self.command = command


class HarnessArgumentParser(argparse.ArgumentParser):
    """Raises HarnessCliError instead of printing+sys.exit on parse failure.

    Lets both `main()` (exit 2 with a message) and `tools.harness_telegram`
    (turn the failure into an operator-facing refusal/prompt) share one
    error path instead of argparse's default process-exit behavior.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        raise HarnessCliError(message)


@dataclasses.dataclass(frozen=True)
class OverrideResult:
    action_slug: str
    summary: str
    runtime: str | None


def _global_options_parser(*, suppress_defaults: bool = False) -> argparse.ArgumentParser:
    """Build --dry-run/--operator, usable before OR after the subcommand.

    argparse's subparsers action parses the chosen subparser with a FRESH
    namespace (not the parent's) and then copies every one of its attributes
    back onto the parent namespace — so if the child parser's own copy of
    these options has an ordinary default, an unset flag on the child side
    silently clobbers a value already set on the parent side (e.g.
    `harness --dry-run orchestrator status` would reset dry_run back to False).
    `default=argparse.SUPPRESS` on the child copy means "flag not given" adds
    nothing to the child's namespace, so there is nothing to clobber with.
    """
    dry_run_default = argparse.SUPPRESS if suppress_defaults else False
    operator_default = argparse.SUPPRESS if suppress_defaults else None
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--dry-run", action="store_true", default=dry_run_default, help="Show what would change without doing it."
    )
    parser.add_argument(
        "--operator",
        default=operator_default,
        help="Override the recorded operator (default: current unix user).",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    top_global_opts = _global_options_parser(suppress_defaults=False)
    sub_global_opts = _global_options_parser(suppress_defaults=True)
    parser = HarnessArgumentParser(
        prog="harness",
        description="Manual-override CLI for the T-2953 stateless harness controller.",
        parents=[top_global_opts],
    )
    parser.add_argument("persona", help="Persona name (e.g. orchestrator, specialist).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "status",
        parents=[sub_global_opts],
        help="Show active runtime, all runtime last-attempts, holds, next-expiry. No mutation.",
    )

    restart_p = subparsers.add_parser(
        "restart",
        parents=[sub_global_opts],
        help="Kill live tmux for persona; controller respawns with a fresh selection.",
    )
    restart_p.add_argument(
        "--runtime",
        default=None,
        help=(
            "Kill live tmux + (with --clear-cooldown) clear that runtime's cooldown; the next "
            "controller cycle re-selects in manifest order over the resulting durable state — "
            "this does NOT force selection of the named runtime if a manifest-earlier runtime "
            "is also viable."
        ),
    )
    restart_p.add_argument(
        "--clear-cooldown", action="store_true", help="Also clear --runtime's cooldown (requires --runtime)."
    )
    restart_p.add_argument("--reason", default=None, help="Required: reason for this override.")

    cc_p = subparsers.add_parser(
        "clear-cooldown", parents=[sub_global_opts], help="Wipe cooldown file for one or all runtimes."
    )
    cc_p.add_argument("runtime", nargs="?", default=None, help="Runtime to clear (<harness>/<profile>).")
    cc_p.add_argument("--all", action="store_true", help="Clear cooldown for every runtime with a record.")
    cc_p.add_argument("--reason", default=None, help="Required: reason for this override.")

    hold_p = subparsers.add_parser(
        "hold", parents=[sub_global_opts], help="Mark <runtime> unselectable regardless of cooldown."
    )
    hold_p.add_argument("runtime", help="Runtime to hold (<harness>/<profile>).")
    hold_p.add_argument("--reason", default=None, help="Required: reason for this override.")

    clear_hold_p = subparsers.add_parser("clear-hold", parents=[sub_global_opts], help="Remove a hold.")
    clear_hold_p.add_argument("runtime", help="Runtime to un-hold (<harness>/<profile>).")
    clear_hold_p.add_argument("--reason", default=None, help="Required: reason for this override.")

    return parser


def validate_args(args: argparse.Namespace) -> None:
    """Post-parse validation shared by the CLI and the telegram parser."""
    if args.command in MUTATING_COMMANDS and not args.reason:
        raise MissingReasonError(args.command)

    if args.command == "clear-cooldown":
        if bool(args.runtime) == bool(args.all):
            raise HarnessCliError("clear-cooldown requires exactly one of <runtime> or --all")

    if args.command == "restart" and args.clear_cooldown and not args.runtime:
        raise HarnessCliError("--clear-cooldown requires --runtime")

    hs.validate_persona(args.persona)
    runtime = getattr(args, "runtime", None)
    if runtime:
        hs.validate_runtime(runtime)


def resolve_operator(args: argparse.Namespace) -> str:
    if args.operator:
        return args.operator
    return getpass.getuser()


def _utc_now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tmux_session_for_persona(persona: str, *, root: Path) -> str | None:
    doc = hs.load_active_runtime(persona, root=root)
    if not doc:
        return None
    session = doc.get("tmux_session")
    return session if isinstance(session, str) and session else None


def _kill_tmux_session(session: str, *, dry_run: bool) -> bool:
    """Return True if `session` was found live. Only actually kills it if not dry_run.

    The has-session check itself is read-only and always runs (even in
    dry-run) so the CLI can honestly report what it found.
    """
    check = subprocess.run(["tmux", "has-session", "-t", session], capture_output=True)
    live = check.returncode == 0
    if live and not dry_run:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    return live


def _do_status(args: argparse.Namespace, *, root: Path) -> int:
    persona = args.persona
    active = hs.load_active_runtime(persona, root=root)
    holds_doc = hs.load_holds(persona, root=root)
    held = holds_doc.get("holds", {})
    records = hs.list_runtime_records(persona, root=root)
    now = _utc_now_ts()

    print(f"persona: {persona}")
    if active:
        print(
            f"active runtime (cache): {active.get('runtime')} "
            f"(tmux={active.get('tmux_session')!r}, pid={active.get('pid')})"
        )
        print("  note: this is a cache, not authority — tmux+pid liveness is authoritative")
    else:
        print("active runtime (cache): none recorded")

    if held:
        print("holds:")
        for runtime, info in sorted(held.items()):
            print(f"  {runtime}: {info.get('reason')!r} (set by {info.get('set_by')} at {info.get('set_ts')})")
    else:
        print("holds: none")

    if records:
        print("runtime last-attempts:")
        active_cooldowns = []
        for runtime, doc in sorted(records.items()):
            cooldown = doc.get("cooldown_until")
            if runtime in held:
                viability = "held"
            elif cooldown and cooldown > now:
                viability = "cooldown-active"
                active_cooldowns.append(cooldown)
            else:
                viability = "viable"
            print(
                f"  {runtime}: outcome={doc.get('outcome')} failure_class={doc.get('failure_class')} "
                f"cooldown_until={cooldown} consecutive_failures={doc.get('consecutive_failures')} "
                f"[{viability}]"
            )
        next_expiry = min(active_cooldowns) if active_cooldowns else None
        print(f"next cooldown expiry: {next_expiry or 'none'}")
    else:
        print("runtime last-attempts: none recorded")
    return 0


def _do_restart(args: argparse.Namespace, *, operator: str, root: Path, dry_run: bool) -> OverrideResult:
    persona = args.persona
    runtime = args.runtime
    now_ts = _utc_now_ts()

    session = _tmux_session_for_persona(persona, root=root)
    if session is None:
        kill_summary = (
            "no active-runtime cache found; nothing to kill "
            "(persona may not be running under the harness controller yet)"
        )
    else:
        was_live = _kill_tmux_session(session, dry_run=dry_run)
        kill_summary = (
            f"killed live tmux session {session!r}" if was_live else f"tmux session {session!r} was not live; nothing to kill"
        )

    cooldown_summary = ""
    if runtime and args.clear_cooldown:
        doc, had_cooldown = hs.clear_cooldown(persona, runtime, operator=operator, now_ts=now_ts, root=root)
        if not dry_run:
            hs.write_last_attempt(persona, runtime, doc, root=root)
        cooldown_summary = (
            f"; cleared cooldown for {runtime}"
            if had_cooldown
            else f"; {runtime} had no active cooldown to clear"
        )
    elif runtime:
        cooldown_summary = (
            f"; {runtime} requested as next candidate, cooldown left untouched "
            "(pass --clear-cooldown to also clear it)"
        )

    force_caveat = ""
    if runtime:
        force_caveat = (
            f" — does not force selection of {runtime} if a manifest-earlier runtime is also viable"
        )

    summary = f"restart on {persona}: {kill_summary}{cooldown_summary}{force_caveat} — reason: {args.reason}"
    return OverrideResult(action_slug="restart", summary=summary, runtime=runtime)


def _do_clear_cooldown(args: argparse.Namespace, *, operator: str, root: Path, dry_run: bool) -> OverrideResult:
    persona = args.persona
    now_ts = _utc_now_ts()

    if args.all:
        targets = sorted(hs.list_runtime_records(persona, root=root))
    else:
        targets = [args.runtime]

    cleared: list[str] = []
    untouched: list[str] = []
    for runtime in targets:
        doc, had_cooldown = hs.clear_cooldown(persona, runtime, operator=operator, now_ts=now_ts, root=root)
        if not dry_run:
            hs.write_last_attempt(persona, runtime, doc, root=root)
        (cleared if had_cooldown else untouched).append(runtime)

    if not targets:
        detail = "no runtimes with a record found; nothing to clear"
    else:
        parts = []
        if cleared:
            parts.append(f"cleared cooldown for: {', '.join(cleared)}")
        if untouched:
            parts.append(f"already clear: {', '.join(untouched)}")
        detail = "; ".join(parts)

    target_label = "all" if args.all else args.runtime
    summary = f"clear-cooldown on {persona} ({target_label}): {detail} — reason: {args.reason}"
    runtime_link = None if args.all else args.runtime
    return OverrideResult(action_slug="clear-cooldown", summary=summary, runtime=runtime_link)


def _do_hold(args: argparse.Namespace, *, operator: str, root: Path, dry_run: bool) -> OverrideResult:
    persona = args.persona
    runtime = args.runtime
    now_ts = _utc_now_ts()
    doc = hs.set_hold(persona, runtime, reason=args.reason, operator=operator, root=root, now_ts=now_ts)
    if not dry_run:
        hs.write_holds(persona, doc, root=root)
    summary = f"hold on {persona}: {runtime} marked unselectable — reason: {args.reason}"
    return OverrideResult(action_slug="hold", summary=summary, runtime=runtime)


def _do_clear_hold(args: argparse.Namespace, *, operator: str, root: Path, dry_run: bool) -> OverrideResult:
    del operator  # not recorded for clear-hold; the ledger entry itself carries the acting operator
    persona = args.persona
    runtime = args.runtime
    doc, existed = hs.clear_hold(persona, runtime, root=root)
    if not dry_run:
        hs.write_holds(persona, doc, root=root)
    detail = "hold removed" if existed else "no hold was set (nothing to remove)"
    summary = f"clear-hold on {persona}: {runtime} — {detail} — reason: {args.reason}"
    return OverrideResult(action_slug="clear-hold", summary=summary, runtime=runtime)


_DISPATCH = {
    "restart": _do_restart,
    "clear-cooldown": _do_clear_cooldown,
    "hold": _do_hold,
    "clear-hold": _do_clear_hold,
}


def _ledger_entry_id(*, persona: str, action_slug: str, ts: str) -> str:
    """Single source of truth for the ledger id format.

    Used both by `_build_ledger_entry` (after the handler runs) and by
    `main()`'s pre-dispatch collision precheck (before the handler runs) —
    duplicating this f-string in two places would let the two drift and
    silently reopen the collision-detection gap this function exists to
    close (see DECISIONS in the T-2967 REBOUND-fix report).
    """
    return f"{ts}-harness-{persona}-override-{action_slug}"


def _build_ledger_entry(*, persona: str, operator: str, result: OverrideResult, ts: str) -> dict[str, Any]:
    links = [f"persona:{persona}"]
    if result.runtime:
        links.append(f"runtime:{result.runtime}")
    return {
        "id": _ledger_entry_id(persona=persona, action_slug=result.action_slug, ts=ts),
        "ts": ts,
        "agent": f"operator:{operator}",
        "action": "harness_override",
        "summary": result.summary,
        "links": links,
    }


def main(argv: Sequence[str] | None = None, *, root: Path | None = None, ledger_dir: Path | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        validate_args(args)
    except (HarnessCliError, hs.HarnessStateError) as exc:
        print(f"harness: error: {exc}", file=sys.stderr)
        return 2

    state_root = root if root is not None else hs.state_root_from_env()
    ledger_root = ledger_dir if ledger_dir is not None else LEDGER_DIR
    operator = resolve_operator(args)

    if args.command == "status":
        return _do_status(args, root=state_root)

    # Pre-compute the ledger id BEFORE dispatching to the handler (which
    # mutates state and, for `restart`, kills live tmux). `args.command`
    # always equals the eventual OverrideResult.action_slug for every
    # mutating verb (restart/clear-cooldown/hold/clear-hold match their own
    # _DISPATCH key exactly), so this is a safe precomputation, not a
    # duplicate-and-hope guess. On a dry-run there is nothing to collide
    # with (no append happens), so the check only runs for real mutations.
    ts = _utc_now_ts()
    entry_id = _ledger_entry_id(persona=args.persona, action_slug=args.command, ts=ts)

    if not args.dry_run:
        try:
            collision = entry_id in existing_entry_ids(ledger_root)
        except OSError as exc:
            print(f"harness: error: could not check ledger for id collisions: {exc}", file=sys.stderr)
            return 2
        if collision:
            print(
                f"harness: error: an override already landed for {args.persona}/{args.command} "
                "this second — retry",
                file=sys.stderr,
            )
            return 2

    handler = _DISPATCH[args.command]
    try:
        result = handler(args, operator=operator, root=state_root, dry_run=args.dry_run)
    except hs.HarnessStateError as exc:
        print(f"harness: error: {exc}", file=sys.stderr)
        return 2

    entry = _build_ledger_entry(persona=args.persona, operator=operator, result=result, ts=ts)

    if args.dry_run:
        print("[dry-run] no state files were changed.")
        print("[dry-run] would append ledger entry:")
        print(json.dumps(entry, indent=2, sort_keys=True))
    else:
        try:
            append_entry(ledger_root, entry)
        except Exception as exc:  # noqa: BLE001 — last-resort guard; see DECISIONS (residual TOCTOU)
            print(f"harness: error: failed to append ledger entry: {exc}", file=sys.stderr)
            return 2
        print(entry["summary"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
