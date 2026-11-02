#!/usr/bin/env python3
"""T-5449: the seat-local ``apply`` / ``rollback`` verbs of the persona installer.

Intent: one installer run puts a whole seat on the Pantheon control plane
(T-5446). The residue ops the host_control executor does not admit (seat
periodics, the controller, the ho-resumer decommission) are executed here,
SEAT-LOCAL, as the target account through ``LocalHostBackend`` -- never through
the broker.

``apply`` is DRY unless ``--execute`` is given. The dry run builds the plan and
runs every operation's ``HostBackend.preflight`` against the real host (the same
loop ``PersonaInstaller.apply`` runs), prints the plan digest and one verdict per
operation, and exits non-zero if anything would refuse. ``--execute`` must be
given the digest the dry run printed (``--plan-digest``), so the operator
approves exactly what runs.

Workflows (``--workflow``):
  all-in-one      default. Whole install in one run (the operator TG 12274).
  stage-prefix    install units + disabled first fire, stop before activation.
  staged-activate activate a previously staged receipt (``--receipt``).
For a seat that is ALREADY LIVE, stage-prefix then staged-activate is the
recommended pair: it leaves a rollback point and a window before the consume
fire. There is no auto-detection.

Every apply writes its receipt through the plan's receipt sink and, beside it,
``<receipt-id>.plan.json`` so ``rollback --receipt <id>`` is pinned to the plan
that actually ran, not to a plan re-derived from a repo that has since moved.
"""

from __future__ import annotations

import json
import dataclasses
import os
import pwd
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.persona_installer import (  # noqa: E402
    ActorContext,
    ApplyReceipt,
    EXECUTION_CONTEXT_ALL_IN_ONE,
    EXECUTION_CONTEXT_STAGE_PREFIX,
    EXECUTION_CONTEXT_STAGED_ACTIVATE,
    HostBackend,
    InstallerError,
    InstallPlan,
    LocalHostBackend,
    Manifest,
    OperationFailed,
    PreflightResult,
    PersonaInstaller,
    _all_plan_capabilities,
    _path_containment_problem,
    _plan_allowed_roots,
    preflight_in_context,
    workflow_scopes,
)

# Equals instance.yaml persona_installer.paths.state_dir (guarded by a test).
DEFAULT_STATE_DIR_RELATIVE = ".local/state/pantheon/persona-installer"

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "instance.yaml"
WORKFLOWS = ("all-in-one", "stage-prefix", "staged-activate", "reconcile-keep-going")
_RECEIPT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
PLAN_SIDECAR_SCHEMA = "pantheon-persona-install-plan-sidecar/v1"


class ApplyRefused(InstallerError):
    """The verb refused before any mutation."""


def actor_for_plan(plan: InstallPlan, *, uid: int | None = None) -> ActorContext:
    return ActorContext(
        uid=os.geteuid() if uid is None else uid,
        persona=plan.persona,
        role=plan.role,
        instance=plan.instance,
        capabilities=_all_plan_capabilities(plan),
    )


def collect_preflight_problems(
    plan: InstallPlan, backend: HostBackend, actor: ActorContext
) -> list[str]:
    """Every refusal ``PersonaInstaller._preflight_apply`` would raise, collected
    instead of stopping at the first. Read-only given a read-only
    ``backend.preflight`` (proved for LocalHostBackend by the dry-run test)."""
    return [
        f"{item['operation_id']}: {item['classification']}: {item['detail']}"
        for item in preflight_verdicts(plan, backend, actor)
        if item["state"] == "refused"
    ]


# T-5449 (HO ruling): kinds whose preflight WRITES (a transient probe). A dry run must be
# honestly read-only, so under dry_run=True these are NOT preflighted; they get the
# distinct "deferred" state below. execute keeps the probe unchanged.
WRITING_PREFLIGHT_KINDS = frozenset({"mailbox-writable-root-check"})
PROBE_DEFERRED_DETAIL = "probe deferred to execute (would write)"


def _verdict(operation_id: str, kind: str, state: str, classification: str, detail: str) -> dict[str, Any]:
    """state is one of pass | refused | deferred. ``ok`` is True ONLY for pass, so a
    deferred op never reads as passed; only ``refused`` counts as a problem."""
    return {
        "operation_id": operation_id,
        "kind": kind,
        "state": state,
        "ok": state == "pass",
        "classification": classification,
        "detail": detail,
    }


