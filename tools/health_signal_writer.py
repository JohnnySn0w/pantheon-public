#!/usr/bin/env python3
"""tools/health_signal_writer.py — ORG-097 external-authority health signal-writer.

Implements RFC-003 (architecture/rfc/RFC-003-health-signal-writer-broker.md), the
non-root AF_UNIX broker that writes fleet-health liveness signals with server-derived
authority. See ORG-097 (Adopted) for the decision-level contract (D-1..D-8).

BUILD STATUS (T-3652): incremental. This slice is the **pure, hermetic
request-validation core** — the security-critical parsing/authorization pipeline
that closes the E11.C20 falsification gap (RFC R2/R3/R5). It is **unwired**: nothing
imports it yet, no socket is bound, no service account or ACL is provisioned. Per
RFC M2, the socket daemon (SO_PEERCRED, R8), the canary probe worker (R6), the
root-owned allowlist file reader (R4 via trusted_files), and the ORG-050 installer
operations (R9) land in later slices; their *apply* is gated on the T-2920
supervised-root backend + the operator's ratify of RFC-003 (the apply grants access =
goal-level). Everything here is reversible and side-effect-free.

Design invariants enforced here (all RFC-cited):
  - D-1 / R3: the write leaf is derived SERVER-SIDE from (uid -> account, signal_kind);
    the request carries NO path component.
  - D-2 / R2: fixed-schema, size-bounded, length-prefixed JSON; unknown/duplicate keys
    rejected, not ignored.
  - H1 / R4: `heartbeat` and `canary` are RESERVED (never caller-requestable); the
    reserved check is casefold-compared (LOW-1) at both config-load and runtime.
  - H3 / R2: `IDENTIFIER_RE` permits internal `/`, so an explicit `"/" not in kind`
    guard is applied in addition to the identifier check.
  - R5: every refusal is a typed REFUSE_* and NOTHING is written on refusal.
"""
from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

# Bootstrap so `from tools.credential_broker import ...` works whether run as a
# script or imported as a package module (mirrors policy_outflow_sync.py).
if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.credential_broker import IDENTIFIER_RE, MAX_IDENTIFIER_LENGTH  # noqa: E402
from tools.host_control.trusted_files import (  # noqa: E402
    RegistryError,
    RootOwnedFileLoader,
)

# ---- Wire framing (RFC R2) -------------------------------------------------

PROTOCOL_VERSION = 1
MAX_FRAME = 4096  # bytes; the JSON body. A length prefix > MAX_FRAME -> REFUSE_OVERSIZED.
MAX_PAYLOAD = 1024  # bytes; the serialized `payload` object.
_LEN_PREFIX = struct.Struct(">I")  # uint32 big-endian (L1: unsigned, never signed).
LEN_PREFIX_SIZE = _LEN_PREFIX.size

# payload shape bounds (RFC R2, M9): flat object of bounded scalars.
MAX_PAYLOAD_KEYS = 16
MAX_PAYLOAD_STR_BYTES = 256
_SCALAR_TYPES = (str, int, float, bool, type(None))

# ---- Reserved signal kinds (RFC R4 / H1 / LOW-1) ---------------------------

# `heartbeat` is account-owned/off-broker (R7); `canary` is broker-internal (R6).
# Never caller-requestable. Compared casefold so `Canary`/`CANARY` cannot slip past.
RESERVED_SIGNAL_KINDS = frozenset({"heartbeat", "canary"})

# ---- Refusal classes (RFC R5) ----------------------------------------------

REFUSE_UNLISTED = "REFUSE_UNLISTED"
REFUSE_RESERVED = "REFUSE_RESERVED"
REFUSE_BAD_IDENTIFIER = "REFUSE_BAD_IDENTIFIER"
REFUSE_MALFORMED = "REFUSE_MALFORMED"
REFUSE_OVERSIZED = "REFUSE_OVERSIZED"
REFUSE_NAMESPACE = "REFUSE_NAMESPACE"

# Server-derived signal tree root (R1). The <account>/<signal_kind> leaf lives under
# <SIGNAL_TREE_ROOT>/<account>/. Tests inject a tmp root; production uses this.
SIGNAL_TREE_ROOT = Path("/var/lib/pantheon/health-signals")


class SignalWriterConfigError(ValueError):
    """The allowlist config is structurally invalid — fail closed (refuse to start)."""


def is_reserved_kind(kind: str) -> bool:
    """True if `kind` is a reserved signal kind, compared casefold (LOW-1)."""
    return kind.casefold() in {r.casefold() for r in RESERVED_SIGNAL_KINDS}


