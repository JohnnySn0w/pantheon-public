"""ORG-076 BT5b (T-6000): the Telegram inbound channel-liveness detector's WRITER half, onto the episode store.

The Recovery-hosted ``channel_liveness_monitor`` probes each persona's inbound Telegram path every cycle
(``probe_bridge``: the Claude plugin bridge, or a codex poller holding the same bot) and hands the RAW
result here, BEFORE any alert cooldown. This module turns it into one schema-2 drop file
(``org076_store_runner`` envelope, T-5999), written atomically to the shared drop dir like
``org076_mail_delivery`` / ``quota_probe_detector``. The runner owns routing: the ``telegram-inbound``
``REGISTRY`` entry builds the episode spec with N=3, M=2 and derives the faulted seat's harness and account
from ``seat_for`` (imported from ``org076_mail_delivery``, the static ``mailbox_wake.AGENT_PANE`` map).

Domain ``telegram-inbound:<persona>``. Verdicts:
  up            -> healthy (including a codex poller serving the bot)
  down          -> broken   ``telegram-inbound-down:<persona>``
  inconclusive  -> unknown  ``telegram-inbound-unmeasurable:<persona>``
  no_bridge     -> NO verdict. ``probe_bridge`` returns it only when bot.pid is provably absent AND the bridge
                  .env is absent (T-1794): an UNCONFIGURED persona. A configured bridge that never spawned
                  (T-5256: bot.pid absent, .env present) is ``down`` and so broken.
A down read inside the T-1622 restart grace (unless the T-5165 crash-loop bound is reached, exactly as the
alert), a seat that is asleep / in maintenance / paused, a muted seat (every verdict) and a persona with no
seat get NO verdict and are logged as suppressed.

``unknown`` is deliberate: a persistent sudo/probe failure on Recovery's account is a real measurement fault the
repair tier should diagnose (one unknown episode per affected persona once it persists for N cycles).

EVIDENCE. The envelope ``detail`` is built only from the persona name (validated), the status word, a reason
code from the closed set ``REASONS`` and the fixed playbook path. The probe's own detail string (pids, a
cmdline excerpt) is NEVER passed through: it is classified by prefix and dropped (RFC-004 s6.15).

DARK by default: ``PANTHEON_ORG076_TELEGRAM_INBOUND_DETECTOR`` must be exactly ``"1"`` (read at call time).
The write is fail-safe: a failure is logged and the monitor's alerting/remediation is unaffected. The drop dir
is the quota writer's (``PANTHEON_ORG076_SAMPLES_DIR`` or its default); this module never creates it.

KNOWN LIMIT: ``seat_for`` is the STATIC harness map; a seat running its fallback harness is routed as its
primary one. A misclassification only reorders repair candidates; the subject-persona exclusion still holds.

ROLLOUT: runner with the registry entry live in the dedicated clone FIRST (an older runner quarantines and
pages every schema-2 file), then Recovery sets the switch on the monitor's unit.
UNINSTALL (ORG-068 Remove), in order: unset the switch; ``org076_store_runner.py --forget-persona <p>`` for
every enrolled persona BEFORE removing the registry entry; remove the ``telegram-inbound`` REGISTRY entry;
delete this module and its tests; remove the one call in ``channel_liveness_monitor.check_agent``.
Queued drops are left. Freshness is not opted in (asleep/paused/muted/unconfigured seats go silent).

Tests: python3 -m pytest tools/test_org076_telegram_inbound.py
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from tools import quota_probe_detector as qpd
    from tools.org076_mail_delivery import seat_for  # noqa: F401  (runner + writer share one seat map)
except ImportError:  # script run with tools/ on sys.path
    import quota_probe_detector as qpd  # type: ignore
    from org076_mail_delivery import seat_for  # type: ignore  # noqa: F401

ENV_SWITCH = "PANTHEON_ORG076_TELEGRAM_INBOUND_DETECTOR"
DETECTOR = "telegram-inbound"
SUBSYSTEM = "telegram-inbound"
FAULT_DOWN = "telegram-inbound-down"
FAULT_UNMEASURABLE = "telegram-inbound-unmeasurable"
FAULT_KINDS = frozenset({FAULT_DOWN, FAULT_UNMEASURABLE})
N, M = 3, 2
PLAYBOOK = "sop/remediation/telegram-plugin-mcp-drop-recovery.md"
ENVELOPE_SCHEMA = 2
ENVELOPE_KIND = "org076-detector-sample"

HEALTHY, BROKEN, UNKNOWN = "healthy", "broken", "unknown"
_VERDICT_FOR = {"up": HEALTHY, "down": BROKEN, "inconclusive": UNKNOWN}

# Closed reason set. Probe detail prefixes (channel_liveness_monitor._probe_claude_bridge / probe_bridge)
# map to a code; anything unrecognised is ``other``. The probe string itself never leaves this function.
_DOWN_PREFIXES = (
    ("bot.pid ABSENT but bridge configured", "pidfile-absent-configured"),
    ("bot.pid=", "pid-not-live-bridge"),   # DEAD / not the bun bridge / recycled pid: all "pid is not the bridge"
)
REASONS = frozenset({"bridge-live", "codex-poller-serves", "pidfile-absent-configured", "pid-not-live-bridge",
                     "probe-inconclusive", "other"})


def enabled(env: Any = None) -> bool:
    """Dark switch: exact ``"1"`` only, read at call time."""
    return (env if env is not None else os.environ).get(ENV_SWITCH) == "1"


def _log(msg: str) -> None:
    print(f"[org076_telegram_inbound] {msg}", file=sys.stderr)


def reason_for(status: str, detail: str) -> str:
    detail = detail if isinstance(detail, str) else ""
    if status == "up":
        return "codex-poller-serves" if detail.startswith("codex-telegram-poller") else "bridge-live"
    if status == "inconclusive":
        return "probe-inconclusive"
    if status == "down":
        for prefix, code in _DOWN_PREFIXES:
            if detail.startswith(prefix):
                return code
    return "other"


def fault_id(persona: str, verdict: str) -> "str | None":
    if verdict == HEALTHY:
        return None
    return f"{FAULT_DOWN if verdict == BROKEN else FAULT_UNMEASURABLE}:{persona}"


def detail_for(persona: str, status: str, reason: str) -> str:
    return (f"persona={persona} status={status} reason={reason} playbook={PLAYBOOK}")[:qpd.DETAIL_LIMIT]


def envelope(persona: str, status: str, probe_detail: str, account: str, run_id: str, now: datetime) -> dict:
    verdict = _VERDICT_FOR[status]
    return {
        "schema": ENVELOPE_SCHEMA, "kind": ENVELOPE_KIND, "detector": DETECTOR,
        "domain_key": f"{DETECTOR}:{persona}", "persona": persona, "account": account,
        "verdict": verdict, "fault_id": fault_id(persona, verdict),
        "detail": detail_for(persona, status, reason_for(status, probe_detail)), "run_id": run_id,
        "sample_id": str(uuid.uuid4()),
        "sampled_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def write_drop(body: dict, drop: "Path | str | None" = None, now: "datetime | None" = None) -> str:
    """Atomic append-only drop (tmp -> fsync -> link), mirroring org076_mail_delivery.write_drop. May raise."""
    persona = body["persona"]
    if not qpd.PERSONA_RE.match(persona or ""):
        raise ValueError(f"persona {persona!r} is not a valid drop-file persona")
    directory = qpd.drop_dir(drop)
    now = now or datetime.now(timezone.utc)
    payload = (json.dumps(body, sort_keys=True) + chr(10)).encode("utf-8")
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    run = qpd._slug(body["run_id"])
    fd, tmp = qpd._mkstemp(directory)
    try:
        try:
            os.write(fd, payload)
            os.fchmod(fd, qpd.SAMPLE_MODE)
            os.fsync(fd)
        finally:
            os.close(fd)
        for _ in range(20):
            name = f"{stamp}__{persona}__{run}__{os.getpid()}{secrets.token_hex(3)}.json"
            try:
                os.link(tmp, directory / name)
            except FileExistsError:
                continue
            qpd._fsync_dir(directory)
            return name
        raise FileExistsError("could not find an unused drop file name")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def run_token(now: datetime) -> str:
    """ONE id per probe invocation (not a time bucket): dedupe then catches only a true replay."""
    return f"{int(now.timestamp() * 1e9):x}{secrets.token_hex(2)}"


def feed_safely(persona: str, status: str, probe_detail: str, *, drop: "Path | str | None" = None,
                now: "datetime | None" = None, env: Any = None) -> "str | None":
    """Write one drop for one persona's raw probe result. Dark unless the switch is exactly "1". Never raises:
    a failure is logged and the monitor's alerting/remediation is unaffected. Returns the drop name or None."""
    try:
        if not enabled(env):
            return None
        if status not in _VERDICT_FOR:  # no_bridge (unconfigured) or anything unexpected: no verdict
            _log(f"{persona}: suppressed (status {status}) -- no verdict written")
            return None
        seat = seat_for(persona)
        if seat is None:
            _log(f"{persona}: suppressed (no seat in the static seat map) -- no verdict written")
            return None
        now = now or datetime.now(timezone.utc)
        return write_drop(envelope(persona, status, probe_detail, seat[1], f"tgi-{persona}-{run_token(now)}", now),
                          drop, now)
    except Exception as exc:  # noqa: BLE001
        _log(f"{persona}: drop failed (ignored; monitor alerting unaffected): {type(exc).__name__}: {exc}")
        return None