def preflight_verdicts(
    plan: InstallPlan,
    backend: HostBackend,
    actor: ActorContext,
    *,
    dry_run: bool = False,
    workflow: str = "all-in-one",
) -> list[dict[str, Any]]:
    """One verdict per operation. ``dry_run`` is threaded explicitly (never read
    from the environment): when True, kinds in WRITING_PREFLIGHT_KINDS are deferred
    instead of preflighted, so nothing in the dry run writes.

    T-5472: ``workflow`` is the one execute will run under. It selects the operation
    scopes ``_preflight_apply`` preflights (all-in-one skips ``staged-cutover`` ops) and
    is handed to the backend so workflow-only refusals (the staged-activate timer-enable
    boundary) are predicted."""
    if workflow not in WORKFLOWS:
        raise ApplyRefused(f"unknown workflow {workflow!r}")
    scopes = workflow_scopes(workflow)
    context = {
        "all-in-one": EXECUTION_CONTEXT_ALL_IN_ONE,
        "reconcile-keep-going": EXECUTION_CONTEXT_ALL_IN_ONE,
        "stage-prefix": EXECUTION_CONTEXT_STAGE_PREFIX,
        "staged-activate": EXECUTION_CONTEXT_STAGED_ACTIVATE,
    }[workflow]
    verdicts: list[dict[str, Any]] = []
    for action in plan.operator_actions:
        if action.required_capability not in actor.capabilities:
            verdicts.append(
                _verdict(
                    action.action_id, "operator-action", "refused", "capability",
                    f"operator action incomplete or actor lacks capability "
                    f"{action.required_capability!r}",
                )
            )
    for operation in plan.operations:
        if operation.execution_scope not in scopes:
            continue
        capability = operation.provenance.get("required_capability", "")
        if operation.privilege != "unprivileged" and capability not in actor.capabilities:
            verdicts.append(
                _verdict(operation.operation_id, operation.kind, "refused", "capability",
                         f"actor lacks capability {capability!r}")
            )
            continue
        if dry_run and operation.kind in WRITING_PREFLIGHT_KINDS:
            verdicts.append(
                _verdict(operation.operation_id, operation.kind, "deferred", "deferred",
                         PROBE_DEFERRED_DETAIL)
            )
            continue
        try:
            result = preflight_in_context(backend, operation, context)
        except OperationFailed as exc:  # a failing read is a verdict, not a crash
            result = PreflightResult(False, "preflight-error", str(exc))
        verdicts.append(
            _verdict(operation.operation_id, operation.kind, "pass" if result.ok else "refused",
                     result.classification, result.detail)
        )
    return verdicts


def build_plan(
    manifest_path: Path,
    persona: str,
    *,
    harness: str | None = None,
    repo_root: Path = REPO_ROOT,
    root: Path = Path("/"),
    maintenance_bypass: bool = False,
    workflow: str = "all-in-one",
) -> tuple[PersonaInstaller, InstallPlan, Manifest]:
    manifest = Manifest.load(manifest_path)
    installer = PersonaInstaller(repo_root=repo_root)
    # T-5475: the flag is a plan input (digest-bound); only passed when set so every default
    # call is the exact pre-T-5475 call
    extra = {"maintenance_bypass": True} if maintenance_bypass else {}
    plan = installer.plan(manifest, persona, harness_override=harness, root=root, **extra)
    if workflow == "reconcile-keep-going":
        keep_ids = {
            "connective.config-dir",
            "connective.systemd-dir",
            "connective.unit-dir",
            "connective.local-dir",
            "connective.local-state-dir",
            "connective.pantheon-state-dir",
            "connective.installer-state-dir",
            "connective.state-dir",
            "connective.receipt-dir",
            "periodic.keep-going.service-unit",
            "periodic.keep-going.timer-unit",
            "periodic.daemon-reload",
            "periodic.keep-going.timer-enable",
            "periodic.keep-going.timer-observe",
        }
        selected = (
            operation
            for operation in plan.operations
            if operation.operation_id in keep_ids
            or (
                operation.kind == "seat-service-decommission"
                and operation.operation_id.endswith("codex-keep-going.decommission")
            )
        )
        operations = tuple(
            dataclasses.replace(
                operation,
                desired={**operation.desired, "after": "periodic.daemon-reload"},
            )
            if operation.kind == "seat-periodic-timer-enable"
            else operation
            for operation in selected
        )
        plan = InstallPlan.create(
            schema=plan.schema,
            manifest_version=plan.manifest_version,
            instance=plan.instance,
            role=plan.role,
            persona=plan.persona,
            account=plan.account,
            configured_harness=plan.configured_harness,
            selected_harness=plan.selected_harness,
            root=plan.root,
            operations=operations,
            operator_actions=(),
            provenance=plan.provenance,
        )
    return installer, plan, manifest