# ---- Allowlist model (RFC R4) ----------------------------------------------


@dataclass(frozen=True)
class Writer:
    """One allowlist entry: a uid authorized to write a bounded set of signal kinds."""

    uid: int
    account: str
    persona: str
    signal_kinds: frozenset[str]
    store_path: str  # the canary probe target (R6); verified by real probe at start.


class Allowlist:
    """The parsed, validated writer allowlist (R4).

    Construction fail-closed validates the whole set (H1: no reserved kind in any
    `signal_kinds`; unique int uids; IDENTIFIER_RE-clean account/persona/kinds). The
    root-owned FILE read (trusted_files, R4) is a later slice; this takes the already
    parsed mapping so the model + policy logic are hermetically testable.
    """

    def __init__(self, writers: Sequence[Writer]) -> None:
        self._by_uid: dict[int, Writer] = {}
        for w in writers:
            if w.uid in self._by_uid:
                raise SignalWriterConfigError(f"duplicate uid in allowlist: {w.uid}")
            self._by_uid[w.uid] = w

    @classmethod
    def from_mapping(cls, doc: Mapping[str, Any]) -> "Allowlist":
        if not isinstance(doc, Mapping):
            raise SignalWriterConfigError("allowlist must be a JSON object")
        if doc.get("version") != 1:
            raise SignalWriterConfigError("allowlist version must be 1")
        raw_writers = doc.get("writers")
        if not isinstance(raw_writers, list):
            raise SignalWriterConfigError("allowlist.writers must be a list")
        writers: list[Writer] = []
        for i, entry in enumerate(raw_writers):
            writers.append(cls._writer_from_entry(i, entry))
        return cls(writers)

    @staticmethod
    def _writer_from_entry(i: int, entry: Any) -> Writer:
        if not isinstance(entry, Mapping):
            raise SignalWriterConfigError(f"writers[{i}] must be an object")
        uid = entry.get("uid")
        # bool is an int subclass — a JSON `true` must not pass as uid 1.
        if not isinstance(uid, int) or isinstance(uid, bool):
            raise SignalWriterConfigError(f"writers[{i}].uid must be an integer")
        account = entry.get("account")
        persona = entry.get("persona")
        for label, val in (("account", account), ("persona", persona)):
            if not _is_clean_identifier(val):
                raise SignalWriterConfigError(
                    f"writers[{i}].{label} must be a bounded identifier with no '/'"
                )
        store_path = entry.get("store_path")
        if not isinstance(store_path, str) or not store_path:
            raise SignalWriterConfigError(f"writers[{i}].store_path must be a non-empty string")
        kinds_raw = entry.get("signal_kinds")
        if not isinstance(kinds_raw, list) or not kinds_raw:
            raise SignalWriterConfigError(f"writers[{i}].signal_kinds must be a non-empty list")
        kinds: set[str] = set()
        for k in kinds_raw:
            if not _is_clean_identifier(k):
                raise SignalWriterConfigError(
                    f"writers[{i}].signal_kinds entry must be a bounded identifier with no '/'"
                )
            # H1: a config that lists a reserved kind fails closed at load, in
            # addition to the runtime REFUSE_RESERVED (belt-and-braces).
            if is_reserved_kind(k):
                raise SignalWriterConfigError(
                    f"writers[{i}].signal_kinds must not contain the reserved kind {k!r}"
                )
            kinds.add(k)
        return Writer(
            uid=uid,
            account=account,
            persona=persona,
            signal_kinds=frozenset(kinds),
            store_path=store_path,
        )

    def account_for_uid(self, uid: int) -> Writer | None:
        return self._by_uid.get(uid)

    def __len__(self) -> int:
        return len(self._by_uid)


def _is_clean_identifier(value: Any) -> bool:
    """A bounded IDENTIFIER_RE token with NO `/` (H3). Used for config validation."""
    return (
        isinstance(value, str)
        and "/" not in value
        and len(value) <= MAX_IDENTIFIER_LENGTH
        and IDENTIFIER_RE.fullmatch(value) is not None
    )


# ---- Wire framing helpers (RFC R2) -----------------------------------------


def frame(body: bytes) -> bytes:
    """uint32-be length prefix || body. Raises if body exceeds MAX_FRAME (caller bug)."""
    if len(body) > MAX_FRAME:
        raise ValueError("body exceeds MAX_FRAME")
    return _LEN_PREFIX.pack(len(body)) + body


