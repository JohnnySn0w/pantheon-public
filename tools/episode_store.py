#!/usr/bin/env python3
"""ORG-076 episode store -- detect -> one episode -> exactly one repair ticket (T-4707 / BT1).

Implements RFC-004 §1 (three verdicts, N-of-M episode machine), §2 (durable
single-writer store, ``episode_id`` idempotency key, write-ahead order, delivery
phases NONE -> COMMIT_INFLIGHT -> LOCAL_COMMIT -> PUSH_INFLIGHT -> PUSHED -> VERIFIED
persisted at every transition, startup reconciliation against the COMMITTED corpus,
suppression) and the emission half of §3(a). Serves D1: overnight faults get a
diagnosable ticket instead of a page that waits for a human.

Invariants (the reason each exists is in the RFC; the enforcement is structural):
  * One JSON document per detector domain at ``<state_dir>/episode-store/<slug>.json``,
    rewritten atomically (temp + fsync + ``os.replace`` + dir fsync) under a per-domain
    ``flock``. One rename == one transition, so every state/phase change is durable.
  * ``episode_id`` is minted and fsync'd at OPEN, before any emission attempt.
  * delivered == committed AND pushed. ``EMITTED`` is set only together with phase
    ``VERIFIED`` (episode_id seen on origin/main).
  * The idempotent-create check is a TRI-STATE query (FOUND / ABSENT / UNKNOWN,
    ``tools/_episode_corpus.py``) preceded by a fetch. UNKNOWN (git down, bad ref)
    fails closed: nothing is filed, the phase stays durable, the next cycle resumes
    (§6.7). The working tree / ``ls tickets/inbox`` is never consulted.
  * The domain flock is held across the delivery drive. Acquisition has a timeout; on
    timeout the cycle returns ``busy_suppressed`` instead of wedging.
  * Heartbeat (ORG-048 D2a, Recovery steward constraint ORG-076 §4): every ``observe()`` /
    ``reconcile()`` writes ``<state_dir>/heartbeat/org076-episode-store.json`` so that
    killing this process makes the file go stale (RFC-004 §6.9). The external watcher is
    BT3/ORG-097 and is NOT built here. Nothing here reads or requires any per-agent
    heartbeat file.

Seams (documented contract for later tranches)
  BT2 (quota-probe detector):  ``observe(spec, verdict, ports=..., state_dir=...)``;
                               wired by ``tools/quota_probe_detector.py`` (dark switch).
  BT3 (reconciler/meta):       ``reconcile(specs, ...)`` (startup), plus the mutations
                               ``mark_recovered_before_repair`` / ``close_stale_and_reopen``
                               / ``reemit_orphan``. BT3b (T-4709) built on these seams WITHOUT
                               changing them: the periodic sweeps live in
                               ``tools/org076_reconciler.py`` (using the extra seams
                               ``arm_stale_watch`` / ``disarm_stale_watch`` / ``mark_rbr_applied``,
                               and ``_apply_verdict``'s ``stale_watch.nh_run`` counter), the
                               in-band meta-detector in ``tools/org076_meta.py``, per-seat sample
                               freshness in ``tools/org076_sample_freshness.py``, and the external
                               witness reader in ``tools/org076_heartbeat_check.py``.
                               ``close_domain_episodes`` serves the runner's ``--forget-persona``.
                               ``close_stale_and_reopen`` pages ONCE (``chain_paged``) when the
                               predecessor chain reaches ``REOPEN_CHAIN_PAGE_DEPTH`` (3).
  ORG-096 (rebase edge):       ``Ports.on_nonff`` / ``Ports.on_recover`` -- WIRED in BT2 by
                               ``make_rebase_edge(repo=, state_dir=, page=)``, which runs
                               ``delivery_rebase_controller._deliver_locked`` /
                               ``_recover_locked`` under ONE store-held git_mutation_lock
                               (the lock is not re-entrant), after a strict push-scope guard,
                               with controller state under ``<state_dir>/delivery-rebase``.
                               The outcome string drives the store (PUSHED / ADOPTED_ORIGIN /
                               UNVERIFIED -> PUSHED then verify; RE_ENTER -> bounded
                               ``rebase_retries``, not a push failure; TYPED_FAILURE / PAGED /
                               refusal / exception -> counted push failure + the page bound).
                               The ``Ports`` defaults stay ``None`` (BT1 behaviour: count and
                               page after a bound); the BT2 hook injects the edge.
                               Recovery (``on_recover``) runs the same strict guard BEFORE
                               ``_recover_locked`` (its 3b / case-iii branches push HEAD:main);
                               a RE_ENTER falls through to ``_deliver_locked`` in-cycle.
  Push scope (BT2):            before ANY push the store checks the episode is the only
                               local-ahead commit it would carry. Default path pushes
                               ``<episode_sha>:main`` and requires ``origin/main..<sha> ==
                               [sha]``; the rebase path (controller pushes HEAD:main)
                               requires ``origin/main..HEAD == [sha]``. Otherwise nothing is
                               pushed, ``last_error`` says "local-ahead commits besides
                               episode; needs dedicated clone (BT3)", and it counts toward
                               the page bound. STILL NEEDED FOR LIVE ENABLE (BT3): choose the
                               run seat / a dedicated clone with no other writers, real
                               liveness + page (BT4), and the external watcher.
  BT4 (grants):                ``Ports.liveness`` / ``Ports.page`` are injected callables;
                               the defaults are placeholders (assume-live / stderr).

Deployment (BT3a, T-4720): ONE store under recovery's seat, run by ``tools/org076_store_runner.py`` from
a dedicated clone (default state dir, no ``PANTHEON_STATE_DIR``); seats only drop sample files.
``observe(..., sample_key=)`` makes ingest replay-safe (the ``INGESTED_KEYS_KEPT`` = 256 window is sized
by ADJACENCY, not backlog: a replay after a crash is always the file being processed, whose key was just
added; evicting it needs 256 further distinct buckets, ~5.3 days of later samples from the same persona,
which cannot be pending ahead of it -- proved by tools/test_org076_bt3b_carryovers.py); ``Ports.heartbeat_path`` /
``PANTHEON_ORG076_HEARTBEAT_PATH`` place the heartbeat outside the state dir. Every path here
resolves from ``state_dir=`` or ``PANTHEON_STATE_DIR`` (default ``~/.local/state/pantheon``); there
is no hard-coded home or /tmp path.

Uninstall leg (ORG-068 Remove): delete modules ``tools/episode_store.py``,
``tools/repair_evidence.py``, ``tools/_episode_corpus.py``; remove the state dirs
``<state_dir>/episode-store/`` and ``<state_dir>/heartbeat/org076-episode-store.json``;
drop the ``--repair-fields-json`` flag from ``tools/ticket.py file`` and ``recovered_before_repair_mutation``
(BT3b). BT1 ships no unit or timer, so there is nothing to disable; the BT3b legs are in
``tools/org076_store_runner.py`` and the modules named above.

Tests: python3 -m pytest tools/test_episode_store.py tools/test_episode_store_kill.py
"""
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import _episode_corpus, _git_utils  # noqa: E402
from tools import repair_evidence  # noqa: E402
from tools.heartbeat_write import _atomic_write_json as _hb_atomic_write  # noqa: E402
from tools.heartbeat_write import build_heartbeat  # noqa: E402

SCHEMA_VERSION = 1

# Mirror of tools/persona_bringup.SOFT_FAILURE_ALERT_STREAK (the proven calibration the
# episode open-threshold inherits, RFC-004 §1). Mirrored rather than imported: importing
# persona_bringup costs ~1s and a 2.5k-line module for one int. A test asserts equality, so
# there is a single source of truth.
DEFAULT_N = 3
DEFAULT_M = 2
DEFAULT_ATTEMPTS_BUDGET = 2  # RFC-004 §3(a)

