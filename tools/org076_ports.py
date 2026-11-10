"""ORG-076 (T-5998): the episode-store runner's REAL page and liveness ports.

Serves ORG-076 fleet health-and-repair (RFC-004 section 6.6 "else page"): an episode with no live,
non-subject repair candidate must produce a real page, and a not-live candidate must really be skipped,
instead of the episode store's placeholders (assume-live liveness, stderr-only paging).

LIVENESS (``make_liveness`` / ``liveness_reason``). Source of truth: the sanitized fleet health export
``/run/pantheon-health/<persona>.json`` plus the ``_exporter.json`` heartbeat (tools/health_export.py,
written by Recovery's own exporter, so Recovery's account reads it with no sudo and no tmux), read through
``recovery_watcher.load_health`` (size-capped, O_NOFOLLOW, schema-checked, enum-sanitized).

Liveness is BINARY. A candidate is live iff ALL hold (enum values are health_export's own definitions:
``PANE_STATES`` idle|busy|modal|unknown, ``CONSUME_STATES`` active|inactive|failed|unknown,
``SEAT_STATES`` active|paused-intake|deep-sleep, ``expected_down`` bool, ``FIELDS`` for the record):
  * the exporter heartbeat is fresh (``recovery_watcher.EXPORT_STALE_S``, with the future-skew bound);
  * the candidate's record exists and is fresh (same bound);
  * ``expected_down`` is not true;
  * ``seat_state == "active"``  (paused-intake and deep-sleep are NOT live);
  * ``pane_state`` in (idle, busy)  (modal and unknown are NOT live);
  * ``consume_unit != "failed"``.
So a deep-sleep / parked / paused watchman counts as NOT live: its episodes go to Recovery (any_live, the
three existing specs) or page. FAILURE DIRECTION: a missing, stale, unreadable, forged or invalid
signal, or any exception, is NOT live (never raises). Why: under section 6.6 and "never silently drop",
a wrongly-not-live candidate ends in Recovery or a visible page, while a wrongly-live one silently files a
repair ticket to a seat that will not read it.

PAGE (``make_page``). ``inbox_consumer.route_failure_alert("recovery", msg, vocab)``: the runner is Recovery's,
so the recovery-self rule applies (orchestrator's mailbox; the operator's Telegram only if that mailbox leg fails;
repeats of one vocab back off 1h / 4h / daily; a muted persona is suppressed fail-open). ``agent`` is
"recovery" on purpose: routing a "no eligible repair target" page to Recovery's own pane is useless when Recovery is
the dead candidate, and orchestrator can act. Backoff is keyed on ``vocab`` = ``org076-`` + a slug of the
subject (<= 40 chars) + ``-`` + sha1(WHOLE subject)[:12]: <= 64 chars, [a-z0-9-] only, and two long
subjects that share a prefix (real episode ids) still get distinct keys. NOTE: a constant subject (for example
"ORG-076 drop file rejected") therefore shares ONE backoff window across files; subjects carrying an
episode id page once per episode. A page failure never raises: it logs and falls back to the store's
stderr page.

KNOWN LIMITS (T-5998 review): (1) the orchestrator mailbox leg needs group pantheon-mailbox: the runner unit nests
``sg pantheon-mailbox`` inside ``sg systemd-journal`` and must be reinstalled + daemon-reloaded. (2) The
recovery-self backoff file (recovery_self_alert_backoff.json) gains one entry per vocab and is never pruned.
(3) Rebase-edge pages (delivery_rebase_controller._deliver_locked) use a constant subject and can repeat
each cycle for an ambiguous episode, bounded only by that recovery-self backoff. (4) Registry detectors'
sample-freshness domains (org076_store_runner, ``org076_sample_freshness.spec_for(..., detector=)``,
candidates=(watchman,)) also get the Recovery any_live fallback; the registry DropDetector specs themselves
keep ``any_live_fallback=()``.

PAGE CALL SITES and their dedupe (every ``ports.page`` use; see also the T-5998 ticket):
  episode_store._route -> repair_evidence.resolve_route: once per episode (the route, including a
      ``page`` route, is cached in ``delivery.route``; known limit: a transient not-live reading at
      first routing pins the episode to its page route).
  episode_store reopen chain ("reopened N times"): once (``chain_paged`` durable before the page).
  episode_store "stuck at PUSH_INFLIGHT" (``_count_failure``): after N consecutive push failures, once
      (``push_paged`` flag, persisted AFTER the page: a crash in between can repeat it once).
  episode_store "silent push/verify loop": once (``verify_loop_paged`` flag, same ordering note).
  episode_store.make_rebase_edge (ORG-096, ``send_page``): the rebase edge's own counted bound and
      ``delivery-rebase`` state in _git_utils, outside this module's dedupe.
  org076_store_runner._page_once (corrupt domain, observe failure, guard, unreadable file, ...): once per
      key in ``state["paged"]``, cleared when the condition clears.
  org076_store_runner "drop file rejected" (quarantined): NOT through _page_once; one page per rejected
      file (the file moves to rejected/), and the constant subject shares one backoff window.
"""
from __future__ import annotations

