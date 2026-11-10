#!/usr/bin/env python3
"""mail_staleness_monitor — the impure shell around `mail_staleness.detect()` (T-2157).

Topology (settled 2026-07-28): CENTRALIZED + RECOVERY-HOSTED. orchestrator owns the pure
`detect()` in `mail_staleness.py`; this module owns everything with side effects —
inbox enumeration, cross-account pane capture, state persistence, and delivery.

READ-ONLY BY CONTRACT. It observes and it alerts. It MUST NOT kick, inject, consume,
ack, or move a message. That is the operator's remediation disarm (TG 6520, verified in the
ledger), not a scoping preference: a detector that can also remediate will eventually
remediate on a false positive, and this one's whole design problem is that its
false-positive surface is a healthy busy agent. `test_monitor_never_kicks` greps this
file's own source for the injection vocabulary so the constraint is enforced rather
than merely documented.

INBOXES ARE ENUMERATED FROM THE FILESYSTEM, NEVER FROM A PERSONA LIST. A maildir is
any hub subdir carrying new/ cur/ tmp/. This is a direct correction: my own T-2159
probe built its box list from agent persona NAMES and silently missed
`<hub>/specialist-a2a` — a real inbox (specialist_seat_server.py's DEFAULT_RESULT_INBOX) that is
not a persona. A monitor that enumerates by persona would have the same blind spot, and
would report all-clear over an incomplete population.

TIERS. The budgets are PER CASE, not fleet-wide — an earlier "max wake latency is 5
minutes fleet-wide" framing was wrong, and both orchestrator and I had to correct it (orchestrator by
re-reading decide() instead of the timer; me by re-deriving rather than accepting the
corrected number). The two tiers have DIFFERENT floors because condition 5 is reachable
for one and structurally unreachable for the other.

  WARN tier — pane_idle True only. An idle pane is BY DEFINITION not mid-turn, so the
  turn-in-flight term does not belong in this budget at all:
        <= 300s wake (condition 5, one timer period)  +  ~180s drain  =  ~8 min
    WARN at 15 min (3 samples @ 300s) — ~2x the floor.
    NOT a contradiction that a True-pane agent whose idle-kick was ALSO swallowed sits
    until condition 4's 1800s TTL and therefore trips this tier: idle + unread + already
    kicked IS the fault this detector exists for. Warning at 15 min there is the point,
    not a false positive.

  COARSE tier — pane_idle None (codex's TUI, archivist's absent pane). Condition 5 is
  UNREACHABLE here by my own contract ("None must never wake"), so these agents are
  bounded by the TTL backstop exactly as they were before T-2158:
        <= 300s   first wake (condition 1, "newer message")   -> marker ts
      +  1800s    REKICK_TTL before condition 4 becomes true
      +  <= 300s  the NEXT SWEEP, since condition 4 is only evaluated when the timer
                  fires — the term orchestrator and I both dropped on our first passes
      +  ~180s    drain
      =  ~43 min legitimate worst case
    COARSE at 60 min (12 samples @ 300s) — ~17 min of margin.

    It was 45 min (9 samples) until orchestrator caught that my "5 min fleet-wide" reading left
    almost no headroom. Re-deriving independently gave ~43 min rather than orchestrator's ~38,
    so the case for moving it was STRONGER than the correction claimed: 45 would have
    left ~2 minutes, i.e. less than one sample interval, and the first live alert would
    likely have been a false one.

`pane_idle` IS A SUPPRESSOR, NEVER A TRIGGER. The dangerous state is not None, it is
False: a generating agent SHOULD have mail waiting, and a busy agent is exactly what an
age-only threshold fires on hardest — so an age-only detector fires most on the
healthiest agents and gets itself ignored. Hence:
    True  (idle)       -> evidence accumulates; alert at the WARN tier.
    False (generating) -> suppress. Legitimately busy. The one bound is T-4323's
                          busy-stale backstop: unread older than 2h alerts anyway,
                          since a pane MISREAD as busy is otherwise never reported.
    None  (unknown)    -> do NOT suppress and do NOT escalate: fall through to the
                          COARSE tier only.

That last line is orchestrator's deviation from my own "None must never drive an action" rule,
and I am adopting it deliberately rather than by omission. My rule was written for the
WAKER, where acting on unknown means injecting into a pane that might be mid-turn. Here
the action is a read-only alert, and the cost asymmetry inverts: suppressing on unknown
would silently drop codex and archivist — the two unreadable-pane substrates — out of
coverage entirely, and they are two of the three agents the originating incident was
about. Unknown + 45 minutes of unmoved mail is a real signal whatever the pane would
have said. The alert text says the pane was UNREADABLE rather than idle, so the claim
never outruns the evidence.

Implemented by calling the pure `detect()` TWICE rather than by changing it: once over
every inbox at the warn threshold, and once over only the unknown-pane inboxes with
`pane_idle` coerced to True at the coarse threshold. The coercion is a CALLER POLICY
and lives here, where it can be read as one; `detect()` stays the semantics of record
for "idle + unread accumulates". Each tier keeps its own state so their alert cooldowns
cannot collide, and the coarse tier suppresses `never_received` (the warn tier already
owns it) so a wiring defect is reported once, not twice.

Usage:
    python3 tools/mail_staleness_monitor.py --dry-run    # observe + report, no delivery
    python3 tools/mail_staleness_monitor.py              # one sample, deliver alerts
    python3 tools/mail_staleness_monitor.py --json       # machine-readable observation
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_staleness as ms  # noqa: E402

# The module itself, for the T-2267 sender notices (delivery, not remediation —
# this file already delivers alerts; what it must never do is drain or ack).
import mailbox  # noqa: E402

# Imported, not redeclared: the on-disk layout of a claim is mailbox.py's fact to
# own. A second copy of this name is how the two /proc/locks parsers drifted into
# four bugs in one night (T-2304) — a shared constant with two owners has none.
from mailbox import _INFLIGHT_DIR, _CLAIM_OWNER_LOCK  # noqa: E402

# Lease/UNKNOWN are imported THROUGH mailbox deliberately (T-2304). mailbox.py
# already owns the tools._lease-vs-_lease resolution, and importing them from
# there guarantees this module gets the SAME module object — and therefore the
# same UNKNOWN sentinel. Importing `_lease` directly here would work right up
# until one module resolved as `tools._lease` and the other as `_lease`, at
# which point `holder is UNKNOWN` silently compares two distinct singletons and
# every unknown reads as a definite answer. That is the double-import trap, and
# it would land in the one predicate we consolidated to stop exactly this.
from mailbox import Lease, UNKNOWN  # noqa: E402

try:  # T-4096: the hub default comes from instance.yaml, never a literal.
    from tools.agent_role_map import mailbox_hub  # noqa: E402
except ImportError:  # script run: tools/ is on sys.path, the repo root is not
    from agent_role_map import mailbox_hub  # type: ignore  # noqa: E402

MAILBOX_HUB = Path(os.environ.get("PANTHEON_MAILBOX_HUB") or mailbox_hub())
STATE_PATH = Path.home() / ".local" / "state" / "pantheon" / "mail_staleness_monitor.json"

# The installed timer's OnUnitActiveSec is the SURFACE THAT DECIDES the real sample
# cadence; this constant only MIRRORS it. If they diverge, every threshold derived from
# this number is validated against a fiction and the tier guard still passes green —
# which is the same "read the constant, not the loop that consults it" error the tier
# guard exists to catch, one layer up. Both REPO_TIMER_UNIT and INSTALLED_TIMER_UNIT
# below are checked against this — and against each other — by
# test_sample_interval_matches_the_timer_units as soon as either unit exists.
SAMPLE_INTERVAL_SECONDS = 300
# TWO timer files, and the INSTALLED one is what actually decides (orchestrator, extending the
# same chain one more hop). The repo copy is itself only a proxy: installed units are
# `cp`s, NOT symlinks, so a repo edit is INERT for a running unit and a clean repo with
# green tests can coexist with a fleet still on the old cadence. That is my own 13:40Z
# finding from the T-2156 blast-radius sweep (plan §4b(2)), and I reproduced the error it
# describes one layer down while fixing the layer above it: constant -> repo timer ->
# installed timer. The check below compares BOTH against this constant and against each
# other, so "someone edited the repo and forgot to re-provision" fails too — which is the
# direction the drift has actually run in practice.
REPO_TIMER_UNIT = Path(__file__).resolve().parent / "systemd" / "pantheon-mail-staleness.timer"
INSTALLED_TIMER_UNIT = Path.home() / ".config" / "systemd" / "user" / "pantheon-mail-staleness.timer"
DEPLOYMENT_TICKET = "T-2200"  # neither exists yet; install is gated on the rename
WARN_SAMPLES = 3               # 15 min
COARSE_SAMPLES = 12            # 60 min — see the COARSE tier derivation above
REALERT_AFTER_SAMPLES = 12     # ~1h between repeats for a persisting fault

_ALERT_VOCAB = "mail_staleness"

# T-2237(b): the severity floor. Kept as a SET rather than an ordered scale
# because ORG-026 defines exactly two levels ({normal, urgent}) and inventing an
# ordering now would be designing for a hierarchy nobody has asked for — the same
# premature-generality trap that left `severity` itself inert for months.
_URGENT_SEVERITIES = frozenset({"urgent"})
_KIND_URGENT_UNDELIVERED = "urgent_undelivered"

# Fallback names ONLY (strategy 3 above) — never the primary source of truth, because
# a list of names cannot contain the box nobody named. specialist-a2a is here precisely
# because it is not a persona and my T-2159 probe missed it for that reason.
# T-6165: the hand-kept names stay as a floor (specialist-a2a is not a persona) and every
# registry persona is unioned in, so a new seat cannot be missed the way watchman was.
CANDIDATE_INBOXES = ["recovery", "orchestrator", "specialist", "archivist", "codex", "mason", "artisan",
                     "specialist-a2a"]


def _candidate_inboxes() -> list[str]:
    try:
        try:
            from tools.agent_role_map import account_persona_map
        except ImportError:
            from agent_role_map import account_persona_map  # type: ignore
        personas = set(account_persona_map().values())
    except Exception:  # the registry is a widening, never a dependency of the fallback
        personas = set()
    return sorted(set(CANDIDATE_INBOXES) | personas)


# ---------------------------------------------------------------- enumeration
def _hub_subdir_count(hub: Path) -> int | None:
    """True number of subdirectories of `hub`, WITHOUT listing it.

    A directory's st_nlink is 2 + its subdirectory count ("." + ".." + one ".." per
    child). Recovery can stat the hub but cannot list it (mode 0710, owner post-office:
    the mailbox group gets --x, i.e. traverse only), so this is the one completeness
    oracle available without borrowing another account's eyes. None if even stat fails.
    """
    try:
        return hub.stat().st_nlink - 2
    except OSError:
        return None


def discover_inboxes(hub: Path = MAILBOX_HUB) -> tuple[list[str], str | None]:
    """Every maildir under `hub`, by SHAPE (new/ cur/ tmp/). Returns (names, warning).

    THREE STRATEGIES, and the third is the one that makes this trustworthy:

    1. Direct listing. Fails for Recovery — the hub is 0710 and only its owner can read it.
    2. Borrow the owner's eyes: `sudo -u <hub owner> ls`. This works and is the normal
       path. (My first attempt at this used bare `sudo`, i.e. sudo-to-ROOT, which Recovery
       does not have — and I nearly recorded "the hub cannot be enumerated" off that
       one failed command. Sudo-to-a-different-user and sudo-to-root are separate
       grants; a denial of one says nothing about the other.)
    3. Probe a candidate list by stat. Traverse works even when listing does not, so
       existence is always checkable ONCE YOU KNOW THE NAME. This is the fallback, and
       it is the strategy with the blind spot: a box nobody thought to name is
       invisible to it.

    WHICHEVER STRATEGY RAN, THE RESULT IS CHECKED AGAINST st_nlink. If we found fewer
    boxes than the hub actually has, the shortfall is RETURNED AS A WARNING and must be
    surfaced — never swallowed. That guard is the entire reason this function returns a
    tuple. The first version of it returned [] on a permission error, which made the
    monitor print "0 alert(s)" over ZERO inboxes and look healthy: failure presenting
    as absence, where absence is also what healthy looks like (the exact lesson
    promoted to lessons/ this morning). A monitor that cannot see its population must
    say so, loudly, rather than report all-clear over an empty set.
    """
    def _shaped(names):
        return sorted(n for n in names
                      if not n.startswith(".")
                      and all((hub / n / sub).is_dir() for sub in ("new", "cur", "tmp")))

    found: list[str] = []
    try:
        found = _shaped(p.name for p in hub.iterdir())
    except OSError:
        pass

    if not found:
        try:
            owner = Path(hub).parent.name  # /home/<owner>/mailbox
            r = subprocess.run(["sudo", "-n", "-u", owner, "ls", "-A", str(hub)],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                found = _shaped(r.stdout.split())
        except Exception:
            pass

    if not found:
        # Resolve the registry only when the two authoritative enumeration paths
        # fail.  Besides avoiding an import-time registry snapshot, this preserves
        # the intended failure boundary: registry trouble drops only the widening.
        found = _shaped(_candidate_inboxes())

    expected = _hub_subdir_count(hub)
    warning = None
    if expected is None:
        warning = f"cannot stat {hub} — inbox population is UNVERIFIED"
    elif len(found) < expected:
        warning = (f"enumerated {len(found)} inbox(es) but {hub} has {expected} "
                   f"subdirectories — {expected - len(found)} NOT being monitored; "
                   f"this monitor's coverage is INCOMPLETE, not clean")
    return found, warning


def _count(d: Path) -> int | None:
    """Files in `d`, or None if it cannot be READ (T-2310, specialist).

    Returning 0 on OSError conflated "empty" with "unreadable", and those are
    different facts that `detect` acts on oppositely: 0/0 makes
    ever_received=False, which is the never-RECEIVED wiring defect — an alert
    whose text says "a kick cannot fix it" about mail sitting right there, and
    which LATCHES, so it becomes the last thing that inbox ever emits.

    None is the same tristate the module already uses for pane_idle and
    _flock_held, applied one level down.
    """
    try:
        return sum(1 for p in d.iterdir() if p.is_file() and not p.name.startswith("."))
    except OSError:
        return None


def _max_age(*ages) -> float | None:
    """Oldest of several ages, ignoring None (nothing there), None if all are."""
    present = [a for a in ages if a is not None]
    return max(present) if present else None


def _claimed(box: Path, now: float) -> tuple:
    """(count, oldest_age_s) over every message held in an in-flight claim, or
    (None, None) if that set could not be read COMPLETELY.

    T-2287 introduced a third resting place for an unacked message. Under the
    claim step a consumer renames new/ -> .inflight/<claim>/ BEFORE running its
    handler, so for the whole duration of a turn the messages being worked are in
    neither new/ nor cur/. Counting only new/ would therefore report a busy
    mailbox as EMPTY for exactly as long as the turn lasts — and "empty" is the
    reading that feeds ever_received=False and the never-received wiring alert.
    That is the same failure-presents-as-absence defect as T-2310, reintroduced
    by a transport change rather than by an error handler.

    A claimed message is still unacked work: it has been delivered to nobody's
    satisfaction until it reaches cur/. So it counts as unread, and its age keeps
    accruing — which is what makes a consumer WEDGED INSIDE a handler visible at
    all, rather than looking like a drained queue.

    Incomplete reads return None rather than a partial count, for the reason this
    module already applies elsewhere: a short count is indistinguishable from
    real absence, and absence is the dangerous direction here."""
    root = box / _INFLIGHT_DIR
    if not root.exists():
        return 0, None
    count, ages = 0, []
    try:
        claim_dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return None, None
    for d in claim_dirs:
        try:
            for p in d.iterdir():
                if not (p.is_file() and not p.name.startswith(".")):
                    continue
                count += 1
                try:
                    ages.append(now - p.stat().st_mtime)
                except OSError:
                    continue
        except OSError:
            return None, None
    return count, (max(ages) if ages else None)


def _oldest_age(d: Path, now: float) -> float | None:
    ages = []
    try:
        for p in d.iterdir():
            if p.is_file() and not p.name.startswith("."):
                try:
                    ages.append(now - p.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        return None
    return max(ages) if ages else None


# ------------------------------------------------------------- pane capture
def _persona_panes() -> dict[str, tuple[str, str]]:
    """inbox name -> (unix account, tmux pane target), for inboxes that HAVE a pane.

    Derived from mailbox_wake.AGENT_PANE so the substrate map has one owner. Inboxes
    with no entry (specialist-a2a, and any future non-persona box) simply have no pane, and
    their pane state is UNKNOWN by construction rather than by failure.
    """
    try:
        import mailbox_wake as mw
        return {
            name: (Path(cfg["home"]).name, cfg["pane_target"])
            for name, cfg in mw.AGENT_PANE.items()
        }
    except Exception:
        return {}


def capture_pane_idle(inbox: str, panes: dict[str, tuple[str, str]] | None = None,
                      timeout: int = 10) -> bool | None:
    """THREE-state pane read for `inbox`, cross-account. None = UNKNOWN.

    Recovery runs this monitor but the panes belong to other accounts, so the capture goes
    through `sudo -u <account>`. Classification is DELEGATED to
    `inbox_consumer.pane_idle_state` — the T-0716 sentinels and their precedence rule
    have one owner, and a second copy is the drift shape T-2171 had to fix in
    keep_going. Any failure at any step is UNKNOWN, never a guess.
    """
    panes = _persona_panes() if panes is None else panes
    entry = panes.get(inbox)
    if entry is None:
        return None  # not a persona box — no pane exists to read
    account, target = entry
    if not target or not target.strip():
        return None
    try:
        # -e: pane_idle_state needs the DIM attribute to tell a prompt suggestion
        # from typed text (T-4284).
        if account == os.environ.get("USER", ""):
            cmd = ["tmux", "capture-pane", "-p", "-e", "-t", target]
        else:
            cmd = ["sudo", "-n", "-u", account, "bash", "-c",
                   f'XDG_RUNTIME_DIR=/run/user/$(id -u) tmux capture-pane -p -e -t {target}']
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return None
        from inbox_consumer import pane_idle_state
        return pane_idle_state(out.stdout)
    except Exception:
        return None


# -------------------------------------------------------------- observation
def observe(hub: Path = MAILBOX_HUB, now: float | None = None,
            pane_fn=capture_pane_idle) -> dict[str, ms.InboxObservation]:
    """One sample of every discovered inbox. Read-only: stats files, moves nothing."""
    now = time.time() if now is None else now
    panes = _persona_panes()
    obs: dict[str, ms.InboxObservation] = {}
    names, _warning = discover_inboxes(hub)
    for name in names:
        box = hub / name
        queued = _count(box / "new")
        cur = _count(box / "cur")
        # T-2287: messages claimed into .inflight/ are unacked work too — see
        # _claimed(). Without this an inbox reads EMPTY for the length of every
        # turn, which is the never-received verdict all over again.
        claimed, claimed_oldest = _claimed(box, now)
        # T-2310: an inbox we cannot read is UNREADABLE, not empty. Reporting
        # zeroes here is what manufactured the never-received verdict.
        readable = queued is not None and cur is not None and claimed is not None
        unread = None if queued is None or claimed is None else queued + claimed
        queued_oldest = _oldest_age(box / "new", now)
        obs[name] = ms.InboxObservation(
            unread=unread or 0,
            in_flight=_count(box / "tmp") or 0,
            oldest_unread_age_s=_max_age(queued_oldest, claimed_oldest),
            pane_idle=pane_fn(name, panes),
            readable=readable,
            # "ever received" = anything has ever been acked into cur/, or something
            # is pending now. A box that has neither has never carried traffic, which
            # is a WIRING defect and not staleness (codex, T-2142/T-2144).
            # Only meaningful when readable; detect() gates on `readable` first.
            ever_received=bool(cur or unread),
            consumer_lease_held=_consumer_lease_held(box),
        )
    return obs


def _consumer_lease_held(box: Path) -> bool:
    """Is this inbox's single-consumer lease held by a LIVE consumer? (T-2283)

    While it is, the agent CANNOT ack its own mail — the drain API takes this
    lease for the WHOLE drain, and the ACP bridge's handler blocks on a full
    agent turn inside it. So depth in new/ is evidence about turn length, not
    about the transport, and the detector must not accumulate on it.

    (Phrased without naming that API's function literally: this file's own
    read-only contract test greps its source for the injection/ack vocabulary,
    and a mention in a comment is indistinguishable from a call to a grep. The
    guard is right to be that blunt — it enforces the operator's remediation disarm
    — so the comment bends, not the guard.)

    LIVENESS IS CHECKED, not just presence. A stale lockfile from a crashed
    consumer must NOT suppress detection for its full TTL (7200s on specialist) —
    that would convert this fix into a two-hour blind spot, which is a worse
    failure than the false alerts it exists to stop. A dead pid means the lease
    is not really held and the inbox really is stuck: alert.

    Fails OPEN (returns False = "not held" = detection proceeds as before) on
    any unreadable/garbled lock, because the safe direction for a DETECTOR is
    to keep detecting. Suppression must be affirmatively justified.

    ⚠️ T-2287 MOVED THE SIGNAL AND THIS FUNCTION HAD TO FOLLOW. The paragraph
    above is now historical: the consumer lease is NO LONGER held for the whole
    drain — it covers only the claim step, and is released before the handler
    runs. So the consumer lock alone answers "is a claim in progress right now",
    a sub-millisecond window, and would read False for the entire turn. Left
    unchanged it would have silently reverted T-2283: a live consumer mid-turn
    would once again be indistinguishable from an inbox nobody is serving.

    The signal did not disappear, it MOVED — and to a better place. Each claim
    holds an owner flock for exactly the life of the handler, which is more
    precisely "somebody is working this queue" than the old lease ever was (that
    one was also held during the reap/prune/claim bookkeeping). So the question
    is now asked of both locks, and the answer is the OR.

    Caught by test_mailbox_system's KnownGapsTest, which pinned the discriminator
    T-2283 established. A unit suite would not have: this only fails when a real
    consumer is genuinely mid-turn.
    """
    if _flock_held(box / ".consumer.lock") is True:
        return True
    return _live_claim_held(box)


def _live_claim_held(box: Path) -> bool:
    """Does any in-flight claim have a LIVE owner? (T-2287)

    Same fail-open direction as everything else in this detector: an unreadable
    .inflight/ or an unanswerable lock yields False, so detection PROCEEDS. Note
    this is the opposite of the direction mailbox._claim_is_abandoned takes on
    the identical question, and the asymmetry is deliberate rather than an
    inconsistency — that one decides whether to REQUEUE, where a wrong answer
    duplicates a message, so it must fail closed. This one decides whether to
    SUPPRESS AN ALERT, where failing closed would suppress on ignorance. Same
    predicate, opposite safe directions, because the consequences differ. That
    is precisely the constraint specialist attached to T-2304: a shared predicate must
    hand the caller a distinguishable unknown rather than pick one default."""
    root = box / _INFLIGHT_DIR
    try:
        claim_dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return False
    return any(_flock_held(d / _CLAIM_OWNER_LOCK) is True for d in claim_dirs)


def _flock_held(lock: Path) -> bool | None:
    """Is an advisory flock ACTUALLY held on this file's inode? (T-2291)

    True = held, False = not held, None = cannot tell.

    ⚠️ THIS IS NOW A THIN ADAPTER OVER _lease.Lease.flock_holder_pid (T-2304).
    It used to be a SECOND independent parse of /proc/locks, and the case for
    collapsing it was empirical rather than aesthetic: this one predicate
    produced FOUR shipped bugs in a single night — pid-existence standing in for
    lock-held (T-2291), an inode matched without its device (T-2296), a
    hex-vs-decimal parse pinned only by an assertion that could not fail, and a
    fixture hardcoding the authoring host's device (d4437acad). Every one of
    those is a detail a second copy can get wrong on its own, and the two copies
    serve OPPOSITE consequences — this one suppresses an alert, the other sends
    SIGKILL to a process group — so a divergence would surface in the worst
    possible place.

    THE POINT SPECIALIST MADE ON T-2307, and the reason consolidating is safe: the
    fix is not "make both callers return the same thing", it is "make the shared
    predicate hand back a DISTINGUISHABLE unknown and let each caller choose its
    own direction". flock_holder_pid does exactly that (int / None / UNKNOWN), so
    the direction stays here, at the caller, where the consequence lives:

        a DETECTOR fails OPEN  — unknown -> None -> nothing is suppressed.
        a KILL PATH fails CLOSED — see _lease._reap; unknown -> refuse to act.

    Both are correct and they are opposites. That asymmetry is the thing a naive
    "one return type" merge would have destroyed.

    THE HISTORY THIS REPLACES, kept because it is why the predicate exists at
    all: T-2283 shipped `os.kill(pid, 0)` against the pid named in the
    lockfile's JSON, which asks "does a process with this pid exist" — not "is
    the lease held". Those come apart in exactly the crash case the check was
    written for: the lease is `fcntl.flock(LOCK_EX|LOCK_NB)`, so a crash
    releases it in the KERNEL while the record text survives on disk still
    naming pid N. Linux then recycles N onto an unrelated process, the existence
    probe succeeds, and detection is suppressed for the lease's whole TTL — the
    precise blind spot the fix existed to prevent, reached from the other side.
    (specialist, T-2291.) /proc/locks answers the real question: it lists the locks
    the kernel actually holds, keyed by MAJOR:MINOR:INODE, so a released lock is
    simply absent regardless of what any file says or which pids exist.

    ONE DELIBERATE BEHAVIOUR CHANGE, called out rather than absorbed: a stat()
    failure that is NOT FileNotFoundError (a permissions regression on the lock,
    say) used to return False here and now returns None. False meant "not held",
    which was a guess; None means "cannot tell", which is true. No caller
    changes behaviour — every one of them tests `is True` — but the observation
    stops asserting something it does not know.
    """
    try:
        holder = Lease(lock).flock_holder_pid()
    except OSError:
        return None                       # cannot tell -> caller fails open
    if holder is UNKNOWN:
        return None
    return holder is not None


def _coerce_draining(obs, advanced):
    """Mark demonstrably-draining inboxes as NOT idle-with-unread before evaluation.

    An ack SINCE THE LAST SAMPLE proves the consumer is alive and working the
    queue right now. Such an inbox must not reach detect() as a staleness
    candidate at all — see the T-2240 note in main() for why filtering afterwards
    is actively harmful. Returns (coerced_obs, [names_suppressed]).

    `advanced` is drain_progress()'s set of inboxes whose cur/ mtime moved since
    the previous sample. T-2257: this REPLACES the old static comparison
    (recency < oldest_unread_age). Both of those grow at the same rate, so their
    difference was CONSTANT and one partial drain suppressed an inbox FOREVER —
    archivist and specialist were silently unalertable for hours on 2026-07-28 while
    the operator was reporting them broken. "Is it draining?" is a question about
    change over time; only a cross-sample delta can answer it.

    Fails LOUD: an inbox absent from `advanced` (no prior sample, unreadable
    clock, or genuinely frozen) coerces nothing.
    """
    out, drained = {}, []
    for name, o in obs.items():
        if name in advanced:
            out[name] = replace(o, unread=0, oldest_unread_age_s=None)
            drained.append(name)
        else:
            out[name] = o
    return out, drained


def delivery_recency(hub: Path = MAILBOX_HUB, now: float | None = None) -> dict[str, float | None]:
    """Seconds since each inbox last ACTUALLY delivered something.

    The `cur/` DIRECTORY mtime is the last-delivery clock. Deliberately the
    directory and not the newest file in it: `rename()` PRESERVES file mtime, so
    the newest file in cur/ is the newest message SENT among those delivered —
    not when delivery happened. That artifact fooled both me and orchestrator in
    opposite directions on the same queue within an hour (2026-07-28).

    None = could not stat, which callers must treat as "unknown", never as
    "recent" — an unreadable clock must not silence an alert.

    T-2257: this age is DISPLAY-ONLY. It must never feed a suppression decision:
    it is (now - fixed_ts), which co-grows with any age it could be compared to,
    so the comparison's sign can never flip and suppression becomes permanent.
    Suppression decisions come from drain_progress() below.
    """
    now = time.time() if now is None else now
    out: dict[str, float | None] = {}
    names, _ = discover_inboxes(hub)
    for name in names:
        try:
            out[name] = now - (hub / name / "cur").stat().st_mtime
        except OSError:
            out[name] = None
    return out


DRAIN_CLOCK_PATH = STATE_PATH.with_name("mail_staleness_drain_clock.json")


def load_drain_clock(path: Path = DRAIN_CLOCK_PATH) -> dict[str, float]:
    """Previous sample's per-inbox cur/ mtimes. Missing/corrupt → {} (first-run
    shape: nothing can claim to have advanced, so nothing is suppressed)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {k: float(v) for k, v in raw.items() if isinstance(v, (int, float))}
    except (OSError, json.JSONDecodeError, ValueError, AttributeError):
        return {}


