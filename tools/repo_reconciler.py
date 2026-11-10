"""tools/repo_reconciler.py — T-2951: the declarative repository-lifecycle reconciler.

WHAT THIS OWNS (ORG-053 §6 cradle-to-grave; ORG-080 plan/apply/verify/rollback +
convergent bringup; ORG-046 logical identity; ORG-063/ORG-052 Action Broker;
ORG-064 declarative-spec reconciler safety). Given the declarative `checkouts/`
surface (`checkout_spec.py`), this module PLANS what each managed checkout needs
to converge to its desired state, and — behind a guarded-inert gate — APPLIES the
non-destructive subset, VERIFIES the result, and can ROLL BACK its own installs.
It is the real implementation behind the `RepositoryLifecyclePlanner` Protocol seam
that `assembly_resolver.py` (T-3182) already composes against.

SAFETY POSTURE — the non-negotiables (plan §"Reconciler algorithm", §DECISIONS):
  - `plan()` is READ-ONLY + privilege-free: it probes on-disk state and issues a
    read-only `git ls-remote` (remote-side query, no local mutation). It never
    fetches, merges, resets, cleans, or checks out. Divergence is read from the
    LOCAL objects/remote-tracking refs the checkout already holds; the freshness of
    those refs is the caller's fetch cadence's business, not plan()'s.
  - DIRTY / DIVERGED / FOREIGN / MISSING_REF / CREDENTIAL / HEALTH are REFUSALS on
    the shared `RefusalKind` surface — never a reset/clean/force/rewind. FOREIGN
    content (a non-managed repo, or non-repo content, at a target path) is REPORTED,
    NEVER reaped: hard refusal, not a flag (ORG-080 §1 per-op privilege + the Ask ownership
    boundary).
  - The disruptive restart is a guarded-inert brokered INTENT (ORG-052 op-family
    shape): emitted into the plan/receipt, NEVER dispatched in phase 2 (the broker
    D-Bus/MCP exposure is unbuilt, T-2652). Every real-apply path requires BOTH a
    FUNCTIONAL probe and an exact one-checkout authorization, so a functional
    fixture/caller alone cannot gain mutation.
  - Rollback/removal never delete. An exactly-owned clone is atomically renamed
    into a receipt-specific private quarantine; unknown data is never moved.

COMPOSITION (T-3182, option a). `RepoReconciler.plan(sources: Sequence[LockedComponent])
-> LifecyclePlan` satisfies the authored Protocol signature exactly, so
`assembly_resolver.plan_assembly_update` composes unchanged: each `LockedComponent`
(what revision is wanted, keyed by `logical_repo`) is joined against the declarative
checkout-spec (where/how/policy), the plan algorithm runs, and the result collapses
to `LifecyclePlan(ok, stages, refusal)` at the boundary. The module's own richer
per-checkout view lives on `ReconcilePlan` (`reconcile_plan()`).

SEAMS (all injected; fixture-stubbed in tests):
  - `probe`          — read-only on-disk observer (`GitCheckoutProbe` is the real one).
  - `remote_resolver`— `logical_repo -> remote url`; the ORG-046 seam. Wrap
    `remote_resolve.resolve_remote` in production; a resolver failure is an
    `UNRESOLVED_SOURCE` refusal.
  - `health_probe`   — `(entry) -> bool`; runs a checkout's health_contract. Default:
    inert (returns True; health not run — the phase-1 plan-only posture).
  - `broker`         — the ORG-063 Action Broker (`available()` + `dispatch(intent)`).
    Default: absent → intents are emitted, never dispatched.
  - `functional_probe`— gates real git mutation in `apply`. Default `lambda: False`
    (inert). This is the guarded-inert gate.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# The supervised operator surface invokes this file both as a module and as
# ``python3 tools/repo_reconciler.py``.  The latter puts ``tools/`` rather than
# the repository parent on sys.path, so establish the package root before the
# module-scope ``tools.*`` imports below.  Resolve from __file__; never trust cwd.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.assembly_resolver import (
    ROLLBACK_STAGES,
    UPDATE_STAGES,
    CompatibilityRefusal,
    LifecyclePlan,
    LockedComponent,
    RefusalKind,
)
from tools.checkout_spec import (
    UPDATE_POLICY_AVAILABILITY_ONLY,
    UPDATE_POLICY_FF_ONLY,
    UPDATE_POLICY_NEVER,
    CheckoutEntry,
    CheckoutSpec,
    RepositoryLifecycleError,
)
from tools.component_catalogue import _json_bytes

# ---------------------------------------------------------------------------
# Plan states (the on-disk classification) + their shared-surface refusal kinds
# ---------------------------------------------------------------------------

STATE_ABSENT = "absent"
STATE_CURRENT = "current"
STATE_BEHIND = "behind"
STATE_DIRTY = "dirty"
STATE_DIVERGED = "diverged"
STATE_MISSING_REF = "missing-ref"
STATE_CREDENTIAL_FAIL = "credential-fail"
STATE_HEALTH_FAIL = "health-fail"
STATE_FOREIGN = "foreign"
STATE_UNRESOLVED = "unresolved-source"

# Every refusing plan-state maps to exactly one shared RefusalKind — one uniform
# failure surface across the assembly + reconciler planes (plan §DECISIONS).
_STATE_REFUSAL: dict[str, RefusalKind] = {
    STATE_DIRTY: RefusalKind.DIRTY_CHECKOUT,
    STATE_DIVERGED: RefusalKind.DIVERGED_CHECKOUT,
    STATE_FOREIGN: RefusalKind.FOREIGN_CHECKOUT,
    STATE_MISSING_REF: RefusalKind.MISSING_REF,
    STATE_CREDENTIAL_FAIL: RefusalKind.CREDENTIAL_FAILURE,
    STATE_HEALTH_FAIL: RefusalKind.HEALTH_FAILURE,
    STATE_UNRESOLVED: RefusalKind.UNRESOLVED_SOURCE,
}

ACTION_CLONE = "clone"
ACTION_FF_ADVANCE = "ff-advance"
ACTION_NOOP = "noop"

AUTHORIZATION_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 3


class ScopedAuthorizationError(ValueError):
    """An apply/receipt artifact is absent, stale, widened, or malformed."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _remote_digest(url: str) -> str:
    return _sha256_bytes(_normalize_url(url).encode("utf-8"))


def _unsafe_git_operand(value: str) -> bool:
    """True when a caller-controlled operand could be parsed as a git option."""

    return not value or value.startswith("-") or "\0" in value


def _unsafe_entry_operands(entry: CheckoutEntry) -> bool:
    """True when a manifest-sourced path or tracking branch could read as a git option.

    Defence in depth beside the `--` call-site delimiters (T-5502): `-C <path>`
    and `--branch <name>` take their value positionally, so `--` is not usable
    there and the leading-`-` refusal is the only layer. Pinned shas are
    already constrained to hex by the spec loader."""

    return _unsafe_git_operand(entry.target_path) or (
        entry.mode.tracking is not None and _unsafe_git_operand(entry.mode.tracking)
    )


def _filesystem_identity(path: Path) -> dict[str, int] | None:
    """Return the directory-entry identity used to prove rollback ownership.

    Device+inode distinguish the exact installed directory from a later clean
    clone placed at the same pathname. ``lstat`` deliberately observes the path
    entry itself rather than following a replacement symlink.
    """

    try:
        observed = os.lstat(path)
    except OSError:
        return None
    return {"device": observed.st_dev, "inode": observed.st_ino}


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename without replacing an existing destination (Linux)."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------

RemoteResolver = Callable[[str], str]
HealthProbe = Callable[[CheckoutEntry], bool]


@dataclass(frozen=True)
class CheckoutObservation:
    """A read-only observation of one checkout's on-disk + remote-side state.

    Produced by a `CheckoutProbe`; consumed by the pure `_classify` function.
    """

    remote_reachable: bool
    desired_present: bool  # the desired ref/sha exists on the remote (or locally, pinned)
    exists: bool  # target path exists AND is non-empty
    is_managed: bool  # a git repo whose origin identity matches the resolved remote
    dirty: bool
    head_sha: str | None
    ahead: int  # local commits not reachable from the desired target
    behind: int  # desired-target commits not reachable from local HEAD (ff available)
    desired_resolvable: bool  # the desired target is present in the local object store