def parse_length_prefix(prefix: bytes) -> int | None:
    """Decode the 4-byte length prefix. None if the prefix is the wrong size."""
    if len(prefix) != LEN_PREFIX_SIZE:
        return None
    return _LEN_PREFIX.unpack(prefix)[0]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """object_pairs_hook: reject a duplicate key at ANY object depth (M9).

    Prevents last-wins key smuggling (`{"v":1,"v":2}`). Fires at every nesting level
    because json invokes the hook per object.
    """
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key: {k!r}")
        out[k] = v
    return out


def _reject_nonfinite(token: str) -> float:
    """parse_constant hook: reject JSON's non-finite extensions (NaN/Infinity/
    -Infinity). Python's json accepts these by default, but they are not valid JSON
    and would round-trip into a stored signal record as an unparseable constant for a
    strict downstream consumer. Rejecting keeps the wire strictly well-formed (R2)."""
    raise ValueError(f"non-finite constant not allowed: {token}")


# ---- Request validation (RFC R2/R5) ----------------------------------------


@dataclass(frozen=True)
class ValidatedRequest:
    signal_kind: str
    payload: Mapping[str, Any]


def parse_and_validate_body(body: bytes) -> tuple[ValidatedRequest | None, str | None]:
    """Parse+validate a request body's schema/size/shape. Returns (request, None) or
    (None, REFUSE_*). Does NOT authorize (that needs the uid — see `authorize`).

    Enforces (R2): length <= MAX_FRAME, strict closed schema {v, signal_kind, payload}
    with no extra/missing keys, v == PROTOCOL_VERSION, no duplicate keys at any depth,
    and a flat bounded `payload` (scalars only, <= MAX_PAYLOAD_KEYS keys, each str
    <= MAX_PAYLOAD_STR_BYTES, serialized <= MAX_PAYLOAD). `signal_kind` identifier
    validity is checked here; reserved/allowlist checks happen in `authorize`.
    """
    if len(body) > MAX_FRAME:
        return None, REFUSE_OVERSIZED
    try:
        doc = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, REFUSE_MALFORMED
    if not isinstance(doc, dict):
        return None, REFUSE_MALFORMED
    # Strict closed schema — exactly these keys, no more, no fewer.
    if set(doc.keys()) != {"v", "signal_kind", "payload"}:
        return None, REFUSE_MALFORMED
    if doc["v"] != PROTOCOL_VERSION:
        return None, REFUSE_MALFORMED
    signal_kind = doc["signal_kind"]
    if not isinstance(signal_kind, str):
        return None, REFUSE_MALFORMED
    payload = doc["payload"]
    refusal = _validate_payload(payload)
    if refusal is not None:
        return None, refusal
    # signal_kind identifier validity (H3: explicit '/' guard + IDENTIFIER_RE).
    if not _is_clean_identifier(signal_kind):
        return None, REFUSE_BAD_IDENTIFIER
    return ValidatedRequest(signal_kind=signal_kind, payload=payload), None


def _validate_payload(payload: Any) -> str | None:
    """Flat, bounded scalar object (R2/M9). Returns a REFUSE_* or None."""
    if not isinstance(payload, dict):
        return REFUSE_MALFORMED
    if len(payload) > MAX_PAYLOAD_KEYS:
        return REFUSE_OVERSIZED
    for k, v in payload.items():
        if not isinstance(k, str):
            return REFUSE_MALFORMED
        if not isinstance(v, _SCALAR_TYPES) or isinstance(v, (dict, list)):
            return REFUSE_MALFORMED  # nested object/array rejected
        if isinstance(v, str) and len(v.encode("utf-8")) > MAX_PAYLOAD_STR_BYTES:
            return REFUSE_OVERSIZED
    try:
        serialized = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return REFUSE_MALFORMED
    if len(serialized) > MAX_PAYLOAD:
        return REFUSE_OVERSIZED
    return None


# ---- Authorization + server-derived leaf (RFC R3) --------------------------


def _default_leaf_builder(tree_root: Path, account: str, signal_kind: str) -> Path:
    """Derive the write leaf entirely server-side (D-1). No caller path component."""
    return tree_root / account / signal_kind


