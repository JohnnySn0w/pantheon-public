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
        # T-5608 (HO ruling 2026-10-27): files.seat_root + the declared
        # inspection reads in KIND_INSPECTION_READS; basis ORG-052 §8
        # "bounded user-systemd inspection for that seat"; precedent T-4861.
        # Classified by EFFECT: a receipt-owned move (never a delete) of the
        # T-5473 phase-Q residue within the seat root, gated by the shared
        # installer predicate (_residue_quarantine_check).
        "seat-runtime-residue-quarantine": "files.seat_root",
    }
)

#: T-5608 (HO ruling 2026-10-27): the ONLY user-manager port calls a kind of a
#: non-inspection class may make, declared rather than implicit. Each entry is
#: ``(port method, unit role, read purpose)``, in the executor's call order.
#: Unit roles name units, never concrete unit names: ``timer`` is the plan-named
#: consume timer; ``consume`` (the service that timer fires) and ``controller``
#: (``pantheon-runtime-controller@<instance>.service``) are derived ONLY from
#: that timer's instance. The read purpose is a ``tools.user_systemd.ReadPurpose``
#: value ("" for a method that takes none). Every call is a READ: no write,
#: start, stop or enable. The table enters the op-registry digest (gate 32a)
#: next to EXECUTOR_KIND_CLASSES, so changing a declared read changes it.
INSPECTION_PORT_METHODS: Final[frozenset[str]] = frozenset(("unit_file_enablement", "read_unit"))
INSPECTION_UNIT_ROLES: Final[frozenset[str]] = frozenset(("timer", "consume", "controller"))
#: The accepted read-purpose values ("" for a method that takes none). A copy of
#: ``tools.user_systemd.ReadPurpose`` values, because the op registry (in the
#: broker's pinned import closure) must not import that module; a test pins equality.
INSPECTION_READ_PURPOSES: Final[frozenset[str]] = frozenset(
    ("", "stale-reset", "unit-file-active", "unit-fire-poll", "service-settle", "service-observe",
     "timer-elapse", "receiver-active", "reload-state", "service-bringup-observe")
)
KIND_INSPECTION_READS: Final[Mapping[str, tuple[tuple[str, str, str], ...]]] = MappingProxyType(
    {
        "seat-runtime-residue-quarantine": (
            ("unit_file_enablement", "timer", ""),
            ("read_unit", "controller", "service-observe"),
            ("read_unit", "consume", "service-observe"),
        ),
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
        # T-5480: shell-box PreToolUse hook + trust in the seat's ~/.codex/config.toml, seat-local too.
        "seat-codex-hook-config",
        # T-5474: first-launch workspace trust. (T-5473's residue quarantine
        # is admitted above since T-5608.)
        "seat-claude-workspace-trust",
    )
)

# T-5470 partition routing (HO ruling 2026-10-26). These kind sets do NOT
# change the envelope above: direct-fire, unit-fire and timer-enable stay
# admitted kinds. ``planner.partition_installer_plan`` routes a plan's launch
# ops to the seat-local residue only when that plan carries a residue
# must-run-before-launch op, and ``launch_order_violation`` is the standing
# invariant over the resulting effective order.

#: Kinds that can start a seat's runtime. The installer plans exactly one
#: timer-enable (the consume timer, whose fire starts the session) and the op
#: carries no field naming what its unit starts, so every timer-enable counts.
LAUNCH_KINDS: Final[frozenset[str]] = frozenset(
    ("direct-fire", "unit-fire", "timer-enable", "seat-service-enable-observe")
)

#: Admitted kinds whose executor handler needs a launch op's effect from the
#: same run, so they are routed with the launches: timer-observe requires the
#: run's unit-fire evidence and an armed consume timer
#: (``executor.engine.Engine._apply_timer_observe``).
LAUNCH_DEPENDENT_KINDS: Final[frozenset[str]] = frozenset(("timer-observe",))

#: Kinds that must run before EVERY launch op present in the plan: the claude
#: shell-box containment (T-5454), the phase-Q residue quarantine and workspace
#: trust (T-5473 / T-5474), and the legacy ho-resumer decommission (T-5633;
#: planned only on a binding that declares legacy_units: [ho-resumer]).
MUST_RUN_BEFORE_LAUNCH_KINDS: Final[frozenset[str]] = frozenset(
    (
        "seat-claude-settings-env",
        "seat-runtime-residue-quarantine",
        "seat-claude-workspace-trust",
        "seat-service-decommission",
    )
)

#: Rule (b), claude seats only: a claude plan that launches must carry the shell-box
#: containment op. T-5480: the codex hook op (seat-codex-hook-config) is deliberately NOT a
#: must-run-before-launch kind, so T-5608's broker-launch routing for codex plans is unchanged.
CLAUDE_REQUIRED_BEFORE_LAUNCH_KIND: Final = "seat-claude-settings-env"

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


class LaunchBeforeContainment(PolicyDenied):
    """T-5470: in the broker's effective order a launch op would run before a
    must-run-before-launch op, or a claude plan launches without containment.
    Wire name stays ``PolicyDenied``; the bounded detail names the ops."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("family_denied")

    def detail(self) -> str:
        return ("family_denied: launch before containment: " + self.reason)[:_DETAIL_LIMIT]

    def __str__(self) -> str:
        return self.detail()


class RoutedLaunchBoundExceeded(PolicyDenied):
    """T-5470: routing would move more than one op of a launch (or launch-
    dependent) kind to the residue. Wire name stays ``PolicyDenied``."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__("family_denied")

    def detail(self) -> str:
        return f"family_denied: more than one routed {self.kind} op"[:_DETAIL_LIMIT]

    def __str__(self) -> str:
        return self.detail()


def require_routed_launch_bound(kinds: Iterable[str]) -> None:
    """T-5470 partition routing (HO ruling 2026-10-26): routed launches have
    their own bound; B8 cap unchanged. At most ONE routed op per kind, and only
    ``LAUNCH_KINDS | LAUNCH_DEPENDENT_KINDS`` kinds are ever routed."""

    seen: set[str] = set()
    for kind in kinds:
        if kind not in LAUNCH_KINDS | LAUNCH_DEPENDENT_KINDS or kind in seen:
            raise RoutedLaunchBoundExceeded(kind)
        seen.add(kind)


def launch_order_violation(
    order: Iterable[tuple[str, str]],
    *,
    selected_harness: str,
    launch_kinds: frozenset[str] = LAUNCH_KINDS,
    must_run_before_launch_kinds: frozenset[str] = MUST_RUN_BEFORE_LAUNCH_KINDS,
) -> str | None:
    """The T-5470 invariant over an execution order of ``(operation_id, kind)``:
    ``None`` when it holds, else the reason. Every must-run-before-launch op
    present must precede every launch op; a claude plan with any launch op must
    carry ``CLAUDE_REQUIRED_BEFORE_LAUNCH_KIND``. Kinds in neither set are
    ignored, so unknown residue ops change nothing."""

    order = tuple(order)
    launches = [index for index, (_id, kind) in enumerate(order) if kind in launch_kinds]
    if not launches:
        return None
    first_launch = launches[0]
    for index, (operation_id, kind) in enumerate(order):
        if kind in must_run_before_launch_kinds and index > first_launch:
            return f"{operation_id} ({kind}) after {order[first_launch][0]}"
    kinds = {kind for _id, kind in order}
    if selected_harness == "claude" and CLAUDE_REQUIRED_BEFORE_LAUNCH_KIND not in kinds:
        return f"claude seat launches without {CLAUDE_REQUIRED_BEFORE_LAUNCH_KIND}"
    return None


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
