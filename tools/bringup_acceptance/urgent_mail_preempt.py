"""urgent-mail-preempt acceptance module (T-2556 phase-N / T-2585, parent T-2556).

Closes the gap T-2585 names in the T-2556 comprehensive suite: the existing
``mailbox-roundtrip`` module proves the mailbox DELIVERY SUBSTRATE only
(normal send -> new/ -> claim -> cur/ -> ack) and, by that module's own
docstring, carries zero urgent/severity/preempt coverage. This module
formalizes the urgent-preempt CAPABILITY (T-1641, ORG-026 Phase 2) into the
closure matrix: a recipient-side PreToolUse hook non-destructively preempting
a busy agent at its NEXT tool-call boundary, the rate-limit
downgrade-never-drop guard, and the escalation-ladder backstop for a
wedged/capped/no-hook-parity recipient.

Four off-prod-buildable legs, run against the REAL T-1641 code (only the
Telegram/tmux-touching alert LEAF is mocked -- see leg 4), all against a
fully DISPOSABLE hub so the real mailbox hub is never touched:

  1. ``marker_write`` -- a real ``mailbox.send(severity="urgent")`` (Phase 2
     flag on) drops the real ``.urgent_pending`` marker (``_write_urgent_marker``)
     with the shape the hook expects; verified from disk, not from send()'s
     return value alone.
  2. ``hook_injects_and_clears`` -- ``tools/hooks/urgent-mail-inject.py`` is
     invoked as a REAL SUBPROCESS (the actual stdin/stdout contract, same
     recipe ``tools/hooks/test_urgent_mail_inject.py``'s own
     ``TestEndToEndSubprocess`` already established) against leg 1's live
     marker: asserts the ``additionalContext`` nudge byte-matches the hook's
     OWN ``build_nudge()`` (loaded via ``importlib`` from the hyphenated
     file, never hand-duplicated -- see DECISIONS) and that the marker is
     gone afterward (the "exactly one nudge per urgent send" invariant).
  3. ``rate_limit_downgrade`` -- N+1 urgent sends from one (sender, inbox)
     pair, N pinned small via ``mock.patch.object(mailbox,
     "URGENT_RATE_LIMIT_COUNT", ...)`` (see DECISIONS for why the env var
     alone cannot control this) -- proves downgrade-never-drop: the
     overflow send's ON-DISK record carries ``severity: "normal"`` (not
     "urgent", never absent), the ``.urgent_rate/<sender>.downgrades.jsonl``
     audit trail gained exactly one line, and the ``.urgent_pending``
     marker still names the last WITHIN-limit send -- never the downgraded
     one (downgraded sends must not call ``_write_urgent_marker``).
  4. ``escalation_ladder`` -- a real urgent send whose ON-DISK ``ts_ns`` is
     then hand-backdated past ``urgent_escalation.URGENT_ESCALATION_THRESHOLD_
     SECONDS`` (the minimal way to exercise "stale" without a real sleep --
     see DECISIONS), then ``urgent_escalation.run()`` (real function) with
     ONLY its Telegram/tmux-touching leaf (``_alert``) mocked -- proves the
     backstop fires exactly once, names the right agent/message, and
     durably marks ``.urgent_escalated/<id>`` so a re-run does not re-alert.

LIVE LEG (deferred, cited -- not attempted in-suite, mirrors harness_fallback_
swap.py's own T-2572 citation verbatim in shape): a REAL working agent
actually preempted mid-turn. HO-attested via ledger entry
``2026-08-05T12:27:39Z-t1641-urgent-preempt-proven-live-recovery`` (T-1641,
closes T-2537): orchestrator -> recovery severity=urgent -> ``.urgent_pending`` marker
-> Recovery's next tool call fired the REAL PreToolUse hook -> injected the
URGENT drain via ``additionalContext`` MID-TURN -> Recovery drained+acked ->
marker cleared (exactly one nudge); double-confirmed (Recovery pane + an
independent mailbox reply). NOT independently re-verified by this module --
this module's own row is ``proven`` for the off-prod half only; the
composite capability :data:`COMPOSITE_ID` additionally needs a landed
``urgent-mail-preempt-live`` module (mirrors every other self-pair composite
in ``composites.py`` -- ``harness-runtime``, ``harness-fallback-swap``,
``forced-cap-return``) before it reads GREEN. The live event above is prose
evidence for a human reader, never a machine-checked green.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
import uuid
from importlib import util as importlib_util
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from tools import mailbox
from tools import urgent_escalation
from tools.bringup_acceptance import restore as restore_mod
from tools.bringup_acceptance.evidence import RunPaths, write_module_evidence
from tools.bringup_acceptance.recorder import TraceRecorder
from tools.bringup_acceptance.types import (
    HardStop,
    MatrixRow,
    ModuleDeclaration,
    ModuleEvidence,
    RestoreEvidence,
    RunContext,
    TargetInventory,
)

MODULE_ID = "urgent-mail-preempt"
COMPOSITE_ID = "urgent-mail-preempt-fully-functional"

# The real hub this module must NEVER write to -- module-level constant, not
# imported from mailbox.py/persona_bringup.py, mirroring mailbox_roundtrip.py's
# own guard (see that module's DECISIONS for why this is deliberately NOT
# shared private state across siblings).
_REAL_HUB_DEFAULT = Path("/home/the-ancestor/mailbox")

# One shared sender identity string (never provisioned as its own inbox --
# no leg here needs a reply route back to a sender mailbox, unlike
# mailbox_roundtrip.py). Rate-limiting is keyed by (inbox, sender), and each
# leg below uses its OWN disposable recipient inbox, so reusing one sender
# string across legs cannot cross-contaminate the rate-limit leg.
_SENDER_PERSONA = "urgent-preempt-sender"
_MARKER_RECIPIENT_PERSONA = "urgent-preempt-marker-recipient"
_RATE_RECIPIENT_PERSONA = "urgent-preempt-rate-recipient"
_ESCALATION_RECIPIENT_PERSONA = "urgent-preempt-escalation-recipient"

# Pinned small so the rate-limit leg is deterministic regardless of ambient
# env or import order (see DECISIONS: mailbox.URGENT_RATE_LIMIT_COUNT is
# frozen at import time, NOT re-read from the env per call the way
# urgent_phase2_enabled() is -- so this module patches the module attribute
# directly rather than relying on an env var).
_RATE_LIMIT_COUNT = 2

_HOOK_PATH = Path(__file__).resolve().parent.parent / "hooks" / "urgent-mail-inject.py"

# The live-leg citation (see module docstring). Named constants so
# report()'s summary and this docstring cannot silently drift apart.
LIVE_LEG_LEDGER_ID = "2026-08-05T12:27:39Z-t1641-urgent-preempt-proven-live-recovery"
LIVE_LEG_TICKET = "T-1641"
_LIVE_LEG_CITATION = (
    f"live leg: HO-attested via ledger entry {LIVE_LEG_LEDGER_ID} ({LIVE_LEG_TICKET}, "
    "closes T-2537) -- orchestrator->recovery severity=urgent -> .urgent_pending marker -> Recovery's "
    "next tool call fired the real PreToolUse hook -> injected the URGENT drain via "
    "additionalContext MID-TURN -> Recovery drained+acked -> marker cleared (exactly one "
    "nudge); double-confirmed (Recovery pane + an independent mailbox reply). NOT "
    "independently re-verified by this module -- composite 'urgent-mail-preempt-fully-"
    "functional' additionally needs a landed urgent-mail-preempt-live module before GREEN."
)


def _load_hook_module():
    """Dotted-import the hyphenated hook file, same recipe
    tools/hooks/test_urgent_mail_inject.py already uses -- so this module's
    expected nudge text is derived from the hook's OWN build_nudge(), never
    hand-duplicated (see DECISIONS)."""

    spec = importlib_util.spec_from_file_location("urgent_mail_inject_for_acceptance", _HOOK_PATH)
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hook_subprocess_env(inbox: Path) -> dict[str, str]:
    """Minimal env for the real hook subprocess -- mirrors
    TestEndToEndSubprocess._env() in tools/hooks/test_urgent_mail_inject.py."""

    env = {
        "URGENT_MAIL_INBOX": str(inbox),
        "PATH": "/usr/bin:/bin",
        "PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED": "1",
    }
    if "LD_LIBRARY_PATH" in os.environ:
        env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
    return env


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- legs


def _marker_write_leg(*, inbox: Path, nonce: str) -> Mapping[str, Any]:
    subject = f"urgent-mail-preempt:{nonce}"
    message_id = mailbox.send(str(inbox), {"nonce": nonce}, _SENDER_PERSONA, subject=subject, severity="urgent")
    marker_path = inbox / ".urgent_pending"
    if not marker_path.exists():
        raise HardStop(f"marker_write: no .urgent_pending written under {inbox} after an urgent send")
    marker = _read_json(marker_path)
    if marker.get("msgid") != message_id:
        raise HardStop(f"marker_write: marker msgid {marker.get('msgid')!r} != sent id {message_id!r}")
    if marker.get("sender") != _SENDER_PERSONA:
        raise HardStop(f"marker_write: marker sender {marker.get('sender')!r} != {_SENDER_PERSONA!r}")
    if marker.get("subject") != subject:
        raise HardStop(f"marker_write: marker subject {marker.get('subject')!r} != {subject!r}")
    return {"message_id": message_id, "subject": subject, "marker": marker}


def _hook_injects_and_clears_leg(*, inbox: Path) -> Mapping[str, Any]:
    marker_path = inbox / ".urgent_pending"
    if not marker_path.exists():
        raise HardStop(f"hook_injects_and_clears: expected a pending marker under {inbox} before invoking the hook")
    # Read BEFORE invoking the hook -- a correct injection clears the marker,
    # so anything read after the subprocess call would race the clear.
    marker_before = _read_json(marker_path)
    hook_module = _load_hook_module()
    try:
        proc = subprocess.run(
            [sys.executable, str(_HOOK_PATH)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "true"}}),
            capture_output=True, text=True, env=_hook_subprocess_env(inbox), timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise HardStop(f"hook_injects_and_clears: hook subprocess timed out: {exc}") from exc
    if proc.returncode != 0:
        raise HardStop(f"hook_injects_and_clears: hook exited {proc.returncode}: stderr={proc.stderr!r}")
    if not proc.stdout.strip():
        raise HardStop("hook_injects_and_clears: hook emitted no output despite a pending marker")
    try:
        obj = json.loads(proc.stdout)
    except ValueError as exc:
        raise HardStop(f"hook_injects_and_clears: hook stdout is not JSON: {proc.stdout!r}") from exc
    hso = obj.get("hookSpecificOutput", {})
    if hso.get("hookEventName") != "PreToolUse":
        raise HardStop(f"hook_injects_and_clears: unexpected hookEventName: {hso!r}")
    if hso.get("permissionDecision") != "allow":
        raise HardStop(f"hook_injects_and_clears: unexpected permissionDecision: {hso!r}")
    additional_context = hso.get("additionalContext")
    expected = hook_module.build_nudge(marker_before["sender"], marker_before["subject"])
    if additional_context != expected:
        raise HardStop(
            f"hook_injects_and_clears: additionalContext {additional_context!r} != expected {expected!r}"
        )
    if marker_path.exists():
        raise HardStop("hook_injects_and_clears: marker was not cleared after injection")
    return {"additional_context": additional_context, "returncode": proc.returncode}


def _rate_limit_downgrade_leg(*, inbox: Path, nonce: str) -> Mapping[str, Any]:
    with mock.patch.object(mailbox, "URGENT_RATE_LIMIT_COUNT", _RATE_LIMIT_COUNT):
        in_limit_ids = []
        for index in range(_RATE_LIMIT_COUNT):
            subject = f"urgent-rate:{nonce}-{index}"
            message_id = mailbox.send(
                str(inbox), {"nonce": f"{nonce}-{index}"}, _SENDER_PERSONA, subject=subject, severity="urgent",
            )
            in_limit_ids.append(message_id)
        overflow_subject = f"urgent-rate:{nonce}-overflow"
        overflow_id = mailbox.send(
            str(inbox), {"nonce": f"{nonce}-overflow"}, _SENDER_PERSONA, subject=overflow_subject, severity="urgent",
        )

    overflow_record = _read_json(inbox / "new" / overflow_id)
    if overflow_record.get("severity") != "normal":
        raise HardStop(
            f"rate_limit_downgrade: overflow send severity {overflow_record.get('severity')!r} != 'normal' "
            "(downgrade-never-drop was not applied)"
        )

    downgrades_path = inbox / ".urgent_rate" / f"{_SENDER_PERSONA}.downgrades.jsonl"
    if not downgrades_path.exists():
        raise HardStop(f"rate_limit_downgrade: no downgrades audit trail at {downgrades_path}")
    downgrade_lines = downgrades_path.read_text(encoding="utf-8").splitlines()
    if len(downgrade_lines) != 1:
        raise HardStop(f"rate_limit_downgrade: expected exactly one downgrade log line, got {downgrade_lines!r}")
    downgrade_entry = json.loads(downgrade_lines[0])
    if downgrade_entry.get("reason") != "urgent_rate_limit_exceeded":
        raise HardStop(f"rate_limit_downgrade: unexpected downgrade reason: {downgrade_entry!r}")
    if downgrade_entry.get("sender") != _SENDER_PERSONA:
        raise HardStop(f"rate_limit_downgrade: downgrade log names the wrong sender: {downgrade_entry!r}")

    marker_path = inbox / ".urgent_pending"
    if not marker_path.exists():
        raise HardStop("rate_limit_downgrade: expected the marker from the last in-limit send to still be present")
    marker = _read_json(marker_path)
    if marker.get("msgid") != in_limit_ids[-1]:
        raise HardStop(
            f"rate_limit_downgrade: marker msgid {marker.get('msgid')!r} is not the last in-limit send "
            f"{in_limit_ids[-1]!r} -- the downgraded send must not (re)write the urgent marker"
        )
    return {
        "in_limit_ids": in_limit_ids, "overflow_id": overflow_id,
        "overflow_severity": overflow_record.get("severity"), "downgrade_entry": downgrade_entry,
    }


def _escalation_ladder_leg(*, hub: Path, inbox: Path, nonce: str) -> Mapping[str, Any]:
    subject = f"urgent-escalation:{nonce}"
    message_id = mailbox.send(str(inbox), {"nonce": nonce}, _SENDER_PERSONA, subject=subject, severity="urgent")
    record_path = inbox / "new" / message_id
    record = _read_json(record_path)
    backdate_seconds = urgent_escalation.URGENT_ESCALATION_THRESHOLD_SECONDS + 60
    record["ts_ns"] = int(record["ts_ns"] - backdate_seconds * 1_000_000_000)
    record["ts"] = record["ts_ns"] / 1_000_000_000
    record_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with mock.patch.object(urgent_escalation, "_alert") as alert_mock:
        result = urgent_escalation.run(hub=hub, now=time.time(), dry_run=False)

    if not result.get("enabled"):
        raise HardStop(f"escalation_ladder: urgent_escalation.run() reported disabled: {result!r}")
    if alert_mock.call_count != 1:
        raise HardStop(f"escalation_ladder: expected exactly one alert, got {alert_mock.call_count}")
    call_args, _ = alert_mock.call_args
    agent_arg, message_arg = call_args
    if agent_arg != inbox.name:
        raise HardStop(f"escalation_ladder: alert named agent {agent_arg!r}, expected {inbox.name!r}")
    if _SENDER_PERSONA not in message_arg or subject not in message_arg:
        raise HardStop(f"escalation_ladder: alert message missing sender/subject context: {message_arg!r}")

    escalated_marker = inbox / ".urgent_escalated" / message_id
    if not escalated_marker.exists():
        raise HardStop(f"escalation_ladder: no .urgent_escalated marker written at {escalated_marker}")
    escalated_ids = {entry.get("id") for entry in result.get("escalated", [])}
    if message_id not in escalated_ids:
        raise HardStop(f"escalation_ladder: {message_id!r} not reported in run()'s escalated list: {result!r}")

    return {"message_id": message_id, "subject": subject, "alert_agent": agent_arg, "alert_message": message_arg}


# ---------------------------------------------------------------------------
# The AcceptanceModule
# ---------------------------------------------------------------------------


class UrgentMailPreemptModule:
    """Proves the urgent-mail-preempt capability's off-prod half against a
    disposable hub. See the module docstring for the live-leg deferral."""

    id = MODULE_ID
    tier = "core"
    contributes_to = (COMPOSITE_ID,)

    def __init__(self) -> None:
        self._baseline: restore_mod.Baseline | None = None

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        entries = [entry for entry in target.entries if entry.module_id == self.id]
        applies = any(entry.status == "applies" for entry in entries)
        if applies:
            reason = f"inventory declares urgent-mail-preempt at {entries[0].path} for persona {target.persona}"
        elif entries:
            reason = f"inventory marks the urgent-mail-preempt entry absent for persona {target.persona}"
        else:
            reason = f"no urgent-mail-preempt inventory entry for persona {target.persona}"
        return ModuleDeclaration(
            module_id=self.id, tier=self.tier, applies=applies, reason=reason,
            contributes_to=self.contributes_to,
        )

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        if ctx.hub_path.resolve() == _REAL_HUB_DEFAULT.resolve():
            raise HardStop(
                f"refusing to prove urgent-mail-preempt against the REAL hub {ctx.hub_path} -- "
                "RunContext.hub_path must be a disposable tree"
            )

        marker_inbox = ctx.hub_path / _MARKER_RECIPIENT_PERSONA
        rate_inbox = ctx.hub_path / _RATE_RECIPIENT_PERSONA
        escalation_inbox = ctx.hub_path / _ESCALATION_RECIPIENT_PERSONA
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)

        # Baseline FIRST -- before anything risky -- so restore() can still
        # clean up even if a later leg raises HardStop partway (mirrors
        # mailbox_roundtrip.py's own ordering).
        target = restore_mod.BaselineTarget(
            account=ctx.account,
            mailbox_dirs={
                "marker-new": marker_inbox / "new",
                "rate-new": rate_inbox / "new",
                "escalation-new": escalation_inbox / "new",
            },
            disposable_markers=(marker_inbox, rate_inbox, escalation_inbox),
            backup_root=paths.module_dir(self.id) / "backups",
        )
        self._baseline = restore_mod.record_baseline(target)
        recorder.append(
            "baseline",
            {"marker_inbox": marker_inbox, "rate_inbox": rate_inbox, "escalation_inbox": escalation_inbox},
        )

        with mock.patch.dict(
            os.environ,
            {
                "PANTHEON_MAILBOX_HUB": str(ctx.hub_path),
                "PANTHEON_PERSONA_STATE_DIR": str(ctx.state_path),
                "PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED": "1",
            },
        ):
            mailbox.provision(marker_inbox)
            mailbox.provision(rate_inbox)
            mailbox.provision(escalation_inbox)

            nonce = uuid.uuid4().hex
            evidence_paths: list[Path] = []
            legs: dict[str, Any] = {}

            marker_detail = _marker_write_leg(inbox=marker_inbox, nonce=nonce)
            recorder.append("marker_write", marker_detail)
            legs["marker_write"] = marker_detail
            evidence_paths.append(write_module_evidence(paths, self.id, "marker_write", marker_detail))

            hook_detail = _hook_injects_and_clears_leg(inbox=marker_inbox)
            recorder.append("hook_injects_and_clears", hook_detail)
            legs["hook_injects_and_clears"] = hook_detail
            evidence_paths.append(write_module_evidence(paths, self.id, "hook_injects_and_clears", hook_detail))

            rate_detail = _rate_limit_downgrade_leg(inbox=rate_inbox, nonce=nonce)
            recorder.append("rate_limit_downgrade", rate_detail)
            legs["rate_limit_downgrade"] = rate_detail
            evidence_paths.append(write_module_evidence(paths, self.id, "rate_limit_downgrade", rate_detail))

            escalation_detail = _escalation_ladder_leg(hub=ctx.hub_path, inbox=escalation_inbox, nonce=nonce)
            recorder.append("escalation_ladder", escalation_detail)
            legs["escalation_ladder"] = escalation_detail
            evidence_paths.append(write_module_evidence(paths, self.id, "escalation_ladder", escalation_detail))

        deferred_detail = {"ledger_id": LIVE_LEG_LEDGER_ID, "ticket": LIVE_LEG_TICKET, "citation": _LIVE_LEG_CITATION}
        recorder.append("live_leg_deferred", deferred_detail)
        legs["live_leg_deferred"] = deferred_detail
        evidence_paths.append(write_module_evidence(paths, self.id, "live_leg_deferred", deferred_detail))

        recorder.append("verdict", {"status": "proven"})
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=recorder.path,
            evidence_paths=tuple(evidence_paths), detail={"nonce": nonce, "legs": legs},
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        if self._baseline is None:
            raise HardStop("restore() called before prove() recorded a baseline")
        with mock.patch.dict(
            os.environ,
            {"PANTHEON_MAILBOX_HUB": str(ctx.hub_path), "PANTHEON_PERSONA_STATE_DIR": str(ctx.state_path)},
        ):
            restore_mod.restore_to(self._baseline)
            post = restore_mod.record_baseline(self._baseline.target)
        restore_mod.verify_zero_residue(self._baseline, post)
        return RestoreEvidence(
            module_id=self.id, restored=True,
            detail=restore_mod.restore_payload(self._baseline, post, restored=True),
        )

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=(
                "urgent-mail-preempt off-prod half proven from ground truth against a "
                "disposable hub: real .urgent_pending marker write, the real recipient-side "
                "PreToolUse hook injecting+clearing via a real subprocess, downgrade-never-"
                "drop rate-limiting, and the escalation-ladder backstop (Telegram/tmux leaf "
                "mocked). " + _LIVE_LEG_CITATION
            ),
            evidence_refs=evidence.evidence_paths,
        )