HEARTBEAT_NAME = "org076-episode-store"
RESOLVED_HISTORY = 50
RAW_EVIDENCE_LIMIT = 256 * 1024  # store-side bound on raw evidence kept per episode
MAX_PUSH_FAILURES_BEFORE_PAGE = 5
MAX_REBASE_RETRIES = 5  # RE_ENTER outcomes tolerated before they count as a hard push failure
SCOPE_REFUSAL = "local-ahead commits besides episode; needs dedicated clone (BT3)"
LOCK_TIMEOUT_SECS = 30.0
INDEX_LOCK_STALE_SECS = 10.0
_MAX_DRIVE_STEPS = 12
# Verify -> re-push loop (BT2 reviewer note 1, T-4720): an episode that is neither local-ahead nor
# on origin cycles PUSH_INFLIGHT -> PUSHED -> verify. Counted AT MOST ONCE per drive (= once per
# observe()/reconcile() cycle) so one cycle, which can spin up to _MAX_DRIVE_STEPS // 2 loops, can
# never trip the page alone; the page needs this many CONSECUTIVE cycles and fires once.
VERIFY_LOOP_PAGE_THRESHOLD = 5
INGESTED_KEYS_KEPT = 256  # sample dedupe keys remembered per domain doc (BT3a)
HEARTBEAT_PATH_ENV = "PANTHEON_ORG076_HEARTBEAT_PATH"

NONE = "NONE"
COMMIT_INFLIGHT = "COMMIT_INFLIGHT"
LOCAL_COMMIT = "LOCAL_COMMIT"
PUSH_INFLIGHT = "PUSH_INFLIGHT"
PUSHED = "PUSHED"
VERIFIED = "VERIFIED"
PHASES = (NONE, COMMIT_INFLIGHT, LOCAL_COMMIT, PUSH_INFLIGHT, PUSHED, VERIFIED)

OPEN = "OPEN"
EMITTED = "EMITTED"
RESOLVED = "RESOLVED"

# kill-injection points (Ports.kill_hook), in delivery order
KILL_POINTS = (
    "after_open_before_emit", "after_commit_inflight_persist", "after_file_before_local_commit",
    "after_local_commit_persist", "before_push", "after_push_before_pushed",
    "after_pushed_persist", "after_verified_persist",
)


# --------------------------------------------------------------------------- #
# Verdicts (RFC-004 §1)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Healthy:
    """Asserts the detector instance's FULL domain is healthy this cycle (no fault_id)."""


@dataclasses.dataclass(frozen=True)
class Broken:
    """The measurement SUCCEEDED and found a bad state."""
    fault_id: str
    evidence: "str | bytes" = ""


@dataclasses.dataclass(frozen=True)
class Unknown:
    """The measurement itself FAILED (first-class, not an error path)."""
    fault_id: str
    evidence: "str | bytes" = ""


@dataclasses.dataclass(frozen=True)
class DetectorSpec:
    """Static description of one detector instance (its domain, thresholds, provenance)."""
    detector: str
    domain_key: str
    subsystem: str
    requester: str
    n: int = DEFAULT_N
    m: int = DEFAULT_M
    harness: str = ""            # the faulted harness (routing prefers another one)
    account: str = ""            # OS account the detector runs under (deferred => same-account)
    candidates: tuple = ()       # tuple[repair_evidence.Candidate, ...] repair duty holders
    any_live_fallback: tuple = ()  # T-5998 B(i): consulted last, any_live only, never for deferred evidence
    redactor: Callable[[str], str] = repair_evidence.default_redactor
    invariant_list_id: str | None = None
    invariant_list_digest: str | None = None
    action: str | None = None
    attempts_budget: int = DEFAULT_ATTEMPTS_BUDGET


@dataclasses.dataclass
class FileResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclasses.dataclass
class PushResult:
    ok: bool
    nonff: bool = False
    detail: str = ""


def _log(msg: str) -> None:
    print(f"[episode_store] {msg}", file=sys.stderr)


def _default_page(subject: str, body: str) -> None:
    # Placeholder (BT3/BT4 wire the real page path). Loud on stderr, never silent.
    _log(f"PAGE: {subject}: {body}")


def _default_push(repo: Path, refspec: str = "HEAD:main", mirror_url=_git_utils._UNSET) -> PushResult:
    """Push ``refspec`` (the store passes ``<episode_sha>:main``) with attempts=1.

    Rebase-edge containment: ``push_with_retry`` fetches and rebases onto origin on a
    non-ff rejection, but only when it has attempts left. With attempts=1 it returns
    (False, "...non-ff exhausted...") at the exhaustion check BEFORE the fetch/rebase, and
    no conflict_resolver is passed, so BT1 never rebases. The pre-push hook's own rebase (if
    one is installed) is aborted by push_with_retry's T-4380 path. ORG-101 mirror legs stay
    governed by the manifest (inert unless a writable mirror is configured). A non-ff result
    is surfaced to ``Ports.on_nonff`` -- the ORG-096 seam.
    """
    ok, detail = _git_utils.push_with_retry(repo, refspec=refspec, attempts=1, mirror_url=mirror_url)
    nonff = (not ok) and any(m in detail.lower() for m in _git_utils._NON_FF_MARKERS)
    return PushResult(ok=ok, nonff=nonff, detail=detail)


def _make_default_filer(repo: Path) -> Callable[[list[str]], FileResult]:
    def _filer(argv: list[str]) -> FileResult:
        r = subprocess.run([sys.executable, str(repo / "tools" / "ticket.py"), *argv],
                           cwd=repo, capture_output=True, text=True)
        return FileResult(r.returncode, r.stdout, r.stderr)
    return _filer


@dataclasses.dataclass
class Ports:
    """Injectable seams. Everything has a production default; tests inject fakes."""
    repo: Path | None = None
    filer: Callable[[list[str]], Any] | None = None      # argv -> FileResult-like
    # An injected pusher receives ONLY ``repo``. The store's scope guard checks repository state
    # before calling it but cannot constrain what a custom pusher pushes; production leaves this
    # None and gets the default ``<episode_sha>:main`` push.
    pusher: Callable[[Path], PushResult] | None = None
    liveness: Callable[[Any], bool] = lambda c: True      # BT4 supplies the real lookup
    page: Callable[[str, str], None] = _default_page
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    rand: Callable[[], str] = lambda: secrets.token_hex(4)
    kill_hook: Callable[[str], None] = lambda point: None  # tests: os._exit at a named point
    on_nonff: Callable[["dict", PushResult], str] | None = None  # ORG-096 seam (make_rebase_edge)
    on_recover: Callable[["dict"], str] | None = None  # ORG-096 restart recovery (make_rebase_edge)
    mirror_url: Any = _git_utils._UNSET  # default push only; None disables the ORG-101 mirror leg
    lock_timeout: float = LOCK_TIMEOUT_SECS
    index_lock_stale_secs: float = INDEX_LOCK_STALE_SECS
    git: Any = _git_utils
    fetch: bool = True
    heartbeat_path: Path | None = None  # BT3a: heartbeat file override, separate from state_dir
    # BT3a: opaque token the RUNNER sets once per run_cycle. A runner cycle runs one observe() per
    # ingested file plus a reconcile(), each with its own _drive; with a token the verify-loop counter
    # advances once per TOKEN. None (direct library callers) => once per drive/observe/reconcile call.
    cycle_token: str | None = None

    def resolved_repo(self) -> Path:
        return self.repo if self.repo is not None else _git_utils.REPO_ROOT


