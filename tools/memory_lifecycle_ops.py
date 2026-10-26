"""ORG-058 memory correction, supersession, and verified-forgetting operations
(T-3198).

The mutation half of the ORG-058 lifecycle-controls slice (report
``2026-08-25-org-058-memory-lifecycle-plan`` §5/§6/§8/§9/§10). ``correct`` owns
the ``supersede`` and ``request-source-correction`` modes; ``forget`` owns
``remove-derived``, ``request-source-erasure``, ``expire`` and ``destroy-store``.
Every op runs against a ``memory_store.MemoryStore`` and returns a typed,
digested receipt, which is also written to ``receipts/<operation_id>.json``,
whether the op succeeded, refused, or completed only in part.

Design posture (each is structural; each has a test that goes RED without it):

* Receipts never overstate removal. A forget receipt ENUMERATES every surface
  (the payload of every version, every projection generation on disk, envelope
  lineage, event log, tombstones, every source, every declared backup, media
  residue, the receipts directory) and gives each one the §8 status
  ``verified``, ``refused``, ``retained-by-policy`` or ``unverifiable``.
  ``verified`` is reserved for what the op itself checked: an operator's
  ``media_scrubbed`` attestation is ``retained-by-policy``, never ``verified``.
  ``outcome`` and the summary fields are DERIVED from those records by one
  function each. There is no success boolean, and ``validate_*_receipt``
  recomputes everything and denies a mismatch or a missing surface kind.
* Receipts never quote content. They carry ids, digests and closed codes
  only: a correction's ``reason`` is a ``CorrectionReason`` code, its free
  text is a purgeable per-version derived payload referenced by ref + a
  SALTED digest (the salt is purged with the note, so the digest cannot
  confirm a guess afterwards), an adapter's free text appears only as
  sha256 + length, and fault evidence is an exception class + errno.
* Canonical sources are never written by memory code. The only path to a
  source is an adapter's ``request_*`` method (P1). A source's claim counts as
  ``verified`` only after THIS op re-reads the source and the bytes match the
  source's own receipt (D11: verify the provider, not the claim).
* Correction is compare-and-swap on ``(version_id, version_digest)`` inside the
  store's lock. A concurrent correction can never overwrite a newer version.
  A successor may not re-home the memory to another source object
  (``source-identity-change``), claim the source says something the live
  source does not, read through the adapter keyed by that source system
  (``source-rewrite-attempt``), or change any field outside the
  ``CORRECTABLE_FIELDS`` allowlist (``policy-field-change``): changing policy
  or access scope is not a correction.
* ``remove-derived`` appends its state event first, so the item is not
  retrievable from that instant. It then purges the payloads (and reason
  notes) of ALL versions, SUPERSEDED ones included, writes the tombstone,
  rebuilds, and deletes every non-current projection generation, so a
  rollback cannot resurrect the item. Only then does it post-verify. A head
  already in a terminal state is purged without a transition; a
  RETAINED_SOURCE_REF head goes to FORGOTTEN_DERIVED like any other derived
  head (T-4275). ``expire`` executes a scheduled purge once ``purge_after``
  has passed. Every step filters by state, so a re-run (same
  ``operation_id``, or a new op on the same target) repairs a crashed or
  incomplete forget.
* An ``operation_id`` means exactly ONE request, and a finished op is never
  re-executed. Before its first mutation an op binds its id to a request
  digest (``ops/<op_id>.request.json``; the op kind is in it). A re-run is
  checked in this order: policy resolved, digest computed, the REUSE GATE (a
  different request, mode or op kind under the id is ``operation-id-reuse``,
  as is a different reason-note text once the op stored its note), THEN the
  finished-op short-circuit (an identical re-run whose op has a success
  receipt gets that receipt back, re-validated, and nothing else happens). A
  source request is sent at most once per op and object (journaled intent +
  sent); a resume never re-sends, with ONE declared exception: a crash after
  the INTENT and before the SENT record leaves a send that may or may not have
  happened. If the fresh read already shows the requested state the resume
  verifies it. Otherwise, only an adapter that declares
  ``idempotent_request_ids is True`` (a repeated request with the same
  ``request_id`` has at most one effect) is re-sent, ONCE, with the SAME
  request_id (journal: ``source-request-resend-intent``, then
  ``source-request-sent`` with ``resend: true``). Any other case is the
  explicit STUCK surface (evidence starts ``intent-only-stuck:``; re-issue
  under a NEW operation_id once the source was checked by hand), listed by
  ``stuck_source_requests``.
* Lifecycle policy is deny-by-default and separate from ``TrustPolicy``
  (INV-1). ``MemoryEnvelope.policy_revision`` is read as the lifecycle-policy
  revision. It must resolve exactly: the registry key, the policy's own
  ``revision``, and a ``digest`` recomputed over its content must all agree,
  AND that digest must equal the ``PolicyPin`` recorded when the version was
  admitted (a revision string names a policy; the pin says which content).
  Unknown revision, unknown policy name, a mode the rule does not allow, or an
  authority mismatch is a typed refusal with no mutation.

Receipt integrity, not authenticity: ``receipt_digest`` is an UNKEYED sha256
over the canonical bytes. It detects accidental corruption and any edit that
is not re-sealed, and the validators recompute every derived field and
require every surface KIND the mode always enumerates
(``REQUIRED_SURFACE_KINDS``). But anyone can recompute an unkeyed digest, so a
receipt that validates is internally consistent, not proven to come from
this code. Authenticity needs a keyed MAC or a signature (out of scope here).

Refusals are typed RETURN values (``LifecycleRefusal`` on the receipt), never
exceptions. The one exception is an op whose ``operation_id`` is not a safe
path component: it still returns its refused receipt, but that receipt cannot
be written, because there is no safe path to write it to. A REAL fault (an
``OSError`` or any other ``Exception``) inside an op is caught and returned as
an ``incomplete`` receipt (failing surface ``unverifiable``, evidence =
exception class + errno only), so a failed attempt always leaves a receipt
for repair. Only ``SimulatedCrash`` (a BaseException, the tests' crash seam)
and real process death escape, and they write nothing.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import functools
import hashlib
import json
import os
import re
import sys
import tempfile
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Protocol

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import memory_store_teardown  # noqa: E402
from memory_envelope import (  # noqa: E402
    CANONICALIZATION_REF,
    LifecycleState,
    MemoryEnvelope,
    SchemaError,
    SourceRef,
    TrustAdjudicationReceipt,
    TrustPolicy,
    canonical_json_bytes,
    is_legal_transition,
    trust_policy_content_digest,
)
from memory_policy_evaluator import (  # noqa: E402
    Decision,
    MemoryPolicyReason,
    validate_envelope,
)
from memory_store import (  # noqa: E402
    PROJECTIONS,
    REASON_NOTE_SUFFIX,
    RETRIEVABLE_STATES,
    ForgettingRule,
    LifecycleMode,
    LifecyclePolicy,
    LifecycleRefusal,
    MemoryStore,
    PolicyPin,
    StoreIntegrityError,
    envelope_version_digest,
    is_safe_id,
    payload_digest,
    lifecycle_policy_digest,
    lifecycle_policy_problem,
    make_lifecycle_policy,
    parse_zoned_time,
    policy_pin,
)

CORRECTION_RECEIPT_SCHEMA_VERSION = "org058-correction-receipt/v1"
FORGET_RECEIPT_SCHEMA_VERSION = "org058-forget-receipt/v1"
RECLASSIFY_RECEIPT_SCHEMA_VERSION = "org058-reclassify-receipt/v1"

DISPOSITION_MIXED = "mixed"
DISPOSITION_NOT_APPLICABLE = "not-applicable"
SOURCE_STATUS_UNCHANGED = "unchanged"
# Source-surface name prefixes. The leg a source record belongs to is part of
# the digested receipt, so a validator can re-derive ``source_status``.
SOURCE_UNCHANGED_PREFIX = "source-unchanged:"
SOURCE_CORRECTION_PREFIX = "source-correction:"
SOURCE_ERASURE_PREFIX = "source-erasure:"
OFF_BOX_BACKUP_EVIDENCE = (
    "T-2178 off-box backup will capture the archive; not verifiable from here"
)
ORPHAN_LABEL = "orphan (crash residue, never admitted)"
REASON_NOTE_REF_PREFIX = "reason-note://"
RECEIPTS_EVIDENCE = "receipts carry ids, digests and closed codes only"
MEDIA_SCRUBBED_EVIDENCE = (
    "operator attests scrub-on-unlink; residue disposition governed by the storage policy; "
    "this op performed no physical scrub"
)
# The ONLY envelope fields a correction may change (an ALLOWLIST: a field added
# to MemoryEnvelope later is refused until it is listed here). Every other
# field must be identical to the prior's, or the successor is refused
# policy-field-change. canonical_source_ref and lifecycle_state are correctable
# only under their own rules (the source guard; the retained kind).
# NOT here, although they read like content, because memory_policy_evaluator
# uses them to GRANT or WIDEN access: ``subjects`` (subject-scope
# authorization in evaluate_retrieval), ``source_integrity`` and
# ``claim_trust`` (asserted integrity labels / the chosen contributing
# evidence set the inherited floor with no adjudication receipt), and
# ``effective_trust_floor`` (the purpose disposition).
CORRECTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "version_id",
        "created_at",
        "observed_at",
        "valid_from",
        "valid_to",
        "confidence_kind",
        "confidence",
        "supporting_refs",
        "contradicting_refs",
        "supersedes",
        "superseded_by",
        "last_consolidated_at",
        "payload_ref",
        "payload_digest",
        "inherited_trust_floor",
        "trust_adjudication_receipt_refs",
    }
)
_OWN_RULE_FIELDS = frozenset({"canonical_source_ref", "lifecycle_state"})

# The ONLY envelope fields a RECLASSIFY may change (T-4502; an ALLOWLIST, so a
# field added to MemoryEnvelope later is refused until it is listed here).
# These are exactly the access-granting fields CORRECTABLE_FIELDS excludes
# (memory_policy_evaluator.evaluate_retrieval: ``subjects`` at the
# subject-scope check, ``effective_trust_floor`` at the purpose disposition;
# the rest feed those floors through validate_envelope's recompute), plus the
# successor-identity fields. Every other field, content included, must be
# IDENTICAL to the prior's. Narrowing is self-serve; widening needs an operator
# ref (``widening_authority_ref``).
RECLASSIFY_SEMANTIC_FIELDS: frozenset[str] = frozenset(
    {
        "subjects",
        "source_integrity",
        "claim_trust",
        "effective_trust_floor",
        "inherited_trust_floor",
        "trust_adjudication_receipt_refs",
    }
)
_RECLASSIFY_IDENTITY_FIELDS = frozenset({"version_id", "created_at", "supersedes", "superseded_by"})
RECLASSIFIABLE_FIELDS: frozenset[str] = RECLASSIFY_SEMANTIC_FIELDS | _RECLASSIFY_IDENTITY_FIELDS
# ``operator:tg:<message id>`` or ``operator:T-<ticket>``; matched with fullmatch.
WIDENING_AUTHORITY_RE = re.compile(r"operator:(tg:[0-9]+|T-[0-9]{4,})")
DIRECTION_NARROWING, DIRECTION_WIDENING, DIRECTION_UNCHANGED = "narrowing", "widening", "unchanged"


# ---------------------------------------------------------------------------
# Closed vocabularies (str-Enum: JSON round-trips through ``.value`` and
# pytest-xdist serializes cleanly; see tools/conftest.py).
# ---------------------------------------------------------------------------
CORRECT_MODES = frozenset({LifecycleMode.SUPERSEDE, LifecycleMode.REQUEST_SOURCE_CORRECTION})
FORGET_MODES = frozenset(
    {
        LifecycleMode.REMOVE_DERIVED,
        LifecycleMode.REQUEST_SOURCE_ERASURE,
        LifecycleMode.EXPIRE,
        LifecycleMode.DESTROY_STORE,
    }
)


class SurfaceStatus(str, Enum):
    """The four per-surface statuses of report §8, verbatim."""

    VERIFIED = "verified"
    REFUSED = "refused"
    RETAINED_BY_POLICY = "retained-by-policy"
    UNVERIFIABLE = "unverifiable"


class SurfaceKind(str, Enum):
    PAYLOAD = "payload"
    PROJECTION = "projection"
    ENVELOPE_METADATA = "envelope-metadata"
    EVENT_LOG = "event-log"
    TOMBSTONE = "tombstone"
    SOURCE = "source"
    BACKUP = "backup"
    MEDIA_RESIDUE = "media-residue"
    STORE = "store"
    RECEIPTS = "receipts"


class CorrectionReason(str, Enum):
    """Closed reason codes for a correction. Free text never goes on a receipt:
    it would quote the very content a later forget removes. A caller's note
    is stored as a purgeable derived payload (``reason_note``)."""

    CONTRADICTION = "contradiction"
    SOURCE_DRIFT = "source-drift"
    SUBJECT_CORRECTION = "subject-correction"
    STALE = "stale"
    OTHER = "other"


# The surface kinds each mode ALWAYS enumerates when it is not refused: the
# single table both the ops and the validators read. A receipt missing a
# required kind is denied, so a surface cannot be dropped to improve the
# derived outcome. (Per KIND, not per record: see the digest note in the
# module docstring.)
_LOCAL_FORGET_KINDS = frozenset(
    {
        SurfaceKind.PAYLOAD,
        SurfaceKind.PROJECTION,
        SurfaceKind.ENVELOPE_METADATA,
        SurfaceKind.TOMBSTONE,
        SurfaceKind.EVENT_LOG,
        SurfaceKind.SOURCE,
        SurfaceKind.BACKUP,
        SurfaceKind.MEDIA_RESIDUE,
        SurfaceKind.RECEIPTS,
    }
)
_CORRECTION_KINDS = frozenset(
    {SurfaceKind.ENVELOPE_METADATA, SurfaceKind.PAYLOAD, SurfaceKind.PROJECTION, SurfaceKind.SOURCE}
)
REQUIRED_SURFACE_KINDS: Mapping[LifecycleMode, frozenset[SurfaceKind]] = MappingProxyType(
    {
        LifecycleMode.SUPERSEDE: _CORRECTION_KINDS,
        LifecycleMode.REQUEST_SOURCE_CORRECTION: _CORRECTION_KINDS,
        LifecycleMode.REMOVE_DERIVED: _LOCAL_FORGET_KINDS,
        LifecycleMode.EXPIRE: _LOCAL_FORGET_KINDS,
        LifecycleMode.REQUEST_SOURCE_ERASURE: _LOCAL_FORGET_KINDS,
        LifecycleMode.DESTROY_STORE: frozenset(
            {SurfaceKind.STORE, SurfaceKind.BACKUP, SurfaceKind.MEDIA_RESIDUE, SurfaceKind.RECEIPTS}
        ),
    }
)


def _missing_kinds(surfaces: Sequence[SurfaceRecord], mode: LifecycleMode) -> str | None:
    missing = REQUIRED_SURFACE_KINDS[mode] - {s.kind for s in surfaces}
    if not missing:
        return None
    return (
        f"receipt omits required surface kind(s) {sorted(k.value for k in missing)} "
        f"that {mode.value} always enumerates"
    )


class LifecycleOutcome(str, Enum):
    VERIFIED = "verified"
    VERIFIED_WITH_RETENTION = "verified-with-retention"
    INCOMPLETE = "incomplete"
    REFUSED = "refused"


# ---------------------------------------------------------------------------
# Lifecycle policy: named, revisioned, digested; deny-by-default resolution.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Refusal:
    code: LifecycleRefusal
    detail: str


def resolve_lifecycle_policy(
    revision: object, lifecycle_policies: object
) -> tuple[LifecyclePolicy | None, _Refusal | None]:
    """Deny-by-default: the policy registered under ``revision`` whose own
    revision matches and whose digest recomputes; anything else refuses. That
    proves the policy self-consistent, not that it is the policy a memory was
    admitted under: ``_resolve_pinned_policy`` adds that identity check."""
    unknown = LifecycleRefusal.UNKNOWN_LIFECYCLE_POLICY
    if not isinstance(lifecycle_policies, Mapping):
        return None, _Refusal(unknown, "no lifecycle policy registry supplied")
    if not isinstance(revision, str) or not revision:
        return None, _Refusal(unknown, "envelope names no lifecycle policy_revision")
    policy = lifecycle_policies.get(revision)
    if not isinstance(policy, LifecyclePolicy):
        return None, _Refusal(unknown, f"no lifecycle policy registered for revision {revision!r}")
    problem = lifecycle_policy_problem(policy, revision)
    if problem is not None:
        return None, _Refusal(unknown, problem)
    return policy, None


def _resolve_pinned_policy(
    store: MemoryStore, memory_id: str, version_id: str, revision: object, lifecycle_policies: object
) -> tuple[LifecyclePolicy | None, _Refusal | None]:
    """``resolve_lifecycle_policy``, then IDENTITY: the registry's policy must
    carry exactly the digest pinned when the version was admitted. Minting
    other content under the same revision string is refused."""
    policy, why = resolve_lifecycle_policy(revision, lifecycle_policies)
    if why is not None or policy is None:
        return None, why
    pinned = store.policy_pin(memory_id, version_id)
    offered = policy_pin(policy)
    if pinned is None or offered != pinned:
        return None, _Refusal(
            LifecycleRefusal.UNKNOWN_LIFECYCLE_POLICY,
            f"registry policy {offered.ref} ({offered.digest}) is not the policy pinned when "
            f"{_ref(memory_id, version_id)} was admitted ({pinned.ref + ', ' + pinned.digest if pinned else 'no pin'})",
        )
    return policy, None


def _correction_authority(
    policy: LifecyclePolicy, env: MemoryEnvelope, authority_ref: str
) -> _Refusal | None:
    required = policy.correction_policies.get(env.correction_policy)
    if required is None:
        return _Refusal(
            LifecycleRefusal.UNKNOWN_LIFECYCLE_POLICY,
            f"correction policy {env.correction_policy!r} unknown to {policy.policy_ref}@{policy.revision}",
        )
    if not authority_ref or authority_ref != required:
        return _Refusal(
            LifecycleRefusal.AUTHORITY_MISMATCH,
            f"correction policy {env.correction_policy!r} requires a different authority_ref",
        )
    return None


def _forgetting_rule(
    policy: LifecyclePolicy, env: MemoryEnvelope, mode: LifecycleMode, authority_ref: str
) -> tuple[ForgettingRule | None, _Refusal | None]:
    rule = policy.forgetting_policies.get(env.forgetting_policy)
    if rule is None:
        return None, _Refusal(
            LifecycleRefusal.UNKNOWN_LIFECYCLE_POLICY,
            f"forgetting policy {env.forgetting_policy!r} unknown to {policy.policy_ref}@{policy.revision}",
        )
    if mode not in rule.allowed_modes:
        return None, _Refusal(
            LifecycleRefusal.MODE_NOT_ALLOWED,
            f"forgetting policy {env.forgetting_policy!r} does not allow {mode.value}",
        )
    if not authority_ref or authority_ref != rule.required_authority:
        return None, _Refusal(
            LifecycleRefusal.AUTHORITY_MISMATCH,
            f"forgetting policy {env.forgetting_policy!r} requires a different authority_ref",
        )
    return rule, None


# ---------------------------------------------------------------------------
# Source adapters. Memory ops hold NO write path to a source: they can only
# ask, and a claim is believed only after the op re-reads the source itself.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SourceReceipt:
    request_id: str
    object_id: str
    before_digest: str | None
    after_digest: str | None
    issued_by: str


@dataclass(frozen=True)
class SourceOutcome:
    status: SurfaceStatus
    source_receipt: SourceReceipt | None
    detail: str = ""


class SourceAdapter(Protocol):
    """A source system. OPTIONAL attribute ``idempotent_request_ids: bool``: a
    source that sets it to exactly ``True`` promises that a repeated request
    with the SAME ``request_id`` has at most one effect. Only such a source is
    re-sent a request whose send may have been lost to a crash (once, same
    request_id); anything else (absent, False, merely truthy) is never re-sent."""

    def read_digest(self, ref: SourceRef) -> str | None:
        """Current digest of the object, or ``None`` when it is absent."""

    def request_correction(
        self, ref: SourceRef, correction: str, authority_ref: str, request_id: str
    ) -> SourceOutcome: ...

    def request_erasure(
        self, ref: SourceRef, authority_ref: str, request_id: str
    ) -> SourceOutcome: ...


def _declares_idempotent_ids(adapter: object) -> bool:
    return getattr(adapter, "idempotent_request_ids", False) is True  # strictly True, never truthy


class _FileBackedSource:
    """Objects are files under ``root``; ``object_id`` is a relative path."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, ref: SourceRef) -> Path | None:
        rel = PurePosixPath(ref.object_id)
        if not ref.object_id or rel.is_absolute() or any(p in ("", ".", "..") for p in rel.parts):
            return None
        return self.root.joinpath(*rel.parts)

    def read_digest(self, ref: SourceRef) -> str | None:
        path = self._path(ref)
        if path is None or not path.is_file():
            return None
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class ReadOnlyCanonSource(_FileBackedSource):
    """Canon such as tickets/ADRs: readable, never mutable by memory ops. Every
    correction/erasure request is refused, with no receipt; canon changes go
    through canon's own review gate."""

    def request_correction(
        self, ref: SourceRef, correction: str, authority_ref: str, request_id: str
    ) -> SourceOutcome:
        return SourceOutcome(
            SurfaceStatus.REFUSED, None, "read-only canon: correct it through its own review gate"
        )

    def request_erasure(self, ref: SourceRef, authority_ref: str, request_id: str) -> SourceOutcome:
        return SourceOutcome(
            SurfaceStatus.REFUSED, None, "read-only canon: erasure is not a memory-op request"
        )


