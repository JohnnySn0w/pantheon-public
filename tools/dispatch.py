#!/usr/bin/env python3
"""Pantheon Dispatch contract + dispatch-ledger — schemaful cross-agent delegation audit.

Realizes the `Dispatch` Object Type from `notes/dei-foundational-wip/objects.md` (adopted
descriptively via T-0668; build ticket T-1086, the Dei keystone). Design + reviewer pass:
`notes/2026-06-12-dispatch-ledger-design.md`.

WHY: today a cross-agent dispatch is a free-form prompt that dies with the transcript — no
persistent artifact, so the audit chain cannot recover *what work was asked* of a dispatchee.
The dispatch discipline lives entirely in prose (D4 brief-quality, D6 locked-answers, D7
autonomy-partition + 3-REBOUND, D9 response-contract). This tool makes that discipline
SCHEMA-ENFORCED rather than prose-remembered (P1): the validator IS the enforcement. The tool
is the contract; calling it when you delegate is the discipline, exactly as ticket.py is for
tickets. (Auto-capture from the Agent tool — no hook surface today — is a deferred
follow-up; so is backfill and any graph/DB.)

STORAGE: `dispatches/ledger.jsonl` — flat append-only JSONL, one record per line. A dispatch is
recorded `kind:"open"` at dispatch-time and evolves via a `kind:"close"` row sharing the SAME
logical `id` (NOT an in-place edit — P4 provenance / Auditable T-0673). Read resolution is a
single-pass group-by-id: a dispatch is open iff it has no close row. `supersedes` is reserved for
genuine corrections (the Ratification semantic), not the close mechanism.

Schema (objects.md §Dispatch). Required at record-time: dispatcher, dispatchee, ticket, brief,
autonomy_partition, response_contract. Auto-stamped: id (`D-NNNN`), ts, kind. Optional: serves
(arc-intent, D7/ORG-034 — warn if absent), locked_answers, isolation, result, rebound_count,
supersedes.

CLI:
    dispatch.py record  [--from-json <file|->]   # validate + stamp + append + commit
    dispatch.py validate [--from-json <file|->]  # validate only, no write (exit 0 / 65)
    dispatch.py close <id> --result <r> [--rebound-count N]
    dispatch.py show   <id>
    dispatch.py ticket T-NNNN                     # all dispatches for a ticket ("what was asked")
    dispatch.py list   [--dispatcher X|--dispatchee Y|--result Z]
    dispatch.py --json <subcommand> ...
Mutating subcommands (record/close) auto-commit+push by default (agent-action audit class, like
ticket.py); `--no-commit` for tests, `--no-push` for offline. Query subcommands are pure reads.
Validation failure exits 65 (EX_DATAERR) with a precise per-field reason on stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_utils import commit_scoped  # noqa: E402
from tools.ticket import _open_world_writable  # noqa: E402 (cross-uid-correct lock primitive)

try:
    from tools.agent_role_map import AGENT_ROLE_MAP  # noqa: E402
    _KNOWN_AGENTS = set(AGENT_ROLE_MAP.keys())
except Exception:  # pragma: no cover - role map optional
    # SYNC MIRROR of AGENT_ROLE_MAP's keys, reached only when the role map cannot
    # be imported at all — so it is the one list in this file that CANNOT derive
    # from the canonical map, by construction. It therefore gets the repo's other
    # answer to a hand-carried list: a check that fails. Held the pre-ORG-042/043
    # five until T-2256 (orchestrator/specialist/recovery/codex/+ the since-retired the-ancestor), a silent 5-of-8 that
    # would have warned "unknown dispatchee" for every archivist/mason/artisan
    # dispatch in a degraded import. test_dispatch enforces the sync.
    _KNOWN_AGENTS = {
        "orchestrator", "specialist", "recovery", "codex",
        "archivist", "mason", "artisan",
        "watchman",  # T-4884 B6
    }

# Standard non-enumerable role names that are valid dispatch endpoints. Ephemeral
# workers are spawned per-arc and are NOT exhaustively listable; this set just
# suppresses the unknown-slug warning for the conventional ones.
_KNOWN_ROLES = _KNOWN_AGENTS | {
    "orchestrator", "orchestrator", "project_orchestrator",
    "implementer", "reviewer", "operator", "claude", "claude-fallback", "claude-research",
}

REPO_ROOT = Path(__file__).resolve().parent.parent
DISPATCH_DIR = REPO_ROOT / "dispatches"
DISPATCH_LEDGER = DISPATCH_DIR / "ledger.jsonl"
DISPATCH_LEDGER_REL = "dispatches/ledger.jsonl"

_DEFAULT_ALLOC_LOCKFILE = "/tmp/pantheon_dispatch_alloc.lock"

SLUG_RE = re.compile(r"^[a-z0-9_-]+$")
TICKET_ID_RE = re.compile(r"^T-\d{3,}$")
DISPATCH_ID_RE = re.compile(r"^D-\d{4,}$")

RESULT_VALUES = {"ready", "rebound", "request_spawn", "silent"}
ISOLATION_VALUES = {"worktree", "foreground", "none"}
REQUIRED_FIELDS = ("dispatcher", "dispatchee", "ticket", "brief", "autonomy_partition", "response_contract")
RESPONSE_CONTRACT_KEYS = ("answer_form", "format", "timing")
REBOUND_BUDGET = 3  # D7 3-REBOUND budget


class DispatchError(Exception):
    """Validation / lifecycle error. Message is the precise per-field reason."""


# ---------------------------------------------------------------------------
# Time (module-level so tests can monkeypatch one symbol).
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Allocate→append lock (cross-uid; dispatch-specific lockfile).
# ---------------------------------------------------------------------------


def _alloc_lockfile() -> str:
    return os.environ.get("PANTHEON_DISPATCH_ALLOC_LOCKFILE") or _DEFAULT_ALLOC_LOCKFILE


@contextlib.contextmanager
def _allocation_lock():
    """Blocking LOCK_EX flock for the mint-id→append phase.

    A `brief` can exceed PIPE_BUF (4096B) so a bare append is not atomic against a
    concurrent writer, and the `D-NNNN` seq is scan-max+1 (a read-then-write race).
    Hold this across read-max→append; release before the network push (push is not
    serialized). Single-machine assumption (fcntl.flock; NFS unsupported), mirroring
    ticket.py._allocation_lock.
    """
    lock_fd = _open_world_writable(_alloc_lockfile())
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


# ---------------------------------------------------------------------------
# Ledger I/O.
# ---------------------------------------------------------------------------


def _ledger_path() -> Path:
    """Resolve the dispatch-ledger path at call-time. `PANTHEON_DISPATCH_LEDGER`
    overrides the default (tests point it at a tmp file; prod uses the repo path)."""
    override = os.environ.get("PANTHEON_DISPATCH_LEDGER")
    return Path(override) if override else DISPATCH_LEDGER


def _read_records(ledger: Path | None = None) -> list[dict]:
    """Read all ledger rows. Skips blank lines; raises on a malformed JSON line
    (a corrupt audit ledger must surface loudly, not silently drop records)."""
    ledger = ledger or _ledger_path()
    if not ledger.exists():
        return []
    out: list[dict] = []
    for lineno, raw in enumerate(ledger.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise DispatchError(f"malformed dispatch-ledger line {ledger}:{lineno}: {exc}")
    return out


def _next_dispatch_id(records: list[dict]) -> str:
    """Monotonic `D-NNNN` = max existing numeric id + 1 (scan under the alloc lock)."""
    maxn = 0
    for rec in records:
        rid = rec.get("id", "")
        if DISPATCH_ID_RE.match(rid):
            maxn = max(maxn, int(rid[2:]))
    return f"D-{maxn + 1:04d}"


def _append_record(record: dict, ledger: Path | None = None) -> None:
    ledger = ledger or _ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Validation — the structural enforcement (P1). Returns (record, warnings).
# ---------------------------------------------------------------------------


def _validate_slug(value, field: str) -> list[str]:
    """Two-tier: malformed = error; well-formed-but-unknown = warning."""
    if not isinstance(value, str) or not SLUG_RE.match(value):
        raise DispatchError(f"{field}: must be a slug matching [a-z0-9_-]+, got {value!r}")
    if value not in _KNOWN_ROLES:
        return [f"{field}: {value!r} is not a known agent/role (ephemeral worker?) — recorded anyway"]
    return []


def validate(payload: dict) -> tuple[dict, list[str]]:
    """Validate a dispatch payload against the schema. Returns (normalized_record, warnings).

    Raises DispatchError on the first hard violation with a precise reason. Only the
    record-time fields are validated here (close-time fields handled by `close`).
    """
    if not isinstance(payload, dict):
        raise DispatchError("payload must be a JSON object")
    warnings: list[str] = []

    for field in REQUIRED_FIELDS:
        if field not in payload or payload[field] in (None, "", [], {}):
            raise DispatchError(f"missing required field: {field}")

    warnings += _validate_slug(payload["dispatcher"], "dispatcher")
    warnings += _validate_slug(payload["dispatchee"], "dispatchee")

    if not TICKET_ID_RE.match(str(payload["ticket"])):
        raise DispatchError(f"ticket: must match T-NNNN, got {payload['ticket']!r}")

    if not isinstance(payload["brief"], str) or not payload["brief"].strip():
        raise DispatchError("brief: must be non-empty text")

    ap = payload["autonomy_partition"]
    if not isinstance(ap, dict) or "autonomous" not in ap or "bubble_up" not in ap:
        raise DispatchError("autonomy_partition: must be an object with keys {autonomous, bubble_up}")
    if not isinstance(ap["autonomous"], list) or not isinstance(ap["bubble_up"], list):
        raise DispatchError("autonomy_partition.autonomous and .bubble_up must both be lists")

    rc = payload["response_contract"]
    if not isinstance(rc, dict):
        raise DispatchError("response_contract: must be an object with keys {answer_form, format, timing}")
    for key in RESPONSE_CONTRACT_KEYS:
        if key not in rc or not str(rc.get(key, "")).strip():
            raise DispatchError(f"response_contract.{key}: required and must be non-empty")

    # Optional fields.
    if "serves" not in payload or not str(payload.get("serves") or "").strip():
        warnings.append("serves: arc-intent line absent (D7/ORG-034 mandates it) — recorded anyway")
    if "locked_answers" in payload and not isinstance(payload["locked_answers"], list):
        raise DispatchError("locked_answers: must be a list when present")
    if "isolation" in payload and payload["isolation"] not in ISOLATION_VALUES:
        raise DispatchError(f"isolation: must be one of {sorted(ISOLATION_VALUES)}")
    if "result" in payload and payload["result"] not in RESULT_VALUES:
        raise DispatchError(f"result: must be one of {sorted(RESULT_VALUES)}")
    if "rebound_count" in payload:
        if not isinstance(payload["rebound_count"], int) or isinstance(payload["rebound_count"], bool) \
                or payload["rebound_count"] < 0:
            raise DispatchError("rebound_count: must be a non-negative integer")

    # Normalized record: carry known schema fields through (drop unknown keys).
    record = {k: payload[k] for k in (
        "dispatcher", "dispatchee", "ticket", "brief", "serves", "locked_answers",
        "autonomy_partition", "response_contract", "isolation", "result", "rebound_count",
        "supersedes",
    ) if k in payload}
    return record, warnings


# ---------------------------------------------------------------------------
# Dispatch resolution — group rows by logical id (open iff no close row).
# ---------------------------------------------------------------------------


def resolve_dispatches(records: list[dict]) -> dict[str, dict]:
    """Collapse the append-only rows into the latest logical state per id.

    Group by logical id: the open row is the base; a close row overlays the
    terminal fields (result, rebound_count) and marks status='closed' (preserving
    the open row's `ts` as the dispatch time and adding `closed_ts`). Order-robust
    (last open / last close wins per id). A dispatch is open iff it has no close row.
    """
    opens: dict[str, dict] = {}
    closes: dict[str, dict] = {}
    for rec in records:
        rid = rec.get("id")
        if not rid:
            continue
        if rec.get("kind") == "close":
            closes[rid] = rec
        else:
            opens[rid] = rec

    by_id: dict[str, dict] = {}
    for rid in set(opens) | set(closes):
        merged = {k: v for k, v in opens.get(rid, {}).items() if k != "kind"}
        merged.setdefault("id", rid)
        if rid in closes:
            c = closes[rid]
            for k in ("result", "rebound_count"):
                if k in c:
                    merged[k] = c[k]
            merged["closed_ts"] = c.get("ts")
            merged["status"] = "closed"
        else:
            merged["status"] = "open"
        by_id[rid] = merged
    return by_id


# ---------------------------------------------------------------------------
# Commit (dispatch-local; the seam T-1088 later extracts as commit_scoped()).
# ---------------------------------------------------------------------------


def _commit_ledger(action: str, did: str, *, push: bool) -> tuple[bool, str]:
    """HERMES-guarded, scope-narrowed commit of the dispatch-ledger only.

    Delegates to the shared `commit_scoped` primitive (T-1088 convergence): HERMES
    file+message guard → sync → stage ONLY dispatches/ledger.jsonl → leak guard →
    commit → push_with_retry. The commit message is terse/templated so free brief
    text never reaches the HERMES message-scan.
    """
    msg = f"dispatch({action}): {did}\n\nAuto-committed by tools/dispatch.py.\nScope: {DISPATCH_LEDGER_REL} only.\n"
    return commit_scoped([DISPATCH_LEDGER_REL], msg, cwd=REPO_ROOT, push=push)


# ---------------------------------------------------------------------------
# Subcommand bodies.
# ---------------------------------------------------------------------------


def cmd_record(payload: dict, *, commit: bool, push: bool) -> tuple[dict, list[str]]:
    """Validate + stamp + append an `open` dispatch row. Returns (record, warnings)."""
    record, warnings = validate(payload)
    with _allocation_lock():
        records = _read_records()
        did = _next_dispatch_id(records)
        record = {"id": did, "ts": _now_iso(), "kind": "open", **record}
        _append_record(record)
    if commit:
        ok, detail = _commit_ledger("record", did, push=push)
        if not ok:
            raise DispatchError(f"recorded locally but commit failed: {detail}")
    return record, warnings


def cmd_close(did: str, *, result: str, rebound_count: int | None, commit: bool, push: bool) -> dict:
    """Append a `close` row (same logical id) with the terminal result."""
    if not DISPATCH_ID_RE.match(did):
        raise DispatchError(f"id: must match D-NNNN, got {did!r}")
    if result not in RESULT_VALUES:
        raise DispatchError(f"result: must be one of {sorted(RESULT_VALUES)}")
    if rebound_count is not None and (not isinstance(rebound_count, int) or rebound_count < 0):
        raise DispatchError("rebound_count: must be a non-negative integer")

    with _allocation_lock():
        records = _read_records()
        resolved = resolve_dispatches(records)
        if did not in resolved:
            raise DispatchError(f"unknown dispatch id: {did}")
        if resolved[did].get("status") == "closed":
            raise DispatchError(f"{did} is already closed")
        row = {"id": did, "ts": _now_iso(), "kind": "close", "result": result}
        if rebound_count is not None:
            row["rebound_count"] = rebound_count
        _append_record(row)
    if commit:
        ok, detail = _commit_ledger("close", did, push=push)
        if not ok:
            raise DispatchError(f"closed locally but commit failed: {detail}")
    return row


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _load_payload(from_json: str | None) -> dict:
    if from_json in (None, "-"):
        text = sys.stdin.read()
    else:
        text = Path(from_json).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DispatchError(f"input is not valid JSON: {exc}")


def _emit(obj, json_mode: bool, render) -> None:
    if json_mode:
        print(json.dumps(obj, indent=2, sort_keys=True))
    else:
        render(obj)


def _warn(warnings: list[str]) -> None:
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dispatch.py", description=__doc__.split("\n")[0])
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("record", "validate"):
        sp = sub.add_parser(name)
        sp.add_argument("--from-json", default=None, help="JSON file path, or - / omitted for stdin")
        if name == "record":
            sp.add_argument("--no-commit", action="store_true")
            sp.add_argument("--no-push", action="store_true")

    sp_close = sub.add_parser("close")
    sp_close.add_argument("id")
    sp_close.add_argument("--result", required=True, choices=sorted(RESULT_VALUES))
    sp_close.add_argument("--rebound-count", type=int, default=None)
    sp_close.add_argument("--no-commit", action="store_true")
    sp_close.add_argument("--no-push", action="store_true")

    sp_show = sub.add_parser("show")
    sp_show.add_argument("id")
    sp_ticket = sub.add_parser("ticket")
    sp_ticket.add_argument("ticket")
    sp_list = sub.add_parser("list")
    sp_list.add_argument("--dispatcher", default=None)
    sp_list.add_argument("--dispatchee", default=None)
    sp_list.add_argument("--result", default=None, choices=sorted(RESULT_VALUES))

    args = p.parse_args(argv)

    try:
        if args.cmd == "validate":
            payload = _load_payload(args.from_json)
            _record, warnings = validate(payload)
            _warn(warnings)
            print("valid" if not args.json else json.dumps({"valid": True, "warnings": warnings}))
            return 0

        if args.cmd == "record":
            payload = _load_payload(args.from_json)
            record, warnings = cmd_record(payload, commit=not args.no_commit, push=not args.no_push)
            _warn(warnings)
            _emit(record, args.json, lambda r: print(f"recorded {r['id']} ({r['dispatcher']}→{r['dispatchee']}, {r['ticket']})"))
            return 0

        if args.cmd == "close":
            row = cmd_close(args.id, result=args.result, rebound_count=args.rebound_count,
                            commit=not args.no_commit, push=not args.no_push)
            _emit(row, args.json, lambda r: print(f"closed {r['id']} → {r['result']}"))
            return 0

        if args.cmd == "show":
            resolved = resolve_dispatches(_read_records())
            if args.id not in resolved:
                print(f"unknown dispatch id: {args.id}", file=sys.stderr)
                return 1
            d = resolved[args.id]
            _emit(d, args.json, lambda o: print(json.dumps(o, indent=2, sort_keys=True)))
            return 0

        if args.cmd == "ticket":
            resolved = resolve_dispatches(_read_records())
            hits = sorted((d for d in resolved.values() if d.get("ticket") == args.ticket),
                          key=lambda d: d.get("id", ""))
            _emit(hits, args.json, lambda hs: _render_list(args.ticket, hs))
            return 0

        if args.cmd == "list":
            resolved = resolve_dispatches(_read_records())
            hits = [d for d in resolved.values()
                    if (args.dispatcher is None or d.get("dispatcher") == args.dispatcher)
                    and (args.dispatchee is None or d.get("dispatchee") == args.dispatchee)
                    and (args.result is None or d.get("result") == args.result)]
            hits.sort(key=lambda d: d.get("id", ""))
            _emit(hits, args.json, lambda hs: _render_list(None, hs))
            return 0
    except DispatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 65  # EX_DATAERR

    return 0


def _render_list(scope: str | None, hits: list[dict]) -> None:
    header = f"# Dispatches for {scope}" if scope else "# Dispatches"
    print(header)
    if not hits:
        print("  (none)")
        return
    for d in hits:
        status = d.get("status", "open")
        res = f" → {d['result']}" if d.get("result") else ""
        print(f"  {d.get('id')}  {d.get('dispatcher')}→{d.get('dispatchee')}  {d.get('ticket')}  [{status}]{res}")


if __name__ == "__main__":
    sys.exit(main())
