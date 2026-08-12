"""Timer/cron + polling/watch producer-fires acceptance module (T-2556 phase 5 / T-2564).

Enumerates every persona-directed producer named in T-2564's ask -- the
persona-consume timer, the mailbox-wake timer + (legacy) path, the
context-fill-monitor / activity-monitor / inbox-consumer timers, and the
Q4 unit-failure-alert (``OnFailure=`` routed into the mailbox) -- and
nonce-proves that EACH ONE actually fires: a structurally-distinct-named
disposable clone unit is installed (never the real persona unit), its
ExecStart writes a one-shot nonce instead of the real payload, we wait for
that nonce to land, then the clone is torn down. No sampling: every
installed unit in :data:`PRODUCER_CATALOG` (or, for the open-ended alert
leg, every entry :mod:`tools.bringup_acceptance.inventory` already tagged
``unit-failure-alert-into-mailbox``) gets its own proof, never a single
representative standing in for the rest.

Isolation. Real unit state is only ever READ (to decide whether a catalog
producer is installed), never mutated: the clone is a brand-new unit name
(``pantheon-bringup-acceptance-<run_id>-<slug>``) in its own disposable
scope, so there is no real active/enabled state to snapshot or restore for
the timer-cron / watch-producer legs. A real production cadence can be up
to 15 minutes (activity-monitor); waiting on that literally would make this
module operationally unusable, so the clone's trigger is an ACCELERATED
stand-in for the real one -- what's proven is "this systemd trigger
PRIMITIVE (timer alarm / path watch), installed in this exact user-systemd-
manager environment, fires and invokes an ExecStart", not the literal
production cadence. The ONE place this module does touch real state is the
Q4 alert leg's non-env-overridable cooldown file
(``~/.local/state/pantheon/unit_failure_alert_state.json``) -- that is
baselined via :mod:`tools.bringup_acceptance.restore`'s byte-snapshot +
fail-closed verify primitive before anything runs, exactly like every other
live module's real-state touch.

Scope carve-out (the operator, T-2564 ticket transition log): keep-going /
continuation kicker is NOT proven here -- it is its own first-class module
(T-2578 build / T-2579 live-green) with a distinct inject-then-real-turn-
then-resume-then-dedupe-bound proof shape. :data:`PRODUCER_CATALOG`
deliberately has no keep-going entry.

Wake-trigger >=1-of. ``pantheon-mailbox-wake@.path`` is deprecated/absent
under Path A (T-1015) -- the clock-driven ``.timer`` is the live mechanism
today. Both modules see the full :class:`~tools.bringup_acceptance.types.
TargetInventory` (not just their own module_id's slice), so each can check
directly whether ITS wake-trigger catalog member is installed and, if not,
whether the sibling module_id's member is installed instead -- in which
case this module has zero obligation for that family (not a gap). If
NEITHER is installed anywhere, that is a named gap, attributed to
``timer-cron`` only (the canonical/expected side per T-1015) so it surfaces
exactly once rather than being silently dropped or double-counted.
:func:`reconcile_wake_trigger` additionally ships the cross-module >=1-of
combinator as a pure, directly-tested primitive -- this is a PHASE-15
COMPOSITE WIRE-POINT, NOT YET CALLED LIVE (no orchestrator exists yet to
call it between the two modules' independent ``prove()`` runs), mirroring
:mod:`tools.bringup_acceptance.closure`'s own "ship the field in phase 1,
wire it in phase 15" precedent. Do not mistake its presence here for active
wiring.
"""

from __future__ import annotations

import dataclasses
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from tools.bringup_acceptance.evidence import RunPaths, read_inventory, write_module_evidence
from tools.bringup_acceptance.recorder import TraceRecorder, validate_token
from tools.bringup_acceptance.restore import (
    Baseline,
    BaselineTarget,
    CommandRunner,
    default_command_runner,
    record_baseline,
    restore_payload,
    restore_to,
    verify_zero_residue,
)
from tools.bringup_acceptance.types import (
    HardStop,
    InventoryPathEntry,
    MatrixRow,
    ModuleDeclaration,
    ModuleEvidence,
    RestoreEvidence,
    RunContext,
    TargetInventory,
    Tier,
)


TIMER_CRON_ID = "timer-cron"
WATCH_PRODUCER_ID = "watch-producer"
UNIT_FAILURE_ALERT_ID = "unit-failure-alert-into-mailbox"
_WAKE_TRIGGER_FAMILY = "mailbox-wake"