class CheckoutProbe(Protocol):
    """Read-only observer seam. `observe` NEVER mutates local state."""

    def observe(self, entry: CheckoutEntry, remote_url: str) -> CheckoutObservation: ...


@dataclass(frozen=True)
class BrokeredIntent:
    """A guarded-inert ORG-052 op-family intent. Emitted into the plan/receipt;
    `dispatched` is always False in phase 2 (the broker exposure is unbuilt)."""

    checkout_id: str
    operation_family: str
    target_unit: str
    dispatched: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "checkout_id": self.checkout_id,
            "operation_family": self.operation_family,
            "target_unit": self.target_unit,
            "dispatched": self.dispatched,
        }


class ActionBroker(Protocol):
    """Future ORG-063 seam. Phase 2 records intents but calls neither method."""

    def available(self) -> bool: ...

    def dispatch(self, intent: BrokeredIntent) -> None: ...


# ---------------------------------------------------------------------------
# Plan / receipt data model (mirrors scheduler_reconcile PlanItem/PlanResult)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcilePlanItem:
    checkout_id: str
    logical_repo: str
    target_path: str
    state: str
    action: str
    detail: str = ""
    refusal: CompatibilityRefusal | None = None
    brokered_intent: BrokeredIntent | None = None
    precondition_digest: str = ""
    remote_digest: str = ""
    desired_sha: str | None = None
    observed_head: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "checkout_id": self.checkout_id,
            "logical_repo": self.logical_repo,
            "target_path": self.target_path,
            "state": self.state,
            "action": self.action,
            "detail": self.detail,
            "refusal": self.refusal.as_text() if self.refusal is not None else None,
            "brokered_intent": (
                self.brokered_intent.to_dict() if self.brokered_intent is not None else None
            ),
            "precondition_digest": self.precondition_digest,
            "remote_digest": self.remote_digest,
            "desired_sha": self.desired_sha,
            "observed_head": self.observed_head,
        }


@dataclass(frozen=True)
class ReconcilePlan:
    items: tuple[ReconcilePlanItem, ...]

    def refusals(self) -> tuple[CompatibilityRefusal, ...]:
        return tuple(
            item.refusal for item in self.items if item.refusal is not None
        )

    @property
    def ok(self) -> bool:
        return not self.refusals()

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.items:
            out[item.state] = out.get(item.state, 0) + 1
        return out

    def to_report_bytes(self) -> bytes:
        """Deterministic report bytes — stable across checkout ordering (items are
        sorted by checkout_id before rendering)."""
        return _json_bytes(
            {
                "schema_version": 1,
                "items": [
                    item.to_dict()
                    for item in sorted(self.items, key=lambda i: i.checkout_id)
                ],
            }
        )


@dataclass
class ApplyReceipt:
    """What an `apply` did (or, when inert, WOULD do). Records reconciler-installed
    content so rollback/removal can quarantine exactly it and nothing else."""

    materialized: bool
    installs: list[str] = field(default_factory=list)  # checkout_ids cloned
    advances: list[str] = field(default_factory=list)  # checkout_ids ff-advanced
    intents: list[BrokeredIntent] = field(default_factory=list)
    dispatched: list[BrokeredIntent] = field(default_factory=list)
    refused: list[CompatibilityRefusal] = field(default_factory=list)
    install_paths: dict[str, str] = field(default_factory=dict)  # checkout_id -> path
    installed_sha: dict[str, str] = field(default_factory=dict)  # checkout_id -> HEAD sha recorded at install
    installed_path_identity: dict[str, dict[str, int]] = field(default_factory=dict)
    schema_version: int = RECEIPT_SCHEMA_VERSION
    authorization_digest: str = ""
    checkout_id: str = ""
    target_path: str = ""
    action: str = ""
    desired_sha: str | None = None
    before_digest: str = ""
    after_digest: str = ""
    rollback_supported: bool = False
    receipt_digest: str = ""
    failed_clone_paths: dict[str, str] = field(default_factory=dict)
    failed_clone_sha: dict[str, str] = field(default_factory=dict)
    failed_clone_identity: dict[str, dict[str, int]] = field(default_factory=dict)

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "materialized": self.materialized,
            "installs": sorted(self.installs),
            "advances": sorted(self.advances),
            "install_paths": dict(sorted(self.install_paths.items())),
            "installed_sha": dict(sorted(self.installed_sha.items())),
            "installed_path_identity": {
                checkout_id: dict(sorted(identity.items()))
                for checkout_id, identity in sorted(self.installed_path_identity.items())
            },
            "authorization_digest": self.authorization_digest,
            "checkout_id": self.checkout_id,
            "target_path": self.target_path,
            "action": self.action,
            "desired_sha": self.desired_sha,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "rollback_supported": self.rollback_supported,
            "failed_clone_paths": dict(sorted(self.failed_clone_paths.items())),
            "failed_clone_sha": dict(sorted(self.failed_clone_sha.items())),
            "failed_clone_identity": {
                checkout_id: dict(sorted(identity.items()))
                for checkout_id, identity in sorted(self.failed_clone_identity.items())
            },
            "intents": [i.to_dict() for i in self.intents],
            "dispatched": [i.to_dict() for i in self.dispatched],
            "refused": [
                {
                    "kind": r.kind.value,
                    "subject_id": r.subject_id,
                    "detail": r.detail,
                    "evidence_ref": r.evidence_ref,
                }
                for r in self.refused
            ],
        }

    def seal(self) -> "ApplyReceipt":
        self.receipt_digest = _sha256_bytes(_json_bytes(self._payload()))
        return self

    def to_bytes(self) -> bytes:
        if not self.receipt_digest:
            self.seal()
        return _json_bytes({**self._payload(), "receipt_digest": self.receipt_digest})

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ApplyReceipt":
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ScopedAuthorizationError("receipt is not valid JSON") from exc
        expected = {
            "schema_version", "materialized", "installs", "advances", "install_paths",
            "installed_sha", "installed_path_identity", "authorization_digest", "checkout_id", "target_path",
            "action", "desired_sha", "before_digest", "after_digest",
            "rollback_supported", "failed_clone_paths", "failed_clone_sha", "failed_clone_identity",
            "intents", "dispatched", "refused", "receipt_digest",
        }
        if not isinstance(data, dict) or set(data) != expected:
            raise ScopedAuthorizationError("receipt has unknown or missing fields")
        digest = data.pop("receipt_digest")
        if type(data.get("schema_version")) is not int or data.get("schema_version") != RECEIPT_SCHEMA_VERSION:
            raise ScopedAuthorizationError("unsupported receipt schema")
        if not isinstance(digest, str) or digest != _sha256_bytes(_json_bytes(data)):
            raise ScopedAuthorizationError("receipt digest mismatch (integrity only; not authorship)")
        typed = {
            "materialized": bool,
            "installs": list,
            "advances": list,
            "install_paths": dict,
            "installed_sha": dict,
            "installed_path_identity": dict,
            "authorization_digest": str,
            "checkout_id": str,
            "target_path": str,
            "action": str,
            "before_digest": str,
            "after_digest": str,
            "rollback_supported": bool,
            "failed_clone_paths": dict,
            "failed_clone_sha": dict,
            "failed_clone_identity": dict,
            "intents": list,
            "dispatched": list,
            "refused": list,
        }
        for key, kind in typed.items():
            if type(data[key]) is not kind:
                raise ScopedAuthorizationError(f"receipt field {key} has wrong type")
        if data["desired_sha"] is not None and type(data["desired_sha"]) is not str:
            raise ScopedAuthorizationError("receipt field desired_sha has wrong type")
        if not all(type(x) is str for x in data["installs"] + data["advances"]):
            raise ScopedAuthorizationError("receipt checkout lists must contain strings")
        if not all(type(k) is str and type(v) is str for m in (data["install_paths"], data["installed_sha"], data["failed_clone_paths"], data["failed_clone_sha"]) for k, v in m.items()):
            raise ScopedAuthorizationError("receipt maps must contain string keys/values")
        for map_name in ("installed_path_identity", "failed_clone_identity"):
            identity_map = data[map_name]
            for checkout_id, identity in identity_map.items():
                if type(checkout_id) is not str or type(identity) is not dict:
                    raise ScopedAuthorizationError(f"receipt {map_name} has wrong type")
                if set(identity) != {"device", "inode"}:
                    raise ScopedAuthorizationError(f"receipt {map_name} is malformed")
                if any(type(identity[field]) is not int or identity[field] < 0 for field in ("device", "inode")):
                    raise ScopedAuthorizationError(f"receipt {map_name} has wrong field type")

        def intent(raw: object) -> BrokeredIntent:
            if not isinstance(raw, dict) or set(raw) != {"checkout_id", "operation_family", "target_unit", "dispatched"}:
                raise ScopedAuthorizationError("receipt intent malformed")
            if not all(type(raw[k]) is str for k in ("checkout_id", "operation_family", "target_unit")) or type(raw["dispatched"]) is not bool:
                raise ScopedAuthorizationError("receipt intent has wrong field type")
            return BrokeredIntent(**raw)

        def refusal(raw: object) -> CompatibilityRefusal:
            if not isinstance(raw, dict) or set(raw) != {"kind", "subject_id", "detail", "evidence_ref"}:
                raise ScopedAuthorizationError("receipt refusal malformed")
            if type(raw["kind"]) is not str or type(raw["subject_id"]) is not str or type(raw["detail"]) is not str:
                raise ScopedAuthorizationError("receipt refusal has wrong field type")
            if raw["evidence_ref"] is not None and type(raw["evidence_ref"]) is not str:
                raise ScopedAuthorizationError("receipt refusal evidence has wrong type")
            try:
                kind = RefusalKind(raw["kind"])
            except ValueError as exc:
                raise ScopedAuthorizationError("receipt refusal kind unknown") from exc
            return CompatibilityRefusal(kind, raw["subject_id"], raw["detail"], raw["evidence_ref"])

        receipt = cls(
            materialized=data["materialized"],
            installs=list(data["installs"]),
            advances=list(data["advances"]),
            install_paths=dict(data["install_paths"]),
            installed_sha=dict(data["installed_sha"]),
            installed_path_identity={
                checkout_id: dict(identity)
                for checkout_id, identity in data["installed_path_identity"].items()
            },
            schema_version=int(data["schema_version"]),
            authorization_digest=str(data["authorization_digest"]),
            checkout_id=str(data["checkout_id"]),
            target_path=str(data["target_path"]),
            action=str(data["action"]),
            desired_sha=data["desired_sha"],
            before_digest=str(data["before_digest"]),
            after_digest=str(data["after_digest"]),
            rollback_supported=data["rollback_supported"],
            receipt_digest=digest,
            failed_clone_paths=dict(data["failed_clone_paths"]),
            failed_clone_sha=dict(data["failed_clone_sha"]),
            failed_clone_identity={
                checkout_id: dict(identity)
                for checkout_id, identity in data["failed_clone_identity"].items()
            },
            intents=[intent(x) for x in data["intents"]],
            dispatched=[intent(x) for x in data["dispatched"]],
            refused=[refusal(x) for x in data["refused"]],
        )
        return receipt


