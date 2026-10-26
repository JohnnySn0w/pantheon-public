"""mailbox-roundtrip acceptance module (T-2556 phase 4 / T-2563).

Nonce-proves the ORG-026 mailbox durable-delivery SUBSTRATE: send -> new/ ->
claim -> cur/ -> ack, a correlated reply-route roundtrip, and the transport's
at-least-once idempotency guarantee -- all against a fully DISPOSABLE hub and
state tree so the real mailbox hub is never touched. This module proves the
SUBSTRATE only; the composite ``mailbox-fully-functional`` capability (see
``tools.bringup_acceptance.closure``) additionally needs the live
lands-and-executes proof (T-2570) before it reads GREEN -- this module's own
report() names that gap explicitly rather than implying delivery is proven.

IDEMPOTENCY SCOPE (verified against ``tools/mailbox.py`` at T-2563 build time,
including an empirical reproduction -- not assumed, and not what an initial
review pass claimed; see this ticket's DECISIONS): the transport dedupes
ONLY against ``new/`` -- a byte-identical resend of a message still PENDING
(unconsumed) collapses to the original id (T-1935, ``_find_pending_duplicate``,
which scans ``new_dir`` only). Past that boundary there is no substrate-level
recognition at all, in either reachable shape:
(a) a byte-identical ``mailbox.send()`` retry AFTER the original was acked
    into ``cur/`` is, by the transport's own documented contract ("A
    consumed message never dedupes (new/ only): re-sending the same content
    later is a fresh delivery" -- ``mailbox.send`` docstring), a FRESH
    delivery under a new id;
(b) a message with the SAME on-disk id physically reappearing in ``new/``
    post-ack (a crash/restore/replay shape, not a ``mailbox.send()`` call)
    is REPROCESSED -- ``_process()`` (tools/mailbox.py ~L1027-1139) calls the
    handler unconditionally for every claimed name; nothing checks ``cur/``
    or ``.acks.jsonl`` first. (The ``already_acked`` result key, T-2279, is a
    different thing entirely: a concurrent-drain RACE on mail nobody has
    acked yet, not a persistent already-acked recognition.)
Either way, staying correct under redelivery is the CONSUMER's job
(``mailbox.py``'s own module docstring: "Ack is at-least-once ... HANDLER
MUST BE IDEMPOTENT"), not a substrate guarantee. So this module's
pending-retry-dedupe proof (``_verify_pending_dedupe``) IS the full extent
of what the SUBSTRATE itself promises about idempotency -- there is no
second, ack-aware dedup path to prove because none exists. See
``MailboxSubstrateDedupeContractTest`` for the empirical pin of both (a)
and (b).
"""

from __future__ import annotations

import dataclasses
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from tools import agent_role_map
from tools import mailbox
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

MODULE_ID = "mailbox-roundtrip"
_SENDER_PERSONA = "roundtrip-sender"
_RECIPIENT_PERSONA = "roundtrip-recipient"
_CONSUME_LABEL = "mailbox-roundtrip-prove"

# The real hub this module must NEVER write to, mirroring
# ``persona_bringup.DEFAULT_MAILBOX_HUB`` / ``mailbox._ledger_target``'s own
# default. A module-level constant (not imported from either) so a change to
# either of those defaults does not silently widen or narrow this guard --
# see the DECISIONS on why this module does not share private state with its
# siblings. The value itself is the canonical manifest hub (instance.yaml
# mailbox.hub via agent_role_map, T-4096) — a public source, not sibling
# private state — so the guard follows the real hub across an ORG-087 move.
_REAL_HUB_DEFAULT = Path(agent_role_map.mailbox_hub())


@contextmanager
def _disposable_env(hub_path: Path, state_path: Path) -> Iterator[None]:
    """Force PANTHEON_MAILBOX_HUB/PANTHEON_PERSONA_STATE_DIR to the disposable
    tree for the duration of the block, restoring whatever was there after.

    Belt-and-suspenders: nothing this module calls today reads
    PANTHEON_PERSONA_STATE_DIR, and mailbox.py's only PANTHEON_MAILBOX_HUB
    reader (``_ledger_target``) already resolves to None for any inbox whose
    parent is not the REAL hub -- which a disposable path already is by
    construction (see ``_REAL_HUB_DEFAULT`` guard in ``prove``). This holds
    even if a future mailbox.py change starts consulting the env var
    directly, rather than relying on every future caller re-deriving the
    same safety argument.
    """
    keys = ("PANTHEON_MAILBOX_HUB", "PANTHEON_PERSONA_STATE_DIR")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ["PANTHEON_MAILBOX_HUB"] = str(hub_path)
    os.environ["PANTHEON_PERSONA_STATE_DIR"] = str(state_path)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _list_files(directory: Path) -> tuple[str, ...]:
    if not directory.exists():
        return ()
    return tuple(sorted(entry.name for entry in directory.iterdir() if entry.is_file()))