def authorize(
    *,
    uid: int,
    body: bytes,
    allowlist: Allowlist,
    tree_root: Path = SIGNAL_TREE_ROOT,
    leaf_builder=_default_leaf_builder,
) -> tuple[Path | None, str | None]:
    """Full request pipeline (R3 order). Returns (leaf, None) on success or
    (None, REFUSE_*) on refusal. NOTHING is written here — the caller writes the leaf
    only on a (leaf, None) result.

    Order (R3): uid -> account (unlisted -> REFUSE_UNLISTED) -> reserved-kind check
    (-> REFUSE_RESERVED) -> identifier/`/`-guard (-> REFUSE_BAD_IDENTIFIER) ->
    membership (-> REFUSE_UNLISTED) -> leaf + realpath namespace guard
    (-> REFUSE_NAMESPACE). The kernel-attested `uid` (R8, SO_PEERCRED) is the sole
    authority — never a value from `body`.

    `leaf_builder` is an injectable seam so R11 can exercise the REFUSE_NAMESPACE
    guard (unreachable through the validated wire path) by feeding an escaping leaf.
    """
    writer = allowlist.account_for_uid(uid)
    if writer is None:
        return None, REFUSE_UNLISTED
    # Parse/validate the body first? No — R3 authorizes by uid, then the body's
    # signal_kind. But the reserved check must run on the *requested* kind, so parse
    # the body to obtain signal_kind. Schema/size refusals surface here too.
    request, refusal = parse_and_validate_body(body)
    if refusal is not None:
        return None, refusal
    assert request is not None
    signal_kind = request.signal_kind
    # Reserved BEFORE allowlist membership (H1): a reserved kind is refused even if a
    # (misconfigured) allowlist somehow granted it. Casefold (LOW-1).
    if is_reserved_kind(signal_kind):
        return None, REFUSE_RESERVED
    # signal_kind identifier validity already checked in parse_and_validate_body, but
    # the '/'-guard is the H3 crux — assert it explicitly (defense in depth).
    if "/" in signal_kind or not _is_clean_identifier(signal_kind):
        return None, REFUSE_BAD_IDENTIFIER
    if signal_kind not in writer.signal_kinds:
        return None, REFUSE_UNLISTED
    leaf = leaf_builder(tree_root, writer.account, signal_kind)
    # Defense-in-depth (M3): the derived leaf must resolve within the account's
    # namespace. Unreachable via the validated wire path; asserted anyway.
    account_root = (tree_root / writer.account).resolve()
    resolved = Path(os.path.realpath(leaf))
    if account_root != resolved and account_root not in resolved.parents:
        return None, REFUSE_NAMESPACE
    return leaf, None


# ---- Canary probe worker (RFC R6 §1; MED-2 isolation) ----------------------

CANARY_PROBE_PREFIX = ".healthsig-canary-"


def probe_store_once(store_path: Path, *, nonce: str) -> str:
    """Raw store-writability probe (R6 §1). Returns "ok" or "fail" — never raises.

    `O_EXCL`-create `<store_path>/.healthsig-canary-<nonce>`, write + `fsync` a small
    payload, confirm the on-disk size, then `unlink` (transient — `O_EXCL` guarantees
    no collision, `unlink` no accumulation). ANY OSError (EACCES from a missing
    named-user ACL, ENOSPC on a full store, ENOENT on a gone subdir, …) → "fail":
    a *real* end-to-end capability test, not a mode-bit/ACL-parse proxy (MED-1). The
    caller feeds the returned outcome to `build_canary_stamp` (R6 §2).
    """
    target = store_path / f"{CANARY_PROBE_PREFIX}{nonce}"
    payload = f"healthsig-probe-{nonce}".encode("utf-8")
    fd = None
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.write(fd, payload)
        os.fsync(fd)
        if os.fstat(fd).st_size != len(payload):
            return "fail"
        return "ok"
    except OSError:
        return "fail"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(target)
        except OSError:
            pass  # never created, or already gone — nothing to clean