def save_drain_clock(clock: dict[str, float], path: Path = DRAIN_CLOCK_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clock, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def drain_progress(hub: Path, prior: dict[str, float]) -> tuple[dict[str, float], set[str]]:
    """The T-2257 draining discriminator: which inboxes ACKED since the last sample?

    cur/'s directory mtime advances exactly when a message is renamed into it —
    i.e. on ack. Comparing that mtime ACROSS SAMPLES answers "is anyone draining
    this queue?" with an actual observation of change, which the old
    static age comparison could not do (both ages grew at the same rate, so one
    partial drain suppressed forever — the defect this function replaces).

    Returns (clock, advanced):
      clock    — this sample's {inbox: cur_mtime}, to persist for the next run
      advanced — inboxes whose mtime moved FORWARD since `prior`

    Fail-loud edges, all resolving to "not advanced" (never suppress on unknown):
      - no prior record (first run, new inbox)      → not advanced
      - cur/ unstattable now                        → not advanced, dropped from clock
      - mtime equal or (clock weirdness) backwards  → not advanced
    """
    clock: dict[str, float] = {}
    advanced: set[str] = set()
    names, _ = discover_inboxes(hub)
    for name in names:
        try:
            m = (hub / name / "cur").stat().st_mtime
        except OSError:
            continue  # unreadable clock: unknown, never "draining"
        clock[name] = m
        p = prior.get(name)
        if p is not None and m > p:
            advanced.add(name)
    return clock, advanced


def suppress_draining(alerts, obs, advanced):
    """Drop alerts for inboxes that are demonstrably DRAINING, just slowly.

    WHY THIS EXISTS. The WARN tier gates on the agent's MAIN PANE being idle.
    For ACP-backed agents (archivist, specialist) the main pane is NOT the consumer —
    the ACP bridge is — so the pane reads idle while the bridge works, and every
    healthy backlog would page. Measured before deploying: archivist sat at 10
    unread with a 26.6-minute-old head while its bridge was provably mid-turn
    (transcript +5.4KB in 20s). That alert would have been true and useless, and
    a monitor that fires on healthy gets switched off — after which the real
    outage it exists for goes unnoticed too.

    THE DISCRIMINATOR (T-2257, replacing the original): an ack SINCE THE LAST
    SAMPLE — drain_progress()'s cross-sample cur/ mtime delta. It needs no pane,
    no transcript and no cross-account read — only a surface this module already
    stats.

    The ORIGINAL discriminator ("a delivery more recent than the oldest pending
    message") was wrong in principle: both ages grow at the same rate, so the
    comparison's sign could never flip and ONE partial drain suppressed the inbox
    FOREVER. Live consequence 2026-07-28: archivist (gap 110m, constant) and
    specialist (gap 31m, constant) were permanently unalertable — precisely the two
    inboxes the operator was reporting as broken. Third defect in this suppression
    path; see the mutation tests before touching it again.

    Still validated against 2026-07-28's real cases, now via the delta:
      archivist, healthy-but-slow  : acks landing between samples -> advances -> SUPPRESS
      specialist, bridge dead 14:35-16:20: cur/ frozen at 14:35, mail piling
                                     -> never advances            -> ALERT
    Fails LOUD: an inbox not in `advanced` (unknown clock, first run, frozen)
    never suppresses.
    """
    kept, dropped = [], []
    for a in alerts:
        if a.inbox in advanced:
            dropped.append(a)
        else:
            kept.append(a)
    return kept, dropped


def _inbox_in_maintenance(inbox: str) -> bool:
    """T-5244: True iff the seat behind `inbox` is in an ACTIVE seat-maintenance
    window (notify_policy, T-2984). Inbox names are persona names. Fail-open.
    ORG-106 L4b: a shim over the shared seat_state.maintenance_active."""
    return _seat_state_mod().maintenance_active(inbox)


def _seat_state_mod():
    """ORG-106 L4b: the seat_state module, importable from a package or a bare run."""
    try:
        from tools import seat_state
    except ImportError:  # bare run with cwd=tools/
        import seat_state  # type: ignore
    return seat_state


def _inbox_seat_paused(inbox: str) -> bool:
    """ORG-106 L4b (T-5527): True iff the seat behind `inbox` has a seat-state RECORD of
    paused-intake or deep-sleep, so mail queued to it is expected, not a staleness fault.
    Keyed on the record's state (not intake_blocked): an OOM hold or the legacy flag also
    block intake but are not a declared pause, and keep alerting exactly as before.
    Fail-open: an unreadable / unknown seat reads False, so an alert is never silenced."""
    try:
        seat = _seat_state_mod().safe_effective_for(inbox)
        # T-5868: a halted seat is an incident, not a declared pause; its mail still pages.
        return bool(seat is not None and seat.state not in ("active", "halted"))
    except Exception:  # noqa: BLE001
        return False


def _inbox_seat_asleep(inbox: str) -> bool:
    """ORG-106 L4b: True iff the seat-state record is deep-sleep (draining or asleep).
    Fail-open like _inbox_seat_paused."""
    try:
        seat = _seat_state_mod().safe_effective_for(inbox)
        return bool(seat is not None and seat.state == "deep-sleep")
    except Exception:  # noqa: BLE001
        return False


def _inbox_held(inbox: str) -> bool:
    """ORDINARY tiers (warn / coarse / busy_stale): maintenance (T-5244) or a declared
    pause / sleep (ORG-106). Both names resolved per call so each leg stays patchable."""
    return _inbox_in_maintenance(inbox) or _inbox_seat_paused(inbox)


def _inbox_urgent_held(inbox: str) -> bool:
    """URGENT tier: maintenance (unchanged) or deep-sleep only. A PAUSED seat is woken by
    urgent mail (ORG-106 decision 2, L2), so urgent mail still unread there means the wake
    failed and must alert; a sleeping seat queues mail for its wake, so it does not."""
    return _inbox_in_maintenance(inbox) or _inbox_seat_asleep(inbox)


def suppress_maintenance(alerts, in_maintenance=None):
    """T-5244: drop alerts for seats held in seat-maintenance. A held seat cannot
    drain by design, so its unread mail is expected, not an outage (mason paged
    '1 unread, 3.0h' 2026-10-19 while held). Applies to every tier, urgent included:
    the hold is deliberate and expires on its own."""
    check = in_maintenance or _inbox_urgent_held  # resolved per call: patchable
    kept, dropped = [], []
    for a in alerts:
        (dropped if check(a.inbox) else kept).append(a)
    return kept, dropped


# -------------------------------------------------------------------- state
# Tiers whose state is {inbox: last_alert_epoch} rather than ms.InboxState.
_STAMP_TIERS = ("urgent", "busy_stale")


def load_state(path: Path = STATE_PATH) -> dict[str, dict]:
    blank: dict[str, dict] = {"warn": {}, "coarse": {}, "urgent": {}, "busy_stale": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return blank
    out = dict(blank)
    for tier in ("warn", "coarse"):
        for inbox, st in (raw.get(tier) or {}).items():
            try:
                out[tier][inbox] = ms.InboxState(**st)
            except TypeError:
                continue  # a shape change must degrade to "start fresh", never crash
    # T-2237 rev2: "urgent" holds {inbox: last_alert_epoch} floats, NOT
    # InboxState — the cooldown stamps for the severity-first path. T-4323's
    # "busy_stale" tier is the same shape.
    for tier in _STAMP_TIERS:
        out[tier] = {k: v for k, v in (raw.get(tier) or {}).items()
                     if isinstance(v, (int, float))}
    return out


def save_state(state: dict[str, dict], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {tier: (dict(boxes) if tier in _STAMP_TIERS
                else {k: asdict(v) for k, v in boxes.items()})
         for tier, boxes in state.items()},
        indent=2) + "\n", encoding="utf-8")


def urgent_unread(box: Path) -> list[tuple[str, str]]:
    """Undelivered messages in `box` carrying severity above the floor (T-2237 b).

    Returns (message_id, sender) for each unread message marked urgent, oldest
    first. Empty when there are none OR when the inbox cannot be read — this is a
    SUPPLEMENT to the age tiers, never a replacement, so an unreadable box is
    still reported by the existing `readable` path rather than twice.

    WHY THIS IS AGE-BLIND, which is the whole point of the operator's critique: the
    tiers ask "has mail been sitting long enough that nobody can be draining",
    which is a proxy for harm that treats nine FYIs and one dead-bridge notice
    identically. An urgent message is one whose UNDELIVERED STATE IS ITSELF THE
    HARM — a cron died, a credential is dead, a message was dead-lettered. Waiting
    15 or 60 minutes to say so is waiting for a clock to agree with something
    already known.

    ⚠️ This is only meaningful because part (a) landed FIRST. Read against a
    corpus where every message is `normal` — which was true of all 21 queued
    messages when the ticket was filed — this returns [] forever and is a guard
    that cannot fire. That ordering is the ticket's explicit instruction and it is
    the reason the severity floor is not simply "a nicer threshold".

    Malformed or unreadable individual messages are SKIPPED, not guessed at: a
    message whose envelope will not parse is a different defect (the DLQ's), and
    inventing a severity for it would put this check in the business of asserting
    things it cannot see.
    """
    out: list[tuple[str, str]] = []
    try:
        entries = sorted(p for p in (box / "new").iterdir()
                         if p.is_file() and not p.name.startswith("."))
    except OSError:
        return []
    for p in entries:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("severity") in _URGENT_SEVERITIES:
            out.append((p.name, str(rec.get("sender") or "?"), rec.get("ts")))
    return out


# T-2237(b) rev2, from the 2026-08-02 live over-fire (the operator got paged): the
# detector was STATELESS and GRACELESS, so a unit-failure alert landing in
# recovery's own inbox paged the operator DIRECT (the recovery-self routing carve-out)
# within one 5-min sample — for mail recovery drained minutes later in the normal
# course — and would have re-paged EVERY sample until the drain. Two bounds:
#
# GRACE is not an age-as-harm threshold; it is allowance for the delivery
# machinery's own latency. The waker fires on a 5-min timer and a drain takes
# a turn — a message 4 minutes old is IN DELIVERY, not undelivered. 10 min =
# two wake cycles; against tiers measured in hours it keeps severity-first
# semantics intact.
URGENT_GRACE_SECONDS = int(os.environ.get("MAIL_STALENESS_URGENT_GRACE_SECONDS", "600"))
# COOLDOWN bounds the re-page rate while the condition persists (the tier
# alerts get this from ms.detect's sample counters; this path bypasses them by
# design and so needs its own).
URGENT_REALERT_SECONDS = int(os.environ.get("MAIL_STALENESS_URGENT_REALERT_SECONDS", "1800"))


def detect_urgent_undelivered(hub: Path = MAILBOX_HUB, prior: dict | None = None,
                              now: float | None = None) -> tuple[list, dict]:
    """Alerts for inboxes holding undelivered urgent mail + next urgent-state.

    Deliberately NOT routed through `ms.detect`: that module is the semantics of
    record for "idle + unread accumulates over samples", and this claim has no
    sample dimension at all — one urgent message PAST THE DELIVERY GRACE is
    enough. Folding an age-blind rule into a sample-counting detector would have
    meant giving it a fake threshold to satisfy the interface.

    `prior` is the {"inbox": last_alert_epoch} map from the state file's
    "urgent" key; the returned dict replaces it. Entries for inboxes no longer
    holding urgent mail are DROPPED, so a recovered inbox re-alerts fresh on the
    next incident rather than inheriting a stale cooldown.
    """
    now = time.time() if now is None else now
    prior = prior or {}
    next_urgent: dict[str, float] = {}
    alerts = []
    names, _ = discover_inboxes(hub)
    for name in names:
        hits = urgent_unread(hub / name)
        # Grace: a message younger than the delivery machinery's own latency is
        # in delivery, not undelivered. Unparseable ts fails toward ALERTING
        # (age unknown must not read as fresh).
        hits = [h for h in hits
                if not (isinstance(h[2], (int, float)) and (now - h[2]) < URGENT_GRACE_SECONDS)]
        if not hits:
            continue
        last = prior.get(name)
        if isinstance(last, (int, float)) and (now - last) < URGENT_REALERT_SECONDS:
            next_urgent[name] = last  # still in cooldown; keep the stamp
            continue
        next_urgent[name] = now
        senders = sorted({s for _, s, _ in hits})
        alerts.append(ms.Alert(
            inbox=name,
            kind=_KIND_URGENT_UNDELIVERED,
            unread=len(hits),
            oldest_age_s=None,
            samples=0,
            text=(f"mailbox: {name} holds {len(hits)} UNDELIVERED URGENT "
                  f"message(s) from {', '.join(senders)} — reported on SEVERITY, "
                  f"not age or count. These are the senders whose undelivered "
                  f"state IS the harm (a dead cron, a dead credential, a "
                  f"dead-lettered message), so this does not wait for a staleness "
                  f"threshold to agree (T-2237)"),
        ))
    return alerts, next_urgent


# T-4323(b): the BUSY backstop. pane_idle False suppresses every tier above, and
# the waker defers to it too, so a pane MISREAD as busy got no alert at all: #386,
# and T-4319 R1's quoted "Enter to view" footer, starved seats in silence. This
# tier bounds that silence on AGE, not samples. A busy seat with steady mail churn
# never reaches unread 0, so a sample count would fire on healthy agents. 2h is
# far past any wake cycle; T-4287's exit-8 counts are the data to tune it with.
BUSY_STALE_AGE_SECONDS = int(os.environ.get("MAIL_STALENESS_BUSY_STALE_AGE_SECONDS", "7200"))
BUSY_STALE_REALERT_SECONDS = int(os.environ.get("MAIL_STALENESS_BUSY_STALE_REALERT_SECONDS", "3600"))
_KIND_BUSY_STALE = "busy_stale"


def detect_busy_stale(obs: dict, hub: Path = MAILBOX_HUB, prior: dict | None = None,
                      now: float | None = None, skip=frozenset()) -> tuple[list, dict]:
    """Alerts for inboxes read BUSY with unread mail older than the backstop.

    `obs` must be the draining-coerced view (main() passes it): an inbox that
    acked since the last sample has unread 0 there, and a consumer that is
    draining is not starved. Left to the tiers that own them: idle panes (WARN),
    unreadable panes (COARSE), a held consumer lease (T-2283: turn length, not
    starvation) and urgent mail (the severity path). `skip` names inboxes some
    other tier already alerted this sample, so none is reported twice.

    `prior` is {inbox: last_alert_epoch}, like the urgent path. A stamp still
    inside the cooldown is kept for any inbox present in `obs`, even one that
    doesn't qualify this sample. Without that, one unknown or IDLE read (a
    flapping pane, a failed capture) dropped it, and the next BUSY read re-alerted
    the same backlog (rev-t4284b r1 R1). Once the cooldown has run out the stamp
    drops, so the next incident alerts fresh.
    """
    now = time.time() if now is None else now
    prior = prior or {}
    next_state: dict[str, float] = {}
    alerts = []
    for name, o in sorted(obs.items()):
        age = o.oldest_unread_age_s
        if (o.pane_idle is not False or o.consumer_lease_held or not o.unread
                or not isinstance(age, (int, float)) or age < BUSY_STALE_AGE_SECONDS
                or name in skip or urgent_unread(hub / name)):
            continue
        last = prior.get(name)
        if isinstance(last, (int, float)) and (now - last) < BUSY_STALE_REALERT_SECONDS:
            next_state[name] = last  # still in cooldown; keep the stamp
            continue
        next_state[name] = now
        alerts.append(ms.Alert(
            inbox=name,
            kind=_KIND_BUSY_STALE,
            unread=o.unread,
            oldest_age_s=age,
            samples=0,
            text=(f"mailbox: {name} read BUSY with {o.unread} unread, oldest "
                  f"{ms._format_age(age)}: past the "
                  f"{ms._format_age(BUSY_STALE_AGE_SECONDS)} busy-stale backstop "
                  f"(T-4323). Every other tier and the waker defer to a busy pane, "
                  f"so this is the only alert it can get. Either one turn has run "
                  f"that long without draining, or the pane is being MISREAD as busy "
                  f"(the T-4319 class). Check the pane: a turn-ended prompt with "
                  f"this backlog is a stuck seat."),
        ))
    for name in obs:
        last = prior.get(name)
        if (name not in next_state and isinstance(last, (int, float))
                and (now - last) < BUSY_STALE_REALERT_SECONDS):
            next_state[name] = last
    return alerts, next_state


def _inbox_seat_is_paused(inbox: str) -> bool:
    """Is this inbox's agent a seat that was deliberately taken out of service?

    T-2397. Delegates to channel_liveness_monitor's `_session_unit_is_disabled`
    rather than carrying a second copy of the predicate — that helper is the one
    with the disabled-vs-inactive reasoning and the tests behind it (a CRASHED
    unit stays `enabled`, so only an explicit disable counts), and a shared
    predicate with two owners has none (the lesson T-2304 landed one layer down).

    Fail-open in BOTH directions that matter: an inbox with no agent entry, an
    unreadable probe, or any import/exception at all returns False → the alert is
    delivered. Silencing a real stale-mail condition because a probe failed is the
    expensive way to be wrong, and this function exists only to remove noise.
    """
    # `tools.` FIRST, deliberately. Both spellings resolve at runtime (the unit runs
    # with cwd=repo-root), but they are DIFFERENT module objects — the double-import
    # trap this file's own header warns about for Lease/UNKNOWN. Preferring the bare
    # name made every patch of `tools.channel_liveness_monitor...` in the suite miss,
    # so the tests exercised the real subprocess instead of the mock and three of them
    # failed for a reason that had nothing to do with the logic under test.
    try:
        from tools.channel_liveness_monitor import _session_unit_is_disabled
        from tools.context_fill_monitor import AGENT_CONFIG
    except ImportError:
        try:
            from channel_liveness_monitor import _session_unit_is_disabled
            from context_fill_monitor import AGENT_CONFIG
        except ImportError:
            return False
    cfg = AGENT_CONFIG.get(inbox)
    if cfg is None:
        return False  # specialist-a2a and friends — not a persona seat, never "paused"
    try:
        return _session_unit_is_disabled(inbox, cfg) is True
    except Exception:
        return False


# Suborch/second-head seats that legitimately sit idle-with-unread by design —
# T-2665. Their session unit stays ENABLED while parked (`_inbox_seat_is_paused`
# above does not see them at all), so an undrained queue on a deliberately-parked
# seat previously false-fired as a real staleness alert (Recovery, mail-staleness
# re-fires 2026-08-08, T-2665 transition log). Declared here because there is no
# existing on-demand-persona registry to extend (grepped repo-wide, T-2665
# investigation) — this constant IS the registry. Verified against both
# `AGENT_CONFIG` and `_persona_panes()` at write time; `InboxSeatIsOnDemandParkedTest
# .test_allowlist_members_are_real_persona_seats` catches drift so a future
# typo/rename fails a test instead of silently disabling attribution.
ON_DEMAND_PERSONAS: frozenset[str] = frozenset({"artisan", "mason", "archivist"})

# T-3454: park-suppression CEILING. An on-demand seat legitimately sits idle-with-
# unread between dispatches, so we suppress — but NOT indefinitely. An up-but-idle
# seat whose WAKE is broken (mail arrives, nothing wakes it to consume) is
# INDISTINGUISHABLE from a legit park by the pane alone — that is exactly how the
# mason/artisan 6-week rot (T-3427) slipped past this monitor: their wake timers were
# never enabled, they sat idle-at-prompt, and park-suppression hid the aging queue.
# So past this ceiling the "parked by design" attribution no longer holds and the
# alert proceeds regardless of pane. Deliberately GENEROUS (48h): far beyond any
# legit dispatch gap, ~20x under the rot that motivated it, and a rare false-positive
# is only a read-only Recovery alert. Safe-direction: this can only REDUCE suppression,
# never add a false-negative (the dangerous direction that caused the rot).
ON_DEMAND_PARK_CEILING_SEC: float = float(
    os.environ.get("PANTHEON_MAIL_STALENESS_PARK_CEILING_SEC", str(48 * 3600)))


def _inbox_seat_is_on_demand_parked(inbox: str, pane_idle: bool | None,
                                    oldest_age_s: float | None = None) -> bool:
    """Is this inbox a declared on-demand-persona seat, observed idle-at-prompt

    right now, AND not stale past the park ceiling? T-2665 + T-3454.

    Returns True (parked-by-design, safe to attribute/suppress the undrained-queue
    alert) ONLY when ALL of:
      (a) `inbox` is on the declared ON_DEMAND_PERSONAS allowlist above, AND
      (b) `pane_idle` is affirmatively True (idle-at-prompt, not mid-turn), AND
      (c) `oldest_age_s` is below ON_DEMAND_PARK_CEILING_SEC (T-3454) — an idle seat
          whose mail has aged past the ceiling is more likely a broken wake than a
          legit park (the T-3427 rot signature), so suppression lifts. `None` age
          (not supplied) does not trip the ceiling — callers without an age get the
          pre-T-3454 behavior.

    CALLER CONTRACT — the load-bearing part: `pane_idle` MUST be the RAW
    three-state value, i.e. `obs[inbox].pane_idle` as built by `observe()` (or an
    equivalently-fresh `capture_pane_idle(inbox)` call). `deliver()` does not
    currently receive `obs` — wiring this in means threading `obs` through to
    `deliver()` so the RAW field can be passed here.

    Do NOT pass anything derived from `coarse_view()`'s output. `coarse_view()`
    coerces an unknown pane (None) to True "as policy" for its own unknown-pane
    detection tier (see its docstring) — that coercion is correct for THAT tier's
    purpose and wrong for this one. Feeding a coarse_view-coerced observation here
    would silently convert an absent-pane on-demand seat into a suppressed alert:
    archivist has no tmux server at all, so its raw pane_idle is None, but its
    coarse_view-coerced pane_idle is True — passing the coerced value would make
    this predicate wrongly attribute archivist as "parked" from silence alone,
    which is exactly the never-suppress-on-unknown invariant this file's fail-safe
    doctrine (`_consumer_lease_held`, `_inbox_seat_is_paused`) exists to prevent.

    DELIBERATE non-coverage (option (a), T-2665 design discussion): an on-demand
    seat with an ABSENT pane (`pane_idle is None`) is therefore NOT attributed
    here — it keeps alerting. archivist fits this today. This is intentional, not
    an oversight: coercing None to "parked" would require guessing that silence
    means benign-park rather than wedged/dead, and archivist is not noisy the way
    artisan/mason are, so the cost of leaving it uncovered is small. Do not "fix"
    this by treating None as parked.

    Any non-allowlisted persona, or pane_idle in {False, None}, returns False —
    the alert proceeds. The try/except below can't be hit by today's pure
    membership-check body, but it's kept anyway to preserve this file's
    fail-open-to-False silhouette (every seat-attribution predicate here reads the
    same way) and to future-proof the contract if the allowlist check ever grows
    more than a static `in`.
    """
    try:
        if inbox not in ON_DEMAND_PERSONAS or pane_idle is not True:
            return False
        # T-3454: past the ceiling, a "parked" idle seat is more likely a broken
        # wake than a legit park — stop suppressing so the aging queue surfaces.
        if oldest_age_s is not None and oldest_age_s >= ON_DEMAND_PARK_CEILING_SEC:
            return False
        return True
    except Exception:
        return False


# --------------------------------------------------------------- evaluation
def coarse_view(obs: dict[str, ms.InboxObservation]) -> dict[str, ms.InboxObservation]:
    """The unknown-pane subset, with pane_idle coerced True and never_received muted.

    THE COERCION IS THE POLICY, stated in one place so it can be argued with: an
    unreadable pane is not evidence of idleness, so this tier only ever fires at the
    COARSE threshold, and its alert text must say the pane was unreadable.
    """
    from dataclasses import replace
    return {
        name: replace(o, pane_idle=True, ever_received=True)
        for name, o in obs.items() if o.pane_idle is None
    }


# ------------------------------------------------ ORG-076 BT5a: raw per-sample verdicts (T-6001)
def raw_sample_verdicts(obs: dict[str, ms.InboxObservation]) -> dict:
    """One RAW per-inbox verdict per sample, for the ORG-076 episode store: inbox -> (verdict, kind).

    This is the SAME classification the monitor already computes, taken BEFORE any cooldown or
    consecutive-sample counter (a cooldown Alert can never give N consecutive non-healthy samples):
    `ms.detect` on a single observation with empty prior state and a threshold of 1, then the coarse
    tier's own `coarse_view` for an unreadable pane. `obs` must be what main() feeds `evaluate` (after
    the draining coercion; held inboxes already removed).

      unreadable inbox                                  -> unknown (KIND_UNREADABLE)
      never received                                    -> broken  (KIND_NEVER_RECEIVED)
      pane idle + unread, no consumer lease             -> broken  (KIND_IDLE_WITH_UNREAD)
      pane None (always so on codex TUI / absent pane) with unread older than the coarse tier's own
        budget (COARSE_SAMPLES * SAMPLE_INTERVAL_SECONDS) -> unknown (the coarse tier's unreadable-pane case)
      everything else (nothing unread, busy pane, lease held, pane None within budget) -> healthy
    """
    coarse_budget_s = COARSE_SAMPLES * SAMPLE_INTERVAL_SECONDS
    out = {}
    for name, o in sorted(obs.items()):
        kinds = {a.kind for a in ms.detect({name: o}, {}, idle_samples_required=1, realert_after_samples=1)[0]}
        if ms.KIND_UNREADABLE in kinds:
            out[name] = ("unknown", ms.KIND_UNREADABLE)
        elif ms.KIND_NEVER_RECEIVED in kinds:
            out[name] = ("broken", ms.KIND_NEVER_RECEIVED)
        elif ms.KIND_IDLE_WITH_UNREAD in kinds:
            out[name] = ("broken", ms.KIND_IDLE_WITH_UNREAD)
        elif (o.pane_idle is None and o.unread > 0 and o.oldest_unread_age_s is not None
              and o.oldest_unread_age_s >= coarse_budget_s
              and ms.detect(coarse_view({name: o}), {}, idle_samples_required=1, realert_after_samples=1)[0]):
            out[name] = ("unknown", ms.KIND_UNREADABLE + "-pane")
        else:
            out[name] = ("healthy", "ok")
    return out


def feed_mail_delivery(obs: dict, held: "set | frozenset" = frozenset(), now=None, drop=None, env=None) -> list:
    """Write this cycle's raw verdicts as ORG-076 drops (dark unless the switch is exactly "1"). Never
    raises and never touches alerting: a failure is logged and the monitor carries on. Held (maintenance /
    paused / asleep) and muted inboxes get no verdict and are logged as suppressed."""
    try:
        try:
            from tools import org076_mail_delivery as omd
        except ImportError:
            import org076_mail_delivery as omd  # type: ignore
        if not omd.enabled(env):
            return []
        suppressed = [(n, "held: seat-maintenance / paused / deep-sleep") for n in sorted(held)]
        live = {}
        for name, o in obs.items():
            if _recipient_is_muted(name):
                suppressed.append((name, "muted"))
            else:
                live[name] = o
        samples = []
        for name, (verdict, kind) in raw_sample_verdicts(live).items():
            o = live[name]
            samples.append(omd.Sample(
                inbox=name, verdict=verdict, kind=kind, unread=o.unread, in_flight=o.in_flight,
                oldest_age_s=o.oldest_unread_age_s,
                pane={True: "idle", False: "busy", None: "unknown"}[o.pane_idle]))
        return omd.feed_safely(samples, suppressed=suppressed, drop=drop, now=now, env=env)
    except Exception as exc:  # noqa: BLE001
        print(f"[mail_staleness_monitor] ORG-076 mail-delivery feed failed (ignored; alerting unaffected): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return []


def evaluate(obs, prior, *, warn_samples=WARN_SAMPLES, coarse_samples=COARSE_SAMPLES,
             realert=REALERT_AFTER_SAMPLES):
    """Run both tiers. Returns (alerts, next_state) with coarse alerts relabelled."""
    warn_alerts, warn_state = ms.detect(
        obs, prior.get("warn", {}),
        idle_samples_required=warn_samples, realert_after_samples=realert)
    coarse_alerts, coarse_state = ms.detect(
        coarse_view(obs), prior.get("coarse", {}),
        idle_samples_required=coarse_samples, realert_after_samples=realert)

    # Never claim "observed idle" for a pane we could not read — AND key that on
    # the CURRENT sample, not on which tier produced the alert (T-2395).
    #
    # The relabel used to apply to the coarse list alone, on the reasoning that
    # coarse IS the unknown-pane tier. That misses the WARN tier re-alerting off a
    # HELD counter. `detect`'s ledger holds (never resets) accumulated idle
    # evidence when the pane goes unknown — deliberate and correct, see its
    # "busy/unknown + unread -> HOLD" comment — and re-alerts fire off
    # samples_since_alert against that held count. So a warn-tier alert can be
    # emitted long after the pane stopped being readable, still asserting
    # "pane was OBSERVED idle".
    #
    # MEASURED 2026-08-02: archivist, whose seat was deliberately paused (no claude
    # process, no tmux server at all), alerted from the warn tier at
    # idle_unread_samples=313 with exactly that text. The 313 were real, and every
    # one of them predates the pause. Coarse held only 2. The claim was true once
    # and was reported in the present tense.
    #
    # HOLDING the evidence is right; DESCRIBING it as a present observation is not.
    # So the discriminator is obs[inbox].pane_idle right now, and each tier says
    # what it actually has.
    def _honest(a, *, coarse: bool):
        if a.kind == ms.KIND_NEVER_RECEIVED:
            return a
        o = obs.get(a.inbox)
        if o is not None and o.pane_idle is not None:
            return a  # pane genuinely readable this sample — the claim stands
        replacement = (
            "pane was UNREADABLE (foreign substrate, no pane, or capture failed) — "
            "reported on age alone at the coarse tier, so treat idleness as UNKNOWN"
            if coarse else
            "pane is NOT READABLE AT THIS SAMPLE — the idle count is evidence HELD "
            "from earlier samples when it was, so treat present idleness as UNKNOWN "
            "and check whether the seat is paused or down before reading this as a "
            "stuck agent"
        )
        return type(a)(**{**asdict(a), "text": a.text.replace(
            "pane was OBSERVED idle, so the agent reached a prompt without draining",
            replacement)})

    return (
        [_honest(a, coarse=False) for a in warn_alerts]
        + [_honest(a, coarse=True) for a in coarse_alerts],
        {"warn": warn_state, "coarse": coarse_state},
    )


# -------------------------------------------------------- sender attribution
# Senders generated BY the mail layer itself. Notifying them would be an alert
# about an alert, which is the loop _maybe_quarantine's exemption exists to stop
# one layer down.
_SYNTHETIC_SENDERS = frozenset({"mailbox-dlq", "mail-staleness", _ALERT_VOCAB})


def stalled_senders(box: Path, now: float) -> dict | None:
    """{sender: {"count": n, "oldest_age_s": s}} over messages sitting in new/.

    None if new/ could not be read — a partial attribution is worse than none,
    because the senders it omits are silently exonerated.

    READ-ONLY, and that is a contract not a preference (the operator's remediation
    disarm, TG 6520): this opens each message with json.load and moves nothing.
    It is the same read the T-2232 dormant-seat diagnosis used, chosen then for
    the same reason — an ack here would consume the very evidence the alert is
    about.
    """
    senders: dict = {}
    try:
        entries = [p for p in (box / "new").iterdir()
                   if p.is_file() and not p.name.startswith(".")]
    except OSError:
        return None
    for p in entries:
        try:
            msg = json.loads(p.read_text(encoding="utf-8"))
            age = now - p.stat().st_mtime
        except (OSError, json.JSONDecodeError, ValueError):
            continue  # unreadable ONE message; the rest are still attributable
        who = msg.get("sender")
        if not isinstance(who, str) or who in _SYNTHETIC_SENDERS:
            continue
        rec = senders.setdefault(who, {"count": 0, "oldest_age_s": 0.0})
        rec["count"] += 1
        rec["oldest_age_s"] = max(rec["oldest_age_s"], age)
    return senders


def notify_senders(alert, hub: Path, now: float, dry_run: bool = False) -> list:
    """Tell each SENDER that its dispatch is sitting in a queue nobody is draining.

    THE GAP THIS CLOSES (T-2267, mason): when a seat is dormant the receiver is
    absent BY DEFINITION, so the only party who could notice is the sender — and
    the sender had no surface that reported it. In the worked case orchestrator
    dispatched T-2245 into a dormant mason seat at 21:38:53Z and reported the
    dispatch upstream as fact; what eventually surfaced it was an OnFailure line
    on an unrelated unit, firing nine minutes later, reaching a third party.

    WHY THIS IS AFTER-THE-FACT RATHER THAN AT SEND TIME, which was one of the
    placements offered and is the one I rejected: a send-time probe is another
    POINT SAMPLE, and mason's own critique of this failure is that two point
    proxies (`pgrep -c` counting a dormant account's systemd/gpg-agent baseline,
    and new==0 read as "drains promptly") both agreed with health while neither
    measured it. A liveness answer that is true at 21:38:53Z says nothing about
    21:39. What is actually load-bearing is drain_progress()'s CROSS-SAMPLE
    measurement — cur/ has not advanced while new/ is non-empty — which is a
    measurement rather than a proxy, and it is only available later. Adding a
    third point proxy on the send hot path would have reproduced the defect the
    ticket is about while appearing to fix it.

    Cadence deliberately rides the alert that produced it rather than carrying
    its own throttle: these are emitted only for alerts that already cleared the
    tier gate and the draining suppression, so there is exactly one cooldown in
    the system to get wrong instead of two.
    """
    notified: list = []
    per_sender = stalled_senders(hub / alert.inbox, now)
    if not per_sender:
        return notified                      # unreadable, or nothing attributable
    for who, rec in sorted(per_sender.items()):
        if who == alert.inbox:
            continue                         # its own mail; it is the one not draining
        mins = rec["oldest_age_s"] / 60.0
        text = (
            f"[{_ALERT_VOCAB}] YOUR DISPATCH IS IN A QUEUE NOBODY IS DRAINING — "
            f"{rec['count']} message(s) you sent to '{alert.inbox}' are still in its "
            f"new/; the oldest has been there {mins:.0f}m and that inbox's cur/ has "
            f"not advanced across samples. THE MAIL IS FINE: well-formed, correctly "
            f"addressed, and really delivered — nothing is draining it, which is why "
            f"you have had silence rather than an error. Silence from a dormant seat "
            f"is indistinguishable from silence from a busy one, so do not read the "
            f"absence of a reply as work in progress. If you reported this dispatch "
            f"upstream as done, it is not. (T-2267)"
        )
        if dry_run:
            print(f"  WOULD NOTIFY SENDER {who}: {text}")
            notified.append(who)
            continue
        try:
            mailbox.send(hub / who, {"kind": "undrained_queue", "inbox": alert.inbox,
                                     "count": rec["count"],
                                     "oldest_age_s": rec["oldest_age_s"]},
                         sender=_ALERT_VOCAB, subject=text[:120])
            notified.append(who)
        except Exception as exc:
            # A sender with no provisioned inbox (a one-off script, a retired
            # persona) is an ordinary outcome, not a monitor failure. Never let
            # it break the sampler.
            print(f"[mail_staleness_monitor] sender-notice to {who} failed: {exc!r}",
                  file=sys.stderr)
    return notified


# ---------------------------------------------------------------- delivery
# rev-t4284b r1 NIT-4 (orchestrator's call): route_failure_alert("recovery") was a direct
# the operator Telegram. That's the wrong tier for a trigger built on a BUSY heuristic
# (D1), so recovery's own busy-stale goes to orchestrator, which escalates if it's real.
# (Since T-4399 route_failure_alert("recovery") also goes to orchestrator first, but this mail
# keeps the structured busy-stale payload and skips the recovery-self backoff.)
_BUSY_STALE_RECOVERY_SELF_ROUTE = "orchestrator"


def _send_busy_stale_to_orchestrator(alert, line: str, hub: Path) -> bool:
    """Mail recovery's own busy-stale alert to orchestrator. False on failure, so the caller
    falls back to the normal route: this monitor fails toward alerting."""
    try:
        mailbox.send(hub / _BUSY_STALE_RECOVERY_SELF_ROUTE,
                     {"kind": _KIND_BUSY_STALE, "inbox": alert.inbox, "unread": alert.unread,
                      "oldest_age_s": alert.oldest_age_s, "text": line},
                     sender=_ALERT_VOCAB, subject=line[:120])
        return True
    except Exception as exc:
        print(f"[mail_staleness_monitor] busy-stale mail to {_BUSY_STALE_RECOVERY_SELF_ROUTE} "
              f"failed ({exc!r}); falling back to the failure route", file=sys.stderr)
        return False


def _busy_stale_goes_to_orchestrator(alert) -> bool:
    """Recovery's own busy-stale alert, while recovery is not muted. rev-t4323 r2 NIT-1: a
    muted recovery takes the failure route, which honours the ORG-076 §7 mute registry;
    the direct mail to orchestrator does not."""
    if alert.kind != _KIND_BUSY_STALE:
        return False
    try:
        from inbox_consumer import SELF_TRIAGE_AGENT, _persona_is_muted
    except Exception as exc:  # fail toward alerting: the orchestrator mail needs only mailbox
        print(f"[mail_staleness_monitor] inbox_consumer import failed ({exc!r}); "
              f"routing recovery's busy-stale to {_BUSY_STALE_RECOVERY_SELF_ROUTE} unmuted",
              file=sys.stderr)
        return alert.inbox == "recovery"
    return alert.inbox == SELF_TRIAGE_AGENT and not _persona_is_muted(SELF_TRIAGE_AGENT)


def _recipient_is_muted(inbox: str) -> bool:
    """Is this inbox's persona muted in the ORG-076 §7 registry (a declared pause)?

    T-4749. The pause signal for controller-managed seats (mason, artisan): their
    session has no restart_service, so _inbox_seat_is_paused cannot see it, but the
    pause runbook mutes them. Fail-open like notify_policy: any lookup failure is
    "not muted", so the sender notice still goes out.
    """
    try:
        from inbox_consumer import _persona_is_muted
        return _persona_is_muted(inbox) is True
    except Exception as exc:
        print(f"[mail_staleness_monitor] mute lookup for {inbox} failed ({exc!r}); "
              f"treating as not muted", file=sys.stderr)
        return False


def deliver(alerts, obs: dict | None = None, dry_run: bool = False,
            coverage_warning: str | None = None,
            hub: Path = MAILBOX_HUB, now: float | None = None) -> None:
    """Route each alert. NO remediation — this never kicks (the operator, TG 6520).

    A coverage warning is routed to RECOVERY regardless of which inbox it concerns: it is a
    statement about the monitor itself, and it goes out even when there are no alerts,
    because that is exactly the case it exists to contradict.
    """
    if coverage_warning:
        line = f"[{_ALERT_VOCAB}] MONITOR COVERAGE INCOMPLETE — {coverage_warning}"
        if dry_run:
            print(f"  WOULD SEND: {line}")
        else:
            try:
                from inbox_consumer import route_failure_alert
                route_failure_alert("recovery", line, _ALERT_VOCAB)
            except Exception as exc:
                print(f"[mail_staleness_monitor] coverage-alert delivery failed: {exc!r}",
                      file=sys.stderr)
    for a in alerts:
        if _inbox_seat_is_paused(a.inbox):
            # T-2397: the seat was taken out of service on purpose, so nobody
            # draining it is the intended state, not a fault. MEASURED: archivist
            # was re-alerted about hourly for the whole operator-directed pause
            # (the operator TG6694) over one held DLQ message that is DELIBERATELY
            # parked there as T-2384's only live specimen. The condition is real,
            # known, ticketed, and cannot change until the seat comes back.
            #
            # Same class as T-2393, and the reason it is only a NOISE fix here
            # rather than a safety one: this monitor is read-only by contract and
            # fails toward ALERTING, never toward acting — it could not have
            # fought the operator the way channel-liveness did. Worth fixing
            # anyway because a pause lasts days and this recurs every one.
            print(f"[mail_staleness_monitor] {a.inbox}: seat deliberately paused "
                  f"(session unit disabled) — not alerting: {a.text[:70]}")
            continue
        # T-2665: on-demand persona seats (suborch/second-head) sit idle-with-unread
        # BY DESIGN — they drain when next spawned, not continuously — so an
        # undrained queue on one is a parked state, not a fault. Their session unit
        # stays ENABLED, so _inbox_seat_is_paused above does not see them, which is
        # why they previously false-fired (artisan/mason, ~hourly, 2026-08-08).
        # Suppress ONLY on affirmative (allowlisted) AND (RAW pane_idle is True);
        # absent/generating pane still alerts (never-suppress-on-unknown). pane_idle
        # is threaded from main()'s obs, which preserves the raw tristate
        # (_coerce_draining does not touch pane_idle, and coarse_view is never applied
        # to main's obs) — feeding a coarse_view-coerced value here would wrongly
        # suppress an absent-pane seat from silence alone.
        _o = obs.get(a.inbox) if obs else None
        if _inbox_seat_is_on_demand_parked(a.inbox, _o.pane_idle if _o else None,
                                           _o.oldest_unread_age_s if _o else None):
            print(f"[mail_staleness_monitor] {a.inbox}: on-demand persona seat "
                  f"idle-at-prompt (parked by design, T-2665) — not alerting: "
                  f"{a.text[:70]}")
            continue
        line = f"[{_ALERT_VOCAB}] {a.text}"
        busy_stale_self = _busy_stale_goes_to_orchestrator(a)
        if dry_run:
            where = f" (to {_BUSY_STALE_RECOVERY_SELF_ROUTE}'s mailbox)" if busy_stale_self else ""
            print(f"  WOULD SEND{where}: {line}")
            continue
        if busy_stale_self and _send_busy_stale_to_orchestrator(a, line, hub):
            continue
        try:
            from inbox_consumer import route_failure_alert
            route_failure_alert(a.inbox if a.inbox in _persona_panes() else "recovery",
                                line, _ALERT_VOCAB)
        except Exception as exc:  # delivery must never break the sampler
            print(f"[mail_staleness_monitor] delivery failed: {exc!r}", file=sys.stderr)
        if a.kind == _KIND_BUSY_STALE:
            # rev-t4284b r1 NIT-3: the sender notice below was written for a dormant
            # seat ("QUEUE NOBODY IS DRAINING"). A BUSY pane may be mid-turn, so it
            # claims more than this tier knows, and it would cost each sender a turn.
            continue
        # T-2267: the alert above tells the RECIPIENT's triage path. When nobody is
        # draining that inbox, the one party guaranteed not to hear about it is the
        # SENDER — so tell it too, separately and by name.
        # T-4749: unless the recipient is muted (a declared pause). route_failure_alert
        # above already withheld the recipient's own alert for that mute, and a sender
        # notice would otherwise repeat every realert (~1h) for the whole pause.
        if _recipient_is_muted(a.inbox):
            print(f"[mail_staleness_monitor] {a.inbox}: recipient muted (declared "
                  f"pause) — not notifying senders: {a.text[:70]}")
            continue
        try:
            notify_senders(a, hub, time.time() if now is None else now, dry_run)
        except Exception as exc:
            print(f"[mail_staleness_monitor] sender attribution failed: {exc!r}",
                  file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Per-inbox mail-staleness detector (T-2157, read-only).")
    ap.add_argument("--dry-run", action="store_true", help="observe + report, deliver nothing, persist nothing")
    ap.add_argument("--json", action="store_true", help="print the raw observation as JSON")
    ap.add_argument("--hub", default=None, help="override the mailbox hub path")
    args = ap.parse_args(argv)

    hub = Path(args.hub) if args.hub else MAILBOX_HUB
    obs = observe(hub)
    if args.json:
        print(json.dumps({k: asdict(v) for k, v in obs.items()}, indent=2))
        return 0

    _, coverage_warning = discover_inboxes(hub)
    prior = load_state()

    # T-2240 CORRECTION to the T-2200 design. Draining-suppression MUST happen at
    # observation time, not as a filter on evaluate()'s output.
    #
    # As first shipped it ran AFTER evaluate(). detect() therefore EMITTED the
    # alert, set samples_since_alert=0, and only then did the filter discard it —
    # so the state machine believed it had alerted and the REALERT_AFTER_SAMPLES
    # throttle (12 samples, ~1h) blocked the next one. The operator saw
    # "0 alert(s)" while the monitor quietly refused to speak.
    #
    # That is not hypothetical: archivist's bridge died silently 19:20-20:50Z on
    # 2026-07-28. The monitor ran 23 times across that window and alerted ZERO
    # times, with state showing idle_unread_samples=22 and samples_since_alert=8 —
    # alerts raised, swallowed, and then throttled. A 90-minute delivery outage
    # went unreported by the tool built that afternoon to report exactly it, and
    # archivist found it by hand.
    #
    # Coercing the OBSERVATION means a draining inbox never counts as an
    # idle-with-unread sample at all: nothing is emitted, so nothing is throttled.
    # The cost is that when draining genuinely stops, the counter starts from zero
    # and the first alert takes warn_samples fires (~15 min) instead of one. That
    # is the right trade — a 15-minute delay beats an hour of enforced silence.
    # T-2257: "draining" is a CROSS-SAMPLE delta (did cur/ advance since the last
    # run?), never a static age comparison — two co-growing ages made the old
    # comparison's sign permanent and suppressed real outages forever. recency
    # stays display-only.
    recency = delivery_recency(hub)
    clock, advanced = drain_progress(hub, load_drain_clock())
    obs, draining = _coerce_draining(obs, advanced)

    # T-5244: a seat held in seat-maintenance is excluded BEFORE evaluate(), not just
    # muted after it: muting an emitted alert would leave the state machine believing it
    # had alerted and throttle the first real alert after the hold (the 2026-07-28
    # swallow-then-throttle defect described above).
    # ORG-106 L4b: a declared pause / deep-sleep queues mail on purpose, same exclusion.
    held_inboxes = {name for name in obs if _inbox_held(name)}
    writer_held = set(held_inboxes)  # T-6001: the drop writer logs these as suppressed
    obs = {k: v for k, v in obs.items() if k not in held_inboxes}

    alerts, next_state = evaluate(obs, prior)

    alerts, suppressed = suppress_draining(alerts, obs, advanced)

    # T-2237(b): the severity floor, added AFTER suppress_draining on purpose.
    # `suppress_draining` mutes an inbox that acked something since the previous
    # sample, because movement means somebody is consuming and the age evidence is
    # stale. That reasoning is right for AGE and wrong for SEVERITY: a consumer can
    # be draining ordinary traffic steadily while an urgent message sits behind it
    # — FIFO guarantees the correction is consumed after the claim it corrects
    # (T-2266) — so "something moved" is not evidence that THIS message moved.
    # Running it after the suppressor keeps the urgent path immune to a mute the
    # age path legitimately earns.
    urgent_alerts, urgent_state = detect_urgent_undelivered(
        hub, prior=prior.get("urgent") if isinstance(prior, dict) else None)
    next_state["urgent"] = urgent_state
    if urgent_alerts:
        seen = {(a.inbox, a.kind) for a in alerts}
        alerts = alerts + [a for a in urgent_alerts if (a.inbox, a.kind) not in seen]

    # T-4323(b): the BUSY backstop runs last, on the draining-coerced `obs`, and
    # skips any inbox another tier already alerted this sample.
    busy_alerts, busy_state = detect_busy_stale(
        obs, hub=hub, prior=prior.get("busy_stale") if isinstance(prior, dict) else None,
        skip={a.inbox for a in alerts})
    next_state["busy_stale"] = busy_state
    alerts = alerts + busy_alerts

    # The urgent tier reads the hub directly, so it still sees held inboxes: drop those
    # alerts AND their cooldown entries, so the urgent tier starts fresh after the hold.
    alerts, held = suppress_maintenance(alerts)
    # ORG-106: the urgent cooldown is dropped only for inboxes whose URGENT tier is held
    # (maintenance / deep-sleep). A paused seat's urgent stamps persist, so a failed wake
    # re-alerts on the normal cooldown, not every sample.
    urgent_held = {name for name in held_inboxes if _inbox_urgent_held(name)}
    for a in held:
        urgent_held.add(a.inbox)
        held_inboxes.add(a.inbox)
    next_state["urgent"] = {k: v for k, v in (next_state.get("urgent") or {}).items()
                            if k not in urgent_held}

    for name, o in sorted(obs.items()):
        state = {True: "idle", False: "busy", None: "unknown"}[o.pane_idle]
        print(f"  {name:<14} unread={o.unread:<3} in_flight={o.in_flight:<3} pane={state}")
    print(f"  {len(alerts)} alert(s)")
    for name in sorted(held_inboxes):
        if _inbox_in_maintenance(name):
            print(f"  · {name}: seat-maintenance active — alerts suppressed (T-5244)")
        else:
            print(f"  · {name}: seat paused / asleep (ORG-106) — mail queues by design, ordinary alerts suppressed (urgent kept unless asleep)")
    for a in suppressed:
        r = recency.get(a.inbox)
        age = f"{r/60:.1f}m ago" if r is not None else "unknown"
        print(f"  · {a.inbox}: draining (acked since the previous sample; "
              f"last delivery {age}) — alert suppressed")
    if coverage_warning:
        # NOT folded into the alert list: this says the monitor cannot see its own
        # population, which invalidates the all-clear rather than being one more
        # finding within it. It must never be possible to read "0 alert(s)" as
        # healthy while this is set.
        print(f"  ⚠️ COVERAGE: {coverage_warning}")

    deliver(alerts, obs=obs, dry_run=args.dry_run, coverage_warning=coverage_warning,
            hub=hub)
    if not args.dry_run:
        feed_mail_delivery(obs, writer_held)  # T-6001: after deliver(); dark + fail-safe
        save_state(next_state)
        save_drain_clock(clock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