@dataclasses.dataclass
class CycleResult:
    status: str                      # counted|opened|suppressed|resolved|healthy|busy_suppressed|reconciled
    episode_ids: list = dataclasses.field(default_factory=list)
    pending: list = dataclasses.field(default_factory=list)   # episodes still mid-delivery
    corrupt: list = dataclasses.field(default_factory=list)   # reconcile: (domain, reason) skipped as corrupt


# --------------------------------------------------------------------------- #
# Paths + durable primitives
# --------------------------------------------------------------------------- #
def resolve_state_dir(state_dir: Path | str | None = None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    env = os.environ.get("PANTHEON_STATE_DIR")
    return Path(env) if env else Path.home() / ".local" / "state" / "pantheon"


def _slug(text: str, limit: int = 24) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "x")[:limit].strip("-") or "x"


def store_path(domain_key: str, state_dir: Path | str | None = None) -> Path:
    return resolve_state_dir(state_dir) / "episode-store" / f"{_slug(domain_key, 64)}.json"


def heartbeat_path(state_dir: Path | str | None = None, heartbeat_file: Path | str | None = None) -> Path:
    """Where the heartbeat lives. Precedence: ``heartbeat_file`` (``Ports.heartbeat_path``) >
    ``$PANTHEON_ORG076_HEARTBEAT_PATH`` (a full file path) > ``<state_dir>/heartbeat/<name>.json``.
    The override is independent of ``state_dir`` (recovery steward constraint ORG-076 §4 #3: the
    state dir lives in recovery's 750 home, the heartbeat must be readable by other accounts)."""
    if heartbeat_file is not None:
        return Path(heartbeat_file)
    env = os.environ.get(HEARTBEAT_PATH_ENV)
    if env:
        return Path(env)
    return resolve_state_dir(state_dir) / "heartbeat" / f"{HEARTBEAT_NAME}.json"


def write_heartbeat(ports: Ports, state_dir: Path | str | None = None) -> None:
    """ORG-048 D2a heartbeat. Failure never fails the cycle. An overridden path is written
    mode 0644 so it is readable by other accounts; the default path keeps mkstemp's 0600."""
    try:
        payload = build_heartbeat(HEARTBEAT_NAME, source="episode_store", now=ports.clock(), pid=os.getpid())
        overridden = ports.heartbeat_path is not None or bool(os.environ.get(HEARTBEAT_PATH_ENV))
        _hb_atomic_write(heartbeat_path(state_dir, ports.heartbeat_path), payload,
                         **({"mode": 0o644} if overridden else {}))
    except Exception as exc:  # noqa: BLE001
        _log(f"heartbeat write failed (continuing): {type(exc).__name__}: {exc}")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


class StoreCorruptionError(Exception):
    pass


def _load(path: Path, domain_key: str) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"schema": SCHEMA_VERSION, "domain_key": domain_key, "runs": {}, "episodes": {}}
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise StoreCorruptionError(f"{path}: unreadable store JSON: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA_VERSION:
        raise StoreCorruptionError(f"{path}: schema mismatch")
    # T-5999 review (MED): ``store_path`` slugs ':' '_' '-' alike, so two distinct domain keys can map to ONE
    # file (e.g. ``mail:stale-recovery`` / ``mail-stale:recovery``). Fail closed: a document is only ever opened for
    # the domain it was created for; any alias (first-writer-wins in either order) raises.
    if doc.get("domain_key") != domain_key:
        raise StoreCorruptionError(f"{path}: holds domain {doc.get('domain_key')!r}, not {domain_key!r} "
                                   f"(store-path slug alias)")
    return doc


def _save(path: Path, doc: dict) -> None:
    resolved = sorted((e for e in doc["episodes"].values() if e["state"] == RESOLVED),
                      key=lambda e: e.get("resolved_at") or "")
    for old in resolved[:-RESOLVED_HISTORY]:
        doc["episodes"].pop(old["episode_id"], None)
    _atomic_write(path, doc)


class _Busy(Exception):
    pass


class _DomainLock:
    """Per-domain single-writer flock with a timeout (amendment F)."""

    def __init__(self, store: Path, timeout: float):
        self.lock_path = store.with_suffix(".lock")
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self) -> "_DomainLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + max(self.timeout, 0.0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fd = fd
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise _Busy() from None
                time.sleep(0.02)

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)


def _iso(ports: Ports) -> str:
    return ports.clock().astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def mint_episode_id(spec: DetectorSpec, ports: Ports) -> str:
    stamp = ports.clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"ep-{_slug(spec.domain_key)}-{stamp}-{ports.rand()}"


def _raw_evidence(ev: "str | bytes") -> str:
    text = ev.decode("utf-8", errors="replace") if isinstance(ev, (bytes, bytearray)) else str(ev)
    if len(text) > RAW_EVIDENCE_LIMIT:
        text = text[:RAW_EVIDENCE_LIMIT] + "\n[truncated by episode store]\n"
    return text


def _new_episode(spec: DetectorSpec, fault_id: str, kind: str, evidence: str, ports: Ports,
                 predecessor: str | None = None) -> dict:
    return {
        "episode_id": mint_episode_id(spec, ports), "fault_id": fault_id, "detector": spec.detector,
        "domain_key": spec.domain_key, "subsystem": spec.subsystem, "verdict_kind": kind,
        "state": OPEN, "opened_at": _iso(ports), "evidence": evidence, "last_evidence": evidence,
        "counters": {"non_healthy": 1, "healthy": 0}, "close_streak": 0,
        "delivery": {"phase": NONE, "commit_sha": None, "ticket_id": None, "route": None,
                     "push_failures": 0, "push_paged": False, "last_error": None},
        "attempts": [], "predecessor_episode_id": predecessor,
        "recovered_before_repair": False, "resolved_at": None, "resolve_reason": None,
    }


# --------------------------------------------------------------------------- #
# Detector entry point (BT2 plugs in here)
# --------------------------------------------------------------------------- #
def observe(spec: DetectorSpec, verdict: "Healthy | Broken | Unknown", *, ports: Ports | None = None,
            state_dir: Path | str | None = None, sample_key: str | None = None) -> CycleResult:
    """Feed one verdict from detector instance ``spec``. Drives episode transitions and
    resumes any of this detector's episodes that are mid-delivery.

    ``sample_key`` (BT3a, optional): an idempotency key for the SAMPLE. Under the domain flock,
    a key already in ``doc["ingested"]`` returns ``CycleResult("deduped")`` and changes nothing.
    Otherwise the key is added to the in-memory doc BEFORE the verdict is applied, so the very
    first ``_save`` of this sample (counters / suppression / OPEN) persists the key in the SAME
    atomic rename: the sample is either wholly applied-and-keyed or not applied at all. That is
    what makes at-least-once replay (ingest crash between observe and file delete) exactly-once."""
    ports = ports or Ports()
    write_heartbeat(ports, state_dir)
    path = store_path(spec.domain_key, state_dir)
    try:
        with _DomainLock(path, ports.lock_timeout):
            doc = _load(path, spec.domain_key)
            if sample_key is not None:
                seen = doc.setdefault("ingested", [])
                if sample_key in seen:
                    return CycleResult("deduped")
                seen.append(sample_key)
                del seen[:-INGESTED_KEYS_KEPT]
            result = _apply_verdict(doc, spec, verdict, path, ports)
            _resume_pending(doc, {spec.detector: spec}, path, ports, result)
            return result
    except _Busy:
        _log(f"{spec.domain_key}: domain lock busy; cycle suppressed")
        return CycleResult("busy_suppressed")