@dataclass(frozen=True)
class RollbackOutcome:
    checkout_id: str
    success: bool
    quarantine_path: str | None = None
    resumed: bool = False
    refusals: tuple[CompatibilityRefusal, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "checkout_id": self.checkout_id,
            "success": self.success,
            "quarantine_path": self.quarantine_path,
            "resumed": self.resumed,
            "refusals": [refusal.as_text() for refusal in self.refusals],
        }


def _quarantine_paths(receipt: ApplyReceipt, target: Path) -> tuple[Path, Path]:
    if not receipt.receipt_digest:
        raise ScopedAuthorizationError("receipt must be sealed before quarantine path derivation")
    container = target.parent / f".repo-reconciler-quarantine-{receipt.receipt_digest}"
    return container, container / "payload"


@dataclass(frozen=True)
class ScopedCheckoutAuthorization:
    """Integrity-bound capability for exactly one already-planned checkout.

    The digest detects accidental/tampered bytes. It is deliberately not a MAC,
    signature, authentication claim, or authorship claim: it is recomputable.
    """

    checkout_id: str
    logical_repo: str
    target_path: str
    action: str
    desired_sha: str | None
    plan_digest: str
    precondition_digest: str
    remote_digest: str
    schema_version: int = AUTHORIZATION_SCHEMA_VERSION
    authorization_digest: str = ""

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "checkout_id": self.checkout_id,
            "logical_repo": self.logical_repo,
            "target_path": self.target_path,
            "action": self.action,
            "desired_sha": self.desired_sha,
            "plan_digest": self.plan_digest,
            "precondition_digest": self.precondition_digest,
            "remote_digest": self.remote_digest,
        }

    def sealed(self) -> "ScopedCheckoutAuthorization":
        return ScopedCheckoutAuthorization(
            **{k: v for k, v in self._payload().items() if k != "schema_version"},
            schema_version=self.schema_version,
            authorization_digest=_sha256_bytes(_json_bytes(self._payload())),
        )

    def to_bytes(self) -> bytes:
        sealed = self if self.authorization_digest else self.sealed()
        return _json_bytes({**sealed._payload(), "authorization_digest": sealed.authorization_digest})

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ScopedCheckoutAuthorization":
        try:
            data: Any = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ScopedAuthorizationError("authorization is not valid JSON") from exc
        expected = {
            "schema_version", "checkout_id", "logical_repo", "target_path", "action",
            "desired_sha", "plan_digest", "precondition_digest", "remote_digest",
            "authorization_digest",
        }
        if not isinstance(data, dict) or set(data) != expected:
            raise ScopedAuthorizationError("authorization has unknown or missing fields")
        digest = data.pop("authorization_digest")
        if type(data.get("schema_version")) is not int or data.get("schema_version") != AUTHORIZATION_SCHEMA_VERSION:
            raise ScopedAuthorizationError("unsupported authorization schema")
        if not isinstance(digest, str) or digest != _sha256_bytes(_json_bytes(data)):
            raise ScopedAuthorizationError("authorization digest mismatch (integrity only; not authorship)")
        if not data.get("checkout_id") or data["checkout_id"] in {"*", "all"}:
            raise ScopedAuthorizationError("authorization scope must name one checkout")
        for key in ("checkout_id", "logical_repo", "target_path", "action", "plan_digest", "precondition_digest", "remote_digest"):
            if type(data[key]) is not str:
                raise ScopedAuthorizationError(f"authorization field {key} has wrong type")
        if data["desired_sha"] is not None and type(data["desired_sha"]) is not str:
            raise ScopedAuthorizationError("authorization field desired_sha has wrong type")
        return cls(**data, authorization_digest=digest)


# ---------------------------------------------------------------------------
# Real read-only git probe (used against bubble-safe /tmp fixtures in tests)
# ---------------------------------------------------------------------------