def _verify_pending_dedupe(
    *, first_id: str, dedupe_id: str, pending_names: tuple[str, ...]
) -> None:
    """Assert a byte-identical pending retry collapsed to the original id.

    This is the SUBSTRATE's own idempotency guarantee (T-1935's
    ``dedupe_pending``) -- see the module docstring's IDEMPOTENCY SCOPE note
    for why a post-ack resend is deliberately out of scope here.
    """
    if dedupe_id != first_id or pending_names != (first_id,):
        raise HardStop(
            "idempotency violated: a byte-identical pending retry was not "
            f"deduped (first={first_id!r} dedupe={dedupe_id!r} pending={pending_names!r})"
        )


def _read_ack(inbox: Path, message_id: str) -> Mapping[str, Any]:
    """Read the ground-truth ack record for ``message_id`` from ``.acks.jsonl``."""

    acks_path = inbox / ".acks.jsonl"
    if not acks_path.exists():
        raise HardStop(f"no .acks.jsonl written under {inbox}")
    matches = []
    for line in acks_path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("id") == message_id:
            matches.append(record)
    if not matches:
        raise HardStop(f"no ack record for {message_id} in {acks_path}")
    if len(matches) != 1:
        raise HardStop(f"expected exactly one ack record for {message_id}, found {len(matches)}")
    record = matches[0]
    required = ("id", "ts_ns", "label", "pid", "uid")
    missing = [key for key in required if key not in record]
    if missing:
        raise HardStop(f"ack record for {message_id} missing fields: {missing}")
    if record["label"] != _CONSUME_LABEL:
        raise HardStop(f"ack record label {record['label']!r} does not match {_CONSUME_LABEL!r}")
    if record["pid"] != os.getpid():
        raise HardStop(f"ack record pid {record['pid']} does not match this process {os.getpid()}")
    if record["uid"] != os.getuid():
        raise HardStop(f"ack record uid {record['uid']} does not match this process {os.getuid()}")
    return record


def _read_sole_reply(inbox: Path) -> tuple[str, Mapping[str, Any]]:
    """Read the ground-truth sole reply landed in ``inbox``'s new/ (unconsumed)."""

    names = _list_files(inbox / "new")
    if len(names) != 1:
        raise HardStop(f"expected exactly one reply under {inbox}/new, found {names}")
    name = names[0]
    record = json.loads((inbox / "new" / name).read_text(encoding="utf-8"))
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise HardStop(f"reply {name} carries no payload mapping: {record!r}")
    return name, payload


def _verify_chain(
    *,
    recipient_inbox: Path,
    message_id: str,
    consume_result: Mapping[str, Any],
    received: list[Mapping[str, Any]],
) -> None:
    """Adversarially re-derive the correlated chain FROM DISK -- never trust
    ``consume_result`` (a caller-supplied dict) as the verdict on its own.
    """

    if consume_result.get("skipped"):
        raise HardStop(f"consume() was skipped: {consume_result}")
    if consume_result.get("quarantined"):
        raise HardStop(f"message was quarantined: {consume_result['quarantined']}")
    if (recipient_inbox / "failed" / message_id).exists():
        raise HardStop(f"message {message_id} found in failed/ quarantine")
    if message_id not in consume_result.get("processed", []):
        raise HardStop(f"message {message_id} not reported processed: {consume_result}")
    if (recipient_inbox / "new" / message_id).exists():
        raise HardStop(f"message {message_id} still present in new/ after consume")
    if not (recipient_inbox / "cur" / message_id).exists():
        raise HardStop(f"message {message_id} missing from cur/ after ack")
    if len(received) != 1 or received[0].get("id") != message_id:
        raise HardStop(
            f"handler observed {len(received)} message(s), expected exactly 1 matching {message_id}"
        )
    # return-to-idle: the substrate has no session-idle concept of its own --
    # idle means no claim/lease residue left behind after ack. This is that
    # check; there is no separate return-to-idle trace phase because there is
    # nothing else "idle" could mean at this layer.
    inflight_root = recipient_inbox / ".inflight"
    if inflight_root.exists() and any(inflight_root.iterdir()):
        raise HardStop(f"orphan in-flight claim(s) left under {inflight_root}")


@dataclasses.dataclass(frozen=True)
class _Chain:
    """Every ground-truth fact this run's prove() derived, JSON-safe."""

    nonce: str
    message_id: str
    dedupe_id: str
    processed: tuple[str, ...]
    ack_record: Mapping[str, Any]
    reply_id: str
    reply_payload: Mapping[str, Any]
    reaped: Mapping[str, Any]