def _account_uid(account: str) -> int | None:
    try:
        return pwd.getpwnam(account).pw_uid
    except KeyError:
        return None


def require_target_account(
    plan: InstallPlan,
    *,
    euid: int | None = None,
    uid_of: Callable[[str], int | None] = _account_uid,
) -> None:
    """The verb runs AS the target account (sudo -u <account> from its own clone)."""
    effective = os.geteuid() if euid is None else euid
    wanted = uid_of(plan.account)
    if wanted is None:
        raise ApplyRefused(f"target account {plan.account!r} does not exist on this host")
    if wanted != effective:
        raise ApplyRefused(
            f"must run as {plan.account!r} (uid {wanted}); effective uid is {effective}"
        )


def ensure_account_path(account_home: Path, environ: dict[str, str] | None = None) -> None:
    """T-2611 attempt-2 lesson: harness-check resolves the harness on PATH, and a
    sudo -u shell may lack the account's npm-global bin."""
    env = os.environ if environ is None else environ
    bin_dir = str(account_home / ".npm-global" / "bin")
    parts = [item for item in env.get("PATH", "").split(os.pathsep) if item]
    if bin_dir not in parts:
        env["PATH"] = os.pathsep.join([bin_dir, *parts])


def _account_home(plan: InstallPlan) -> Path:
    receipt_dir = Path(plan.provenance["receipt_dir"])
    for parent in receipt_dir.parents:
        if parent.name == ".local":
            return parent.parent
    return Path("/home") / plan.account


def _default_backend() -> HostBackend:
    return LocalHostBackend(command_timeout=30, timed_fire_timeout=240)