def run_probe_with_timeout(
    store_path: Path,
    *,
    nonce: str,
    timeout_s: float,
    probe_fn=probe_store_once,
) -> str:
    """Run the probe on its OWN worker thread with a bounded timeout (MED-2).

    Returns "ok"/"fail" from the probe, or **"inconclusive"** if it does not complete
    within `timeout_s` (slow, not proven dead — so transient hub slowness never reads
    as `store-dead`). The probe runs off the caller's thread, so a hung `fsync` never
    stalls the accept loop / other signal writes. A timed-out worker is a daemon
    thread (abandoned; the OS reclaims it on process exit, and a leaked probe file is
    swept on next start — see `sweep_stale_probes`); `timeout_s` MUST be strictly less
    than the caller's `canary_period`.
    """
    import threading

    result: dict[str, str] = {}

    def _worker() -> None:
        result["outcome"] = probe_fn(store_path, nonce=nonce)

    t = threading.Thread(target=_worker, name=f"healthsig-probe-{nonce}", daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return "inconclusive"
    return result.get("outcome", "fail")


def sweep_stale_probes(store_path: Path) -> int:
    """Remove leftover `.healthsig-canary-*` files under `store_path` (LOW-3).

    A broker that died between `O_EXCL`-create and `unlink` leaks a probe file; across
    crashes they accumulate in a maildir `tmp/`. Called on start (before serving).
    Only this broker's own prefix is touched — a foreign file is never removed.
    Returns the count removed. Never raises (a probe target may be unreadable).
    """
    removed = 0
    try:
        entries = list(store_path.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(CANARY_PROBE_PREFIX):
            continue
        try:
            entry.unlink()
            removed += 1
        except OSError:
            pass
    return removed


# ---- Canary stamp state machine (RFC R6 §2; NEW-1/NEW-2) -------------------

CANARY_SCHEMA = "pantheon-health-canary/v1"
PROBE_OUTCOMES = ("ok", "fail", "inconclusive")


def next_inconclusive_tracking(
    prev: Mapping[str, Any] | None, outcome: str, *, now_iso: str
) -> tuple[int, str | None]:
    """Compute (consecutive_inconclusive, inconclusive_since) for a new probe outcome.

    NEW-2: `consecutive_inconclusive` counts an unbroken run of `inconclusive`
    outcomes and RESETS to 0 on any `ok`/`fail`; `inconclusive_since` holds the
    wall-clock start of the current streak (carried forward across the streak), else
    null. Emitting both lets the consumer enforce a bounded-inconclusive escalation
    from a SINGLE canary read (the threshold + paging are consumer-scope, M5).
    """
    if outcome != "inconclusive":
        return 0, None
    if prev is not None and prev.get("probe_ok") == "inconclusive":
        count = int(prev.get("consecutive_inconclusive", 0)) + 1
        since = prev.get("inconclusive_since") or now_iso
    else:
        count = 1
        since = now_iso
    return count, since


def build_canary_stamp(
    *,
    outcome: str,
    prev: Mapping[str, Any] | None,
    boot_id: str,
    monotonic_since_boot: float,
    wall_clock_written_at: str,
    probe_store_path: str,
) -> dict[str, Any]:
    """Build the canary stamp written EVERY cadence (NEW-1 — never "only on success").

    The broker refreshes the canary on every probe outcome (ok/fail/inconclusive), so
    a fresh `probe_ok=fail` is a *positive*, readable store-death signal and a STALE
    canary means only "broker not writing" (see R10). Un-forgeability comes from FILE
    OWNERSHIP (the canary is written only by pantheon-healthsig in its own /var/lib
    tree — enforced by the caller that writes this dict), not from anything here.
    """
    if outcome not in PROBE_OUTCOMES:
        raise ValueError(f"outcome must be one of {PROBE_OUTCOMES}, got {outcome!r}")
    count, since = next_inconclusive_tracking(prev, outcome, now_iso=wall_clock_written_at)
    return {
        "schema": CANARY_SCHEMA,
        "probe_ok": outcome,
        "boot_id": boot_id,
        "monotonic_since_boot": monotonic_since_boot,
        "wall_clock_written_at": wall_clock_written_at,
        "probe_store_path": probe_store_path,
        "consecutive_inconclusive": count,
        "inconclusive_since": since,
    }


# ---- SO_PEERCRED + socket transport (RFC R8/R2/R1; M6) ---------------------

import socket  # noqa: E402  (kept near its use; stdlib, no import-time cost concern)
import time  # noqa: E402

# struct ucred { pid_t pid; uid_t uid; gid_t gid; } — pid signed, uid/gid UNSIGNED
# (L1: `iII`, never `3i`, so a valid high uid is never read as a negative alias).
_UCRED = struct.Struct("iII")

READ_DEADLINE_S = 0.75  # M6: sub-second per-connection read deadline (slow-loris bound)
MAX_CONNS = 16          # M6: bounded connection pool
PER_UID_INFLIGHT = 2    # M6: one account cannot exhaust the pool


def read_peer_uid(conn: "socket.socket") -> int:
    """Kernel-attested caller uid via SO_PEERCRED (R8). Read-only; never trusts the
    request body. `SCM_CREDENTIALS` is deliberately NOT used (D-8)."""
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    _pid, uid, _gid = _UCRED.unpack(creds)
    return uid


def _recv_exactly(conn: "socket.socket", n: int, deadline: float) -> bytes | None:
    """Read exactly n bytes before `deadline` (monotonic). None on timeout/EOF/error.
    Never "read until close" — the length is bounded by the caller (R2)."""
    chunks: list[bytes] = []
    got = 0
    while got < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            conn.settimeout(remaining)
            chunk = conn.recv(n - got)
        except (TimeoutError, socket.timeout, OSError):
            return None
        if not chunk:
            return None  # peer closed before sending n bytes
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_request_frame(
    conn: "socket.socket", *, read_deadline_s: float = READ_DEADLINE_S
) -> tuple[bytes | None, str | None]:
    """Read one length-prefixed request body within the deadline (R2/M6).
    Returns (body, None) or (None, REFUSE_*). A short/oversized/timed-out read is a
    typed refusal — never a hang and never an unbounded read."""
    deadline = time.monotonic() + read_deadline_s
    prefix = _recv_exactly(conn, LEN_PREFIX_SIZE, deadline)
    if prefix is None:
        return None, REFUSE_MALFORMED
    length = parse_length_prefix(prefix)
    if length is None or length < 1:
        return None, REFUSE_MALFORMED
    if length > MAX_FRAME:
        return None, REFUSE_OVERSIZED
    body = _recv_exactly(conn, length, deadline)
    if body is None:
        return None, REFUSE_MALFORMED
    return body, None


def build_signal_record(
    *, account: str, signal_kind: str, payload: Mapping[str, Any], uid: int, now_iso: str
) -> bytes:
    """The stored leaf record: the caller's fact plus SERVER-derived provenance
    (account/uid are server-derived from SO_PEERCRED, never the body — D-1)."""
    return json.dumps(
        {
            "schema": "pantheon-health-signal/v1",
            "account": account,
            "signal_kind": signal_kind,
            "uid": uid,
            "wall_clock_written_at": now_iso,
            "payload": dict(payload),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


LEAF_MODE = 0o644  # R1: world-readable so any account can verify freshness; service-writable only.


def atomic_write_leaf(leaf: Path, data: bytes) -> None:
    """Atomic temp+rename write with O_NOFOLLOW on all leaves (L4). The <account> dir
    is created if absent (owned by the service under StateDirectory in production).

    The leaf is fchmod'd to 0644 EXPLICITLY (not left to the open-mode): R1 requires
    signal files be world-readable so a *different* account can verify freshness, but
    the unit runs with a hardened UMask=0077 (which would strip the open-mode to 0600).
    fchmod overrides the umask, so world-readability holds regardless of the service's
    umask. Ownership (pantheon-healthsig) is what keeps it un-forgeable, not the mode."""
    leaf.parent.mkdir(parents=True, exist_ok=True)
    tmp = leaf.parent / f".{leaf.name}.tmp-{os.getpid()}-{time.time_ns()}"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, LEAF_MODE)
    try:
        os.write(fd, data)
        os.fchmod(fd, LEAF_MODE)  # umask-independent: guarantee 0644 under UMask=0077
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, leaf)  # atomic; replaces any symlink at the leaf path (not followed)


def handle_connection(
    conn: "socket.socket",
    *,
    allowlist: Allowlist,
    tree_root: Path = SIGNAL_TREE_ROOT,
    leaf_writer=atomic_write_leaf,
    now_iso_fn=None,
    read_deadline_s: float = READ_DEADLINE_S,
) -> str | None:
    """Serve ONE request → one response, then close (R2). Returns None on success or
    the REFUSE_* class. The leaf is written (via `leaf_writer`) ONLY on authorize
    success — every refusal writes nothing. `leaf_writer`/`now_iso_fn` are injectable
    seams so R11 can exercise the handler hermetically over a socketpair."""
    try:
        uid = read_peer_uid(conn)
        body, refusal = read_request_frame(conn, read_deadline_s=read_deadline_s)
        if refusal is not None:
            _send_response(conn, ok=False, leaf=None, refusal=refusal)
            return refusal
        leaf, refusal = authorize(uid=uid, body=body, allowlist=allowlist, tree_root=tree_root)
        if refusal is not None:
            _send_response(conn, ok=False, leaf=None, refusal=refusal)
            return refusal
        assert leaf is not None
        request, _ = parse_and_validate_body(body)  # re-parse for payload (already valid)
        writer = allowlist.account_for_uid(uid)
        now_iso = (now_iso_fn or _utc_now_iso)()
        record = build_signal_record(
            account=writer.account, signal_kind=request.signal_kind,
            payload=request.payload, uid=uid, now_iso=now_iso,
        )
        leaf_writer(leaf, record)
        _send_response(conn, ok=True, leaf=str(leaf), refusal=None)
        return None
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _send_response(conn: "socket.socket", *, ok: bool, leaf: str | None, refusal: str | None) -> None:
    try:
        conn.sendall(build_response(ok=ok, leaf=leaf, refusal=refusal))
    except OSError:
        pass  # peer gone; the refusal/success is already decided server-side


class ConnectionGate:
    """M6 concurrency bounds: a global MAX_CONNS cap + a PER_UID_INFLIGHT cap, so one
    slow/hostile account cannot exhaust the pool and mask a store-death. Pure + thread
    -safe; the accept loop calls try_acquire(uid) before dispatching and release(uid)
    when the handler finishes."""

    def __init__(self, *, max_conns: int = MAX_CONNS, per_uid: int = PER_UID_INFLIGHT) -> None:
        import threading
        self._max = max_conns
        self._per_uid = per_uid
        self._total = 0
        self._by_uid: dict[int, int] = {}
        self._lock = threading.Lock()

    def try_acquire(self, uid: int) -> bool:
        with self._lock:
            if self._total >= self._max:
                return False
            if self._by_uid.get(uid, 0) >= self._per_uid:
                return False
            self._total += 1
            self._by_uid[uid] = self._by_uid.get(uid, 0) + 1
            return True

    def release(self, uid: int) -> None:
        with self._lock:
            if self._by_uid.get(uid, 0) > 0:
                self._by_uid[uid] -= 1
                self._total -= 1
                if self._by_uid[uid] == 0:
                    del self._by_uid[uid]


# ---- Root-owned allowlist file reader (RFC R4) + verify-by-probe (MED-1) ---

ALLOWLIST_PATH = Path("/etc/pantheon/health-signal-writers.conf")
ALLOWLIST_MAX_BYTES = 64 * 1024  # generous bound for the writers list


def load_allowlist(
    path: Path = ALLOWLIST_PATH, *, loader: "RootOwnedFileLoader | None" = None
) -> Allowlist:
    """Read + validate the ROOT-OWNED allowlist (R4).

    Reads via trusted_files' `RootOwnedFileLoader` — `O_NOFOLLOW` per path hop,
    `st_uid == 0`, size-bounded, duplicate-key rejection — so a runtime message can
    never mutate policy (ORG-050) and a symlinked / non-root-owned / oversized file is
    refused. Then `Allowlist.from_mapping` fail-closed-validates the content (H1
    reserved-kind rejection, unique int uid, IDENTIFIER_RE-clean fields). `loader` is
    injectable for tests (a `_open_for_test` loader over a tmp tree owned by the test
    uid). Any trust/parse failure surfaces as `SignalWriterConfigError` — fail closed.
    """
    own = loader is None
    if own:
        loader = RootOwnedFileLoader.open((path.parent,))
    try:
        doc = loader.read_json(path, max_bytes=ALLOWLIST_MAX_BYTES)
    except RegistryError as exc:
        raise SignalWriterConfigError(f"allowlist unreadable/untrusted: {exc}") from exc
    finally:
        if own:
            loader.close()
    return Allowlist.from_mapping(doc)


def verify_store_writable(
    store_path: Path, *, timeout_s: float = READ_DEADLINE_S, nonce: str | None = None
) -> bool:
    """MED-1 verify: confirm the broker can actually write `store_path` by an ACTUAL
    `O_EXCL` probe (create + immediately unlink) — NOT an `st_mode & S_IWGRP` proxy,
    which false-passes on the ACL mask (`0o2770` reads writable while the real
    `group::` entry is `---`). Returns True iff the probe returns "ok"; a probe that
    fails or times out ("inconclusive") is NOT writable-confirmed → False. In
    production this must run AS `pantheon-healthsig`, so it co-gates with apply on the
    T-2920 supervised-root backend (a plain unprivileged verify cannot run it); the
    logic is identical here and is what the installer `verify` (R9) calls per store.
    """
    import uuid

    probe_nonce = nonce or uuid.uuid4().hex
    return run_probe_with_timeout(store_path, nonce=probe_nonce, timeout_s=timeout_s) == "ok"


# ---- Daemon serve loop + SIGHUP reload (RFC R1/R9; M7) ---------------------


def reload_allowlist(
    current: Allowlist, path: Path = ALLOWLIST_PATH, *, loader: "RootOwnedFileLoader | None" = None
) -> tuple[Allowlist, bool]:
    """M7 SIGHUP reload: re-read + validate the allowlist, atomic-swap ONLY on success.

    Returns (new_allowlist, True) on a valid reload, else (current, False) — the
    LAST-GOOD config is kept. A malformed/untrusted/unreadable allowlist never crashes
    the daemon and is never adopted mid-flight (ORG-050: policy comes only from the
    root-owned file, and a bad reload is inert)."""
    try:
        new = load_allowlist(path, loader=loader)
    except (SignalWriterConfigError, OSError):
        return current, False
    return new, True


class SignalWriterServer:
    """The accept loop that wires the SO_PEERCRED transport (R8) + authorization (R3)
    + the M6 ConnectionGate + M7 reload into a runnable daemon.

    Kept injectable/stepwise (`serve_one`) so it is testable over a real listener
    without spawning the full forever-loop. SIGHUP is handled flag-only (M7): the
    async-signal-safe handler calls `request_reload()`; the reload itself runs in the
    main loop between connections (re-read + validate + atomic-swap-on-success).

    Capacity shedding (M6): when the ConnectionGate is full (global or per-uid), the
    connection is CLOSED immediately (the caller retries) rather than queued — this is
    an operational overload response, distinct from the typed request-refusals (R5),
    so it introduces no new REFUSE_* class. It is counted for observability.
    """

    def __init__(
        self,
        listener: "socket.socket",
        allowlist: Allowlist,
        *,
        tree_root: Path = SIGNAL_TREE_ROOT,
        allowlist_path: Path = ALLOWLIST_PATH,
        gate: "ConnectionGate | None" = None,
        leaf_writer=atomic_write_leaf,
        reload_loader: "RootOwnedFileLoader | None" = None,
    ) -> None:
        self._listener = listener
        self._allowlist = allowlist
        self._tree_root = tree_root
        self._allowlist_path = allowlist_path
        self._gate = gate if gate is not None else ConnectionGate()
        self._leaf_writer = leaf_writer
        # In production `reload_loader` is None -> reload_allowlist opens the root-owned
        # file itself; tests inject a _open_for_test loader over a tmp tree.
        self._reload_loader = reload_loader
        self._reload_requested = False
        self._stopping = False
        self.shed_count = 0  # M6 capacity-shed counter (observability)

    def request_reload(self) -> None:
        """SIGHUP handler entry point — flag-only, async-signal-safe (no I/O)."""
        self._reload_requested = True

    def request_stop(self) -> None:
        self._stopping = True

    @property
    def allowlist(self) -> Allowlist:
        return self._allowlist

    def _maybe_reload(self) -> bool | None:
        """Process a pending reload between connections. Returns True/False if a reload
        ran (swapped / kept-last-good), None if none was pending."""
        if not self._reload_requested:
            return None
        self._reload_requested = False
        self._allowlist, ok = reload_allowlist(
            self._allowlist, self._allowlist_path, loader=self._reload_loader
        )
        return ok

    def serve_one(self, *, accept_timeout: float | None = None) -> str:
        """One accept -> gate -> dispatch iteration. Returns a short status
        ("idle"/"gated"/"ok"/REFUSE_*) for stepwise testing. Processes a pending
        reload first (M7)."""
        self._maybe_reload()
        try:
            if accept_timeout is not None:
                self._listener.settimeout(accept_timeout)
            conn, _ = self._listener.accept()
        except (TimeoutError, socket.timeout):
            return "idle"
        uid = read_peer_uid(conn)
        if not self._gate.try_acquire(uid):
            self.shed_count += 1
            try:
                conn.close()  # capacity shed: drop, caller retries (M6)
            except OSError:
                pass
            return "gated"
        try:
            outcome = handle_connection(
                conn, allowlist=self._allowlist, tree_root=self._tree_root,
                leaf_writer=self._leaf_writer,
            )
            return outcome if outcome is not None else "ok"
        finally:
            self._gate.release(uid)

    def serve_forever(self, *, accept_timeout: float = 1.0) -> None:
        """Blocking accept loop until `request_stop()`. Each connection is handled
        inline within the gate (a production build may dispatch to a bounded worker
        pool; the gate already bounds concurrency). The accept_timeout lets the loop
        notice stop/reload flags between connections."""
        while not self._stopping:
            self.serve_one(accept_timeout=accept_timeout)


def install_signal_handlers(server: SignalWriterServer) -> None:
    """Wire SIGHUP -> reload flag and SIGTERM/SIGINT -> stop. MAIN-THREAD only
    (Python restricts signal.signal to the main thread). The SIGHUP handler is
    flag-only (M7); no I/O runs in signal context."""
    import signal

    signal.signal(signal.SIGHUP, lambda *_: server.request_reload())
    signal.signal(signal.SIGTERM, lambda *_: server.request_stop())
    signal.signal(signal.SIGINT, lambda *_: server.request_stop())


# ---- Response building (RFC R2) --------------------------------------------


def build_response(*, ok: bool, leaf: str | None, refusal: str | None) -> bytes:
    """Serialize a framed response body (R2). Returns the framed bytes."""
    body = json.dumps(
        {"v": PROTOCOL_VERSION, "ok": ok, "leaf": leaf, "refusal": refusal},
        separators=(",", ":"),
    ).encode("utf-8")
    return frame(body)