def _active(doc: dict, detector: str):
    return [e for e in doc["episodes"].values() if e["detector"] == detector and e["state"] in (OPEN, EMITTED)]


def _apply_verdict(doc: dict, spec: DetectorSpec, verdict, path: Path, ports: Ports) -> CycleResult:
    runs = doc["runs"]
    if isinstance(verdict, Healthy):
        runs.pop(spec.detector, None)
        closed = []
        for ep in _active(doc, spec.detector):
            ep["close_streak"] += 1
            ep["counters"]["healthy"] += 1
            if ep.get("stale_watch") is not None:
                ep["stale_watch"]["nh_run"] = 0  # a healthy sample breaks the post-resolution run
            if ep["close_streak"] >= spec.m and _closable(ep):
                _resolve(ep, "hysteresis-close", ports)
                closed.append(ep["episode_id"])
        _save(path, doc)
        return CycleResult("resolved" if closed else "healthy", closed)
    if not isinstance(verdict, (Broken, Unknown)):
        raise TypeError(f"unsupported verdict {verdict!r}")
    kind = "broken" if isinstance(verdict, Broken) else "unknown"
    evidence = _raw_evidence(verdict.evidence)
    for ep in _active(doc, spec.detector):
        ep["close_streak"] = 0  # a non-healthy sample breaks every consecutive-healthy run
    for ep in _active(doc, spec.detector):
        if ep["fault_id"] == verdict.fault_id:  # suppression: counters/evidence only
            ep["counters"]["non_healthy"] += 1
            ep["last_evidence"] = evidence
            if ep.get("stale_watch") is not None:  # BT3b stale-EMITTED cool-down, in detector-native units
                ep["stale_watch"]["nh_run"] += 1
            _save(path, doc)
            return CycleResult("suppressed", [ep["episode_id"]])
    run = runs.get(spec.detector)
    if run and run["fault_id"] == verdict.fault_id:
        run["n"] += 1
    else:
        run = {"fault_id": verdict.fault_id, "n": 1}
    run.update(kind=kind, evidence=evidence)
    runs[spec.detector] = run
    if run["n"] < spec.n:
        _save(path, doc)
        return CycleResult("counted")
    runs.pop(spec.detector)
    ep = _new_episode(spec, verdict.fault_id, kind, evidence, ports)
    ep["counters"]["non_healthy"] = run["n"]
    doc["episodes"][ep["episode_id"]] = ep
    _save(path, doc)  # durable (fsync'd) BEFORE any emission attempt
    ports.kill_hook("after_open_before_emit")
    return CycleResult("opened", [ep["episode_id"]])


def _closable(ep: dict) -> bool:
    """Hysteresis-close applies to EMITTED episodes and to OPEN ones that have not begun
    delivery. An OPEN episode mid-delivery finishes delivering first (a committed-but-
    unpushed ticket must not be stranded); its close is re-evaluated when it reaches EMITTED."""
    return ep["state"] == EMITTED or ep["delivery"]["phase"] == NONE


def _resolve(ep: dict, reason: str, ports: Ports) -> None:
    ep["state"] = RESOLVED
    ep["resolved_at"] = _iso(ports)
    ep["resolve_reason"] = reason
    # healed while its ticket may still be open: the BT3b healed-under-ticket sweep sets
    # recovered_before_repair on the ticket. ONLY a hysteresis-close is a heal; a stale-closed
    # episode (ticket resolved, fault still measuring non-healthy) did not heal (RFC-004 section 2b).
    ep["healed_under_ticket"] = reason == "hysteresis-close" and bool(ep["delivery"].get("ticket_id"))


# --------------------------------------------------------------------------- #
# Startup reconciliation + BT3 mutations
# --------------------------------------------------------------------------- #
def reconcile(specs: Sequence[DetectorSpec], *, ports: Ports | None = None,
              state_dir: Path | str | None = None) -> CycleResult:
    """Startup reconciliation (RFC-004 §2/§2b): resume every OPEN-without-VERIFIED episode in
    the domains of ``specs`` against the COMMITTED corpus. The periodic sweeps are BT3."""
    ports = ports or Ports()
    write_heartbeat(ports, state_dir)
    by_detector = {s.detector: s for s in specs}
    out = CycleResult("reconciled")
    for domain in sorted({s.domain_key for s in specs}):
        path = store_path(domain, state_dir)
        if not path.exists():
            continue
        try:
            with _DomainLock(path, ports.lock_timeout):
                doc = _load(path, domain)
                _resume_pending(doc, by_detector, path, ports, out)
        except _Busy:
            out.status = "busy_suppressed"
        except StoreCorruptionError as exc:  # T-5999: one corrupt/aliased domain must not stop the others
            _log(f"{domain}: reconcile skipped: {exc}")
            out.corrupt.append((domain, str(exc)))
    return out


def _resume_pending(doc: dict, specs: dict, path: Path, ports: Ports, result: CycleResult) -> None:
    for ep in list(doc["episodes"].values()):
        spec = specs.get(ep["detector"])
        if spec is None or ep["state"] != OPEN:
            continue
        _drive(doc, ep, spec, path, ports)
        if ep["state"] == OPEN:
            result.pending.append(ep["episode_id"])
        elif ep["state"] == EMITTED and ep["close_streak"] >= spec.m:
            _resolve(ep, "hysteresis-close", ports)
            _save(path, doc)


def _with_episode(domain_key: str, episode_id: str, ports: Ports, state_dir, fn):
    path = store_path(domain_key, state_dir)
    with _DomainLock(path, ports.lock_timeout):
        doc = _load(path, domain_key)
        ep = doc["episodes"].get(episode_id)
        if ep is None:
            raise KeyError(f"unknown episode {episode_id} in domain {domain_key}")
        return fn(doc, ep, path)


def mark_recovered_before_repair(domain_key: str, episode_id: str, *, ports: Ports | None = None,
                                 state_dir: Path | str | None = None) -> dict:
    """BT3 seam (healed-under-ticket, §2b/§6.5): record that the episode healed while its
    ticket was open. Store-side flag only; BT3 applies it to the ticket via ticket.py."""
    ports = ports or Ports()

    def _do(doc, ep, path):
        ep["recovered_before_repair"] = True
        _save(path, doc)
        return ep
    return _with_episode(domain_key, episode_id, ports, state_dir, _do)


REOPEN_CHAIN_PAGE_DEPTH = 3  # predecessors before the one-time "keeps coming back" page (BT3b)


def _chain_depth(doc: dict, ep: dict) -> int:
    n, seen, cur = 0, set(), ep
    while cur is not None and cur.get("predecessor_episode_id") and cur["predecessor_episode_id"] not in seen:
        seen.add(cur["predecessor_episode_id"])
        n += 1
        cur = doc["episodes"].get(cur["predecessor_episode_id"])
    return n


def close_stale_and_reopen(spec: DetectorSpec, episode_id: str, *, ports: Ports | None = None,
                           state_dir: Path | str | None = None) -> str:
    """BT3 seam (stale-EMITTED, §2b/§6.5): its ticket resolved but the fault still measures
    non-healthy. Close the stale episode and open a FRESH one (new episode_id, predecessor
    linked in the record and in the ticket's evidence). Returns the new episode_id.

    When the predecessor chain reaches ``REOPEN_CHAIN_PAGE_DEPTH`` the fault is evidently being
    "resolved" without being fixed: ONE page (deduped in the episode record) is sent. The reopen
    itself is never blocked by it (P7 cheap protective action; RFC-004 section 2b intent)."""
    ports = ports or Ports()

    def _do(doc, ep, path):
        _resolve(ep, "stale-closed", ports)
        new = _new_episode(spec, ep["fault_id"], ep["verdict_kind"], ep["last_evidence"], ports,
                           predecessor=ep["episode_id"])
        doc["episodes"][new["episode_id"]] = new
        depth = _chain_depth(doc, new)
        if depth >= REOPEN_CHAIN_PAGE_DEPTH and not new.get("chain_paged"):
            new["chain_paged"] = True  # durable BEFORE the page: at-most-once
            _save(path, doc)
            ports.page(f"ORG-076 fault {new['fault_id']} reopened {depth} times",
                       f"episode {new['episode_id']} is predecessor-depth {depth}: its repair tickets keep "
                       f"being resolved while the fault still measures non-healthy")
        _save(path, doc)
        ports.kill_hook("after_open_before_emit")
        _drive(doc, new, spec, path, ports)
        return new["episode_id"]
    return _with_episode(spec.domain_key, episode_id, ports, state_dir, _do)


