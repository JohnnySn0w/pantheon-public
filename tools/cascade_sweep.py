"""tools/cascade_sweep.py -- level-triggered ORG-013 cascade sweep (T-4186).

Run by `tools/systemd/pantheon-cascade-sweep.{service,timer}` (every 15 min, `claude` seat), or by
hand:

    python3 tools/cascade_sweep.py            # reconcile: cascade every stale depends_on entry
    python3 tools/cascade_sweep.py --check    # read-only: list what a sweep would fix

WHY. ORG-013 says a dependant is un-gated when its dependency ships. `ticket.py resolve` does that
on the resolving seat, EDGE-triggered: it only fires for a ticket resolved through that verb. A
ticket that reaches done/ any other way (PR squash-merge, admin merge, hand move, a missed run)
never cascaded, and the Action built to catch those (cascade-on-merge.yml, T-3289) never could:
GitHub's read-only GITHUB_TOKEN cannot push to main and protect-main's bypass actors are a deploy
key and one user. Zero `cascade:` commits ever existed. The leak was real, not theoretical: three
stale deps (T-3381, T-3176, T-3218) sat on main when this was written.

WHAT. The controller-loop fix: reconcile STATE, not events. Every tick, find each inbox/ + active/
ticket whose `depends_on` names a SHIPPED ticket and run `ticket.py cascade --tid <tid>` for it.
A missed run, a lost push, a hand move: the next tick sees the same stale state and fixes it, so
nothing here needs to be exactly-once. In steady state it finds nothing and commits nothing.

REUSE, NOT REIMPLEMENTATION. The write path is `ticket.main(["cascade", "--tid", tid])`: the
existing CLI, so its checkout guard, `git_mutation_lock`, pre-pull, README rebuild, byte-sequence
guard and `push_with_retry` (rebase over a racing push) all apply unchanged. This module holds no
lock itself (`git_mutation_lock` is non-reentrant and each cascade takes it). Detection is a
lock-free, advisory read: `cmd_cascade` recomputes staleness from disk under the lock, so a
detect-then-apply race cannot cause a wrong write, only a wasted no-op. "Satisfied" is
`ticket._dep_is_shipped` (ORG-089: in done/ AND not `substate: dropped`), the same predicate the
cascade, the inbox consumer and every other reader use, so a dropped bet is never stripped.

OUTCOMES (per tid):
    fixed      cascade returned 0 and the tid is no longer stale.
    skipped    a sibling's cascade already pruned it (cascade strips every shipped sibling dep).
    deferred   a busy git_mutation_lock, or the seat checkout is on a named non-main branch.
               Transient by nature; the next tick retries. Exit 0, so a single deferral never
               pages. But a PERMANENT one (T-4220) would be a sweep that never sweeps and never
               pages: consecutive pure-deferral runs are counted in a small state file, and from
               DEFERRAL_ESCALATE_AFTER (8, ~2h at */15) on the run exits 1 with the reason and the
               count, so OnFailure fires. Any run that is not a pure deferral resets the count.
    failed     cascade returned non-zero (pull/push failure, wedged clone), raised, or returned 0
               yet left the tid stale. Exit 1 -> OnFailure -> Recovery's mailbox (6h throttle).
    untouched  beyond --max-tids; left for the next tick.
Each tid is attempted at most once per run, so a detector/cascade disagreement cannot loop.

Source-cleanliness: the byte-sequence trigger is never written contiguously in this file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

if __package__ in (None, ""):  # run as `python3 tools/cascade_sweep.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import ticket  # noqa: E402
from tools._git_utils import GitMutationLockTimeout, pull_repo_or_warn  # noqa: E402

DEFAULT_MAX_TIDS = 25
# A run that only DEFERS (busy git_mutation_lock, or the seat checkout on a named non-main branch)
# exits 0: transient by nature, so it must not page. This many CONSECUTIVE such runs is not
# transient any more -- 8 is about 2h at the */15 cadence -- so `main()` exits 1 from then on and
# the unit's OnFailure alert fires. `--escalate-after` overrides it.
DEFERRAL_ESCALATE_AFTER = 8
_DEFERRAL_STATE_NAME = "cascade_sweep_deferrals.json"


@dataclass
class SweepResult:
    stale: dict[str, list[str]] = field(default_factory=dict)  # detected at the start of the run
    fixed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    untouched: list[str] = field(default_factory=list)
    deferred_reason: str = ""

    @property
    def exit_code(self) -> int:
        """1 only for a genuine failure to converge; deferral and the cap are not faults."""
        return 1 if self.failed else 0

    @property
    def is_deferral(self) -> bool:
        """True when the run's ONLY non-idle outcome was a deferral (T-4220).

        A run that fixed something proves the sweep is not stuck (a lock that was free for the
        first tid and busy for the next is contention), and a run that failed already pages via
        exit 1, so neither counts toward a persistent deferral."""
        return bool(self.deferred) and not self.fixed and not self.failed


def _tid_key(tid: str) -> int:
    return int(tid.split("-", 1)[1])


def find_stale() -> dict[str, list[str]]:
    """{shipped tid -> sorted dependant ids} over inbox/ + active/, ordered by tid number.

    Mirrors `ticket._find_dependants` (same directories, same filename filter, malformed
    tickets skipped) and uses `ticket._dep_is_shipped` as the one "satisfied" predicate.
    Reads `ticket.TICKETS_ROOT` at call time.
    """
    stale: dict[str, list[str]] = {}
    for state in ("inbox", "active"):
        directory = ticket.TICKETS_ROOT / state
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_file() or not ticket.TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            try:
                parsed = ticket.parse_ticket(entry)
            except ticket.TicketError:
                continue  # malformed: grooming surfaces those; never fatal to the sweep
            deps = parsed.fields.get("depends_on")
            if not isinstance(deps, list):
                continue
            for dep in deps:
                if isinstance(dep, str) and ticket._dep_is_shipped(dep):
                    stale.setdefault(dep, []).append(entry.stem)
    return {tid: sorted(set(stale[tid])) for tid in sorted(stale, key=_tid_key)}


def default_cascade(tid: str) -> int:
    """The existing CLI, in-process: checkout guard + lock + pull + commit + push-with-retry."""
    return ticket.main(["cascade", "--tid", tid])


def _sync_checkout() -> None:
    """Best-effort `fetch` + ff-merge of the seat checkout, so detection sees origin's done/."""
    pull_repo_or_warn("cascade_sweep")


