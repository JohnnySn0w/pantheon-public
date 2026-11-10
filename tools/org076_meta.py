"""ORG-076 BT3b -- in-band store-liveness meta-detector (RFC-004 sections 1, 2b), T-4709.

Serves ORG-076 fleet health-and-repair and D1: the store notices when it has quietly stopped doing
its job (cycles too rare, heartbeat writes failing silently, ingest backing up) and files ONE
diagnosable repair ticket instead of leaving a silent gap. It is a detector like any other: same
store, its own domain ``org076-store-liveness``, fed once per runner cycle by
``tools/org076_store_runner.py`` AFTER the guard passed, ingest + reconcile + sweeps finished.

Three detector instances share the domain (so each ``Healthy`` is scoped to ITS check, RFC-004 s1):

  org076-cycle-rate          STATE measure, not a one-shot event: the number of completed cycles in
                             the trailing ``WINDOW_SECS`` (expected 12 from the 5-min timer) vs
                             ``MIN_CYCLES``. After a gap the count stays low for several cycles, so
                             the N=3 open threshold CAN be reached (a one-shot "gap seen" event
                             would count once and never open). Broken("cycle-rate-low").
                             Warm-up: Healthy until the store has been observed for a full window,
                             so a fresh install does not page.
                             A cycle is recorded ONLY when it passed the guard and finished
                             reconcile (``record_cycle``); the list is trimmed to the window and
                             capped at ``MAX_STORED``.
  org076-heartbeat-readback  ``episode_store.write_heartbeat`` swallows its own errors. Read the file
                             back and require its epoch >= this cycle's start. Missing / stale /
                             unparseable => Broken("heartbeat-write-failing") (the measurement
                             succeeded and found a bad state); a read that cannot be done at all
                             (permission) => Unknown("heartbeat-readback-unreadable").
  org076-ingest-backlog      the oldest ingestable drop file (top-level ``*.json``, mtime) older
                             than ``BACKLOG_SECS`` (2 buckets) after this cycle's ingest while cycles
                             run => Broken("ingest-backlog"). Missing drop dir => Healthy (nothing
                             to back up); unreadable dir => Unknown("ingest-backlog-unmeasurable").

HONEST LIMITS. This is IN-BAND: it runs inside the process it judges. It CANNOT see the store totally
dead (timer stopped, process killed, host down) -- that is the external witness
(``tools/org076_heartbeat_check.py``, RFC-004 acceptance 6.9, another failure domain). It also cannot
run when the guard refuses a cycle (no heartbeat is written either, by design: a wedged store reads
stale externally and OnFailure alerts). If the store cannot push a ticket (git/GitHub down), the
episode accumulates durably and emits on recovery (RFC-004 s5 git row).

Tickets: requester ``recovery`` (detector owner / steward), routed to a repair persona on another harness
(Watchman@codex, ``repair_evidence.RECOVERY_CANDIDATE``) through the store's ``_route``
(RFC-004 s5).

Uninstall leg (ORG-068 Remove): delete this module and ``tools/test_org076_meta.py``; drop the
``org076_meta`` calls from ``org076_store_runner.run_cycle``; remove the runner-state key ``meta``
and the store files ``<state_dir>/episode-store/org076-store-liveness.json`` (+ ``.lock``).

Tests: python3 -m pytest tools/test_org076_meta.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import episode_store as es  # noqa: E402

DOMAIN = "org076-store-liveness"
SUBSYSTEM = "org076-store-liveness"
CYCLE_RATE, HB_READBACK, BACKLOG = "org076-cycle-rate", "org076-heartbeat-readback", "org076-ingest-backlog"
DETECTORS = (CYCLE_RATE, HB_READBACK, BACKLOG)
WINDOW_SECS = 3600
MIN_CYCLES = 8          # of the 12 a 5-min timer yields per window: tolerates a few slow/overrun cycles
BACKLOG_SECS = 3600     # 2 x 30-min probe buckets
MAX_STORED = 64


def specs(account: str, requester: str = "recovery") -> list:
    from tools import repair_evidence
    out = []
    for det in DETECTORS:
        out.append(es.DetectorSpec(
            detector=det, domain_key=DOMAIN, subsystem=SUBSYSTEM, requester=requester, harness="claude",
            account=account, candidates=(repair_evidence.RECOVERY_CANDIDATE,),
            any_live_fallback=(repair_evidence.RECOVERY_CANDIDATE,),  # T-5998 B(i)
            redactor=repair_evidence.make_default_redactor(account)))
    return out


# --------------------------------------------------------------------------- #
# cycle-rate (state in the runner's state.json under "meta")
# --------------------------------------------------------------------------- #
def record_cycle(state: dict, now_epoch: float) -> None:
    """Record ONE completed cycle (guard passed + reconcile finished). Trims to the window."""
    meta = state.setdefault("meta", {})
    meta.setdefault("first_cycle_at", now_epoch)
    cycles = [t for t in meta.get("cycles", []) if now_epoch - t <= WINDOW_SECS] + [now_epoch]
    meta["cycles"] = cycles[-MAX_STORED:]


def cycle_rate_verdict(state: dict, now_epoch: float):
    meta = state.get("meta") or {}
    first = meta.get("first_cycle_at")
    if first is None or now_epoch - first < WINDOW_SECS:
        return es.Healthy()  # warm-up: the store has not been observed for a full window yet
    count = len([t for t in meta.get("cycles", []) if now_epoch - t <= WINDOW_SECS])
    if count >= MIN_CYCLES:
        return es.Healthy()
    return es.Broken("cycle-rate-low",
                     f"{count} completed runner cycles in the trailing {WINDOW_SECS}s; expected about "
                     f"{WINDOW_SECS // 300} from the 5-minute timer (threshold {MIN_CYCLES}).\n"
                     f"The timer was stopped/failing or cycles are being refused or overrunning; "
                     f"check the ORG-076 store-runner timer on the recovery seat (`systemctl --user list-timers`).\n")


# --------------------------------------------------------------------------- #
# heartbeat read-back
# --------------------------------------------------------------------------- #
def heartbeat_verdict(path: Path, cycle_start_epoch: float):
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return es.Broken("heartbeat-write-failing", f"heartbeat file {path} does not exist after a full cycle\n")
    except OSError as exc:
        return es.Unknown("heartbeat-readback-unreadable", f"cannot read {path}: {type(exc).__name__}: {exc}\n")
    try:
        epoch = json.loads(raw)["epoch"]
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            raise ValueError("epoch is not an integer")
    except (ValueError, KeyError, TypeError) as exc:
        return es.Broken("heartbeat-write-failing", f"heartbeat file {path} is unparseable: {exc}\n")
    if epoch < int(cycle_start_epoch):
        return es.Broken("heartbeat-write-failing",
                         f"heartbeat file {path} was last written at epoch {epoch}, before this cycle "
                         f"started ({int(cycle_start_epoch)}): write_heartbeat is failing silently\n")
    return es.Healthy()


# --------------------------------------------------------------------------- #
# ingest backlog
# --------------------------------------------------------------------------- #
def backlog_verdict(drop: Path, now_epoch: float):
    try:
        if not Path(drop).is_dir():
            return es.Healthy()
        oldest = None
        for p in Path(drop).iterdir():
            if p.name.endswith(".json") and not p.name.startswith(".") and p.is_file():
                m = p.stat().st_mtime
                oldest = m if oldest is None else min(oldest, m)
    except OSError as exc:
        return es.Unknown("ingest-backlog-unmeasurable", f"cannot scan {drop}: {type(exc).__name__}: {exc}\n")
    if oldest is not None and now_epoch - oldest > BACKLOG_SECS:
        return es.Broken("ingest-backlog",
                         f"oldest un-ingested drop file is {int(now_epoch - oldest)}s old (> {BACKLOG_SECS}s) "
                         f"while runner cycles are running: ingest is held (busy domain / unreadable file / "
                         f"observe errors)\n")
    return es.Healthy()


def feed(meta_specs: list, *, state: dict, drop: Path, heartbeat: Path, cycle_start_epoch: float,
         now_epoch: float, ports: "es.Ports", state_dir: Any) -> list:
    """Observe the three verdicts. Returns the (detector, status) pairs. Each observe is independent: one
    failing does not stop the others (the caller turns exceptions into runner errors)."""
    by = {s.detector: s for s in meta_specs}
    verdicts = {CYCLE_RATE: cycle_rate_verdict(state, now_epoch),
                HB_READBACK: heartbeat_verdict(heartbeat, cycle_start_epoch),
                BACKLOG: backlog_verdict(drop, now_epoch)}
    out = []
    for det in DETECTORS:
        res = es.observe(by[det], verdicts[det], ports=ports, state_dir=state_dir)
        out.append((det, res.status))
    return out