def _run_git(args: list[str], *, cwd: Path | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    """Thin read-only-friendly `git` wrapper; never raises on non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _normalize_url(url: str) -> str:
    return url.rstrip("/").removesuffix(".git")


class GitCheckoutProbe:
    """The real read-only observer. Uses only non-mutating git commands:
    `ls-remote` (remote-side), `rev-parse`, `status --porcelain`, `cat-file -e`,
    `rev-list --count`, `remote get-url`. It NEVER fetches, merges, or writes."""

    def __init__(self, *, timeout: int = 30) -> None:
        self._timeout = timeout
        self.last_desired_sha: str | None = None
        self.last_symbolic_ref: str | None = None
        self.last_path_identity: str | None = None

    def _ls_remote(self, remote_url: str) -> dict[str, str] | None:
        if _unsafe_git_operand(remote_url):
            return None
        # `git ls-remote [--] <repository>`: delimiter is supported by git and
        # prevents a manifest value from being parsed as an option (T-5502).
        res = _run_git(["ls-remote", "--", remote_url], timeout=self._timeout)
        if res.returncode != 0:
            return None
        refs: dict[str, str] = {}
        for line in res.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                refs[parts[1]] = parts[0]
        return refs

    def observe(self, entry: CheckoutEntry, remote_url: str) -> CheckoutObservation:
        self.last_desired_sha = None
        self.last_symbolic_ref = None
        self.last_path_identity = None
        remote_refs = self._ls_remote(remote_url)
        remote_reachable = remote_refs is not None

        path = Path(entry.target_path)
        exists = path.is_dir() and any(path.iterdir()) if path.exists() else False
        path_identity: str | None = None
        try:
            stat = path.stat() if path.exists() else path.parent.stat()
            path_identity = f"{path.resolve()}:{stat.st_dev}:{stat.st_ino}:{int(path.exists())}"
        except OSError:
            path_identity = None

        is_managed = False
        head_sha: str | None = None
        symbolic_ref: str | None = None
        dirty = False
        if exists:
            top = _run_git(["-C", str(path), "rev-parse", "--show-toplevel"], timeout=self._timeout)
            if top.returncode == 0 and Path(top.stdout.strip()).resolve() == path.resolve():
                origin = _run_git(
                    ["-C", str(path), "remote", "get-url", "origin"], timeout=self._timeout
                )
                if origin.returncode == 0 and _normalize_url(origin.stdout.strip()) == _normalize_url(
                    remote_url
                ):
                    is_managed = True

        # Desired target: the pin sha, or the tracking branch's remote tip.
        desired_present = False
        desired_target: str | None = None
        if entry.mode.is_pinned:
            desired_target = entry.mode.pinned
            if remote_refs is not None:
                # present if the sha is a remote tip, or resolvable in the local repo.
                if desired_target in remote_refs.values():
                    desired_present = True
                elif is_managed:
                    chk = _run_git(
                        ["-C", str(path), "cat-file", "-e", f"{desired_target}^{{commit}}"],
                        timeout=self._timeout,
                    )
                    desired_present = chk.returncode == 0
        else:
            branch = entry.mode.tracking
            if remote_refs is not None:
                tip = remote_refs.get(f"refs/heads/{branch}")
                if tip is not None:
                    desired_present = True
                    desired_target = tip

        ahead = 0
        behind = 0
        desired_resolvable = False
        if is_managed:
            head = _run_git(["-C", str(path), "rev-parse", "HEAD"], timeout=self._timeout)
            head_sha = head.stdout.strip() if head.returncode == 0 else None
            sym = _run_git(["-C", str(path), "symbolic-ref", "-q", "HEAD"], timeout=self._timeout)
            symbolic_ref = sym.stdout.strip() if sym.returncode == 0 else None
            dirty = (
                _run_git(["-C", str(path), "status", "--porcelain"], timeout=self._timeout).stdout.strip()
                != ""
            )
            if desired_target is not None:
                resolvable = _run_git(
                    ["-C", str(path), "cat-file", "-e", f"{desired_target}^{{commit}}"],
                    timeout=self._timeout,
                )
                desired_resolvable = resolvable.returncode == 0
                if desired_resolvable and head_sha is not None:
                    ahead = _rev_count(path, f"{desired_target}..HEAD", self._timeout)
                    behind = _rev_count(path, f"HEAD..{desired_target}", self._timeout)

        self.last_desired_sha = desired_target
        self.last_symbolic_ref = symbolic_ref
        self.last_path_identity = path_identity
        return CheckoutObservation(
            remote_reachable=remote_reachable,
            desired_present=desired_present,
            exists=exists,
            is_managed=is_managed,
            dirty=dirty,
            head_sha=head_sha,
            ahead=ahead,
            behind=behind,
            desired_resolvable=desired_resolvable,
        )


def _rev_count(path: Path, spec: str, timeout: int) -> int:
    res = _run_git(["-C", str(path), "rev-list", "--count", spec], timeout=timeout)
    try:
        return int(res.stdout.strip()) if res.returncode == 0 else 0
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Pure classification
# ---------------------------------------------------------------------------


def _classify_state(entry: CheckoutEntry, obs: CheckoutObservation, health_ok: bool) -> tuple[str, str]:
    """Classify one checkout into (plan_state, detail). Pure; no I/O."""
    if not obs.remote_reachable:
        return STATE_CREDENTIAL_FAIL, "remote unreachable / auth failed"
    if not obs.desired_present:
        return STATE_MISSING_REF, f"desired ref/sha {entry.mode.desired!r} absent on remote"
    if not obs.exists:
        return STATE_ABSENT, "target path absent — clone needed"
    if not obs.is_managed:
        return STATE_FOREIGN, "target path holds non-managed content — refuse, never reap"
    if obs.dirty:
        return STATE_DIRTY, "working tree has uncommitted changes — refuse, never reset"
    if obs.desired_resolvable:
        if obs.ahead > 0:
            return STATE_DIVERGED, f"local is ahead/diverged ({obs.ahead} local commit(s)) — never rewind"
        if obs.behind > 0:
            state = STATE_BEHIND
        else:
            state = STATE_CURRENT
    else:
        # Desired target not yet in the local object store: an update is available,
        # but ff-vs-diverged cannot be proven locally. Classify BEHIND; a real
        # apply is ff-only, which refuses divergence safely.
        if obs.head_sha is not None and entry.mode.is_pinned and obs.head_sha == entry.mode.pinned:
            state = STATE_CURRENT
        else:
            state = STATE_BEHIND
    if state == STATE_CURRENT and not health_ok:
        return STATE_HEALTH_FAIL, "post-state health probe failed"
    return state, ""


def _action_for(entry: CheckoutEntry, state: str) -> str:
    if state == STATE_ABSENT:
        # never = touch nothing; ff-only / availability-only clone to establish presence.
        return ACTION_NOOP if entry.update_policy == UPDATE_POLICY_NEVER else ACTION_CLONE
    if state == STATE_BEHIND:
        # only ff-only advances; availability-only + never suppress the advance.
        return ACTION_FF_ADVANCE if entry.update_policy == UPDATE_POLICY_FF_ONLY else ACTION_NOOP
    if state == STATE_CURRENT:
        return ACTION_NOOP
    return f"refuse:{_STATE_REFUSAL[state].value}"


# ---------------------------------------------------------------------------
# The reconciler
# ---------------------------------------------------------------------------


class RepoReconciler:
    def __init__(
        self,
        spec: CheckoutSpec,
        *,
        probe: CheckoutProbe,
        remote_resolver: RemoteResolver,
        health_probe: HealthProbe | None = None,
        broker: ActionBroker | None = None,
        functional_probe: Callable[[], bool] | None = None,
    ) -> None:
        self._spec = spec
        self._probe = probe
        self._remote_resolver = remote_resolver
        self._health_probe = health_probe or (lambda _entry: True)
        self._broker = broker
        self._functional_probe = functional_probe or (lambda: False)

    # -- planning ---------------------------------------------------------

    def _resolve(self, entry: CheckoutEntry) -> str | None:
        try:
            url = self._remote_resolver(entry.logical_repo)
        except Exception:  # noqa: BLE001 — any resolver failure is an unresolved source
            return None
        if not isinstance(url, str) or _unsafe_git_operand(url):
            return None
        return url

    @staticmethod
    def _precondition_digest(
        entry: CheckoutEntry,
        remote_url: str,
        obs: CheckoutObservation,
        *,
        desired_sha: str | None,
        symbolic_ref: str | None,
        path_identity: str | None,
    ) -> str:
        return _sha256_bytes(
            _json_bytes(
                {
                    "checkout_id": entry.checkout_id,
                    "logical_repo": entry.logical_repo,
                    "target_path": entry.target_path,
                    "mode": entry.mode.desired,
                    "update_policy": entry.update_policy,
                    "remote_digest": _remote_digest(remote_url),
                    "remote_reachable": obs.remote_reachable,
                    "desired_present": obs.desired_present,
                    "desired_sha": desired_sha,
                    "exists": obs.exists,
                    "is_managed": obs.is_managed,
                    "dirty": obs.dirty,
                    "head_sha": obs.head_sha,
                    "symbolic_ref": symbolic_ref,
                    "ahead": obs.ahead,
                    "behind": obs.behind,
                    "desired_resolvable": obs.desired_resolvable,
                    "path_identity": path_identity,
                }
            )
        )

    def _classify_entry(self, entry: CheckoutEntry) -> ReconcilePlanItem:
        remote_url = self._resolve(entry)
        if remote_url is None:
            refusal = CompatibilityRefusal(
                RefusalKind.UNRESOLVED_SOURCE,
                entry.checkout_id,
                f"logical repo {entry.logical_repo!r} did not resolve to a remote",
            )
            return ReconcilePlanItem(
                checkout_id=entry.checkout_id,
                logical_repo=entry.logical_repo,
                target_path=entry.target_path,
                state=STATE_UNRESOLVED,
                action=f"refuse:{RefusalKind.UNRESOLVED_SOURCE.value}",
                detail=refusal.detail,
                refusal=refusal,
            )

        obs = self._probe.observe(entry, remote_url)
        desired_sha = getattr(self._probe, "last_desired_sha", None)
        symbolic_ref = getattr(self._probe, "last_symbolic_ref", None)
        path_identity = getattr(self._probe, "last_path_identity", None)
        if desired_sha is None and entry.mode.is_pinned:
            desired_sha = entry.mode.pinned
        # Health is only meaningful for an otherwise-converged checkout; probe it
        # lazily so the health seam is never consulted for a refusing/absent state.
        health_ok = True
        if (
            obs.remote_reachable
            and obs.desired_present
            and obs.exists
            and obs.is_managed
            and not obs.dirty
            and obs.desired_resolvable
            and obs.ahead == 0
            and obs.behind == 0
        ):
            health_ok = bool(self._health_probe(entry))

        state, detail = _classify_state(entry, obs, health_ok)
        action = _action_for(entry, state)

        refusal = None
        if state in _STATE_REFUSAL:
            refusal = CompatibilityRefusal(_STATE_REFUSAL[state], entry.checkout_id, detail)

        brokered_intent = None
        if action in (ACTION_CLONE, ACTION_FF_ADVANCE) and entry.restart_contract is not None:
            brokered_intent = BrokeredIntent(
                checkout_id=entry.checkout_id,
                operation_family=entry.restart_contract.operation_family,
                target_unit=entry.restart_contract.target_unit,
                dispatched=False,
            )

        return ReconcilePlanItem(
            checkout_id=entry.checkout_id,
            logical_repo=entry.logical_repo,
            target_path=entry.target_path,
            state=state,
            action=action,
            detail=detail,
            refusal=refusal,
            brokered_intent=brokered_intent,
            precondition_digest=self._precondition_digest(
                entry,
                remote_url,
                obs,
                desired_sha=desired_sha,
                symbolic_ref=symbolic_ref,
                path_identity=path_identity,
            ),
            remote_digest=_remote_digest(remote_url),
            desired_sha=desired_sha,
            observed_head=obs.head_sha,
        )

    def reconcile_plan(self) -> ReconcilePlan:
        """The module's own rich, per-checkout plan over every declared checkout."""
        items = tuple(
            self._classify_entry(self._spec.checkouts[cid])
            for cid in sorted(self._spec.checkouts)
        )
        return ReconcilePlan(items=items)

    def reconcile_checkout(self, checkout_id: str) -> ReconcilePlan:
        """Plan exactly one checkout; empty, wildcard and unknown scopes refuse."""

        if not checkout_id or checkout_id in {"*", "all"}:
            raise ScopedAuthorizationError("checkout scope must be one explicit id")
        entry = self._spec.checkouts.get(checkout_id)
        if entry is None:
            raise ScopedAuthorizationError(f"unknown checkout scope {checkout_id!r}")
        return ReconcilePlan(items=(self._classify_entry(entry),))

    def issue_authorization(
        self, plan: ReconcilePlan, checkout_id: str
    ) -> ScopedCheckoutAuthorization:
        """Bind a one-checkout plan to an integrity-only apply capability."""

        if len(plan.items) != 1 or plan.items[0].checkout_id != checkout_id:
            raise ScopedAuthorizationError("authorization cannot widen or select another checkout")
        item = plan.items[0]
        if item.refusal is not None:
            raise ScopedAuthorizationError("cannot authorize a refusing plan")
        if item.action in (ACTION_CLONE, ACTION_FF_ADVANCE) and not item.desired_sha:
            raise ScopedAuthorizationError("mutating authorization requires an exact desired commit")
        authorization = ScopedCheckoutAuthorization(
            checkout_id=item.checkout_id,
            logical_repo=item.logical_repo,
            target_path=item.target_path,
            action=item.action,
            desired_sha=item.desired_sha,
            plan_digest=_sha256_bytes(_json_bytes(item.to_dict())),
            precondition_digest=item.precondition_digest,
            remote_digest=item.remote_digest,
        )
        return authorization.sealed()

    # -- T-3182 RepositoryLifecyclePlanner Protocol -----------------------

    def plan(self, sources: Sequence[LockedComponent]) -> LifecyclePlan:
        """Join each candidate `LockedComponent` (keyed by logical_repo) against the
        declarative checkout-spec, run the plan algorithm, and collapse to the
        Protocol's `LifecyclePlan`. Signature preserved so `plan_assembly_update`
        composes unchanged."""
        refusals: list[CompatibilityRefusal] = []
        for component in sources:
            for entry in self._spec.by_logical_repo(component.logical_repo):
                item = self._classify_entry(entry)
                if item.refusal is not None:
                    refusals.append(item.refusal)
        if refusals:
            refusals.sort(key=lambda r: (r.kind.value, r.subject_id, r.detail))
            return LifecyclePlan(ok=False, stages=ROLLBACK_STAGES, refusal=refusals[0])
        return LifecyclePlan(ok=True, stages=UPDATE_STAGES, refusal=None)

    # -- apply / verify / rollback ----------------------------------------

    def apply(
        self,
        plan: ReconcilePlan,
        authorization: ScopedCheckoutAuthorization | None = None,
    ) -> ApplyReceipt:
        """Materialize ONLY the approved, non-destructive actions — and ONLY when the
        functional probe and an exact scoped authorization are live. Refusals are
        skipped (never mutated). Restart remains evidence-only: no broker method is
        called in phase 2, even when a caller injects a broker claiming availability.
        """
        functional = bool(self._functional_probe())
        materialize = functional and authorization is not None
        receipt = ApplyReceipt(materialized=materialize)

        if authorization is None:
            # Compatibility: a functional fixture without a scoped capability is
            # still inert. Planning remains usable; mutation never follows merely
            # from functional_probe=True.
            return receipt.seal()
        if not functional:
            return receipt.seal()
        if len(plan.items) != 1:
            raise ScopedAuthorizationError("apply requires a one-checkout plan")
        item = plan.items[0]
        sealed = ScopedCheckoutAuthorization.from_bytes(authorization.to_bytes())
        if (
            item.checkout_id != sealed.checkout_id
            or item.logical_repo != sealed.logical_repo
            or item.target_path != sealed.target_path
            or item.action != sealed.action
            or item.desired_sha != sealed.desired_sha
            or item.precondition_digest != sealed.precondition_digest
            or item.remote_digest != sealed.remote_digest
            or _sha256_bytes(_json_bytes(item.to_dict())) != sealed.plan_digest
        ):
            raise ScopedAuthorizationError("authorization scope/plan mismatch")

        # Re-observe immediately before mutation. Any state, identity, HEAD,
        # desired-tip, path or precondition drift closes the gate.
        fresh = self.reconcile_checkout(item.checkout_id).items[0]
        if fresh.to_dict() != item.to_dict():
            raise ScopedAuthorizationError("checkout preconditions drifted after authorization")

        receipt.authorization_digest = sealed.authorization_digest
        receipt.checkout_id = item.checkout_id
        receipt.target_path = item.target_path
        receipt.action = item.action
        receipt.desired_sha = item.desired_sha
        receipt.before_digest = item.precondition_digest
        receipt.rollback_supported = item.action == ACTION_CLONE

        for item in sorted(plan.items, key=lambda i: i.checkout_id):
            entry = self._spec.checkouts.get(item.checkout_id)
            if item.refusal is not None:
                receipt.refused.append(item.refusal)
                continue
            if item.action == ACTION_CLONE and entry is not None:
                if materialize:
                    self._do_clone(entry, item.desired_sha, receipt)
            elif item.action == ACTION_FF_ADVANCE and entry is not None:
                if materialize:
                    self._do_ff_advance(entry, item.desired_sha, receipt)
            # noop: nothing.

            if item.brokered_intent is not None:
                receipt.intents.append(item.brokered_intent)
                # Phase 2 deliberately has no dispatch site. T-2652/T-5506 remain
                # prerequisites; even an injected broker claiming availability is
                # evidence-only here.

        after = self.reconcile_checkout(item.checkout_id).items[0]
        receipt.after_digest = after.precondition_digest
        return receipt.seal()

    def _apply_operand_refusal(self, entry: CheckoutEntry) -> tuple[str | None, CompatibilityRefusal | None]:
        """Resolve the remote and vet every operand for an apply step (T-6151).

        Returns (remote_url, None) when safe, else (None, refusal) naming the entry
        and the operand. The operand VALUE is never echoed: manifest URLs may embed
        credentials. A resolver failure stays a plain None (no operand to name)."""

        try:
            url = self._remote_resolver(entry.logical_repo)
        except Exception:  # noqa: BLE001 — any resolver failure is an unresolved source
            return None, None
        if not isinstance(url, str):
            return None, None
        if _unsafe_git_operand(url):
            why = "empty" if not url else "dash-led or NUL-bearing"
            return None, CompatibilityRefusal(
                RefusalKind.UNRESOLVED_SOURCE,
                entry.checkout_id,
                f"unsafe operand url refused at apply ({why}); nothing was run",
            )
        for name, value in (("target_path", entry.target_path), ("tracking", entry.mode.tracking)):
            if value is not None and _unsafe_git_operand(value):
                why = "empty" if not value else "dash-led or NUL-bearing"
                return None, CompatibilityRefusal(
                    RefusalKind.LIFECYCLE_REFUSED,
                    entry.checkout_id,
                    f"unsafe operand {name} refused at apply ({why}); nothing was run",
                )
        return url, None

    def _do_clone(
        self, entry: CheckoutEntry, desired_sha: str | None, receipt: ApplyReceipt
    ) -> None:
        remote_url, refusal = self._apply_operand_refusal(entry)
        if refusal is not None:
            receipt.refused.append(refusal)
            return
        if remote_url is None or desired_sha is None:
            return
        # `git clone [options] [--] <repository> [<directory>]` supports this
        # delimiter. Tracking clones name the authorized branch, then the HEAD
        # equality check below closes a remote-tip race without reset/force.
        clone_args = ["clone"]
        if entry.mode.tracking is not None:
            clone_args.extend(["--branch", entry.mode.tracking])
        clone_args.extend(["--", remote_url, entry.target_path])
        res = _run_git(clone_args)
        if res.returncode != 0:
            target = Path(entry.target_path)
            identity = _filesystem_identity(target)
            if identity is not None:
                receipt.failed_clone_paths[entry.checkout_id] = entry.target_path
                receipt.failed_clone_identity[entry.checkout_id] = identity
                head = _run_git(["-C", entry.target_path, "rev-parse", "HEAD"])
                if head.returncode == 0 and head.stdout.strip():
                    receipt.failed_clone_sha[entry.checkout_id] = head.stdout.strip()
                detail = "clone failed after creating target content; left in place"
                kind = RefusalKind.FOREIGN_CHECKOUT
            else:
                detail = "clone failed before target creation"
                kind = RefusalKind.HEALTH_FAILURE
            receipt.refused.append(
                CompatibilityRefusal(kind, entry.checkout_id, f"{detail} (rc={res.returncode})")
            )
            return

        head = _run_git(["-C", entry.target_path, "rev-parse", "HEAD"])
        if head.returncode != 0 or head.stdout.strip() != desired_sha:
            # Never delete a failed clone: another actor may already have
            # changed or replaced it. Preserve exact evidence for supervised
            # inspection; failed clones are ineligible for rollback.
            landed = head.stdout.strip() if head.returncode == 0 else ""
            receipt.failed_clone_paths[entry.checkout_id] = entry.target_path
            receipt.failed_clone_sha[entry.checkout_id] = landed
            identity = _filesystem_identity(Path(entry.target_path))
            if identity is not None:
                receipt.failed_clone_identity[entry.checkout_id] = identity
            receipt.refused.append(
                CompatibilityRefusal(
                    RefusalKind.DIVERGED_CHECKOUT,
                    entry.checkout_id,
                    f"clone landed on {landed!r}, not authorized {desired_sha!r}; left in place",
                )
            )
            return
        receipt.installs.append(entry.checkout_id)
        receipt.install_paths[entry.checkout_id] = entry.target_path
        receipt.installed_sha[entry.checkout_id] = desired_sha
        identity = _filesystem_identity(Path(entry.target_path))
        if identity is None:
            receipt.installs.remove(entry.checkout_id)
            receipt.install_paths.pop(entry.checkout_id, None)
            receipt.installed_sha.pop(entry.checkout_id, None)
            receipt.failed_clone_paths[entry.checkout_id] = entry.target_path
            receipt.failed_clone_sha[entry.checkout_id] = desired_sha
            receipt.refused.append(
                CompatibilityRefusal(
                    RefusalKind.FOREIGN_CHECKOUT,
                    entry.checkout_id,
                    "clone filesystem identity could not be established; left in place",
                )
            )
            return
        receipt.installed_path_identity[entry.checkout_id] = identity

    def _do_ff_advance(
        self, entry: CheckoutEntry, desired_sha: str | None, receipt: ApplyReceipt
    ) -> None:
        remote_url, refusal = self._apply_operand_refusal(entry)
        if refusal is not None:
            receipt.refused.append(refusal)
            return
        if remote_url is None or desired_sha is None:
            return
        path = entry.target_path
        # Fetch the SPECIFIC desired ref/object (never a bare `git fetch <url>`,
        # whose FETCH_HEAD is the remote's DEFAULT branch — advancing a
        # non-default `tracking:` checkout onto the wrong branch, and leaving a
        # pinned object un-fetched so `merge --ff-only <sha>` silently no-ops).
        # The apply target MUST equal the ref the plan computed as desired.
        # Fetch updates refs/objects only — never the working tree; `merge
        # --ff-only` inherently REFUSES divergence: no reset, no force, ever.
        if entry.mode.is_pinned:
            target = desired_sha
            # Fetch the exact pin. If the server refuses a bare-sha want, fall
            # back to fetching every branch tip (a ref that contains the pin);
            # the merge target stays the exact pin sha either way.
            fetch = _run_git(["-C", path, "fetch", "--", remote_url, target])
            if fetch.returncode != 0:
                fetch = _run_git(
                    ["-C", path, "fetch", "--", remote_url, "+refs/heads/*:refs/remotes/origin/*"]
                )
                if fetch.returncode != 0:
                    return
        else:
            # Fetch the desired branch by name; FETCH_HEAD is then THAT branch's
            # tip (the ref the plan computed), not the remote's default HEAD.
            target = desired_sha
            fetch = _run_git(["-C", path, "fetch", "--", remote_url, f"refs/heads/{entry.mode.tracking}"])
            if fetch.returncode != 0:
                return
        res = _run_git(["-C", path, "merge", "--ff-only", target])
        head = _run_git(["-C", path, "rev-parse", "HEAD"])
        if res.returncode == 0 and head.returncode == 0 and head.stdout.strip() == desired_sha:
            receipt.advances.append(entry.checkout_id)

    def verify(self, plan: ReconcilePlan | None = None) -> ReconcilePlan:
        """Diagnose every checkout on one uniform failure surface. A re-plan IS the
        verification (convergent: a fully-applied plan re-plans to all CURRENT/noop).
        `plan` is accepted for symmetry; verify always re-observes."""
        return self.reconcile_plan()

    def _canonical_clone_receipt(
        self, receipt: ApplyReceipt, requested_checkout_id: str | None = None
    ) -> tuple[ApplyReceipt, CheckoutEntry]:
        """Strictly load and semantically bind one successful clone receipt."""
        if not receipt.receipt_digest:
            raise ScopedAuthorizationError("rollback requires a sealed receipt")
        receipt = ApplyReceipt.from_bytes(receipt.to_bytes())
        checkout_id = receipt.checkout_id
        entry = self._spec.checkouts.get(checkout_id)
        if entry is None or (requested_checkout_id is not None and checkout_id != requested_checkout_id):
            raise ScopedAuthorizationError("receipt checkout is unknown or outside requested scope")
        expected_keys = {checkout_id}
        if not receipt.materialized or receipt.action != ACTION_CLONE or not receipt.rollback_supported:
            raise ScopedAuthorizationError("receipt is not a materialized rollback-supported clone")
        if receipt.installs != [checkout_id] or receipt.advances:
            raise ScopedAuthorizationError("receipt clone/install semantics are not exact")
        if (
            set(receipt.install_paths) != expected_keys
            or set(receipt.installed_sha) != expected_keys
            or set(receipt.installed_path_identity) != expected_keys
        ):
            raise ScopedAuthorizationError("receipt install evidence is not exact-checkout scoped")
        if receipt.target_path != entry.target_path or receipt.install_paths[checkout_id] != entry.target_path:
            raise ScopedAuthorizationError("receipt recorded path does not equal the spec target")
        if (
            not receipt.desired_sha
            or receipt.installed_sha[checkout_id] != receipt.desired_sha
            or not receipt.authorization_digest
        ):
            raise ScopedAuthorizationError("receipt desired/install authorization evidence is inconsistent")
        if (
            receipt.failed_clone_paths
            or receipt.failed_clone_sha
            or receipt.failed_clone_identity
            or receipt.refused
            or receipt.dispatched
        ):
            raise ScopedAuthorizationError("receipt contains failed/refused/dispatched ambiguity")
        return receipt, entry

    @staticmethod
    def _rollback_refusal(
        checkout_id: str,
        kind: RefusalKind,
        detail: str,
        *,
        quarantine_path: Path | None = None,
    ) -> RollbackOutcome:
        refusal = CompatibilityRefusal(
            kind, checkout_id, detail, str(quarantine_path) if quarantine_path is not None else None
        )
        return RollbackOutcome(
            checkout_id=checkout_id,
            success=False,
            quarantine_path=str(quarantine_path) if quarantine_path is not None else None,
            refusals=(refusal,),
        )

    def rollback(self, receipt: ApplyReceipt) -> RollbackOutcome:
        """Atomically quarantine an exactly-owned clone; never delete any bytes."""
        receipt, entry = self._canonical_clone_receipt(receipt)
        return self._quarantine(entry, receipt)

    @staticmethod
    def _working_tree_state(path: Path) -> tuple[bool, bool] | None:
        """Return (tracked_dirty, has_untracked) for a checkout — read-only.

        `tracked_dirty` = uncommitted changes to tracked files; `has_untracked` =
        untracked (foreign) files present. Either means the working tree is not the
        pristine install we recorded, so it must never be quarantined."""
        res = _run_git(["-C", str(path), "status", "--porcelain", "--untracked-files=all"])
        if res.returncode != 0:
            return None
        tracked_dirty = False
        has_untracked = False
        for line in res.stdout.splitlines():
            if not line.strip():
                continue
            if line.startswith("??"):
                has_untracked = True
            else:
                tracked_dirty = True
        return tracked_dirty, has_untracked

    def _quarantine(self, entry: CheckoutEntry, receipt: ApplyReceipt) -> RollbackOutcome:
        checkout_id = entry.checkout_id
        path = Path(receipt.install_paths[checkout_id])
        recorded_sha = receipt.installed_sha[checkout_id]
        recorded_identity = receipt.installed_path_identity[checkout_id]
        container, payload = _quarantine_paths(receipt, path)
        if not self._functional_probe():
            return self._rollback_refusal(
                checkout_id, RefusalKind.HEALTH_FAILURE, "functional rollback gate is inert"
            )

        try:
            target_stat = os.lstat(path)
        except FileNotFoundError:
            target_stat = None
        try:
            container_stat = os.lstat(container)
        except FileNotFoundError:
            container_stat = None

        if target_stat is None:
            if (
                container_stat is None
                or stat.S_ISLNK(container_stat.st_mode)
                or not stat.S_ISDIR(container_stat.st_mode)
                or stat.S_IMODE(container_stat.st_mode) != 0o700
            ):
                return self._rollback_refusal(
                    checkout_id, RefusalKind.FOREIGN_CHECKOUT,
                    "target is absent without an unambiguous receipt quarantine",
                )
            try:
                quarantine_entries = set(os.listdir(container))
            except OSError as exc:
                return self._rollback_refusal(
                    checkout_id, RefusalKind.FOREIGN_CHECKOUT,
                    f"receipt quarantine could not be inspected: {exc}",
                    quarantine_path=container,
                )
            if quarantine_entries != {payload.name}:
                return self._rollback_refusal(
                    checkout_id, RefusalKind.FOREIGN_CHECKOUT,
                    "target is absent but quarantine contains foreign or ambiguous entries",
                    quarantine_path=container,
                )
            try:
                payload_stat = os.lstat(payload)
            except FileNotFoundError:
                payload_stat = None
            payload_identity = (
                {"device": payload_stat.st_dev, "inode": payload_stat.st_ino}
                if payload_stat is not None and stat.S_ISDIR(payload_stat.st_mode) and not stat.S_ISLNK(payload_stat.st_mode)
                else None
            )
            try:
                final_resume_container = os.lstat(container)
                final_resume_entries = set(os.listdir(container))
            except OSError:
                final_resume_container = None
                final_resume_entries = set()
            if (
                payload_identity == recorded_identity
                and final_resume_container is not None
                and (final_resume_container.st_dev, final_resume_container.st_ino)
                == (container_stat.st_dev, container_stat.st_ino)
                and stat.S_ISDIR(final_resume_container.st_mode)
                and not stat.S_ISLNK(final_resume_container.st_mode)
                and stat.S_IMODE(final_resume_container.st_mode) == 0o700
                and final_resume_entries == {payload.name}
            ):
                return RollbackOutcome(checkout_id, True, str(payload), True)
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT,
                "target is absent but quarantine is empty, foreign, or ambiguous",
                quarantine_path=payload if payload_stat is not None else container,
            )

        if container_stat is not None:
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT,
                f"quarantine namespace already exists while target remains present: {container}",
                quarantine_path=container,
            )
        if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISDIR(target_stat.st_mode):
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT, "target is a symlink or not a directory"
            )
        if {"device": target_stat.st_dev, "inode": target_stat.st_ino} != recorded_identity:
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT, "target filesystem identity differs from receipt"
            )

        remote_url = self._resolve(entry)
        if remote_url is None:
            return self._rollback_refusal(
                checkout_id, RefusalKind.UNRESOLVED_SOURCE,
                "cannot confirm repository identity before quarantine",
            )
        obs = self._probe.observe(entry, remote_url)
        if not obs.is_managed:
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT, "target is no longer the managed repository"
            )
        tree_state = self._working_tree_state(path)
        if tree_state is None:
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT, "git status was nonzero or indeterminate"
            )
        tracked_dirty, has_untracked = tree_state
        if tracked_dirty:
            return self._rollback_refusal(
                checkout_id, RefusalKind.DIRTY_CHECKOUT, "target has uncommitted changes"
            )
        if has_untracked:
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT, "target holds untracked foreign files"
            )
        if obs.head_sha != recorded_sha:
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT, "target HEAD differs from installed SHA"
            )

        try:
            final_stat = os.lstat(path)
        except OSError:
            final_stat = None
        if (
            final_stat is None
            or stat.S_ISLNK(final_stat.st_mode)
            or {"device": final_stat.st_dev, "inode": final_stat.st_ino} != recorded_identity
        ):
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT, "target identity changed during validation"
            )

        try:
            os.mkdir(container, 0o700)
            created_container_stat = os.lstat(container)
        except OSError as exc:
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT,
                f"exclusive quarantine creation refused: {exc}", quarantine_path=container,
            )
        if (
            stat.S_ISLNK(created_container_stat.st_mode)
            or not stat.S_ISDIR(created_container_stat.st_mode)
            or stat.S_IMODE(created_container_stat.st_mode) != 0o700
        ):
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT,
                "new quarantine container is not a private mode-0700 directory",
                quarantine_path=container,
            )
        created_container_identity = {
            "device": created_container_stat.st_dev,
            "inode": created_container_stat.st_ino,
        }
        try:
            if os.listdir(container):
                return self._rollback_refusal(
                    checkout_id, RefusalKind.FOREIGN_CHECKOUT,
                    "new quarantine container is not empty",
                    quarantine_path=container,
                )
        except OSError as exc:
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT,
                f"new quarantine container could not be inspected: {exc}",
                quarantine_path=container,
            )
        try:
            os.lstat(payload)
        except FileNotFoundError:
            pass
        else:
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT,
                "quarantine payload unexpectedly exists", quarantine_path=payload,
            )
        try:
            _rename_noreplace(path, payload)
        except OSError as exc:
            detail = "cross-device quarantine refused" if exc.errno == errno.EXDEV else f"atomic quarantine rename refused: {exc}"
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT, detail, quarantine_path=container,
            )

        try:
            final_container_stat = os.lstat(container)
            final_entries = set(os.listdir(container))
        except OSError:
            final_container_stat = None
            final_entries = set()
        final_container_identity = (
            {"device": final_container_stat.st_dev, "inode": final_container_stat.st_ino}
            if final_container_stat is not None
            else None
        )
        payload_identity = _filesystem_identity(payload)
        if (
            payload_identity == recorded_identity
            and final_container_identity == created_container_identity
            and final_container_stat is not None
            and stat.S_ISDIR(final_container_stat.st_mode)
            and not stat.S_ISLNK(final_container_stat.st_mode)
            and stat.S_IMODE(final_container_stat.st_mode) == 0o700
            and final_entries == {payload.name}
        ):
            try:
                os.lstat(path)
            except FileNotFoundError:
                return RollbackOutcome(checkout_id, True, str(payload), False)
            except OSError as exc:
                return self._rollback_refusal(
                    checkout_id, RefusalKind.FOREIGN_CHECKOUT,
                    f"final target absence check failed for {path}; preserved quarantine {payload}: {exc}",
                    quarantine_path=payload,
                )
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT,
                f"target {path} was repopulated after quarantine; preserved both target and {payload}",
                quarantine_path=payload,
            )

        try:
            _rename_noreplace(payload, path)
        except OSError as exc:
            return self._rollback_refusal(
                checkout_id, RefusalKind.FOREIGN_CHECKOUT,
                f"moved identity mismatch; preserved both paths {path} and {payload}: {exc}",
                quarantine_path=payload,
            )
        return self._rollback_refusal(
            checkout_id, RefusalKind.FOREIGN_CHECKOUT,
            f"moved identity mismatch; restored foreign content without overwrite to {path}",
        )

    def remove(self, checkout_id: str, receipt: ApplyReceipt) -> RollbackOutcome:
        """Compatibility entrypoint: strict clone receipt → quarantine outcome."""
        receipt, entry = self._canonical_clone_receipt(receipt, checkout_id)
        return self._quarantine(entry, receipt)