def arm_stale_watch(domain_key: str, episode_id: str, *, ports: Ports | None = None,
                    state_dir: Path | str | None = None) -> dict:
    """BT3b: start counting post-resolution non-healthy samples for an EMITTED episode whose ticket
    is resolved. Idempotent: an existing watch is left untouched (the count must survive cycles)."""
    ports = ports or Ports()

    def _do(doc, ep, path):
        if ep.get("stale_watch") is None:
            ep["stale_watch"] = {"since": _iso(ports), "nh_run": 0}
            _save(path, doc)
        return dict(ep["stale_watch"])
    return _with_episode(domain_key, episode_id, ports, state_dir, _do)


def disarm_stale_watch(domain_key: str, episode_id: str, *, ports: Ports | None = None,
                       state_dir: Path | str | None = None) -> None:
    """BT3b: the ticket is no longer resolved (reopened): forget the cool-down."""
    ports = ports or Ports()

    def _do(doc, ep, path):
        if ep.pop("stale_watch", None) is not None:
            _save(path, doc)
    _with_episode(domain_key, episode_id, ports, state_dir, _do)


def mark_rbr_applied(domain_key: str, episode_id: str, how: str, *, ports: Ports | None = None,
                     state_dir: Path | str | None = None) -> None:
    """BT3b: record that the healed-under-ticket sweep finished with this episode. ``how`` is
    ``flagged`` / ``already`` (the ticket carries the flag on origin; also sets the store-side
    ``recovered_before_repair``) or ``ticket-closed`` / ``ticket-gone`` (nothing to flag)."""
    ports = ports or Ports()

    def _do(doc, ep, path):
        if how in ("flagged", "already"):
            ep["recovered_before_repair"] = True
        ep["rbr_applied"] = how
        _save(path, doc)
    _with_episode(domain_key, episode_id, ports, state_dir, _do)


def close_domain_episodes(domain_key: str, reason: str, *, ports: Ports | None = None,
                          state_dir: Path | str | None = None) -> list:
    """BT3b (``--forget-persona``): RESOLVE every active (OPEN/EMITTED) episode of a domain with
    ``reason``, with NO ticket mutation and no heal claim (``healed_under_ticket`` stays false). Also
    clears the open-threshold run counters. Returns the closed episode ids. A missing domain is a no-op."""
    ports = ports or Ports()
    path = store_path(domain_key, state_dir)
    if not path.exists():
        return []
    closed: list = []
    with _DomainLock(path, ports.lock_timeout):
        doc = _load(path, domain_key)
        for ep in list(doc["episodes"].values()):
            if ep["state"] in (OPEN, EMITTED):
                _resolve(ep, reason, ports)
                closed.append(ep["episode_id"])
        if closed or doc["runs"]:
            doc["runs"] = {}
            _save(path, doc)
    return closed


def reemit_orphan(spec: DetectorSpec, episode_id: str, *, ports: Ports | None = None,
                  state_dir: Path | str | None = None) -> dict:
    """BT3 seam (orphan, §2b): an EMITTED episode's ticket vanished from the committed
    corpus. Re-emit under the SAME episode_id (the committed-corpus check suppresses a
    duplicate if it is in fact still there)."""
    ports = ports or Ports()

    def _do(doc, ep, path):
        ep["delivery"].update(phase=NONE, commit_sha=None, orphaned_ticket_id=ep["delivery"].get("ticket_id"),
                              ticket_id=None, push_failures=0, push_paged=False)
        ep["state"] = OPEN
        _save(path, doc)
        _drive(doc, ep, spec, path, ports)
        return ep
    return _with_episode(spec.domain_key, episode_id, ports, state_dir, _do)


def load_domain(domain_key: str, state_dir: Path | str | None = None) -> dict:
    """Read-only snapshot of a domain document (tests / BT3 sweeps)."""
    return _load(store_path(domain_key, state_dir), domain_key)


# --------------------------------------------------------------------------- #
# Delivery machine (RFC-004 §2). Runs under the domain flock.
# --------------------------------------------------------------------------- #
def _persist(doc: dict, ep: dict, path: Path, phase: str | None = None, **delivery) -> None:
    if phase is not None:
        ep["delivery"]["phase"] = phase
    ep["delivery"].update(delivery)
    _save(path, doc)


def _ticket_id_from_path(text: str) -> str | None:
    m = re.search(r"(T-\d+)\.md", text)
    return m.group(1) if m else None


def _ticket_id_of_commit(repo: Path, sha: str, ports: Ports) -> str | None:
    r = ports.git._git("show", "--name-only", "--format=", sha, "--", "tickets/", cwd=repo)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if "README" not in line:
            tid = _ticket_id_from_path(line)
            if tid:
                return tid
    return None


def _drive(doc: dict, ep: dict, spec: DetectorSpec, path: Path, ports: Ports) -> None:
    """Advance delivery as far as it will go this cycle; stops (durably) on UNKNOWN / failure."""
    repo = ports.resolved_repo()
    cycle: dict = {}  # per-drive scratch (not persisted): lets _step_verify count once per cycle
    for _ in range(_MAX_DRIVE_STEPS):
        d = ep["delivery"]
        phase = d["phase"]
        if phase == VERIFIED:
            return
        if phase == NONE:
            if not _step_none(doc, ep, spec, path, ports, repo):
                return
        elif phase == COMMIT_INFLIGHT:
            if not _step_commit_inflight(doc, ep, path, ports, repo):
                return
        elif phase == LOCAL_COMMIT:
            _persist(doc, ep, path, PUSH_INFLIGHT)
        elif phase == PUSH_INFLIGHT:
            if not _step_push(doc, ep, path, ports, repo):
                return
        elif phase == PUSHED:
            if not _step_verify(doc, ep, path, ports, repo, cycle):
                return
        else:
            raise StoreCorruptionError(f"{ep['episode_id']}: unknown delivery phase {phase!r}")
    _log(f"{ep['episode_id']}: drive step bound hit at phase {ep['delivery']['phase']}; resuming next cycle")


def _mark_verified(doc, ep, path, ticket_id, ports: Ports) -> None:
    ep["state"] = EMITTED
    _persist(doc, ep, path, VERIFIED, ticket_id=ticket_id or ep["delivery"].get("ticket_id"), last_error=None,
             verify_loops=0, verify_loop_paged=False, verify_cycle_token=None)
    ports.kill_hook("after_verified_persist")