import hashlib
import re
import sys
import time
from pathlib import Path
from typing import Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import recovery_watcher  # noqa: E402

LIVE_PANE_STATES = frozenset(("idle", "busy"))
VOCAB_PREFIX = "org076-"
VOCAB_LIMIT = 64
VOCAB_SLUG_LIMIT = 40   # readable part; a sha1 of the WHOLE subject keeps long subjects distinct
VOCAB_HASH_LEN = 12
MSG_LIMIT = 1500
PAGE_AGENT = "recovery"
# route_failure_alert("recovery") wraps the text as "[recovery-self-failure] ... Recovery cannot self-remediate; orchestrator,
# please check Recovery's seat" (inbox_consumer; no sanctioned generic orchestrator-alert helper exists), which
# misdescribes an ORG-076 page. This first line disambiguates it. Known limit; follow-up filed by mason.
PAGE_HEADER = "[ORG-076 episode page, not a Recovery seat fault]"


def _log(msg: str) -> None:
    print(f"[org076_ports] {msg}", file=sys.stderr)


def liveness_reason(candidate, *, health_dir: "Path | str" = recovery_watcher.DEFAULT_HEALTH_DIR,
                    clock: Callable[[], float] = time.time) -> "tuple[bool, str]":
    """(live, reason) for one repair candidate. Read-only, no side effects, never raises."""
    try:
        # health_export names each file <persona>.json and checks record persona == file name, so the
        # lookup is by the candidate's NAME only (never by account: another persona must not match).
        records, _invalid, exporter_at = recovery_watcher.load_health(Path(health_dir), frozenset({candidate.name}))
        now = clock()
        if exporter_at is None:
            return False, "export heartbeat missing or invalid"
        if now - exporter_at > recovery_watcher.EXPORT_STALE_S:
            return False, "export heartbeat stale"
        if exporter_at - now > recovery_watcher.EXPORT_FUTURE_SKEW_S:
            return False, "export heartbeat in the future"
        rec = records.get(candidate.name)
        if rec is None:
            return False, "no valid record"
        if now - rec["exported_at"] > recovery_watcher.EXPORT_STALE_S:
            return False, "record stale"
        if rec.get("expected_down") is True:
            return False, "expected_down"
        if rec.get("seat_state") != "active":
            return False, f"seat_state={rec.get('seat_state')}"
        if rec.get("pane_state") not in LIVE_PANE_STATES:
            return False, f"pane_state={rec.get('pane_state')}"
        if rec.get("consume_unit") == "failed":
            return False, "consume_unit=failed"
        return True, "live"
    except Exception as exc:  # noqa: BLE001 - fail toward not-live (page), never crash the cycle
        _log(f"liveness({getattr(candidate, 'name', '?')}) failed: {type(exc).__name__}: {exc}")
        return False, f"error {type(exc).__name__}"


def make_liveness(*, health_dir: "Path | str" = recovery_watcher.DEFAULT_HEALTH_DIR,
                  clock: Callable[[], float] = time.time) -> Callable[..., bool]:
    """The ``Ports.liveness`` callable: Candidate -> bool (binary; see the module docstring)."""
    def liveness(candidate) -> bool:
        ok, why = liveness_reason(candidate, health_dir=health_dir, clock=clock)
        if not ok:
            _log(f"{getattr(candidate, 'name', '?')} not live: {why}")
        return ok
    return liveness


def vocab_for(subject: str) -> str:
    """Bounded, charset-safe backoff key (<= 64 chars, [a-z0-9-]) derived from a page subject."""
    subject = str(subject)
    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:VOCAB_SLUG_LIMIT].rstrip("-")
    digest = hashlib.sha1(subject.encode("utf-8", errors="replace")).hexdigest()[:VOCAB_HASH_LEN]
    return f"{VOCAB_PREFIX}{slug}-{digest}" if slug else f"{VOCAB_PREFIX}{digest}"


def make_page(*, fallback: "Callable[[str, str], None] | None" = None) -> Callable[[str, str], None]:
    """The ``Ports.page`` callable: (subject, body) -> None. Never raises."""
    def page(subject: str, body: str) -> None:
        subject, body = str(subject), str(body)
        msg = f"{subject}: {body}"
        if len(msg) > MSG_LIMIT:
            msg = msg[:MSG_LIMIT - 3] + "..."
        try:
            from tools.inbox_consumer import route_failure_alert  # lazy: heavy import
            route_failure_alert(PAGE_AGENT, f"{PAGE_HEADER}\n{msg}", vocab_for(subject))
        except BaseException as exc:  # noqa: BLE001 - a page failure must not crash the cycle
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            _log(f"page transport failed ({type(exc).__name__}: {exc}); stderr fallback")
            try:
                if fallback is not None:
                    fallback(subject, body)
                else:
                    from tools import episode_store as es
                    es._default_page(subject, body)
            except Exception:  # noqa: BLE001
                pass
    return page
