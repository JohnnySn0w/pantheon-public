"""tools/mailbox.py — daemonless per-agent file-mailbox (ORG-026, T-0772).

The inter-agent message-passing transport ratified in ADR ORG-026: a Maildir-style
per-agent inbox on the shared filesystem. DIY (no vendored dependency, per the
ratification amendment — attack-surface minimization). This module is the TRANSPORT
core only (send + consume); the wake/deployment layer (systemd --user .path units,
per-account provisioning, linger) is the next increment.

Layout — each agent owns `inbox/{tmp,new,cur}`:

    inbox/tmp/   staging; a half-written message lives here and is never enumerated
    inbox/new/   delivered, unconsumed; "a message IS a file in new/"
    inbox/cur/   acked (consumed); the audit trail

Delivery is atomic and lockless (ORG-026 §Decision 1):
    write tmp/<name>  ->  fsync(file)  ->  rename tmp/->new/  ->  fsync(new/ dir)
`rename` is atomic on a single FS, so a reader in new/ never sees a partial file.
Multi-writer-safe by unique sortable names — no locks on the send path.

Consumption (ORG-026 §Decision 3 — the REQ2 acceptance bar):
    "inotify is a hint, readdir(new/) is the TRUTH."
The consumer authoritatively rescans new/ (sorted) on every call — it never trusts
an inotify event to enumerate work. Ack is at-least-once via move new/->cur/, so a
HANDLER MUST BE IDEMPOTENT. On handler error the message is left in new/ for retry
(DLQ shape is a deferred refinement). A per-inbox single-consumer lease
(tools/_lease.py) serializes concurrent consumers; if the lease is held, consume()
returns immediately (skipped) rather than racing a second drain.

Offline-survival (the T-0772 PoC acceptance): a message sent while the target is
killed/capped simply sits in new/ on disk and is processed on the next consume —
durability is the filesystem's, not a daemon's.
"""
from __future__ import annotations

import errno
import itertools
import json
import os
import re
import time
from pathlib import Path

# Import the shared lease tools.-first so a full `unittest discover -s tools` (module
# id `tools._lease`) and a bare run from inside tools/ both resolve to ONE module
# object — the double-import trap that defeats mocks (feedback_full_suite_leak...).
try:
    from tools._lease import Lease
except ImportError:  # bare run with cwd=tools/
    from _lease import Lease

# A sender label becomes part of the on-disk filename — constrain it so it can never
# inject a path separator, whitespace, or the '.' field-delimiter ambiguity.
_SENDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_SUBDIRS = ("tmp", "new", "cur")

# A recipient inbox path flows into _ensure_subdirs, which will mkdir at ANY path it is
# handed. When that path originates from an untrusted mailbox payload (a peer-written
# reply_inbox), an unvalidated value is a path-injection sink — traversal or an odd
# charset would create directories outside the mailbox tree. Constrain every caller AT
# THE SINK (send) to an absolute, traversal-free, safe-charset path. This is the
# defense-in-depth root guard ancestor_consume's payload-level _safe_inbox check (this
# same shape, applied earlier) layers on top of (T-0906).
_INBOX_RE = re.compile(r"/[A-Za-z0-9/._-]+\Z")


def is_safe_inbox(path) -> bool:
    """True iff `path` is an absolute, traversal-free, safe-charset inbox path.

    The sink-level recipient guard for send(): a path must satisfy this before
    _ensure_subdirs creates directories at it. Mirrors the sender-name contract
    (_SENDER_RE) — a constrained surface so untrusted input can never escape the tree.
    """
    s = "" if path is None else str(path)
    return bool(s) and ".." not in s and bool(_INBOX_RE.match(s))

# Process-wide monotonic counter — guarantees uniqueness for two sends within the
# same nanosecond from one process (pid + seq together make the name collision-proof).
_seq = itertools.count()