def _current_branch() -> str:
    return ticket._current_branch(ticket.REPO_ROOT)


def _log(message: str) -> None:
    print(f"cascade_sweep: {message}", flush=True)


# -- consecutive-deferral counter (T-4220) --------------------------------------------------------
#
# State: `{"count": N, "reason": "..."}` in PANTHEON_STATE_DIR (default ~/.local/state/pantheon,
# resolved at call time like the sibling tools). No file means a count of 0, so a steady-state
# tick writes nothing. The counter is ADVISORY and fails OPEN, in two layers, so that no fault in
# it can change the run's exit code -- a broken counter can cost a delayed escalation but can never
# turn every run red:
#   1. LOCAL handlers for the faults a bad or missing state file can cause (I/O, undecodable bytes,
#      malformed or pathologically nested JSON, wrong shape): each logs one line and degrades to a
#      count of 0 (read) or carries on (write / reset).
#   2. `main()` wraps the whole counter call in a catch-all as the BACKSTOP, which covers what layer
#      1 does not enumerate (e.g. `Path.home()` raising when the state path is resolved).
# Everything is logged to stdout (-> the unit's journal). The one blind spot this buys: a counter
# that can never persist never escalates, and only the log line shows it.
# One timer-driven process at a time, so there is no lock; a manual run racing a tick is off by one.