def _step_none(doc, ep, spec, path, ports: Ports, repo: Path) -> bool:
    lk = _episode_corpus.lookup_episode(ep["episode_id"], repo=repo, fetch=ports.fetch, git=ports.git)
    if lk.status == _episode_corpus.UNKNOWN:  # fail closed (amendments A/B): file nothing
        _persist(doc, ep, path, last_error=f"corpus UNKNOWN: {lk.detail}")
        return False
    if lk.status == _episode_corpus.FOUND:  # idempotent-create: already committed
        if lk.where == "origin":
            _mark_verified(doc, ep, path, _ticket_id_from_path(lk.detail), ports)
        else:
            _persist(doc, ep, path, LOCAL_COMMIT, commit_sha=lk.sha,
                     ticket_id=_ticket_id_of_commit(repo, lk.sha, ports))
        return True
    route = _route(ep, spec, ports)
    ep["attempts"].append({"at": _iso(ports), "kind": "emit"})
    _persist(doc, ep, path, COMMIT_INFLIGHT, route=route, last_error=None)
    ports.kill_hook("after_commit_inflight_persist")
    fields = _repair_fields(ep, spec, route)
    argv_path = path.parent / f".{ep['episode_id']}.repair.json"
    argv_path.write_text(json.dumps(fields), encoding="utf-8")
    try:
        filer = ports.filer or _make_default_filer(repo)
        res = filer(_file_argv(ep, spec, route, argv_path))
    finally:
        try:
            argv_path.unlink()
        except FileNotFoundError:
            pass
    ports.kill_hook("after_file_before_local_commit")
    if res.returncode != 0:
        ep["delivery"]["last_error"] = f"ticket.py file rc={res.returncode}: {(res.stderr or '')[-300:]}"
    return True  # next loop iteration resolves COMMIT_INFLIGHT against the committed corpus


def _step_commit_inflight(doc, ep, path, ports: Ports, repo: Path) -> bool:
    lk = _episode_corpus.lookup_episode(ep["episode_id"], repo=repo, fetch=ports.fetch, git=ports.git)
    if lk.status == _episode_corpus.UNKNOWN:
        _persist(doc, ep, path, last_error=f"corpus UNKNOWN: {lk.detail}")
        return False
    if lk.status == _episode_corpus.FOUND:
        if lk.where == "origin":
            _mark_verified(doc, ep, path, _ticket_id_from_path(lk.detail), ports)
        else:
            _persist(doc, ep, path, LOCAL_COMMIT, commit_sha=lk.sha,
                     ticket_id=_ticket_id_of_commit(repo, lk.sha, ports), last_error=None)
            ports.kill_hook("after_local_commit_persist")
        return True
    # ABSENT: the commit did not land. Remove ONLY this episode's own residue, drop to NONE.
    cleaned = _cleanup_uncommitted(ep["episode_id"], repo, ports)
    last = ep["delivery"].get("last_error")
    _persist(doc, ep, path, NONE, cleaned=cleaned, last_error=last)
    return last is None  # crash recovery re-emits now; a ticket.py failure retries next cycle


