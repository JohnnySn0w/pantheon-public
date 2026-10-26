"""ORG-076 BT3b -- per-seat sample FRESHNESS (recovery steward non-blocking item 1), T-4709.

Serves ORG-076 and D1: a seat whose drop-file writes fail silently (full/unwritable drop dir, wrong
mode, seat-side bug) must become VISIBLE. Seats only ever append drop files; if they stop, nothing
downstream is wrong-looking -- the quota-probe readability domain just stops receiving samples. This
check closes that: "no drop record of ANY kind from seat X for more than 2 buckets (60 min) while X's
quota unit ran" => ``Unknown("sample-freshness-silent:<persona>")`` on the domain
``org076-sample-freshness:<persona>`` (one detector instance per seat; requester ``recovery``, routed to
Consultant@codex like every ORG-076 ticket).

Evidence that the quota unit ran (cross-account, no new grants). Recovery (claude-fallback) is in group
``systemd-journal``; ``/var/log/journal/<machine>/user-<uid>.journal`` is mode 0640 root:systemd-journal
(SplitMode=uid), so recovery can query ANY seat's user-manager unit lifecycle:
    journalctl _UID=<uid> USER_UNIT=pantheon-persona-quota@<persona>.service \\
        MESSAGE_ID=7d4958e842da4a758f6c1cdc7b36dcc5 --since=@<epoch> -o json
(``7d4958e8...`` is systemd's JOB_START "Starting ..." record; ``USER_UNIT`` carries the template
instance). DEPENDENCY, stated plainly: this REQUIRES the running account to be in ``systemd-journal``
(or ``adm``). A reader outside the group gets rc=0 and EMPTY output -- indistinguishable from "the unit
never ran". The default port therefore runs a POSITIVE CONTROL (``journalctl _UID=<uid> -n 1`` must
return at least one record, any age); no record => the journal is not readable => ``Unknown(
"sample-freshness-journal-unreadable:<persona>")`` (its own fault_id), NEVER Healthy (fail toward
visibility; RFC-004 6.14 spirit). The journal is an injectable port (``JournalPort``); tests use
fixtures captured from a real user journal, never the real journal.

Two false-positive traps, handled:
  1. quota-check legitimately writes NO measured sample on no-measurement runs (non-claude primary,
     operation-busy, transition/fallback no-op, unsupported). With the dark switch ON the seat writes a
     minimal ``not-measured`` drop record on those runs too (``quota_probe_detector.NOT_MEASURED``);
     the runner counts it for freshness and never feeds it to ``observe()``. "Unit ran, but zero drop
     records of any kind" is therefore a drop-write failure.
  2. A seat with the switch OFF still runs its quota unit and writes nothing, by design. Enrollment:
     freshness applies only to personas the runner has seen at least one drop record from (runner state
     ``personas``). ``org076_store_runner.py --forget-persona X`` de-enrolls a seat (Uninstall leg).

Verdict (``verdict``): fresh (last record <= 60 min ago) => Healthy WITHOUT touching the journal;
stale and at least one unit start in (last_record, now - 10 min] => Unknown(silent) (the 10-minute
grace covers an in-flight run, the unit's 5-min timeout and runner lag); stale with no start => Healthy
(vacuous: indistinguishable from a down seat, which is the quota/BT5 domain, not this check).

HONEST LIMITS. (a) A seat whose drop writes fail from its very FIRST sample is never enrolled -- recovery's
supervised first-fire covers that. (b) A seat whose user manager is down has no start records and is
not flagged here. (c) A previously enrolled seat that later turns the switch OFF while its unit keeps
running reads as silent until ``--forget-persona``. (d) The "last record" clock is the RUNNER's ingest
time, so seat clock skew cannot fake freshness.

Uninstall leg (ORG-068 Remove): ``--forget-persona`` for each enrolled seat (closes its freshness
episodes, removes enrollment); delete this module and ``tools/test_org076_sample_freshness.py``; drop
the freshness calls from ``org076_store_runner.run_cycle``; remove store files
``<state_dir>/episode-store/org076-sample-freshness-*.json`` (+ ``.lock``).

Tests: python3 -m pytest tools/test_org076_sample_freshness.py
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, Protocol

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import episode_store as es  # noqa: E402

DETECTOR = "org076-sample-freshness"
DOMAIN_PREFIX = "org076-sample-freshness"
STALE_SECS = 3600          # 2 x 30-min probe buckets
GRACE_SECS = 600
UNIT_TEMPLATE = "pantheon-persona-quota@{persona}.service"
JOB_START_MESSAGE_ID = "7d4958e842da4a758f6c1cdc7b36dcc5"  # systemd JOB_START ("Starting ...")
JOURNAL_TIMEOUT_SECS = 15


def domain_key(persona: str) -> str:
    return f"{DOMAIN_PREFIX}:{persona}"


def fault_silent(persona: str) -> str:
    return f"sample-freshness-silent:{persona}"


def fault_unreadable(persona: str) -> str:
    return f"sample-freshness-journal-unreadable:{persona}"


def spec_for(persona: str, account: str, requester: str = "recovery"):
    """``account`` is the account the DETECTOR runs under (recovery's), for the evidence redactor."""
    from tools import repair_evidence
    return es.DetectorSpec(
        detector=DETECTOR, domain_key=domain_key(persona), subsystem=DETECTOR, requester=requester,
        harness="claude", account=account,
        candidates=(repair_evidence.RECOVERY_CANDIDATE,),
        redactor=repair_evidence.make_default_redactor(account))


# --------------------------------------------------------------------------- #
# Journal port
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class JournalResult:
    starts: list                 # epoch seconds (float) of unit JOB_START records after ``since``
    control_ok: bool = True      # the positive control returned >= 1 record
    error: "str | None" = None   # the query itself failed (rc != 0, timeout, spawn error)


class JournalPort(Protocol):
    def starts(self, uid: int, unit: str, since_epoch: float) -> JournalResult: ...


Runner = Callable[[list], "tuple[int, str, str]"]  # argv -> (returncode, stdout, stderr)


def _subprocess_runner(argv: list) -> "tuple[int, str, str]":
    r = subprocess.run(argv, capture_output=True, text=True, timeout=JOURNAL_TIMEOUT_SECS)
    return r.returncode, r.stdout, r.stderr


def parse_records(stdout: str) -> list:
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            out.append(json.loads(line))
    return out


class JournalctlPort:
    """Default port: ``journalctl`` (the running account must be in ``systemd-journal``/``adm``)."""

    def __init__(self, runner: Runner = _subprocess_runner, extra: tuple = ()):
        self._run = runner
        self._extra = list(extra)

    def starts(self, uid: int, unit: str, since_epoch: float) -> JournalResult:
        base = ["journalctl", *self._extra, "-o", "json", "--output-fields=__REALTIME_TIMESTAMP", "--no-pager"]
        try:
            rc, out, err = self._run(base + [f"_UID={uid}", f"USER_UNIT={unit}",
                                             f"MESSAGE_ID={JOB_START_MESSAGE_ID}", f"--since=@{int(since_epoch)}"])
            if rc != 0:
                return JournalResult([], False, f"journalctl rc={rc}: {(err or out).strip()[:200]}")
            starts = [int(r["__REALTIME_TIMESTAMP"]) / 1e6 for r in parse_records(out)]
            crc, cout, cerr = self._run(base + [f"_UID={uid}", "-n", "1"])
            if crc != 0:
                return JournalResult([], False, f"journalctl control rc={crc}: {(cerr or cout).strip()[:200]}")
            return JournalResult(starts, control_ok=bool(parse_records(cout)))
        except Exception as exc:  # noqa: BLE001 -- timeout, spawn failure, bad JSON: unreadable, not healthy
            return JournalResult([], False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #
def resolve_uid(account: str) -> "int | None":
    import pwd
    try:
        return pwd.getpwnam(account).pw_uid
    except KeyError:
        return None


def verdict(persona: str, *, seen_epoch: float, now_epoch: float, account: str,
            journal: JournalPort, uid_of: Callable[[str], "int | None"] = resolve_uid):
    age = now_epoch - seen_epoch
    if age <= STALE_SECS:
        return es.Healthy()
    unit = UNIT_TEMPLATE.format(persona=persona)
    uid = uid_of(account)
    if uid is None:
        return es.Unknown(fault_unreadable(persona), f"cannot resolve a uid for account {account!r}\n")
    try:
        res = journal.starts(uid, unit, seen_epoch)
    except Exception as exc:  # noqa: BLE001
        return es.Unknown(fault_unreadable(persona), f"journal port raised {type(exc).__name__}: {exc}\n")
    if res.error or not res.control_ok:
        return es.Unknown(fault_unreadable(persona),
                          f"cannot read {account}'s user journal ({res.error or 'positive control returned no records'}); "
                          f"the reader must be in group systemd-journal. Last drop record from {persona} was "
                          f"{int(age)}s ago.\n")
    ran = sorted(s for s in res.starts if seen_epoch < s <= now_epoch - GRACE_SECS)
    if not ran:
        return es.Healthy()
    return es.Unknown(fault_silent(persona),
                      f"no drop record of any kind from {persona} for {int(age)}s (> {STALE_SECS}s) but {unit} "
                      f"started {len(ran)} time(s) since (last start {int(now_epoch - ran[-1])}s ago).\n"
                      f"Seat-side drop writes are failing silently: check the drop dir "
                      f"(/var/lib/pantheon/org076/samples: mode 2775 group pantheon-shared) and the seat's "
                      f"PANTHEON_ORG076_QUOTA_DETECTOR / stderr in `journalctl --user -u {unit}`.\n")


def feed_all(state: dict, *, ports: "es.Ports", state_dir, now_epoch: float, runner_account: str,
             journal: JournalPort, uid_of: Callable[[str], "int | None"] = resolve_uid, errors: "list | None" = None) -> list:
    """Observe one freshness verdict per ENROLLED persona (runner ``state["personas"]``). A persona with no
    ``seen`` entry is stamped ``now`` first (conservative enrollment). The caller saves ``state``.
    Returns [(persona, status)]; per-persona failures are appended to ``errors`` and do not stop the rest."""
    out = []
    for persona, account in sorted(state["personas"].items()):
        seen = state["seen"].setdefault(persona, now_epoch)
        try:
            v = verdict(persona, seen_epoch=seen, now_epoch=now_epoch, account=account, journal=journal,
                        uid_of=uid_of)
            res = es.observe(spec_for(persona, runner_account), v, ports=ports, state_dir=state_dir)
            out.append((persona, res.status))
        except Exception as exc:  # noqa: BLE001
            if errors is not None:
                errors.append(f"freshness {persona}: {type(exc).__name__}: {exc}")
    return out