ProducerKind = Literal["timer", "path", "alert"]

DEFAULT_POLL_BUDGET_SECONDS = 8.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.2


# ---------------------------------------------------------------------------
# Fixed producer catalog -- the ticket's own enumerated list, minus keep-going
# (carved out to T-2578/T-2579) and minus the open-ended Q4 alert leg (which
# has no fixed catalog member -- see UnitFailureAlertModule below).
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ProducerSpec:
    """One catalog-known producer's identity + which module_id proves it."""

    name: str
    unit_base: str
    kind: ProducerKind
    module_id: str
    wake_trigger_family: str | None = None


PRODUCER_CATALOG: tuple[ProducerSpec, ...] = (
    ProducerSpec("persona-consume", "pantheon-persona-consume", "timer", TIMER_CRON_ID),
    ProducerSpec(
        "mailbox-wake-timer", "pantheon-mailbox-wake", "timer", TIMER_CRON_ID,
        wake_trigger_family=_WAKE_TRIGGER_FAMILY,
    ),
    ProducerSpec(
        "mailbox-wake-path", "pantheon-mailbox-wake", "path", WATCH_PRODUCER_ID,
        wake_trigger_family=_WAKE_TRIGGER_FAMILY,
    ),
    ProducerSpec("context-fill-monitor", "pantheon-context-fill-monitor", "timer", TIMER_CRON_ID),
    ProducerSpec("activity-monitor", "pantheon-activity-monitor", "timer", TIMER_CRON_ID),
    ProducerSpec("inbox-consumer", "pantheon-inbox-consumer", "timer", TIMER_CRON_ID),
)


def _catalog_entries_for(
    spec: ProducerSpec, target: TargetInventory
) -> tuple[InventoryPathEntry, ...]:
    """Every ``target`` entry that is spec's unit, on the module_id spec expects.

    Matched on the unit's ``base@instance.kind`` shape -- the same shape
    ``inventory.py`` itself parses -- never on a bare substring search.
    """

    matches: list[InventoryPathEntry] = []
    for entry in target.entries:
        if entry.module_id != spec.module_id:
            continue
        name = str(entry.path)
        if "@" not in name:
            continue
        base, _, rest = name.partition("@")
        if base == spec.unit_base and rest.endswith(f".{spec.kind}"):
            matches.append(entry)
    return tuple(matches)


@dataclasses.dataclass(frozen=True)
class WakeFamilyStatus:
    """Which wake-trigger catalog members are actually installed on this target."""

    installed: Mapping[str, tuple[InventoryPathEntry, ...]]


def resolve_wake_family(target: TargetInventory) -> WakeFamilyStatus:
    """Pure function of ``target`` -- no cross-module evidence needed: install
    presence (not fire outcome) is what decides whether a module owns the
    wake-trigger obligation, and installation is directly visible in the full
    inventory regardless of which module_id ends up proving it."""

    installed: dict[str, tuple[InventoryPathEntry, ...]] = {}
    for spec in PRODUCER_CATALOG:
        if spec.wake_trigger_family != _WAKE_TRIGGER_FAMILY:
            continue
        matches = _catalog_entries_for(spec, target)
        if matches:
            installed[spec.name] = matches
    return WakeFamilyStatus(installed=installed)


def _slug(unit_name: str) -> str:
    return validate_token(unit_name.replace("@", "-at-"), label="unit slug")


# ---------------------------------------------------------------------------
# Nonce-fire engine: injectable install/await_fire/teardown, matching
# restore.py's CommandRunner-injection convention -- production wiring shells
# out for real, tests inject in-memory fakes (no live systemd/sleep in the
# suite).
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class UnitPlan:
    """One nonce-fire attempt's identity: the real unit being stood in for,
    the disposable clone's name, and where its nonce marker will land."""

    unit: str
    disposable_unit: str
    kind: ProducerKind
    nonce: str
    marker_path: Path


@dataclasses.dataclass(frozen=True)
class FireOutcome:
    fired: bool
    detail: Mapping[str, Any]


InstallFn = Callable[[UnitPlan], None]
AwaitFireFn = Callable[[UnitPlan], FireOutcome]
TeardownFn = Callable[[UnitPlan], None]


@dataclasses.dataclass(frozen=True)
class FireHarness:
    """The three injectable primitives one nonce-fire attempt needs."""

    install: InstallFn
    await_fire: AwaitFireFn
    teardown: TeardownFn