class FileSource(_FileBackedSource):
    """A source that owns its own mutation. A correction writes a new revision
    and returns a receipt with before/after digests; an erasure deletes the
    object and returns a receipt. (It accepts any authority_ref; a real source
    applies its own policy.)"""

    # Honest claim: an erasure of an absent file is a no-op (REFUSED, nothing
    # changes) and a correction writes the same text again (same resulting
    # bytes), so a repeated request has at most one effect. Caveat: it keeps no
    # request_id memory, so this holds for a re-send of the SAME request.
    idempotent_request_ids = True

    def request_correction(
        self, ref: SourceRef, correction: str, authority_ref: str, request_id: str
    ) -> SourceOutcome:
        path = self._path(ref)
        if path is None or not path.is_file():
            return SourceOutcome(SurfaceStatus.REFUSED, None, "object absent: nothing to correct")
        before = self.read_digest(ref)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(correction.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        receipt = SourceReceipt(request_id, ref.object_id, before, self.read_digest(ref), "file-source")
        return SourceOutcome(SurfaceStatus.VERIFIED, receipt, "new revision written by the source")

    def request_erasure(self, ref: SourceRef, authority_ref: str, request_id: str) -> SourceOutcome:
        path = self._path(ref)
        if path is None or not path.is_file():
            return SourceOutcome(SurfaceStatus.REFUSED, None, "object absent: nothing to erase")
        before = self.read_digest(ref)
        path.unlink()
        receipt = SourceReceipt(request_id, ref.object_id, before, None, "file-source")
        return SourceOutcome(SurfaceStatus.VERIFIED, receipt, "object erased by the source")


# An adapter is an untrusted narrator: its free text (``detail``, a receipt's
# ``issued_by``, any "digest" it hands back, an exception's class name) could
# quote the very content being forgotten. ``detail`` reaches a receipt as its
# LENGTH only (an unsalted digest of short text is an oracle); a digest only if
# it is exactly sha256-shaped; a class name only if identifier-shaped.
_ISSUER_RE = re.compile(r"[A-Za-z0-9._:@-]{1,64}")
_DIGEST_TEXT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_CLASS_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _byte_len(value: object) -> int:
    return len(str(value).encode("utf-8", "backslashreplace"))


def _opaque(value: object) -> str:
    raw = str(value).encode("utf-8", "backslashreplace")
    return f"sha256:{hashlib.sha256(raw).hexdigest()} len={len(raw)}"


def _issuer(value: object) -> str:
    return str(value) if isinstance(value, str) and _ISSUER_RE.fullmatch(value) else _opaque(value)


def _digest_text(value: object) -> str:
    if value is None:
        return "None"
    return value if isinstance(value, str) and _DIGEST_TEXT_RE.fullmatch(value) else f"non-digest (len {_byte_len(value)})"


def _cls(exc: BaseException) -> str:
    """An exception's class name, only if identifier-shaped (a class can be
    named with any string)."""
    name = type(exc).__name__
    return name if _CLASS_NAME_RE.fullmatch(name) else "?"


def verify_source_request(
    outcome: object,
    *,
    request_id: str,
    object_id: str,
    before_digest: str | None,
    fresh_digest: str | None,
    erasure: bool,
) -> tuple[SurfaceStatus, str]:
    """D11 verification of a source's answer to one request.

    ``before_digest`` is what THIS op read before the request, and
    ``fresh_digest`` is what it read after. ``verified`` is returned ONLY if the
    source's receipt is bound to this ``request_id`` and ``object_id``, its
    ``before_digest`` equals the op's own pre-read, and the fresh read equals
    its ``after_digest`` (``None`` for an erasure; a real new revision for a
    correction). A refusal or retention claim is believed only if the source
    is in fact unchanged. Whatever the adapter claims, anything else is
    ``unverifiable``. Adapter text is never quoted: ``detail`` appears as
    sha256 + length, ``issued_by`` verbatim only if it is identifier-shaped.
    """
    if not isinstance(outcome, SourceOutcome) or not isinstance(outcome.status, SurfaceStatus):
        return SurfaceStatus.UNVERIFIABLE, "adapter returned no SourceOutcome"
    if outcome.status in (SurfaceStatus.REFUSED, SurfaceStatus.RETAINED_BY_POLICY):
        if fresh_digest != before_digest:
            return SurfaceStatus.UNVERIFIABLE, (
                f"source answered {outcome.status.value} but its digest moved "
                f"({_digest_text(before_digest)} -> {_digest_text(fresh_digest)})"
            )
        return outcome.status, (
            f"source {outcome.status.value} (detail len={_byte_len(outcome.detail)}); "
            f"bytes unchanged ({_digest_text(before_digest)})"
        )
    if outcome.status is not SurfaceStatus.VERIFIED:
        return SurfaceStatus.UNVERIFIABLE, (
            f"source answered {outcome.status.value} (detail len={_byte_len(outcome.detail)})"
        )
    r = outcome.source_receipt
    if not isinstance(r, SourceReceipt):
        return SurfaceStatus.UNVERIFIABLE, "source claimed verified without a source receipt"
    if r.request_id != request_id or r.object_id != object_id:
        return SurfaceStatus.UNVERIFIABLE, "source receipt is not bound to this request/object"
    if r.before_digest != before_digest:
        return SurfaceStatus.UNVERIFIABLE, (
            f"receipt before_digest {_digest_text(r.before_digest)} != this op's pre-request read "
            f"{_digest_text(before_digest)}"
        )
    if erasure:
        if r.after_digest is not None:
            return SurfaceStatus.UNVERIFIABLE, "erasure receipt names an after_digest"
        if fresh_digest is not None:
            return SurfaceStatus.UNVERIFIABLE, (
                f"fresh re-read: object still present ({_digest_text(fresh_digest)}) despite an erasure receipt"
            )
    else:
        if r.after_digest is None or r.after_digest == before_digest:
            return SurfaceStatus.UNVERIFIABLE, "correction receipt shows no new revision"
        if fresh_digest != r.after_digest:
            return SurfaceStatus.UNVERIFIABLE, (
                f"fresh re-read {_digest_text(fresh_digest)} != receipt after_digest {_digest_text(r.after_digest)}"
            )
    return SurfaceStatus.VERIFIED, (
        f"source receipt {request_id} issued_by {_issuer(r.issued_by)} verified by fresh re-read: "
        f"{_digest_text(before_digest)} -> {_digest_text(fresh_digest)}"
    )


# ---------------------------------------------------------------------------
# Receipts: list-of-records, derived outcome/summaries, exclusive-preimage
# digest (same discipline as memory_envelope.compute_receipt_digest).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SurfaceRecord:
    surface: str
    kind: SurfaceKind
    status: SurfaceStatus
    evidence: str


@dataclass(frozen=True)
class IndexPurgeReceipt:
    projection: str
    generation_before: str | None
    generation_after: str | None
    target_refs: tuple[str, ...]
    negative_lookup_verified: bool
    evidence: str


@dataclass(frozen=True)
class CorrectionReceipt:
    # report §6, verbatim
    operation_id: str
    target_memory_id: str
    prior_version_digest: str | None
    successor_version_id: str | None
    source_status: str
    reason: CorrectionReason | None  # a closed code; None only on a refusal of a non-code
    actor: str
    authority_ref: str
    index_purge_receipts: tuple[IndexPurgeReceipt, ...]
    completed_at: str
    # T-3198 additions
    surfaces: tuple[SurfaceRecord, ...]
    lifecycle_policy_pins: tuple[PolicyPin, ...]
    reason_note_ref: str | None
    reason_note_digest: str | None
    outcome: LifecycleOutcome
    refusal: LifecycleRefusal | None
    refusal_detail: str
    receipt_schema_version: str
    canonicalization_ref: str
    receipt_digest: str


@dataclass(frozen=True)
class ForgetReceipt:
    # report §6, verbatim
    operation_id: str
    subject_scope: tuple[str, ...]
    target_ids: tuple[str, ...]
    mode: str
    source_disposition: str
    derived_payload_disposition: str
    projection_purge_receipts: tuple[IndexPurgeReceipt, ...]
    backup_disposition: str
    unverifiable_surfaces: tuple[str, ...]
    authority_ref: str
    completed_at: str
    # T-3198 additions
    surfaces: tuple[SurfaceRecord, ...]
    lifecycle_policy_pins: tuple[PolicyPin, ...]
    outcome: LifecycleOutcome
    refusal: LifecycleRefusal | None
    refusal_detail: str
    receipt_schema_version: str
    canonicalization_ref: str
    receipt_digest: str


def derive_outcome(
    surfaces: Sequence[SurfaceRecord], refusal: LifecycleRefusal | None
) -> LifecycleOutcome:
    """The ONLY producer of ``outcome``. A refused op is ``refused``. Otherwise
    ``verified`` iff every surface is verified, and ``verified-with-retention``
    iff every surface is verified or retained-by-policy. Anything else,
    including an empty enumeration (nothing proven), is ``incomplete``."""
    if refusal is not None:
        return LifecycleOutcome.REFUSED
    statuses = {s.status for s in surfaces}
    if not statuses:
        return LifecycleOutcome.INCOMPLETE
    if statuses == {SurfaceStatus.VERIFIED}:
        return LifecycleOutcome.VERIFIED
    if statuses <= {SurfaceStatus.VERIFIED, SurfaceStatus.RETAINED_BY_POLICY}:
        return LifecycleOutcome.VERIFIED_WITH_RETENTION
    return LifecycleOutcome.INCOMPLETE


def derive_disposition(surfaces: Sequence[SurfaceRecord], kind: SurfaceKind) -> str:
    """Summary over one kind: the single common status, else ``mixed``, else
    ``not-applicable`` when no surface of that kind exists."""
    statuses = {s.status for s in surfaces if s.kind == kind}
    if not statuses:
        return DISPOSITION_NOT_APPLICABLE
    if len(statuses) == 1:
        return next(iter(statuses)).value
    return DISPOSITION_MIXED


def derive_source_status(surfaces: Sequence[SurfaceRecord]) -> str:
    """``unchanged`` when every source record is a verified unchanged-proof
    leg (no source request was made); otherwise the source disposition. It
    derives both ``CorrectionReceipt.source_status`` and
    ``ForgetReceipt.source_disposition``, so a deliberately untouched source
    reads the same on both."""
    source = [s for s in surfaces if s.kind == SurfaceKind.SOURCE]
    if source and all(
        s.surface.startswith(SOURCE_UNCHANGED_PREFIX) and s.status == SurfaceStatus.VERIFIED
        for s in source
    ):
        return SOURCE_STATUS_UNCHANGED
    return derive_disposition(surfaces, SurfaceKind.SOURCE)


def unverifiable_surface_names(surfaces: Sequence[SurfaceRecord]) -> tuple[str, ...]:
    return tuple(s.surface for s in surfaces if s.status == SurfaceStatus.UNVERIFIABLE)


def _v(x: object) -> object:
    return x.value if isinstance(x, Enum) else x


def _surface_to_json(s: SurfaceRecord) -> dict[str, object]:
    return {"surface": s.surface, "kind": _v(s.kind), "status": _v(s.status), "evidence": s.evidence}


def _pin_to_json(p: PolicyPin) -> dict[str, object]:
    return {"ref": p.ref, "digest": p.digest}


def _ipr_to_json(p: IndexPurgeReceipt) -> dict[str, object]:
    return {
        "projection": p.projection,
        "generation_before": p.generation_before,
        "generation_after": p.generation_after,
        "target_refs": list(p.target_refs),
        "negative_lookup_verified": p.negative_lookup_verified,
        "evidence": p.evidence,
    }


def correction_receipt_to_json(
    r: CorrectionReceipt, *, include_digest: bool = True
) -> dict[str, object]:
    """``include_digest=False`` omits ``receipt_digest``: the digest preimage."""
    body: dict[str, object] = {
        "receipt_schema_version": r.receipt_schema_version,
        "canonicalization_ref": r.canonicalization_ref,
        "operation_id": r.operation_id,
        "target_memory_id": r.target_memory_id,
        "prior_version_digest": r.prior_version_digest,
        "successor_version_id": r.successor_version_id,
        "source_status": r.source_status,
        "reason": _v(r.reason),
        "actor": r.actor,
        "authority_ref": r.authority_ref,
        "index_purge_receipts": [_ipr_to_json(p) for p in r.index_purge_receipts],
        "completed_at": r.completed_at,
        "surfaces": [_surface_to_json(s) for s in r.surfaces],
        "lifecycle_policy_pins": [_pin_to_json(p) for p in r.lifecycle_policy_pins],
        "reason_note_ref": r.reason_note_ref,
        "reason_note_digest": r.reason_note_digest,
        "outcome": _v(r.outcome),
        "refusal": _v(r.refusal),
        "refusal_detail": r.refusal_detail,
    }
    if include_digest:
        body["receipt_digest"] = r.receipt_digest
    return body


def forget_receipt_to_json(r: ForgetReceipt, *, include_digest: bool = True) -> dict[str, object]:
    """``include_digest=False`` omits ``receipt_digest``: the digest preimage."""
    body: dict[str, object] = {
        "receipt_schema_version": r.receipt_schema_version,
        "canonicalization_ref": r.canonicalization_ref,
        "operation_id": r.operation_id,
        "subject_scope": list(r.subject_scope),
        "target_ids": list(r.target_ids),
        "mode": r.mode,
        "source_disposition": r.source_disposition,
        "derived_payload_disposition": r.derived_payload_disposition,
        "projection_purge_receipts": [_ipr_to_json(p) for p in r.projection_purge_receipts],
        "backup_disposition": r.backup_disposition,
        "unverifiable_surfaces": list(r.unverifiable_surfaces),
        "authority_ref": r.authority_ref,
        "completed_at": r.completed_at,
        "surfaces": [_surface_to_json(s) for s in r.surfaces],
        "lifecycle_policy_pins": [_pin_to_json(p) for p in r.lifecycle_policy_pins],
        "outcome": _v(r.outcome),
        "refusal": _v(r.refusal),
        "refusal_detail": r.refusal_detail,
    }
    if include_digest:
        body["receipt_digest"] = r.receipt_digest
    return body


def _digest(body: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def compute_correction_receipt_digest(r: CorrectionReceipt) -> str:
    return _digest(correction_receipt_to_json(r, include_digest=False))


def compute_forget_receipt_digest(r: ForgetReceipt) -> str:
    return _digest(forget_receipt_to_json(r, include_digest=False))


# --- from_json: fail closed by raising SchemaError on structural malformation,
# including any key the record does not define (an extra "success": true must
# never ride along on a receipt that then validates).
def _closed(m: Mapping[str, object], cls: type, label: str) -> None:
    extra = set(m) - {f.name for f in dataclasses.fields(cls)}
    if extra:
        raise SchemaError(f"{label}: unknown key(s) {sorted(str(k) for k in extra)}")


def _req(m: Mapping[str, object], key: str) -> object:
    if key not in m:
        raise SchemaError(f"missing required key: {key}")
    return m[key]


def _str(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{key} must be a string")
    return value


def _opt_str(value: object, key: str) -> str | None:
    return None if value is None else _str(value, key)


def _list(value: object, key: str) -> list:
    if not isinstance(value, list):
        raise SchemaError(f"{key} must be a list")
    return value


def _enum(enum_cls: type, value: object, key: str):
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise SchemaError(f"{key}: invalid {enum_cls.__name__} {value!r}") from exc


def _surface_from_json(m: object) -> SurfaceRecord:
    if not isinstance(m, Mapping):
        raise SchemaError("surface record must be an object")
    _closed(m, SurfaceRecord, "surface record")
    return SurfaceRecord(
        surface=_str(_req(m, "surface"), "surface"),
        kind=_enum(SurfaceKind, _req(m, "kind"), "kind"),
        status=_enum(SurfaceStatus, _req(m, "status"), "status"),
        evidence=_str(_req(m, "evidence"), "evidence"),
    )


def _ipr_from_json(m: object) -> IndexPurgeReceipt:
    if not isinstance(m, Mapping):
        raise SchemaError("index purge receipt must be an object")
    _closed(m, IndexPurgeReceipt, "index purge receipt")
    neg = _req(m, "negative_lookup_verified")
    if not isinstance(neg, bool):
        raise SchemaError("negative_lookup_verified must be a bool")
    return IndexPurgeReceipt(
        projection=_str(_req(m, "projection"), "projection"),
        generation_before=_opt_str(_req(m, "generation_before"), "generation_before"),
        generation_after=_opt_str(_req(m, "generation_after"), "generation_after"),
        target_refs=tuple(_str(x, "target_refs[]") for x in _list(_req(m, "target_refs"), "target_refs")),
        negative_lookup_verified=neg,
        evidence=_str(_req(m, "evidence"), "evidence"),
    )


def _pin_from_json(m: object) -> PolicyPin:
    if not isinstance(m, Mapping):
        raise SchemaError("policy pin must be an object")
    _closed(m, PolicyPin, "policy pin")
    return PolicyPin(ref=_str(_req(m, "ref"), "ref"), digest=_str(_req(m, "digest"), "digest"))


def _refusal_from_json(value: object) -> LifecycleRefusal | None:
    return None if value is None else _enum(LifecycleRefusal, value, "refusal")


def correction_receipt_from_json(m: Mapping[str, object]) -> CorrectionReceipt:
    if not isinstance(m, Mapping):
        raise SchemaError("correction receipt must be an object")
    _closed(m, CorrectionReceipt, "correction receipt")
    return CorrectionReceipt(
        operation_id=_str(_req(m, "operation_id"), "operation_id"),
        target_memory_id=_str(_req(m, "target_memory_id"), "target_memory_id"),
        prior_version_digest=_opt_str(_req(m, "prior_version_digest"), "prior_version_digest"),
        successor_version_id=_opt_str(_req(m, "successor_version_id"), "successor_version_id"),
        source_status=_str(_req(m, "source_status"), "source_status"),
        reason=None if _req(m, "reason") is None else _enum(CorrectionReason, m["reason"], "reason"),
        actor=_str(_req(m, "actor"), "actor"),
        authority_ref=_str(_req(m, "authority_ref"), "authority_ref"),
        index_purge_receipts=tuple(
            _ipr_from_json(x) for x in _list(_req(m, "index_purge_receipts"), "index_purge_receipts")
        ),
        completed_at=_str(_req(m, "completed_at"), "completed_at"),
        surfaces=tuple(_surface_from_json(x) for x in _list(_req(m, "surfaces"), "surfaces")),
        lifecycle_policy_pins=tuple(
            _pin_from_json(x) for x in _list(_req(m, "lifecycle_policy_pins"), "lifecycle_policy_pins")
        ),
        reason_note_ref=_opt_str(_req(m, "reason_note_ref"), "reason_note_ref"),
        reason_note_digest=_opt_str(_req(m, "reason_note_digest"), "reason_note_digest"),
        outcome=_enum(LifecycleOutcome, _req(m, "outcome"), "outcome"),
        refusal=_refusal_from_json(_req(m, "refusal")),
        refusal_detail=_str(_req(m, "refusal_detail"), "refusal_detail"),
        receipt_schema_version=_str(_req(m, "receipt_schema_version"), "receipt_schema_version"),
        canonicalization_ref=_str(_req(m, "canonicalization_ref"), "canonicalization_ref"),
        receipt_digest=_str(_req(m, "receipt_digest"), "receipt_digest"),
    )


def forget_receipt_from_json(m: Mapping[str, object]) -> ForgetReceipt:
    if not isinstance(m, Mapping):
        raise SchemaError("forget receipt must be an object")
    _closed(m, ForgetReceipt, "forget receipt")
    return ForgetReceipt(
        operation_id=_str(_req(m, "operation_id"), "operation_id"),
        subject_scope=tuple(_str(x, "subject_scope[]") for x in _list(_req(m, "subject_scope"), "subject_scope")),
        target_ids=tuple(_str(x, "target_ids[]") for x in _list(_req(m, "target_ids"), "target_ids")),
        mode=_str(_req(m, "mode"), "mode"),
        source_disposition=_str(_req(m, "source_disposition"), "source_disposition"),
        derived_payload_disposition=_str(
            _req(m, "derived_payload_disposition"), "derived_payload_disposition"
        ),
        projection_purge_receipts=tuple(
            _ipr_from_json(x)
            for x in _list(_req(m, "projection_purge_receipts"), "projection_purge_receipts")
        ),
        backup_disposition=_str(_req(m, "backup_disposition"), "backup_disposition"),
        unverifiable_surfaces=tuple(
            _str(x, "unverifiable_surfaces[]")
            for x in _list(_req(m, "unverifiable_surfaces"), "unverifiable_surfaces")
        ),
        authority_ref=_str(_req(m, "authority_ref"), "authority_ref"),
        completed_at=_str(_req(m, "completed_at"), "completed_at"),
        surfaces=tuple(_surface_from_json(x) for x in _list(_req(m, "surfaces"), "surfaces")),
        lifecycle_policy_pins=tuple(
            _pin_from_json(x) for x in _list(_req(m, "lifecycle_policy_pins"), "lifecycle_policy_pins")
        ),
        outcome=_enum(LifecycleOutcome, _req(m, "outcome"), "outcome"),
        refusal=_refusal_from_json(_req(m, "refusal")),
        refusal_detail=_str(_req(m, "refusal_detail"), "refusal_detail"),
        receipt_schema_version=_str(_req(m, "receipt_schema_version"), "receipt_schema_version"),
        canonicalization_ref=_str(_req(m, "canonicalization_ref"), "canonicalization_ref"),
        receipt_digest=_str(_req(m, "receipt_digest"), "receipt_digest"),
    )


# --- validators: recompute everything, deny any mismatch.
def _deny(reason: MemoryPolicyReason, detail: str) -> Decision:
    return Decision(False, reason, detail)


def _shape_problem(
    surfaces: object, iprs: object, outcome: object, refusal: object, pins: object
) -> str | None:
    if not isinstance(surfaces, tuple) or any(
        not isinstance(s, SurfaceRecord)
        or not isinstance(s.kind, SurfaceKind)
        or not isinstance(s.status, SurfaceStatus)
        for s in surfaces
    ):
        return "surfaces must be SurfaceRecords with closed kind/status"
    if not isinstance(pins, tuple) or any(
        not isinstance(p, PolicyPin) or not isinstance(p.ref, str) or not isinstance(p.digest, str) for p in pins
    ):
        return "lifecycle_policy_pins must be PolicyPin records"
    # A receipt that CLAIMS a removal must say which policy content authorized
    # it; an incomplete one (e.g. a fault before any policy resolved) claims none.
    success = (LifecycleOutcome.VERIFIED, LifecycleOutcome.VERIFIED_WITH_RETENTION)
    if refusal is None and not pins and outcome in success:
        return "a receipt claiming verified removal must name the lifecycle policy it acted under"
    if not isinstance(iprs, tuple) or any(
        not isinstance(p, IndexPurgeReceipt) or not isinstance(p.negative_lookup_verified, bool)
        for p in iprs
    ):
        return "purge receipts must be IndexPurgeReceipts"
    if not isinstance(outcome, LifecycleOutcome):
        return "outcome is not a LifecycleOutcome"
    if refusal is not None and not isinstance(refusal, LifecycleRefusal):
        return "refusal is not a LifecycleRefusal"
    return None


def _outcome_problem(
    surfaces: tuple[SurfaceRecord, ...],
    iprs: tuple[IndexPurgeReceipt, ...],
    outcome: LifecycleOutcome,
    refusal: LifecycleRefusal | None,
) -> str | None:
    derived = derive_outcome(surfaces, refusal)
    if outcome != derived:
        return f"outcome {outcome.value!r} is not the outcome its surfaces derive ({derived.value!r})"
    # A purge receipt may not disagree with the surfaces the outcome derives
    # from: a failed negative lookup must show up as a non-verified projection.
    if any(not p.negative_lookup_verified for p in iprs) and all(
        s.status == SurfaceStatus.VERIFIED for s in surfaces if s.kind == SurfaceKind.PROJECTION
    ):
        return "a purge receipt reports a failed negative lookup that no projection surface records"
    return None


def _reason_problem(r: CorrectionReceipt) -> str | None:
    """``reason`` is a closed code (``None`` only when the op refused a non-code),
    and the note is referenced by ref + digest only, never quoted."""
    if r.reason is not None and not isinstance(r.reason, CorrectionReason):
        return "reason is not a CorrectionReason code"
    if r.reason is None and r.refusal is None:
        return "an op that was not refused must carry a CorrectionReason code"
    ref, digest = r.reason_note_ref, r.reason_note_digest
    if (ref is None) != (digest is None):
        return "reason_note_ref and reason_note_digest come as a pair"
    if ref is not None and not (
        isinstance(ref, str)
        and ref.startswith(REASON_NOTE_REF_PREFIX)
        and isinstance(digest, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    ):
        return "reason note must be a reason-note:// ref and a sha256 digest"
    return None


def validate_correction_receipt(r: object) -> Decision:
    if not isinstance(r, CorrectionReceipt):
        return _deny(MemoryPolicyReason.MALFORMED_INPUT, "not a CorrectionReceipt")
    if (
        r.receipt_schema_version != CORRECTION_RECEIPT_SCHEMA_VERSION
        or r.canonicalization_ref != CANONICALIZATION_REF
    ):
        return _deny(MemoryPolicyReason.RECEIPT_WRONG_CANONICALIZATION, "schema/canonicalization")
    problem = _shape_problem(r.surfaces, r.index_purge_receipts, r.outcome, r.refusal, r.lifecycle_policy_pins)
    if problem is None:
        problem = _outcome_problem(r.surfaces, r.index_purge_receipts, r.outcome, r.refusal)
    if problem is None and r.source_status != derive_source_status(r.surfaces):
        problem = f"source_status {r.source_status!r} != derived {derive_source_status(r.surfaces)!r}"
    if problem is None:
        problem = _reason_problem(r)
    if problem is None and r.refusal is None:
        problem = _missing_kinds(r.surfaces, LifecycleMode.SUPERSEDE)
    if problem is not None:
        return _deny(MemoryPolicyReason.MALFORMED_INPUT, problem)
    if r.receipt_digest != compute_correction_receipt_digest(r):
        return _deny(MemoryPolicyReason.RECEIPT_DIGEST_MISMATCH, "receipt_digest does not recompute")
    return Decision(True, MemoryPolicyReason.AUTHORIZED, "correction receipt recomputes")


def validate_forget_receipt(r: object) -> Decision:
    if not isinstance(r, ForgetReceipt):
        return _deny(MemoryPolicyReason.MALFORMED_INPUT, "not a ForgetReceipt")
    if (
        r.receipt_schema_version != FORGET_RECEIPT_SCHEMA_VERSION
        or r.canonicalization_ref != CANONICALIZATION_REF
    ):
        return _deny(MemoryPolicyReason.RECEIPT_WRONG_CANONICALIZATION, "schema/canonicalization")
    problem = _shape_problem(r.surfaces, r.projection_purge_receipts, r.outcome, r.refusal, r.lifecycle_policy_pins)
    if problem is None:
        problem = _outcome_problem(r.surfaces, r.projection_purge_receipts, r.outcome, r.refusal)
    if problem is None:
        for field_name, derived in (
            ("source_disposition", derive_source_status(r.surfaces)),
            ("derived_payload_disposition", derive_disposition(r.surfaces, SurfaceKind.PAYLOAD)),
            ("backup_disposition", derive_disposition(r.surfaces, SurfaceKind.BACKUP)),
        ):
            if getattr(r, field_name) != derived:
                problem = f"{field_name} {getattr(r, field_name)!r} != derived {derived!r}"
                break
    if problem is None and r.unverifiable_surfaces != unverifiable_surface_names(r.surfaces):
        problem = "unverifiable_surfaces does not list exactly the unverifiable surfaces"
    if problem is None and r.refusal is None and r.mode not in {m.value for m in FORGET_MODES}:
        problem = f"mode {r.mode!r} is not a forget-owned mode"
    if problem is None and r.refusal is None:
        problem = _missing_kinds(r.surfaces, LifecycleMode(r.mode))
    if problem is not None:
        return _deny(MemoryPolicyReason.MALFORMED_INPUT, problem)
    if r.receipt_digest != compute_forget_receipt_digest(r):
        return _deny(MemoryPolicyReason.RECEIPT_DIGEST_MISMATCH, "receipt_digest does not recompute")
    return Decision(True, MemoryPolicyReason.AUTHORIZED, "forget receipt recomputes")


def _correction_receipt(
    *,
    operation_id: str,
    target_memory_id: str,
    prior_version_digest: str | None,
    successor_version_id: str | None,
    reason: CorrectionReason | None,
    actor: str,
    authority_ref: str,
    index_purge_receipts: Sequence[IndexPurgeReceipt],
    completed_at: str,
    surfaces: Sequence[SurfaceRecord],
    lifecycle_policy_pins: Sequence[PolicyPin],
    reason_note_ref: str | None,
    reason_note_digest: str | None,
    refusal: LifecycleRefusal | None,
    refusal_detail: str,
) -> CorrectionReceipt:
    surfaces = tuple(surfaces)
    r = CorrectionReceipt(
        operation_id=operation_id,
        target_memory_id=target_memory_id,
        prior_version_digest=prior_version_digest,
        successor_version_id=successor_version_id,
        source_status=derive_source_status(surfaces),
        reason=reason,
        actor=actor,
        authority_ref=authority_ref,
        index_purge_receipts=tuple(index_purge_receipts),
        completed_at=completed_at,
        surfaces=surfaces,
        lifecycle_policy_pins=tuple(sorted(set(lifecycle_policy_pins), key=lambda p: (p.ref, p.digest))),
        reason_note_ref=reason_note_ref,
        reason_note_digest=reason_note_digest,
        outcome=derive_outcome(surfaces, refusal),
        refusal=refusal,
        refusal_detail=refusal_detail,
        receipt_schema_version=CORRECTION_RECEIPT_SCHEMA_VERSION,
        canonicalization_ref=CANONICALIZATION_REF,
        receipt_digest="",
    )
    return dataclasses.replace(r, receipt_digest=compute_correction_receipt_digest(r))


def _forget_receipt(
    *,
    operation_id: str,
    subject_scope: tuple[str, ...],
    target_ids: tuple[str, ...],
    mode: str,
    projection_purge_receipts: Sequence[IndexPurgeReceipt],
    authority_ref: str,
    completed_at: str,
    surfaces: Sequence[SurfaceRecord],
    lifecycle_policy_pins: Sequence[PolicyPin],
    refusal: LifecycleRefusal | None,
    refusal_detail: str,
) -> ForgetReceipt:
    surfaces = tuple(surfaces)
    r = ForgetReceipt(
        operation_id=operation_id,
        subject_scope=subject_scope,
        target_ids=target_ids,
        mode=mode,
        source_disposition=derive_source_status(surfaces),
        derived_payload_disposition=derive_disposition(surfaces, SurfaceKind.PAYLOAD),
        projection_purge_receipts=tuple(projection_purge_receipts),
        backup_disposition=derive_disposition(surfaces, SurfaceKind.BACKUP),
        unverifiable_surfaces=unverifiable_surface_names(surfaces),
        authority_ref=authority_ref,
        completed_at=completed_at,
        surfaces=surfaces,
        lifecycle_policy_pins=tuple(sorted(set(lifecycle_policy_pins), key=lambda p: (p.ref, p.digest))),
        outcome=derive_outcome(surfaces, refusal),
        refusal=refusal,
        refusal_detail=refusal_detail,
        receipt_schema_version=FORGET_RECEIPT_SCHEMA_VERSION,
        canonicalization_ref=CANONICALIZATION_REF,
        receipt_digest="",
    )
    return dataclasses.replace(r, receipt_digest=compute_forget_receipt_digest(r))


# ---------------------------------------------------------------------------
# ReclassifyReceipt (T-4502): the typed receipt of reclassify(). It records
# the direction and the NAMES of the changed fields, never their values
# (subjects can be personal), plus the projection evidence.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReclassifyReceipt:
    operation_id: str
    memory_id: str
    prior_version_id: str | None
    prior_version_digest: str | None
    successor_version_id: str | None
    successor_version_digest: str | None
    direction: str | None  # narrowing | widening; None only when refused before it was derived
    changed_fields: tuple[str, ...]  # field NAMES only
    actor: str
    authority_ref: str
    widening_authority_ref: str | None  # carried only by a widening
    index_purge_receipts: tuple[IndexPurgeReceipt, ...]
    completed_at: str
    surfaces: tuple[SurfaceRecord, ...]
    lifecycle_policy_pins: tuple[PolicyPin, ...]
    outcome: LifecycleOutcome
    refusal: LifecycleRefusal | None
    refusal_detail: str
    receipt_schema_version: str
    canonicalization_ref: str
    receipt_digest: str


# Success enumerates these kinds. No SOURCE: reclassify never touches a source.
_RECLASSIFY_KINDS = frozenset({SurfaceKind.ENVELOPE_METADATA, SurfaceKind.PAYLOAD, SurfaceKind.PROJECTION})


def reclassify_receipt_to_json(r: ReclassifyReceipt, *, include_digest: bool = True) -> dict[str, object]:
    """``include_digest=False`` omits ``receipt_digest``: the digest preimage."""
    body: dict[str, object] = {
        "receipt_schema_version": r.receipt_schema_version,
        "canonicalization_ref": r.canonicalization_ref,
        "operation_id": r.operation_id,
        "memory_id": r.memory_id,
        "prior_version_id": r.prior_version_id,
        "prior_version_digest": r.prior_version_digest,
        "successor_version_id": r.successor_version_id,
        "successor_version_digest": r.successor_version_digest,
        "direction": r.direction,
        "changed_fields": list(r.changed_fields),
        "actor": r.actor,
        "authority_ref": r.authority_ref,
        "widening_authority_ref": r.widening_authority_ref,
        "index_purge_receipts": [_ipr_to_json(p) for p in r.index_purge_receipts],
        "completed_at": r.completed_at,
        "surfaces": [_surface_to_json(s) for s in r.surfaces],
        "lifecycle_policy_pins": [_pin_to_json(p) for p in r.lifecycle_policy_pins],
        "outcome": _v(r.outcome),
        "refusal": _v(r.refusal),
        "refusal_detail": r.refusal_detail,
    }
    if include_digest:
        body["receipt_digest"] = r.receipt_digest
    return body


def compute_reclassify_receipt_digest(r: ReclassifyReceipt) -> str:
    return _digest(reclassify_receipt_to_json(r, include_digest=False))


def reclassify_receipt_from_json(m: Mapping[str, object]) -> ReclassifyReceipt:
    if not isinstance(m, Mapping):
        raise SchemaError("reclassify receipt must be an object")
    _closed(m, ReclassifyReceipt, "reclassify receipt")
    return ReclassifyReceipt(
        operation_id=_str(_req(m, "operation_id"), "operation_id"),
        memory_id=_str(_req(m, "memory_id"), "memory_id"),
        prior_version_id=_opt_str(_req(m, "prior_version_id"), "prior_version_id"),
        prior_version_digest=_opt_str(_req(m, "prior_version_digest"), "prior_version_digest"),
        successor_version_id=_opt_str(_req(m, "successor_version_id"), "successor_version_id"),
        successor_version_digest=_opt_str(_req(m, "successor_version_digest"), "successor_version_digest"),
        direction=_opt_str(_req(m, "direction"), "direction"),
        changed_fields=tuple(_str(x, "changed_fields[]") for x in _list(_req(m, "changed_fields"), "changed_fields")),
        actor=_str(_req(m, "actor"), "actor"),
        authority_ref=_str(_req(m, "authority_ref"), "authority_ref"),
        widening_authority_ref=_opt_str(_req(m, "widening_authority_ref"), "widening_authority_ref"),
        index_purge_receipts=tuple(
            _ipr_from_json(x) for x in _list(_req(m, "index_purge_receipts"), "index_purge_receipts")
        ),
        completed_at=_str(_req(m, "completed_at"), "completed_at"),
        surfaces=tuple(_surface_from_json(x) for x in _list(_req(m, "surfaces"), "surfaces")),
        lifecycle_policy_pins=tuple(
            _pin_from_json(x) for x in _list(_req(m, "lifecycle_policy_pins"), "lifecycle_policy_pins")
        ),
        outcome=_enum(LifecycleOutcome, _req(m, "outcome"), "outcome"),
        refusal=_refusal_from_json(_req(m, "refusal")),
        refusal_detail=_str(_req(m, "refusal_detail"), "refusal_detail"),
        receipt_schema_version=_str(_req(m, "receipt_schema_version"), "receipt_schema_version"),
        canonicalization_ref=_str(_req(m, "canonicalization_ref"), "canonicalization_ref"),
        receipt_digest=_str(_req(m, "receipt_digest"), "receipt_digest"),
    )


def _reclassify_problem(r: ReclassifyReceipt) -> str | None:
    if r.direction not in (None, DIRECTION_NARROWING, DIRECTION_WIDENING):
        return "direction is not narrowing or widening"
    if (
        not isinstance(r.changed_fields, tuple)
        or any(not isinstance(f, str) for f in r.changed_fields)
        or list(r.changed_fields) != sorted(set(r.changed_fields))
        or not set(r.changed_fields) <= RECLASSIFY_SEMANTIC_FIELDS
    ):
        return "changed_fields must be a sorted set of reclassifiable field NAMES"
    ref = r.widening_authority_ref
    if ref is not None and not isinstance(ref, str):
        return "widening_authority_ref must be a string or None"
    if ref is not None and r.direction != DIRECTION_WIDENING:
        return "only a widening carries a widening_authority_ref"
    if r.refusal is LifecycleRefusal.WIDENING_UNAUTHORIZED and r.direction != DIRECTION_WIDENING:
        return "widening-unauthorized is a refusal of a widening"
    if r.refusal is None and r.outcome in (LifecycleOutcome.VERIFIED, LifecycleOutcome.VERIFIED_WITH_RETENTION):
        if r.direction is None or not r.changed_fields:
            return "a successful reclassify names its direction and the fields it changed"
        if r.direction == DIRECTION_WIDENING and not (
            isinstance(ref, str) and WIDENING_AUTHORITY_RE.fullmatch(ref)
        ):
            return "a successful widening must carry an operator: widening_authority_ref"
        if not (r.prior_version_id and r.prior_version_digest and r.successor_version_id and r.successor_version_digest):
            return "a successful reclassify names both versions and their digests"
        missing = _RECLASSIFY_KINDS - {s.kind for s in r.surfaces}
        if missing:
            return f"receipt omits required surface kind(s) {sorted(k.value for k in missing)}"
    return None


def validate_reclassify_receipt(r: object) -> Decision:
    if not isinstance(r, ReclassifyReceipt):
        return _deny(MemoryPolicyReason.MALFORMED_INPUT, "not a ReclassifyReceipt")
    if (
        r.receipt_schema_version != RECLASSIFY_RECEIPT_SCHEMA_VERSION
        or r.canonicalization_ref != CANONICALIZATION_REF
    ):
        return _deny(MemoryPolicyReason.RECEIPT_WRONG_CANONICALIZATION, "schema/canonicalization")
    problem = _shape_problem(r.surfaces, r.index_purge_receipts, r.outcome, r.refusal, r.lifecycle_policy_pins)
    if problem is None:
        problem = _outcome_problem(r.surfaces, r.index_purge_receipts, r.outcome, r.refusal)
    if problem is None:
        problem = _reclassify_problem(r)
    if problem is not None:
        return _deny(MemoryPolicyReason.MALFORMED_INPUT, problem)
    if r.receipt_digest != compute_reclassify_receipt_digest(r):
        return _deny(MemoryPolicyReason.RECEIPT_DIGEST_MISMATCH, "receipt_digest does not recompute")
    return Decision(True, MemoryPolicyReason.AUTHORIZED, "reclassify receipt recomputes")


def _reclassify_receipt(
    *,
    operation_id: str,
    memory_id: str,
    prior_version_id: str | None,
    prior_version_digest: str | None,
    successor_version_id: str | None,
    successor_version_digest: str | None,
    direction: str | None,
    changed_fields: Sequence[str],
    actor: str,
    authority_ref: str,
    widening_authority_ref: str | None,
    index_purge_receipts: Sequence[IndexPurgeReceipt],
    completed_at: str,
    surfaces: Sequence[SurfaceRecord],
    lifecycle_policy_pins: Sequence[PolicyPin],
    refusal: LifecycleRefusal | None,
    refusal_detail: str,
) -> ReclassifyReceipt:
    surfaces = tuple(surfaces)
    r = ReclassifyReceipt(
        operation_id=operation_id,
        memory_id=memory_id,
        prior_version_id=prior_version_id,
        prior_version_digest=prior_version_digest,
        successor_version_id=successor_version_id,
        successor_version_digest=successor_version_digest,
        direction=direction,
        changed_fields=tuple(sorted(set(changed_fields))),
        actor=actor,
        authority_ref=authority_ref,
        widening_authority_ref=widening_authority_ref,
        index_purge_receipts=tuple(index_purge_receipts),
        completed_at=completed_at,
        surfaces=surfaces,
        lifecycle_policy_pins=tuple(sorted(set(lifecycle_policy_pins), key=lambda p: (p.ref, p.digest))),
        outcome=derive_outcome(surfaces, refusal),
        refusal=refusal,
        refusal_detail=refusal_detail,
        receipt_schema_version=RECLASSIFY_RECEIPT_SCHEMA_VERSION,
        canonicalization_ref=CANONICALIZATION_REF,
        receipt_digest="",
    )
    return dataclasses.replace(r, receipt_digest=compute_reclassify_receipt_digest(r))


# ---------------------------------------------------------------------------
# Shared op helpers.
# ---------------------------------------------------------------------------
OP_REQUEST_SCHEMA = "org058-op-request/v1"
_SUCCESS_OUTCOMES = frozenset({LifecycleOutcome.VERIFIED.value, LifecycleOutcome.VERIFIED_WITH_RETENTION.value})


def _finished_receipt(
    store: MemoryStore, operation_id: str, from_json: Callable[..., object], validate: Callable[[object], Decision]
) -> tuple[object | None, bool]:
    """An operation_id means exactly ONE request, and a finished op is never
    re-executed. Returns ``(receipt, False)`` when a SUCCESS receipt of this
    op exists and re-validates (the caller returns it unchanged: no source
    request, no rebuild, no new attempt file); ``(None, True)`` when a receipt
    that may be a success cannot be re-validated; ``(None, False)`` otherwise."""
    for path in store.receipt_paths(operation_id):
        try:
            raw = json.loads(path.read_bytes())
        except (OSError, ValueError):
            return None, True
        if not isinstance(raw, dict) or raw.get("outcome") not in _SUCCESS_OUTCOMES:
            continue
        try:
            receipt = from_json(raw)
        except SchemaError:
            return None, True
        if getattr(receipt, "operation_id", None) != operation_id or not validate(receipt).allowed:
            return None, True
        return receipt, False
    return None, False


def _request_digest(request: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(request)).hexdigest()


def _op_request_refusal(store: MemoryStore, operation_id: str, digest: str) -> _Refusal | None:
    """A re-run whose request differs from the one this op_id is bound to."""
    bound = store.op_request(operation_id)
    if bound is not None and bound.get("request_digest") != digest:
        return _Refusal(
            LifecycleRefusal.OPERATION_ID_REUSE,
            "operation_id is bound to a different request (op-request journal digest mismatch); use a new operation_id",
        )
    return None


def _bind_op_request(store: MemoryStore, operation_id: str, op: str, digest: str) -> None:
    """Bind the op_id to its request before its first mutation (write-once;
    ids, digests and closed codes only)."""
    store.bind_op_request(
        operation_id,
        {"schema": OP_REQUEST_SCHEMA, "operation_id": operation_id, "op": op, "request_digest": digest},
    )


class SimulatedCrash(BaseException):
    """TEST SEAM ONLY: raise it from a ``_fault`` hook to simulate a process
    crash. It is a BaseException, so it bypasses the ops' ``Exception``
    handler exactly as a real crash would: nothing is written, and a re-run
    must repair. Any real ``Exception`` is instead caught and reported."""


def _no_fault(_step: str) -> None:
    return None


@dataclass
class _Progress:
    """Where an op is, so a real fault is reported against the step it hit."""

    mode: LifecycleMode
    step: str = ""
    kind: SurfaceKind = SurfaceKind.ENVELOPE_METADATA
    mutated: bool = False

    def __post_init__(self) -> None:
        self.at("preflight", SurfaceKind.ENVELOPE_METADATA)

    def at(self, step: str, kind: SurfaceKind, *, mutates: bool = False) -> None:
        self.step, self.kind = f"{self.mode.value}:{step}", kind
        self.mutated = self.mutated or mutates


def _exc_evidence(exc: BaseException) -> str:
    """Exception CLASS + errno only. Never the message or a path: either can
    carry the very content being forgotten. An ``OSError`` is an I/O fault, a
    ``StoreIntegrityError`` is corrupt store DATA, and anything else is an
    internal defect; the evidence says which."""
    if isinstance(exc, OSError):
        if isinstance(exc.errno, int):
            return f"{_cls(exc)} errno={exc.errno} ({errno.errorcode.get(exc.errno, '?')})"
        return f"{_cls(exc)} (I/O fault, no errno)"
    if isinstance(exc, StoreIntegrityError):
        return f"store-integrity-error {_cls(exc)} (the store's own files contradict each other; not an I/O fault)"
    return f"internal-error {_cls(exc)} (not an I/O fault; defect)"


def _report_defect(progress: _Progress, exc: BaseException) -> None:
    """A defect's traceback FRAMES (file:line:function, never the message) go
    to stderr, so it can be fixed without content reaching a log."""
    frames = " <- ".join(
        f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}"
        for f in reversed(traceback.extract_tb(exc.__traceback__))
    )
    print(
        f"memory_lifecycle_ops: internal-error {_cls(exc)} at step {progress.step}; frames: {frames}",
        file=sys.stderr,
    )


def _fault_surfaces(progress: _Progress, exc: Exception) -> list[SurfaceRecord]:
    """The receipt of an op stopped by a real fault: the failing step as an
    ``unverifiable`` surface, and every other kind the mode always enumerates
    marked not verified. Its outcome derives to ``incomplete``; a re-run with
    the same ``operation_id`` repairs and lands as ``+attempt-<n>``."""
    ev = _exc_evidence(exc)
    if not isinstance(exc, (OSError, StoreIntegrityError)):
        _report_defect(progress, exc)
    phase = "after mutation began" if progress.mutated else "before any mutation"
    out = [
        SurfaceRecord(
            f"op-error:{progress.step}", progress.kind, SurfaceStatus.UNVERIFIABLE,
            f"{ev} at step {progress.step} ({phase}); re-run the same operation_id to repair",
        )
    ]
    for kind in sorted(REQUIRED_SURFACE_KINDS[progress.mode] - {progress.kind}, key=lambda k: k.value):
        out.append(
            SurfaceRecord(
                f"{kind.value}:not-verified", kind, SurfaceStatus.UNVERIFIABLE,
                f"not verified: the op stopped at {progress.step} ({ev})",
            )
        )
    return out


def _ref(memory_id: str, version_id: str) -> str:
    return f"{memory_id}/{version_id}"


def _admitted_versions(store: MemoryStore, memory_id: str) -> frozenset[str]:
    """Version ids of ``memory_id`` that have an event; any other version or
    payload file is crash residue."""
    return frozenset(v for (m, v) in store.folded() if m == memory_id)


def _src_key(ref: SourceRef) -> tuple[str, str]:
    """The internal identity of a source object: a TUPLE, so two objects can
    never share a key (the joined name below is only for display)."""
    return (ref.source_system, ref.object_id)


def _src_name(ref: SourceRef) -> str:
    """``system/object`` for surface names and evidence. Injective because
    admission refuses any source_system containing ``/``."""
    return f"{ref.source_system}/{ref.object_id}"


def _parse_src_name(name: str) -> tuple[str, str] | None:
    system, sep, object_id = name.partition("/")
    return (system, object_id) if sep else None


@dataclass(frozen=True)
class _Read:
    ok: bool
    digest: str | None
    error: str = ""


def _read_source(adapter: object, ref: SourceRef) -> _Read:
    if adapter is None:
        return _Read(False, None, f"no source adapter for {ref.source_system!r}")
    try:
        digest = adapter.read_digest(ref)  # type: ignore[attr-defined]
    except Exception as exc:  # an adapter fault is unverifiable, never "absent"
        return _Read(False, None, f"read_digest raised {_cls(exc)}")
    if digest is not None and not (isinstance(digest, str) and _DIGEST_TEXT_RE.fullmatch(digest)):
        return _Read(False, None, f"read_digest returned a {_digest_text(digest)}")
    return _Read(True, digest)


def _unchanged_surface(ref: SourceRef, before: _Read, after: _Read) -> SurfaceRecord:
    name = f"{SOURCE_UNCHANGED_PREFIX}{_src_name(ref)}"
    if not before.ok or not after.ok:
        return SurfaceRecord(
            name,
            SurfaceKind.SOURCE,
            SurfaceStatus.UNVERIFIABLE,
            f"cannot prove the source unchanged: {before.error or after.error}",
        )
    if before.digest != after.digest:
        return SurfaceRecord(
            name,
            SurfaceKind.SOURCE,
            SurfaceStatus.UNVERIFIABLE,
            f"source digest moved during the op ({before.digest} -> {after.digest})",
        )
    return SurfaceRecord(
        name,
        SurfaceKind.SOURCE,
        SurfaceStatus.VERIFIED,
        f"unchanged: read_digest before == after == {before.digest}",
    )


def _source_request_surface(
    *,
    erasure: bool,
    ref: SourceRef,
    adapter: object,
    request_id: str,
    authority_ref: str,
    correction: str = "",
    prior_receipt: str | None = None,
    store: MemoryStore | None = None,
    operation_id: str = "",
) -> SurfaceRecord:
    """One source request, verified per §2.7. ``prior_receipt`` names an
    earlier, validating receipt of the SAME operation that already verified
    this erasure: if the object is now absent, the re-run converges on it
    instead of taking the source's "nothing to erase" answer.

    At most ONE send per op and source object: an intent record is journaled
    before the send and a sent record after it. A resume that finds a sent
    record never sends again; the fresh read decides. A resume that finds only
    the INTENT (crash before the sent record) and whose fresh read does not show
    the requested state re-sends ONCE, with the SAME request_id, only if the
    adapter declares ``idempotent_request_ids is True``: journal
    ``source-request-resend-intent`` first, then ``source-request-sent`` with
    ``resend: true``, then the normal verification. A journal that already holds
    a resend-intent never sends again. Every other intent-only case is
    ``unverifiable`` with an ``intent-only-stuck:`` surface."""
    prefix = SOURCE_ERASURE_PREFIX if erasure else SOURCE_CORRECTION_PREFIX
    name = f"{prefix}{_src_name(ref)}"

    def record(status: SurfaceStatus, evidence: str) -> SurfaceRecord:
        return SurfaceRecord(name, SurfaceKind.SOURCE, status, evidence)

    if adapter is None:
        return record(
            SurfaceStatus.UNVERIFIABLE,
            f"no adapter for source system {ref.source_system!r}: not requested, not verifiable",
        )
    before = _read_source(adapter, ref)
    if not before.ok:
        return record(SurfaceStatus.UNVERIFIABLE, f"pre-request read failed: {before.error}")
    if erasure and prior_receipt is not None and before.digest is None:
        return record(
            SurfaceStatus.VERIFIED,
            f"object absent on this op's fresh read; erasure attributed to prior receipt {prior_receipt} "
            "(integrity-checked, not authenticated: unkeyed digest)",
        )
    source = [ref.source_system, ref.object_id]
    resend = False
    if store is not None:
        earlier = [r for r in store.op_journal(operation_id) if r.get("source") == source]
        if earlier:
            # The op-request binding guarantees this re-run asks for the SAME
            # correction, so its digest is derived here, never journaled.
            requested = None if erasure else _text_digest(correction)
            intent_only = not any(r.get("record") in _SETTLED_RECORDS for r in earlier)
            if intent_only and _resumed_shows(earlier, before, erasure, requested):
                # Settled by this fresh read: say so in the journal (append-only,
                # ids only) so the op no longer reads as intent-only/stuck.
                store.append_op_journal(
                    operation_id,
                    {"record": "source-request-verified", "source": source, "request_id": request_id},
                )
            # A resend is only safe if the source is provably unchanged since the
            # intent (else the old request would overwrite newer content).
            intents = [r for r in earlier if r.get("record") == "source-request-intent"]
            unchanged = bool(intents) and before.digest == intents[0].get("before_digest")
            resend_ok = (
                intent_only
                and not _resumed_shows(earlier, before, erasure, requested)
                and not any(r.get("record") == "source-request-resend-intent" for r in earlier)
                and _declares_idempotent_ids(adapter)
                and unchanged
            )
            if not resend_ok:
                return _resumed_source_surface(
                    record, earlier, before, erasure, requested,
                    resend_exhausted=any(r.get("record") == "source-request-resend-intent" for r in earlier),
                    source_changed=not unchanged,
                )
            resend = True
            store.append_op_journal(
                operation_id,
                {"record": "source-request-resend-intent", "source": source, "request_id": request_id,
                 "before_digest": before.digest},
            )
        else:
            store.append_op_journal(
                operation_id,
                {"record": "source-request-intent", "source": source, "request_id": request_id,
                 "before_digest": before.digest},
            )
    try:
        if erasure:
            outcome = adapter.request_erasure(ref, authority_ref, request_id)  # type: ignore[attr-defined]
        else:
            outcome = adapter.request_correction(ref, correction, authority_ref, request_id)  # type: ignore[attr-defined]
    except Exception as exc:
        return record(SurfaceStatus.UNVERIFIABLE, f"request raised {_cls(exc)}")
    if store is not None:
        receipt = getattr(outcome, "source_receipt", None)
        store.append_op_journal(
            operation_id,
            {
                "record": "source-request-sent",
                "source": source,
                "request_id": request_id,
                "receipt_digest": _receipt_digest(receipt),
                "after_digest": receipt.after_digest
                if isinstance(receipt, SourceReceipt) and _is_digest(receipt.after_digest)
                else None,
                **({"resend": True} if resend else {}),
            },
        )
    after = _read_source(adapter, ref)
    if not after.ok:
        return record(SurfaceStatus.UNVERIFIABLE, f"post-request read failed: {after.error}")
    status, evidence = verify_source_request(
        outcome,
        request_id=request_id,
        object_id=ref.object_id,
        before_digest=before.digest,
        fresh_digest=after.digest,
        erasure=erasure,
    )
    return record(status, evidence)


def _resumed_shows(earlier: Sequence[dict], fresh: _Read, erasure: bool, requested: str | None) -> bool:
    """Does the op's own fresh read show the requested state (object absent; or
    the requested / receipted digest)?"""
    if erasure:
        return fresh.digest is None
    sent = [r for r in earlier if r.get("record") == "source-request-sent"]
    expected = {r.get("after_digest") for r in sent} | {requested}
    return fresh.digest is not None and fresh.digest in expected


STUCK_TOKEN = "intent-only-stuck:"
_SETTLED_RECORDS = ("source-request-sent", "source-request-verified")


def _request_kind(request_id: object) -> str:
    text = str(request_id)
    return "erasure" if ":source-erasure:" in text else "correction" if ":source-correction:" in text else ""


def _resumed_source_surface(
    record: Callable[[SurfaceStatus, str], SurfaceRecord],
    earlier: Sequence[dict],
    fresh: _Read,
    erasure: bool,
    requested: str | None,
    *,
    resend_exhausted: bool = False,
    source_changed: bool = False,
) -> SurfaceRecord:
    """A resume after this op already sent (or may have sent) its source
    request: no send here. Verified only if the op's own fresh read shows the
    requested state. An intent-only journal whose fresh read does not show it
    is the explicit STUCK surface (``intent-only-stuck:``)."""
    sent = [r for r in earlier if r.get("record") == "source-request-sent"]
    settled = sent or any(r.get("record") == "source-request-verified" for r in earlier)
    how = "sent once by this operation (op journal)" if sent else (
        "settled by an earlier fresh read of this operation (op journal)" if settled else
        "a send may have happened: the op crashed before journaling it"
    )
    if _resumed_shows(earlier, fresh, erasure, requested):
        return record(SurfaceStatus.VERIFIED, f"{how}; fresh read shows the requested state; not re-sent")
    state = f"fresh read ({_digest_text(fresh.digest)}) does not show the requested state; not re-sent"
    if settled:
        return record(SurfaceStatus.UNVERIFIABLE, f"{how}; {state}")
    why = (
        "the single same-request_id resend was already attempted and is exhausted"
        if resend_exhausted
        else "the source changed since the intent, so a resend could overwrite newer content"
        if source_changed
        else "the adapter does not declare idempotent_request_ids"
    )
    return record(
        SurfaceStatus.UNVERIFIABLE,
        f"{STUCK_TOKEN} {how}; {state}; {why}. Check the source by hand, then re-issue "
        "under a NEW operation_id",
    )


def stuck_source_requests(store: MemoryStore) -> tuple[tuple[str, tuple[str, str], str], ...]:
    """Every (operation_id, (source_system, object_id), request_id) whose op
    journal holds a ``source-request-intent`` and no ``source-request-sent`` or
    ``-verified`` for that source, from the op journals alone, and which no
    OTHER op has since settled. Another op settles it only if its journal holds a
    sent/verified record AND an intent record for the same source and request
    kind whose ``before_digest`` EQUALS this op's: it addressed the same source
    state (the documented re-issue under a new operation_id). An older op that
    saw an earlier state never clears it. Residual, accepted: an ABA re-creation
    with byte-identical content cannot be told apart, and if the source changed
    (say, fixed by hand) the old entry stays listed: noise, never a hidden stuck
    op. The kind is read from the request_id; an id with neither marker never
    matches. Ids only, never content."""
    ops_dir = store.root / "ops"
    names = sorted(p.name[: -len(".journal.jsonl")] for p in ops_dir.glob("*.journal.jsonl")) if ops_dir.is_dir() else []
    journals = {op_id: store.op_journal(op_id) for op_id in names}
    # (system, object, kind) -> before_digests of intents belonging to ops that settled it
    settled: dict[tuple[str, str, str], set[object]] = {}
    for records in journals.values():
        done = {
            (*tuple(r["source"]), _request_kind(r.get("request_id")))
            for r in records
            if r.get("record") in _SETTLED_RECORDS and r.get("source")
        }
        for r in records:
            if r.get("record") == "source-request-intent" and r.get("source"):
                key = (*tuple(r["source"]), _request_kind(r.get("request_id")))
                if key in done and key[2]:
                    settled.setdefault(key, set()).add(r.get("before_digest"))
    out: list[tuple[str, tuple[str, str], str]] = []
    for op_id, records in journals.items():
        seen: set[tuple[str, str]] = set()
        own_settled = {tuple(r["source"]) for r in records if r.get("record") in _SETTLED_RECORDS and r.get("source")}
        for r in records:
            if r.get("record") != "source-request-intent" or not r.get("source"):
                continue
            key = tuple(r["source"])
            if key in seen or key in own_settled:
                continue
            seen.add(key)
            kind = _request_kind(r.get("request_id"))
            if kind and r.get("before_digest") in settled.get((*key, kind), set()):
                continue
            out.append((op_id, key, str(r.get("request_id"))))
    return tuple(out)


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_TEXT_RE.fullmatch(value) is not None


def _text_digest(text: str | None) -> str | None:
    return None if text is None else "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _receipt_digest(receipt: object) -> str | None:
    if not isinstance(receipt, SourceReceipt):
        return None
    fields = {k: None if v is None else str(v) for k, v in dataclasses.asdict(receipt).items()}
    return "sha256:" + hashlib.sha256(canonical_json_bytes(fields)).hexdigest()


def _store_residue_surfaces(store: MemoryStore) -> list[SurfaceRecord]:
    """Backups and media residue, as DECLARED by the store manifest. An unknown
    declaration is never read as "none"."""
    manifest = store.read_manifest()
    out: list[SurfaceRecord] = []
    if manifest is None or manifest.backup_surfaces is None:
        out.append(
            SurfaceRecord(
                "backup:undeclared",
                SurfaceKind.BACKUP,
                SurfaceStatus.UNVERIFIABLE,
                "store_manifest missing or backup_surfaces undeclared: backups are unknown, not absent",
            )
        )
    elif not manifest.backup_surfaces:
        out.append(
            SurfaceRecord(
                "backup:none-declared",
                SurfaceKind.BACKUP,
                SurfaceStatus.RETAINED_BY_POLICY,
                "store_manifest declares no backup surfaces (explicit empty list, an operator attestation); "
                "this op verified nothing about backups",
            )
        )
    else:
        for b in manifest.backup_surfaces:
            if b.retention:
                out.append(
                    SurfaceRecord(
                        f"backup:{b.name}",
                        SurfaceKind.BACKUP,
                        SurfaceStatus.RETAINED_BY_POLICY,
                        f"declared backup retains prior copies until its retention ({b.retention}); "
                        f"verifiable={b.verifiable}, but no backup verifier is wired here, so no purge is claimed",
                    )
                )
            else:
                out.append(
                    SurfaceRecord(
                        f"backup:{b.name}",
                        SurfaceKind.BACKUP,
                        SurfaceStatus.UNVERIFIABLE,
                        "declared backup states no retention",
                    )
                )
    if manifest is not None and manifest.media_scrubbed is True:
        # An attestation is not a verification: ``verified`` is reserved for
        # what this op itself checked, and it scrubbed nothing.
        out.append(
            SurfaceRecord(
                "media-residue", SurfaceKind.MEDIA_RESIDUE, SurfaceStatus.RETAINED_BY_POLICY, MEDIA_SCRUBBED_EVIDENCE
            )
        )
    else:
        out.append(
            SurfaceRecord(
                "media-residue",
                SurfaceKind.MEDIA_RESIDUE,
                SurfaceStatus.UNVERIFIABLE,
                "unlinked file blocks may persist on the medium; the manifest does not declare "
                "media_scrubbed=true and no physical scrub is claimed",
            )
        )
    return out


def _emit(store: object, receipt: object, to_json: Callable[..., dict]) -> object:
    if isinstance(store, MemoryStore) and is_safe_id(getattr(receipt, "operation_id", None)):
        store.write_receipt(receipt.operation_id, to_json(receipt))  # type: ignore[attr-defined]
    return receipt


def _str_seq(value: object) -> tuple[str, ...] | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
        return None
    if any(not isinstance(v, str) for v in value):
        return None
    return tuple(sorted(set(value)))


# ---------------------------------------------------------------------------
# correct(): supersede, plus an optional request-source-correction leg.
# ---------------------------------------------------------------------------
_NO_SOURCES: Mapping[str, SourceAdapter] = MappingProxyType({})


def _is_adapter(value: object) -> bool:
    return all(callable(getattr(value, m, None)) for m in ("read_digest", "request_correction", "request_erasure"))


def _sources_problem(sources: object) -> str | None:
    if not isinstance(sources, Mapping):
        return "sources must map source_system -> adapter"
    if any(not isinstance(k, str) or not _is_adapter(v) for k, v in sources.items()):
        return "sources must map a source_system string to a SourceAdapter"
    return None


def _trust_inputs_problem(trust_policy: object, trust_receipts: object) -> str | None:
    if trust_policy is not None and not isinstance(trust_policy, TrustPolicy):
        return "trust_policy must be a TrustPolicy or None"
    if trust_receipts is None:
        return None
    if isinstance(trust_receipts, Mapping):
        ok = all(isinstance(k, str) and isinstance(v, TrustAdjudicationReceipt) for k, v in trust_receipts.items())
    elif isinstance(trust_receipts, Sequence) and not isinstance(trust_receipts, (str, bytes)):
        ok = all(isinstance(v, TrustAdjudicationReceipt) for v in trust_receipts)
    else:
        ok = False
    return None if ok else "trust_receipts must be None, a Mapping or a Sequence of TrustAdjudicationReceipt"


def _correction_reason(reason: object) -> CorrectionReason | None:
    try:
        return CorrectionReason(reason)
    except (ValueError, TypeError):
        return None


def _validate_successor(
    store: MemoryStore,
    prior: MemoryEnvelope,
    successor: MemoryEnvelope,
    trust_policy: TrustPolicy | None,
    trust_receipts: object,
    *,
    allowed: frozenset[str] = CORRECTABLE_FIELDS | _OWN_RULE_FIELDS,
    op_noun: str = "correction",
) -> _Refusal | None:
    M = LifecycleRefusal
    if successor.memory_id != prior.memory_id:
        return _Refusal(M.MALFORMED_INPUT, "successor.memory_id must equal the corrected memory")
    if not is_safe_id(successor.version_id):
        return _Refusal(M.MALFORMED_INPUT, "successor.version_id is not a safe id")
    if (prior.memory_id, successor.version_id) in store.folded():
        # An orphan (never-admitted) version file is left to append_version,
        # which completes an identical retry and refuses anything else.
        return _Refusal(M.VERSION_EXISTS, "successor version_id is already admitted")
    if prior.version_id not in successor.supersedes:
        return _Refusal(M.MALFORMED_INPUT, "successor.supersedes must contain the expected version")
    folded = store.current_envelope(prior.memory_id, prior.version_id)
    if folded is None or not is_legal_transition(folded.lifecycle_state, LifecycleState.SUPERSEDED):
        state = folded.lifecycle_state.value if folded else "unadmitted"
        return _Refusal(M.ILLEGAL_TRANSITION, f"{state} -> SUPERSEDED is not a legal transition")
    if successor.lifecycle_state not in RETRIEVABLE_STATES:
        return _Refusal(
            M.ILLEGAL_TRANSITION,
            f"a successor is retained as RETAINED_*, not {successor.lifecycle_state.value}",
        )
    changed = [
        f.name
        for f in dataclasses.fields(MemoryEnvelope)
        if f.name not in allowed
        and getattr(successor, f.name) != getattr(prior, f.name)
    ]
    if successor.lifecycle_state != folded.lifecycle_state:
        changed.append("lifecycle_state (retained kind)")
    if changed:
        return _Refusal(
            M.POLICY_FIELD_CHANGE,
            f"successor changes {', '.join(changed)}: changing policy is not a {op_noun}",
        )
    decision = validate_envelope(successor, trust_policy, receipts=trust_receipts)  # type: ignore[arg-type]
    if not decision.allowed:
        return _Refusal(M.ENVELOPE_INVALID, f"{decision.reason_code.value}: {decision.detail}")
    return None


def _source_rewrite_guard(
    prior_ref: SourceRef, successor_ref: SourceRef, sources: Mapping[str, SourceAdapter]
) -> _Refusal | None:
    """Memory may not declare the source says something it does not.

    The source OBJECT is fixed: ``(source_system, object_id)`` must equal the
    prior's (re-homing a memory is not a correction). The source STATE may
    move only to what the source itself now holds: ``(source_revision,
    source_digest)`` is either unchanged, or the digest differs from the
    prior's AND equals a fresh ``read_digest`` through the adapter keyed by
    that ``source_system``. A new revision label over unchanged bytes is a
    fabricated label; no adapter for the system means no proof (fail-closed).
    """
    M = LifecycleRefusal
    if (successor_ref.source_system, successor_ref.object_id) != (prior_ref.source_system, prior_ref.object_id):
        return _Refusal(
            M.SOURCE_IDENTITY_CHANGE,
            f"successor cites {_src_name(successor_ref)}, the corrected memory cites {_src_name(prior_ref)}: "
            "re-homing a memory is not a correction",
        )
    if (successor_ref.source_revision, successor_ref.source_digest) == (
        prior_ref.source_revision,
        prior_ref.source_digest,
    ):
        return None
    claim = f"{_src_name(successor_ref)}@{successor_ref.source_revision} digest {successor_ref.source_digest}"
    if successor_ref.source_digest == prior_ref.source_digest:
        return _Refusal(
            M.SOURCE_REWRITE_ATTEMPT,
            f"successor relabels the source revision {prior_ref.source_revision} -> "
            f"{successor_ref.source_revision} over unchanged bytes (fabricated label): {claim}",
        )
    adapter = sources.get(prior_ref.source_system)
    if adapter is None:
        return _Refusal(
            M.SOURCE_REWRITE_ATTEMPT,
            f"successor claims {claim}, and no adapter is keyed for source system "
            f"{prior_ref.source_system!r} to verify it (fail-closed)",
        )
    live = _read_source(adapter, successor_ref)
    if live.ok and live.digest is not None and live.digest == successor_ref.source_digest:
        return None
    seen = live.digest if live.ok else f"unreadable ({live.error})"
    return _Refusal(M.SOURCE_REWRITE_ATTEMPT, f"successor claims {claim}, live source reads {seen}")


def _own_successor_is_current(
    store: MemoryStore,
    memory_id: str,
    prior_vid: str,
    prior_digest: str,
    successor: MemoryEnvelope,
    successor_payload: str,
    operation_id: str,
    reason_note: str | None,
) -> bool:
    """True iff THIS operation already admitted exactly this successor over
    exactly this prior (the admission event carries the operation_id), and
    it is the memory's only current version: the case a fault after
    admission leaves behind, which a same-operation re-run completes."""
    fold = store.folded()
    mine, prior = fold.get((memory_id, successor.version_id)), fold.get((memory_id, prior_vid))
    if mine is None or prior is None or mine.admitted_by != operation_id:
        return False
    if prior.state != LifecycleState.SUPERSEDED or successor.version_id not in prior.superseded_by:
        return False
    if store.version_digest(memory_id, prior_vid) != prior_digest:
        return False
    if store.version_digest(memory_id, successor.version_id) != envelope_version_digest(successor):
        return False
    if store.read_payload(memory_id, successor.version_id) != successor_payload:
        return False
    if reason_note is not None and store.reason_note_digest(memory_id, successor.version_id, reason_note) is None:
        return False
    return store.retrievable_version_ids(memory_id) == (successor.version_id,)


def _correction_projection_evidence(
    store: MemoryStore,
    memory_id: str,
    prior_vid: str,
    successor_vid: str,
    gens: Mapping[str, tuple[str | None, str]],
    rebuild_error: str,
) -> tuple[list[IndexPurgeReceipt], list[SurfaceRecord]]:
    prior_ref, succ_ref = (memory_id, prior_vid), (memory_id, successor_vid)
    iprs: list[IndexPurgeReceipt] = []
    surfaces: list[SurfaceRecord] = []
    for name in PROJECTIONS:
        cur = store.current_generation(name)
        before = gens[name][0] if name in gens else cur
        if rebuild_error:
            evidence = f"rebuild raised ({rebuild_error}); CURRENT left on {cur}, which predates the correction"
            iprs.append(IndexPurgeReceipt(name, before, cur, (_ref(*prior_ref),), False, evidence))
            surfaces.append(
                SurfaceRecord(
                    f"projection:{name}:{cur or 'none'}", SurfaceKind.PROJECTION, SurfaceStatus.UNVERIFIABLE, evidence
                )
            )
        else:
            try:
                held: frozenset[tuple[str, str]] | None = store.raw_refs(name, cur) if cur else frozenset()
                err = ""
            except Exception as exc:
                held, err = None, _exc_evidence(exc)
            if held is None:
                evidence = f"CURRENT {cur} unreadable ({err})"
                positive = negative = False
            else:
                positive, negative = succ_ref in held, prior_ref not in held
                evidence = (
                    f"CURRENT {cur} (raw): successor {_ref(*succ_ref)} "
                    f"{'present' if positive else 'MISSING'}, superseded {_ref(*prior_ref)} "
                    f"{'absent' if negative else 'STILL PRESENT'}"
                )
            iprs.append(IndexPurgeReceipt(name, before, cur, (_ref(*prior_ref),), negative, evidence))
            surfaces.append(
                SurfaceRecord(
                    f"projection:{name}:{cur}",
                    SurfaceKind.PROJECTION,
                    SurfaceStatus.VERIFIED if positive and negative else SurfaceStatus.UNVERIFIABLE,
                    evidence,
                )
            )
        for g in store.generations_on_disk(name):
            if g == cur:
                continue
            try:
                held_g = store.raw_refs(name, g)
            except Exception as exc:
                surfaces.append(
                    SurfaceRecord(
                        f"projection:{name}:{g}",
                        SurfaceKind.PROJECTION,
                        SurfaceStatus.UNVERIFIABLE,
                        f"unreadable ({_cls(exc)})",
                    )
                )
                continue
            if prior_ref in held_g:
                surfaces.append(
                    SurfaceRecord(
                        f"projection:{name}:{g}",
                        SurfaceKind.PROJECTION,
                        SurfaceStatus.RETAINED_BY_POLICY,
                        f"rollback generation (§9) still holds superseded {_ref(*prior_ref)}; "
                        "the query path re-filters against the retrievable set",
                    )
                )
            else:
                surfaces.append(
                    SurfaceRecord(
                        f"projection:{name}:{g}",
                        SurfaceKind.PROJECTION,
                        SurfaceStatus.VERIFIED,
                        f"{g} holds no superseded version",
                    )
                )
    return iprs, surfaces


def _correction_state_surfaces(
    store: MemoryStore,
    memory_id: str,
    prior_vid: str,
    successor_vid: str,
    reason_note: str | None,
    note_digest: str | None,
    resumed: bool = False,
) -> list[SurfaceRecord]:
    out: list[SurfaceRecord] = []
    prior = store.current_envelope(memory_id, prior_vid)
    closed = (
        prior is not None
        and prior.lifecycle_state == LifecycleState.SUPERSEDED
        and successor_vid in prior.superseded_by
    )
    out.append(
        SurfaceRecord(
            f"lineage:{_ref(memory_id, prior_vid)}",
            SurfaceKind.ENVELOPE_METADATA,
            SurfaceStatus.VERIFIED if closed else SurfaceStatus.UNVERIFIABLE,
            f"folded state {prior.lifecycle_state.value if prior else 'absent'}, superseded_by "
            f"{list(prior.superseded_by) if prior else []}; version file never rewritten",
        )
    )
    live = successor_vid in store.retrievable_version_ids(memory_id)
    out.append(
        SurfaceRecord(
            f"lineage:{_ref(memory_id, successor_vid)}",
            SurfaceKind.ENVELOPE_METADATA,
            SurfaceStatus.VERIFIED if live else SurfaceStatus.UNVERIFIABLE,
            ("successor retained and retrievable" if live else "successor NOT retrievable")
            + ("; admitted by an earlier attempt of this operation (resumed)" if resumed else ""),
        )
    )
    files = store.payload_files(memory_id)
    admitted = _admitted_versions(store, memory_id)
    note = f"{successor_vid}{REASON_NOTE_SUFFIX}"
    expected = [successor_vid] + ([note] if reason_note is not None else [])
    for name in expected:
        if name not in files:
            out.append(
                SurfaceRecord(
                    f"payload:{_ref(memory_id, name)}", SurfaceKind.PAYLOAD, SurfaceStatus.UNVERIFIABLE,
                    f"successor {_payload_label(name)} missing",
                )
            )
    for name in files:
        s = f"payload:{_ref(memory_id, name)}"
        label = _payload_label(name)
        if store.payload_file_version(name) not in admitted:
            out.append(
                SurfaceRecord(
                    s, SurfaceKind.PAYLOAD, SurfaceStatus.UNVERIFIABLE,
                    f"{ORPHAN_LABEL}; not retrievable, retained by no policy; purge with remove-derived",
                )
            )
        elif name == successor_vid:
            try:
                ok = store.read_payload(memory_id, successor_vid) is not None
            except StoreIntegrityError:
                ok = False
            out.append(
                SurfaceRecord(
                    s, SurfaceKind.PAYLOAD, SurfaceStatus.VERIFIED if ok else SurfaceStatus.UNVERIFIABLE,
                    "successor payload present and digest-verified" if ok else "successor payload fails its digest",
                )
            )
        elif name == note and reason_note is not None:
            ok = note_digest is not None and store.reason_note_digest(memory_id, successor_vid, reason_note) == note_digest
            out.append(
                SurfaceRecord(
                    s, SurfaceKind.PAYLOAD, SurfaceStatus.VERIFIED if ok else SurfaceStatus.UNVERIFIABLE,
                    "successor reason note present and digest-verified; a purgeable derived payload, "
                    "referenced from the receipt by ref + digest only"
                    if ok else "successor reason note fails its digest",
                )
            )
        else:
            out.append(
                SurfaceRecord(
                    s, SurfaceKind.PAYLOAD, SurfaceStatus.RETAINED_BY_POLICY,
                    f"superseded {label} retained, not retrievable, for lineage (P4); purge with remove-derived",
                )
            )
    return out


def _payload_label(name: str) -> str:
    return "reason note" if name.endswith(REASON_NOTE_SUFFIX) else "payload"


def correct(
    store: MemoryStore,
    *,
    operation_id: str,
    memory_id: str,
    expected_version_id: str,
    expected_version_digest: str,
    successor: MemoryEnvelope,
    successor_payload: str,
    reason: CorrectionReason | str,
    actor: str,
    authority_ref: str,
    lifecycle_policies: Mapping[str, LifecyclePolicy],
    trust_policy: TrustPolicy | None,
    trust_receipts: Mapping[str, TrustAdjudicationReceipt]
    | Sequence[TrustAdjudicationReceipt]
    | None = None,
    sources: Mapping[str, SourceAdapter] = _NO_SOURCES,
    source_correction: str | None = None,
    reason_note: str | None = None,
    now: str,
) -> CorrectionReceipt:
    """Close ``expected_version_id`` as SUPERSEDED and retain ``successor``.

    Order (all inside the store lock): (1) resolve the lifecycle policy for the
    expected version's ``policy_revision`` and the authority its
    ``correction_policy`` requires; (2) compare-and-swap: the memory's only
    retrievable version must be ``expected_version_id`` with digest
    ``expected_version_digest``; (3) validate the successor, including the
    source guard (same source object; a moved digest only if the adapter keyed
    by that ``source_system`` reads it live); (4) append the successor with ONE
    event that records
    both states; (5) rebuild projections and prove successor-present /
    prior-absent in the new CURRENT; (6) the source leg, which is independent
    of the supersession: a verified ``request-source-correction`` if
    ``source_correction`` is given, else a before == after ``unchanged`` proof;
    (7) write the receipt. A refusal at steps 1-3 mutates nothing but the
    receipts directory.

    ``reason`` is a closed ``CorrectionReason`` code. Free text goes in
    ``reason_note``, which is stored as the successor's purgeable derived
    payload ``<version_id>.reason``; the receipt carries only its ref + digest,
    so no receipt ever quotes content that a later forget removes.
    """
    M = LifecycleRefusal
    pins: list[PolicyPin] = []
    code = _correction_reason(reason)
    note_digest: str | None = None  # sha256(salt + note), read back after admission

    def finish(
        *,
        surfaces: Sequence[SurfaceRecord] = (),
        refusal: LifecycleRefusal | None = None,
        detail: str = "",
        prior_digest: str | None = None,
        successor_vid: str | None = None,
        iprs: Sequence[IndexPurgeReceipt] = (),
        emit: bool = True,
    ) -> CorrectionReceipt:
        noted = successor_vid is not None and note_digest is not None
        r = _correction_receipt(
            operation_id=str(operation_id),
            target_memory_id=str(memory_id),
            prior_version_digest=prior_digest,
            successor_version_id=successor_vid,
            reason=code,
            actor=str(actor),
            authority_ref=str(authority_ref),
            index_purge_receipts=iprs,
            completed_at=str(now),
            surfaces=surfaces,
            lifecycle_policy_pins=pins,
            reason_note_ref=f"{REASON_NOTE_REF_PREFIX}{memory_id}/{successor_vid}" if noted else None,
            reason_note_digest=note_digest if noted else None,
            refusal=refusal,
            refusal_detail=detail,
        )
        return _emit(store, r, correction_receipt_to_json) if emit else r  # type: ignore[return-value]

    if not isinstance(store, MemoryStore) or not is_safe_id(operation_id):
        return finish(refusal=M.MALFORMED_INPUT, detail="need a MemoryStore and a safe operation_id")
    if not (is_safe_id(memory_id) and is_safe_id(expected_version_id)):
        return finish(refusal=M.MALFORMED_INPUT, detail="memory_id/expected_version_id not safe ids")
    for label, value in (
        ("expected_version_digest", expected_version_digest),
        ("actor", actor),
        ("now", now),
    ):
        if not isinstance(value, str) or not value:
            return finish(refusal=M.MALFORMED_INPUT, detail=f"{label} must be a non-empty string")
    if code is None:  # never echo the offending value: it may be content
        return finish(
            refusal=M.MALFORMED_INPUT,
            detail="reason must be a CorrectionReason code; free text belongs in reason_note",
        )
    if reason_note is not None and (not isinstance(reason_note, str) or not reason_note):
        return finish(refusal=M.MALFORMED_INPUT, detail="reason_note must be a non-empty string or None")
    if not isinstance(authority_ref, str):
        return finish(refusal=M.MALFORMED_INPUT, detail="authority_ref must be a string")
    if not isinstance(successor, MemoryEnvelope) or not isinstance(successor_payload, str):
        return finish(refusal=M.MALFORMED_INPUT, detail="need a successor MemoryEnvelope and str payload")
    if source_correction is not None and (not isinstance(source_correction, str) or not source_correction):
        return finish(refusal=M.MALFORMED_INPUT, detail="source_correction must be a non-empty string")
    # Every input is type-checked HERE, before the lock: a wrong-typed input
    # is a typed refusal, never a fault inside the op.
    problem = _sources_problem(sources) or _trust_inputs_problem(trust_policy, trust_receipts)
    if problem is None and not isinstance(lifecycle_policies, Mapping):
        problem = "lifecycle_policies must map revision -> LifecyclePolicy"
    if problem is not None:
        return finish(refusal=M.MALFORMED_INPUT, detail=problem)

    progress = _Progress(LifecycleMode.SUPERSEDE)
    prior_digest: str | None = None
    admitted_vid: str | None = None
    try:
        with store.locked():
            prior = store.stored_envelope(memory_id, expected_version_id)
            if prior is None or (memory_id, expected_version_id) not in store.folded():
                return finish(
                    refusal=M.STALE_VERSION, detail=f"{_ref(memory_id, expected_version_id)} is not an admitted version"
                )
            prior_digest = envelope_version_digest(prior)

            # (1) lifecycle policy (pinned identity) + authority, deny-by-default
            policy, why = _resolve_pinned_policy(
                store, memory_id, expected_version_id, prior.policy_revision, lifecycle_policies
            )
            if why is None and policy is not None:
                pins.append(policy_pin(policy))
                why = _correction_authority(policy, prior, authority_ref)
            if why is not None or policy is None:
                assert why is not None
                return finish(refusal=why.code, detail=why.detail, prior_digest=prior_digest)

            # the op's request; a re-run of this op_id must be exactly it
            request_digest = _request_digest(
                {
                    "op": "correct",
                    "mode": (
                        LifecycleMode.REQUEST_SOURCE_CORRECTION if source_correction is not None else LifecycleMode.SUPERSEDE
                    ).value,
                    "memory_id": memory_id,
                    "expected_version_id": expected_version_id,
                    "expected_version_digest": expected_version_digest,
                    "successor": envelope_version_digest(successor),
                    "payload_digest": payload_digest(successor_payload),
                    "reason": code.value,
                    "reason_note": reason_note is not None,
                    "source_request": _text_digest(source_correction),
                    "policy_pin": {"ref": pins[0].ref, "digest": pins[0].digest},
                    "authority_ref": authority_ref,
                    "actor": actor,
                }
            )
            why = _op_request_refusal(store, operation_id, request_digest)
            if why is None and reason_note is not None and store.op_request(operation_id) is not None:
                # The digest binds note PRESENCE (the salt lives only in the note
                # payload); once this op stored its note, its text is the binding.
                note_file = f"{successor.version_id}{REASON_NOTE_SUFFIX}"
                if note_file in store.payload_files(memory_id) and (
                    store.reason_note_digest(memory_id, successor.version_id, reason_note) is None
                ):
                    why = _Refusal(
                        M.OPERATION_ID_REUSE,
                        "this operation_id stored a different reason note; use a new operation_id",
                    )
            if why is not None:
                return finish(refusal=why.code, detail=why.detail, prior_digest=prior_digest)
            done, bad = _finished_receipt(store, operation_id, correction_receipt_from_json, validate_correction_receipt)
            if bad:  # no side effects: not even a receipt
                return finish(
                    refusal=M.MALFORMED_INPUT,
                    detail="an earlier success receipt of this operation_id fails validation",
                    emit=False,
                )
            if done is not None:
                return done  # type: ignore[return-value]

            # A re-run of THIS operation whose own successor is already current
            # (a fault after admission) resumes after the admission: it re-runs
            # the rebuild, post-verify and source leg. Anything else goes
            # through compare-and-swap, so a different operation stays stale.
            src_ref = successor.canonical_source_ref
            adapter = sources.get(src_ref.source_system)
            resumed = _own_successor_is_current(
                store, memory_id, expected_version_id, expected_version_digest,
                successor, successor_payload, operation_id, reason_note,
            )
            if resumed:
                _bind_op_request(store, operation_id, "correct", request_digest)
                progress.at("resume", SurfaceKind.ENVELOPE_METADATA, mutates=True)
                before = _read_source(adapter, src_ref)
            else:
                # (2) compare-and-swap on (version_id, version_digest)
                live = store.retrievable_version_ids(memory_id)
                if live != (expected_version_id,):
                    return finish(
                        refusal=M.STALE_VERSION,
                        detail=f"current retrievable version(s) {list(live)} != expected {expected_version_id}",
                        prior_digest=prior_digest,
                    )
                if prior_digest != expected_version_digest:
                    return finish(
                        refusal=M.STALE_VERSION,
                        detail="expected_version_digest does not match the stored version",
                        prior_digest=prior_digest,
                    )

                # (3) successor validation + source-rewrite guard
                why = _validate_successor(store, prior, successor, trust_policy, trust_receipts)
                if why is None:
                    why = _source_rewrite_guard(prior.canonical_source_ref, successor.canonical_source_ref, sources)
                if why is not None:
                    return finish(refusal=why.code, detail=why.detail, prior_digest=prior_digest)

                # The guard fixed (source_system, object_id), so one source object.
                before = _read_source(adapter, src_ref)

                # (4) append successor + ONE event closing the prior
                _bind_op_request(store, operation_id, "correct", request_digest)
                progress.at("admit", SurfaceKind.ENVELOPE_METADATA, mutates=True)
                admit = store.append_version(
                    successor,
                    successor_payload,
                    lifecycle_policy=policy,
                    trust_policy=trust_policy,
                    trust_receipts=trust_receipts,
                    now=now,
                    operation_id=operation_id,
                    supersedes_version=expected_version_id,
                    reason_note=reason_note,
                )
                if admit.refusal is not None:
                    return finish(refusal=admit.refusal, detail=admit.detail, prior_digest=prior_digest)
            admitted_vid = successor.version_id
            if reason_note is not None:
                note_digest = store.reason_note_digest(memory_id, successor.version_id, reason_note)

            # (5) rebuild + positive/negative proof in the new CURRENT
            progress.at("rebuild", SurfaceKind.PROJECTION)
            gens: dict[str, tuple[str | None, str]] = {}
            rebuild_error = ""
            try:
                gens = store.rebuild_projections()
            except Exception as exc:
                rebuild_error = _exc_evidence(exc)
            progress.at("post-verify", SurfaceKind.ENVELOPE_METADATA)
            iprs, proj_surfaces = _correction_projection_evidence(
                store, memory_id, expected_version_id, successor.version_id, gens, rebuild_error
            )
            surfaces = _correction_state_surfaces(
                store, memory_id, expected_version_id, successor.version_id, reason_note, note_digest, resumed
            )
            surfaces.extend(proj_surfaces)

            # (6) source leg, independent of the supersession
            progress.at("source-leg", SurfaceKind.SOURCE, mutates=source_correction is not None)
            if source_correction is None:
                surfaces.append(_unchanged_surface(src_ref, before, _read_source(adapter, src_ref)))
            else:
                surfaces.append(
                    _source_request_surface(
                        erasure=False,
                        ref=src_ref,
                        adapter=adapter,
                        request_id=f"{operation_id}:source-correction:{_src_name(src_ref)}",
                        authority_ref=authority_ref,
                        correction=source_correction,
                        store=store,
                        operation_id=operation_id,
                    )
                )

            # (7) receipt
            return finish(
                surfaces=surfaces,
                prior_digest=prior_digest,
                successor_vid=successor.version_id,
                iprs=iprs,
            )
    except Exception as exc:  # a REAL fault; SimulatedCrash (BaseException) is not caught
        return finish(surfaces=_fault_surfaces(progress, exc), prior_digest=prior_digest, successor_vid=admitted_vid)


# ---------------------------------------------------------------------------
# reclassify(): fix wrongly derived subjects / trust fields (T-4502).
# ---------------------------------------------------------------------------
def reclassify_direction(prior: MemoryEnvelope, successor: MemoryEnvelope, trust_policy: TrustPolicy | None) -> str:
    """``narrowing`` | ``widening`` | ``unchanged``, decided structurally.

    ``unchanged``: every semantic field (RECLASSIFY_SEMANTIC_FIELDS) is
    identical. ``narrowing`` iff ALL of: the successor's subjects are a subset
    of the prior's; its effective and inherited floors are ``leq`` the prior's;
    its claim_ids are a subset of the prior's and each shared claim's effective
    and inherited floors are ``leq`` the prior's. Anything else, including an
    incomparable pair, an unknown label or no trust policy, is ``widening``
    (fail closed): the evaluator grants access only through ``subjects``
    (subject-scope check) and ``effective_trust_floor`` (purpose disposition),
    and a floor that rose or an extra subject can only add retrievals."""
    if all(getattr(prior, n) == getattr(successor, n) for n in RECLASSIFY_SEMANTIC_FIELDS):
        return DIRECTION_UNCHANGED
    if trust_policy is None:
        return DIRECTION_WIDENING
    if not set(successor.subjects) <= set(prior.subjects):
        return DIRECTION_WIDENING
    pairs = [
        (successor.effective_trust_floor, prior.effective_trust_floor),
        (successor.inherited_trust_floor, prior.inherited_trust_floor),
    ]
    prior_claims = {c.claim_id: c for c in prior.claim_trust}
    for c in successor.claim_trust:
        old = prior_claims.get(c.claim_id)
        if old is None:
            return DIRECTION_WIDENING
        pairs.append((c.effective_trust_floor, old.effective_trust_floor))
        pairs.append((c.inherited_trust_floor, old.inherited_trust_floor))
    if all(trust_policy.leq(new, old) for new, old in pairs):
        return DIRECTION_NARROWING
    return DIRECTION_WIDENING


def reclassify(
    store: MemoryStore,
    *,
    operation_id: str,
    memory_id: str,
    expected_version_id: str,
    expected_version_digest: str,
    successor: MemoryEnvelope,
    actor: str,
    authority_ref: str,
    widening_authority_ref: str | None = None,
    lifecycle_policies: Mapping[str, LifecyclePolicy],
    trust_policy: TrustPolicy | None,
    trust_receipts: Mapping[str, TrustAdjudicationReceipt]
    | Sequence[TrustAdjudicationReceipt]
    | None = None,
    now: str,
) -> ReclassifyReceipt:
    """Supersede the retrievable head with ``successor``, which may differ from
    it ONLY in RECLASSIFIABLE_FIELDS (the subjects / trust fields ``correct``
    refuses). Content (payload, digests, refs, policies, source) never changes;
    the payload is carried over from the prior.

    Same shape as ``correct``, all inside the store lock: pinned policy +
    the authority the memory's ``correction_policy`` requires; the op-request
    REUSE gate, then the finished-op short-circuit; compare-and-swap on
    (version, digest); successor validation (allowlist + ``validate_envelope``,
    floors recomputed); DIRECTION (``reclassify_direction``); one event; rebuild
    + successor-present / prior-absent proof; receipt.

    NARROWING needs only the correction authority. WIDENING (anything not
    provably narrower) ADDITIONALLY needs ``widening_authority_ref`` matching
    ``operator:tg:<n>`` or ``operator:T-<nnnn>``; otherwise ``widening-
    unauthorized`` and nothing is mutated. An unchanged reclassify is refused
    ``malformed-input``. It acts only on a RETAINED_* head (never resurrects).

    It sends NO source request and reads no source: reclassify never touches
    the source, so the receipt enumerates no SOURCE surface. The receipt names
    changed field NAMES, never their values."""
    M = LifecycleRefusal
    pins: list[PolicyPin] = []
    state: dict[str, object] = {"direction": None, "changed": (), "succ_digest": None}

    def finish(
        *,
        surfaces: Sequence[SurfaceRecord] = (),
        refusal: LifecycleRefusal | None = None,
        detail: str = "",
        prior_digest: str | None = None,
        successor_vid: str | None = None,
        iprs: Sequence[IndexPurgeReceipt] = (),
        emit: bool = True,
    ) -> ReclassifyReceipt:
        direction = state["direction"]
        r = _reclassify_receipt(
            operation_id=str(operation_id),
            memory_id=str(memory_id),
            prior_version_id=str(expected_version_id) if prior_digest is not None else None,
            prior_version_digest=prior_digest,
            successor_version_id=successor_vid,
            successor_version_digest=state["succ_digest"] if successor_vid is not None else None,  # type: ignore[arg-type]
            direction=direction,  # type: ignore[arg-type]
            changed_fields=state["changed"],  # type: ignore[arg-type]
            actor=str(actor),
            authority_ref=str(authority_ref),
            widening_authority_ref=widening_authority_ref if direction == DIRECTION_WIDENING else None,
            index_purge_receipts=iprs,
            completed_at=str(now),
            surfaces=surfaces,
            lifecycle_policy_pins=pins,
            refusal=refusal,
            refusal_detail=detail,
        )
        return _emit(store, r, reclassify_receipt_to_json) if emit else r  # type: ignore[return-value]

    if not isinstance(store, MemoryStore) or not is_safe_id(operation_id):
        return finish(refusal=M.MALFORMED_INPUT, detail="need a MemoryStore and a safe operation_id")
    if not (is_safe_id(memory_id) and is_safe_id(expected_version_id)):
        return finish(refusal=M.MALFORMED_INPUT, detail="memory_id/expected_version_id not safe ids")
    for label, value in (("expected_version_digest", expected_version_digest), ("actor", actor), ("now", now)):
        if not isinstance(value, str) or not value:
            return finish(refusal=M.MALFORMED_INPUT, detail=f"{label} must be a non-empty string")
    if not isinstance(authority_ref, str):
        return finish(refusal=M.MALFORMED_INPUT, detail="authority_ref must be a string")
    if widening_authority_ref is not None and not isinstance(widening_authority_ref, str):
        return finish(refusal=M.MALFORMED_INPUT, detail="widening_authority_ref must be a string or None")
    if not isinstance(successor, MemoryEnvelope):
        return finish(refusal=M.MALFORMED_INPUT, detail="need a successor MemoryEnvelope")
    problem = _trust_inputs_problem(trust_policy, trust_receipts)
    if problem is None and not isinstance(lifecycle_policies, Mapping):
        problem = "lifecycle_policies must map revision -> LifecyclePolicy"
    if problem is not None:
        return finish(refusal=M.MALFORMED_INPUT, detail=problem)

    progress = _Progress(LifecycleMode.SUPERSEDE)
    prior_digest: str | None = None
    admitted_vid: str | None = None
    try:
        with store.locked():
            prior = store.stored_envelope(memory_id, expected_version_id)
            if prior is None or (memory_id, expected_version_id) not in store.folded():
                return finish(
                    refusal=M.STALE_VERSION, detail=f"{_ref(memory_id, expected_version_id)} is not an admitted version"
                )
            prior_digest = envelope_version_digest(prior)

            policy, why = _resolve_pinned_policy(
                store, memory_id, expected_version_id, prior.policy_revision, lifecycle_policies
            )
            if why is None and policy is not None:
                pins.append(policy_pin(policy))
                why = _correction_authority(policy, prior, authority_ref)
            if why is not None or policy is None:
                assert why is not None
                return finish(refusal=why.code, detail=why.detail, prior_digest=prior_digest)

            # The trust lattice IS the access gate, so the direction is only as
            # good as the policy it is computed under: bind it. Recompute the
            # policy's content digest here (never trust the declared one) and
            # require it to equal BOTH the digest the policy declares and the
            # trust_policy_digest the prior envelope pins.
            if trust_policy is None:
                return finish(
                    refusal=M.TRUST_POLICY_MISMATCH, detail="reclassify needs the trust policy the prior pins",
                    prior_digest=prior_digest,
                )
            content = trust_policy_content_digest(trust_policy)
            if content != trust_policy.digest or content != prior.trust_policy_digest:
                return finish(
                    refusal=M.TRUST_POLICY_MISMATCH,
                    detail="trust policy content digest does not equal both its declared digest and the "
                    "digest the prior envelope pins",
                    prior_digest=prior_digest,
                )

            request_digest = _request_digest(
                {
                    "op": "reclassify",
                    "memory_id": memory_id,
                    "expected_version_id": expected_version_id,
                    "expected_version_digest": expected_version_digest,
                    "successor": envelope_version_digest(successor),
                    "policy_pin": {"ref": pins[0].ref, "digest": pins[0].digest},
                    "authority_ref": authority_ref,
                    "widening_authority_ref": widening_authority_ref,
                    "actor": actor,
                }
            )
            why = _op_request_refusal(store, operation_id, request_digest)
            if why is not None:
                return finish(refusal=why.code, detail=why.detail, prior_digest=prior_digest)
            done, bad = _finished_receipt(
                store, operation_id, reclassify_receipt_from_json, validate_reclassify_receipt
            )
            if bad:  # no side effects: not even a receipt
                return finish(
                    refusal=M.MALFORMED_INPUT,
                    detail="an earlier success receipt of this operation_id fails validation",
                    emit=False,
                )
            if done is not None:
                return done  # type: ignore[return-value]

            prior_payload = store.read_payload(memory_id, expected_version_id)

            # A re-run of THIS operation whose own successor is already current
            # (a fault after admission) resumes after the admission.
            resumed = prior_payload is not None and _own_successor_is_current(
                store, memory_id, expected_version_id, expected_version_digest,
                successor, prior_payload, operation_id, None,
            )
            if not resumed:
                # Only a RETAINED_* head is reclassified: nothing is resurrected.
                head = store.current_envelope(memory_id, expected_version_id)
                if head is None or head.lifecycle_state not in RETRIEVABLE_STATES:
                    held = head.lifecycle_state.value if head else "unadmitted"
                    return finish(
                        refusal=M.ILLEGAL_TRANSITION,
                        detail=f"{held} is not a retrievable head; reclassify never resurrects",
                        prior_digest=prior_digest,
                    )
                if prior_payload is None:
                    return finish(
                        refusal=M.ENVELOPE_INVALID, detail="the prior's payload is absent", prior_digest=prior_digest
                    )
                live = store.retrievable_version_ids(memory_id)
                if live != (expected_version_id,):
                    return finish(
                        refusal=M.STALE_VERSION,
                        detail=f"current retrievable version(s) {list(live)} != expected {expected_version_id}",
                        prior_digest=prior_digest,
                    )
                if prior_digest != expected_version_digest:
                    return finish(
                        refusal=M.STALE_VERSION,
                        detail="expected_version_digest does not match the stored version",
                        prior_digest=prior_digest,
                    )
                why = _validate_successor(
                    store, prior, successor, trust_policy, trust_receipts,
                    allowed=RECLASSIFIABLE_FIELDS | {"lifecycle_state"}, op_noun="reclassify",
                )
                if why is not None:
                    return finish(refusal=why.code, detail=why.detail, prior_digest=prior_digest)

            if not successor.subjects or len(set(successor.subjects)) != len(successor.subjects):
                return finish(
                    refusal=M.MALFORMED_INPUT,
                    detail="successor.subjects must be non-empty and free of duplicates "
                    "(an empty set leaves the memory out of the subject projection, so a subject forget "
                    "cannot find it)",
                    prior_digest=prior_digest,
                )

            # DIRECTION, then the authority it needs.
            direction = reclassify_direction(prior, successor, trust_policy)
            state["succ_digest"] = envelope_version_digest(successor)
            if direction == DIRECTION_UNCHANGED:
                return finish(
                    refusal=M.MALFORMED_INPUT,
                    detail="successor changes no subject or trust field: nothing to reclassify",
                    prior_digest=prior_digest,
                )
            state["direction"] = direction
            state["changed"] = tuple(
                n for n in sorted(RECLASSIFY_SEMANTIC_FIELDS) if getattr(prior, n) != getattr(successor, n)
            )
            if direction == DIRECTION_WIDENING and not (
                isinstance(widening_authority_ref, str) and WIDENING_AUTHORITY_RE.fullmatch(widening_authority_ref)
            ):
                return finish(
                    refusal=M.WIDENING_UNAUTHORIZED,
                    detail="this reclassify widens access (a subject or floor is added or raised, or not "
                    "provably narrower): it needs a widening_authority_ref operator:tg:<n> or operator:T-<nnnn>",
                    prior_digest=prior_digest,
                )

            _bind_op_request(store, operation_id, "reclassify", request_digest)
            if resumed:
                progress.at("resume", SurfaceKind.ENVELOPE_METADATA, mutates=True)
            else:
                progress.at("admit", SurfaceKind.ENVELOPE_METADATA, mutates=True)
                admit = store.append_version(
                    successor,
                    prior_payload,
                    lifecycle_policy=policy,
                    trust_policy=trust_policy,
                    trust_receipts=trust_receipts,
                    now=now,
                    operation_id=operation_id,
                    supersedes_version=expected_version_id,
                )
                if admit.refusal is not None:
                    return finish(refusal=admit.refusal, detail=admit.detail, prior_digest=prior_digest)
            admitted_vid = successor.version_id

            progress.at("rebuild", SurfaceKind.PROJECTION)
            gens: dict[str, tuple[str | None, str]] = {}
            rebuild_error = ""
            try:
                gens = store.rebuild_projections()
            except Exception as exc:
                rebuild_error = _exc_evidence(exc)
            progress.at("post-verify", SurfaceKind.ENVELOPE_METADATA)
            iprs, proj_surfaces = _correction_projection_evidence(
                store, memory_id, expected_version_id, successor.version_id, gens, rebuild_error
            )
            surfaces = _correction_state_surfaces(
                store, memory_id, expected_version_id, successor.version_id, None, None, resumed
            )
            surfaces.extend(proj_surfaces)
            return finish(
                surfaces=surfaces, prior_digest=prior_digest, successor_vid=successor.version_id, iprs=iprs
            )
    except Exception as exc:  # a REAL fault; SimulatedCrash (BaseException) is not caught
        return finish(surfaces=_fault_surfaces(progress, exc), prior_digest=prior_digest, successor_vid=admitted_vid)


# ---------------------------------------------------------------------------
# forget(): remove-derived, request-source-erasure, expire, destroy-store.
# ---------------------------------------------------------------------------
_PURGED, _SCHEDULED, _UNTOUCHED = "purged", "scheduled", "untouched"
# Terminal heads remove-derived purges WITHOUT a transition (the §5 map has
# no exit from them); SUPERSEDED versions are already handled the same way.
_PURGE_ONLY_STATES = frozenset(
    {LifecycleState.FORGOTTEN_DERIVED, LifecycleState.DECAYED, LifecycleState.SUPERSEDED}
)


@dataclass(frozen=True)
class _TargetPlan:
    memory_id: str
    head: MemoryEnvelope | None
    rule: ForgettingRule | None
    refusal: _Refusal | None
    transition: LifecycleState | None = None  # the head's state change; None = none (re-runs, terminal heads)
    expectation: str = ""  # purged | scheduled | untouched
    pin: PolicyPin | None = None  # the admission-pinned policy this target acts under
    note: str = ""  # appended to the envelope-metadata evidence


def _resolve_targets(
    store: MemoryStore, target_ids: tuple[str, ...], subject_scope: tuple[str, ...]
) -> tuple[str, ...]:
    """``target_ids`` UNION every memory with a subject in ``subject_scope``.

    A global subject request fans out over every ADMITTED version, in every
    mode: a never-admitted orphan's subjects never select a target (so they
    never cause a purge, a source request or a tombstone). An orphan is still
    purged, as residue, when its memory is targeted for another reason."""
    ids = set(target_ids)
    if subject_scope:
        scope = set(subject_scope)
        for mid in store.memory_ids():
            for vid in sorted(_admitted_versions(store, mid)):
                env = store.stored_envelope(mid, vid)
                if env is not None and scope & set(env.subjects):
                    ids.add(mid)
                    break
    return tuple(sorted(ids))


def _head(store: MemoryStore, memory_id: str) -> tuple[MemoryEnvelope | None, str]:
    fold = store.folded()
    admitted = [v for v in store.version_ids(memory_id) if (memory_id, v) in fold]
    if not admitted:
        return None, f"no admitted version of {memory_id} in this store"
    heads = [v for v in admitted if not fold[(memory_id, v)].superseded_by]
    if len(heads) != 1:
        return None, f"ambiguous lineage head for {memory_id}: {heads}"
    return store.current_envelope(memory_id, heads[0]), ""


def _plan_target(
    store: MemoryStore,
    memory_id: str,
    mode: LifecycleMode,
    authority_ref: str,
    lifecycle_policies: object,
    now: str,
) -> _TargetPlan:
    M = LifecycleRefusal
    head, why_head = _head(store, memory_id)
    if head is None:
        return _TargetPlan(memory_id, None, None, _Refusal(M.MALFORMED_INPUT, why_head))
    policy, why = _resolve_pinned_policy(
        store, memory_id, head.version_id, head.policy_revision, lifecycle_policies
    )
    if why is not None or policy is None:
        return _TargetPlan(memory_id, head, None, why)
    rule, why = _forgetting_rule(policy, head, mode, authority_ref)
    if why is not None or rule is None:
        return _TargetPlan(memory_id, head, None, why)
    plan = _plan_by_state(memory_id, head, rule, mode, now)
    return dataclasses.replace(plan, pin=policy_pin(policy)) if plan.refusal is None else plan


def _plan_by_state(
    memory_id: str, head: MemoryEnvelope, rule: ForgettingRule, mode: LifecycleMode, now: str
) -> _TargetPlan:
    """What the mode does to this target, given the head's folded state. A
    head already in a terminal state is not transitioned again; its payloads
    are still purged (a ``payload-purged`` event), so a purge can always run."""
    M = LifecycleRefusal
    state = head.lifecycle_state

    def plan(transition: LifecycleState | None, expectation: str, note: str = "") -> _TargetPlan:
        return _TargetPlan(memory_id, head, rule, None, transition, expectation, note=note)

    def refuse(code: LifecycleRefusal, detail: str) -> _TargetPlan:
        return _TargetPlan(memory_id, head, rule, _Refusal(code, detail))

    if mode is LifecycleMode.REMOVE_DERIVED:
        if state in _PURGE_ONLY_STATES:
            return plan(None, _PURGED, f"head already {state.value} (terminal): payloads purged, no transition")
        if is_legal_transition(state, LifecycleState.FORGOTTEN_DERIVED):
            return plan(LifecycleState.FORGOTTEN_DERIVED, _PURGED)
        return refuse(M.ILLEGAL_TRANSITION, f"{state.value} -> FORGOTTEN_DERIVED is not a legal transition")
    if mode is LifecycleMode.EXPIRE:
        if head.retention_until is None:
            return refuse(M.RETENTION_NOT_ELAPSED, "no retention_until: an early removal is remove-derived, not expire")
        until, at = parse_zoned_time(head.retention_until), parse_zoned_time(now)
        if until is None or at is None:
            return refuse(M.MALFORMED_INPUT, "retention_until/now must be ISO-8601 with a zone")
        if until > at:
            return refuse(
                M.RETENTION_NOT_ELAPSED,
                f"retention_until {head.retention_until} is after now {now}; "
                "an early removal is remove-derived, not expire",
            )
        # A scheduled purge is DUE once purge_after <= now: it then executes.
        due = parse_zoned_time(rule.purge_after)
        if rule.expire_purge == "now":
            expectation, note = _PURGED, ""
        elif due is not None and due <= at:
            expectation, note = _PURGED, f"scheduled purge executed (purge_after {rule.purge_after} <= now {now})"
        else:
            expectation, note = _SCHEDULED, ""
        if state == LifecycleState.DECAYED:
            return plan(None, expectation, note)
        if not is_legal_transition(state, LifecycleState.DECAYED):
            return refuse(M.ILLEGAL_TRANSITION, f"{state.value} -> DECAYED is not a legal transition")
        return plan(LifecycleState.DECAYED, expectation, note)
    return plan(None, _UNTOUCHED)


def _memory_source_refs(store: MemoryStore, memory_id: str) -> tuple[SourceRef, ...]:
    """The source objects the memory was ADMITTED from (the event log), never
    an orphan version file's: crash residue that was never admitted must not
    cause a source request, a tombstone, or a convergence match."""
    seen: dict[tuple[str, str], SourceRef] = {}
    for vid in sorted(_admitted_versions(store, memory_id)):
        env = store.stored_envelope(memory_id, vid)
        if env is not None:
            seen.setdefault(_src_key(env.canonical_source_ref), env.canonical_source_ref)
    return tuple(seen[k] for k in sorted(seen))


def _refused_target_surfaces(
    store: MemoryStore, plan: _TargetPlan, mode: LifecycleMode
) -> list[SurfaceRecord]:
    assert plan.refusal is not None
    mid = plan.memory_id
    out = [
        SurfaceRecord(
            f"target:{mid}",
            SurfaceKind.ENVELOPE_METADATA,
            SurfaceStatus.REFUSED,
            f"{plan.refusal.code.value}: {plan.refusal.detail}",
        )
    ]
    for name in store.payload_files(mid):
        out.append(
            SurfaceRecord(
                f"payload:{_ref(mid, name)}",
                SurfaceKind.PAYLOAD,
                SurfaceStatus.REFUSED,
                "not acted on: target refused",
            )
        )
    if mode is LifecycleMode.REQUEST_SOURCE_ERASURE:
        for ref in _memory_source_refs(store, mid):
            out.append(
                SurfaceRecord(
                    f"{SOURCE_ERASURE_PREFIX}{_src_name(ref)}",
                    SurfaceKind.SOURCE,
                    SurfaceStatus.REFUSED,
                    "not requested: target refused",
                )
            )
    return out


@dataclass
class _Evidence:
    surfaces: list[SurfaceRecord]
    iprs: list[IndexPurgeReceipt]


def _verify_local(
    store: MemoryStore,
    eligible: Sequence[_TargetPlan],
    mode: LifecycleMode,
    pre_files: Mapping[str, tuple[str, ...]],
    gens: Mapping[str, tuple[str | None, str]],
    rebuild_error: str,
    torn_event_tail: bool = False,
) -> _Evidence:
    """Post-verify every LOCAL surface of the eligible targets by reading the
    store, never by trusting the steps that ran. Statuses depend on the
    target's expectation: purged (must be gone), scheduled (expired, purge
    later) or untouched (this mode leaves local derived data alone)."""
    surfaces: list[SurfaceRecord] = []
    by_mid = {p.memory_id: p for p in eligible}
    retrievable = {e.memory_id for e in store.retrievable_envelopes()}
    tombstones = store.tombstones()
    untouched_note = "request-source-erasure does not touch local derived data; compose with remove-derived"

    for p in eligible:
        mid, exp = p.memory_id, p.expectation
        assert p.rule is not None
        present = set(store.payload_files(mid))
        admitted = _admitted_versions(store, mid)
        for name in sorted(set(store.version_ids(mid)) | set(pre_files.get(mid, ())) | present):
            s = f"payload:{_ref(mid, name)}"
            tag = "" if store.payload_file_version(name) in admitted else f"{ORPHAN_LABEL}: "
            label = _payload_label(name)
            if name not in present:
                surfaces.append(SurfaceRecord(s, SurfaceKind.PAYLOAD, SurfaceStatus.VERIFIED, f"{tag}{label} file absent"))
            elif exp == _PURGED:
                surfaces.append(
                    SurfaceRecord(s, SurfaceKind.PAYLOAD, SurfaceStatus.UNVERIFIABLE, f"{tag}{label} file still present")
                )
            elif exp == _SCHEDULED:
                surfaces.append(
                    SurfaceRecord(
                        s,
                        SurfaceKind.PAYLOAD,
                        SurfaceStatus.RETAINED_BY_POLICY,
                        f"expired and non-retrievable; payload purge scheduled (purge_after={p.rule.purge_after})",
                    )
                )
            else:
                surfaces.append(SurfaceRecord(s, SurfaceKind.PAYLOAD, SurfaceStatus.RETAINED_BY_POLICY, untouched_note))

        s = f"projection:retrievable-set:{mid}"
        if mid not in retrievable:
            surfaces.append(
                SurfaceRecord(
                    s,
                    SurfaceKind.PROJECTION,
                    SurfaceStatus.VERIFIED,
                    "absent from retrievable_envelopes(), the only set evaluate_retrieval is given",
                )
            )
        elif exp == _UNTOUCHED:
            surfaces.append(SurfaceRecord(s, SurfaceKind.PROJECTION, SurfaceStatus.RETAINED_BY_POLICY, untouched_note))
        else:
            surfaces.append(
                SurfaceRecord(s, SurfaceKind.PROJECTION, SurfaceStatus.UNVERIFIABLE, "still retrievable")
            )

        stored = [store.stored_envelope(mid, vid) for vid in store.version_ids(mid)]
        digests = sorted({env.payload_digest for env in stored if env is not None})
        surfaces.append(
            SurfaceRecord(
                f"envelope-metadata:{mid}",
                SurfaceKind.ENVELOPE_METADATA,
                SurfaceStatus.RETAINED_BY_POLICY,
                f"{len(stored)} version file(s) retained for lineage (P4); the retained "
                f"envelope carries payload_digest {', '.join(digests)}" + (f"; {p.note}" if p.note else ""),
            )
        )

        entries = [t for t in tombstones if t.get("memory_id") == mid]
        required = p.rule.tombstone_required and mode in (LifecycleMode.REMOVE_DERIVED, LifecycleMode.EXPIRE)
        if entries:
            objs = sorted({f"{t.get('source_system')}/{t.get('object_id')}" for t in entries})
            surfaces.append(
                SurfaceRecord(
                    f"tombstone:{mid}",
                    SurfaceKind.TOMBSTONE,
                    SurfaceStatus.RETAINED_BY_POLICY,
                    f"tombstone retains ids only ({', '.join(objs)}); non-retrievable; blocks re-ingestion",
                )
            )
        elif required:
            surfaces.append(
                SurfaceRecord(
                    f"tombstone:{mid}", SurfaceKind.TOMBSTONE, SurfaceStatus.UNVERIFIABLE,
                    "policy requires a tombstone; none present",
                )
            )
        else:
            surfaces.append(
                SurfaceRecord(
                    f"tombstone:{mid}", SurfaceKind.TOMBSTONE, SurfaceStatus.VERIFIED,
                    "policy requires no tombstone; tombstones.jsonl holds no entry for this memory",
                )
            )

    iprs: list[IndexPurgeReceipt] = []
    target_refs = tuple(
        sorted(_ref(mid, vid) for mid in by_mid for vid in store.version_ids(mid))
    )
    for name in PROJECTIONS:
        cur = store.current_generation(name)
        on_disk = store.generations_on_disk(name)
        negative_everywhere = True
        notes: list[str] = []
        if not on_disk:
            surfaces.append(
                SurfaceRecord(
                    f"projection:{name}:none", SurfaceKind.PROJECTION, SurfaceStatus.VERIFIED,
                    "no generation on disk",
                )
            )
        for g in on_disk:
            s = f"projection:{name}:{g}"
            label = f"{g}{' (CURRENT)' if g == cur else ''}"
            try:
                held = store.raw_refs(name, g)
            except Exception as exc:
                negative_everywhere = False
                surfaces.append(
                    SurfaceRecord(s, SurfaceKind.PROJECTION, SurfaceStatus.UNVERIFIABLE, f"{label} unreadable ({_cls(exc)})")
                )
                notes.append(f"{label} unreadable")
                continue
            hits = sorted(r for r in held if r[0] in by_mid)
            if not hits:
                surfaces.append(
                    SurfaceRecord(s, SurfaceKind.PROJECTION, SurfaceStatus.VERIFIED, f"raw scan of {label}: no row for any target")
                )
                continue
            negative_everywhere = False
            held_refs = ", ".join(_ref(*r) for r in hits)
            exps = {by_mid[m].expectation for m, _v in hits}
            if exps == {_UNTOUCHED}:
                status, why = SurfaceStatus.RETAINED_BY_POLICY, untouched_note
            elif exps == {_SCHEDULED} and g != cur:
                status, why = SurfaceStatus.RETAINED_BY_POLICY, "rollback generation kept while the payload purge is scheduled"
            else:
                status, why = SurfaceStatus.UNVERIFIABLE, "deletion not achieved"
            surfaces.append(SurfaceRecord(s, SurfaceKind.PROJECTION, status, f"{label} still holds {held_refs}: {why}"))
            notes.append(f"{label} holds {held_refs}")
        before = gens[name][0] if name in gens else cur
        evidence = (
            f"raw scan of every generation on disk {list(on_disk)}: "
            + ("negative for every target" if negative_everywhere else "; ".join(notes))
        )
        if rebuild_error:
            evidence += f"; rebuild raised ({rebuild_error}), CURRENT left on {cur}"
        elif mode is LifecycleMode.REQUEST_SOURCE_ERASURE:
            evidence += "; this mode does not rebuild projections"
        iprs.append(IndexPurgeReceipt(name, before, cur, target_refs, negative_everywhere, evidence))

    if torn_event_tail or store.event_log_torn_tail():
        surfaces.append(
            SurfaceRecord(
                "event-log",
                SurfaceKind.EVENT_LOG,
                SurfaceStatus.UNVERIFIABLE,
                "torn trailing line (crash residue): some earlier append did not complete; the fold ignores "
                "it and the next append starts on a fresh line",
            )
        )
    else:
        surfaces.append(
            SurfaceRecord(
                "event-log",
                SurfaceKind.EVENT_LOG,
                SurfaceStatus.RETAINED_BY_POLICY,
                "append-only lifecycle events retained for lineage (P4); records carry ids and states, never payload text",
            )
        )
    surfaces.append(_receipts_surface())
    return _Evidence(surfaces, iprs)


def _receipts_surface() -> SurfaceRecord:
    """The receipts directory is a store surface too. It is retained (P4, and
    report §13: failed ops keep their receipts for repair); it holds no
    content because receipts are built from ids, digests and closed codes."""
    return SurfaceRecord(
        "receipts", SurfaceKind.RECEIPTS, SurfaceStatus.RETAINED_BY_POLICY,
        f"{RECEIPTS_EVIDENCE}; so do the op-request journals (ops/); retained for lineage and repair (P4, report §13)",
    )


def _run_local_forget(
    store: MemoryStore,
    eligible: Sequence[_TargetPlan],
    mode: LifecycleMode,
    *,
    operation_id: str,
    sources: Mapping[str, SourceAdapter],
    now: str,
    fault: Callable[[str], None],
    progress: _Progress,
    torn_event_tail: bool,
) -> _Evidence:
    """remove-derived / expire, in the crash-safe order: state event FIRST,
    purge every version's payload, tombstone, rebuild + drop non-current
    generations, then post-verify. Each step filters by state, so re-running
    completes whatever a crashed or incomplete attempt left behind."""
    step = mode.value
    refs: dict[tuple[str, str], SourceRef] = {}
    for p in eligible:
        for ref in _memory_source_refs(store, p.memory_id):
            refs.setdefault(_src_key(ref), ref)
    before = {k: _read_source(sources.get(ref.source_system), ref) for k, ref in refs.items()}
    pre_files = {p.memory_id: store.payload_files(p.memory_id) for p in eligible}

    # 1. the state change, first: non-retrievable from this instant
    progress.at("event", SurfaceKind.EVENT_LOG, mutates=True)
    transitions = [
        {"memory_id": p.memory_id, "version_id": p.head.version_id, "to": p.transition.value}  # type: ignore[union-attr]
        for p in eligible
        if p.transition is not None
    ]
    if transitions:
        store.append_event(
            "forget-derived" if mode is LifecycleMode.REMOVE_DERIVED else "decay",
            operation_id=operation_id,
            now=now,
            transitions=transitions,
            mode=mode.value,
        )
    fault(f"{step}:after-event")

    # 2. purge the payload of EVERY version (SUPERSEDED ones stay SUPERSEDED)
    progress.at("payload-purge", SurfaceKind.PAYLOAD, mutates=True)
    purged: list[dict[str, str]] = []
    for p in eligible:
        if p.expectation != _PURGED:
            continue
        for name in store.payload_files(p.memory_id):
            if store.purge_payload_file(p.memory_id, name) and is_safe_id(name):
                purged.append(
                    {"memory_id": p.memory_id, "version_id": store.payload_file_version(name), "file": name}
                )
    if purged:
        store.append_event(
            "payload-purged", operation_id=operation_id, now=now, payload_purged=purged, mode=mode.value
        )
    fault(f"{step}:after-payload-purge")

    # 3. tombstone every source object of the memory, if policy requires it
    progress.at("tombstone", SurfaceKind.TOMBSTONE, mutates=True)
    for p in eligible:
        assert p.rule is not None and p.head is not None
        if not p.rule.tombstone_required:
            continue
        for ref in _memory_source_refs(store, p.memory_id):
            if not store.is_tombstoned(ref.source_system, ref.object_id, memory_id=p.memory_id):
                store.add_tombstone(
                    memory_id=p.memory_id,
                    source_system=ref.source_system,
                    object_id=ref.object_id,
                    operation_id=operation_id,
                    now=now,
                    policy_revision=p.head.policy_revision,
                    forgetting_policy=p.head.forgetting_policy,
                )
    fault(f"{step}:after-tombstone")

    # 4. rebuild; then no rollback generation may resurrect purged content
    progress.at("rebuild", SurfaceKind.PROJECTION, mutates=True)
    gens: dict[str, tuple[str | None, str]] = {}
    rebuild_error = ""
    try:
        gens = store.rebuild_projections(_fault=fault)
    except Exception as exc:
        rebuild_error = _exc_evidence(exc)
    if any(p.expectation == _PURGED for p in eligible):
        store.delete_noncurrent_generations()
    fault(f"{step}:after-rebuild")

    # 5. post-verify every surface; the receipt is written only after this
    progress.at("post-verify", SurfaceKind.ENVELOPE_METADATA)
    evidence = _verify_local(store, eligible, mode, pre_files, gens, rebuild_error, torn_event_tail)
    for k, ref in refs.items():
        evidence.surfaces.append(
            _unchanged_surface(ref, before[k], _read_source(sources.get(ref.source_system), ref))
        )
    evidence.surfaces.extend(_store_residue_surfaces(store))
    return evidence


def _run_source_erasure(
    store: MemoryStore,
    eligible: Sequence[_TargetPlan],
    *,
    operation_id: str,
    authority_ref: str,
    sources: Mapping[str, SourceAdapter],
    progress: _Progress,
    torn_event_tail: bool,
) -> _Evidence:
    """Ask each owning source to erase under its own policy and verify its
    answer (§2.7). Local derived data is NOT touched and is reported as such;
    source erasure is never inferred from local deletion."""
    progress.at("post-verify", SurfaceKind.ENVELOPE_METADATA)
    evidence = _verify_local(
        store, eligible, LifecycleMode.REQUEST_SOURCE_ERASURE,
        {p.memory_id: store.payload_files(p.memory_id) for p in eligible}, {}, "", torn_event_tail,
    )
    refs: dict[tuple[str, str], SourceRef] = {}
    for p in eligible:
        for ref in _memory_source_refs(store, p.memory_id):
            refs.setdefault(_src_key(ref), ref)
    progress.at("source-request", SurfaceKind.SOURCE, mutates=True)
    for k, ref in refs.items():
        evidence.surfaces.append(
            _source_request_surface(
                erasure=True,
                ref=ref,
                adapter=sources.get(ref.source_system),
                request_id=f"{operation_id}:source-erasure:{_src_name(ref)}",
                authority_ref=authority_ref,
                prior_receipt=_prior_verified_erasure(store, operation_id, k),
                store=store,
                operation_id=operation_id,
            )
        )
    evidence.surfaces.extend(_store_residue_surfaces(store))
    return evidence


def _prior_verified_erasure(store: MemoryStore, operation_id: str, key: tuple[str, str]) -> str | None:
    """The file name of an earlier receipt of THIS operation that validates
    and records the erasure of source object ``key`` as verified, else None.
    Validation is integrity only (the digest is unkeyed), so the evidence
    says the erasure is ATTRIBUTED to that receipt, not proven by it. Receipts of other operations, and receipts that fail
    validation, are never believed."""
    for path in store.receipt_paths(operation_id):
        try:
            r = forget_receipt_from_json(json.loads(path.read_bytes()))
        except (OSError, ValueError, SchemaError):
            continue
        if (
            r.operation_id == operation_id
            and r.mode == LifecycleMode.REQUEST_SOURCE_ERASURE.value
            and validate_forget_receipt(r).allowed
            and any(
                s.surface.startswith(SOURCE_ERASURE_PREFIX)
                and _parse_src_name(s.surface[len(SOURCE_ERASURE_PREFIX):]) == key
                and s.status == SurfaceStatus.VERIFIED
                for s in r.surfaces
            )
        ):
            return path.name
    return None


def _destroy_store(
    finish: Callable[..., ForgetReceipt],
    *,
    store: MemoryStore,
    operation_id: str,
    request_digest: Callable[[Sequence[PolicyPin]], str],
    reused_or_finished: Callable[[str], ForgetReceipt | None],
    has_scope: bool,
    authority_ref: str,
    lifecycle_policies: object,
    archive_root: object,
    pin_store: object,
    revision: object,
    policy_name: object,
    progress: _Progress,
) -> ForgetReceipt:
    """Tear down a whole per-agent pin store via ``memory_store_teardown``.

    The rule is resolved from
    ``lifecycle_policies[pin_store_policy_revision]
    .forgetting_policies[pin_store_forgetting_policy]``: a pin store carries no
    envelope to name its policy, so the caller names it and resolution stays
    deny-by-default. ``teardown()`` itself does not check the target's shape
    (its CLI does), so ``looks_like_pin_store`` is checked here first, and a
    non-pin-store path is refused with the directory left intact.
    """
    M = LifecycleRefusal
    if has_scope:
        return finish(refusal=M.MALFORMED_INPUT, detail="destroy-store tears down a whole pin store; it takes no memory scope")
    if pin_store is None or archive_root is None:
        return finish(refusal=M.MALFORMED_INPUT, detail="destroy-store requires pin_store and archive_root")
    if not isinstance(revision, str) or not revision or not isinstance(policy_name, str) or not policy_name:
        return finish(
            refusal=M.UNKNOWN_LIFECYCLE_POLICY,
            detail="destroy-store requires pin_store_policy_revision and pin_store_forgetting_policy",
        )
    policy, why = resolve_lifecycle_policy(revision, lifecycle_policies)
    if why is not None or policy is None:
        return finish(refusal=why.code if why else M.UNKNOWN_LIFECYCLE_POLICY, detail=why.detail if why else "")
    finish = functools.partial(finish, pins=[policy_pin(policy)])
    rule = policy.forgetting_policies.get(policy_name)
    if rule is None:
        return finish(refusal=M.UNKNOWN_LIFECYCLE_POLICY, detail=f"forgetting policy {policy_name!r} unknown")
    if LifecycleMode.DESTROY_STORE not in rule.allowed_modes:
        return finish(refusal=M.MODE_NOT_ALLOWED, detail=f"{policy_name!r} does not allow destroy-store")
    if not authority_ref or authority_ref != rule.required_authority:
        return finish(refusal=M.AUTHORITY_MISMATCH, detail=f"{policy_name!r} requires a different authority_ref")
    digest = request_digest([policy_pin(policy)])
    early = reused_or_finished(digest)  # before the shape check: a finished teardown left no pin store
    if early is not None:
        return early
    pin = Path(str(pin_store))
    if not pin.is_dir() or not memory_store_teardown.looks_like_pin_store(pin):
        return finish(
            refusal=M.MALFORMED_INPUT,
            detail="not a pin store (no MEMORY.md, no <type>_<slug>.md pin): teardown refused, directory left intact",
        )
    _bind_op_request(store, operation_id, "forget", digest)
    archive: Path | None = None
    error = ""
    progress.at("teardown", SurfaceKind.STORE, mutates=True)
    try:
        archive = memory_store_teardown.teardown(pin, Path(str(archive_root)))
    except Exception as exc:
        error = _exc_evidence(exc)
    surfaces: list[SurfaceRecord] = []
    if pin.exists():
        surfaces.append(
            SurfaceRecord(
                f"store:{pin}", SurfaceKind.STORE, SurfaceStatus.UNVERIFIABLE,
                f"store still present ({error or 'teardown returned but the directory remains'}); store kept",
            )
        )
    else:
        surfaces.append(
            SurfaceRecord(f"store:{pin}", SurfaceKind.STORE, SurfaceStatus.VERIFIED, "directory absent after teardown")
        )
    if archive is not None and archive.is_file():
        surfaces.append(
            SurfaceRecord(
                f"backup:archive:{archive}", SurfaceKind.BACKUP, SurfaceStatus.RETAINED_BY_POLICY,
                f"teardown archive retained at {archive} (audit-preserving, P4)",
            )
        )
    else:
        surfaces.append(
            SurfaceRecord(
                "backup:archive", SurfaceKind.BACKUP, SurfaceStatus.UNVERIFIABLE,
                f"no archive returned ({error or 'archive missing'})",
            )
        )
    surfaces.append(
        SurfaceRecord("backup:off-box", SurfaceKind.BACKUP, SurfaceStatus.UNVERIFIABLE, OFF_BOX_BACKUP_EVIDENCE)
    )
    surfaces.append(
        SurfaceRecord(
            "media-residue", SurfaceKind.MEDIA_RESIDUE, SurfaceStatus.UNVERIFIABLE,
            "teardown unlinked pin-store files; no manifest declares that medium scrubbed; no physical scrub claimed",
        )
    )
    surfaces.append(_receipts_surface())
    return finish(surfaces=surfaces)


def forget(
    store: MemoryStore,
    *,
    operation_id: str,
    mode: LifecycleMode | str,
    subject_scope: Sequence[str] = (),
    target_ids: Sequence[str] = (),
    authority_ref: str,
    lifecycle_policies: Mapping[str, LifecyclePolicy],
    sources: Mapping[str, SourceAdapter] = _NO_SOURCES,
    now: str,
    archive_root: Path | str | None = None,
    pin_store: Path | str | None = None,
    pin_store_policy_revision: str | None = None,
    pin_store_forgetting_policy: str | None = None,
    _fault: Callable[[str], None] | None = None,
) -> ForgetReceipt:
    """Forget per ``mode`` and return a receipt that enumerates every surface.

    Scope is ``target_ids`` UNION every memory with a subject in
    ``subject_scope``. Each target resolves its own lifecycle policy (by its
    head version's ``policy_revision``), checks that the mode is allowed, and
    checks the authority. A refused target becomes ``refused`` surfaces while
    the others proceed; if every target is refused, the receipt is refused.
    ``destroy-store`` instead takes ``pin_store``/``archive_root`` and names its
    rule with ``pin_store_policy_revision`` + ``pin_store_forgetting_policy``.
    ``_fault`` is a test seam called at named steps
    (``"<mode>:after-event"``, ``"<mode>:after-payload-purge"``,
    ``"<mode>:after-tombstone"``, ``"<mode>:after-rebuild"``, and the store's
    ``"build:<projection>:before-manifest"`` / ``"publish:before-swap"``).
    Raise ``SimulatedCrash`` from it to simulate a process crash: it bypasses
    every handler and nothing is written. Any REAL exception (``OSError`` or
    otherwise) is caught: an exception inside the rebuild is reported against
    the projections, and one anywhere else yields an ``incomplete`` receipt
    whose failing surface is ``unverifiable`` with evidence = exception class
    + errno only. The first receipt of an ``operation_id`` is never
    overwritten; re-runs land as ``+attempt-<n>``.
    """
    M = LifecycleRefusal
    fault = _fault or _no_fault
    mode_value = mode.value if isinstance(mode, LifecycleMode) else str(mode)
    scope_t = _str_seq(subject_scope)
    ids_t = _str_seq(target_ids)

    def finish(
        *,
        targets: tuple[str, ...] = ids_t or (),
        surfaces: Sequence[SurfaceRecord] = (),
        iprs: Sequence[IndexPurgeReceipt] = (),
        refusal: LifecycleRefusal | None = None,
        detail: str = "",
        pins: Sequence[PolicyPin] = (),
        emit: bool = True,
    ) -> ForgetReceipt:
        r = _forget_receipt(
            operation_id=str(operation_id),
            subject_scope=scope_t or (),
            target_ids=targets,
            mode=mode_value,
            projection_purge_receipts=iprs,
            authority_ref=str(authority_ref),
            completed_at=str(now),
            surfaces=surfaces,
            lifecycle_policy_pins=pins,
            refusal=refusal,
            refusal_detail=detail,
        )
        return _emit(store, r, forget_receipt_to_json) if emit else r  # type: ignore[return-value]

    if not isinstance(store, MemoryStore) or not is_safe_id(operation_id):
        return finish(refusal=M.MALFORMED_INPUT, detail="need a MemoryStore and a safe operation_id")
    if not isinstance(now, str) or not now or not isinstance(authority_ref, str):
        return finish(refusal=M.MALFORMED_INPUT, detail="now and authority_ref must be strings")
    if scope_t is None or ids_t is None or any(not is_safe_id(t) for t in ids_t):
        return finish(targets=(), refusal=M.MALFORMED_INPUT, detail="subject_scope/target_ids must be lists of ids")
    # Every input is type-checked HERE, before the lock: a wrong-typed input
    # is a typed refusal, never a fault inside the op.
    problem = _sources_problem(sources)
    if problem is None and not isinstance(lifecycle_policies, Mapping):
        problem = "lifecycle_policies must map revision -> LifecyclePolicy"
    if problem is None and _fault is not None and not callable(_fault):
        problem = "_fault must be callable or None"
    if problem is None and any(
        v is not None and not isinstance(v, (str, os.PathLike)) for v in (archive_root, pin_store)
    ):
        problem = "archive_root / pin_store must be a path or None"
    if problem is None and any(
        v is not None and not isinstance(v, str) for v in (pin_store_policy_revision, pin_store_forgetting_policy)
    ):
        problem = "pin_store_policy_revision / pin_store_forgetting_policy must be strings or None"
    if problem is not None:
        return finish(refusal=M.MALFORMED_INPUT, detail=problem)
    try:
        lm = LifecycleMode(mode)
    except ValueError:
        return finish(refusal=M.MALFORMED_INPUT, detail=f"unknown mode {mode_value!r}")
    if lm in CORRECT_MODES:
        return finish(refusal=M.MODE_NOT_OWNED, detail=f"{lm.value} is owned by correct(), not forget()")
    progress = _Progress(lm)
    targets: tuple[str, ...] = ids_t
    refused: list[SurfaceRecord] = []
    pins: list[PolicyPin] = []

    def request_digest(policy_pins: Sequence[PolicyPin]) -> str:
        """The op's request; a re-run of this op_id must be exactly it."""
        return _request_digest(
            {
                "op": "forget",
                "mode": lm.value,
                "target_ids": list(ids_t),
                "subject_scope": list(scope_t),
                "authority_ref": authority_ref,
                "policy_pins": sorted({(p.ref, p.digest) for p in policy_pins}),
                "pin_store": None if pin_store is None else str(pin_store),
                "archive_root": None if archive_root is None else str(archive_root),
                "pin_store_policy_revision": pin_store_policy_revision,
                "pin_store_forgetting_policy": pin_store_forgetting_policy,
            }
        )

    def reused_or_finished(digest: str) -> ForgetReceipt | None:
        """The reuse gate FIRST (another request under this op_id is refused),
        THEN the finished-op short-circuit (an identical re-run of a finished
        op gets its receipt back and nothing runs)."""
        why = _op_request_refusal(store, operation_id, digest)
        if why is not None:
            return finish(targets=targets, refusal=why.code, detail=why.detail)
        done, bad = _finished_receipt(store, operation_id, forget_receipt_from_json, validate_forget_receipt)
        if bad:  # no side effects: not even a receipt
            return finish(
                refusal=M.MALFORMED_INPUT,
                detail="an earlier success receipt of this operation_id fails validation",
                emit=False,
            )
        return done  # type: ignore[return-value]

    try:
        if lm is LifecycleMode.DESTROY_STORE:
            return _destroy_store(
                finish,
                store=store,
                operation_id=operation_id,
                request_digest=request_digest,
                reused_or_finished=reused_or_finished,
                has_scope=bool(scope_t or ids_t),
                authority_ref=authority_ref,
                lifecycle_policies=lifecycle_policies,
                archive_root=archive_root,
                pin_store=pin_store,
                revision=pin_store_policy_revision,
                policy_name=pin_store_forgetting_policy,
                progress=progress,
            )

        with store.locked():
            # A torn trailing event line seen at the start is reported even if
            # this op's own first append heals it (it is crash residue of some
            # earlier operation).
            torn_event_tail = store.event_log_torn_tail()
            targets = _resolve_targets(store, ids_t, scope_t)
            if not targets:
                return finish(targets=(), refusal=M.MALFORMED_INPUT, detail="empty scope: no target matched")
            plans = [_plan_target(store, mid, lm, authority_ref, lifecycle_policies, now) for mid in targets]
            for p in plans:
                if p.refusal is not None:
                    refused.extend(_refused_target_surfaces(store, p, lm))
            eligible = [p for p in plans if p.refusal is None]
            if not eligible:
                first = plans[0].refusal
                assert first is not None
                return finish(
                    targets=targets,
                    surfaces=refused,
                    refusal=first.code,
                    detail=f"every target refused (first: {first.detail})",
                )
            pins.extend(p.pin for p in eligible if p.pin is not None)
            digest = request_digest(pins)
            early = reused_or_finished(digest)
            if early is not None:
                return early
            _bind_op_request(store, operation_id, "forget", digest)
            if lm is LifecycleMode.REQUEST_SOURCE_ERASURE:
                evidence = _run_source_erasure(
                    store, eligible, operation_id=operation_id, authority_ref=authority_ref, sources=sources,
                    progress=progress, torn_event_tail=torn_event_tail,
                )
            else:
                evidence = _run_local_forget(
                    store, eligible, lm, operation_id=operation_id, sources=sources, now=now, fault=fault,
                    progress=progress, torn_event_tail=torn_event_tail,
                )
            return finish(targets=targets, surfaces=refused + evidence.surfaces, iprs=evidence.iprs, pins=pins)
    except Exception as exc:  # a REAL fault; SimulatedCrash (BaseException) is not caught
        return finish(targets=targets, surfaces=refused + _fault_surfaces(progress, exc), pins=pins)


__all__ = [
    "CORRECTION_RECEIPT_SCHEMA_VERSION",
    "FORGET_RECEIPT_SCHEMA_VERSION",
    "SOURCE_STATUS_UNCHANGED",
    "LifecycleMode",
    "LifecycleRefusal",
    "SurfaceStatus",
    "SurfaceKind",
    "CORRECTABLE_FIELDS",
    "REQUIRED_SURFACE_KINDS",
    "CorrectionReason",
    "LifecycleOutcome",
    "ForgettingRule",
    "LifecyclePolicy",
    "PolicyPin",
    "lifecycle_policy_digest",
    "make_lifecycle_policy",
    "resolve_lifecycle_policy",
    "SourceReceipt",
    "SourceOutcome",
    "SourceAdapter",
    "ReadOnlyCanonSource",
    "FileSource",
    "verify_source_request",
    "stuck_source_requests",
    "SimulatedCrash",
    "SurfaceRecord",
    "IndexPurgeReceipt",
    "CorrectionReceipt",
    "ForgetReceipt",
    "derive_outcome",
    "derive_disposition",
    "derive_source_status",
    "unverifiable_surface_names",
    "correction_receipt_to_json",
    "forget_receipt_to_json",
    "correction_receipt_from_json",
    "forget_receipt_from_json",
    "compute_correction_receipt_digest",
    "compute_forget_receipt_digest",
    "validate_correction_receipt",
    "validate_forget_receipt",
    "correct",
    "forget",
    "RECLASSIFIABLE_FIELDS",
    "RECLASSIFY_SEMANTIC_FIELDS",
    "RECLASSIFY_RECEIPT_SCHEMA_VERSION",
    "WIDENING_AUTHORITY_RE",
    "ReclassifyReceipt",
    "reclassify_direction",
    "reclassify",
    "reclassify_receipt_to_json",
    "reclassify_receipt_from_json",
    "compute_reclassify_receipt_digest",
    "validate_reclassify_receipt",
]