# ---------------------------------------------------------------------------
# CLI (report-only — the plan IS the output; no apply, no mutation)
# ---------------------------------------------------------------------------


def _inert_resolver(_logical_repo: str) -> str:
    # The production wiring (remote_resolve.resolve_remote under a verified
    # manifest) is phase-2; the CLI stays inert here so a bare `plan` run never
    # touches a real remote. Returns "" → every checkout reports UNRESOLVED_SOURCE.
    return ""


def _write_new_artifact(path: Path, content: bytes) -> None:
    """Create, never overwrite, a durable authorization/receipt artifact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _production_resolver(repo_root: Path, pin_path: Path) -> RemoteResolver:
    from tools.remote_resolve import load_production_manifest, resolve_remote

    manifest = load_production_manifest(repo_root=repo_root, pin_path=pin_path)

    def resolve(logical_repo: str) -> str:
        return resolve_remote(logical_repo, manifest=manifest).url

    return resolve


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="T-2951 checkout-lifecycle reconciler (report-only)")
    sub = parser.add_subparsers(dest="command", required=True)
    plan_cmd = sub.add_parser("plan", help="report the reconcile plan (read-only; no apply)")
    plan_cmd.add_argument("--checkouts-dir", type=Path, default=Path.cwd() / "checkouts")
    generate = sub.add_parser("generate-schema", help="write schemas/checkout-spec.schema.json")
    generate.add_argument("--repo-root", type=Path, default=Path.cwd())
    scoped_commands: dict[str, object] = {}
    for name in ("authorize", "apply", "verify", "rollback"):
        cmd = sub.add_parser(name, help=f"exact-checkout {name} (scoped artifact required)")
        cmd.add_argument("--checkouts-dir", type=Path, required=True)
        cmd.add_argument("--repo-root", type=Path, required=True)
        cmd.add_argument("--pin-path", type=Path, required=True)
        cmd.add_argument("--checkout-id", required=True)
        scoped_commands[name] = cmd
    scoped_commands["authorize"].add_argument("--authorization-out", type=Path, required=True)  # type: ignore[attr-defined]
    scoped_commands["apply"].add_argument("--authorization", type=Path, required=True)  # type: ignore[attr-defined]
    scoped_commands["apply"].add_argument("--receipt-out", type=Path, required=True)  # type: ignore[attr-defined]
    scoped_commands["verify"].add_argument("--receipt", type=Path, required=True)  # type: ignore[attr-defined]
    scoped_commands["rollback"].add_argument("--receipt", type=Path, required=True)  # type: ignore[attr-defined]
    args = parser.parse_args(argv)

    if args.command == "generate-schema":
        from tools.checkout_spec import generate_checkout_spec_schema
        from tools.component_catalogue import _atomic_write

        _atomic_write(
            args.repo_root / "schemas" / "checkout-spec.schema.json",
            generate_checkout_spec_schema(),
        )
        return 0

    from tools.checkout_spec import load_checkout_spec

    try:
        spec = load_checkout_spec(args.checkouts_dir)
    except RepositoryLifecycleError as exc:
        print(f"checkout-spec-invalid: {exc}")
        return 1
    if args.command in scoped_commands:
        try:
            resolver = _production_resolver(args.repo_root, args.pin_path)
            reconciler = RepoReconciler(
                spec,
                probe=GitCheckoutProbe(),
                remote_resolver=resolver,
                functional_probe=(lambda: args.command in {"apply", "rollback"}),
            )
            plan = reconciler.reconcile_checkout(args.checkout_id)
            if args.command == "authorize":
                auth = reconciler.issue_authorization(plan, args.checkout_id)
                _write_new_artifact(args.authorization_out, auth.to_bytes())
                sys.stdout.buffer.write(auth.to_bytes())
                return 0
            if args.command == "apply":
                auth = ScopedCheckoutAuthorization.from_bytes(args.authorization.read_bytes())
                receipt = reconciler.apply(plan, auth)
                _write_new_artifact(args.receipt_out, receipt.to_bytes())
                sys.stdout.buffer.write(receipt.to_bytes())
                return 0 if receipt.installs or receipt.advances or plan.items[0].action == ACTION_NOOP else 2
            receipt = ApplyReceipt.from_bytes(args.receipt.read_bytes())
            if receipt.checkout_id != args.checkout_id:
                raise ScopedAuthorizationError("receipt checkout does not match CLI scope")
            if args.command == "verify":
                item = plan.items[0]
                ok = (
                    item.refusal is None
                    and item.state == STATE_CURRENT
                    and item.observed_head == receipt.desired_sha
                )
                sys.stdout.buffer.write(
                    _json_bytes({"checkout_id": args.checkout_id, "verified": ok, "item": item.to_dict()})
                )
                return 0 if ok else 2
            outcome = reconciler.rollback(receipt)
            sys.stdout.buffer.write(
                _json_bytes(
                    {
                        "checkout_id": args.checkout_id,
                        "rolled_back": outcome.success,
                        "quarantine_path": outcome.quarantine_path,
                        "resumed": outcome.resumed,
                        "refusals": [r.as_text() for r in outcome.refusals],
                    }
                )
            )
            return 0 if outcome.success else 2
        except (OSError, RepositoryLifecycleError, ScopedAuthorizationError, ValueError) as exc:
            print(f"scoped-reconcile-refused: {exc}", file=sys.stderr)
            return 2

    reconciler = RepoReconciler(spec, probe=GitCheckoutProbe(), remote_resolver=_inert_resolver)
    plan = reconciler.reconcile_plan()
    sys.stdout.buffer.write(plan.to_report_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