def nonce_prove_unit(plan: UnitPlan, *, harness: FireHarness) -> FireOutcome:
    """install -> await_fire -> teardown, ALWAYS tearing down (even on failure).

    A timeout (the unit simply never fired) is a legitimate ``FireOutcome
    (fired=False, ...)`` -- not a HardStop. HardStop is reserved for teardown
    itself failing to clean up, i.e. actual residue -- the acceptance run's
    own integrity, not a verdict on the thing under test.
    """

    harness.install(plan)
    try:
        outcome = harness.await_fire(plan)
    finally:
        try:
            harness.teardown(plan)
        except HardStop:
            raise
        except Exception as exc:  # noqa: BLE001 - any teardown failure is residue
            raise HardStop(
                f"teardown failed for disposable unit {plan.disposable_unit}: {exc}"
            ) from exc
    return outcome


def make_live_fire_harness(
    *,
    account: str,
    unit_search_dir: Path,
    runner: CommandRunner = default_command_runner,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    poll_budget_seconds: float = DEFAULT_POLL_BUDGET_SECONDS,
) -> FireHarness:
    """Real systemd --user wiring for timer/path clones. Not exercised by the
    test suite (which injects fakes) -- this is the production glue the
    module's own state machine calls through the same :class:`FireHarness`
    seam, per ``inventory.py``'s own "live gathering is a later wiring
    concern outside this module" precedent.
    """

    def _unit_paths(plan: UnitPlan) -> tuple[Path, Path]:
        service_name = plan.disposable_unit.rsplit(".", 1)[0] + ".service"
        return unit_search_dir / plan.disposable_unit, unit_search_dir / service_name

    def _install(plan: UnitPlan) -> None:
        unit_search_dir.mkdir(parents=True, exist_ok=True)
        trigger_path, service_path = _unit_paths(plan)
        plan.marker_path.parent.mkdir(parents=True, exist_ok=True)
        service_path.write_text(
            "[Unit]\nDescription=bringup-acceptance disposable nonce writer\n\n"
            "[Service]\nType=oneshot\n"
            f"ExecStart=/bin/sh -c 'printf %s \"{plan.nonce}\" > \"{plan.marker_path}\"'\n"
        )
        if plan.kind == "timer":
            trigger_path.write_text(
                "[Unit]\nDescription=bringup-acceptance disposable timer\n\n"
                f"[Timer]\nOnActiveSec=1s\nUnit={service_path.name}\n\n"
                "[Install]\nWantedBy=timers.target\n"
            )
        else:
            watched_dir = plan.marker_path.parent / "watch"
            watched_dir.mkdir(parents=True, exist_ok=True)
            trigger_path.write_text(
                "[Unit]\nDescription=bringup-acceptance disposable path watch\n\n"
                f"[Path]\nPathModified={watched_dir}\nUnit={service_path.name}\n\n"
                "[Install]\nWantedBy=paths.target\n"
            )
        runner(("systemctl", "--user", f"--machine={account}@.host", "daemon-reload"))
        runner(("systemctl", "--user", f"--machine={account}@.host", "start", plan.disposable_unit))
        if plan.kind == "path":
            (plan.marker_path.parent / "watch" / "trigger").write_text("fire")

    def _await_fire(plan: UnitPlan) -> FireOutcome:
        deadline = time.monotonic() + poll_budget_seconds
        while time.monotonic() < deadline:
            if plan.marker_path.is_file() and plan.marker_path.read_text().strip() == plan.nonce:
                return FireOutcome(fired=True, detail={"observed_marker": True})
            sleep(poll_interval_seconds)
        return FireOutcome(
            fired=False, detail={"observed_marker": False, "timeout_seconds": poll_budget_seconds}
        )

    def _teardown(plan: UnitPlan) -> None:
        trigger_path, service_path = _unit_paths(plan)
        runner(("systemctl", "--user", f"--machine={account}@.host", "stop", plan.disposable_unit))
        runner(("systemctl", "--user", f"--machine={account}@.host", "disable", plan.disposable_unit))
        for path in (trigger_path, service_path):
            if path.exists():
                path.unlink()
        runner(("systemctl", "--user", f"--machine={account}@.host", "daemon-reload"))
        if plan.marker_path.exists():
            plan.marker_path.unlink()

    return FireHarness(install=_install, await_fire=_await_fire, teardown=_teardown)


