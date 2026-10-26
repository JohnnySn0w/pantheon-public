#!/usr/bin/env python3
"""memory_store_repair.py - T-4503 (ORG-058): explicit, offline repair of a
corrupt memory ``events.jsonl`` / ``tombstones.jsonl``.

A mid-file line that is neither JSON nor a healed torn line (F8b, or the L2
double tear: a heal append that itself tore, leaving the first torn line
terminated mid-file with no voider) wedges every read of the store. This tool
repairs it WITHOUT rewriting the log (a rewrite shifts the byte offsets every
later ``torn_predecessor`` names, and destroys the audit trail):

1. every corrupt line is copied, bytes and all, to a write-once 0600 sidecar
   ``<root>/quarantine/<log>.<operation_id>.jsonl`` (fsynced);
2. ONE ``repair_void`` record naming those bytes (offset + length + digest) is
   APPENDED to the log; ``memory_store._read_jsonl`` then skips exactly them;
3. the result is verified (the log loads; the fold equals an independent fold
   of the surviving lines; projections rebuild).

Tombstones fail LOUD: a lost tombstone can resurrect forgotten data. A corrupt
line's content is unknowable, so voiding ANY corrupt line in tombstones.jsonl
ALWAYS requires ``--ack-tombstone-loss <digest>`` for that line; there is no
"proven" path. Without it the run exits 2 and writes nothing except the
re-appends below. On every --apply, acked or not (and even with no corrupt
line), the expected tombstones are re-derived from durable evidence (forget
receipts that record a retained ``tombstone:<memory_id>`` surface, plus the
admitted versions' source refs) and any that are missing are RE-APPENDED before
the void. Re-appending an evidenced tombstone is always safe: it only narrows
retrieval. The report gives, per memory id, the expected / re-appended counts
and whether the evidence was complete, so the ack is informed.

Events: before anything is written, the retrievable set is computed from the
planned survivor fold (RETAINED_* + payload present and verified + not
tombstoned); the run REFUSES if a memory named by a non-refused remove-derived
/ expire forget receipt, or holding ANY surviving tombstone, would be
retrievable (a voided line may have held its forget transition). That check is
NEVER overridable by an ack. Only memory ids and counts are reported. The
protected set comes from receipts, so when the receipt evidence is incomplete
(an unreadable or invalid receipt, an op with a bound request but no receipt,
a tombstone from an op with no receipt) voiding a corrupt events line also
needs ``--ack-event-loss <digest>`` for that line; the ack overrides the
evidence gap only.

A torn residue that happens to parse as a JSON object was fully written, so it
is a valid record: it is neither repaired nor voided. (Only a final
UNTERMINATED segment is a torn tail, left for the next append to heal.)

The report carries class / line / offset / length / digest ONLY, never bytes.

Exit codes: 0 clean or applied+verified; 1 dry run found work; 2 refused (or a
verify failure; nothing more is written); 3 usage / lock / not a store.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import datetime as _dt
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_store as ms  # noqa: E402
from memory_lifecycle_ops import LifecycleOutcome, SurfaceStatus, forget_receipt_from_json, validate_forget_receipt  # noqa: E402
from memory_store import StoreIntegrityError, fold_events  # noqa: E402

LOGS = {"events": "events.jsonl", "tombstones": "tombstones.jsonl"}

OK, BLANK, HEALED, REPAIRED = "ok", "blank", "healed-torn", "repair-voided"
TORN_TAIL, UNPARSEABLE, NONOBJECT = "torn-tail", "corrupt-unparseable", "corrupt-nonobject"
CORRUPT = (UNPARSEABLE, NONOBJECT)

EXIT_OK, EXIT_WORK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2, 3


class _Refused(Exception):
    """A refusal. The message names lines/digests/ids, never content."""


@dataclasses.dataclass
class Line:
    n: int
    offset: int
    length: int
    digest: str
    cls: str
    raw: bytes = dataclasses.field(repr=False)  # never printed
    record: dict | None = dataclasses.field(default=None, repr=False)

    @property
    def is_survivor(self) -> bool:
        return self.cls == OK and self.record is not None and "repair_void" not in self.record


def classify(data: bytes, label: str) -> list[Line]:
    """Classify every physical line of a log (see module docstring)."""
    segs = data.split(b"\n")
    lines: list[tuple[int, bytes]] = []
    offset = 0
    for seg in segs[:-1]:
        lines.append((offset, seg))
        offset += len(seg) + 1
    parsed: list[object] = []
    for at, seg in lines:
        try:
            parsed.append(json.loads(seg) if seg.strip() else None)
        except ValueError:
            parsed.append(ValueError("unparseable"))
    repaired = ms._repair_void_set(lines, parsed, label)  # raises StoreIntegrityError on a forged void
    out: list[Line] = []
    for i, ((at, seg), rec) in enumerate(zip(lines, parsed)):
        digest = ms._digest_of(seg)
        nxt = parsed[i + 1] if i + 1 < len(parsed) else None
        if isinstance(nxt, dict) and ms._voided_line(nxt, at, seg):
            cls = HEALED
        elif (at, len(seg), digest) in repaired:
            cls = REPAIRED
        elif rec is None:
            cls = BLANK
        elif isinstance(rec, ValueError):
            cls = UNPARSEABLE
        elif not isinstance(rec, dict):
            cls = NONOBJECT
        else:
            cls = OK
        out.append(Line(i + 1, at, len(seg), digest, cls, seg, rec if isinstance(rec, dict) else None))
    tail = segs[-1]
    if tail:
        out.append(Line(len(lines) + 1, offset, len(tail), ms._digest_of(tail), TORN_TAIL, tail))
    return out


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tomb_key(rec: dict) -> tuple[object, str, str]:
    return (rec.get("memory_id"), str(rec.get("source_system")), ms.canonical_object_id(str(rec.get("object_id"))))


@dataclasses.dataclass
class Plan:
    log: str
    path: Path
    lines: list[Line]
    corrupt: list[Line]
    survivors: list[dict]
    missing: dict  # tombstones only: key -> record fields to re-append
    ack_needed: list[Line]
    acked: list[Line] = dataclasses.field(default_factory=list)
    evidence_ok: bool = True
    notes: list[str] = dataclasses.field(default_factory=list)
    expected: dict = dataclasses.field(default_factory=dict)  # tombstones only


@dataclasses.dataclass
class Evidence:
    expected: dict  # (memory_id, system, canonical object) -> fields to re-append
    complete: bool  # every receipt readable + valid, no unverifiable tombstone surface
    notes: list[str]
    forgotten: set[str]  # memory ids a non-refused remove-derived/expire receipt covers


def derive_expected_tombstones(
    store: ms.MemoryStore, event_survivors: list[dict] | None, tomb_survivors: list[dict] = ()  # type: ignore[assignment]
) -> Evidence:
    """Expected tombstones re-derived from durable evidence: forget receipts
    whose ``tombstone:<memory_id>`` surface is RETAINED_BY_POLICY (a tombstone
    was written and present when the receipt was made), crossed with the source
    refs of that memory's ADMITTED versions (the same derivation forget used).
    Also collects the memory ids covered by a non-refused remove-derived/expire
    forget (``forgotten``), which must never become retrievable again."""
    notes: list[str] = []
    ok = True
    retained: set[str] = set()
    unverifiable: set[str] = set()
    forgotten: set[str] = set()
    rdir = store.root / "receipts"
    for path in sorted(rdir.glob("*.json")) if rdir.is_dir() else ():
        try:
            raw = json.loads(path.read_bytes())
        except (OSError, ValueError):
            ok = False
            notes.append(f"receipt {path.name} unreadable")
            continue
        if not (isinstance(raw, dict) and "target_ids" in raw and "mode" in raw):
            continue  # not a forget receipt
        try:
            r = forget_receipt_from_json(raw)
            allowed = validate_forget_receipt(r).allowed
        except Exception:  # noqa: BLE001 - a receipt that fails to parse is never believed
            allowed = False
        if not allowed:
            ok = False
            notes.append(f"receipt {path.name} does not validate")
            # conservative: whatever it names as a target stays protected
            tids = raw.get("target_ids")
            if isinstance(tids, list):
                forgotten.update(t for t in tids if isinstance(t, str))
            continue
        if r.mode in (ms.LifecycleMode.REMOVE_DERIVED.value, ms.LifecycleMode.EXPIRE.value) and r.outcome != LifecycleOutcome.REFUSED:
            forgotten.update(
                s.surface[len("envelope-metadata:"):] for s in r.surfaces if s.surface.startswith("envelope-metadata:")
            )
            forgotten.update(
                s.surface[len("tombstone:"):] for s in r.surfaces if s.surface.startswith("tombstone:")
            )
        for s in r.surfaces:
            if s.surface.startswith("tombstone:"):
                mid = s.surface[len("tombstone:"):]
                if s.status == SurfaceStatus.RETAINED_BY_POLICY:
                    retained.add(mid)
                elif s.status == SurfaceStatus.UNVERIFIABLE:
                    unverifiable.add(mid)
    # A DELETED receipt cannot be read, so look for what it leaves behind: an op
    # whose request was bound but which has no receipt, and a surviving
    # tombstone written by an op that has no receipt (re-appends by this tool,
    # ``repair-*``, are exempt). Any tombstone also protects its memory (N2).
    odir = store.root / "ops"
    for p in sorted(odir.glob("*.request.json")) if odir.is_dir() else ():
        opid = p.name[: -len(".request.json")]
        if not store.receipt_paths(opid):
            ok = False
            notes.append(f"operation {opid}: request bound but no receipt")
    for rec in tomb_survivors:
        if isinstance(rec.get("memory_id"), str):
            forgotten.add(rec["memory_id"])
        opid = rec.get("operation_id")
        if isinstance(opid, str) and not opid.startswith("repair-") and not store.receipt_paths(opid):
            ok = False
            note = f"operation {opid}: tombstone present but no receipt"
            if note not in notes:
                notes.append(note)
    for mid in sorted(unverifiable - retained):
        ok = False
        notes.append(f"memory {mid}: receipt says a required tombstone was not present")
    expected: dict = {}
    if not retained:
        return Evidence(expected, ok, notes, forgotten)
    try:
        if event_survivors is None:
            event_survivors = list(store.events())
        admitted = sorted(fold_events(event_survivors))
    except StoreIntegrityError:
        notes.append("events unreadable: cannot derive source refs")
        return Evidence(expected, False, notes, forgotten)
    for mid in sorted(retained):
        vids = [v for (m, v) in admitted if m == mid]
        envs = []
        for vid in sorted(vids):
            try:
                env = store.stored_envelope(mid, vid)
            except StoreIntegrityError:
                env = None
            if env is not None:
                envs.append(env)
        if not envs:
            ok = False
            notes.append(f"memory {mid}: no admitted version file to derive source refs")
            continue
        head = envs[-1]
        for env in envs:
            ref = env.canonical_source_ref
            key = (mid, ref.source_system, ms.canonical_object_id(ref.object_id))
            expected.setdefault(
                key,
                {
                    "memory_id": mid,
                    "source_system": ref.source_system,
                    "object_id": ref.object_id,
                    "policy_revision": head.policy_revision,
                    "forgetting_policy": head.forgetting_policy,
                },
            )
    return Evidence(expected, ok, notes, forgotten)


def _read_classified(store: ms.MemoryStore, log: str) -> tuple[Path, list[Line]]:
    path = store.root / LOGS[log]
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b""
    return path, classify(data, LOGS[log])


def build_tombstone_plan(
    store: ms.MemoryStore, acks: set[str], event_survivors: list[dict] | None
) -> tuple[Plan, Evidence]:
    path, lines = _read_classified(store, "tombstones")
    corrupt = [ln for ln in lines if ln.cls in CORRUPT]
    survivors = [ln.record for ln in lines if ln.is_survivor]
    ev = derive_expected_tombstones(store, event_survivors, survivors)
    plan = Plan("tombstones", path, lines, corrupt, survivors, {}, [], evidence_ok=ev.complete, notes=ev.notes,
                expected=ev.expected)
    have = {_tomb_key(r) for r in survivors}
    plan.missing = {k: v for k, v in ev.expected.items() if k not in have}
    # A corrupt line's content is unknowable: voiding it ALWAYS needs its ack.
    plan.ack_needed = [ln for ln in corrupt if ln.digest not in acks]
    plan.acked = [ln for ln in corrupt if ln.digest in acks]
    return plan, ev


def build_events_plan(
    store: ms.MemoryStore, tomb_keys: set, evidence: Evidence, event_acks: set[str]
) -> Plan:
    forgotten = evidence.forgotten
    path, lines = _read_classified(store, "events")
    corrupt = [ln for ln in lines if ln.cls in CORRUPT]
    survivors = [ln.record for ln in lines if ln.is_survivor]
    plan = Plan("events", path, lines, corrupt, survivors, {}, [], evidence_ok=evidence.complete,
                notes=evidence.notes)
    if not corrupt:
        return plan
    try:
        fold = fold_events(survivors)
    except StoreIntegrityError as exc:
        raise _Refused(
            f"events.jsonl: the surviving lines do not fold ({exc}); voiding the corrupt lines would "
            "drop a needed event. Nothing written."
        ) from exc
    # The retrievable set under the PLANNED survivors (same rule as
    # MemoryStore.retrievable_envelopes). A voided line may have held a
    # forget/expire transition: no forgotten memory may come back.
    back: set[str] = set()
    for (mid, vid), fv in fold.items():
        if fv.state not in ms.RETRIEVABLE_STATES or mid not in forgotten:
            continue
        try:
            env = store.stored_envelope(mid, vid)
            if env is None or store.read_payload(mid, vid) is None:
                continue
        except StoreIntegrityError:
            continue
        src = env.canonical_source_ref
        if (mid, src.source_system, ms.canonical_object_id(src.object_id)) in tomb_keys:
            continue
        back.add(mid)
    if back:
        raise _Refused(
            f"events.jsonl: voiding the corrupt line(s) would make {len(back)} forgotten memory(ies) "
            f"retrievable again ({', '.join(sorted(back))}); a forget/expire transition may have been lost. "
            "Nothing written."
        )
    # The ack overrides an evidence GAP only, never the check above. With any
    # unreadable/invalid/missing receipt the forgotten set may be incomplete, so
    # each corrupt events line needs its own --ack-event-loss.
    if not evidence.complete:
        plan.ack_needed = [ln for ln in corrupt if ln.digest not in event_acks]
        plan.acked = [ln for ln in corrupt if ln.digest in event_acks]
    return plan


def _report(plan: Plan, apply: bool) -> None:
    name = LOGS[plan.log]
    counts: dict[str, int] = {}
    for ln in plan.lines:
        counts[ln.cls] = counts.get(ln.cls, 0) + 1
    print(f"{name}: " + ", ".join(f"{c}={counts[c]}" for c in sorted(counts)) if counts else f"{name}: empty")
    for ln in plan.lines:
        if ln.cls in CORRUPT or ln.cls == TORN_TAIL:
            print(f"  {name} line {ln.n} offset {ln.offset} length {ln.length} digest {ln.digest} class {ln.cls}")
    if plan.log == "events" and plan.corrupt:
        print(f"  {name}: receipt evidence complete: {'yes' if plan.evidence_ok else 'NO'}")
        for note in plan.notes:
            print(f"  {name}: evidence: {note}")
        for ln in plan.acked:
            print(f"  {name} line {ln.n} digest {ln.digest}: event loss ACKNOWLEDGED (--ack-event-loss)")
        for ln in plan.ack_needed:
            print(f"  {name} line {ln.n} digest {ln.digest}: evidence incomplete; voiding needs --ack-event-loss")
    if plan.log == "tombstones" and (plan.corrupt or plan.expected or plan.notes):
        for mid in sorted({k[0] for k in plan.expected}, key=str):
            exp = sum(1 for k in plan.expected if k[0] == mid)
            gone = sum(1 for k in plan.missing if k[0] == mid)
            print(f"  {name}: memory {mid}: expected {exp} tombstone(s), {gone} to re-append")
        print(f"  {name}: evidence complete for every receipt: {'yes' if plan.evidence_ok else 'NO'}")
        for note in plan.notes:
            print(f"  {name}: evidence: {note}")
        for ln in plan.acked:
            print(f"  {name} line {ln.n} digest {ln.digest}: tombstone loss ACKNOWLEDGED (--ack-tombstone-loss)")
        for ln in plan.ack_needed:
            print(f"  {name} line {ln.n} digest {ln.digest}: content unknowable; voiding needs --ack-tombstone-loss")


def _write_quarantine(root: Path, log: str, op: str, corrupt: list[Line]) -> str:
    qdir = root / "quarantine"
    qdir.mkdir(mode=0o700, exist_ok=True)
    rel = f"quarantine/{log}.{op}.jsonl"
    fd = os.open(root / rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            for ln in corrupt:
                rec = {
                    "line": ln.n,
                    "offset": ln.offset,
                    "length": ln.length,
                    "digest": ln.digest,
                    "class": ln.cls,
                    "bytes_b64": base64.b64encode(ln.raw).decode("ascii"),
                }
                fh.write((json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(root / rel)
        raise
    ms._fsync_dir(qdir)
    ms._fsync_dir(root)
    return rel


def _apply_plan(store: ms.MemoryStore, plan: Plan, op: str, now: str, acks: set[str]) -> list[str]:
    """Write the sidecar, append the void, verify (tombstone re-appends already
    happened, see ``_reappend``). Returns report lines. Raises _Refused on a
    verify failure."""
    out: list[str] = []
    if not plan.corrupt:
        return out
    rel = _write_quarantine(store.root, plan.log, op, plan.corrupt)
    out.append(f"{LOGS[plan.log]}: quarantined {len(plan.corrupt)} line(s) to {rel}")
    record = {
        "repair_void": {
            "operation_id": op,
            "at": now,
            "voids": [{"offset": ln.offset, "length": ln.length, "digest": ln.digest} for ln in plan.corrupt],
            "quarantine": rel,
        }
    }
    ms._append_jsonl(plan.path, record)
    out.append(f"{LOGS[plan.log]}: appended repair_void for {len(plan.corrupt)} line(s)")
    # VERIFY
    try:
        if plan.log == "events":
            got = store.events()
            if list(got) != plan.survivors:
                raise _Refused("events.jsonl: loaded records differ from the surviving lines")
            if store.folded() != fold_events(plan.survivors):
                raise _Refused("events.jsonl: fold differs from the independent fold of the surviving lines")
            store.rebuild_projections()
        else:
            _verify_tombstones(store, plan)
    except StoreIntegrityError as exc:
        raise _Refused(f"{LOGS[plan.log]}: verify failed ({exc})") from exc
    out.append(f"{LOGS[plan.log]}: verified")
    return out


def _verify_tombstones(store: ms.MemoryStore, plan: Plan) -> None:
    want = {_tomb_key(r) for r in plan.survivors} | set(plan.missing)
    if store._tombstone_keys() != want:
        raise _Refused("tombstones.jsonl: tombstone set differs from survivors + re-appended")
    for key in plan.expected:
        if not store.is_tombstoned(key[1], key[2], memory_id=key[0]):
            raise _Refused("tombstones.jsonl: an expected tombstone is not visible")


def _reappend(store: ms.MemoryStore, plan: Plan, op: str, now: str) -> list[str]:
    """Re-append every evidenced-but-missing tombstone (always safe: it only
    narrows retrieval). Runs on every --apply, before any void."""
    out: list[str] = []
    for key, fields in sorted(plan.missing.items(), key=lambda kv: str(kv[0])):
        store.add_tombstone(operation_id=op, now=now, **fields)
        out.append(f"tombstones.jsonl: re-appended tombstone for memory {key[0]} source {key[1]}/{key[2]}")
    if plan.missing and not plan.corrupt:
        try:
            _verify_tombstones(store, plan)
        except StoreIntegrityError as exc:
            raise _Refused(f"tombstones.jsonl: verify failed ({exc})") from exc
        out.append("tombstones.jsonl: verified")
    return out


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # usage error -> exit 3, no traceback
        print(f"usage error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def main(argv: list[str] | None = None) -> int:
    ap = _Parser(prog="memory_store_repair", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", required=True, help="memory store root")
    ap.add_argument("--log", choices=["events", "tombstones", "both"], default="both")
    ap.add_argument("--apply", action="store_true", help="repair (default: dry run)")
    ap.add_argument("--ack-tombstone-loss", action="append", default=[], metavar="DIGEST")
    ap.add_argument("--ack-event-loss", action="append", default=[], metavar="DIGEST")
    ap.add_argument("--operation-id", default=None)
    args = ap.parse_args(argv)
    root = Path(args.root)
    if not (root / "store_manifest.json").is_file():
        print("not a memory store (no store_manifest.json)", file=sys.stderr)
        return EXIT_USAGE
    now = _utc_now()
    op = args.operation_id or f"repair-{now.replace('-', '').replace(':', '')}-{secrets.token_hex(3)}"
    if not ms._ID_RE.match(op):
        print("usage error: --operation-id is not a safe id", file=sys.stderr)
        return EXIT_USAGE
    acks = set(args.ack_tombstone_loss)
    event_acks = set(args.ack_event_loss)
    logs = ["events", "tombstones"] if args.log == "both" else [args.log]
    try:
        store = ms.MemoryStore(root)
        lock = store.locked()
        lock.__enter__()
    except Exception:  # noqa: BLE001 - any failure here: exit 3, never a traceback or message content
        print("cannot take the store lock", file=sys.stderr)
        return EXIT_USAGE
    try:
        return _run(store, logs, args.apply, acks, op, now, event_acks)
    finally:
        lock.__exit__(None, None, None)


def _run(
    store: ms.MemoryStore, logs: list[str], apply: bool, acks: set[str], op: str, now: str,
    event_acks: set[str] = frozenset(),  # type: ignore[assignment]
) -> int:
    plans: list[Plan] = []
    refused: list[str] = []
    try:
        # events survivors by OUR classification (independent of a wedged reader)
        _p, ev_lines = _read_classified(store, "events")
        ev_survivors = [ln.record for ln in ev_lines if ln.is_survivor]
        tplan, evidence = build_tombstone_plan(store, acks, ev_survivors)
        tomb_keys = {_tomb_key(r) for r in tplan.survivors}
        if "tombstones" in logs:
            tomb_keys |= set(tplan.missing)  # re-appended on apply
        eplan = build_events_plan(store, tomb_keys, evidence, event_acks)
        plans = [p for p in (eplan, tplan) if p.log in logs]
    except _Refused as exc:
        print(f"REFUSED: {exc}")
        return EXIT_REFUSED
    except StoreIntegrityError as exc:
        print(f"REFUSED: {exc}")
        return EXIT_REFUSED
    work = False
    for plan in plans:
        _report(plan, apply)
        if plan.corrupt or plan.missing:
            work = True
        for ln in plan.ack_needed:
            refused.append(f"{LOGS[plan.log]} line {ln.n} digest {ln.digest}")
        qpath = store.root / "quarantine" / f"{plan.log}.{op}.jsonl"
        if plan.corrupt and qpath.exists():
            refused.append(f"{LOGS[plan.log]}: operation id {op} already used (sidecar exists)")
    try:
        if apply and refused:
            # re-appending an evidenced tombstone only narrows retrieval: always done
            for plan in plans:
                if plan.log == "tombstones":
                    for line in _reappend(store, plan, op, now):
                        print(line)
    except (_Refused, OSError) as exc:
        print(f"REFUSED: {type(exc).__name__ if isinstance(exc, OSError) else exc}")
        return EXIT_REFUSED
    if refused:
        print("REFUSED (nothing written" + (" except tombstone re-appends" if apply else "") + "): " + "; ".join(refused))
        return EXIT_REFUSED
    if not work:
        print("clean: no corrupt lines")
        return EXIT_OK
    if not apply:
        print(f"dry run: {sum(len(p.corrupt) for p in plans)} corrupt line(s), "
              f"{sum(len(p.missing) for p in plans)} tombstone(s) to re-append; re-run with --apply")
        return EXIT_WORK
    try:
        for plan in plans:
            if plan.log == "tombstones":
                for line in _reappend(store, plan, op, now):
                    print(line)
            for line in _apply_plan(store, plan, op, now, acks):
                print(line)
    except _Refused as exc:
        print(f"REFUSED: {exc}")
        return EXIT_REFUSED
    except OSError as exc:
        print(f"REFUSED: I/O failure ({type(exc).__name__}); writes stopped")
        return EXIT_REFUSED
    print(f"applied and verified (operation {op})")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
