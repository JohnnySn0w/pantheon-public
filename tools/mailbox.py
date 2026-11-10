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
HANDLER MUST BE IDEMPOTENT. On handler error the message is left in new/ for retry.
A per-inbox single-consumer lease (tools/_lease.py) serializes concurrent consumers;
if the lease is held, consume() returns immediately (skipped) rather than racing a
second drain.

Dead-letter quarantine (ORG-026 deferred refinement, landed T-1876): a message that
fails the SAME way `dlq_max_attempts` times (default 3) — handler raises, or the
message file is unreadable — is moved new/->failed/ instead of retried forever, and
an alert is sent back into the same inbox describing it. Only a RECORDED failure (the
code actually reaching the except-branch) counts; a hard crash mid-handler reaches no
such code, so it is never counted and the message just retries next consume() as
before — crash-storms stay plain at-least-once retries, DLQ targets poison content.
Attempt counts live in a lazily-created inbox/.attempts/<name> sidecar keyed by the
message's on-disk filename, so in-flight messages need no format change and keep
their exact filename until quarantine (see _drain / _maybe_quarantine below).

Transient-failure opt-out (T-2038): a handler that raises an exception carrying a
truthy `mailbox_transient` attribute signals an ENVIRONMENTAL failure — one that
fails every message the same way (an upstream usage-cap, a wedged wrapper) rather
than message poison. Such a failure is recorded but NOT counted toward DLQ: the
message stays in new/ and retries cleanly when the environment recovers, instead of
being quarantined (which would dead-letter innocent messages and — via the
quarantine-exempt mailbox-dlq alerts — spin forever). Duck-typed so mailbox.py never
imports a consumer's exception types.