def make_live_alert_fire_harness(
    *,
    account: str,
    unit_search_dir: Path,
    recovery_inbox_override: Path,
    runner: CommandRunner = default_command_runner,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    poll_budget_seconds: float = DEFAULT_POLL_BUDGET_SECONDS,
) -> FireHarness:
    """Real wiring for the Q4 OnFailure->mailbox leg: a disposable FAILING
    service whose ``OnFailure=`` targets the real ``pantheon-unit-failure-
    alert@.service``, with ``PANTHEON_RECOVERY_INBOX`` redirected to a disposable
    dir so the real inbox is never touched. Not exercised by the test suite.
    """

    def _install(plan: UnitPlan) -> None:
        unit_search_dir.mkdir(parents=True, exist_ok=True)
        plan.marker_path.parent.mkdir(parents=True, exist_ok=True)
        service_path = unit_search_dir / plan.disposable_unit
        alert_unit = f"pantheon-unit-failure-alert@{plan.disposable_unit}.service"
        service_path.write_text(
            "[Unit]\nDescription=bringup-acceptance disposable failing unit\n"
            f"OnFailure={alert_unit}\n\n"
            "[Service]\nType=oneshot\n"
            f"Environment=PANTHEON_RECOVERY_INBOX={recovery_inbox_override}\n"
            f"Environment=PANTHEON_ACCEPTANCE_NONCE={plan.nonce}\n"
            "ExecStart=/bin/false\n"
        )
        runner(("systemctl", "--user", f"--machine={account}@.host", "daemon-reload"))
        runner(("systemctl", "--user", f"--machine={account}@.host", "start", plan.disposable_unit))

    def _await_fire(plan: UnitPlan) -> FireOutcome:
        new_dir = plan.marker_path.parent
        deadline = time.monotonic() + poll_budget_seconds
        while time.monotonic() < deadline:
            if new_dir.is_dir():
                for candidate in new_dir.iterdir():
                    if candidate.is_file() and plan.nonce in candidate.read_text(errors="replace"):
                        return FireOutcome(fired=True, detail={"mailbox_file": str(candidate)})
            sleep(poll_interval_seconds)
        return FireOutcome(
            fired=False, detail={"observed_in_mailbox": False, "timeout_seconds": poll_budget_seconds}
        )

    def _teardown(plan: UnitPlan) -> None:
        service_path = unit_search_dir / plan.disposable_unit
        if service_path.exists():
            service_path.unlink()
        runner(("systemctl", "--user", f"--machine={account}@.host", "daemon-reload"))

    return FireHarness(install=_install, await_fire=_await_fire, teardown=_teardown)


# ---------------------------------------------------------------------------
# Re-derive TargetInventory from the run's already-written evidence tree, for
# callers that only have a RunContext -- AcceptanceModule.prove(ctx) doesn't
# thread TargetInventory through per types.py's fixed protocol (only
# declare(target) receives it directly), so a real orchestrator run relies on
# discovery having already written inventory.json before prove() is called.
# ---------------------------------------------------------------------------


def _load_target_from_ctx(ctx: RunContext) -> TargetInventory:
    paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
    document = read_inventory(paths)
    entries = tuple(
        InventoryPathEntry(
            path=Path(item["path"]),
            tier=item["tier"],
            status=item["status"],
            module_id=item["module_id"],
        )
        for item in document.get("entries", ())
    )
    return TargetInventory(persona=document.get("persona", ctx.persona), entries=entries)


# ---------------------------------------------------------------------------
# timer-cron / watch-producer modules
# ---------------------------------------------------------------------------


