#!/usr/bin/env python3
"""tools/acp_bridge_backlog_watchdog.py — ACP-bridge mailbox backlog watchdog
(T-1633 Unit 2, ORG-031).

Detects messages piling up UNCONSUMED in a piloted agent's Path-A hub inbox
(/home/the-ancestor/mailbox/<agent>/new) — the signature of a dead, stuck, or
quiesced tools/acp_bridge.py bridge process — and alerts, so a silent bridge
failure doesn't strand mail (tools/mailbox.py's own durability guarantee is
that a message just sits in new/ forever if nothing drains it; this is the
"something IS draining it, is it still alive" check on top of that).

Pure file-read (no LLM/API): readdir(new/) authoritatively (mirrors
mailbox.py's own "readdir is the truth" REQ2 posture) + the sortable
`<ts_ns>.<pid>.<seq>.<sender>` filename prefix (tools/mailbox.py's
_make_name) for the oldest-message age, falling back to file mtime if a name
doesn't parse. Alerts (throttled per agent) when the backlog COUNT exceeds a
threshold OR the OLDEST message exceeds an age threshold — either signal
alone is enough (a bridge that's dead outright piles up count; a bridge
that's alive-but-wedged-on-one-turn just has one very old message).

Reuses the SAME direct-to-operator Telegram alert primitive as
tools/disk_usage_monitor.py (`_send_telegram_alert` from tools.inbox_consumer)
rather than reinventing a notifier. NOT installed/enabled by this build — the
.service/.timer templates are for a later, supervised pilot-flip install.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Tunable (env-overridable) thresholds.
COUNT_WARN = int(os.environ.get("PANTHEON_ACP_BACKLOG_COUNT_WARN", "20"))
AGE_WARN_SECONDS = int(os.environ.get("PANTHEON_ACP_BACKLOG_AGE_WARN_SECONDS", str(30 * 60)))
ALERT_COOLDOWN_SECONDS = int(
    os.environ.get("PANTHEON_ACP_BACKLOG_ALERT_COOLDOWN_SECONDS", str(30 * 60))
)

MAILBOX_HUB = Path(os.environ.get("PANTHEON_MAILBOX_HUB", "/home/the-ancestor/mailbox"))

# T-3473: auto-clear provably-resolved DLQ leftovers instead of re-alerting until a
# human hand-moves them. DARK-SHIP default-off (same supervised-first-fire pattern as
# the STALENESS_ENABLED flags elsewhere): the watchdog runs on specialist's account and this
# turns a pure-read monitor into a MUTATOR of specialist's own mailbox, so it stays gated
# behind an explicit opt-in until a first supervised fire confirms the move behaves.
AUTOCLEAR_ENABLED = os.environ.get("PANTHEON_ACP_DLQ_AUTOCLEAR", "").lower() in ("1", "true", "yes")

STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
STATE_PATH = STATE_DIR / "acp_bridge_backlog_alert_state.json"


def _log(msg: str) -> None:
    print(f"[acp_bridge_backlog_watchdog ts={int(time.time())}] {msg}", flush=True)


def _parse_ts_ns(name: str) -> int | None:
    """Extract the ts_ns prefix from a tools/mailbox.py message filename
    (`f"{ts_ns:020d}.{pid}.{seq:06d}.{sender}"`). None if the name doesn't
    match that shape (defensive — never raises on an unexpected file)."""
    head = name.split(".", 1)[0]
    if len(head) == 20 and head.isdigit():
        return int(head)
    return None


class BacklogUnreadable(Exception):
    """Raised by read_backlog() whenever `new/` couldn't be confirmed both
    PRESENT and READABLE — an absent dir (FileNotFoundError) and an
    unreadable one (PermissionError et al — the T-1015 missing-sg-pantheon-mailbox
    -wrap failure shape is the live example) get the SAME treatment.

    Absent is deliberately NOT treated as a benign zero-backlog OK: a
    piloted agent's inbox dirs are created once by mailbox provisioning
    (tools/mailbox.py's _ensure_subdirs) and persist thereafter — a
    dead/stuck bridge leaves the dir PRESENT with mail piling up in it (the
    count/age path below still fires correctly for that case). The only way
    `new/` is absent while this watchdog's timer is enabled is a config
    error (typo'd --inbox, a never-provisioned agent, a vanished hub dir) —
    every one of those is a permanent silent false-negative if treated as
    benign. Per the verify-negative-claims discipline, "couldn't check" must
    never be silently reported as "checked, found nothing" — treating
    unreadable as alert but absent as OK would just relocate that same
    half-open asymmetry rather than close it."""


def read_backlog(inbox, *, now: float | None = None) -> tuple[int, float | None]:
    """(count, oldest_age_seconds) for `inbox`'s new/. oldest_age_seconds is
    None iff new/ exists and is empty. Raises BacklogUnreadable (never
    silently returns 0) if new/ is absent OR a permission/IO error prevents
    actually checking it — os.listdir()/os.stat() are used directly (not
    pathlib's is_dir()/iterdir(), which swallow OSError into False/[]) so
    that distinction is preserved. The ONLY OK/zero path is: the dir exists,
    is readable, and is empty-or-within-thresholds."""
    now = time.time() if now is None else now
    new_dir = Path(inbox) / "new"
    try:
        entries = os.listdir(new_dir)
    except FileNotFoundError as exc:
        raise BacklogUnreadable(f"inbox dir missing at {new_dir}") from exc
    except OSError as exc:
        raise BacklogUnreadable(f"could not list {new_dir}: {exc}") from exc

    names = []
    for name in sorted(entries):
        path = new_dir / name
        try:
            if path.is_file():
                names.append(name)
        except OSError as exc:
            raise BacklogUnreadable(f"could not stat {path}: {exc}") from exc
    if not names:
        return 0, None
    oldest_name = names[0]
    oldest_ts_ns = _parse_ts_ns(oldest_name)
    if oldest_ts_ns is not None:
        oldest_age = now - (oldest_ts_ns / 1_000_000_000)
    else:
        try:
            oldest_age = now - (new_dir / oldest_name).stat().st_mtime
        except OSError as exc:
            raise BacklogUnreadable(f"could not stat {new_dir / oldest_name}: {exc}") from exc
    return len(names), oldest_age


def read_failed(inbox) -> tuple[int, list[str]]:
    """(count, sorted names) for `inbox`'s failed/ dead-letter dir (T-1985).

    Unlike new/, the failed/ dir is created LAZILY by mailbox._quarantine on
    the first dead-letter (_ensure_dir), so an ABSENT failed/ is the normal
    healthy state — zero quarantines ever — and returns (0, []), NOT a
    can't-check alert. This is the deliberate asymmetry with read_backlog:
    new/ is provisioned-once so absent means misconfig, but failed/ absent
    means "nothing has ever dead-lettered here," which is exactly OK.

    A PermissionError / IO error on a dir that DOES exist is still a genuine
    couldn't-check and raises BacklogUnreadable (same verify-negative-claims
    posture as read_backlog — os.listdir directly, not pathlib swallowing)."""
    failed_dir = Path(inbox) / "failed"
    try:
        entries = os.listdir(failed_dir)
    except FileNotFoundError:
        return 0, []  # never quarantined here — healthy, not a miss
    except OSError as exc:
        raise BacklogUnreadable(f"could not list {failed_dir}: {exc}") from exc
    names = []
    for name in sorted(entries):
        path = failed_dir / name
        try:
            if path.is_file():
                names.append(name)
        except OSError as exc:
            raise BacklogUnreadable(f"could not stat {path}: {exc}") from exc
    return len(names), names


def quarantine_is_leftover(inbox, failed_names: list[str]) -> bool | None:
    """Has the bridge successfully ACKED anything SINCE the newest quarantine?

    True  = yes, so the drain loop is demonstrably working and these files are
            LEFTOVERS from an already-resolved fault.
    False = nothing acked since; the bridge may still be stuck on this.
    None  = cannot tell (unreadable cur/, or no acked messages at all).

    T-2187 defect (3): a quarantine does not clear itself, so a fault fixed
    hours ago keeps generating alerts about a condition that no longer exists.
    The ticket's proposed signal was "is the quarantine older than the current
    bridge session" — this is the same discriminator without any process or
    systemd introspection, which matters because the watchdog runs cross-account
    and cannot reliably read another seat's unit state. A message acked into
    cur/ AFTER the newest quarantine is direct evidence the drain loop
    recovered, and it is readable with the same permissions we already need.

    Fail-safe direction: None and False both mean "do not claim it is stale".
    """
    try:
        newest_q = max(
            (Path(inbox) / "failed" / n).stat().st_mtime for n in failed_names
        )
    except (OSError, ValueError):
        return None
    try:
        cur = Path(inbox) / "cur"
        acked = [(cur / n).stat().st_mtime for n in os.listdir(cur)
                 if not n.startswith(".")]
    except OSError:
        return None
    if not acked:
        return None
    return max(acked) > newest_q


def auto_clear_leftovers(inbox, failed_names: list[str], *, now: float | None = None,
                         dry_run: bool = False) -> tuple[list[str], list[str]]:
    """Move provably-resolved DLQ leftovers into failed/.cleared-<date>-auto-resolved/.

    T-3473: the DLQ alert ALREADY computes (quarantine_is_leftover is True) that a
    non-empty failed/ is leftovers from an already-resolved fault — the drain loop has
    acked messages since the newest quarantine. Until now a human still had to hand-move
    them, and the alert re-fired until they did (falling back to the operator when the recovery
    pane was unreachable). This performs the exact disposition the alert prescribes.

    MUST only be called when quarantine_is_leftover(...) is True — the caller enforces
    the fail-safe direction; this function does not re-derive resolvedness.

    Returns (cleared, failed_to_clear): names successfully moved, and names whose move
    raised. A non-empty failed_to_clear means the caller MUST still alert (never silently
    lose a leftover — a move that races/perm-fails falls through to the normal alert).

    move-not-delete (reversible; preserves message+cause for diagnostics). dry_run
    reports what WOULD move without touching the filesystem.
    """
    now = time.time() if now is None else now
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    dest_dir = Path(inbox) / "failed" / f".cleared-{day}-auto-resolved"
    cleared: list[str] = []
    failed_to_clear: list[str] = []
    if dry_run:
        _log(f"[dry-run] would auto-clear {len(failed_names)} leftover(s) -> {dest_dir}")
        return list(failed_names), []
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log(f"auto-clear: could not create {dest_dir} ({exc}); falling through to alert")
        return [], list(failed_names)
    for name in failed_names:
        src = Path(inbox) / "failed" / name
        try:
            os.replace(src, dest_dir / name)
            cleared.append(name)
        except OSError as exc:
            _log(f"auto-clear: move failed for {name} ({exc}); falling through to alert")
            failed_to_clear.append(name)
    if cleared:
        _log(f"auto-cleared {len(cleared)} resolved leftover(s) -> {dest_dir}")
    return cleared, failed_to_clear


def classify(count: int, oldest_age: float | None) -> str:
    """OK / ALERT from count + oldest-age thresholds (either alone breaches)."""
    if count > COUNT_WARN:
        return "ALERT"
    if oldest_age is not None and oldest_age > AGE_WARN_SECONDS:
        return "ALERT"
    return "OK"


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state))
    except OSError as e:
        print(f"[acp_bridge_backlog_watchdog] could not persist state: {e}", file=sys.stderr)


# T-2187 defect (1): a 30-minute cooldown is right for a TRANSIENT condition (a
# backlog that drains) and wrong for a PERSISTENT one. A quarantined file never
# goes away on its own, so one stuck message became an indefinite 30-minute alert
# loop — ~20 messages to the operator over 10 hours about a fault fixed in the first 7
# minutes. The fix is not a longer timer; it is to make the re-alert conditional on
# the SET CHANGING, with a long timer only as the "still broken, in case you forgot"
# backstop.
PERSISTENT_REALERT_SECONDS = int(
    os.environ.get("PANTHEON_ACP_DLQ_REALERT_SECONDS", str(24 * 60 * 60))
)


def _dlq_fingerprint(names: list[str]) -> str:
    """Stable digest of the quarantined-name SET — the identity of the condition.

    Set-based, not count-based: one file clearing while another arrives keeps the
    count at 1 but is a genuinely new condition worth re-alerting on."""
    return hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()[:16]


def _should_alert(agent: str, now: float, state: dict,
                  fingerprint: str | None = None) -> bool:
    """Throttle per key. Two regimes, and the difference is the T-2187 fix.

    WITHOUT a fingerprint (the transient backlog alert): unchanged — re-alert once
    the ordinary cooldown elapses, because the condition is expected to clear.

    WITH a fingerprint (the DLQ alert): re-alert only when the SET CHANGES, or after
    PERSISTENT_REALERT_SECONDS as a backstop. An unchanged set inside that window is
    the same fault already reported, and repeating it is pure noise on the operator.
    """
    last = state.get(agent, 0)
    if isinstance(last, dict):  # new-format record
        last_ts, last_fp = last.get("ts", 0), last.get("fp")
    else:                       # legacy bare-timestamp record; upgrade in place
        last_ts, last_fp = last, None
    if fingerprint is None:
        return (now - last_ts) >= ALERT_COOLDOWN_SECONDS
    if fingerprint != last_fp:
        return True  # the condition itself changed — always worth saying
    return (now - last_ts) >= PERSISTENT_REALERT_SECONDS


def _alert(text: str) -> None:
    """Direct-to-operator Bot-API alert — reuses the exact same transmit
    primitive as tools/disk_usage_monitor.py's `_alert`, so it delivers
    independent of Claude OAuth and self-suppresses under test (see
    tools.inbox_consumer._live_telegram_suppressed + tools/conftest.py)."""
    from tools.inbox_consumer import _send_telegram_alert
    _send_telegram_alert(text)


def _maybe_alert(agent: str, now: float, body: str, *, dry_run: bool,
                 state_key: str | None = None, sink=None, sink_desc: str = "the operator",
                 fingerprint: str | None = None) -> None:
    """Shared throttled-send path. `sink(body)` is the transmit primitive
    (default `_alert`, the direct-to-operator backlog channel). `state_key`
    lets a distinct alert class carry its OWN cooldown so, e.g., the failed/
    DLQ alert and the new/ backlog alert don't stomp each other's throttle
    (default: the agent name, the original backlog-cooldown key)."""
    key = state_key or agent
    transmit = sink or (lambda text: _alert(text))
    if dry_run:
        _log(f"[dry-run] would alert (key={key})")
        return
    state = _load_state()
    if _should_alert(key, now, state, fingerprint):
        transmit(body)
        state[key] = {"ts": now, "fp": fingerprint}
        _save_state(state)
        _log(f"alerted {sink_desc} (key={key})")
    else:
        why = "unchanged condition" if fingerprint else "cooldown"
        _log(f"alert suppressed ({why}, key={key})")


def _route_dlq_recovery(agent: str, body: str) -> None:
    """Route a dead-letter alert to Recovery (the ratified recovery seat) via the
    T-0722 per-agent failure-class path, NOT direct-to-operator and NOT back
    into the dead-lettered seat's own inbox. This is the fix for the
    self-referential observability defect (T-1985): mailbox._alert_owner sends
    the DLQ notice into the SAME inbox the bridge is failing to drain, where a
    genuinely-parked seat can never see it. route_failure_alert sends it to
    Recovery instead (and Recovery-self-failures fall through to direct the operator)."""
    from tools.inbox_consumer import route_failure_alert
    route_failure_alert(agent, body, vocab=f"acp-dlq ({agent})")


def build_dlq_body(agent: str, failed_count: int, failed_names: list[str],
                   leftover: bool | None) -> str:
    """The dead-letter alert text. Extracted so the CLAIMS IT MAKES are testable
    against the emitted string rather than against a grep of this file.

    T-2187 defect (2): the previous text asserted "anything here is genuinely
    poisoned content worth reading, not a false-positive heartbeat timeout." That
    was FALSE on its own first real instance — the quarantined message was an
    ordinary keep_going nudge that died on the CONTEXT-WEDGE path (ACP -32603
    "Prompt is too long"), not the heartbeat path T-1985 fixed. It asserted a
    category its own payload contradicted, to the operator, roughly twenty times. An
    alert that confidently mis-classifies its payload spends the reader's trust,
    which is a worse outcome than saying nothing.

    So this states what was OBSERVED and names what was NOT determined. The two
    dead-letter paths are genuinely indistinguishable from the filesystem, and the
    honest move is to say so and point at the log that does distinguish them.
    """
    if leftover is True:
        verdict = (
            "The bridge has ACKED messages SINCE the newest of these, so the drain "
            "loop is demonstrably working and these are LEFTOVERS from an "
            "already-resolved fault, not a live stall. Clear them (move to a "
            "failed/.cleared-<date>-<cause>/ subdir) rather than treating this as "
            "an incident."
        )
    elif leftover is False:
        verdict = (
            "Nothing has been acked since the newest of these, so the bridge may "
            "STILL be stuck on this content."
        )
    else:
        verdict = "Could not determine whether the bridge has acked anything since."
    return (
        f"[acp-dlq] {agent}: {failed_count} dead-lettered message(s) in the "
        f"ACP bridge inbox failed/ dir — a message that failed its handler "
        f"the DLQ-max times was quarantined out of the drain loop. "
        f"Inspect `<hub>/{agent}/failed/` (names: {', '.join(failed_names[:5])}"
        f"{' …' if failed_count > 5 else ''}). {verdict} "
        f"WHY IT DIED IS NOT DETERMINED HERE: quarantine happens on both the "
        f"poisoned-content path and the environment/context-wedge path (e.g. ACP "
        f"-32603 'Prompt is too long'), and the two are indistinguishable from the "
        f"filesystem — read acp-bridge-{agent}.log for the recorded handler error "
        f"before concluding the payload itself is bad."
    )


def check(agent: str, inbox, *, dry_run: bool, now: float | None = None) -> int:
    """Read the backlog; alert (throttled) on ALERT. Returns 1 if breached.

    An unreadable OR ABSENT inbox (BacklogUnreadable) is its OWN
    unconditional alert condition: "couldn't check" must never be reported
    as "checked, found nothing" (verify-negative-claims discipline). A
    piloted agent's inbox dirs are provisioned once and persist, so an
    absent dir here is a config error (typo'd --inbox, never-provisioned
    agent, vanished hub dir), not a benign empty state. It is NOT gated by
    the count/age thresholds below — any unreadable-or-absent result alerts
    regardless of COUNT_WARN/AGE_WARN_SECONDS. The ONLY OK/zero path is: the
    dir exists, is readable, and is empty-or-within-thresholds.
    """
    now = time.time() if now is None else now
    try:
        count, oldest_age = read_backlog(inbox, now=now)
    except BacklogUnreadable as exc:
        _log(f"agent={agent} ALERT: inbox unreadable-or-absent ({exc})")
        body = (
            f"\U0001f4ec [acp-bridge-backlog] {agent}: inbox could NOT be checked "
            f"({exc}). This is an UNKNOWN-STATE alert, not a healthy-empty result. "
            f"If the dir is missing: check for a typo'd --inbox, a never-provisioned "
            f"agent, or a vanished hub dir. If it exists but couldn't be read: most "
            f"likely the watchdog's own systemd unit is missing its "
            f"`sg pantheon-mailbox -c` wrap (T-1015) and can't traverse the Path-A hub, or "
            f"hub ownership/permissions changed. Check the watchdog unit's ExecStart "
            f"and the hub's group/mode before assuming the bridge itself is fine."
        )
        _maybe_alert(agent, now, body, dry_run=dry_run)
        return 1

    cls = classify(count, oldest_age)
    age_desc = f"{oldest_age / 60:.1f}min" if oldest_age is not None else "n/a"
    _log(f"agent={agent} {cls}: count={count} oldest_age={age_desc}")

    # T-1985: dead-letter observability. A non-empty failed/ means the bridge
    # quarantined a message — surfaced Recovery-side (not into the dead seat's own
    # inbox, and not direct-operator). Independent of the new/ backlog result:
    # a bridge can be draining new/ fine while old poison sits in failed/.
    breached = 1 if cls == "ALERT" else 0
    try:
        failed_count, failed_names = read_failed(inbox)
    except BacklogUnreadable as exc:
        _log(f"agent={agent} ALERT: failed/ unreadable ({exc})")
        _maybe_alert(
            agent, now,
            f"[acp-dlq] {agent}: dead-letter dir could NOT be checked ({exc}).",
            dry_run=dry_run, state_key=f"{agent}:failed",
            sink=lambda b: _route_dlq_recovery(agent, b), sink_desc="Recovery",
        )
        breached = 1
        failed_count = 0
    if failed_count:
        _log(f"agent={agent} DLQ: {failed_count} message(s) in failed/")
        leftover = quarantine_is_leftover(inbox, failed_names)

        # T-3473: when the leftovers are PROVABLY resolved (leftover is True) and
        # auto-clear is enabled, perform the disposition the alert already prescribes
        # — move them aside instead of nagging until a human does. The fail-safe
        # direction is never inverted: leftover False/None (possibly-live fault) is
        # NEVER auto-cleared, and a move that fails falls through to the normal alert.
        if AUTOCLEAR_ENABLED and leftover is True:
            cleared, failed_to_clear = auto_clear_leftovers(
                inbox, failed_names, now=now, dry_run=dry_run
            )
            if not failed_to_clear:
                # Every resolved leftover moved aside — log-only, no alert.
                _log(f"agent={agent} DLQ: auto-cleared {len(cleared)} resolved "
                     f"leftover(s); no alert")
                return breached
            # Some moves failed — re-alert on ONLY the ones that survived, so we
            # never silently lose a leftover.
            failed_names = failed_to_clear
            failed_count = len(failed_to_clear)

        dlq_body = build_dlq_body(agent, failed_count, failed_names, leftover)
        _maybe_alert(
            agent, now, dlq_body, dry_run=dry_run, state_key=f"{agent}:failed",
            sink=lambda b: _route_dlq_recovery(agent, b), sink_desc="Recovery",
            fingerprint=_dlq_fingerprint(failed_names),
        )
        breached = 1

    if cls == "ALERT":
        body = (
            f"\U0001f4ec [acp-bridge-backlog] {agent}: {count} message(s) unconsumed "
            f"in inbox/new/ (warn >{COUNT_WARN}), oldest {age_desc} old "
            f"(warn >{AGE_WARN_SECONDS / 60:.0f}min)."
            f"\nLikely a dead/stuck/quiesced tools/acp_bridge.py bridge process."
            f" Check `tmux capture-pane -t acp-bridge-{agent}` and the bridge's "
            f"systemd unit status."
        )
        _maybe_alert(agent, now, body, dry_run=dry_run)
    return breached


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--agent", required=True, help="agent persona (e.g. orchestrator) — state key + inbox default")
    p.add_argument("--inbox", default=None,
                   help="override inbox root (default: <mailbox-hub>/<agent>)")
    p.add_argument("--check", action="store_true", help="check backlog + alert on breach (timer path)")
    p.add_argument("--dry-run", action="store_true", help="classify + print, send no alert")
    args = p.parse_args(argv)

    if not (args.check or args.dry_run):
        print(
            "ERROR: refusing to run with no mode flag. Pass --check (timer path) "
            "or --dry-run to preview without alerting.",
            file=sys.stderr,
        )
        return 64

    inbox = args.inbox or str(MAILBOX_HUB / args.agent)
    check(args.agent, inbox, dry_run=args.dry_run)
    # Exit 0 always: a monitor must not fail its own timer. Breach state is in the log.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