def _frontmatter_has_episode(path: Path, episode_id: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return _episode_corpus.frontmatter_has_episode(text, episode_id)


def _strip_ticket_rows(working: str, head: str, tid: str) -> str | None:
    """``working`` minus the table row(s) for exactly ``tid`` that HEAD's README lacks.
    Returns None when there is nothing to remove. T-4707 never matches T-47070."""
    row = re.compile(rf"^\|\s*{re.escape(tid)}\s*\|")
    head_lines = set(head.splitlines())
    kept, removed = [], False
    for line in working.splitlines(keepends=True):
        if row.match(line) and line.rstrip("\n") not in head_lines:
            removed = True
            continue
        kept.append(line)
    return "".join(kept) if removed else None


def _cleanup_uncommitted(episode_id: str, repo: Path, ports: Ports) -> list[str]:
    """Amendment C. Under the shared git mutation lock: remove the ticket file for THIS
    episode_id if it is untracked OR staged-not-committed (unstage first); undo the README
    regeneration that rode along; clear a stale ``.git/index.lock``. Never deletes anything
    else."""
    git = ports.git
    cleaned: list[str] = []
    try:
        with _git_utils.git_mutation_lock(label=f"episode cleanup {episode_id}", timeout_secs=10.0):
            lock = repo / ".git" / "index.lock"
            if lock.is_file() and time.time() - lock.stat().st_mtime >= ports.index_lock_stale_secs:
                lock.unlink()
                cleaned.append(".git/index.lock")
            st = git._git("status", "--porcelain", "--untracked-files=all", "--", "tickets/", cwd=repo)
            if st.returncode != 0:
                return cleaned
            entries = [(ln[:2], ln[3:].strip().strip('"')) for ln in st.stdout.splitlines() if len(ln) > 3]
            for code, rel in entries:
                p = repo / rel
                if "README" in rel or not rel.endswith(".md") or code not in ("??", "A ", "AM"):
                    continue
                if not _frontmatter_has_episode(p, episode_id):
                    continue
                if code != "??":
                    git._git("reset", "-q", "HEAD", "--", rel, cwd=repo)
                p.unlink(missing_ok=True)
                cleaned.append(rel)
                tid = _ticket_id_from_path(rel)
                readme = "tickets/README.md"
                if tid and any(r == readme for _, r in entries):
                    # Remove ONLY this ticket's own index row(s) from the README, never `checkout`
                    # it (that would discard unrelated operator edits). A row is "ours" iff it is
                    # an exact `| T-N |` row absent from HEAD's README. Unstage first: ticket.py's
                    # scope guard refuses a staged README outside its own commit. If anything is
                    # unclear the README is left alone (a stale row is regenerated by ticket.py/
                    # grooming; a lost operator edit is not recoverable).
                    cached = git._git("diff", "--cached", "--", readme, cwd=repo)
                    if cached.returncode == 0 and cached.stdout.strip():
                        git._git("reset", "-q", "HEAD", "--", readme, cwd=repo)
                        cleaned.append(readme + " (unstaged)")
                    head = git._git("show", f"HEAD:{readme}", cwd=repo)
                    work = repo / readme
                    if head.returncode == 0 and work.is_file():
                        stripped = _strip_ticket_rows(work.read_text(encoding="utf-8"), head.stdout, tid)
                        if stripped is not None:
                            work.write_text(stripped, encoding="utf-8")
                            cleaned.append(readme + " (own row removed)")
    except _git_utils.GitMutationLockTimeout:
        _log(f"{episode_id}: git mutation lock busy during cleanup; retry next cycle")
    return cleaned


def _episode_sha(ep: dict, repo: Path, ports: Ports) -> str | None:
    from tools import delivery_rebase_controller as drc
    return drc.find_episode_commit(ep["episode_id"], repo=repo, git=ports.git)


def _scope_error(ep: dict, repo: Path, ports: Ports, *, strict_head: bool) -> "tuple[str | None, str | None]":
    """Push-scope guard (fail-closed). Returns (sha, error). ``(None, None)`` = the episode is no
    longer local-ahead (already on origin): nothing to push. Caller holds the git mutation lock.
    Default path (``strict_head=False``): only ancestors of the episode sha travel, so
    ``origin/main..<sha>`` must be exactly ``[sha]``. Rebase path (``strict_head=True``): the
    controller rebases and pushes HEAD, so ``origin/main..HEAD`` must be exactly ``[sha]``."""
    try:
        sha = _episode_sha(ep, repo, ports)
    except Exception as exc:  # noqa: BLE001 -- ambiguity / git failure: fail closed
        return None, f"push scope guard: cannot identify episode commit: {type(exc).__name__}: {exc}"
    if sha is None:  # not local-ahead: already on origin (or gone) -> nothing to push; verify decides
        return None, None
    tip = sha if not strict_head else "HEAD"
    r = ports.git._git("rev-list", f"origin/main..{tip}", cwd=repo)
    if r.returncode != 0:
        return sha, f"push scope guard: rev-list failed: {(r.stderr or '').strip()[:200]}"
    ahead = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if ahead != [sha]:
        return sha, f"push scope guard: {SCOPE_REFUSAL} ({len(ahead)} would travel)"
    return sha, None


def _edge_kind(ret: Any) -> str | None:
    return ret.split(":", 1)[0].strip() if isinstance(ret, str) and ret else None


def _apply_edge(doc, ep, path, ports: Ports, ret: Any) -> str:
    """Act on a rebase-edge outcome string. Returns 'advance' (phase -> PUSHED, drive on),
    'retry' (stay PUSH_INFLIGHT, not a failure), 'failed' (count it), or 'none'."""
    kind = _edge_kind(ret)
    d = ep["delivery"]
    if kind in ("PUSHED", "ADOPTED_ORIGIN", "UNVERIFIED"):
        _persist(doc, ep, path, PUSHED, push_failures=0, rebase_retries=0, last_error=None)
        ports.kill_hook("after_pushed_persist")
        return "advance"
    if kind == "RE_ENTER":
        n = int(d.get("rebase_retries", 0)) + 1
        _persist(doc, ep, path, rebase_retries=n, last_error=f"rebase edge: {ret}"[:300])
        return "retry" if n <= MAX_REBASE_RETRIES else "failed"
    if kind in ("TYPED_FAILURE", "PAGED", "REFUSED", "ERROR"):
        ep["delivery"]["last_error"] = f"rebase edge: {ret}"[:300]
        return "failed"
    return "none"


def _count_failure(doc, ep, path, ports: Ports, last_error: str) -> None:
    n = ep["delivery"]["push_failures"] + 1
    _persist(doc, ep, path, push_failures=n, last_error=last_error[-300:])
    if n >= MAX_PUSH_FAILURES_BEFORE_PAGE and not ep["delivery"]["push_paged"]:
        ports.page(f"ORG-076 episode {ep['episode_id']} stuck at PUSH_INFLIGHT",
                   f"{n} consecutive push failures; last: {ep['delivery']['last_error']}")
        _persist(doc, ep, path, push_paged=True)


def _step_push(doc, ep, path, ports: Ports, repo: Path) -> bool:
    if ports.on_recover is not None:  # restart with a controller state file: recover first
        try:
            ret = ports.on_recover(ep)
        except Exception as exc:  # noqa: BLE001
            ret = f"ERROR: recover raised {type(exc).__name__}: {exc}"
        act = _apply_edge(doc, ep, path, ports, ret)
        if act == "advance":
            return True
        if act == "retry":
            return False
        if act == "failed":
            _count_failure(doc, ep, path, ports, ep["delivery"]["last_error"])
            return False
    ports.kill_hook("before_push")
    try:
        with _git_utils.git_mutation_lock(label=f"episode push {ep['episode_id']}", timeout_secs=30.0):
            sha, err = _scope_error(ep, repo, ports, strict_head=False)
            if err is not None:
                res = None
            elif sha is None:
                res = PushResult(ok=True, detail="episode not local-ahead; verifying on origin")
            elif ports.pusher is not None:
                res = ports.pusher(repo)
            else:
                res = _default_push(repo, refspec=f"{sha}:main", mirror_url=ports.mirror_url)
    except _git_utils.GitMutationLockTimeout:
        _persist(doc, ep, path, last_error="git mutation lock busy before push")
        return False
    if res is None:  # scope guard refused: nothing pushed
        _count_failure(doc, ep, path, ports, err)
        return False
    ports.kill_hook("after_push_before_pushed")
    if res.ok:
        _persist(doc, ep, path, PUSHED, push_failures=0, last_error=None)
        ports.kill_hook("after_pushed_persist")
        return True
    detail = f"push failed: {res.detail[-300:]}"
    if res.nonff and ports.on_nonff is not None:
        try:
            ret = ports.on_nonff(ep, res)  # ORG-096 seam
        except Exception as exc:  # noqa: BLE001 -- controller failure is a typed failure, not a crash
            ret = f"ERROR: on_nonff raised {type(exc).__name__}: {exc}"
        act = _apply_edge(doc, ep, path, ports, ret)
        if act == "advance":
            return True
        if act == "retry":
            return False
        if act == "failed":
            detail = ep["delivery"]["last_error"]
    _count_failure(doc, ep, path, ports, detail)
    return False


# --------------------------------------------------------------------------- #
# ORG-096 rebase edge wiring (BT2)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class RebaseEdge:
    """``on_nonff`` / ``on_recover`` callables for ``Ports``, bound to one repo + state dir."""
    repo: Path
    controller_state_dir: Path
    page: Callable[[str, str], None]
    git: Any = _git_utils

    def on_nonff(self, ep: dict, res: PushResult) -> str:
        from tools import delivery_rebase_controller as drc
        eid = ep["episode_id"]
        # ONE store-held lock across guard + rebase + push: the lock is NOT re-entrant, so the
        # lock-free core is used (the controller's documented seam for this caller).
        with self.git.git_mutation_lock(label=f"episode rebase-edge {eid}", timeout_secs=30.0):
            sha, err = _scope_error(ep, self.repo, Ports(git=self.git), strict_head=True)
            if err is not None:
                return f"REFUSED: {err}"
            if sha is None:
                return "UNVERIFIED: episode not local-ahead; verify on origin"
            r = drc._deliver_locked(eid, sha, repo=self.repo, state_dir=self.controller_state_dir,
                                    git=self.git, send_page=self.page)
        return f"{r.outcome.name}: {r.detail}"

    def on_recover(self, ep: dict) -> str:
        """Restart recovery when a controller state file exists. ``_recover_locked`` can push
        HEAD:main (its 3b and case-iii branches), so the strict scope guard runs FIRST, under
        the same lock. A RE_ENTER (e.g. a persisted INFLIGHT state with HEAD == prev_sha, left
        by any rebase conflict) is not a terminal answer: it means "re-drive delivery", so we
        fall through to ``_deliver_locked`` in the same cycle. That overwrites the stale state
        file (``save_state`` preserves created_at) rather than deleting it, and carries
        ``reenter_count + 1`` forward so the controller's own MAX_REENTERS cap pages a state
        that never converges; ``rebase_retries`` stays the store-side bound."""
        from tools import delivery_rebase_controller as drc
        eid = ep["episode_id"]
        if not (self.controller_state_dir / f"{eid}.json").exists():
            return "NONE"
        with self.git.git_mutation_lock(label=f"episode rebase-recover {eid}", timeout_secs=30.0):
            sha, err = _scope_error(ep, self.repo, Ports(git=self.git), strict_head=True)
            if err is not None:
                return f"REFUSED: {err}"  # sha None (already on origin) is safe: recovery step 1 handles it
            r = drc._recover_locked(eid, repo=self.repo, state_dir=self.controller_state_dir,
                                    git=self.git, send_page=self.page)
            if r.outcome is drc.RebaseOutcome.RE_ENTER:
                try:
                    count = int((drc.load_state(eid, self.controller_state_dir) or {}).get("reenter_count", 0)) + 1
                except drc.StateCorruptionError:
                    count = 1
                # bounded: _deliver_locked loops at most MAX_REBASE_ATTEMPTS, once per cycle
                r = drc._deliver_locked(eid, r.prev_sha or sha or "", repo=self.repo,
                                        state_dir=self.controller_state_dir, git=self.git,
                                        send_page=self.page, reenter_count=count)
        return f"{r.outcome.name}: {r.detail}"


def make_rebase_edge(*, repo: Path, state_dir: Path | str | None = None,
                     page: Callable[[str, str], None] = _default_page, git: Any = _git_utils) -> RebaseEdge:
    """Build the ORG-096 rebase edge. Controller state lives under ``<state_dir>/delivery-rebase``
    (``state_dir`` resolved like the store's own; never the controller's home default)."""
    return RebaseEdge(Path(repo), resolve_state_dir(state_dir) / "delivery-rebase", page, git)


def _step_verify(doc, ep, path, ports: Ports, repo: Path, cycle: "dict | None" = None) -> bool:
    lk = _episode_corpus.lookup_episode(ep["episode_id"], repo=repo, fetch=ports.fetch, git=ports.git)
    if lk.status == _episode_corpus.FOUND and lk.where == "origin":
        _mark_verified(doc, ep, path, _ticket_id_from_path(lk.detail), ports)
        return True
    if lk.status == _episode_corpus.ABSENT or (lk.status == _episode_corpus.FOUND):
        d = ep["delivery"]
        loops = int(d.get("verify_loops", 0))
        extra: dict = {}
        token = ports.cycle_token
        if token is not None:
            due = d.get("verify_cycle_token") != token  # once per runner cycle, across every drive in it
            extra["verify_cycle_token"] = token
        else:
            due = cycle is None or not cycle.get("verify_counted")  # once per drive
        if due:
            if cycle is not None:
                cycle["verify_counted"] = True
            loops += 1
            extra["verify_loops"] = loops
            if loops >= VERIFY_LOOP_PAGE_THRESHOLD and not d.get("verify_loop_paged"):
                ports.page(f"ORG-076 episode {ep['episode_id']} silent push/verify loop",
                           f"pushed but never visible on origin/main for {loops} consecutive cycles "
                           f"(not local-ahead either); re-pushing each cycle")
                extra["verify_loop_paged"] = True
        _persist(doc, ep, path, PUSH_INFLIGHT, last_error="pushed but not on origin/main; re-pushing", **extra)
        return True
    _persist(doc, ep, path, last_error=f"corpus UNKNOWN: {lk.detail}")
    return False


# --------------------------------------------------------------------------- #
# Ticket construction (RFC-004 §3(a))
# --------------------------------------------------------------------------- #
def _route(ep: dict, spec: DetectorSpec, ports: Ports) -> dict:
    existing = ep["delivery"].get("route")
    if existing:
        return existing
    deferred = repair_evidence.redact_evidence(ep["evidence"], spec.redactor).deferred
    if not spec.candidates:
        return {"kind": "unrouted", "target": spec.requester, "harness": spec.harness,
                "deferred": deferred}
    # T-5999 Recovery condition: never route an episode to its own SUBJECT persona (from the fault_id;
    # ``subject_of`` returns None, i.e. no exclusion, for ids not shaped ``<kind>:<persona>``).
    r = repair_evidence.resolve_route(spec.harness, spec.candidates, liveness=ports.liveness,
                                      page=ports.page, evidence_deferred=deferred,
                                      origin_account=spec.account or None,
                                      subject=f"ORG-076 {ep['episode_id']}: no eligible repair target",
                                      episode_subject=repair_evidence.subject_of(ep.get("fault_id")),
                                      any_live_fallback=spec.any_live_fallback)
    extra = {"excluded_subject": list(r.excluded_subject)} if r.excluded_subject else {}
    if r.target is None:
        return {"kind": "page", "target": spec.requester, "harness": spec.harness, "deferred": deferred,
                "refused_cross_account": list(r.refused_cross_account), **extra}
    return {"kind": r.kind, "target": r.target.name, "harness": r.target.harness, "deferred": deferred,
            "refused_cross_account": list(r.refused_cross_account), **extra}


def _repair_fields(ep: dict, spec: DetectorSpec, route: dict) -> dict:
    evidence_text = ep["evidence"]
    if ep.get("predecessor_episode_id"):
        evidence_text = f"predecessor_episode_id: {ep['predecessor_episode_id']}\n" + evidence_text
    ev = repair_evidence.redact_evidence(evidence_text, spec.redactor)
    repair_route = route["kind"] if route["kind"] == "page" else f"{route['target']}@{route['harness'] or 'unknown'}"
    fields: dict[str, Any] = {
        "fault_id": ep["fault_id"], "episode_id": ep["episode_id"], "domain_key": spec.domain_key,
        "subsystem": spec.subsystem, "detector": spec.detector, "verdict": ep["verdict_kind"],
        "evidence_deferred": ev.deferred, "repair_route": repair_route,
        "attempts_budget": spec.attempts_budget, "recovered_before_repair": False,
    }
    if spec.invariant_list_id:
        fields["invariant_list_id"] = spec.invariant_list_id
    if spec.invariant_list_digest:
        fields["invariant_list_digest"] = spec.invariant_list_digest
    if spec.action:
        fields["action"] = spec.action
    if ev.snapshot is not None:  # deferred => the key is ABSENT (RFC-004 §6.16)
        fields["evidence_snapshot"] = ev.snapshot
    return fields


def _repair_ticket_target(seat: str) -> str:
    """Ticket `--target` for a repair route's chosen SEAT (T-5239 / ORG-088: role-only).

    The route record (and the `repair_route` field) keeps the seat/harness/account the
    route guard chose - that is audit data. Only the ticket's delivery target is
    translated to the seat's pool role. NOTE the pool is wider than the seat (e.g.
    role:system_orchestrator = recovery + watchman), so the chosen seat/harness/account
    ride as the `subject:` label + an ask line: they DESCRIBE, they do not route."""
    from tools.agent_role_map import persona_pool_role
    if seat == "operator":
        return seat
    role = persona_pool_role(seat)
    if not role:
        raise ValueError(f"repair route seat {seat!r} has no pool role (ORG-088 role-only routing)")
    return f"role:{role}"


def _file_argv(ep: dict, spec: DetectorSpec, route: dict, fields_path: Path) -> list[str]:
    title = f"[repair] {ep['fault_id']}"[:100]
    if ep["verdict_kind"] == "unknown":
        stance = "Measurement FAILED (unknown): diagnose first, do not blind-swap."
    else:
        stance = "Measurement succeeded and found a bad state (broken): open the playbook for this fault_id."
    # T-6084: `subject:` names the seat the fault is ON (repair_evidence.subject_of), matching
    # soak_filer and test_ticket_role_only_targets; the seat chosen to fix it is `repair-target:`.
    # Both labels describe; neither routes.
    faulted = repair_evidence.subject_of(ep.get("fault_id"))
    faulted_txt = f"Faulted seat: {faulted}. " if faulted else ""
    ask = (f"ORG-076 repair episode {ep['episode_id']}: {ep['fault_id']} ({ep['verdict_kind']}) "
           f"on {spec.subsystem}. {stance} Route: {route['kind']} -> {route['target']}. "
           f"{faulted_txt}"
           f"Repair target seat: {route['target']} (harness {route.get('harness') or 'unknown'}"
           f"{', account ' + spec.account if spec.account else ''}) - routed to its role pool; "
           f"labels subject:/repair-target: describe, they do not route. "
           f"Attempts budget {spec.attempts_budget}.")
    labels = ["repair", "org-076", _slug(spec.subsystem, 40), f"repair-target:{route['target']}"]
    if faulted:
        labels.append(f"subject:{faulted}")
    labels = ",".join(labels)
    return ["file", "--automated-filer", "episode_store", "--requester", spec.requester, "--target", _repair_ticket_target(route["target"]), "--title", title,
            "--labels", labels, "--serves", "ORG-076 fleet health-and-repair: one repair ticket per fault episode",
            "--project-id", "null", "--nearest-intent-ref", f"maintenance:fleet-health/{_slug(spec.subsystem, 40)}",
            "--appetite", "5", "--ask", ask, "--no-push", "--repair-fields-json", str(fields_path)]