def deferral_state_path() -> Path:
    """The state file. May raise (`Path.home()` can, with no resolvable HOME): the caller in
    `main()` treats any counter failure as advisory."""
    env = os.environ.get("PANTHEON_STATE_DIR")
    base = Path(env) if env else Path.home() / ".local" / "state" / "pantheon"
    return base / _DEFERRAL_STATE_NAME


def _read_deferral_count(path: Path) -> int:
    """The stored count, or 0 when there is none or it cannot be used (logged unless the file is
    simply absent). Handles every fault a bad file can cause: I/O errors, undecodable bytes
    (UnicodeDecodeError is a ValueError), malformed JSON, deep nesting (RecursionError), and a
    payload of the wrong shape."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0  # the normal "not deferring" state, not a fault
    except (OSError, ValueError) as exc:  # ValueError: UnicodeDecodeError on non-UTF-8 bytes
        _log(f"deferral counter unreadable ({path}: {type(exc).__name__}: {exc}); counting from 0 - "
             f"escalation may be delayed, run outcome unaffected")
        return 0
    try:
        count = json.loads(text)["count"]
        if type(count) is not int or count < 0:
            raise ValueError(f"count must be a non-negative integer, got {count!r}")
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        _log(f"deferral counter unreadable ({path}: {type(exc).__name__}: {exc}); counting from 0 - "
             f"escalation may be delayed, run outcome unaffected")
        return 0
    return count


def _write_deferral_state(path: Path, count: int, reason: str) -> None:
    """Atomically replace the state file (same-dir temp + fsync + os.replace). An I/O or encoding
    fault is logged and swallowed, and the temp file is cleaned up."""
    temporary: Optional[Path] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"count": count, "reason": reason}, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except (OSError, ValueError) as exc:
        _log(f"deferral counter not persisted ({path}: {exc}); this run's count is {count}, "
             f"run outcome unaffected")
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _reset_deferral_state(path: Path) -> None:
    """Drop the counter (no file == 0). Missing is the common case and silent; an I/O fault is
    logged and swallowed. It never reads the file, so a corrupt one cannot obstruct a reset."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log(f"deferral counter not reset ({path}: {exc}); run outcome unaffected")


def note_deferral_outcome(result: SweepResult, *, path: Optional[Path] = None) -> int:
    """Fold this run into the consecutive-deferral counter; return the count AFTER this run.

    A pure deferral (`SweepResult.is_deferral`) adds one; any other outcome resets to 0. The
    returned count is the in-memory truth even when it could not be persisted. The helpers absorb
    the file faults listed above; anything else (e.g. `deferral_state_path()` raising) propagates,
    and `main()` is what makes that harmless."""
    path = deferral_state_path() if path is None else path
    if not result.is_deferral:
        _reset_deferral_state(path)
        return 0
    count = _read_deferral_count(path) + 1
    _write_deferral_state(path, count, result.deferred_reason)
    return count