class _CatalogFireModule:
    """Shared declare/prove/report for the two catalog-driven module_ids.

    ``prove`` optionally accepts ``target`` directly (a real orchestrator
    that already holds it in memory can pass it; ``AcceptanceModule``'s
    ``runtime_checkable`` Protocol only checks method presence, not exact
    signatures, so this extra keyword-only param never breaks conformance).
    Absent that, it re-derives via :func:`_load_target_from_ctx`.
    """

    id: str
    tier: Tier = "core"

    def __init__(self, *, harness: FireHarness) -> None:
        self._harness = harness

    def _specs(self) -> tuple[ProducerSpec, ...]:
        return tuple(spec for spec in PRODUCER_CATALOG if spec.module_id == self.id)

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        matches = tuple(entry for entry in target.entries if entry.module_id == self.id)
        reason = (
            f"{len(matches)} {self.id}-tagged entr{'y' if len(matches) == 1 else 'ies'} on target"
            if matches
            else f"no {self.id}-tagged entries on target -- catalog producers will gap"
        )
        return ModuleDeclaration(module_id=self.id, tier=self.tier, applies=True, reason=reason)

    def prove(self, ctx: RunContext, *, target: TargetInventory | None = None) -> ModuleEvidence:
        resolved_target = target if target is not None else _load_target_from_ctx(ctx)
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
        recorder.append("declare", {"persona": resolved_target.persona})

        wake_status = resolve_wake_family(resolved_target)
        producers: dict[str, Any] = {}
        gaps: list[str] = []
        evidence_paths: list[Path] = []

        for spec in self._specs():
            if spec.wake_trigger_family:
                self._prove_wake_trigger_member(
                    spec, wake_status, recorder, paths, producers, gaps, evidence_paths
                )
                continue

            matches = _catalog_entries_for(spec, resolved_target)
            if not matches:
                gaps.append(f"{spec.name}: producer unit is not installed on this target")
                producers[spec.name] = {"unit": None, "fired": False, "note": "not installed"}
                continue
            self._prove_matches(spec, matches, recorder, paths, producers, gaps, evidence_paths)

        status = "gap" if gaps else "proven"
        recorder.append("verdict", {"status": status, "gaps": gaps})
        return ModuleEvidence(
            module_id=self.id,
            status=status,
            trace_path=recorder.path,
            evidence_paths=tuple(evidence_paths),
            detail={"producers": producers, "gaps": gaps},
        )

    def _prove_wake_trigger_member(
        self,
        spec: ProducerSpec,
        wake_status: WakeFamilyStatus,
        recorder: TraceRecorder,
        paths: RunPaths,
        producers: dict[str, Any],
        gaps: list[str],
        evidence_paths: list[Path],
    ) -> None:
        installed_matches = wake_status.installed.get(spec.name, ())
        if installed_matches:
            if len(installed_matches) != 1:
                raise HardStop(
                    f"{spec.name}: expected exactly one installed instance, "
                    f"found {len(installed_matches)}"
                )
            self._prove_matches(spec, installed_matches, recorder, paths, producers, gaps, evidence_paths)
            fired = producers[spec.name]["fired"]
            unit = producers[spec.name]["unit"]
            producers[spec.name]["wake_trigger"] = {"installed": True, "fired": fired, "unit": unit}
            return

        if wake_status.installed:
            # Sibling family member IS installed elsewhere -- zero obligation here.
            producers[spec.name] = {
                "unit": None,
                "fired": None,
                "note": "not installed on this target; satisfied via sibling module_id for the mailbox-wake family",
                "wake_trigger": {"installed": False, "fired": None, "unit": None},
            }
            return

        # Neither member installed anywhere -- named gap, attributed ONLY to
        # timer-cron so it surfaces exactly once, never dropped, never doubled.
        producers[spec.name] = {
            "unit": None,
            "fired": None,
            "note": "neither mailbox-wake.timer nor mailbox-wake.path is installed on this target",
            "wake_trigger": {"installed": False, "fired": None, "unit": None},
        }
        if self.id == TIMER_CRON_ID:
            gaps.append("wake-trigger: neither mailbox-wake.timer nor mailbox-wake.path is installed")

    def _prove_matches(
        self,
        spec: ProducerSpec,
        matches: Sequence[InventoryPathEntry],
        recorder: TraceRecorder,
        paths: RunPaths,
        producers: dict[str, Any],
        gaps: list[str],
        evidence_paths: list[Path],
    ) -> None:
        for entry in matches:
            unit_name = str(entry.path)
            key = spec.name if len(matches) == 1 else f"{spec.name}:{unit_name}"
            nonce = secrets.token_hex(16)
            slug = _slug(unit_name)
            marker_path = paths.module_evidence_dir(self.id) / f"{slug}.nonce"
            disposable_unit = f"pantheon-bringup-acceptance-{recorder.run_id}-{slug}.{spec.kind}"
            plan = UnitPlan(
                unit=unit_name, disposable_unit=disposable_unit, kind=spec.kind,
                nonce=nonce, marker_path=marker_path,
            )
            recorder.append(
                "install", {"unit": unit_name, "disposable_unit": disposable_unit, "nonce": nonce}
            )
            outcome = nonce_prove_unit(plan, harness=self._harness)
            recorder.append(
                "await_fire", {"unit": unit_name, "fired": outcome.fired, "detail": outcome.detail}
            )
            evidence_path = write_module_evidence(
                paths, self.id, slug,
                {
                    "unit": unit_name, "disposable_unit": disposable_unit, "nonce": nonce,
                    "fired": outcome.fired, "detail": outcome.detail,
                },
            )
            evidence_paths.append(evidence_path)
            producers[key] = {"unit": unit_name, "fired": outcome.fired, "note": None}
            if not outcome.fired:
                gaps.append(f"{spec.name} ({unit_name}): did not fire within budget")

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
        evidence_dir = paths.module_evidence_dir(self.id)
        leftover = tuple(evidence_dir.glob("*.nonce")) if evidence_dir.exists() else ()
        if leftover:
            raise HardStop(
                f"{self.id}: disposable nonce marker(s) left behind after teardown: "
                + ", ".join(str(path) for path in leftover)
            )
        return RestoreEvidence(module_id=self.id, restored=True, detail={"leftover_markers": []})

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        gaps = tuple(evidence.detail.get("gaps", ()))
        summary = f"{self.id}: " + ("; ".join(gaps) if gaps else "every catalog producer nonce-fired")
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=summary, evidence_refs=evidence.evidence_paths,
        )