Offline-survival (the T-0772 PoC acceptance): a message sent while the target is
killed/capped simply sits in new/ on disk and is processed on the next consume —
durability is the filesystem's, not a daemon's.
"""
from __future__ import annotations

import errno
import hashlib
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
    from tools._lease import Lease, UNKNOWN
except ImportError:  # bare run with cwd=tools/
    from _lease import Lease, UNKNOWN

# A sender label becomes part of the on-disk filename — constrain it so it can never
# inject a path separator, whitespace, or the '.' field-delimiter ambiguity.
_SENDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_SUBDIRS = ("tmp", "new", "cur")

# Group-writable setgid mode shared by the eager subdirs (provision()) and any
# lazily-created dir (failed/, .attempts/) so ownership/mode stays uniform.
_DIR_MODE = 0o2770

# Reserved sender for DLQ quarantine alerts (T-1876). A message with this sender is
# exempt from DLQ attempt-counting — an alert-about-an-alert must never itself be
# able to quarantine-loop. Same same-user/same-group trust model as the rest of this
# module (not a spoof barrier, just a loop-prevention convention).
_DLQ_ALERT_SENDER = "mailbox-dlq"

# Default N for consume(dlq_max_attempts=...): quarantine after this many RECORDED
# failed consumptions of the same message (see module docstring).
_DEFAULT_DLQ_MAX_ATTEMPTS = 3

# ---------------------------------------------------------------- claim step (T-2287)
# The consumer lease used to be held across the WHOLE drain, handler included, so a
# long agent turn kept its own mailbox closed for the duration (specialist measured 47
# minutes). It cannot simply be released around the handler: the ack rename happens
# AFTER the handler returns — deliberately, so a crash mid-handler cannot lose the
# message — so a short lease without a claim step means a second consumer finds the
# message still in new/ and delivers it TWICE. That trades a loud, recoverable
# liveness problem for a silent duplicate-delivery one, which is strictly worse.
#
# So: under the lease, messages are CLAIMED by renaming new/ -> .inflight/<claim>/;
# the lease is released; the handler runs; then .inflight/<claim>/ -> cur/. The lease
# now covers only a rename loop (sub-millisecond), and at-least-once is preserved by
# the intermediate state — a crashed consumer leaves its messages in .inflight/ where
# the reaper requeues them.
_INFLIGHT_DIR = ".inflight"

# Each claim gets its OWN subdir holding an flock'd owner file, rather than all
# consumers sharing one in-flight dir. Ownership has to be per-claim because the
# reaper's whole job is deciding "is this batch abandoned", and with a shared dir it
# could not tell one live consumer's messages from a dead one's — it would requeue
# live work and reintroduce the duplicate delivery the claim step exists to prevent.
_CLAIM_OWNER_LOCK = ".owner.lock"

# The claim lease's TTL. This is the number T-2287 asks to reconsider, and the claim
# step is what makes it answerable: the lease no longer spans a handler, a prompt, or
# a turn — only the rename loop — so the old 7200s (set by T-1935 to stop a healthy
# long-turn holder being declared stale) is now three orders of magnitude larger than
# any legitimate hold.
#
# ⚠️ WHY THIS MATTERS BEYOND TIDINESS, per specialist on T-2287: acquire(reap_stale=True)
# REAPS a holder that is past its own recorded TTL, and the reap escalates to
# SIGKILL of the holder's process GROUP. So the TTL is not only "how long a crashed
# holder blocks live consumers" — it is also "how long a HEALTHY holder is safe from
# being killed by the next acquirer", and those two pull in opposite directions.
# Under the old design lowering it made the second worse. Under the claim step the
# tension dissolves rather than being traded off, because no healthy holder is ever
# in the window.
#
# 300s is deliberately still ~5 orders of magnitude above a real claim: the margin is
# for a pathologically slow filesystem, not for a slow handler, and it sits far below
# the 1800s prompt_timeout that motivated the 7200 in the first place.
_DEFAULT_CONSUME_TTL_SECS = 300

# Severity envelope (ORG-026, T-1640 Ph1). Additive, INERT: nothing reads this field
# yet — it is validated + recorded only, so landing it changes zero runtime behavior.
_VALID_SEVERITIES = frozenset({"normal", "urgent"})

# ---------------------------------------------------------- urgent Phase 2 (T-1641)
# BUILD-NOT-ACTIVATE KILL SWITCH. `severity="urgent"` is NOT hypothetical-future-only
# by the time this lands — five live producers already use it (this module's own
# _alert_owner DLQ path, credential_expiry_monitor, disk_usage_monitor,
# the since-removed ancestor_consume, unit_failure_alert), and mailbox_wake's timer sweep is ALREADY
# running fleet-wide for every persona. So every Phase-2 side effect below (rate-limit
# bookkeeping, the .urgent_pending marker, mailbox_wake's debounce-bypass) is gated by
# this ONE flag, default OFF, so merging this file changes ZERO live behavior. With the
# flag unset, send() is byte-for-byte identical to Phase 1. Flipping it on is a
# separate, operator-coordinated supervised-first-fire step (T-1640 design note §Build
# phasing) — outside this ticket's scope.
_URGENT_PHASE2_ENV = "PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED"


def urgent_phase2_enabled() -> bool:
    """True iff the Phase-2 urgent-path mechanics are switched on for this process.
    Read fresh each call (not cached) so a test/operator flipping the env var mid
    -process takes effect immediately, matching HOOK_CHECK_ENABLED_ENV's pattern in
    context_fill_monitor.py."""
    return os.environ.get(_URGENT_PHASE2_ENV, "").strip().lower() in ("1", "true", "yes")


# Per-(sender, recipient) urgent rate limit: max N urgent sends per window: excess
# DOWNGRADES to normal + logs — NEVER drops (T-1640 design note §Guards). Scoped per
# recipient inbox, not globally per sender, because the thrash this guards against is
# "one working agent gets spammed" — the same inbox-scoped-sidecar shape as
# .attempts/ and .backoff/ above. Env-overridable, matching _BACKOFF_BASE_SECONDS.
URGENT_RATE_LIMIT_COUNT = int(os.environ.get("PANTHEON_MAILBOX_URGENT_RATE_LIMIT_COUNT", "3"))
URGENT_RATE_LIMIT_WINDOW_SECONDS = float(
    os.environ.get("PANTHEON_MAILBOX_URGENT_RATE_LIMIT_WINDOW_SECONDS", "300")
)


def _urgent_rate_path(inbox: Path, sender: str) -> Path:
    return inbox / ".urgent_rate" / f"{sender}.json"


def _check_urgent_rate_limit(inbox: Path, sender: str, now: float | None = None) -> bool:
    """True iff this urgent send from `sender` to `inbox` is still within the
    fixed-window limit (and the window's counter has been advanced); False means the
    caller must downgrade this send to normal.

    FAIL-OPEN TOWARD TRUE (keep urgent) on any bookkeeping error — a sidecar glitch
    must never be the reason a genuinely urgent message goes quiet. Mirrors the
    fail-soft direction of backoff_active()/_record_backoff() above: a broken store
    degrades to the pre-Phase-2 behavior (deliver as asked), never to silence.
    """
    now = time.time() if now is None else now
    path = _urgent_rate_path(inbox, sender)
    try:
        _ensure_dir(path.parent)
        try:
            state = json.loads(path.read_text())
            window_start = float(state["window_start"])
            count = int(state["count"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            window_start, count = now, 0
        if now - window_start > URGENT_RATE_LIMIT_WINDOW_SECONDS:
            window_start, count = now, 0
        count += 1
        path.write_text(json.dumps({"window_start": window_start, "count": count}))
        return count <= URGENT_RATE_LIMIT_COUNT
    except OSError:
        return True  # fail-open


def _log_urgent_downgrade(inbox: Path, sender: str, subject) -> None:
    """Best-effort audit trail for a rate-limited urgent->normal downgrade. Same
    never-fail-the-send contract as _record_sent: a lost log line is a strictly
    smaller harm than turning a completed (downgraded) send into an exception."""
    try:
        path = inbox / ".urgent_rate" / f"{sender}.downgrades.jsonl"
        _ensure_dir(path.parent)
        line = json.dumps({
            "ts": time.time(), "sender": sender, "subject": subject,
            "reason": "urgent_rate_limit_exceeded",
        }) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def _write_urgent_marker(inbox: Path, sender: str, subject, name: str) -> None:
    """Drop the cheap `.urgent_pending` marker the recipient-side PreToolUse hook
    (tools/hooks/urgent-mail-inject.py) stats every tool call. Best-effort, NEVER
    allowed to fail delivery — the message is already durably in new/ by the time
    this runs (T-2275's "delivered at this point" invariant). Deliberately NOT the
    atomic tmp+fsync+rename dance the maildir itself uses: this is a disposable
    nudge, not a delivery — the hook already tolerates a missing/corrupt marker
    (fail-open), so a torn write here costs at most one skipped nudge, not a lost
    message."""
    try:
        (inbox / ".urgent_pending").write_text(
            json.dumps({"sender": sender, "subject": subject, "msgid": name,
                       "ts": time.time()}) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass

# A recipient inbox path flows into _ensure_subdirs, which will mkdir at ANY path it is
# handed. When that path originates from an untrusted mailbox payload (a peer-written
# reply_inbox), an unvalidated value is a path-injection sink — traversal or an odd
# charset would create directories outside the mailbox tree. Constrain every caller AT
# THE SINK (send) to an absolute, traversal-free, safe-charset path. This is the
# defense-in-depth root guard (T-0906). Its original payload-level layer,
# ancestor_consume's _safe_inbox check, was removed with that module (T-4741 #835).
_INBOX_RE = re.compile(r"/[A-Za-z0-9/._-]+\Z")


class MailboxNotFound(FileNotFoundError):
    """Raised by send() when `recipient_inbox` is not an already-provisioned
    mailbox (see exists()) and provisioning was not explicitly requested via
    `provision_if_missing=True` (T-2216).

    Subclasses FileNotFoundError, not a bare Exception/OSError: "no such
    mailbox" IS "no such file/directory" idiomatically, and every existing
    fail-soft caller in this codebase already catches OSError (or the
    (OSError, ValueError, TypeError) tuple several producers use) — they
    catch this for free, with zero code changes at those call sites.
    """


def _alias_hint(inbox: Path) -> str:
    """T-4311: when a missing inbox is named by a persona ALIAS (consultant), name the
    canonical box that does exist (codex). Only a hint in the error: send() never
    redirects, since delivering to a path other than the one given would move the
    trust boundary (T-2216). agent_role_map is imported lazily, as for the hub
    (see _manifest_hub), and any failure means no hint."""
    try:
        try:
            from tools.agent_role_map import PERSONA_ALIASES
        except ImportError:  # bare run with cwd=tools/
            from agent_role_map import PERSONA_ALIASES
        canonical = PERSONA_ALIASES.get(inbox.name)
        if canonical and canonical != inbox.name and exists(inbox.with_name(canonical)):
            return (f"; {inbox.name} is an alias of {canonical}, whose mailbox is "
                    f"{inbox.with_name(canonical)}")
    except Exception:  # noqa: BLE001 - a hint must never mask the real error
        pass
    return ""


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


def exists(inbox) -> bool:
    """True iff `inbox` is an already-provisioned mailbox (T-2216): ALL of
    tmp/, new/, cur/ are directories.

    This is the defining surface, not a proxy. A bare top-level directory —
    or one with only SOME of the three subdirs present, e.g. a
    partially-provisioned or half-cleaned-up path — is NOT "exists"; it
    collapses into the same missing bucket as a path that was never touched
    at all. send() and consume() both key off this function and neither ever
    silently repairs a partial state into a healthy-looking one — doing so
    would reintroduce exactly the every-cheap-proxy-agrees-with-health lie
    T-2216 closes (an old/wrong hub path resurrecting itself and quietly
    accumulating undrained mail).
    """
    inbox = Path(inbox)
    return all((inbox / sub).is_dir() for sub in _SUBDIRS)


def _ensure_dir(path: Path, mode: int = _DIR_MODE) -> Path:
    """Lazily create `path` (e.g. failed/, .attempts/) with the same mode pattern
    as the eager subdirs. Best-effort chmod, mirroring provision()'s tolerance for
    a caller that doesn't own the dir."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EACCES):
            raise
    return path


# ----------------------------------------------------------------------------- send
_SESSION_ENV_VAR = "CLAUDE_CODE_SESSION_ID"


def sender_session() -> str | None:
    """The sending SEAT-SESSION's stable identifier, or None (T-2230).

    WHAT THIS EXISTS FOR. `sender` is derived from $USER, so two CONCURRENT
    sessions of one persona are one name to the recipient. That is not
    hypothetical: over 2026-07-28 17:58Z-18:41Z archivist received five
    messages signed `specialist` from two concurrent claude-research sessions with
    DIFFERENT TOOL CAPABILITIES, treated them as one correspondent, and acted
    on a diagram review whose visual claims the other session then retracted.
    The only reason the seam was ever visible is that one of them said so in
    prose.

    WHY NOT THE PID, which was the obvious cheap candidate and is already in
    the maildir filename `<ts_ns>.<pid>.<seq>.<sender>`: MEASURED over the 76
    specialist-signed messages in archivist's cur/, there are 76 DISTINCT PIDS.
    Sends go through a fresh short-lived process each time, so the pid is
    per-SEND. Surfacing it would manufacture 76 apparent correspondents out of
    one name — worse than one name, because it looks like a discriminator and
    is not one. The harness session id is per-session by construction.

    WHAT IT DOES NOT RESOLVE, stated because a field that overstates its reach
    recreates this exact bug somewhere new:
      * Non-Claude-Code seats (codex) export no such variable and get None.
        None means "this sender's harness exposes no session identity" — it is
        never fabricated, and a reader must not treat two Nones as the same
        sender-session.
      * Subagents may inherit their parent's id, so this distinguishes SEATS,
        not necessarily every concurrent worker.
      * It says WHO is speaking. It can never say whether they looked. The
        recipient-side defence — ask for a property of the artifact that only
        opening it would yield, and that you did not supply — is the stronger
        half and is unaffected by anything here.
    """
    value = os.environ.get(_SESSION_ENV_VAR)
    return value if value else None


def _payload_digest(sender: str, subject, payload, severity: str = "normal",
                    session: str | None = None) -> str:
    """Stable content digest over (sender, subject, payload, severity, session)
    — the T-1935 idempotency key. Canonical JSON (sorted keys, tight
    separators) so two sends of the same logical content always collide.
    Severity IS part of the identity: an urgent re-send of still-pending normal
    content is an escalation, not a duplicate — it must land.

    T-2230: so is the sender's SESSION. A retry is the same session, so retry
    dedupe — the thing this key exists for — is unchanged. What changes is two
    CONCURRENT sessions of one persona sending identical content: those used to
    collapse into one message, which destroyed the very fact the recipient most
    needed. Two agreeing reports from one persona are either corroboration or
    one seat's work counted twice, and collapsing them silently picks the
    wrong answer. Sessions without an id (None) keep the old behaviour exactly.
    """
    blob = json.dumps(
        {"sender": sender, "subject": subject, "payload": payload,
         "severity": severity, "session": session},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# Scan-cap for pending-duplicate detection: past this many same-sender pending
# messages, skip dedupe (fail-open to at-least-once) rather than pay an
# unbounded read on every send. A backlog this deep is a dead-consumer incident
# (the ACP backlog watchdog's territory), not a dedupe question.
_DEDUPE_SCAN_CAP = 500


def _find_pending_duplicate(new_dir: Path, sender: str, digest: str) -> str | None:
    """Return the id of a pending (new/) message from `sender` whose content
    digest matches, else None. Defensive: any unreadable/unparseable candidate
    is skipped — dedupe is best-effort; correctness rests on consumer
    idempotency (at-least-once), this just collapses the common retry shapes."""
    try:
        names = [n for n in os.listdir(new_dir) if n.endswith(f".{sender}")]
    except OSError:
        return None
    if len(names) > _DEDUPE_SCAN_CAP:
        return None
    for name in sorted(names):
        try:
            record = json.loads((new_dir / name).read_text())
        except (OSError, ValueError):
            continue
        existing = record.get("digest")
        if existing is None:
            # Legacy record predating the digest field — recompute from content.
            existing = _payload_digest(
                record.get("sender", ""), record.get("subject"),
                record.get("payload"), record.get("severity", "normal"),
                record.get("sender_session"),
            )
        if existing == digest:
            return record.get("id", name)
    return None


def _retract_pending(inbox: Path, message_id: str, sender: str) -> bool:
    """Delete `message_id` from `inbox/new/` iff it is still unread AND ours.

    Returns True if we removed it (recipient never saw it), False otherwise —
    already consumed, never existed, or belongs to a different sender.

    SENDER CHECK IS A SAFETY PROPERTY, not politeness (T-2165): supersession must
    not become a way for one agent to delete another agent's undelivered mail. A
    sender may retract only what it sent. Anything else is left alone and the
    correction is delivered as a marked supersession instead.

    Races with a concurrent drain are expected and benign: the consumer moves
    new/ -> cur/ under the per-inbox lease, so a message can vanish between our
    read and our unlink. FileNotFoundError therefore means "already read", which
    is exactly the case that must fall through to the marked-delivery path.
    """
    target = inbox / "new" / message_id
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False  # gone, unreadable, or already consumed
    if record.get("sender") != sender:
        return False  # not ours to retract
    try:
        target.unlink()
    except OSError:
        return False  # lost the race with a drain; treat as already read
    _fsync_dir(inbox / "new")
    return True


def send(recipient_inbox, payload, sender: str, *, subject: str | None = None,
         severity: str = "normal", dedupe_pending: bool = True,
         provision_if_missing: bool = False,
         supersedes: str | None = None) -> str:
    """Deliver `payload` to `recipient_inbox` from `sender`. Returns the message id.

    Delivery targets an EXISTING mailbox by default (T-2216): `recipient_inbox`
    must already satisfy `exists()` (all of tmp/, new/, cur/ present) or this
    raises `MailboxNotFound` — a well-formed-but-never-provisioned path is a
    misconfiguration (a mistyped or vanished hub, most dangerously), not
    something to paper over by silently creating it there. Provisioning a NEW
    mailbox is a deliberate act with its own call site: pass
    `provision_if_missing=True` (delegates to `provision()`, so an opted-in
    call is idempotent and mode-correct the same way a direct `provision()`
    call would be) at a genuine onboarding/seed producer.

    Idempotent-while-pending (T-1935): with `dedupe_pending=True` (default), a
    send whose (sender, subject, payload) content-digest matches a message from
    the same sender STILL SITTING UNCONSUMED in new/ is a no-op returning the
    existing message's id. This collapses the two observed duplicate shapes —
    a retried handler/turn re-emitting its sends (retry-after-no-ack), and a
    producer double-call (re-fired oneshot) — at the one shared boundary. A
    consumed message never dedupes (new/ only): re-sending the same content
    later is a fresh delivery. Best-effort (concurrent same-content senders can
    still both land; consumers stay idempotent per the at-least-once contract).
    Pass `dedupe_pending=False` for producers whose identical payloads are
    intentionally distinct events (none known today — timestamps in payloads
    already distinguish them).

    Atomic + lockless: stage in tmp/, fsync, rename into new/, fsync the dir. The
    message lands in new/ complete-or-not-at-all; a concurrent consumer never sees a
    partial file. `payload` must be JSON-serializable (a TypeError propagates if not).

    `severity` is "normal" (default) or "urgent"; an invalid value raises ValueError
    at this trust boundary alongside the sender/recipient checks. Severity discipline:
    urgent = "acting on stale state is worse than interrupting" (fleet incidents,
    wedge/spend alerts, safety stops). If everything is urgent, nothing is. Harness
    asymmetry: codex (no PreToolUse parity) does not get hook-injection for urgent —
    it relies on the escalation ladder + mailbox_wake's debounce-bypass instead. Read
    side must always use `record.get("severity", "normal")` for back-compat with
    records that predate this field.

    Phase 2 (T-1641), gated by `urgent_phase2_enabled()` (default OFF — see that
    function's docstring for why): an urgent send that exceeds
    `URGENT_RATE_LIMIT_COUNT` per `URGENT_RATE_LIMIT_WINDOW_SECONDS` for this
    (sender, recipient_inbox) pair is DOWNGRADED to normal and logged, never
    dropped — the persisted record's `severity` reflects what actually shipped.
    A send that stays urgent after that check drops a cheap `.urgent_pending`
    marker in the recipient's inbox for the PreToolUse hook
    (tools/hooks/urgent-mail-inject.py) to pick up at its next tool-call
    boundary. With the flag unset, none of this runs — send() is byte-for-byte
    Phase 1 behavior.
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
    if severity not in _VALID_SEVERITIES:
        raise ValueError(
            f"invalid severity {severity!r}: must be one of {sorted(_VALID_SEVERITIES)}"
        )
    inbox = Path(recipient_inbox)
    if provision_if_missing:
        provision(inbox)
    elif not exists(inbox):
        raise MailboxNotFound(
            f"recipient inbox {inbox} does not exist{_alias_hint(inbox)} (pass "
            "provision_if_missing=True to create it, or call provision() explicitly "
            "at a deliberate onboarding/seed site)"
        )

    # Urgent Phase 2 (T-1641), flag-gated (see urgent_phase2_enabled() above): a
    # rate-limited sender's urgent send is DOWNGRADED to normal here, before the
    # digest/record are built, so the persisted record reflects what actually
    # shipped. `effective_severity`, not `severity`, is what the rest of send()
    # uses from this point on.
    effective_severity = severity
    if severity == "urgent" and urgent_phase2_enabled():
        try:
            if not _check_urgent_rate_limit(inbox, sender):
                effective_severity = "normal"
                _log_urgent_downgrade(inbox, sender, subject)
        except Exception:
            pass  # fail-open: keep effective_severity == "urgent"

    session = sender_session()
    digest = _payload_digest(sender, subject, payload, effective_severity, session)
    if dedupe_pending:
        existing_id = _find_pending_duplicate(inbox / "new", sender, digest)
        if existing_id is not None:
            return existing_id

    # T-2165 supersession. Retract-if-unread runs BEFORE the write so the two
    # outcomes are distinguishable in the envelope the recipient actually gets.
    retracted = False
    if supersedes:
        retracted = _retract_pending(inbox, supersedes, sender)

    ts_ns = time.time_ns()
    name = _make_name(sender, ts_ns, next(_seq))
    record = {
        "id": name,
        "sender": sender,
        # T-2230: which SEAT-SESSION of `sender` sent this. None when the
        # sender's harness exposes no session identity (codex) — never
        # fabricated, and two Nones are not the same session. See
        # sender_session() for what this does and does not resolve.
        "sender_session": session,
        "subject": subject,
        "severity": effective_severity,
        # supersedes: the id this message replaces, or None.
        # superseded_was_unread: True  -> the original was deleted before the
        #   recipient ever saw it; nothing downstream acted on the retracted claim.
        #   False -> the original was ALREADY READ. This is the case that matters:
        #   a plain follow-up is indistinguishable from ordinary mail and carries
        #   no signal that work done off the first message needs revisiting.
        "supersedes": supersedes,
        "superseded_was_unread": retracted if supersedes else None,
        "ts_ns": ts_ns,
        "ts": ts_ns / 1_000_000_000,
        "digest": digest,
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
    # T-2275: the message is DELIVERED at this point. Everything below is
    # bookkeeping and is not allowed to change that.
    if effective_severity == "urgent" and urgent_phase2_enabled():
        _write_urgent_marker(inbox, sender, subject, name)
    _record_sent(inbox, record, name)
    return name


# ------------------------------------------------------------- sender-side ledger
# T-2275 (specialist): `supersedes=` needs the id of your OWN earlier message, and send()
# returned that id to stdout and kept nothing. So the retraction mechanism was
# unusable by construction — measured at 0 uses across 1552 messages and 9 senders,
# while two seats spent a night on precisely the failure it exists to prevent.
#
# The maildir is the RECIPIENT's record. Nothing was ever the sender's.


def _default_sent_ledger() -> Path:
    """Resolve the sender ledger from the call-time state root.

    Pytest redirects ``PANTHEON_STATE_DIR`` after this module may already have
    been imported.  Capturing ``Path.home()`` in a module constant therefore
    bypasses that isolation; the fallback is deliberately evaluated here.
    """
    state_dir = os.environ.get("PANTHEON_STATE_DIR", "").strip()
    root = Path(state_dir) if state_dir else Path.home() / ".local" / "state" / "pantheon"
    return root / "sent.jsonl"


def _manifest_hub() -> str:
    """instance.yaml's mailbox.hub via agent_role_map (T-4096). Imported LAZILY: this
    is the fleet's leaf transport library (codex bwrap, the a2a venv, bare cwd=tools/
    runs all import it), so it keeps agent_role_map off its module import path."""
    try:
        from tools.agent_role_map import mailbox_hub
    except ImportError:  # bare run with cwd=tools/
        from agent_role_map import mailbox_hub
    return mailbox_hub()


def _ledger_target(inbox: Path) -> Path | None:
    """Where to append for a delivery to `inbox`, or None to skip. Call-time.

    ⚠️ THE GUARD IS THE HUB CHECK, NOT AN ENV VAR, AND THAT IS THE SECOND ATTEMPT.
    My first version skipped when PANTHEON_DISABLE_LIVE_TELEGRAM / PYTEST_CURRENT_TEST
    was set. It leaked anyway: `patch.dict(os.environ, ..., clear=True)` appears in
    five test modules, so the marker this depended on is DELETED mid-run and every
    send inside that window wrote to the operator's real ledger. A guard whose input
    the thing being guarded is free to erase is not a guard.

    So the discriminator is the delivery itself: record a message only if it went to
    a real mailbox under the hub. That is environment-independent, survives an env
    clear, and is the honest semantic — this file answers "what did I actually send
    to whom", and a message into a pytest tmpdir is not something this account sent.

    Cost of the first version, measured on the real file rather than reasoned about:
    775 fixture rows, senders like "test-harness", recipients like "fixture-draining".
    The ledger's ONLY job is letting a sender find the id of a message it really sent,
    and burying real ids under hundreds of fixtures destroys exactly that.

    PANTHEON_SENT_LEDGER still overrides the destination outright, so tests that
    assert on ledger CONTENT point it at a sandbox. An explicit override always wins.
    """
    override = os.environ.get("PANTHEON_SENT_LEDGER", "").strip()
    if override:
        return Path(override)
    try:
        hub = Path(os.environ.get("PANTHEON_MAILBOX_HUB") or _manifest_hub())
        if inbox.resolve().parent != hub.resolve():
            return None          # not a real hub delivery — a fixture, not a send
    except (OSError, ImportError):
        return None              # cannot tell where it went -> do not claim we sent it
    return _default_sent_ledger()


def _record_sent(inbox: Path, record: dict, name: str) -> None:
    """Append one line to this account's sent ledger. BEST-EFFORT BY CONTRACT.

    ⚠️ This must never be able to fail a delivery. The message is already durably
    in the recipient's new/ by the time this runs — bookkeeping about a delivered
    message must not turn a success into an exception, and a lost ledger line is a
    strictly smaller harm than a message the caller believes it did not send.
    Hence the bare except and the deliberate absence of fsync: an append that loses
    its tail on a crash costs recall, not correctness.

    Append-only, one JSON object per line, opened O_APPEND so concurrent senders on
    one account interleave whole lines rather than corrupting each other — the
    two-executor case (specialist, archivist) is the normal case here, not an edge.
    """
    target = _ledger_target(inbox)
    if target is None:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "id": name,
            "ts_ns": record["ts_ns"],
            "recipient": inbox.name,
            "sender": record["sender"],
            "sender_session": record.get("sender_session"),
            "subject": record.get("subject"),
            "digest": record["digest"],
            "supersedes": record.get("supersedes"),
        }, sort_keys=True) + "\n"
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass  # see the contract above — never propagate


def sent_ledger_path() -> Path:
    """Where this account's sent ledger lives, honouring PANTHEON_SENT_LEDGER.
    Exposed so tooling and an operator can find it without re-deriving the path."""
    override = os.environ.get("PANTHEON_SENT_LEDGER", "").strip()
    return Path(override) if override else _default_sent_ledger()


# -------------------------------------------------------------------------- consume
def _dlq_attempts_path(inbox: Path, name: str) -> Path:
    return inbox / ".attempts" / name


# --------------------------------------------------- retry backoff (T-2386)
# THE INVARIANT, and it is specialist's framing because it is the right one: EXEMPTION
# FROM DLQ COUNTING MUST NOT IMPLY EXEMPTION FROM BACKOFF.
#
# Two paths deliberately requeue a message WITHOUT counting it toward the DLQ —
# the mailbox-dlq alert (an alert-about-an-alert must never itself DLQ-loop) and a
# `mailbox_transient` handler failure (T-2038: an upstream outage is not message
# poison). Both are correct about COUNTING and both were unbounded in RATE, so
# during an upstream outage each one spins as fast as the drain loop runs. Every
# spin is a full agent turn.
#
# MEASURED TWICE, from opposite ends, by two agents who did not know they were
# looking at the same defect:
#   specialist (T-2386):  an entitlement error -32603 "Your organization does not have
#                    access to Claude" matched none of the four cap signatures, so
#                    a keep_going nudge took the poison path, quarantined on the
#                    3rd strike, and the resulting mailbox-dlq alert — exempt —
#                    retried ~60x into the seat's context in ~74 minutes. Its
#                    .attempts/ dir was EMPTY, which positively confirms the
#                    exemption: never counted, therefore never bounded.
#   archivist (T-2384): the same quarantine at her seat left one mailbox-dlq
#                    message parked in new/, so the waker read exactly 1 unread
#                    and re-kicked. 242 consecutive wakes across three days, all
#                    saying "1". Every one of them was TRUE.
#
# Widening the signature list alone would have closed neither: it is a policy list
# that was already incomplete when it was written, and a fifth wording next month
# revives the identical loop. Backoff makes the list an optimisation rather than
# the safety property — ANY unclassified environmental error is now bounded,
# including ones nobody has seen.
#
# Cleared on ack (a message that succeeds has no retry history worth keeping), and
# capped, so a message is slowed but never starved.
_BACKOFF_BASE_SECONDS = float(os.environ.get("PANTHEON_MAILBOX_BACKOFF_BASE_SECONDS", "30"))
_BACKOFF_CAP_SECONDS = float(os.environ.get("PANTHEON_MAILBOX_BACKOFF_CAP_SECONDS", "1800"))


def _backoff_path(inbox: Path, name: str) -> Path:
    return inbox / ".backoff" / name


def _record_backoff(inbox: Path, name: str, now: float | None = None) -> float:
    """Record one uncounted retry of `name`; return the epoch before which it must
    not be retried. Exponential on consecutive uncounted failures, capped.

    Fail-soft by design: if the sidecar cannot be written we return a deadline in
    the past, i.e. we degrade to the CURRENT unbounded behaviour rather than
    blocking delivery. A backoff store that fails closed would be a way to lose
    mail, which is the one outcome this layer must never produce."""
    now = time.time() if now is None else now
    path = _backoff_path(inbox, name)
    try:
        _ensure_dir(path.parent)
        try:
            failures = int(json.loads(path.read_text()).get("failures", 0))
        except (OSError, ValueError, AttributeError, json.JSONDecodeError):
            failures = 0
        failures += 1
        delay = min(_BACKOFF_BASE_SECONDS * (2 ** (failures - 1)), _BACKOFF_CAP_SECONDS)
        not_before = now + delay
        path.write_text(json.dumps({"failures": failures, "not_before": not_before}))
        return not_before
    except OSError:
        return now


def backoff_active(inbox: Path, name: str, now: float | None = None) -> bool:
    """True while `name` is inside its retry-backoff window. Unreadable/absent →
    False (retry now) — same fail-soft direction as _record_backoff.

    PUBLIC because the drain is no longer the only thing that must not act on a
    backed-off message (T-2384). `mailbox_wake` counts unread mail to decide
    whether to wake a seat, and a message the drain has decided not to claim is
    not something the seat can do anything about — waking for it burns a turn per
    re-kick TTL and delivers nothing. Both callers ask the same question, so they
    ask it of the same function rather than of two readdirs that agree by
    coincidence."""
    now = time.time() if now is None else now
    try:
        return float(json.loads(_backoff_path(inbox, name).read_text())["not_before"]) > now
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


# The pre-T-2384 private name. Kept as an alias so in-module call sites and the
# existing tests keep reading naturally; there is exactly one implementation.
_backoff_active = backoff_active


def _clear_backoff(inbox: Path, name: str) -> None:
    try:
        _backoff_path(inbox, name).unlink()
    except OSError:
        pass


def _record_dlq_failure(inbox: Path, name: str) -> int:
    """Increment + persist the failed-consumption counter for message `name`;
    return the new count. The sidecar dir is created lazily on first failure.

    ⚠️ THE REASON THIS NEEDS NO LOCKING CHANGED WITH T-2287, even though the
    conclusion did not. It used to be "the per-inbox consumer lease serializes
    drains, so only one process is ever in here". That is no longer true: the
    lease is released after the CLAIM, so two consumers can be in their handler
    loops at once. It is still safe for a different reason — claims are disjoint
    by construction (a message is renamed into exactly one claim dir under the
    lease), and this sidecar is keyed by message name, so two processes never
    touch the same path. Recording the new rationale rather than leaving the old
    one standing: a stale justification that happens to reach the right answer is
    how the next person "confirms" an assumption that no longer holds."""
    path = _dlq_attempts_path(inbox, name)
    _ensure_dir(path.parent)
    try:
        count = int(path.read_text().strip())
    except (OSError, ValueError):
        count = 0
    count += 1
    path.write_text(str(count))
    return count


def _clear_dlq_state(inbox: Path, name: str) -> None:
    try:
        _dlq_attempts_path(inbox, name).unlink()
    except OSError:
        pass  # no sidecar to clear, or already gone — harmless


# Bound the janitor's per-drain work: a healthy .attempts dir holds at most a
# handful of entries (messages mid-retry), so a cap this high never bites in
# practice; it only stops a pathological dir from making a drain O(huge).
_SIDECAR_PRUNE_SCAN_CAP = 1000


def _prune_orphan_dlq_sidecars(inbox: Path, live_names: set) -> int:
    """Remove .attempts/ sidecars whose message is no longer live (T-1992).

    ⚠️ `live_names` MUST be new/ UNION every in-flight claim, not just new/
    (T-2287). This is the DLQ interaction the ticket called the actual risk, and
    it is a silent one: a claimed message is by definition absent from new/, so a
    pruner that only looked at new/ would delete the attempt counter of every
    message currently being handled. A poison message would then reset to zero on
    each drain, never reach `dlq_max_attempts`, and retry forever — the DLQ would
    still be present, wired, and tested, and would simply never fire. Presence is
    not activation; see T-2312 for the same shape one layer up.

    A sidecar tracks a message's CONSECUTIVE failed consumptions; it is cleared
    on ack (_process_claim) or quarantine (_quarantine). But the ack/quarantine
    rename and that clear are two non-atomic statements — a process death BETWEEN
    them (a bridge restart / lease-reap SIGKILL mid-drain) leaves the message in
    cur/ or failed/ while its sidecar orphans, and nothing self-heals it (the
    message is gone from new/, never reprocessed). Over many restarts these
    accrete as dead 1-byte counters. Harmless (message names are globally unique
    by ts_ns, so a stale counter can never mis-attach to a future message) but
    unbounded cruft. This janitor runs once per drain, under the claim lease and
    AFTER the abandoned-claim reaper, so the live set it is handed is a consistent
    snapshot. Returns the count pruned. Best-effort: any error is swallowed (a
    janitor must never break the drain)."""
    attempts_dir = inbox / ".attempts"
    pruned = 0
    try:
        names = os.listdir(attempts_dir)
    except OSError:
        return 0  # no .attempts dir yet — nothing to prune
    for name in names[:_SIDECAR_PRUNE_SCAN_CAP]:
        try:
            if name not in live_names:
                (attempts_dir / name).unlink()
                pruned += 1
        except OSError:
            continue  # racing unlink / unreadable — skip, best-effort
    return pruned


def _alert_owner(inbox: Path, record: dict) -> None:
    """Send a DLQ alert back into the SAME inbox, from the reserved alert sender
    (exempt from DLQ tracking — see _maybe_quarantine). Payload is the structured
    quarantine record itself (id/sender/subject/attempts/reason) so a handler can
    act on it programmatically, not just display it. Best-effort: a lost alert is
    a degraded-but-safe outcome (the quarantine itself already happened), not
    something that should crash the drain."""
    try:
        # T-2237(a): a message was just DEAD-LETTERED. If this notice also sits
        # undrained, the loss is silent — the exact case the DLQ exists to surface.
        send(inbox, dict(record), _DLQ_ALERT_SENDER,
             subject=f"DLQ quarantine: {record['id']}", severity="urgent")
    except Exception:
        pass


def _quarantine(inbox: Path, name: str, src: Path, msg: dict | None,
                 reason: str, attempts: int) -> dict:
    failed_dir = _ensure_dir(inbox / "failed")
    os.rename(src, failed_dir / name)
    _clear_dlq_state(inbox, name)
    record = {
        "id": name,
        "sender": (msg or {}).get("sender"),
        "subject": (msg or {}).get("subject"),
        "attempts": attempts,
        "reason": reason,
    }
    _alert_owner(inbox, record)
    return record


def _maybe_quarantine(inbox: Path, name: str, src: Path, reason: str,
                       msg: dict | None, dlq_max_attempts: int) -> dict | None:
    """Count this failed consumption of `name`; quarantine (return a record) once
    `dlq_max_attempts` is reached, else return None. DLQ alert messages (identified
    by sender) are exempt so an alert-about-an-alert can never itself DLQ-loop —
    they retry forever exactly like pre-DLQ messages did."""
    if msg is not None and msg.get("sender") == _DLQ_ALERT_SENDER:
        # Still exempt from COUNTING — an alert-about-an-alert must never DLQ-loop,
        # that part was right. But it now backs off (T-2386): this is the message
        # that retried ~60x into specialist's context in 74 minutes with an EMPTY
        # .attempts/ dir, and the empty dir was the proof it could never bound
        # itself.
        _record_backoff(inbox, name)
        return None
    count = _record_dlq_failure(inbox, name)
    if count < dlq_max_attempts:
        return None
    return _quarantine(inbox, name, src, msg, reason, count)


# ------------------------------------------------------------------ claim/in-flight

def _claim_dirs(inbox: Path) -> list:
    """Every in-flight claim dir currently on disk. Best-effort: an unreadable
    .inflight/ yields [] rather than raising — but note the ASYMMETRY that buys,
    which is why this is safe to swallow: an empty list makes the reaper do
    NOTHING and makes the sidecar pruner's live set SMALLER. The first is inert;
    the second could prune a live counter. So the pruner is given new/ ∪ inflight
    only when the inflight read SUCCEEDED — see _live_names."""
    try:
        return [d for d in (inbox / _INFLIGHT_DIR).iterdir() if d.is_dir()]
    except OSError:
        return []


def _live_names(new: Path, inbox: Path) -> set | None:
    """Names of every message that still exists as work: queued in new/ or claimed
    in some .inflight/<claim>/. Returns None if the set could not be read
    COMPLETELY — an incomplete live set must never be handed to the sidecar
    pruner, because the pruner deletes everything it does not see, so a partial
    read is indistinguishable from real absence and would silently reset DLQ
    counters. "Cannot tell" is not "not there" (T-2307)."""
    names = set()
    try:
        names.update(p.name for p in new.iterdir() if p.is_file())
    except OSError:
        return None
    root = inbox / _INFLIGHT_DIR
    if not root.exists():
        return names
    try:
        claim_dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return None
    for d in claim_dirs:
        try:
            names.update(p.name for p in d.iterdir()
                         if p.is_file() and p.name != _CLAIM_OWNER_LOCK)
        except OSError:
            return None
    return names


def _claim_is_abandoned(claim_dir: Path) -> bool:
    """True only when the kernel AFFIRMS that nothing holds this claim's owner
    lock. Deliberately fails CLOSED on every other answer.

    The owner lock is an flock held for the life of the claim, so the kernel
    releases it on process death — that is the signal. `flock_holder_pid()`
    returns three things since T-2307, and the direction here is forced by
    consequence: requeueing a claim whose owner is still ALIVE delivers its
    messages a second time, silently, which is the exact failure the claim step
    was built to prevent. So an int (someone holds it) and UNKNOWN (the kernel
    could not be asked) both mean LEAVE IT ALONE; only a definite None reaps.

    The cost of failing closed is bounded and loud: an abandoned claim that we
    cannot prove is abandoned stays in .inflight/ and its messages are delayed,
    which the mail-staleness detector already surfaces. The cost of failing open
    is a duplicate delivery nobody ever sees."""
    lock = claim_dir / _CLAIM_OWNER_LOCK
    if not lock.exists():
        # No owner lock at all: the claim dir was created but the owner died
        # before flocking it, or an incomplete cleanup left the dir behind.
        # Both are definitively ownerless.
        return True
    probe = Lease(lock)
    try:
        holder = probe.flock_holder_pid()
    except OSError:
        return False  # could not ask — fail closed
    if holder is UNKNOWN:
        return False
    return holder is None


def _reap_abandoned_claims(inbox: Path, new: Path) -> dict:
    """Requeue messages from claims whose owner is provably gone, then remove the
    empty claim dir. Runs under the consumer lease, so it is serialized against
    other consumers' CLAIM steps — but NOT against their handler loops, which is
    precisely why abandonment is decided by the owner flock rather than by an
    mtime or a TTL. A live consumer in a two-hour handler is indistinguishable
    from a dead one by age; it is not indistinguishable by flock.

    Returns {"claims": n, "requeued": [names], "skipped_live": n} so the caller
    can surface it. The ticket asked for the reaper's observability to be designed
    in rather than added later: a reaper that silently does nothing is the same
    class of silent-delivery failure the whole mail-staleness effort exists to
    catch, so its counts ride out on every consume() result."""
    stats = {"claims": 0, "requeued": [], "skipped_live": 0}
    for claim_dir in _claim_dirs(inbox):
        stats["claims"] += 1
        if not _claim_is_abandoned(claim_dir):
            stats["skipped_live"] += 1
            continue
        try:
            entries = [p for p in claim_dir.iterdir()
                       if p.is_file() and p.name != _CLAIM_OWNER_LOCK]
        except OSError:
            continue
        for src in entries:
            try:
                os.rename(src, new / src.name)
                stats["requeued"].append(src.name)
            except OSError:
                pass  # leave it; the next reap retries
        try:
            (claim_dir / _CLAIM_OWNER_LOCK).unlink()
        except OSError:
            pass
        try:
            claim_dir.rmdir()
        except OSError:
            pass  # non-empty (a requeue failed) — next reap finishes it
    return stats


def _open_claim(inbox: Path) -> tuple:
    """Create a claim dir and take its owner flock. Returns (claim_dir, lease).

    The flock is taken with reap_stale=False: this lock exists only to answer
    "is the owner still alive", and a claim must never be able to SIGKILL another
    consumer's process group the way a contended consumer lease can."""
    root = _ensure_dir(inbox / _INFLIGHT_DIR)
    claim_dir = _ensure_dir(root / f"{time.time_ns()}.{os.getpid()}")
    lease = Lease(claim_dir / _CLAIM_OWNER_LOCK, label=f"mailbox-claim:{inbox}")
    lease.acquire(reap_stale=False)
    return claim_dir, lease


def _close_claim(claim_dir: Path, lease) -> None:
    """Release the owner flock and remove the claim dir if it is empty."""
    lease.release()
    try:
        (claim_dir / _CLAIM_OWNER_LOCK).unlink()
    except OSError:
        pass
    try:
        claim_dir.rmdir()
    except OSError:
        pass  # still holds messages (a requeue failed) — the reaper will finish it


def _claim(new: Path, claim_dir: Path, inbox: Path | None = None) -> list:
    """Move every message currently in new/ into `claim_dir`. Returns the claimed
    names in sorted order. readdir(new/) stays the authoritative work list — we
    never trust an event to tell us what to process. A rename that loses the race
    to another consumer's claim (ENOENT) is simply not ours.

    T-2386: a message inside its retry-backoff window is NOT claimed. The skip
    belongs here rather than after the claim — claiming and then declining would
    have to requeue, and a requeue is exactly the spin the backoff exists to stop.
    `inbox` is optional so the pre-T-2386 two-arg call still works (it simply
    claims everything, the old behaviour)."""
    claimed = []
    for name in sorted(p.name for p in new.iterdir() if p.is_file()):
        if inbox is not None and _backoff_active(inbox, name):
            continue
        try:
            os.rename(new / name, claim_dir / name)
            claimed.append(name)
        except OSError:
            continue
    return claimed


def _drain(new: Path, cur: Path, handler, *, inbox: Path, dlq_max_attempts: int,
           label: str | None = None, batch_handler=None) -> dict:
    """Process every message currently in new/ (sorted), acking by move to cur/.

    readdir(new/) is the authoritative work list — we never trust an event to tell
    us what to process. A handler that raises leaves its message in new/ for a later
    retry (at-least-once → handler must be idempotent); an unreadable file is recorded
    and left in new/ too — UNLESS this is the `dlq_max_attempts`-th RECORDED failure
    of that same message, in which case it is quarantined to failed/ and the owner
    is alerted (see _maybe_quarantine). A crash mid-handler never reaches this code
    at all, so it is never counted — only observed, completed failures count.

    This is the use_lease=False path: no claim step, so messages are handled in
    place and a failure simply leaves them where they already are. It keeps the
    pre-T-2287 semantics exactly, for callers that opt out of leasing.
    """
    live = _live_names(new, inbox)
    if live is not None:
        _prune_orphan_dlq_sidecars(inbox, live)
    names = [n for n in sorted(p.name for p in new.iterdir() if p.is_file())
             if not _backoff_active(inbox, n)]
    if batch_handler is not None:
        return _process_batch(new, cur, batch_handler, inbox=inbox,
                              dlq_max_attempts=dlq_max_attempts,
                              names=names, requeue_to=None, label=label)
    return _process(new, cur, handler, inbox=inbox,
                    dlq_max_attempts=dlq_max_attempts,
                    names=names, requeue_to=None, label=label)


def _record_ack(inbox: Path, name: str, label: str | None) -> None:
    """Append WHO acked `name`, to inbox/.acks.jsonl. BEST-EFFORT (T-2266).

    ⚠️ A MAILDIR ACK RECORDS *WHEN*, NEVER *WHO*. The rename carries a ctime and
    nothing else, and `_process` renames inside the per-message loop where
    `handler` is whatever the caller passed — a full agent turn for an ACP bridge,
    an append-to-a-list collector for a hand-run drain. So 12-14 renames in one
    second is routine for a manual drain and says NOTHING about a bridge's
    per-message cost. Until the actor is recorded, no ack-density statistic from
    these maildirs is attributable to any consumer, by any seat — which is the
    precise claim archivist could not make, and could not refute, from ctimes.

    Never able to fail an ack: the message is already in cur/ when this runs, and
    losing a bookkeeping line is a far smaller harm than turning a completed ack
    into an exception.
    """
    try:
        rec = {
            "id": name,
            "ts_ns": time.time_ns(),
            "label": label,                 # what the consumer called itself
            "pid": os.getpid(),
            "uid": os.getuid(),
        }
        with open(inbox / ".acks.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except Exception:
        pass


def _process(src_dir: Path, cur: Path, handler, *, inbox: Path,
             dlq_max_attempts: int, names: list, requeue_to,
             label: str | None = None) -> dict:
    """Run `names` (living in `src_dir`) through `handler`, acking by move to cur/.

    `requeue_to` is where a FAILED message goes so it will be retried: new/ when
    the messages were claimed into an in-flight dir, or None when `src_dir` is
    already new/ and a failure just means leaving it where it is. Splitting that
    out keeps the two call paths on one body — the DLQ counting, the transient
    opt-out and the quarantine rules must not be allowed to drift apart between
    the leased and unleased paths."""
    processed: list[str] = []
    errors: list[dict] = []
    quarantined: list[dict] = []
    already_acked: list[str] = []   # T-2279: taken by the peer drain path

    def _requeue(name: str, src: Path) -> None:
        if requeue_to is None:
            return  # already sitting in new/; nothing to move
        try:
            os.rename(src, requeue_to / name)
        except OSError as exc:
            errors.append({"id": name, "error": f"requeue-failed: {exc}"})

    for name in names:
        src = src_dir / name
        try:
            msg = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            reason = f"unreadable: {exc}"
            errors.append({"id": name, "error": reason})
            q = _maybe_quarantine(inbox, name, src, reason, None, dlq_max_attempts)
            if q is not None:
                quarantined.append(q)  # already moved to failed/ — must NOT requeue
            else:
                _requeue(name, src)
            continue  # back to new/ (or quarantined); do not silently drop
        try:
            handler(msg)
        except Exception as exc:  # handler owns its errors; we only sequence retries
            reason = f"handler: {exc}"
            errors.append({"id": name, "error": reason})
            # Transient/environmental failure opt-out (T-2038): a handler that
            # raises an exception carrying a truthy `mailbox_transient` attribute
            # is telling us this failure is NOT message poison — it is an upstream
            # outage that fails EVERY message the same way (an ACP usage-cap, a
            # wedged wrapper). Counting it toward DLQ would quarantine innocent
            # messages during the outage, and each such quarantine spawns a
            # mailbox-dlq alert that — being quarantine-exempt — then retries
            # forever (the T-2038 loop). So record the error for visibility but
            # leave the message in new/ WITHOUT counting it; it retries cleanly
            # when the environment recovers. Duck-typed (getattr, not isinstance)
            # so mailbox.py stays generic and never imports a consumer's
            # exception types.
            if getattr(exc, "mailbox_transient", False):
                # DELIBERATELY NOT BACKED OFF (T-2386). I applied backoff here
                # first and the existing suite caught a real regression, not a
                # stale expectation: test_breaker_recovers_after_cooldown asserts
                # the message delivers on the drain right after the cap resets,
                # and a backoff window makes it sit out its own recovery. A
                # transient failure ALREADY has a bound — the consumer's own
                # circuit breaker (acp_bridge cap_cooldown_secs), which defers the
                # whole drain and clears the instant the upstream does. Adding a
                # second, slower timer on top would only delay recovery while
                # bounding nothing the breaker does not already bound.
                #
                # The unbounded case is the DLQ alert (see _maybe_quarantine),
                # which is exempt from counting AND has no breaker. That is where
                # the backoff goes.
                _requeue(name, src)
                continue  # back to new/, uncounted — retry when upstream recovers
            q = _maybe_quarantine(inbox, name, src, reason, msg, dlq_max_attempts)
            if q is not None:
                quarantined.append(q)  # already moved to failed/ — must NOT requeue
            else:
                _requeue(name, src)
            continue  # back to new/ (or quarantined) for retry
        try:
            os.rename(src, cur / name)  # ack-by-move
            processed.append(name)
            _clear_dlq_state(inbox, name)
            _clear_backoff(inbox, name)  # T-2386: success ends the retry history
            _record_ack(inbox, name, label)   # T-2266: WHO acked, not just when
        except FileNotFoundError:
            # T-2279 (archivist): the message is GONE because the other drain path
            # already acked it. That is the EXPECTED outcome of concurrent drain,
            # not a failure — and concurrent drain is the feature (T-2259 exists so
            # an idle main seat can take the queue instead of waiting on a long ACP
            # turn). Recording it in errors[] produced 22 real-looking
            # "No such file or directory" lines in one iteration and made a healthy
            # race read as a broken transport.
            #
            # ⚠️ NARROW ON PURPOSE — FileNotFoundError ONLY. A permissions or IO
            # failure is a genuine ack failure and must still surface loudly; the
            # whole point of this ticket is telling the two apart, and catching
            # OSError broadly here would swallow the real one to silence the
            # benign one. Nothing is requeued: the peer has it, and requeueing
            # would resurrect a message somebody already consumed.
            already_acked.append(name)
        except OSError as exc:
            errors.append({"id": name, "error": f"ack-failed: {exc}"})
            # The handler SUCCEEDED and we could not ack, so requeueing means it
            # will run again — a duplicate. That is the correct call and it is
            # unchanged from the pre-claim behaviour, where a failed ack likewise
            # left the message in new/ to be redelivered: this transport is
            # at-least-once and handlers are required to be idempotent. The
            # alternative — dropping it because it "probably ran" — turns a
            # duplicate into a LOSS, and a lost message is the one outcome this
            # layer must never produce. Without this the message would also
            # strand in the claim dir until the owning process exited.
            _requeue(name, src)
    return {"skipped": False, "processed": processed, "errors": errors,
            "quarantined": quarantined, "already_acked": already_acked}


def _process_batch(src_dir: Path, cur: Path, batch_handler, *, inbox: Path,
                   dlq_max_attempts: int, names: list, requeue_to,
                   label: str | None = None) -> dict:
    """Deliver ALL readable `names` to `batch_handler(msgs)` as ONE call, then
    ack every message on a clean return or requeue every message on a raise.

    The batched sibling of `_process` (T-2352). It preserves at-least-once
    exactly: the ack rename happens only AFTER batch_handler returns, so a lost
    turn acks nothing and every message is redelivered (tolerated duplicate, not
    loss). One prompt, N messages — the throughput win of T-2286 — while failure
    isolation is preserved by DEGRADING, not by per-id verdicts: a batch failure
    is UNATTRIBUTABLE (any of the N could be poison), so it is NEVER counted
    toward the DLQ here. The caller (acp_bridge) owns the fallback: after a batch
    failure it re-drains this same snapshot PER-MESSAGE (the `_process` path),
    where each message is adjudicated alone and the poison one is quarantined
    against its own id. So one bad message costs one extra round trip, never a
    permanent inbox wedge — and the worst case degrades to today's per-message
    behaviour, never worse.

    An UNREADABLE file is the one thing attributable without a turn — a corrupt
    file is poison to that id alone — so it is recorded and quarantined
    individually up front (same as `_process`) rather than being allowed to fail
    the whole batch's delivery. Only the readable messages go into the one turn.
    """
    processed: list[str] = []
    errors: list[dict] = []
    quarantined: list[dict] = []
    already_acked: list[str] = []

    def _requeue(name: str, src: Path) -> None:
        if requeue_to is None:
            return  # already sitting in new/; nothing to move
        try:
            os.rename(src, requeue_to / name)
        except OSError as exc:
            errors.append({"id": name, "error": f"requeue-failed: {exc}"})

    msgs: list[dict] = []
    readable: list[str] = []
    for name in names:
        src = src_dir / name
        try:
            msg = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            reason = f"unreadable: {exc}"
            errors.append({"id": name, "error": reason})
            q = _maybe_quarantine(inbox, name, src, reason, None, dlq_max_attempts)
            if q is not None:
                quarantined.append(q)  # already moved to failed/ — must NOT requeue
            else:
                _requeue(name, src)
            continue
        msgs.append(msg)
        readable.append(name)

    if not readable:
        return {"skipped": False, "processed": processed, "errors": errors,
                "quarantined": quarantined, "already_acked": already_acked}

    try:
        batch_handler(msgs)
    except Exception as exc:  # one failure fails the batch; NO per-id attribution
        # A batch turn returns one verdict, not N, so blame cannot be pinned on
        # any single message — record the same error for every readable id and
        # requeue them ALL, WITHOUT counting toward the DLQ (that would quarantine
        # innocent messages on the strength of a poison peer). The transient
        # opt-out (T-2038) is honoured too: an environmental failure (usage cap)
        # requeues uncounted the same way — it is not batch-specific poison. The
        # caller re-drains per-message on the next pass, where attribution and
        # quarantine actually happen.
        reason = f"batch-handler: {exc}"
        for name in readable:
            errors.append({"id": name, "error": reason})
            _requeue(name, src_dir / name)
        return {"skipped": False, "processed": processed, "errors": errors,
                "quarantined": quarantined, "already_acked": already_acked}

    # Clean return -> ack ALL. Per-message ack faults (already-acked by a peer
    # drain, or an ack rename that fails) are handled exactly as `_process` does.
    for name in readable:
        src = src_dir / name
        try:
            os.rename(src, cur / name)  # ack-by-move
            processed.append(name)
            _clear_dlq_state(inbox, name)
            _clear_backoff(inbox, name)
            _record_ack(inbox, name, label)
        except FileNotFoundError:
            already_acked.append(name)  # peer drain took it (T-2279) — expected
        except OSError as exc:
            errors.append({"id": name, "error": f"ack-failed: {exc}"})
            _requeue(name, src)  # handler ran; requeue => idempotent redelivery
    return {"skipped": False, "processed": processed, "errors": errors,
            "quarantined": quarantined, "already_acked": already_acked}


def consume(inbox, handler=None, *, batch_handler=None,
            use_lease: bool = True, ttl_secs: int | None = None,
            label: str | None = None,
            dlq_max_attempts: int = _DEFAULT_DLQ_MAX_ATTEMPTS) -> dict:
    """Drain `inbox`'s new/ through `handler`, acking each by move to cur/.

    Acquires a per-inbox single-consumer lease (non-blocking). If another consumer
    holds it, returns {"skipped": True, ...} immediately rather than racing a second
    drain. Returns {"skipped", "processed": [ids], "errors": [{id,error}],
    "quarantined": [{id,sender,subject,attempts,reason}]}, plus "reaped" on the
    leased path (see below). The lease-skip return is UNCHANGED and still carries
    exactly the original four keys — callers distinguish skips by "skipped"/"reason",
    never by key count.

    THE LEASE COVERS THE CLAIM, NOT THE HANDLER (T-2287). Under the lease this
    reaps abandoned claims, prunes orphan DLQ sidecars, and renames new/ ->
    .inflight/<claim>/; then it RELEASES the lease and only then runs `handler`.
    So an agent sitting in a two-hour turn no longer holds its own mailbox shut —
    the measured symptom was 47 minutes during which a seat could not ack its own
    mail. What makes the short lease safe is the claim: the ack rename happens
    after the handler returns (deliberately — a crash mid-handler must not lose
    the message), so simply releasing the lease around the handler would leave
    the message visible in new/ and a second consumer would deliver it TWICE.
    Do not shorten the lease without the claim step.

    "reaped" is {"claims": n, "requeued": [names], "skipped_live": n} from the
    abandoned-claim reaper. It is surfaced rather than kept internal on purpose:
    the reaper is the new component that can fail silently, and a transport whose
    recovery path is invisible is the failure class this whole area exists to
    catch. A caller that logs nothing else should still log a nonzero "requeued".

    A recipient inbox that is not an already-provisioned mailbox (T-2216, see
    `exists()`) is distinguishable from an empty-but-healthy one: returns
    {"skipped": True, "reason": "inbox_missing", "processed": [], "errors": [],
    "quarantined": []} and creates NOTHING on disk — checked before any lease
    acquisition. This is deliberately a flag, not a raise: consume() is driven
    by unattended polling loops (acp_bridge, specialist_seat_server, ...) that
    already treat `skipped` as an ordinary outcome and already use a `reason`
    string for their own skip variants. The pre-existing lease-held skip dict
    is UNCHANGED (it carries no "reason" key) — callers must tell the two
    apart by the presence/absence of "reason", never by string-matching it.

    handler(msg: dict) is called once per message (msg has id/sender/sender_session/
    subject/ts/payload). Read `sender_session` with `.get()` — records written
    before T-2230 do not carry it, and absent is not the same as None-because-
    the-sender-has-no-session-identity.
    Raising signals failure → the message stays in new/ for the next consume. Returning
    normally acks it. Because ack is at-least-once, the handler MUST be idempotent.

    `batch_handler(msgs: list[dict])` (T-2352) is the MUTUALLY EXCLUSIVE alternative
    to `handler`: pass exactly one. When `batch_handler` is given, the whole claimed
    snapshot is delivered to it in ONE call — a clean return acks every message, a
    raise requeues every message WITHOUT DLQ attribution (a batch turn yields one
    verdict, not N, so blame cannot be pinned on any single message). Callers that
    want failure isolation re-drain the same snapshot PER-MESSAGE (a plain `handler`
    consume) on the next pass — that is where quarantine happens. See `_process_batch`.

    `dlq_max_attempts` (default 3): after this many RECORDED failed consumptions of
    the SAME message, it is moved to failed/ instead of retried, and an alert is
    sent into this same inbox. See the module docstring's Dead-letter quarantine
    section for the exact semantics.
    """
    if (handler is None) == (batch_handler is None):
        raise ValueError("consume() requires exactly one of handler / batch_handler")
    inbox = Path(inbox)
    if not exists(inbox):
        return {"skipped": True, "reason": "inbox_missing",
                "processed": [], "errors": [], "quarantined": []}
    new, cur = inbox / "new", inbox / "cur"

    if not use_lease:
        return _drain(new, cur, handler, batch_handler=batch_handler, inbox=inbox,
                      dlq_max_attempts=dlq_max_attempts, label=label)

    lease_kwargs = {"label": label or f"mailbox-consume:{inbox}",
                    "ttl_secs": ttl_secs if ttl_secs is not None
                    else _DEFAULT_CONSUME_TTL_SECS}
    lease = Lease(inbox / ".consumer.lock", **lease_kwargs)
    if not lease.acquire():
        return {"skipped": True, "processed": [], "errors": [], "quarantined": []}

    # ---- under the lease: reap, prune, claim. No handler runs in here. ----
    try:
        reaped = _reap_abandoned_claims(inbox, new)
        live = _live_names(new, inbox)
        if live is not None:
            _prune_orphan_dlq_sidecars(inbox, live)
        claim_dir, owner = _open_claim(inbox)
        try:
            claimed = _claim(new, claim_dir, inbox)
        except BaseException:
            _close_claim(claim_dir, owner)
            raise
    finally:
        lease.release()

    # ---- lease is RELEASED. The handler runs here, however long it takes. ----
    try:
        if batch_handler is not None:
            result = _process_batch(claim_dir, cur, batch_handler, inbox=inbox,
                                    dlq_max_attempts=dlq_max_attempts,
                                    names=claimed, requeue_to=new,
                                    label=lease_kwargs["label"])
        else:
            result = _process(claim_dir, cur, handler, inbox=inbox,
                              dlq_max_attempts=dlq_max_attempts,
                              names=claimed, requeue_to=new, label=lease_kwargs["label"])
    finally:
        _close_claim(claim_dir, owner)
    result["reaped"] = reaped
    return result


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


if __name__ == "__main__":  # pragma: no cover
    # T-2184 postscript: this module is a LIBRARY, not a CLI. Invoked as a script
    # it previously fell off the end of the file — exit 0, no output, nothing sent.
    # Specialist lost a message that way and only caught it by listing the recipient's
    # new/ and finding it empty. Exit 0 from a library invoked as a script is the
    # same defect class as the rest of that arc: a green signal from an instrument
    # that cannot fail. Fail loudly instead, and say what to do.
    import sys as _sys

    _sys.exit(
        "mailbox.py is a LIBRARY and has no CLI.\n"
        "Import it instead:\n"
        "  python3 -c \"import sys; sys.path.insert(0,'/home/claude/pantheon-config/tools'); \"\\\n"
        "            \"import mailbox; print(mailbox.send('/home/post-office/mailbox/<to>', \"\\\n"
        "            \"sender='<from>', subject='...', payload='...'))\"\n"
        "Use the ABSOLUTE tools path — a relative one silently imports Python's stdlib mailbox."
    )