def dry_run(
    manifest_path: Path,
    persona: str,
    *,
    workflow: str = "all-in-one",
    harness: str | None = None,
    repo_root: Path = REPO_ROOT,
    root: Path = Path("/"),
    backend: HostBackend | None = None,
    euid: int | None = None,
    uid_of: Callable[[str], int | None] = _account_uid,
    maintenance_bypass: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Plan + full preflight. Never mutates (see the module docstring and
    test_persona_installer_apply_cli.DryRunIsReadOnlyTests). Operations whose
    preflight would write (the mailbox writable-root probe) are DEFERRED to execute:
    a distinct state that is neither a pass nor a refusal and does not fail the run."""
    _installer, plan, _manifest = build_plan(
        manifest_path, persona, harness=harness, repo_root=repo_root, root=root,
        maintenance_bypass=maintenance_bypass, workflow=workflow,
    )
    require_target_account(plan, euid=euid, uid_of=uid_of)
    ensure_account_path(_account_home(plan))
    verdicts = preflight_verdicts(
        plan, backend or _default_backend(), actor_for_plan(plan, uid=euid), dry_run=True,
        workflow=workflow,
    )
    problems = [v for v in verdicts if v["state"] == "refused"]
    deferred = [v for v in verdicts if v["state"] == "deferred"]
    report = {
        "mode": "dry-run",
        "persona": plan.persona,
        "account": plan.account,
        "workflow": workflow,
        "plan_digest": plan.digest,
        "operation_count": len(plan.operations),
        "operations": verdicts,
        "problems": [f"{v['operation_id']}: {v['classification']}: {v['detail']}" for v in problems],
        "deferred": [f"{v['operation_id']}: {v['detail']}" for v in deferred],
        "ok": not problems,
        "next": (
            f"re-run with --execute --plan-digest {plan.digest} to apply exactly this plan"
            if not problems
            else "resolve the problems above; nothing was changed"
        ),
    }
    return (0 if not problems else 1), report


def _atomic_json(directory: Path, name: str, payload: Mapping[str, Any]) -> Path:
    path = directory / name
    content = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".plan-sidecar.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def _write_plan_sidecar(plan: InstallPlan, receipt: ApplyReceipt) -> Path:
    problem = _path_containment_problem(
        "receipt_dir", plan.provenance["receipt_dir"], "dir", _plan_allowed_roots(plan)
    )
    if problem:
        raise ApplyRefused(f"plan sidecar target path check: {problem}")
    directory = Path(plan.provenance["receipt_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    return _atomic_json(
        directory,
        f"{receipt.receipt_id}.plan.json",
        {"schema": PLAN_SIDECAR_SCHEMA, "receipt_id": receipt.receipt_id, "plan": plan.to_dict()},
    )


def _load_receipt_and_plan(plan_for_dir: InstallPlan, receipt_id: str) -> tuple[ApplyReceipt, InstallPlan]:
    return _load_receipt_and_plan_from_dir(Path(plan_for_dir.provenance["receipt_dir"]), receipt_id)


def _load_receipt_and_plan_from_dir(directory: Path, receipt_id: str) -> tuple[ApplyReceipt, InstallPlan]:
    if not _RECEIPT_ID_RE.fullmatch(receipt_id):
        raise ApplyRefused("receipt id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
    try:
        receipt = ApplyReceipt.from_dict(json.loads((directory / f"{receipt_id}.json").read_text("utf-8")))
        sidecar = json.loads((directory / f"{receipt_id}.plan.json").read_text("utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        raise ApplyRefused(f"receipt or plan sidecar unreadable for {receipt_id!r}: {exc}") from exc
    if sidecar.get("schema") != PLAN_SIDECAR_SCHEMA or not isinstance(sidecar.get("plan"), Mapping):
        raise ApplyRefused("plan sidecar schema mismatch")
    return receipt, InstallPlan.from_dict(sidecar["plan"])


def execute(
    manifest_path: Path,
    persona: str,
    *,
    plan_digest: str,
    workflow: str = "all-in-one",
    receipt_id: str | None = None,
    harness: str | None = None,
    repo_root: Path = REPO_ROOT,
    root: Path = Path("/"),
    backend: HostBackend | None = None,
    euid: int | None = None,
    uid_of: Callable[[str], int | None] = _account_uid,
    maintenance_bypass: bool = False,
) -> tuple[int, dict[str, Any]]:
    if workflow not in WORKFLOWS:
        raise ApplyRefused(f"unknown workflow {workflow!r}")
    installer, plan, manifest = build_plan(
        manifest_path, persona, harness=harness, repo_root=repo_root, root=root,
        maintenance_bypass=maintenance_bypass, workflow=workflow,
    )
    require_target_account(plan, euid=euid, uid_of=uid_of)
    if plan_digest != plan.digest:
        raise ApplyRefused(
            f"--plan-digest {plan_digest!r} does not match the freshly built plan "
            f"{plan.digest!r}; re-run the dry run from this commit and approve its digest"
        )
    ensure_account_path(_account_home(plan))
    backend = backend or _default_backend()
    actor = actor_for_plan(plan, uid=euid)
    if workflow in {"all-in-one", "reconcile-keep-going"}:
        report = installer.apply(plan, plan.digest, backend, actor, manifest=manifest)
    elif workflow == "stage-prefix":
        report = installer.stage_disabled(plan, plan.digest, backend, actor, manifest=manifest)
    else:
        if not receipt_id:
            raise ApplyRefused("staged-activate requires --receipt <staged receipt id>")
        staged, staged_plan = _load_receipt_and_plan(plan, receipt_id)
        if staged_plan.digest != plan.digest:
            raise ApplyRefused("staged plan digest does not match the freshly built plan")
        if staged.phase != "staged":
            raise ApplyRefused(f"receipt {receipt_id!r} is phase {staged.phase!r}, not 'staged'")
        report = installer.activate_staged(plan, plan.digest, staged, backend, actor, manifest=manifest)
    sidecar = None
    if report.receipt is not None:
        sidecar = str(_write_plan_sidecar(plan, report.receipt))
    summary = {
        "mode": "execute",
        "persona": plan.persona,
        "workflow": workflow,
        "plan_digest": plan.digest,
        "status": report.status,
        "changed": report.changed,
        "errors": list(report.errors),
        "rollback": list(report.rollback),
        "receipt_id": report.receipt.receipt_id if report.receipt else None,
        "plan_sidecar": sidecar,
    }
    return (0 if report.status in {"applied", "noop"} else 1), summary


def _account_of_euid(euid: int) -> tuple[str, Path]:
    try:
        entry = pwd.getpwuid(euid)
    except KeyError as exc:
        raise ApplyRefused(f"effective uid {euid} has no passwd entry") from exc
    return entry.pw_name, Path(entry.pw_dir)


def _break_glass_receipt_dir(
    persona: str,
    receipt_dir: Path | None,
    *,
    root: Path,
    euid: int,
    account_of_euid: Callable[[int], tuple[str, Path]],
) -> tuple[str, Path]:
    """(effective account, receipt dir) with no manifest. The dir is bounded to the
    effective account's home (trusted: from passwd, not from the untrusted sidecar)."""
    account, home = account_of_euid(euid)
    if root != Path("/"):
        home = root / home.relative_to("/")
    directory = receipt_dir or (home / DEFAULT_STATE_DIR_RELATIVE / persona / "receipts")
    problem = _path_containment_problem("receipt_dir", str(directory), "dir", (str(home),))
    if problem:
        raise ApplyRefused(f"break-glass receipt dir check: {problem}")
    return account, directory


def rollback_receipt(
    manifest_path: Path,
    persona: str,
    receipt_id: str,
    *,
    break_glass: bool = False,
    repo_root: Path = REPO_ROOT,
    root: Path = Path("/"),
    backend: HostBackend | None = None,
    euid: int | None = None,
    uid_of: Callable[[str], int | None] = _account_uid,
    receipt_dir: Path | None = None,
    account_of_euid: Callable[[int], tuple[str, Path]] = _account_of_euid,
) -> tuple[int, dict[str, Any]]:
    """Roll back one receipt. With ``break_glass`` the manifest is NEVER loaded, even
    if it would load: break-glass is an explicit operator opt-in for when instance.yaml
    is the broken thing, and one deterministic path beats try-then-fallback. The
    receipt dir comes from the effective account's home (+ ``receipt_dir`` override),
    the account from the digest-checked sidecar plan, and the euid guard still runs
    against that account before any mutation. Without it, behaviour is unchanged."""
    if receipt_dir is not None and not break_glass:
        raise ApplyRefused("receipt_dir override is only valid with break_glass")
    if break_glass:
        installer = PersonaInstaller(repo_root=repo_root)
        effective = os.geteuid() if euid is None else euid
        account, directory = _break_glass_receipt_dir(
            persona, receipt_dir, root=root, euid=effective, account_of_euid=account_of_euid
        )
        receipt, plan = _load_receipt_and_plan_from_dir(directory, receipt_id)
        if plan.account != account:
            raise ApplyRefused(
                f"sidecar plan account {plan.account!r} does not match effective account {account!r}"
            )
        require_target_account(plan, euid=euid, uid_of=uid_of)
        manifest = None
    else:
        installer, plan_now, manifest = build_plan(manifest_path, persona, repo_root=repo_root, root=root)
        require_target_account(plan_now, euid=euid, uid_of=uid_of)
        receipt, plan = _load_receipt_and_plan(plan_now, receipt_id)
    if plan.persona != persona:
        raise ApplyRefused("receipt persona mismatch")
    ensure_account_path(_account_home(plan))
    result = installer.rollback(
        receipt,
        backend or _default_backend(),
        actor_for_plan(plan, uid=euid),
        plan=plan,
        manifest=manifest,
        break_glass=break_glass,
    )
    summary = {
        "mode": "rollback",
        "persona": persona,
        "receipt_id": receipt_id,
        "status": result.status,
        "restored": result.restored,
        "errors": list(result.errors),
        "break_glass": result.break_glass,
    }
    return (0 if result.status in {"rolled-back", "noop"} else 1), summary


__all__ = [
    "ApplyRefused",
    "DEFAULT_STATE_DIR_RELATIVE",
    "WORKFLOWS",
    "actor_for_plan",
    "build_plan",
    "collect_preflight_problems",
    "dry_run",
    "execute",
    "preflight_verdicts",
    "require_target_account",
    "rollback_receipt",
]
