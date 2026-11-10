#!/usr/bin/env python3
"""ORG-076 BT2 -- claude quota-probe READABILITY detector (T-4708).

Serves D1: when the claude quota probe persistently cannot read usage, the fleet opens ONE
fault episode in the BT1 store (``tools/episode_store.py``) and files ONE deduped,
diagnose-first repair ticket, instead of waiting silently for a human overnight.

What it measures (RFC-004 §1): *can the probe read usage?* -- a READABILITY domain. ``Healthy``
here never means "has quota"; it means "the probe produced a trustworthy reading".

Input surface -- verified, not the ticket's assumption: the probe runs in
``persona_bringup.quota_check`` -> ``refresh_primary_quota`` (the ``quota-check`` command on
its own timer). The consume soft streak (``bump_soft_streak``) counts consume-attempt failures
and never sees the probe. Today a persistently unreadable probe is SILENT (quota-check always
exits 0); this detector CREATES the detection. The consume page (``EXIT_SOFT_PERSISTENT``) is
untouched and stays the backstop for its own domain.

Verdict mapping (one sample per measured 30-minute probe bucket):
  usage read                              -> Healthy
  confirmed cap (status=capped/sentinel)  -> Healthy   (a cap is a successful READ; caps are
                                             handled by quota_check's planned failover
                                             (evaluate_planned_quota_swap ->
                                             begin_planned_transition) and by
                                             runtime_controller's capped-profile breaker /
                                             emergency transition, not by this detector)
  probe exception / cached failure        -> Unknown("quota-probe-unreadable:<persona>")
  probe identity/prompt/pong mismatch     -> Unknown(same fault_id)
  statusline evidence mismatch            -> Unknown(same fault_id)
  no measurement (unsupported, operation-busy, transition/fallback noop, non-claude primary)
                                          -> no sample, no ``observe`` call; BUT (BT3b) with the
                                             switch on the seat writes a minimal ``not-measured``
                                             drop record ("the unit ran, nothing to measure"). The
                                             recovery runner counts it for per-seat FRESHNESS
                                             (``tools/org076_sample_freshness.py``) and NEVER feeds it
                                             to ``observe()``, so a legitimately sample-less run is not
                                             mistaken for a drop-write failure.

DARK by default: ``PANTHEON_ORG076_QUOTA_DETECTOR`` must be exactly ``"1"`` (read at call time).
Off => nothing here runs: no sample is written, ``episode_store`` is not even imported, and
``quota-check`` output and exit code are byte-identical. On => the detector is advisory: ANY
failure is logged to stderr and swallowed; it can never change the quota-check decision or exit
code.

SINGLE-STORE PLACEMENT (BT3a, T-4720; recovery steward constraint ORG-076 section 4): seats NEVER call
``observe()`` in-process. With the switch on, each measured sample is written as ONE atomic,
append-only file into the drop dir (``$PANTHEON_ORG076_SAMPLES_DIR`` or
``/var/lib/pantheon/org076/samples/``) and nothing else: no store, no clone, no dedupe state on
the seat. The recovery-side runner (``tools/org076_store_runner.py``, systemd --user timer under
recovery, running from its dedicated clone) ingests the files in name order into the ONE store.

Drop-file contract (``schema`` 1, ``kind`` "quota-probe-sample"):
  name  ``<UTC %Y%m%dT%H%M%S%f>__<persona>__<run_id slug|nr>__<pid><rand>.json`` (name order ==
        chronological order); dot-prefixed names are writer temp files and are never ingested.
  body  JSON: schema, kind, detector, persona, account, outcome_kind, run_id|null, detail
        (<= 1000 chars), sampled_at (UTC ISO), sample_id (uuid4).
  write tmp file in the drop dir (mode 0664) -> fsync -> ``os.link`` to the final name (atomic and
        NO-CLOBBER: EEXIST retries with a new suffix, an existing file is never overwritten) ->
        unlink tmp -> fsync the dir. The seat never creates the drop dir.
  dir   ASSUMED: ``/var/lib/pantheon/org076/samples`` mode 2775 (setgid), group ``pantheon-shared``,
        NO sticky bit. Seats are different OS accounts in that group; setgid makes every file
        group ``pantheon-shared`` (so recovery can read it), and the missing sticky bit is what lets
        recovery (claude-fallback) DELETE other accounts' files after ingest.
  Replays are deduped recovery-side on ``run_id`` (the probe bucket) per persona domain, so two files
  for one bucket count once.

ROLLOUT ORDER (BT3b): update the recovery runner clone BEFORE any seat starts writing ``not-measured``
records (an older runner quarantines + pages on the unknown ``outcome_kind``). The switch is OFF
fleet-wide under recovery's hold, so this is a sequencing note, not a live hazard.

Uninstall leg (ORG-068 Remove): unset ``PANTHEON_ORG076_QUOTA_DETECTOR`` (that alone makes it
inert and stops new drop files); then delete ``tools/quota_probe_detector.py`` and its tests
(``tools/test_persona_bringup_quota_detector.py``, ``tools/test_org076_sample_drop.py``), drop the
``outcome_sink`` kwarg / ``refresh_primary_quota_detailed`` / the hook block in
``persona_bringup.py``'s ``quota-check`` branch. Queued drop files are left in place for the
runner (or deleted by recovery with the drop dir); the store/runner/unit legs are documented in
``tools/org076_store_runner.py``.

Tests: python3 -m pytest tools/test_persona_bringup_quota_detector.py tools/test_org076_sample_drop.py
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import secrets
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENV_SWITCH = "PANTHEON_ORG076_QUOTA_DETECTOR"
ENV_DROP_DIR = "PANTHEON_ORG076_SAMPLES_DIR"
DEFAULT_DROP_DIR = "/var/lib/pantheon/org076/samples"
SAMPLE_SCHEMA = 1
SAMPLE_KIND = "quota-probe-sample"
SAMPLE_MODE = 0o664
DETAIL_LIMIT = 1000
TMP_PREFIX = ".sample-tmp-"  # the writer's own temp prefix (the runner reaps stale ones by it)
PERSONA_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

DETECTOR = "claude-quota-probe-readability"
SUBSYSTEM = "claude-quota-probe-readability"

# ProbeOutcome kinds
READ = "read"
NOT_MEASURED = "not-measured"  # BT3b: the unit ran but there was nothing to measure (freshness only)
CAPPED = "probe-capped"
PROBE_ERROR = "probe-error"
CACHED_FAILURE = "probe-cached-failure"
PROBE_INVALID = "probe-invalid"
EVIDENCE_MISMATCH = "evidence-mismatch"

_UNREADABLE = (PROBE_ERROR, CACHED_FAILURE, PROBE_INVALID, EVIDENCE_MISMATCH)


@dataclasses.dataclass(frozen=True)
class ProbeOutcome:
    """What ``refresh_primary_quota`` found, before it collapsed to ``None``."""
    kind: str
    run_id: str | None = None
    detail: str = ""


def enabled(env: Any = None) -> bool:
    """Dark switch: exact ``"1"`` only, read at call time."""
    return (env if env is not None else os.environ).get(ENV_SWITCH) == "1"


def fault_id(persona: str) -> str:
    return f"quota-probe-unreadable:{persona}"


def outcome_to_verdict(outcome: ProbeOutcome, persona: str):
    from tools import episode_store as es
    if outcome.kind in (READ, CAPPED):
        return es.Healthy()
    if outcome.kind not in _UNREADABLE:
        raise ValueError(f"unknown probe outcome kind {outcome.kind!r}")
    evidence = (f"kind: {outcome.kind}\nrun_id: {outcome.run_id}\npersona: {persona}\n"
                f"detail: {outcome.detail}\n")
    return es.Unknown(fault_id(persona), evidence)


def spec_for(binding: Any):
    """The DetectorSpec for one persona's probe. Claude-side repair routes to Watchman@codex
    (``repair_evidence.RECOVERY_CANDIDATE``; ORG-048 A1, T-5058) through the store's ``_route`` /
    ``resolve_route``."""
    from tools import episode_store as es
    from tools import repair_evidence
    return es.DetectorSpec(
        detector=DETECTOR,
        domain_key=f"{DETECTOR}:{binding.persona}",
        subsystem=SUBSYSTEM,
        requester=binding.persona,
        harness="claude",
        account=binding.account,
        candidates=(repair_evidence.RECOVERY_CANDIDATE,),
        any_live_fallback=(repair_evidence.RECOVERY_CANDIDATE,),  # T-5998 B(i)
        redactor=repair_evidence.make_default_redactor(binding.account),
    )


def _log(msg: str) -> None:
    print(f"[quota_probe_detector] {msg}", file=sys.stderr)


def drop_dir(override: Path | str | None = None) -> Path:
    if override is not None:
        return Path(override)
    return Path(os.environ.get(ENV_DROP_DIR) or DEFAULT_DROP_DIR)


def _slug(text: str, limit: int = 40) -> str:
    return (re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-") or "x")[:limit]


def sample_body(binding: Any, outcome: ProbeOutcome, now: datetime) -> dict:
    return {
        "schema": SAMPLE_SCHEMA, "kind": SAMPLE_KIND, "detector": DETECTOR,
        "persona": binding.persona, "account": binding.account,
        "outcome_kind": outcome.kind, "run_id": outcome.run_id,
        "detail": (outcome.detail or "")[:DETAIL_LIMIT],
        "sampled_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sample_id": str(uuid.uuid4()),
    }


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkstemp(directory: Path) -> "tuple[int, Path]":
    for _ in range(20):
        tmp = directory / f"{TMP_PREFIX}{os.getpid()}-{secrets.token_hex(4)}"
        try:
            return os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, SAMPLE_MODE), tmp
        except FileExistsError:
            continue
    raise FileExistsError("could not create a drop temp file")


def feed(binding: Any, outcome: ProbeOutcome, *, drop: Path | str | None = None,
         now: datetime | None = None) -> str:
    """Write ONE measured probe outcome as an append-only drop file. Never touches a store or a
    clone. Returns the final file name. May raise; the caller (``feed_safely``) swallows."""
    if outcome.kind not in (READ, CAPPED, NOT_MEASURED) + _UNREADABLE:
        raise ValueError(f"unknown probe outcome kind {outcome.kind!r}")
    if not PERSONA_RE.match(binding.persona or ""):
        raise ValueError(f"persona {binding.persona!r} is not a valid drop-file persona")
    directory = drop_dir(drop)
    now = now or datetime.now(timezone.utc)
    payload = (json.dumps(sample_body(binding, outcome, now), sort_keys=True) + chr(10)).encode("utf-8")
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    run = _slug(outcome.run_id) if outcome.run_id else "nr"
    fd, tmp = _mkstemp(directory)
    try:
        try:
            os.write(fd, payload)
            os.fchmod(fd, SAMPLE_MODE)  # independent of the seat's umask
            os.fsync(fd)
        finally:
            os.close(fd)
        for _ in range(20):
            name = f"{stamp}__{binding.persona}__{run}__{os.getpid()}{secrets.token_hex(3)}.json"
            try:
                os.link(tmp, directory / name)  # atomic publish; EEXIST => never overwrite
            except FileExistsError:
                continue
            _fsync_dir(directory)
            return name
        raise FileExistsError("could not find an unused drop file name")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def feed_safely(binding: Any, outcome: ProbeOutcome, **kw) -> str | None:
    """Advisory wrapper: never raises (except KeyboardInterrupt/SystemExit)."""
    try:
        return feed(binding, outcome, **kw)
    except Exception as exc:  # noqa: BLE001 -- the detector must never affect quota-check
        _log(f"detector failed (ignored; quota-check unaffected): {type(exc).__name__}: {exc}")
        return None