# --------------------------------------------------------------------------- naming
def _make_name(sender: str, ts_ns: int, seq: int) -> str:
    """Unique, lexically-sortable message name: `<ts_ns>.<pid>.<seq>.<sender>`.

    ts_ns is zero-padded to a fixed width so lexical sort == chronological order
    (single host = one clock, per ORG-026). pid+seq disambiguate same-instant sends.
    """
    return f"{ts_ns:020d}.{os.getpid()}.{seq:06d}.{sender}"


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a rename into it is durable across power loss."""
    dfd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _ensure_subdirs(inbox: Path) -> None:
    for sub in _SUBDIRS:
        (inbox / sub).mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------------- send
def send(recipient_inbox, payload, sender: str, *, subject: str | None = None) -> str:
    """Deliver `payload` to `recipient_inbox` from `sender`. Returns the message id.

    Atomic + lockless: stage in tmp/, fsync, rename into new/, fsync the dir. The
    message lands in new/ complete-or-not-at-all; a concurrent consumer never sees a
    partial file. `payload` must be JSON-serializable (a TypeError propagates if not).
    """
    if not _SENDER_RE.fullmatch(sender or ""):
        raise ValueError(
            f"invalid sender {sender!r}: must match [A-Za-z0-9][A-Za-z0-9_-]* "
            "(it becomes part of the on-disk message name)"
        )
    if not is_safe_inbox(recipient_inbox):
        raise ValueError(
            f"invalid recipient_inbox {recipient_inbox!r}: must be an absolute, "
            "traversal-free, safe-charset path (it is created as a directory at the sink)"
        )
    inbox = Path(recipient_inbox)
    _ensure_subdirs(inbox)

    ts_ns = time.time_ns()
    name = _make_name(sender, ts_ns, next(_seq))
    record = {
        "id": name,
        "sender": sender,
        "subject": subject,
        "ts_ns": ts_ns,
        "ts": ts_ns / 1_000_000_000,
        "payload": payload,
    }
    data = (json.dumps(record) + "\n").encode("utf-8")

    tmp_path = inbox / "tmp" / name
    new_path = inbox / "new" / name
    # O_EXCL: a name collision is a hard bug (pid+seq+ts_ns should preclude it), not
    # something to silently overwrite.
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp_path, new_path)  # atomic on a single FS
    _fsync_dir(inbox / "new")
    return name


# -------------------------------------------------------------------------- consume
def _drain(new: Path, cur: Path, handler) -> dict:
    """Process every message currently in new/ (sorted), acking by move to cur/.

    readdir(new/) is the authoritative work list — we never trust an event to tell
    us what to process. A handler that raises leaves its message in new/ for a later
    retry (at-least-once → handler must be idempotent); an unreadable file is recorded
    and left in new/ too (DLQ is a deferred refinement, not a silent drop).
    """
    processed: list[str] = []
    errors: list[dict] = []
    names = sorted(p.name for p in new.iterdir() if p.is_file())
    for name in names:
        src = new / name
        try:
            msg = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append({"id": name, "error": f"unreadable: {exc}"})
            continue  # leave in new/; do not silently drop
        try:
            handler(msg)
        except Exception as exc:  # handler owns its errors; we only sequence retries
            errors.append({"id": name, "error": f"handler: {exc}"})
            continue  # leave in new/ for retry
        try:
            os.rename(src, cur / name)  # ack-by-move
            processed.append(name)
        except OSError as exc:
            errors.append({"id": name, "error": f"ack-failed: {exc}"})
    return {"skipped": False, "processed": processed, "errors": errors}


def consume(inbox, handler, *, use_lease: bool = True, ttl_secs: int | None = None,
            label: str | None = None) -> dict:
    """Drain `inbox`'s new/ through `handler`, acking each by move to cur/.

    Acquires a per-inbox single-consumer lease (non-blocking). If another consumer
    holds it, returns {"skipped": True, ...} immediately rather than racing a second
    drain. Returns {"skipped", "processed": [ids], "errors": [{id,error}]}.

    handler(msg: dict) is called once per message (msg has id/sender/subject/ts/payload).
    Raising signals failure → the message stays in new/ for the next consume. Returning
    normally acks it. Because ack is at-least-once, the handler MUST be idempotent.
    """
    inbox = Path(inbox)
    _ensure_subdirs(inbox)
    new, cur = inbox / "new", inbox / "cur"

    if not use_lease:
        return _drain(new, cur, handler)

    lease_kwargs = {"label": label or f"mailbox-consume:{inbox}"}
    if ttl_secs is not None:
        lease_kwargs["ttl_secs"] = ttl_secs
    lease = Lease(inbox / ".consumer.lock", **lease_kwargs)
    if not lease.acquire():
        return {"skipped": True, "processed": [], "errors": []}
    try:
        return _drain(new, cur, handler)
    finally:
        lease.release()


# ------------------------------------------------------------------------- provision
def provision(inbox, *, mode: int = 0o2770) -> Path:
    """Create inbox/{tmp,new,cur} with a group-writable setgid mode (default 02770).

    setgid makes new files inherit the parent's group (pantheon-shared) so any agent
    can deliver. Group ownership itself is left to the deployment layer (chgrp needs
    the directories to already exist and the caller to be in the group).
    """
    inbox = Path(inbox)
    _ensure_subdirs(inbox)
    for sub in _SUBDIRS:
        try:
            os.chmod(inbox / sub, mode)
        except OSError as exc:  # best-effort; deployment layer owns ownership/perms
            if exc.errno not in (errno.EPERM, errno.EACCES):
                raise
    return inbox