class MailboxRoundtripModule:
    """Nonce-proves the mailbox durable-delivery substrate against a
    disposable hub. See the module docstring for scope."""

    id = MODULE_ID
    tier = "core"
    contributes_to = ("mailbox-fully-functional",)

    def __init__(self) -> None:
        self._baseline: restore_mod.Baseline | None = None

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        entries = [entry for entry in target.entries if entry.module_id == self.id]
        applies = any(entry.status == "applies" for entry in entries)
        if applies:
            reason = (
                f"inventory declares a mailbox at {entries[0].path} for persona {target.persona}"
            )
        elif entries:
            reason = f"inventory marks the mailbox-roundtrip entry absent for persona {target.persona}"
        else:
            reason = f"no mailbox-roundtrip inventory entry for persona {target.persona}"
        return ModuleDeclaration(
            module_id=self.id, tier=self.tier, applies=applies, reason=reason,
            contributes_to=self.contributes_to,
        )

    def prove(self, ctx: RunContext) -> ModuleEvidence:
        if ctx.hub_path.resolve() == _REAL_HUB_DEFAULT.resolve():
            raise HardStop(
                f"refusing to prove mailbox-roundtrip against the REAL hub {ctx.hub_path} -- "
                "RunContext.hub_path must be a disposable tree"
            )

        sender_inbox = ctx.hub_path / _SENDER_PERSONA
        recipient_inbox = ctx.hub_path / _RECIPIENT_PERSONA
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)

        # Baseline FIRST -- before anything risky -- so restore() can still
        # clean up even if the rest of this method raises HardStop partway.
        target = restore_mod.BaselineTarget(
            account=ctx.account,
            mailbox_dirs={
                "sender-new": sender_inbox / "new",
                "recipient-new": recipient_inbox / "new",
                "recipient-cur": recipient_inbox / "cur",
            },
            disposable_markers=(sender_inbox, recipient_inbox),
            backup_root=paths.module_dir(self.id) / "backups",
        )
        self._baseline = restore_mod.record_baseline(target)
        recorder.append(
            "baseline",
            {"sender_inbox": sender_inbox, "recipient_inbox": recipient_inbox},
        )

        with _disposable_env(ctx.hub_path, ctx.state_path):
            mailbox.provision(sender_inbox)
            mailbox.provision(recipient_inbox)

            nonce = uuid.uuid4().hex
            payload = {
                "nonce": nonce,
                "correlation_id": nonce,
                "reply_route": {"recipient_inbox": str(sender_inbox)},
            }
            first_id = mailbox.send(
                str(recipient_inbox), payload, _SENDER_PERSONA, subject=f"mailbox-roundtrip:{nonce}",
            )
            recorder.append("send", {"nonce": nonce, "message_id": first_id})

            dedupe_id = mailbox.send(
                str(recipient_inbox), payload, _SENDER_PERSONA, subject=f"mailbox-roundtrip:{nonce}",
            )
            pending_names = _list_files(recipient_inbox / "new")
            _verify_pending_dedupe(first_id=first_id, dedupe_id=dedupe_id, pending_names=pending_names)
            recorder.append(
                "idempotency-pending-dedupe",
                {"dedupe_id": dedupe_id, "pending": pending_names},
            )

            received: list[Mapping[str, Any]] = []

            def _handler(msg: dict) -> None:
                if not isinstance(msg.get("payload"), dict) or msg["payload"].get("nonce") != nonce:
                    raise HardStop(f"handler observed unexpected message: {msg.get('id')}")
                received.append(msg)
                route = msg["payload"]["reply_route"]
                mailbox.send(
                    route["recipient_inbox"],
                    {
                        "kind": "persona_result",
                        "correlation_id": msg["payload"]["correlation_id"],
                        "nonce": nonce,
                    },
                    _RECIPIENT_PERSONA,
                    subject=f"result:{msg['payload']['correlation_id']}",
                )

            consume_result = mailbox.consume(str(recipient_inbox), _handler, label=_CONSUME_LABEL)
            recorder.append("consume", {"result": consume_result})

        _verify_chain(
            recipient_inbox=recipient_inbox,
            message_id=first_id,
            consume_result=consume_result,
            received=received,
        )
        recorder.append("chain-verify", {"message_id": first_id})

        ack_record = _read_ack(recipient_inbox, first_id)
        reply_id, reply_payload = _read_sole_reply(sender_inbox)
        if reply_payload.get("correlation_id") != nonce:
            raise HardStop(
                f"reply-route mismatch: expected correlation_id {nonce!r}, got "
                f"{reply_payload.get('correlation_id')!r}"
            )
        recorder.append(
            "reply-route-verify", {"reply_id": reply_id, "reply_payload": reply_payload},
        )

        chain = _Chain(
            nonce=nonce, message_id=first_id, dedupe_id=dedupe_id,
            processed=tuple(consume_result["processed"]), ack_record=ack_record,
            reply_id=reply_id, reply_payload=reply_payload,
            reaped=consume_result.get("reaped", {}),
        )
        evidence_paths = (
            write_module_evidence(paths, self.id, "chain", dataclasses.asdict(chain)),
        )
        return ModuleEvidence(
            module_id=self.id, status="proven", trace_path=recorder.path,
            evidence_paths=evidence_paths,
            detail={"nonce": nonce, "message_id": first_id, "reply_id": reply_id},
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        if self._baseline is None:
            raise HardStop("restore() called before prove() recorded a baseline")
        with _disposable_env(ctx.hub_path, ctx.state_path):
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
                "mailbox durable-delivery substrate: send->new/->claim->cur/->ack, "
                "reply-route correlation, and pending-retry idempotency all proven from "
                "ground truth against a disposable hub (substrate-proven). This is NOT "
                "the 'mailbox-fully-functional' composite -- that additionally needs "
                "T-2570's live lands-and-executes proof before it reads GREEN."
            ),
            evidence_refs=evidence.evidence_paths,
        )