class TimerCronModule(_CatalogFireModule):
    id = TIMER_CRON_ID


class WatchProducerModule(_CatalogFireModule):
    id = WATCH_PRODUCER_ID


# ---------------------------------------------------------------------------
# Q4: unit-failure-alert-into-mailbox -- open-ended (no fixed catalog member;
# driven entirely by whatever inventory.py already discovered as a host unit
# whose OnFailure= routes into the mailbox).
# ---------------------------------------------------------------------------


class UnitFailureAlertModule:
    id = UNIT_FAILURE_ALERT_ID
    tier: Tier = "optional"

    def __init__(
        self,
        *,
        harness: FireHarness,
        state_path: Path,
        backup_root: Path,
        account: str = "",
    ) -> None:
        self._harness = harness
        self._state_path = state_path
        self._backup_root = backup_root
        self._account = account
        self._baseline: Baseline | None = None

    def declare(self, target: TargetInventory) -> ModuleDeclaration:
        matches = tuple(entry for entry in target.entries if entry.module_id == self.id)
        return ModuleDeclaration(
            module_id=self.id, tier=self.tier, applies=bool(matches),
            reason=(
                f"{len(matches)} {self.id} entries discovered" if matches
                else "no host units route OnFailure into the mailbox on this target"
            ),
        )

    def prove(self, ctx: RunContext, *, target: TargetInventory | None = None) -> ModuleEvidence:
        resolved_target = target if target is not None else _load_target_from_ctx(ctx)
        recorder = TraceRecorder(ctx.evidence_root, module_id=self.id, run_id=ctx.run_id)
        paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
        matches = tuple(entry for entry in resolved_target.entries if entry.module_id == self.id)

        if not matches:
            recorder.append("declared_absent", {"reason": "no matching entries on this target"})
            return ModuleEvidence(
                module_id=self.id, status="proven", trace_path=recorder.path, evidence_paths=(),
                detail={"declared_absent": True, "producers": {}, "gaps": []},
            )

        # Real-state-touch guardrail: baseline the non-env-overridable cooldown
        # file BEFORE any mutation. restore() below verifies byte-identity and
        # HardStops on any mismatch (restore.py's own fail-closed primitive).
        baseline_target = BaselineTarget(
            account=self._account, settings_paths=(self._state_path,), backup_root=self._backup_root,
        )
        self._baseline = record_baseline(baseline_target)
        recorder.append(
            "baseline",
            {
                "state_path": str(self._state_path),
                "existed": self._baseline.settings[str(self._state_path)].existed,
            },
        )

        producers: dict[str, Any] = {}
        gaps: list[str] = []
        evidence_paths: list[Path] = []
        for entry in matches:
            unit_name = str(entry.path)
            slug = _slug(unit_name)
            nonce = secrets.token_hex(16)
            mailbox_new_dir = paths.module_evidence_dir(self.id) / slug / "mailbox" / "new"
            disposable_unit = f"pantheon-bringup-acceptance-{recorder.run_id}-{slug}.service"
            marker_path = mailbox_new_dir / f"{nonce}.msg"
            plan = UnitPlan(
                unit=unit_name, disposable_unit=disposable_unit, kind="alert",
                nonce=nonce, marker_path=marker_path,
            )
            recorder.append(
                "install",
                {"unit": unit_name, "disposable_unit": disposable_unit, "mailbox_dir": str(mailbox_new_dir)},
            )
            outcome = nonce_prove_unit(plan, harness=self._harness)
            recorder.append(
                "await_fire", {"unit": unit_name, "fired": outcome.fired, "detail": outcome.detail}
            )
            evidence_path = write_module_evidence(
                paths, self.id, slug,
                {
                    "unit": unit_name, "disposable_unit": disposable_unit, "nonce": nonce,
                    "fired": outcome.fired, "detail": outcome.detail,
                },
            )
            evidence_paths.append(evidence_path)
            producers[unit_name] = {"unit": unit_name, "fired": outcome.fired}
            if not outcome.fired:
                gaps.append(f"{unit_name}: OnFailure alert did not reach the disposable mailbox")

        status = "gap" if gaps else "proven"
        recorder.append("verdict", {"status": status, "gaps": gaps})
        return ModuleEvidence(
            module_id=self.id, status=status, trace_path=recorder.path,
            evidence_paths=tuple(evidence_paths),
            detail={"declared_absent": False, "producers": producers, "gaps": gaps},
        )

    def restore(self, ctx: RunContext) -> RestoreEvidence:
        if self._baseline is None:
            return RestoreEvidence(
                module_id=self.id, restored=True, detail={"note": "nothing proved -- no baseline taken"}
            )
        restore_to(self._baseline)
        post = record_baseline(self._baseline.target)
        verify_zero_residue(self._baseline, post)  # HardStops, naming the file, on any byte mismatch
        return RestoreEvidence(
            module_id=self.id, restored=True, detail=restore_payload(self._baseline, post, restored=True)
        )

    def report(self, evidence: ModuleEvidence) -> MatrixRow:
        if evidence.detail.get("declared_absent"):
            return MatrixRow(
                module_id=self.id, tier=self.tier, status="declared_absent",
                summary=f"{self.id}: no host units route OnFailure into the mailbox on this target",
                evidence_refs=evidence.evidence_paths,
            )
        gaps = tuple(evidence.detail.get("gaps", ()))
        summary = f"{self.id}: " + (
            "; ".join(gaps) if gaps
            else "every discovered OnFailure->mailbox alert nonce-fired and reached the disposable mailbox"
        )
        return MatrixRow(
            module_id=self.id, tier=self.tier, status=evidence.status,
            summary=summary, evidence_refs=evidence.evidence_paths,
        )