def sweep(
    *,
    max_tids: int = DEFAULT_MAX_TIDS,
    cascade: Callable[[str], int] = default_cascade,
    sync: Optional[Callable[[], None]] = None,
    current_branch: Optional[Callable[[], str]] = None,
) -> SweepResult:
    """One reconcile pass. Never raises for an operational fault: it is reported in the result."""
    sync = _sync_checkout if sync is None else sync
    current_branch = _current_branch if current_branch is None else current_branch
    result = SweepResult()

    try:
        sync()
    except Exception as exc:  # noqa: BLE001 -- stale-but-running beats halted (pull is fail-open)
        _log(f"pre-sweep sync failed ({type(exc).__name__}: {exc}); continuing on the local view")

    result.stale = find_stale()
    todo = list(result.stale)
    if not todo:
        return result

    try:
        branch = current_branch()
    except Exception:  # noqa: BLE001 -- can't tell: let the cascade's own guard speak
        branch = ""
    if branch not in ("", "main"):
        result.deferred = todo
        result.deferred_reason = (
            f"seat checkout is on {branch!r}, not main (ticket.py refuses to commit from there)"
        )
        return result

    attempts = todo[:max_tids]
    result.untouched = todo[max_tids:]
    for index, tid in enumerate(attempts):
        if tid not in find_stale():  # a sibling's cascade already pruned it
            result.skipped.append(tid)
            continue
        try:
            rc = cascade(tid)
        except GitMutationLockTimeout:
            # The lock is per checkout: if one cascade could not get it, the rest cannot either.
            result.deferred = attempts[index:]
            result.deferred_reason = "git_mutation_lock busy (another ticket writer is mid-mutation)"
            return result
        except Exception as exc:  # noqa: BLE001 -- one tid's crash must not hide the others
            result.failed.append((tid, f"{type(exc).__name__}: {exc}"))
            continue
        if rc != 0:
            result.failed.append((tid, f"cascade exited rc={rc}"))
        elif tid in find_stale():
            result.failed.append((tid, "cascade exited rc=0 but the tid is still stale"))
        else:
            result.fixed.append(tid)
    return result


def _summarise(result: SweepResult) -> list[str]:
    lines = [
        f"{len(result.stale)} stale tid(s): fixed={len(result.fixed)} skipped={len(result.skipped)} "
        f"deferred={len(result.deferred)} failed={len(result.failed)} untouched={len(result.untouched)}"
    ]
    if result.fixed:
        lines.append(f"fixed: {', '.join(result.fixed)}")
    if result.skipped:
        lines.append(f"skipped (cleared as a sibling side effect): {', '.join(result.skipped)}")
    if result.deferred:
        lines.append(f"deferred to next tick ({result.deferred_reason}): {', '.join(result.deferred)}")
    for tid, why in result.failed:
        lines.append(f"FAILED {tid}: {why}")
    if result.untouched:
        lines.append(f"over the cap, left for the next tick: {', '.join(result.untouched)}")
    return lines


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Level-triggered ORG-013 cascade sweep: cascade every stale depends_on entry (T-4186)."
    )
    parser.add_argument("--check", action="store_true",
                        help="read-only: list the stale entries a sweep would fix; never pulls or writes")
    parser.add_argument("--max-tids", type=_positive_int, default=DEFAULT_MAX_TIDS,
                        help=f"cascade at most N tids per run (default {DEFAULT_MAX_TIDS})")
    parser.add_argument("--escalate-after", type=_positive_int, default=DEFERRAL_ESCALATE_AFTER,
                        help="exit 1 (so OnFailure pages) once this many CONSECUTIVE runs did nothing but "
                             f"defer (default {DEFERRAL_ESCALATE_AFTER}, about 2h at the */15 cadence)")
    args = parser.parse_args(argv)

    if args.check:
        stale = find_stale()
        for tid, dependants in stale.items():
            print(f"stale: {tid} -> {', '.join(dependants)}")
        count = len({d for deps in stale.values() for d in deps})
        _log(f"{len(stale)} stale tid(s), {count} dependant(s)")
        return 0

    result = sweep(max_tids=args.max_tids)
    for line in _summarise(result):
        _log(line)
    exit_code = result.exit_code
    try:
        consecutive = note_deferral_outcome(result)
    except Exception as exc:  # noqa: BLE001 -- the counter is advisory: nothing in it may change the exit code
        _log(f"deferral counter error ({type(exc).__name__}: {exc}); run outcome unaffected")
        consecutive = 0
    if consecutive >= args.escalate_after:
        _log(f"ESCALATING: {consecutive} consecutive deferred runs (threshold {args.escalate_after}); "
             f"reason: {result.deferred_reason}; waiting: {', '.join(result.deferred)}. Exiting 1 so "
             f"OnFailure pages; the count resets on the first run that is not a pure deferral.")
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
