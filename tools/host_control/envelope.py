"""The closed per-seat executor envelope (ORG-052 section 8 bootstrap slice;
ORG-052 Unit 5a, T-2917).

Maps every persona-installer ``OperationKind`` the per-seat executor
implements to an executor CAPABILITY CLASS. These classes are code: they
name what the target-uid executor can physically do. They are deliberately
NOT broker policy families; the family namespace stays root-owned policy
data (T-2911 PO override 1), so nothing here grants or widens authority.

Installer kinds outside the ORG-052 section 8 slice are listed explicitly
in ``OUT_OF_ENVELOPE_KINDS`` and are refused at plan time with
``family_denied`` (T-2917 plan ruling i4). Adding one is new scope and
possibly an ORG-052 section 8 amendment, never a silent table edit.

No class here is a substrate family (ORG-082 section 1): the ORG-082
substrate list is mirrored in ``ORG082_SUBSTRATE_FAMILIES`` only so a test
can prove the two are disjoint. The table is exported as one frozen
constant so the ORG-082 gate 32a runtime-subset projection can digest it
later; Unit 5 builds none of the ORG-080 registry machinery.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Iterable, Mapping

from tools.host_control.contracts import PolicyDenied, Unsupported

EXECUTOR_KIND_CLASSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        # read-only host and user-manager diagnostics
        "identity-check": "diagnostics.read",
        "source-check": "diagnostics.read",
        "harness-check": "diagnostics.read",
        "brief-check": "diagnostics.read",
        "policy-check": "diagnostics.read",
        "manager-check": "diagnostics.read",
        # post-ratification (T-2953); §8a addendum on T-4861 (HO, 2026-10-12)
        "timer-enablement-absence-check": "diagnostics.read",
        # installer-owned file materialization within the seat root
        "mkdir": "files.seat_root",
        "atomic-write": "files.seat_root",
        "user-unit-install": "files.seat_root",
        # user-systemd reload / fire / timer enable + observe
        "daemon-reload": "user_systemd.activate",
        # post-ratification (T-2953); §8a addendum on T-4861: ONLY via the 5c
        # user-manager port (never systemctl --user), only for plan-named units
        "stale-unit-failure-reset": "user_systemd.activate",
        "direct-fire": "user_systemd.activate",
        "unit-fire": "user_systemd.activate",
        "timer-enable": "user_systemd.activate",
        "timer-observe": "user_systemd.activate",
        # mailbox send/receive round-trip probes
        "mailbox-probe": "mailbox.probe",
        # post-ratification (T-2953); §8a addendum on T-4861 (HO, 2026-10-12)
        "mailbox-writable-root-check": "mailbox.probe",
        # --- §8a classification, HO clarifying ruling T-4861 ---
        # Classified by EFFECT, never by kind name. The repo-source checks
        # below run against the root-owned registry copy or inside the
        # executor, NEVER against a seat checkout (enforced in 5b where the
        # executor runs them).
        "codex-standing-instructions-source-check": "diagnostics.read",
        "codex-standing-instructions-target-check": "diagnostics.read",
        "codex-standing-instructions-override-check": "diagnostics.read",
        "claude-mcp-server-check": "diagnostics.read",
        # T-4861 ruling: user-systemd INSPECTION only, through the 5c
        # user-manager port (receiver active/enabled/EnvironmentFiles reads;
        # no write, no start, no enable). Admitted in T-2917 5c.2.
        "codex-telegram-disabled-first": "diagnostics.read",
        # Constraints: bytes carried in the plan and hash-pinned,
        # managed-marker guard, receipt-snapshot rollback.
        "codex-standing-instructions-install": "files.seat_root",
    }
)

EXECUTOR_CAPABILITY_CLASSES: Final[frozenset[str]] = frozenset(
    EXECUTOR_KIND_CLASSES.values()
)

# Installer kinds NOT admitted (T-2917 ruling i4 + HO ruling T-4861):
# refused at plan time, never silently filtered or partially applied.
OUT_OF_ENVELOPE_KINDS: Final[frozenset[str]] = frozenset(
    (
        # §8b widening (the operator's call); operator leg until then. Named
        # *-check but they WRITE (release extract/allowlist/symlink;
        # ~/.codex/config.toml including tool approval_mode).
        "codex-telegram-source-check",
        "codex-telegram-mcp-check",
        # Ruled OUT of the broker: seat-local after the broker phase,
        # completion observed via Verify.
        "claude-mcp-server-registration",
        # T-5447: seat-core periodic timers are seat-local residue (partition
        # carries them as OperatorActions); the executor does NOT implement them.
        "seat-periodic-unit-install",
        "seat-periodic-daemon-reload",
        "seat-periodic-timer-enable",
        "seat-periodic-timer-observe",
        # T-5448: controller bring-up + ho-resumer decommission are seat-local too.
        "seat-service-enable-observe",
        "seat-service-decommission",
        # T-5454: shell-box containment wiring in the seat's own settings.json.
        "seat-claude-settings-env",
        # T-5473 / T-5474: phase-Q residue quarantine + first-launch workspace trust.
        "seat-runtime-residue-quarantine",
        "seat-claude-workspace-trust",
    )
)

# Kinds whose effect changes a harness permission posture (e.g. Codex tool
# approval_mode, Claude MCP registration). The broker NEVER applies these
# (HO ruling T-4861); a test proves none is admitted.
PERMISSION_POSTURE_KINDS: Final[frozenset[str]] = frozenset(
    (
        "codex-telegram-mcp-check",
        "claude-mcp-server-registration",
    )
)

# Mirror of ORG-082 section 1 (substrate families off the daemon surface).
ORG082_SUBSTRATE_FAMILIES: Final[frozenset[str]] = frozenset(
    (
        "account.ensure",
        "account.retire",
        "group.ensure",
        "group.member",
        "acl.converge",
        "owner_mode.set",
        "sudoers_fragment.ensure",
        "sudoers_fragment.retire",
        "unit.system.install",
        "unit.system.enable",
        "dbus_policy.install",
        "dbus_policy.retire",
        "mailbox_dir.ensure",
        "mailbox_dir.retire",
        "linger.enable",
        "linger.disable",
    )
)

_DETAIL_LIMIT = 200


class OutOfEnvelopeKinds(PolicyDenied):
    """A plan contains installer kinds the per-seat executor does not
    implement. Wire name stays ``PolicyDenied``; the bounded detail names
    the refused kinds so an operator sees why (T-2917 ruling i4)."""

    refused_kinds: tuple[str, ...]

    def __init__(self, refused_kinds: Iterable[str]) -> None:
        self.refused_kinds = tuple(sorted(set(refused_kinds)))
        super().__init__("family_denied")

    def detail(self) -> str:
        text = "family_denied: kinds outside executor envelope: " + ",".join(
            self.refused_kinds
        )
        return text[:_DETAIL_LIMIT]

    def __str__(self) -> str:
        return self.detail()


def require_executor_envelope(kinds: Iterable[str]) -> None:
    """Refuse unless every kind is implemented by the per-seat executor.

    Unknown kinds (neither in the envelope nor the named out-of-envelope
    set) are refused the same way: fail closed.
    """

    refused = {kind for kind in kinds if kind not in EXECUTOR_KIND_CLASSES}
    if refused:
        raise OutOfEnvelopeKinds(refused)


# ---------------------------------------------------------------------------
# What THIS revision implements (T-2917 5b, rulings B3/B4)
# ---------------------------------------------------------------------------
# The table above is the whole executor envelope; this is the subset the
# per-seat executor implements in this increment, per execution kind. 5c.2
# adds user_systemd.activate (through the user-manager port; Verify observes
# live state read-only); 5d.2 adds rollback; 5d.3 adds mailbox.probe (the
# capability flip, ruling T-2917:D22: real persona installer plans are no
# longer Unsupported). Both the broker (before any record is written or any grant is
# bound) and the executor (before it touches the host) refuse a plan holding
# a class outside it, as Unsupported. Verify makes no restorable change:
# diagnostics run as-is, seat-root file kinds are only OBSERVED, and since
# 5d.3 the mailbox probe re-runs live, writing and unlinking transient
# dot-named probe files under the hub (ADR section 4: verify "may run
# active probes").
#
# T-2917 5d.2: ``rollback`` holds all four classes (ruling T-2917:D15). A
# rollback restores only RECEIPT-OWNED changed mutations; diagnostics and
# mailbox operations record ``changed=False``, which is never restored, so
# admitting their class here widens no effect. ``reconcile`` is the
# read-only live-state probe of an apply (ruling T-2917:D13): it observes
# exactly the classes apply can change, and changes nothing (for
# mailbox.probe it reports deterministic probe residue, 5d.3).
IMPLEMENTED_EXECUTION_CLASSES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "apply": frozenset(
            ("diagnostics.read", "files.seat_root", "user_systemd.activate", "mailbox.probe")
        ),
        "verify": frozenset(
            ("diagnostics.read", "files.seat_root", "user_systemd.activate", "mailbox.probe")
        ),
        "rollback": frozenset(
            ("diagnostics.read", "files.seat_root", "user_systemd.activate", "mailbox.probe")
        ),
        "reconcile": frozenset(
            ("diagnostics.read", "files.seat_root", "user_systemd.activate", "mailbox.probe")
        ),
    }
)


def unimplemented_kinds(execution_kind: str, kinds: Iterable[str]) -> tuple[str, ...]:
    """The kinds (sorted, unique) this revision cannot execute for
    ``execution_kind``; every kind for an execution kind it does not serve."""

    allowed = IMPLEMENTED_EXECUTION_CLASSES.get(execution_kind, frozenset())
    return tuple(
        sorted(
            {
                kind
                for kind in kinds
                if EXECUTOR_KIND_CLASSES.get(kind) not in allowed
            }
        )
    )


def require_revision_implemented(execution_kind: str, kinds: Iterable[str]) -> None:
    """Fail closed with ``Unsupported`` unless this revision implements
    every kind for ``execution_kind``. The bounded detail names the kinds."""

    kinds = tuple(kinds)
    if execution_kind not in IMPLEMENTED_EXECUTION_CLASSES:
        raise Unsupported(f"{execution_kind} is not executable in this revision")
    missing = unimplemented_kinds(execution_kind, kinds)
    if missing:
        raise Unsupported(
            (f"{execution_kind} not implemented in this revision for: " + ",".join(missing))[
                :_DETAIL_LIMIT
            ]
        )
