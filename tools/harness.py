#!/usr/bin/env python3
"""tools/harness.py — manual-override CLI for the T-2953 harness controller (T-2967 BT4).

Command surface + ledger schema per notes/2026-08-18-t2953-phase1-spec.md
§Manual-override CLI (the ratify addition, TG 7935, item #3):

    harness <persona> status
    harness <persona> restart [--runtime <name>] [--clear-cooldown] [--skip-handoff] [--override-hold] --reason "<text>"
    harness <persona> clear-cooldown <runtime>|--all --reason "<text>"
    harness <persona> hold <runtime> --reason "<text>"
    harness <persona> clear-hold <runtime> --reason "<text>"
    harness <persona> pause [--for <duration> | --indefinite] [--owner <who>] [--ticket T-NNNN] --reason "<text>"
    harness <persona> wake --reason "<text>"
    harness <persona> quarantine list
    harness <persona> quarantine land|drop <entry-id> --reason "<text>"

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
import fcntl
import getpass
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import agent_role_map  # noqa: E402  — T-4737 ledger identity
from tools import harness_state as hs  # noqa: E402
from tools import seat_sleep  # noqa: E402  — ORG-106 L3a (T-5526)
from tools import seat_state  # noqa: E402  — ORG-106 L1 (T-5524)
from tools.ledger_shard import append_entry, existing_entry_ids  # noqa: E402

LEDGER_DIR = _REPO_ROOT / "ledger"

# T-5021: an override's ledger entry is landed on origin/main from a throwaway
# detached worktree, never written into the clone harness.py runs from (that is
# the SEAT's clone, and a dirty seat clone makes the consume plane refuse on
# source provenance). An entry that cannot land is spooled in the state dir.
SPOOL_SUBDIR = "harness-overrides"
SPOOL_FILENAME = "unlanded.jsonl"
QUARANTINE_FILENAME = "quarantine.jsonl"
GIT_FETCH_TIMEOUT_SECONDS = 30
GIT_PUSH_TIMEOUT_SECONDS = 60
GIT_LOCAL_TIMEOUT_SECONDS = 60
LANDING_ATTEMPTS = 3

MUTATING_COMMANDS = frozenset({"restart", "clear-cooldown", "hold", "clear-hold", "pause", "sleep", "wake"})

#: ORG-106: where `status` looks for the OOM hold. A module constant so tests can
#: point it at a tempdir instead of the host's real /var/lib/pantheon/oom/holds.json.
_OOM_HOLDS_PATH = seat_state.DEFAULT_OOM_HOLDS
#: ORG-104 rule 3 hold-down flag dir (recovery, watchman); tests inject a temp dir.
_OOM_HOLD_DIR = seat_sleep.DEFAULT_HOLD_DIR


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
    #: T-5028: entries landed BEFORE this one, in order (restart --override-hold
    #: lands its clear-hold as its own entry first).
    extra_results: tuple["OverrideResult", ...] = ()
    #: T-5028: extra ORG-001 typed links, e.g. {"type": "ledger", "ref": <id>}.
    extra_links: tuple[dict[str, str], ...] = ()
    #: T-5028: called once after the ledger step with "landed" | "spooled" |
    #: "failed" (never on a dry-run): the restart override outcome row and alert,
    #: the quarantine tombstone.
    after_landing: Any = None
    #: T-5028 review MINOR 1: a failure AFTER the override took effect (the kill
    #: ran). The entries still land and the outcome still records it; main exits 2.
    error: str = ""


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
    restart_p.add_argument(
        "--skip-handoff",
        action="store_true",
        help=(
            "Do not ask the live session to write ~/HANDOFF.md first (T-5069). Without this flag a "
            "restart of a live session refuses unless the session confirms a fresh handoff."
        ),
    )
    restart_p.add_argument(
        "--override-hold",
        action="store_true",
        help=(
            "T-5028: if --runtime is operator-held, clear the hold (its own landed ledger entry) "
            "after the handoff and before the kill, then record the outcome and alert Recovery. "
            "Without it a restart --runtime onto a held runtime is refused."
        ),
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

    pause_p = subparsers.add_parser(
        "pause", parents=[sub_global_opts],
        help="ORG-106: stop the seat taking new work (paused-intake) until it expires or `wake`.",
    )
    pause_p.add_argument(
        "--for", dest="for_duration", default=None, metavar="DURATION",
        help="How long, e.g. 30m, 4h, 2d, 1d12h (default 4h; above 7d needs --indefinite).",
    )
    pause_p.add_argument("--indefinite", action="store_true", help="No expiry (exclusive with --for).")
    pause_p.add_argument(
        "--owner", default=None,
        help="Who answers for this pause (default: the operator's role, or operator).",
    )
    pause_p.add_argument("--ticket", default=None, help="Optional ticket id (T-NNNN).")
    pause_p.add_argument("--reason", default=None, help="Required: reason for this pause.")

    sleep_p = subparsers.add_parser(
        "sleep", parents=[sub_global_opts],
        help="ORG-106: put the seat into deep-sleep (handoff, session ended, nothing relaunches it) until it expires or `wake`.",
    )
    sleep_p.add_argument(
        "--for", dest="for_duration", default=None, metavar="DURATION",
        help="How long, e.g. 8h, 2d, 1d12h (default 24h; above 14d needs --indefinite).",
    )
    sleep_p.add_argument("--indefinite", action="store_true", help="No expiry (exclusive with --for).")
    sleep_p.add_argument(
        "--owner", default=None,
        help="Who answers for this sleep (default: the operator's role, or operator).",
    )
    sleep_p.add_argument("--ticket", default=None, help="Optional ticket id (T-NNNN).")
    sleep_p.add_argument(
        "--telegram-dark-ok", action="store_true",
        help="Acknowledge that a Telegram-receiver seat goes dark: Telegram keeps updates for 24h, "
             "so a sleep longer than 20h can lose inbound messages. Required for such seats.",
    )
    sleep_p.add_argument(
        "--wait", action="store_true",
        help="After recording, wait (up to 11 min) until the controller reports phase=asleep.",
    )
    sleep_p.add_argument("--reason", default=None, help="Required: reason for this sleep.")

    wake_p = subparsers.add_parser(
        "wake", parents=[sub_global_opts], help="ORG-106: end the seat's pause (tombstones the record)."
    )
    wake_p.add_argument("--reason", default=None, help="Required: reason for this wake.")

    quarantine_p = subparsers.add_parser(
        "quarantine",
        parents=[sub_global_opts],
        help="T-5028: list, land or drop harness ledger entries parked in quarantine.jsonl.",
    )
    quarantine_p.add_argument("action", choices=("list", "land", "drop"))
    quarantine_p.add_argument("entry_id", nargs="?", default=None, help="Quarantined entry id (land/drop).")
    quarantine_p.add_argument("--reason", default=None, help="Required for land/drop.")

    return parser


# ASCII digits only, at most 6 per unit: bounds timedelta and refuses Unicode numerals.
_DURATION_RE = re.compile(r"^(?:(\d{1,6})d)?(?:(\d{1,6})h)?(?:(\d{1,6})m)?$", re.ASCII)
_TICKET_RE = re.compile(r"^T-\d+$")
_OWNER_MAX = 80


def parse_duration(text: str) -> timedelta:
    """`30m`, `4h`, `2d`, `1d12h` -> timedelta. Must be positive; no other units."""
    match = _DURATION_RE.fullmatch(text or "")
    if not text or match is None:
        raise HarnessCliError(f"--for {text!r}: expected e.g. 30m, 4h, 2d or 1d12h")
    days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    try:
        delta = timedelta(days=days, hours=hours, minutes=minutes)
    except (OverflowError, ValueError) as exc:
        raise HarnessCliError(f"--for {text!r}: out of range ({exc})") from exc
    if delta <= timedelta(0):
        raise HarnessCliError(f"--for {text!r}: duration must be positive")
    return delta


def _validate_pause_args(args: argparse.Namespace, *, state: str = seat_state.STATE_PAUSED_INTAKE) -> None:
    if args.for_duration is not None and args.indefinite:
        raise HarnessCliError("--for and --indefinite are mutually exclusive")
    if args.for_duration is not None:
        ttl = parse_duration(args.for_duration)
        cap = seat_state.TTL_MAX[state]
        if ttl > cap:
            raise HarnessCliError(
                f"--for {args.for_duration} is above the {cap.days}d maximum; use --indefinite "
                f"to {'sleep' if state == seat_state.STATE_DEEP_SLEEP else 'pause'} with no expiry"
            )
    if args.ticket is not None and not _TICKET_RE.fullmatch(args.ticket):
        raise HarnessCliError(f"--ticket {args.ticket!r}: expected T-NNNN")
    owner = args.owner
    if owner is not None and not (
        1 <= len(owner) <= _OWNER_MAX and owner.isprintable() and not any(c.isspace() for c in owner)
    ):
        raise HarnessCliError(f"--owner: 1-{_OWNER_MAX} printable characters, no spaces")


def validate_args(args: argparse.Namespace) -> None:
    """Post-parse validation shared by the CLI and the telegram parser."""
    if args.command in MUTATING_COMMANDS and not args.reason:
        raise MissingReasonError(args.command)

    if args.command == "clear-cooldown":
        if bool(args.runtime) == bool(args.all):
            raise HarnessCliError("clear-cooldown requires exactly one of <runtime> or --all")

    if args.command == "restart" and args.clear_cooldown and not args.runtime:
        raise HarnessCliError("--clear-cooldown requires --runtime")

    if args.command == "restart" and getattr(args, "override_hold", False) and not args.runtime:
        raise HarnessCliError("--override-hold requires --runtime")

    if args.command == "pause":
        _validate_pause_args(args)
    if args.command == "sleep":
        _validate_pause_args(args, state=seat_state.STATE_DEEP_SLEEP)

    if args.command == "quarantine" and args.action in ("land", "drop"):
        if not args.entry_id:
            raise HarnessCliError(f"quarantine {args.action} requires <entry-id>")
        if not args.reason:
            raise MissingReasonError(f"quarantine {args.action}")
    if args.command == "quarantine" and args.action == "list" and args.entry_id:
        raise HarnessCliError("quarantine list takes no <entry-id>")

    hs.validate_persona(args.persona)
    # T-4737: a role id shared by several seats cannot say whose override this
    # is (ORG-001 executor attribution); refuse it before anything runs.
    operator = getattr(args, "operator", None)
    if operator and operator in agent_role_map.NON_INJECTIVE_ROLE_IDS:
        seats = ", ".join(agent_role_map.personas_for_role_id(operator))
        raise HarnessCliError(
            f"--operator {operator} is a role shared by several seats ({seats}); "
            "name the seat instead"
        )
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


def _request_handoff_first(persona: str) -> Any:
    """Ask the live session for a fresh HANDOFF and wait (bounded) for the ack (T-5069).

    Returns a handoff_first.HandoffOutcome. Tests patch this seam. Anything
    unreadable from this account (manifest, tmux socket) comes back as an
    unconfirmed outcome, never an exception, so the restart refuses (or the
    operator passes --skip-handoff).
    """
    from tools import handoff_first, persona_bringup, runtime_controller as rc

    path = Path.home() / "HANDOFF.md"
    try:
        primary = persona_bringup.load_binding(persona_bringup.DEFAULT_MANIFEST, persona)
        bindings = persona_bringup.configured_runtime_bindings(persona_bringup.DEFAULT_MANIFEST, primary)
        path = handoff_first.binding_handoff_path(primary)
        probe = _default_pane_probe(bindings, persona)
        if probe is None:
            return handoff_first.HandoffOutcome(False, "no live pane to ask", path)
        live = next(b for b in bindings if rc._binding_runtime_ref(b) == probe[0])
        return handoff_first.request_handoff_for_binding(live)
    except Exception as exc:  # noqa: BLE001
        return handoff_first.HandoffOutcome(False, f"{type(exc).__name__}: {exc}", path)


def _tmux_session_live(session: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0


def _default_pane_probe(bindings: Sequence[Any] | None, persona: str) -> tuple[str, int] | None:
    """Which configured runtime the LIVE pane is running: (ref, pane pid), or None
    if there is no live pane. Raises on anything unreadable from this account
    (tmux socket, /proc, manifest); the caller degrades to "unavailable"."""
    from tools import persona_bringup, runtime_controller as rc

    if bindings is None:
        primary = persona_bringup.load_binding(persona_bringup.DEFAULT_MANIFEST, persona)
        bindings = persona_bringup.configured_runtime_bindings(persona_bringup.DEFAULT_MANIFEST, primary)
    if not bindings:
        raise RuntimeError("no configured bindings")
    target = bindings[0].controller.tmux
    if not (rc._session_exists(target.session) and rc.pane_exists(target)):
        return None
    pid = rc._pane_process_pid(target)
    if pid is None:
        raise RuntimeError("cannot read the pane pid (tmux socket not visible from this account?)")
    argv = rc._read_pane_cmdline(pid)
    if not argv:
        raise RuntimeError(f"cannot read /proc/{pid}/cmdline")
    matches = [b for b in bindings if rc._pane_argv_matches_binding(argv, b)]
    if len(matches) != 1:
        raise RuntimeError(f"pane argv matched {len(matches)} configured runtimes")
    return rc._binding_runtime_ref(matches[0]), pid


def _print_seat_state(persona: str, *, root: Path) -> None:
    """ORG-106 L1: the effective seat state (all sources). Never raises."""
    try:
        eff = seat_state.effective(persona, state_root=root, oom_holds_path=_OOM_HOLDS_PATH)
    except Exception as exc:  # noqa: BLE001 -- status must never raise
        print(f"seat state: unavailable ({type(exc).__name__}: {exc})"[:300])
        return
    print(f"seat state: {eff.state}")
    if eff.sources:
        print(f"  sources: {', '.join(eff.sources)}")
        print(f"  owner: {eff.owner or '-'}")
        print(f"  reason: {eff.reason or '-'}")
        if eff.expires:
            print(f"  expires: {eff.expires} ({_time_left(eff.expires, datetime.now(timezone.utc))})")
        else:
            print("  expires: never")
    if eff.fault:
        print(f"  fault: {eff.fault}")
    try:  # ORG-106 L3: the deep-sleep phase (draining -> asleep)
        rec = hs.load_seat_state(persona, root=root)
        if isinstance(rec, dict) and rec.get("state") == seat_state.STATE_DEEP_SLEEP:
            print(f"  phase: {rec.get('phase')} (since {rec.get('phase_ts')})")
    except Exception:  # noqa: BLE001 -- status must never raise
        pass


def _do_status(
    args: argparse.Namespace,
    *,
    root: Path,
    bindings: Sequence[Any] | None = None,
    pane_probe: Any = None,
) -> int:
    persona = args.persona
    active = hs.load_active_runtime(persona, root=root)
    holds_malformed = None
    try:
        holds_file = hs.holds_path(persona, root=root)
        if (holds_file.exists() or holds_file.is_symlink()) and not holds_file.is_file():
            raise hs.HarnessStateError(f"{holds_file} is not a readable regular file")
        raw = hs._read_json_or_none(holds_file)
        held = {} if raw is None else raw.get("holds")
        if not isinstance(held, dict):
            # Same rule as runtime_controller.load_holds: a missing or non-object
            # 'holds' is malformed, not "no holds".
            raise hs.HarnessStateError(
                f"missing or non-object 'holds' in {holds_file}"
            )
    except hs.HarnessStateError as exc:
        # T-5028 (e): both runtime planes fail CLOSED on this; say so, don't crash.
        holds_malformed, held = str(exc), {}
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

    # T-4862: read-only live-pane identity; never raises (unreadable tmux//proc degrade).
    try:
        probe = pane_probe(bindings) if pane_probe is not None else _default_pane_probe(bindings, persona)
        if probe is None:
            print("live pane: none (no live pane found)")
        else:
            live_ref, live_pid = probe
            print(f"live pane: {live_ref} (pid={live_pid})")
            if active and active.get("runtime") not in (None, live_ref):
                print(
                    f"  MISMATCH: cache says {active.get('runtime')} but the live pane runs {live_ref} "
                    "— the cache is stale; trust the pane"
                )
    except Exception as exc:  # noqa: BLE001 -- status must never raise
        print(f"live pane: unavailable ({type(exc).__name__}: {exc})"[:300])

    if holds_malformed is not None:
        print(f"holds: MALFORMED ({holds_malformed})")
        print("  both runtime planes treat EVERY runtime as held until this file is fixed")
    elif held:
        print("holds:")
        for runtime, info in sorted(held.items()):
            print(f"  {runtime}: {info.get('reason')!r} (set by {info.get('set_by')} at {info.get('set_ts')})")
    else:
        print("holds: none")

    _print_seat_state(persona, root=root)

    if records:
        print("runtime last-attempts:")
        active_cooldowns = []
        for runtime, doc in sorted(records.items()):
            cooldown = doc.get("cooldown_until")
            if holds_malformed is not None or runtime in held:
                viability = "held"
            elif doc.get("outcome") == "failed" and doc.get("failure_class") in hs.UNTIMED_BLOCKING_FAILURE_CLASSES:
                viability = "blocked"  # T-5028 (d) / T-5791: until clear-cooldown
            elif cooldown and cooldown > now:
                viability = "cooldown-active"
                active_cooldowns.append(cooldown)
            else:
                viability = "viable"
            trigger = (doc.get("provenance") or {}).get("trigger")
            print(
                f"  {runtime}: outcome={doc.get('outcome')} failure_class={doc.get('failure_class')} "
                f"cooldown_until={cooldown} consecutive_failures={doc.get('consecutive_failures')} "
                f"[{viability}]" + (f" trigger={trigger}" if trigger in ("early-reset-probe", "mid-session-cap-detect", "usability-probe") else "")
            )
        next_expiry = min(active_cooldowns) if active_cooldowns else None
        print(f"next cooldown expiry: {next_expiry or 'none'}")
    else:
        print("runtime last-attempts: none recorded")

    usability = hs.list_usability(persona, root=root)
    if usability:
        print("runtime usability (live probe; a verdict is an observation, not a reset promise):")
        for runtime, doc in sorted(usability.items()):
            print(
                f"  {runtime}: verdict={doc.get('verdict')} source={doc.get('source')} "
                f"observed_at={doc.get('observed_at')} reprobe_at={doc.get('reprobe_at')}"
            )
    # T-5021: an override whose ledger entry has not reached origin/main yet.
    unlanded = _read_spool(root)
    if unlanded:
        print(f"unlanded ledger entries: {len(unlanded)} (spooled at {spool_path(root)})")
    quarantined = _open_quarantine(root)  # T-5028 (f): open rows only
    if quarantined:
        print(f"quarantined ledger entries: {len(quarantined)} (at {quarantine_path(root)})")
    for row in _orphan_overrides(persona, root):  # T-5028 (f+)
        print(
            f"override without outcome: {row.get('destination')} {row.get('correlation_id')} "
            f"(recorded {row.get('recorded_at')}; no outcome after {hs.OVERRIDE_OUTCOME_GRACE_SECONDS}s "
            "— the swap/restart may have died mid-way; check the live runtime and the hold)"
        )
    return 0


def _personas_root(root: Path) -> Path:
    return root / "personas"


def _restart_hold_gate(args: argparse.Namespace, *, root: Path, dry_run: bool) -> bool:
    """T-5028 (a-harness): True iff this restart must clear a hold on --runtime.
    Raises (nothing killed, nothing asked) when --runtime is held and
    --override-hold was not given. Reads holds the way both runtime planes do
    (malformed fails closed: every runtime is held)."""
    from tools import runtime_controller as rc

    runtime = args.runtime
    override = bool(getattr(args, "override_hold", False))
    if not runtime:
        return False
    held = runtime in rc.load_holds(args.persona, state_root=_personas_root(root))
    if held and not override:
        raise hs.HarnessStateError(
            f"restart refused: {runtime} is operator-held for {args.persona}, so the respawn would "
            "never select it; nothing was killed. Pass --override-hold to clear the hold as part of "
            "this restart (a manifest-earlier viable runtime still wins the next selection)"
        )
    if override and not held and not dry_run:
        _harness_evidence(
            args.persona, "operator_swap_override_hold_noop",
            {"destination": runtime, "correlation_id": _restart_correlation(args),
             "detail": "override-hold-noop: destination not held", "path": "harness-restart"},
            root=root,
        )
    return held and override


def _restart_correlation(args: argparse.Namespace) -> str:
    return _ledger_entry_id(persona=args.persona, action_slug="restart", ts=getattr(args, "ledger_ts", "") or "")


def _harness_evidence(persona: str, event: str, payload: dict[str, Any], *, root: Path) -> None:
    """Best-effort controller-evidence row from the CLI (same log the planes write)."""
    from tools import runtime_controller as rc

    try:
        rc._append_controller_evidence(persona, event, payload, state_root=_personas_root(root))
    except Exception:  # noqa: BLE001 -- evidence never decides an override
        pass


def _write_operator_restart_marker(
    args: argparse.Namespace, *, persona: str, operator: str, root: Path, session: str
) -> bool:
    """T-5483: drop the durable operator-restart marker BEFORE the kill so the
    runtime controller (which may re-exec) does not record the kill as a runtime
    failure. Fail-open: a write failure never blocks the restart (stderr + evidence)."""
    try:
        active = hs.load_active_runtime(persona, root=root) or {}
        runtime_ref = active.get("runtime")
        if not isinstance(runtime_ref, str) or not runtime_ref:
            return False
        pid = active.get("pid")
        hs.write_operator_restart_marker(
            persona, root=root, operator=operator, runtime=runtime_ref, tmux_session=session,
            pid=pid if isinstance(pid, int) else None,
            ts=_utc_now_ts(),  # fresh: the handoff wait can precede the kill by minutes
            correlation_id=_restart_correlation(args),
        )
        return True
    except Exception as exc:  # noqa: BLE001 -- fail-open: the restart must still proceed
        msg = f"{type(exc).__name__}: {exc}"[:300]
        print(f"harness: WARN operator-restart marker not written ({msg}); restart proceeds", file=sys.stderr)
        _harness_evidence(persona, "operator_restart_marker_write_failed", {"error": msg}, root=root)
        return False


def _do_restart(args: argparse.Namespace, *, operator: str, root: Path, dry_run: bool) -> OverrideResult:
    persona = args.persona
    # ORG-106 L3a: nothing relaunches a deep-sleeping seat, so a restart would only kill
    # it (or do nothing). Refuse before the handoff, the hold gate and the kill.
    if seat_sleep.launch_block(persona, root=root, oom_holds_path=_OOM_HOLDS_PATH).sleeping:
        raise hs.HarnessStateError(
            f"restart refused: {persona} is in deep-sleep and nothing will relaunch it; "
            f"run `harness {persona} wake --reason ...` first"
        )
    runtime = args.runtime
    now_ts = _utc_now_ts()
    if runtime and args.clear_cooldown:
        # T-5028 review MINOR 1: a malformed last-attempt.json refuses HERE, before
        # the hold gate, the handoff and the kill, with nothing changed.
        hs.load_last_attempt(persona, runtime, root=root)
    clear_held = _restart_hold_gate(args, root=root, dry_run=dry_run)
    hold_summary = ""
    cleared: OverrideResult | None = None

    session = _tmux_session_for_persona(persona, root=root)
    if session is None:
        kill_summary = (
            "no active-runtime cache found; nothing to kill "
            "(persona may not be running under the harness controller yet)"
        )
        if clear_held:
            hold_summary, cleared = _restart_clear_hold(args, operator=operator, root=root, dry_run=dry_run)
    else:
        handoff_summary = ""
        if _tmux_session_live(session) and not getattr(args, "skip_handoff", False):
            if dry_run:
                handoff_summary = "would ask the live session for a fresh handoff first; "
            else:
                outcome = _request_handoff_first(persona)
                if not outcome.confirmed:
                    raise hs.HarnessStateError(
                        f"restart refused: the live session did not confirm a fresh handoff "
                        f"({outcome.reason}); nothing was killed. Retry, or pass --skip-handoff "
                        f"to restart without one"
                    )
                handoff_summary = "fresh handoff confirmed; "
        elif getattr(args, "skip_handoff", False):
            handoff_summary = "handoff skipped (--skip-handoff); "
        if clear_held:
            hold_summary, cleared = _restart_clear_hold(args, operator=operator, root=root, dry_run=dry_run)
        marker_written = False
        if not dry_run and _tmux_session_live(session):
            marker_written = _write_operator_restart_marker(
                args, persona=persona, operator=operator, root=root, session=session
            )
        try:
            was_live = _kill_tmux_session(session, dry_run=dry_run)
        except BaseException:
            if marker_written:
                hs.remove_operator_restart_marker(persona, root=root)
            raise
        if marker_written and not was_live:
            hs.remove_operator_restart_marker(persona, root=root)
        kill_summary = handoff_summary + (
            f"killed live tmux session {session!r}" if was_live else f"tmux session {session!r} was not live; nothing to kill"
        )

    cooldown_summary = ""
    late_error = ""
    if runtime and args.clear_cooldown:
        try:
            doc, had_cooldown = hs.clear_cooldown(persona, runtime, operator=operator, now_ts=now_ts, root=root)
            if not dry_run:
                hs.write_last_attempt(persona, runtime, doc, root=root)
        except Exception as exc:  # noqa: BLE001 -- the kill already ran; land what happened
            late_error = f"cooldown clear for {runtime} failed after the restart: {type(exc).__name__}: {exc}"[:500]
            cooldown_summary = f"; {late_error}"
        else:
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

    summary = (
        f"restart on {persona}: {hold_summary}{kill_summary}{cooldown_summary}{force_caveat} — reason: {args.reason}"
    )
    if cleared is None:
        return OverrideResult(action_slug="restart", summary=summary, runtime=runtime, error=late_error)
    correlation = _restart_correlation(args)

    def outcome(status: str) -> None:
        _restart_override_outcome(
            args, root=root, status=status, kill_summary=kill_summary, correlation=correlation, error=late_error
        )

    return OverrideResult(
        action_slug="restart", summary=summary, runtime=runtime, extra_results=(cleared,),
        extra_links=({"type": "ledger", "ref": _ledger_entry_id(persona=persona, action_slug="clear-hold",
                                                                ts=getattr(args, "ledger_ts", "") or "")},),
        after_landing=None if dry_run else outcome,
        error=late_error,
    )


def _restart_clear_hold(
    args: argparse.Namespace, *, operator: str, root: Path, dry_run: bool
) -> tuple[str, OverrideResult]:
    """Clear the hold on --runtime: after T-5069's handoff step, before the kill.
    Raises (nothing killed) if the clear cannot take effect."""
    from tools import runtime_controller as rc

    del operator
    persona, runtime = args.persona, args.runtime
    restart_id = _restart_correlation(args)
    clear_summary = (
        f"clear-hold on {persona}: {runtime} — hold removed by restart --override-hold ({restart_id}) "
        f"— reason: {args.reason}"
    )
    result = OverrideResult(action_slug="clear-hold", summary=clear_summary, runtime=runtime,
                            extra_links=({"type": "ledger", "ref": restart_id},))
    if dry_run:
        return f"would clear the hold on {runtime} (--override-hold); ", result
    doc, _existed = hs.clear_hold(persona, runtime, root=root)
    hs.write_holds(persona, doc, root=root)
    if runtime in rc.load_holds(persona, state_root=_personas_root(root)):
        raise hs.HarnessStateError(f"--override-hold could not clear the hold on {runtime}; nothing was killed")
    _harness_evidence(
        persona, hs.OVERRIDE_EVENT,
        {"destination": runtime, "correlation_id": restart_id, "reason": args.reason,
         "path": "harness-restart", "ledger": "pending"},
        root=root,
    )
    return f"hold on {runtime} cleared (--override-hold); ", result


def _restart_override_outcome(
    args: argparse.Namespace, *, root: Path, status: str, kill_summary: str, correlation: str, error: str = ""
) -> None:
    """One outcome row + one Recovery alert after the restart's ledger step. Never raises."""
    from tools import runtime_controller as rc

    persona, runtime = args.persona, args.runtime
    try:
        still_cleared = runtime not in rc.load_holds(persona, state_root=_personas_root(root))
    except Exception:  # noqa: BLE001
        still_cleared = False
    _harness_evidence(
        persona, hs.OVERRIDE_OUTCOME_EVENT,
        {"destination": runtime, "correlation_id": correlation, "ok": not error, "status": "restarted",
         "error": error, "hold_still_cleared": still_cleared, "ledger": status, "path": "harness-restart",
         "detail": kill_summary[:500]},
        root=root,
    )
    try:
        rc._route_failure_alert(
            persona,
            f"harness restart on {persona}: hold on {runtime} cleared with --override-hold ({correlation}; "
            f"ledger {status}); {kill_summary}; {('FAILED LATE: ' + error + '; ') if error else ''}hold {'stays cleared' if still_cleared else 'is set again'} "
            f"— reason: {args.reason}"[:1500],
            f"operator-hold-override ({persona}) {runtime} {correlation}",
        )
    except Exception:  # noqa: BLE001 -- alerting is best-effort
        pass


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