# ---------------------------------------------------------------------------
# Wake-trigger >=1-of composite -- PHASE-15 WIRE-POINT, NOT YET CALLED LIVE.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class WakeTriggerVerdict:
    satisfied: bool
    gap: str | None


def reconcile_wake_trigger(
    *, timer_detail: Mapping[str, Any], watch_detail: Mapping[str, Any]
) -> WakeTriggerVerdict:
    """>=1-of across TimerCronModule's and WatchProducerModule's own
    ``wake_trigger`` sub-details: satisfied iff at least one INSTALLED
    candidate fired. A gap iff neither is installed anywhere, or every
    installed candidate failed to fire.

    PHASE-15 COMPOSITE WIRE-POINT -- NOT YET CALLED LIVE. No orchestrator
    exists yet to invoke this between the two modules' independent prove()
    runs; it ships now as a pure, directly-tested primitive per
    ``closure.py``'s own "ship the field in phase 1, wire it in phase 15"
    precedent. Do not mistake its presence here for active wiring.
    """

    timer_leg = timer_detail.get("producers", {}).get("mailbox-wake-timer", {}).get("wake_trigger")
    watch_leg = watch_detail.get("producers", {}).get("mailbox-wake-path", {}).get("wake_trigger")
    candidates = [leg for leg in (timer_leg, watch_leg) if leg and leg.get("installed")]
    if not candidates:
        return WakeTriggerVerdict(
            satisfied=False,
            gap="wake-trigger: neither mailbox-wake.timer nor mailbox-wake.path is installed",
        )
    if any(candidate.get("fired") for candidate in candidates):
        return WakeTriggerVerdict(satisfied=True, gap=None)
    return WakeTriggerVerdict(
        satisfied=False, gap="wake-trigger: installed candidate(s) present but none fired"
    )