def _quarantine_row(root: Path, entry_id: str) -> dict[str, Any]:
    rows = [r for r in _open_quarantine(root) if str(r["entry"].get("id")) == entry_id]
    if not rows:
        raise hs.HarnessStateError(
            f"no open quarantined entry {entry_id!r} under {quarantine_path(root)} "
            "(unknown, or already landed/dropped)"
        )
    return rows[-1]


def _quarantine_id_lock(root: Path, entry_id: str) -> int:
    """Non-blocking exclusive flock for one quarantined id; the caller closes it."""
    import hashlib

    path = quarantine_path(root).parent / f".quarantine-{hashlib.sha256(entry_id.encode()).hexdigest()[:16]}.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise hs.HarnessStateError(
            f"another quarantine land/drop of {entry_id!r} is running; retry when it finishes"
        ) from None
    return fd


def _append_tombstone(root: Path, entry_id: str, *, action: str, reason: str, landed_as: str | None) -> None:
    """Append-only under the spool flock: resolves every quarantine row for `entry_id`."""
    row = {"tombstone": entry_id, "action": action, "reason": reason, "at": _utc_now_ts()}
    if landed_as:
        row["landed_as"] = landed_as
    fd = _spool_lock(root)
    try:
        path = quarantine_path(root)
        out = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(out, (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(out)
    finally:
        os.close(fd)


def _do_quarantine(args: argparse.Namespace, *, operator: str, root: Path, dry_run: bool) -> OverrideResult:
    """T-5028 (f) land|drop. `land` lands a NEW entry (fresh id) carrying the original
    verbatim, since the original can never land as-is (conflicting id or a refused
    shape); `drop` lands a record of the drop. Either way the quarantine row is
    tombstoned only after the ledger step landed or spooled the new entry."""
    del operator
    # T-5028 review MINOR 3: one land/drop per id at a time (held until the
    # tombstone step), and the open-row lookup under the spool flock.
    id_lock = _quarantine_id_lock(root, args.entry_id)
    try:
        spool_fd = _spool_lock(root)
        try:
            row = _quarantine_row(root, args.entry_id)
        finally:
            os.close(spool_fd)
    except BaseException:
        os.close(id_lock)
        raise
    original = row["entry"]
    slug = f"quarantine-{args.action}"
    new_id = _ledger_entry_id(persona=args.persona, action_slug=slug, ts=getattr(args, "ledger_ts", "") or "")
    original_text = json.dumps(original, sort_keys=True)[:2000]
    if args.action == "land":
        summary = (
            f"quarantine land on {args.persona}: re-landed quarantined entry {args.entry_id} as {new_id} "
            f"(quarantined because: {row.get('reason')}) — original: {original_text} — reason: {args.reason}"
        )
    else:
        summary = (
            f"quarantine drop on {args.persona}: dropped quarantined entry {args.entry_id} "
            f"(quarantined because: {row.get('reason')}) — original: {original_text} — reason: {args.reason}"
        )
    runtime = next(
        (link.get("ref") for link in original.get("links", []) if isinstance(link, dict) and link.get("type") == "runtime"),
        None,
    )

    def tombstone(status: str) -> None:
        try:
            if status in ("landed", "spooled"):
                _append_tombstone(
                    root, args.entry_id, action="landed" if args.action == "land" else "dropped",
                    reason=args.reason, landed_as=new_id if args.action == "land" else None,
                )
        finally:
            os.close(id_lock)

    if dry_run:
        os.close(id_lock)
    return OverrideResult(
        action_slug=slug, summary=summary, runtime=runtime if isinstance(runtime, str) else None,
        extra_links=({"type": "ledger", "ref": str(args.entry_id)},),
        after_landing=None if dry_run else tombstone,
    )


def _do_quarantine_list(args: argparse.Namespace, *, root: Path) -> int:
    rows = _open_quarantine(root)
    if not rows:
        print(f"quarantine: none open (at {quarantine_path(root)})")
        return 0
    print(f"quarantine: {len(rows)} open (at {quarantine_path(root)})")
    for row in rows:
        entry = row["entry"]
        personas = [l.get("ref") for l in entry.get("links", []) if isinstance(l, dict) and l.get("type") == "persona"]
        print(
            f"  {entry.get('id')}: persona={','.join(map(str, personas)) or '?'} "
            f"quarantined_at={row.get('quarantined_at')} reason={row.get('reason')!r}"
        )
    return 0


def _fmt_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_pause_owner(operator: str) -> str:
    """Owner when --owner is omitted: `operator` for a human operator, else the seat
    operator's ORG-100 role as `role:<id>`, else `operator:<user>`."""
    agent, _executor = ledger_identity_for_operator(operator)
    if agent == "human:operator":
        return "operator"
    return f"role:{agent}" if agent else f"operator:{operator}"


def _time_left(expires: str, now: datetime) -> str:
    end = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    secs = int((end - now).total_seconds())
    if secs <= 0:
        return "expired"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = [f"{days}d"] if days else []
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return "".join(parts) + " left"


class SleepFlagError(hs.HarnessStateError):
    """The ORG-104 hold-down flag could not be removed. Raised under the seat-state lock; the
    lock wrapper pages (cooldown-gated, after a repeat) once the lock is released."""


def _clear_sleep_flag(persona: str, root: Path) -> None:
    """Remove the ORG-104 hold-down flag for a system-orchestrator seat (no-op otherwise).
    A failure refuses the operation (SleepFlagError): the record stays, nothing half-done.
    The failure is counted durably so a repeat pages (ORG-106 L3b)."""
    try:
        seat_sleep.clear_sysorch_flag_accounted(persona, root=root, hold_dir=_OOM_HOLD_DIR)
    except OSError as exc:
        raise SleepFlagError(
            f"could not remove the oom-hold flag for {persona} ({exc}); the seat-state record was "
            "left as it is. Fix the flag permissions (group pantheon-health) and retry"
        ) from exc


def _do_pause_unlocked(args: argparse.Namespace, *, operator: str, root: Path, dry_run: bool) -> OverrideResult:
    persona = args.persona
    existing = hs.load_seat_state(persona, root=root)  # raises (path named) if unparseable
    downgrade = isinstance(existing, dict) and existing.get("state") == seat_state.STATE_DEEP_SLEEP
    ts = args.ledger_ts
    now = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    indefinite = bool(args.indefinite)
    if indefinite:
        expires = None
    else:
        ttl = (parse_duration(args.for_duration) if args.for_duration is not None
               else seat_state.TTL_DEFAULT[seat_state.STATE_PAUSED_INTAKE])
        expires = _fmt_ts(now + ttl)
    owner = args.owner or _default_pause_owner(operator)
    record = {
        "schema": hs.SCHEMA_SEAT_STATE,
        "persona": persona,
        "state": seat_state.STATE_PAUSED_INTAKE,
        "phase": seat_state.PHASE_SET,
        "phase_ts": ts,
        "set_ts": ts,
        "set_by": f"operator:{operator}",
        "owner": owner,
        "reason": args.reason,
        "ticket": args.ticket,
        "expires": expires,
        "indefinite": indefinite,
        "telegram_dark_ack": False,
        "pre_expiry_notified": False,
        "ledger_entry": _ledger_entry_id(persona=persona, action_slug="pause", ts=ts),
    }
    problems = seat_state.validate_record(record)
    if problems:
        raise hs.HarnessStateError("refusing to write an invalid seat-state record: " + "; ".join(problems))
    refreshed = isinstance(existing, dict) and existing.get("state") == seat_state.STATE_PAUSED_INTAKE
    if not dry_run:
        if downgrade:
            # ORG-106 L3a: deep-sleep -> paused-intake. The system-orchestrator hold-down
            # flag goes FIRST: a stuck flag keeps the seat down and is the dangerous side.
            _clear_sleep_flag(persona, root)
            hs.remove_sleep_marker(persona, root=root)
        if existing is not None:  # P4: the replaced record leaves a tombstone first
            hs.append_seat_tombstone(
                persona, {**existing, "ended_ts": ts, "ended_by": f"replaced:operator:{operator}"}, root=root)
        hs.write_seat_state(persona, record, root=root)
    until = f"until {expires}" if expires else "indefinitely"
    summary = (
        f"pause on {persona}: intake paused {until}"
        f"{' (refreshed)' if refreshed else ''}{' (downgraded from deep-sleep; the seat relaunches on the next consume fire)' if downgrade else ''} "
        f"— owner: {owner} — reason: {args.reason}"
    )
    return OverrideResult(action_slug="pause", summary=summary, runtime=None)


#: ORG-106 L3b test seams (module constants / functions, like _OOM_HOLD_DIR).
SLEEP_WAIT_TIMEOUT_S = 660.0
SLEEP_WAIT_POLL_S = 2.0
_wait_sleep = time.sleep


def _default_session_probe(persona: str) -> bool | None:
    """Is a live session running for `persona`? True / False, or None when this account cannot
    tell (tmux socket or manifest unreadable): None leaves the finish to the controller."""
    try:
        return _default_pane_probe(None, persona) is not None
    except Exception:  # noqa: BLE001
        return None


def _default_fast_path_seams(persona: str, root: Path) -> Any:
    """Production seams for the no-session fast path (the controller's own factory)."""
    from tools import persona_bringup, runtime_controller as rc

    primary = persona_bringup.load_binding(persona_bringup.DEFAULT_MANIFEST, persona)
    cands = persona_bringup.configured_runtime_bindings(persona_bringup.DEFAULT_MANIFEST, primary)
    return rc.build_seat_sleep_seams(persona, cands, state_root=root / "personas")


_SESSION_PROBE: Callable[[str], bool | None] = _default_session_probe
_FAST_PATH_SEAMS: Callable[[str, Path], Any] = _default_fast_path_seams


def _sleep_preflight(args: argparse.Namespace) -> None:
    """Refusals that need the manifest, in the order that matters, before ANY side effect:
    non-control-plane seat, then the Telegram-dark guard. (Reason and TTL limits are checked
    earlier, in validate_args.)"""
    persona = args.persona
    supervisor = agent_role_map.instance_binding(persona).get("supervisor")
    if supervisor != "control-plane":
        raise hs.HarnessStateError(
            f"sleep refused: {persona} is not supervised by the harness control plane "
            f"(supervisor: {supervisor or 'none'}); deep-sleep only drains and holds control-plane "
            "seats. A bespoke seat (the codex daemon) is not put to sleep with this verb"
        )
    ttl = None if args.indefinite else (
        parse_duration(args.for_duration) if args.for_duration is not None
        else seat_state.TTL_DEFAULT[seat_state.STATE_DEEP_SLEEP])
    if (ttl is None or ttl > seat_state.TELEGRAM_DARK_THRESHOLD) and not args.telegram_dark_ok \
            and seat_sleep.is_telegram_receiver(persona):
        raise hs.HarnessStateError(
            f"sleep refused: {persona} has a Telegram receiver, and Telegram keeps undelivered "
            f"updates for only 24h; a sleep longer than {int(seat_state.TELEGRAM_DARK_THRESHOLD.total_seconds() // 3600)}h "
            "(or --indefinite) can lose inbound messages. Re-run with --telegram-dark-ok to accept "
            "that, or sleep for a shorter time (--for 20h)"
        )


def _sleep_wait_for_asleep(persona: str, root: Path) -> None:
    deadline = SLEEP_WAIT_TIMEOUT_S
    waited = 0.0
    while True:
        rec = hs.load_seat_state(persona, root=root)
        if not isinstance(rec, dict) or rec.get("state") != seat_state.STATE_DEEP_SLEEP:
            raise hs.HarnessStateError(f"{persona}: the deep-sleep record is gone (woken or replaced) while waiting")
        if rec.get("phase") == seat_state.PHASE_ASLEEP:
            print(f"{persona}: asleep (drain outcome: {rec.get('drain_outcome') or '-'})")
            return
        if waited >= deadline:
            raise hs.HarnessStateError(
                f"{persona}: sleep recorded but still draining after {int(waited)}s; the seat's "
                "controller finishes the drain (watch `harness status`)")
        _wait_sleep(SLEEP_WAIT_POLL_S)
        waited += SLEEP_WAIT_POLL_S


def _sleep_after_landing(args: argparse.Namespace, *, root: Path) -> Callable[[str], None]:
    def after(_status: str) -> None:
        persona = args.persona
        # A1: a seat with no live session goes straight to asleep (drain_outcome no-session).
        # Outside the lock (the drain re-takes it); a session that launches in the gap is
        # re-drained by the controller (asleep + live session).
        if _SESSION_PROBE(persona) is False:
            from tools import seat_sleep_drain

            result = seat_sleep_drain.run_drain(_FAST_PATH_SEAMS(persona, root))
            print(f"{persona}: no live session; drain {result}")
        if args.wait:
            _sleep_wait_for_asleep(persona, root)
    return after


def _do_sleep_unlocked(args: argparse.Namespace, *, operator: str, root: Path, dry_run: bool) -> OverrideResult:
    persona = args.persona
    existing = hs.load_seat_state(persona, root=root)  # raises (path named) if unparseable
    ts = args.ledger_ts
    now = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    indefinite = bool(args.indefinite)
    if indefinite:
        expires = None
    else:
        ttl = (parse_duration(args.for_duration) if args.for_duration is not None
               else seat_state.TTL_DEFAULT[seat_state.STATE_DEEP_SLEEP])
        expires = _fmt_ts(now + ttl)
    owner = args.owner or _default_pause_owner(operator)
    # A refresh of a live deep-sleep record keeps its phase and drain bookkeeping: an asleep
    # seat stays asleep and a draining one keeps its remaining budget (no re-prompt).
    refreshed = (isinstance(existing, dict) and existing.get("state") == seat_state.STATE_DEEP_SLEEP
                 and not seat_state.validate_record(existing)
                 and seat_state.parse_record(existing, now=now).active)
    carried: dict[str, Any] = {}
    if refreshed:
        carried = {k: existing[k] for k in ("drain_started_ts", "handoff_prompted_ts", "drain_outcome")
                   if existing.get(k) is not None}
    record = {
        "schema": hs.SCHEMA_SEAT_STATE,
        "persona": persona,
        "state": seat_state.STATE_DEEP_SLEEP,
        "phase": existing["phase"] if refreshed else seat_state.PHASE_DRAINING,
        "phase_ts": existing["phase_ts"] if refreshed else ts,
        "set_ts": ts,
        "set_by": f"operator:{operator}",
        "owner": owner,
        "reason": args.reason,
        "ticket": args.ticket,
        "expires": expires,
        "indefinite": indefinite,
        "telegram_dark_ack": bool(args.telegram_dark_ok) and seat_sleep.is_telegram_receiver(persona),
        "pre_expiry_notified": False,
        "ledger_entry": _ledger_entry_id(persona=persona, action_slug="sleep", ts=ts),
        **({"drain_started_ts": ts} if not refreshed else carried),
    }
    problems = seat_state.validate_record(record)
    if problems:
        raise hs.HarnessStateError("refusing to write an invalid seat-state record: " + "; ".join(problems))
    after_landing = None
    if not dry_run:
        flag_existed = False
        if seat_sleep.is_sysorch(persona):
            flag_existed = seat_sleep.sysorch_flag_path(persona, hold_dir=_OOM_HOLD_DIR).exists()
        try:
            # The system-orchestrator hold-down flag goes FIRST: if it cannot be written the
            # sleep is refused and nothing is recorded (fail toward a running seat, ORG-106 s6).
            seat_sleep.write_sysorch_flag(
                persona, hold_dir=_OOM_HOLD_DIR, note=f"deep-sleep {persona}: {args.reason}")
        except OSError as exc:
            raise hs.HarnessStateError(
                f"sleep refused: could not write the oom-hold flag for {persona} ({exc}); no "
                "record was written. This seat account needs write access to the flag directory "
                "(group pantheon-health); that is a privilege change, report it rather than work around it"
            ) from exc
        try:
            if existing is not None:  # P4: the replaced record leaves a tombstone first
                hs.append_seat_tombstone(
                    persona, {**existing, "ended_ts": ts, "ended_by": f"replaced:operator:{operator}"},
                    root=root)
            if not refreshed:
                hs.remove_sleep_marker(persona, root=root)
            hs.write_seat_state(persona, record, root=root)
        except BaseException:
            if not flag_existed:
                try:
                    seat_sleep.clear_sysorch_flag(persona, hold_dir=_OOM_HOLD_DIR)
                except OSError:
                    pass
            raise
        after_landing = _sleep_after_landing(args, root=root)
    until = f"until {expires}" if expires else "indefinitely"
    summary = (
        f"sleep on {persona}: deep-sleep {until} ({record['phase']})"
        f"{' (refreshed)' if refreshed else ''} — owner: {owner} — reason: {args.reason}"
    )
    return OverrideResult(action_slug="sleep", summary=summary, runtime=None, after_landing=after_landing)


def _do_wake_unlocked(args: argparse.Namespace, *, operator: str, root: Path, dry_run: bool) -> OverrideResult:
    persona = args.persona
    record = hs.load_seat_state(persona, root=root)  # raises (path named) if unparseable
    if record is None:
        detail = "no seat-state record (nothing to wake)"
    else:
        ts = args.ledger_ts
        now = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        valid = not seat_state.validate_record(record)
        expired = valid and not seat_state.parse_record(record, now=now).active
        ended_by = "expiry" if expired else f"operator:{operator}"
        if not dry_run:
            if record.get("state") == seat_state.STATE_DEEP_SLEEP:
                _clear_sleep_flag(persona, root)  # flag first: see _do_pause_unlocked
                hs.remove_sleep_marker(persona, root=root)
            hs.append_seat_tombstone(
                persona, {**record, "ended_ts": ts, "ended_by": ended_by}, root=root)
            hs.clear_seat_state(persona, root=root)
        detail = "expired record cleared" if expired else "seat-state record removed"
        if not valid:
            detail += " (it was invalid)"
    summary = f"wake on {persona}: {detail} — reason: {args.reason}"
    return OverrideResult(action_slug="wake", summary=summary, runtime=None)


SEAT_STATE_LOCK_TIMEOUT_S = 30.0


def _locked_seat_state(fn: Callable[..., "OverrideResult"]) -> Callable[..., "OverrideResult"]:
    """ORG-106 L5 (T-5529): serialise pause/wake with the expiry janitor on the per-seat
    lock (blocking, 30s), so a refresh and a janitor write can never interleave."""
    def run(args: argparse.Namespace, *, operator: str, root: Path, dry_run: bool) -> "OverrideResult":
        if dry_run:
            return fn(args, operator=operator, root=root, dry_run=dry_run)
        # SeatStateBusy is a HarnessStateError: main() already turns it into "error" + exit 2.
        try:
            with hs.seat_state_lock(args.persona, root=root, blocking=True,
                                    timeout_s=SEAT_STATE_LOCK_TIMEOUT_S):
                return fn(args, operator=operator, root=root, dry_run=dry_run)
        except SleepFlagError:
            # ORG-106 L3b: page after the lock is released (routing a page can block).
            seat_sleep.maybe_alert_flag_failure(args.persona, root)
            raise
    return run


_do_pause = _locked_seat_state(_do_pause_unlocked)
_do_wake = _locked_seat_state(_do_wake_unlocked)
_do_sleep_locked = _locked_seat_state(_do_sleep_unlocked)


def _do_sleep(args: argparse.Namespace, *, operator: str, root: Path, dry_run: bool) -> OverrideResult:
    _sleep_preflight(args)  # every refusal that needs the manifest, before the lock and any write
    return _do_sleep_locked(args, operator=operator, root=root, dry_run=dry_run)


_DISPATCH = {
    "pause": _do_pause,
    "sleep": _do_sleep,
    "wake": _do_wake,
    "quarantine": _do_quarantine,
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


def ledger_identity_for_operator(operator: str) -> tuple[str, str | None]:
    """The ORG-001 `(agent, executor)` of a harness override run by *operator*
    (an OS account, or a name given with --operator). T-4737, orchestrator ruling B1:

    - a fleet seat (its OS account, persona or role id) writes its ORG-100 role
      id and seat slot (`agent_role_map.ledger_identity`);
    - anyone else is a human at the shell, and the operator is the only human
      operator on this host: `human:operator`, no executor. The operator name
      itself stays in the rationale.
    """
    persona = agent_role_map.account_persona(operator)
    if persona is None:
        candidate = agent_role_map.resolve_persona(operator)
        if candidate in agent_role_map.account_persona_map().values():
            persona = candidate
    if persona is None or persona == "operator":
        return "human:operator", None
    return agent_role_map.ledger_identity(persona)


def _build_ledger_entry(*, persona: str, operator: str, result: OverrideResult, ts: str) -> dict[str, Any]:
    # T-4737: ORG-001 typed links, required rationale + justifies_via, and the
    # role-id agent / seat-slot executor. `summary` stays for the CLI echo.
    links = [{"type": "persona", "ref": persona}]
    if result.runtime:
        links.append({"type": "runtime", "ref": result.runtime})
    links.extend(dict(link) for link in result.extra_links)
    agent, executor = ledger_identity_for_operator(operator)
    entry: dict[str, Any] = {
        "id": _ledger_entry_id(persona=persona, action_slug=result.action_slug, ts=ts),
        "ts": ts,
        "agent": agent,
        "action": "harness_override",
        "rationale": f"{result.summary} (operator: {operator})",
        "justifies_via": ["P4"],
        "summary": result.summary,
        "links": links,
    }
    if executor is not None:
        entry["executor"] = executor
    return entry


def spool_path(state_root: Path) -> Path:
    return state_root / SPOOL_SUBDIR / SPOOL_FILENAME


def _read_spool(state_root: Path) -> list[dict[str, Any]]:
    """Entries spooled because they could not land. An unparseable line is skipped
    (so the next drain rewrite drops it) rather than failing the CLI."""
    try:
        text = spool_path(state_root).read_text(encoding="utf-8")
    except OSError:
        return []
    entries = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            entries.append(value)
    return entries


def _spool_lock(state_root: Path) -> int:
    """Exclusive flock on the spool's sidecar lock file: one drain/append at a time."""
    path = spool_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _append_spool(state_root: Path, entry: dict[str, Any]) -> Path:
    """Append-only, mode 0600: one compact JSON line per unlanded entry."""
    path = spool_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return path


def _rewrite_spool(state_root: Path, entries: Sequence[dict[str, Any]]) -> None:
    """Drain step: atomically replace the spool with what is still unlanded."""
    path = spool_path(state_root)
    if not entries:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".unlanded-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _git_run(args: Sequence[str], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout
    )


def _ledger_entries_by_id(ledger_dir: Path) -> dict[str, str]:
    """id -> canonical JSON of every entry under `ledger_dir` (for idempotent landing)."""
    found: dict[str, str] = {}
    for path in sorted(ledger_dir.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and isinstance(value.get("id"), str):
                found.setdefault(value["id"], json.dumps(value, sort_keys=True))
    return found


@dataclasses.dataclass(frozen=True)
class LandingResult:
    ok: bool
    detail: str
    landed: tuple[str, ...] = ()       # ids on origin/main now (new or already there)
    conflicting: tuple[str, ...] = ()  # ids on origin/main with DIFFERENT content
    rejected: tuple[tuple[str, str], ...] = ()  # (id, why): permanently unwritable entries


_NON_FF_MARKERS = ("rejected", "non-fast-forward", "fetch first")
STALE_LANDING_DIR_SECONDS = 3600
_LANDING_DIR_PREFIX = "harness-ledger-"


def _sweep_stale_landing_dirs(tmp_root: Path | None) -> None:
    """Remove landing temp dirs a crashed run left behind: only our own prefix, only
    dirs this account owns, only older than STALE_LANDING_DIR_SECONDS."""
    base = Path(tmp_root) if tmp_root is not None else Path(tempfile.gettempdir())
    cutoff = datetime.now(timezone.utc).timestamp() - STALE_LANDING_DIR_SECONDS
    try:
        candidates = list(base.glob(f"{_LANDING_DIR_PREFIX}*"))
    except OSError:
        return
    for path in candidates:
        try:
            info = path.lstat()
        except OSError:
            continue
        if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid() or info.st_mtime >= cutoff:
            continue
        shutil.rmtree(path, ignore_errors=True)


def land_ledger_entries(
    entries: Sequence[dict[str, Any]],
    *,
    repo_root: Path = _REPO_ROOT,
    tmp_root: Path | None = None,
) -> LandingResult:
    """Land `entries` on origin/main in ONE commit from a throwaway detached worktree.

    The worktree lives in a fresh temp dir under `tmp_root` (default: the system
    temp dir) and is always removed; stale dirs and worktree records a crashed run
    left are swept first. The clone at `repo_root` only gains git metadata (the
    fetched ref); its working tree is never written. Every git call is bounded by a
    timeout, and a rejected (non-fast-forward) update is retried by rebuilding the
    commit on a fresh origin/main, never by rebasing an append.

    Per entry: one already on origin/main with identical content counts as landed
    (the drain is idempotent); an id already there with different content is
    reported in `conflicting`; one the ledger writer refuses permanently
    (ValueError: a bad ts, an ORG-001 refusal) or the HERMES guard refuses is
    reported in `rejected`. Neither of those blocks the other entries.
    """
    from tools._git_utils import _hermes_scan_file, _hermes_scan_message

    entry_ids = [str(e["id"]) for e in entries]
    message = f"ledger: harness_override {', '.join(entry_ids)}"
    if not _hermes_scan_message(message):
        return LandingResult(False, "HERMES guard refused the commit message")
    _sweep_stale_landing_dirs(tmp_root)
    holder = Path(tempfile.mkdtemp(prefix=_LANDING_DIR_PREFIX, dir=tmp_root))
    worktree = holder / "wt"
    added = False
    try:
        _git_run(["worktree", "prune"], cwd=repo_root, timeout=GIT_LOCAL_TIMEOUT_SECONDS)
        last = "no landing attempt ran"
        for attempt in range(1, LANDING_ATTEMPTS + 1):
            fetch = _git_run(["fetch", "origin", "main"], cwd=repo_root, timeout=GIT_FETCH_TIMEOUT_SECONDS)
            if fetch.returncode != 0:
                return LandingResult(False, f"git fetch origin main failed: {(fetch.stderr or fetch.stdout).strip()}")
            tip = _git_run(["rev-parse", "FETCH_HEAD"], cwd=repo_root, timeout=GIT_LOCAL_TIMEOUT_SECONDS)
            if tip.returncode != 0:
                return LandingResult(False, f"cannot resolve the fetched origin/main: {tip.stderr.strip()}")
            if not added:
                step = _git_run(
                    ["worktree", "add", "--detach", str(worktree), tip.stdout.strip()],
                    cwd=repo_root, timeout=GIT_LOCAL_TIMEOUT_SECONDS,
                )
                added = step.returncode == 0
            else:
                step = _git_run(["reset", "--hard", tip.stdout.strip()], cwd=worktree, timeout=GIT_LOCAL_TIMEOUT_SECONDS)
            if step.returncode != 0:
                return LandingResult(False, f"landing worktree setup failed: {(step.stderr or step.stdout).strip()}")
            ledger = worktree / "ledger"
            on_main = _ledger_entries_by_id(ledger)
            landed: list[str] = []
            conflicting: list[str] = []
            rejected: list[tuple[str, str]] = []
            written: list[str] = []
            for entry in entries:
                canonical = json.dumps(entry, sort_keys=True)
                present = on_main.get(entry["id"])
                if present is not None:
                    (landed if present == canonical else conflicting).append(entry["id"])
                    continue
                if not _hermes_scan_message(canonical):
                    rejected.append((entry["id"], "HERMES guard refused the entry text"))
                    continue
                try:
                    path = append_entry(ledger, entry)
                except ValueError as exc:  # refused before the file is opened
                    rejected.append((entry["id"], f"ledger writer refused: {exc}"[:300]))
                    continue
                clean, why = _hermes_scan_file(path)
                if not clean:
                    return LandingResult(False, f"HERMES guard refused {path.name}: {why}")
                relative = str(path.relative_to(worktree))
                if relative not in written:
                    written.append(relative)
                landed.append(entry["id"])
            outcome = dict(landed=tuple(landed), conflicting=tuple(conflicting), rejected=tuple(rejected))
            if not written:
                return LandingResult(True, "nothing new to write to origin/main", **outcome)
            stage = _git_run(["add", "--", *written], cwd=worktree, timeout=GIT_LOCAL_TIMEOUT_SECONDS)
            commit = _git_run(["commit", "-m", message, "--", *written], cwd=worktree, timeout=GIT_LOCAL_TIMEOUT_SECONDS)
            if stage.returncode != 0 or commit.returncode != 0:
                detail = (commit.stderr or stage.stderr or commit.stdout).strip()
                return LandingResult(False, f"git commit failed: {detail}")
            update = _git_run(["push", "origin", "HEAD:main"], cwd=worktree, timeout=GIT_PUSH_TIMEOUT_SECONDS)
            if update.returncode == 0:
                return LandingResult(True, f"landed on origin/main (attempt {attempt})", **outcome)
            last = (update.stderr or update.stdout).strip()
            if not any(marker in last.lower() for marker in _NON_FF_MARKERS):
                return LandingResult(False, f"origin/main update failed: {last}")
        return LandingResult(False, f"origin/main update failed after {LANDING_ATTEMPTS} attempts: {last}")
    except subprocess.TimeoutExpired as exc:
        return LandingResult(False, f"git timed out after {exc.timeout}s: {' '.join(map(str, exc.cmd))}")
    except (OSError, ValueError) as exc:
        return LandingResult(False, f"{type(exc).__name__}: {exc}")
    finally:
        if added:
            try:
                _git_run(["worktree", "remove", "--force", str(worktree)], cwd=repo_root, timeout=GIT_LOCAL_TIMEOUT_SECONDS)
            except (subprocess.TimeoutExpired, OSError):
                pass
        shutil.rmtree(holder, ignore_errors=True)
        if added:
            try:
                _git_run(["worktree", "prune"], cwd=repo_root, timeout=GIT_LOCAL_TIMEOUT_SECONDS)
            except (subprocess.TimeoutExpired, OSError):
                pass


def quarantine_path(state_root: Path) -> Path:
    return state_root / SPOOL_SUBDIR / QUARANTINE_FILENAME


def _open_quarantine(state_root: Path) -> list[dict[str, Any]]:
    """T-5028 (f): rows not yet resolved by a land/drop tombstone."""
    try:
        text = quarantine_path(state_root).read_text(encoding="utf-8")
    except OSError:
        return []
    return hs.open_quarantine_rows(text.splitlines())


def _orphan_overrides(persona: str, root: Path) -> list[dict[str, Any]]:
    """T-5028 (f+): override rows with no outcome after the grace, from the tail of
    the persona's controller-evidence log (bounded read)."""
    path = _personas_root(root) / persona / "runtime-controller" / "controller-evidence.jsonl"
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 2_000_000))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return hs.orphan_override_rows(text.splitlines(), now=datetime.now(timezone.utc))


def _read_quarantine(state_root: Path) -> list[dict[str, Any]]:
    try:
        text = quarantine_path(state_root).read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _append_quarantine(state_root: Path, entry: dict[str, Any], why: str) -> None:
    """Append-only, mode 0600: an entry that can never land automatically, with why.
    It leaves the spool so it cannot block later landings; a human lands or drops it."""
    path = quarantine_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    row = {"entry": entry, "reason": why, "quarantined_at": _utc_now_ts()}
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _land_with_spool(
    entry: dict[str, Any], *, state_root: Path, repo_root: Path, tmp_root: Path | None
) -> tuple[bool, str]:
    """Land the spooled backlog plus `entry`, per entry.

    A transient failure (network, timeout, git error) spools `entry` with the rest.
    An entry that can never land (rejected by the ledger writer, or its id already on
    origin/main with different content) is moved to the quarantine file, so it never
    blocks a later landing. The spool flock is held only around the spool reads and
    writes, never across the network; the landing itself is idempotent per entry, so
    two overlapping runs cannot double-land. Returns (entry landed, detail)."""
    fd = _spool_lock(state_root)
    try:
        pending = [e for e in _read_spool(state_root) if e.get("id") != entry["id"]]
    finally:
        os.close(fd)
    result = land_ledger_entries([*pending, entry], repo_root=repo_root, tmp_root=tmp_root)
    fd = _spool_lock(state_root)
    try:
        if not result.ok:
            _append_spool(state_root, entry)
            return False, result.detail
        landed = set(result.landed)
        why_by_id = {i: "id already on origin/main with different content" for i in result.conflicting}
        why_by_id.update(dict(result.rejected))
        for item in pending:
            if item["id"] in why_by_id:
                _append_quarantine(state_root, item, why_by_id[item["id"]])
        remaining = [
            e for e in _read_spool(state_root)
            if e["id"] not in landed and e["id"] not in why_by_id and e["id"] != entry["id"]
        ]
        _rewrite_spool(state_root, remaining)
        if entry["id"] in why_by_id:
            _append_quarantine(state_root, entry, why_by_id[entry["id"]])
            return False, f"{entry['id']} quarantined at {quarantine_path(state_root)}: {why_by_id[entry['id']]}"
        detail = result.detail
        quarantined = sorted(i for i in why_by_id if i != entry["id"])
        if quarantined:
            detail += f"; quarantined at {quarantine_path(state_root)}: {', '.join(quarantined)}"
        return True, detail
    finally:
        os.close(fd)


def _expected_seat_account(persona: str) -> str | None:
    """T-5483: the OS account the manifest binds `persona` to, or None when unknown
    or ambiguous (then only the root check applies)."""
    try:
        accounts = sorted(a for a, p in agent_role_map.account_persona_map().items() if p == persona)
    except Exception:  # noqa: BLE001 -- unresolved manifest -> warn-only path
        return None
    return accounts[0] if len(accounts) == 1 else None


def _account_guard(
    args: argparse.Namespace, *, mutating: bool, euid: int, current_user: str, expected: str | None
) -> str | None:
    """T-5483: harness state lives in the SEAT account's home. Run as another user (or
    root, which lands in the wrong HOME) the CLI cannot see or write that state.
    Returns a message starting "refusing" (mutating, real run), a WARN message, or None."""
    form = (
        f"pantheon-sudo -u {expected or '<seat-account>'} env HOME=<seat-home> "
        f"python3 tools/harness.py --operator <you> {args.persona} {args.command} ..."
    )
    problem = None
    if euid == 0:
        problem = "running as root lands in the wrong HOME and cannot see the seat's state"
    elif expected is not None and current_user != expected:
        problem = f"running as {current_user!r} but {args.persona}'s seat account is {expected!r}"
    if problem is None:
        return None
    refuse = mutating and not getattr(args, "dry_run", False)
    prefix = "refusing: " if refuse else "WARN: "
    return f"{prefix}{problem}; run it as the seat account, e.g. {form}"


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    ledger_dir: Path | None = None,
    repo_root: Path | None = None,
    landing_tmp_root: Path | None = None,
    euid_fn: Callable[[], int] | None = None,
    account_fn: Callable[[str], str | None] | None = None,
) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        validate_args(args)
    except (HarnessCliError, hs.HarnessStateError) as exc:
        print(f"harness: error: {exc}", file=sys.stderr)
        return 2

    try:
        state_root = root if root is not None else hs.state_root_from_env()
    except hs.HarnessStateError as exc:  # T-5028 (b): a split holds path refuses loud
        print(f"harness: error: {exc}", file=sys.stderr)
        return 2
    ledger_root = ledger_dir if ledger_dir is not None else LEDGER_DIR
    operator = resolve_operator(args)

    # T-5483: seat-account guard. Production always has root=None (the CLI entry); a
    # caller injecting a temp state root (the unit suite) is not touching a seat's home,
    # so the guard applies only when root is None or a test injects euid_fn/account_fn.
    if root is None or euid_fn is not None or account_fn is not None:
        euid = (euid_fn or os.geteuid)()
        try:
            current_user = pwd.getpwuid(euid).pw_name
        except KeyError:
            current_user = str(euid)
        expected = (account_fn or _expected_seat_account)(args.persona)
        mutating = args.command in MUTATING_COMMANDS or (
            args.command == "quarantine" and getattr(args, "action", None) != "list"
        )
        verdict = _account_guard(args, mutating=mutating, euid=euid, current_user=current_user, expected=expected)
        if verdict is not None:
            print(f"harness: {verdict}", file=sys.stderr)
            if verdict.startswith("refusing"):
                return 2

    if args.command == "status":
        return _do_status(args, root=state_root)
    if args.command == "quarantine" and args.action == "list":
        return _do_quarantine_list(args, root=state_root)

    # Pre-compute the ledger id BEFORE dispatching to the handler (which
    # mutates state and, for `restart`, kills live tmux). `args.command`
    # always equals the eventual OverrideResult.action_slug for every
    # mutating verb (restart/clear-cooldown/hold/clear-hold match their own
    # _DISPATCH key exactly), so this is a safe precomputation, not a
    # duplicate-and-hope guess. On a dry-run there is nothing to collide
    # with (no append happens), so the check only runs for real mutations.
    ts = _utc_now_ts()
    args.ledger_ts = ts  # T-5028: handlers derive linked/correlation ids from it
    slugs = [args.command if args.command != "quarantine" else f"quarantine-{args.action}"]
    if args.command == "restart" and getattr(args, "override_hold", False):
        slugs.append("clear-hold")
    entry_ids = {_ledger_entry_id(persona=args.persona, action_slug=slug, ts=ts) for slug in slugs}
    entry_id = next(iter(entry_ids)) if len(entry_ids) == 1 else None

    if not args.dry_run:
        try:
            collision = bool(entry_ids & existing_entry_ids(ledger_root)) or (
                ledger_dir is None and any(e.get("id") in entry_ids for e in _read_spool(state_root))
            )
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

    del entry_id
    results = [*result.extra_results, result]
    entries = [_build_ledger_entry(persona=args.persona, operator=operator, result=r, ts=ts) for r in results]

    if args.dry_run:
        print("[dry-run] no state files were changed.")
        for entry in entries:
            print("[dry-run] would append ledger entry:")
            print(json.dumps(entry, indent=2, sort_keys=True))
        return 0
    worst = "landed"
    code = 0
    for entry in entries:
        status, entry_code = _record_entry(
            entry, state_root=state_root, ledger_dir=ledger_dir, ledger_root=ledger_root,
            repo_root=repo_root, landing_tmp_root=landing_tmp_root,
        )
        rank = {"landed": 0, "spooled": 1, "failed": 2}
        worst = max(worst, status, key=rank.__getitem__)
        code = max(code, entry_code)
    if result.after_landing is not None:
        try:
            result.after_landing(worst)
        except Exception as exc:  # noqa: BLE001 -- the override already took effect
            print(f"harness: error: post-landing step failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            code = max(code, 2)
    if result.error:
        print(f"harness: error: {result.error}", file=sys.stderr)
        code = max(code, 2)
    return code


def _record_entry(
    entry: dict[str, Any],
    *,
    state_root: Path,
    ledger_dir: Path | None,
    ledger_root: Path,
    repo_root: Path | None,
    landing_tmp_root: Path | None,
) -> tuple[str, int]:
    """Land one entry (the pre-T-5028 tail of main, per entry). Returns
    ("landed" | "spooled" | "failed", exit code)."""
    if ledger_dir is None:
        # T-5021: the production path. Never write the clone we run from (the seat's).
        try:
            landed, detail = _land_with_spool(
                entry,
                state_root=state_root,
                repo_root=repo_root if repo_root is not None else _REPO_ROOT,
                tmp_root=landing_tmp_root,
            )
        except Exception as exc:  # noqa: BLE001 — the override already took effect
            landed, detail = False, f"{type(exc).__name__}: {exc}"
            spooled = True
            try:
                _append_spool(state_root, entry)
            except OSError as spool_exc:
                detail += f"; spooling also failed: {spool_exc}"
                spooled = False
        else:
            spooled = not landed and "quarantined at" not in detail
        print(entry["summary"])
        if not landed:
            print(
                "harness: error: override applied but its ledger entry did not reach origin/main "
                f"({detail}); kept under {spool_path(state_root).parent} (unlanded.jsonl is retried "
                "by the next mutating harness run; quarantine.jsonl needs a human)",
                file=sys.stderr,
            )
            return ("spooled" if spooled else "failed"), 2
        return "landed", 0
    try:
        append_entry(ledger_root, entry)
    except Exception as exc:  # noqa: BLE001 — last-resort guard; see DECISIONS (residual TOCTOU)
        print(f"harness: error: failed to append ledger entry: {exc}", file=sys.stderr)
        return "failed", 2
    print(entry["summary"])
    return "landed", 0


if __name__ == "__main__":
    sys.exit(main())
