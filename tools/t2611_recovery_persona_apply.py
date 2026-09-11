#!/usr/bin/env python3
"""T-2611 driver: install-done precondition + real persona_installer apply
for Recovery's control-plane cutover.

Folded into tools/t2590_recovery_cutover.sh at two points:
  - `precondition` (preflight, BEFORE the destructive bespoke-disable): a
    read-only check that persona_installer's real apply would NOT refuse --
    catches the exact attempt-2 failure (harness-check refusing because the
    apply's PATH lacked claude-fallback's npm-global bin; see
    tickets/inbox/T-2611.md, root-cause note 2026-08-06T03:50) before Recovery is
    ever taken down.
  - `apply` (execute, AFTER the bespoke session is confirmed dead): the real
    persona_installer apply that installs/converges the consume units and
    activates Recovery under the control plane. Must run under
    PATH=/home/claude-fallback/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
    (the caller -- t2590_recovery_cutover.sh -- sets this via `env`/`PATH=`).

Mirrors the driver pattern in tools/t2381_live_acceptance.py (Manifest.load
-> PersonaInstaller(repo_root=...).plan(...) -> ActorContext -> backend ->
installer.apply()/verify()), adapted for Recovery's real (non-fixture) account
instead of a disposable test fixture.

`precondition` is robust to a HALF-installed account (Recovery's claude-fallback
already carries a partial persona-install footprint from earlier attempts --
see T-2611 team-lead sweep 2026-08-06T04:05): it does not gate on unit
absence. persona_installer's own preflight already treats "absent",
"fingerprint matches", and "installer-managed, may converge" as OK; only a
genuinely conflicting unknown file refuses. `_preflight_problems` below
mirrors that same capability + backend.preflight loop that
PersonaInstaller.apply() runs internally via its private
`_preflight_apply` (persona_installer.py ~1183-1214), reimplemented locally
rather than calling the private method directly -- this only reads
`InstallPlan.operations` / `.operator_actions`, the same fields
tools/test_persona_installer.py already inspects directly from outside the
class, so the surface is stable-by-precedent. The plan/digest/schema/instance
checks in the private method are skipped deliberately: they guard against an
externally-approved-then-later-applied digest mismatch, which cannot occur
here since the plan is built fresh and used immediately in the same call.

Read-only guarantee for `precondition`: HostBackend.preflight() (both
LocalHostBackend and SimulatedHostBackend) only stats/reads files, calls
shutil.which, or issues read-only `systemctl --user is-enabled` /
`show-environment` queries -- no unit is enabled, started, or written until
HostBackend.execute(), which only runs from PersonaInstaller.apply(), never
from `precondition`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.persona_installer import (  # noqa: E402
    ActorContext,
    ApplyReceipt,
    HostBackend,
    InstallerError,
    InstallPlan,
    LocalHostBackend,
    Manifest,
    PersonaInstaller,
    _all_plan_capabilities,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
STAMP_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")


def _validate_stamp(stamp: str) -> str:
    if not STAMP_RE.fullmatch(stamp):
        raise InstallerError("operator stamp must match [A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
    return stamp


def _checkpoint_path(plan: InstallPlan, stamp: str) -> Path:
    receipt_dir = Path(plan.provenance["receipt_dir"])
    for parent in (receipt_dir, *receipt_dir.parents):
        if parent.name == ".local":
            return _checkpoint_path_from_home(parent.parent, plan.persona, stamp)
    raise InstallerError("plan receipt_dir is not beneath an account .local state tree")


def _checkpoint_path_from_home(account_home: Path, persona: str, stamp: str) -> Path:
    return (
        account_home
        / ".local"
        / "state"
        / "pantheon"
        / "personas"
        / persona
        / "staged-cutover-{}.json".format(_validate_stamp(stamp))
    )


def _write_checkpoint(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".staged-cutover.", dir=path.parent)
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


def _read_checkpoint(path: Path) -> Mapping[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InstallerError(f"staged checkpoint missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InstallerError(f"staged checkpoint is not valid JSON: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != "pantheon-persona-staged-cutover/v1":
        raise InstallerError("unsupported staged checkpoint schema")
    return raw


def _build_plan(
    manifest_path: Path,
    persona: str,
    harness: str,
    repo_root: Path,
    root: Path = Path("/"),
) -> tuple[PersonaInstaller, InstallPlan]:
    manifest = Manifest.load(manifest_path)
    installer = PersonaInstaller(repo_root=repo_root)
    plan = installer.plan(manifest, persona, harness_override=harness, root=root)
    return installer, plan


def _build_actor(plan: InstallPlan) -> ActorContext:
    return ActorContext(
        uid=os.geteuid(),
        persona=plan.persona,
        role=plan.role,
        instance=plan.instance,
        capabilities=_all_plan_capabilities(plan),
    )


def _preflight_problems(plan: InstallPlan, backend: HostBackend, actor: ActorContext) -> list[str]:
    """Read-only mirror of PersonaInstaller._preflight_apply's capability +
    backend.preflight loop. See module docstring for why this is
    reimplemented locally instead of calling the private method.

    MAINTENANCE COUPLING: this intentionally re-implements
    PersonaInstaller._preflight_apply's iteration (persona_installer.py
    ~1183-1214), not just calls it. If that iteration's semantics ever
    change upstream (new check kind, changed capability-lookup shape, etc.),
    this mirror must be updated in lockstep -- otherwise `precondition` can
    go green while the real `apply()` still refuses at fire time. Bounded
    risk: run_apply's summary surfaces `applied.errors`/verify failures
    verbatim, so a stale-mirror false-green is immediately diagnosable from
    one grep-able line, not silent.
    """
    problems: list[str] = []
    for action in plan.operator_actions:
        if action.required_capability not in actor.capabilities:
            problems.append(
                f"{action.action_id}: operator action incomplete or actor lacks "
                f"capability {action.required_capability!r}"
            )
    for operation in plan.operations:
        capability = operation.provenance.get("required_capability", "")
        if operation.privilege != "unprivileged" and capability not in actor.capabilities:
            problems.append(f"{operation.operation_id}: actor lacks capability {capability!r}")
            continue
        result = backend.preflight(operation)
        if not result.ok:
            problems.append(f"{operation.operation_id}: {result.classification}: {result.detail}")
    return problems


def check_precondition(
    manifest_path: Path,
    persona: str,
    harness: str,
    repo_root: Path,
    backend: HostBackend | None = None,
    root: Path = Path("/"),
) -> tuple[bool, str]:
    """Read-only: never enables, starts, or writes anything. Returns
    (ok, message) rather than raising, so callers (the cutover script) can
    surface the message verbatim on abort."""
    try:
        installer, plan = _build_plan(manifest_path, persona, harness, repo_root, root=root)
    except InstallerError as exc:
        return False, f"plan precondition failed (consume templates not loadable?): {exc}"

    resolved = shutil.which(harness)
    if resolved is None:
        return False, (
            f"harness {harness!r} not found on PATH={os.environ.get('PATH', '')!r} -- "
            "this is the exact T-2611 attempt-2 failure mode"
        )

    backend = backend or LocalHostBackend()
    actor = _build_actor(plan)
    problems = _preflight_problems(plan, backend, actor)
    if problems:
        return False, "consume-dry-run preflight failed: " + "; ".join(problems)

    return True, (
        f"plan OK ({len(plan.operations)} operations); harness {harness!r} resolved at {resolved}; "
        f"consume-dry-run preflight clean (digest {plan.digest[:12]})"
    )


def run_apply(
    manifest_path: Path,
    persona: str,
    harness: str,
    repo_root: Path,
    backend: HostBackend | None = None,
    root: Path = Path("/"),
) -> tuple[int, str]:
    """The real activation. Only call this after the bespoke session is
    confirmed dead (see cutover script). Returns (exit_code, summary) --
    a refused/failed apply is reported, not raised.

    KNOWN RESIDUAL (deferred, not closed here): if Recovery's pre-existing
    installed pantheon-persona-consume@.service template does NOT byte-match
    persona_installer's desired content, the real apply refuses-rather-than-
    clobbers at unit-write -- the `precondition` check above (a live
    LocalHostBackend preflight) already catches this before the destructive
    disable, so this is defense-in-depth, not the sole guard. The `simulate`
    CLI path already exercised unit convergence and passed clean for recovery
    (T-2611 attempt-2 note 2026-08-06T02:25); if it ever doesn't, this
    function's verbatim APPLY_ERRORS make the refusal immediately
    diagnosable rather than opaque (that opacity was attempt-2's whole cost).
    """
    installer, plan = _build_plan(manifest_path, persona, harness, repo_root, root=root)
    backend = backend or LocalHostBackend(command_timeout=30, timed_fire_timeout=240)
    actor = _build_actor(plan)
    applied = installer.apply(plan, plan.digest, backend, actor)
    verified = installer.verify(plan, backend)
    ok = applied.status in {"applied", "noop"} and verified.status == "verified"
    # VerifyReport has no flat .errors -- failures live per-check inside
    # .layers (see PersonaInstaller.verify, persona_installer.py ~1216-1261).
    verify_failures = [
        f"{check.operation_id}: {check.detail}"
        for layer in verified.layers
        for check in layer.checks
        if not check.ok
    ]
    summary = (
        f"APPLY_STATUS={applied.status} CHANGED={applied.changed} APPLY_ERRORS={list(applied.errors)!r}\n"
        f"VERIFY_STATUS={verified.status} VERIFY_ERRORS={verify_failures!r}"
    )
    return (0 if ok else 1), summary


def stage_disabled(
    manifest_path: Path,
    persona: str,
    harness: str,
    repo_root: Path,
    stamp: str,
    backend: HostBackend | None = None,
    root: Path = Path("/"),
) -> tuple[int, str]:
    installer, plan = _build_plan(manifest_path, persona, harness, repo_root, root=root)
    backend = backend or LocalHostBackend(command_timeout=30, timed_fire_timeout=240)
    actor = _build_actor(plan)
    receipt_id = f"{_validate_stamp(stamp)}-{plan.digest[:16]}"
    report = installer.stage_disabled(plan, plan.digest, backend, actor, receipt_id=receipt_id)
    if report.receipt is None:
        return 1, f"STAGE_STATUS={report.status} STAGE_ERRORS={list(report.errors)!r}"
    checkpoint = _checkpoint_path(plan, stamp)
    _write_checkpoint(
        checkpoint,
        {
            "schema": "pantheon-persona-staged-cutover/v1",
            "phase": "staged",
            "stamp": _validate_stamp(stamp),
            "plan_digest": plan.digest,
            "plan": plan.to_dict(),
            "receipt": report.receipt.to_dict(),
            "checkpoint": str(checkpoint),
        },
    )
    return (
        0 if report.status in {"applied", "noop"} else 1,
        f"STAGE_STATUS={report.status} CHANGED={report.changed} PLAN_DIGEST={plan.digest} CHECKPOINT={checkpoint}",
    )


def _checkpoint_plan_and_receipt(raw: Mapping[str, object]) -> tuple[InstallPlan, ApplyReceipt]:
    plan_raw = raw.get("plan")
    receipt_raw = raw.get("receipt")
    if not isinstance(plan_raw, Mapping) or not isinstance(receipt_raw, Mapping):
        raise InstallerError("staged checkpoint missing plan or receipt")
    return InstallPlan.from_dict(plan_raw), ApplyReceipt.from_dict(receipt_raw)


def activate_staged(
    manifest_path: Path,
    persona: str,
    harness: str,
    repo_root: Path,
    stamp: str,
    backend: HostBackend | None = None,
    root: Path = Path("/"),
) -> tuple[int, str]:
    installer, plan = _build_plan(manifest_path, persona, harness, repo_root, root=root)
    checkpoint = _checkpoint_path(plan, stamp)
    raw = _read_checkpoint(checkpoint)
    checkpoint_plan, staged_receipt = _checkpoint_plan_and_receipt(raw)
    if checkpoint_plan.digest != plan.digest:
        return 1, "ACTIVATE_STATUS=refused ACTIVATE_ERRORS=['checkpoint plan digest mismatch']"
    backend = backend or LocalHostBackend(command_timeout=30, timed_fire_timeout=240)
    actor = _build_actor(plan)
    report = installer.activate_staged(plan, plan.digest, staged_receipt, backend, actor)
    if report.receipt is not None:
        _write_checkpoint(
            checkpoint,
            {
                "schema": "pantheon-persona-staged-cutover/v1",
                "phase": report.receipt.phase,
                "stamp": _validate_stamp(stamp),
                "plan_digest": plan.digest,
                "plan": plan.to_dict(),
                "receipt": report.receipt.to_dict(),
                "checkpoint": str(checkpoint),
            },
        )
    ok = report.status in {"applied", "noop"}
    return (
        0 if ok else 1,
        f"ACTIVATE_STATUS={report.status} CHANGED={report.changed} ACTIVATE_ERRORS={list(report.errors)!r} ROLLBACK={list(report.rollback)!r}",
    )


def check_staged(
    manifest_path: Path,
    persona: str,
    harness: str,
    repo_root: Path,
    stamp: str,
    backend: HostBackend | None = None,
    root: Path = Path("/"),
) -> tuple[bool, str]:
    installer, plan = _build_plan(manifest_path, persona, harness, repo_root, root=root)
    checkpoint = _checkpoint_path(plan, stamp)
    raw = _read_checkpoint(checkpoint)
    checkpoint_plan, staged_receipt = _checkpoint_plan_and_receipt(raw)
    if checkpoint_plan.digest != plan.digest:
        return False, "staged checkpoint plan digest mismatch"
    backend = backend or LocalHostBackend(command_timeout=30, timed_fire_timeout=240)
    actor = _build_actor(plan)
    errors = installer._validate_staged_receipt(plan, staged_receipt, backend, actor)
    if errors:
        return False, "; ".join(errors)
    return True, f"staged checkpoint OK ({checkpoint})"


def rollback_staged(
    persona: str,
    stamp: str,
    account_home: Path,
    backend: HostBackend | None = None,
) -> tuple[int, str]:
    checkpoint = _checkpoint_path_from_home(account_home, persona, stamp)
    raw = _read_checkpoint(checkpoint)
    plan, receipt = _checkpoint_plan_and_receipt(raw)
    if plan.persona != persona:
        return 1, "ROLLBACK_STAGED_STATUS=refused ROLLBACK_ERRORS=['checkpoint persona mismatch']"
    installer = PersonaInstaller(repo_root=Path(plan.provenance.get("repo_root", REPO_ROOT)))
    backend = backend or LocalHostBackend(command_timeout=30, timed_fire_timeout=240)
    actor = _build_actor(plan)
    result = installer.rollback(receipt, backend, actor)
    _write_checkpoint(
        checkpoint,
        {
            "schema": "pantheon-persona-staged-cutover/v1",
            "phase": f"rollback-{result.status}",
            "stamp": _validate_stamp(stamp),
            "plan_digest": plan.digest,
            "plan": plan.to_dict(),
            "receipt": receipt.to_dict(),
            "rollback": {
                "status": result.status,
                "restored": result.restored,
                "errors": list(result.errors),
            },
            "checkpoint": str(checkpoint),
        },
    )
    return (
        0 if result.status in {"rolled-back", "noop"} else 1,
        f"ROLLBACK_STAGED_STATUS={result.status} RESTORED={result.restored} ROLLBACK_ERRORS={list(result.errors)!r}",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("precondition", "apply", "stage-disabled", "check-staged", "activate-staged"):
        child = subparsers.add_parser(name)
        child.add_argument("--manifest", type=Path, required=True)
        child.add_argument("--persona", required=True)
        child.add_argument("--harness", required=True)
        child.add_argument("--repo-root", type=Path, required=True)
        if name in {"stage-disabled", "check-staged", "activate-staged"}:
            child.add_argument("--stamp", required=True)
    rollback = subparsers.add_parser("rollback-staged")
    rollback.add_argument("--persona", required=True)
    rollback.add_argument("--stamp", required=True)
    rollback.add_argument("--account-home", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "precondition":
            ok, message = check_precondition(args.manifest, args.persona, args.harness, args.repo_root)
            print(message)
            return 0 if ok else 1
        if args.command == "stage-disabled":
            exit_code, summary = stage_disabled(
                args.manifest,
                args.persona,
                args.harness,
                args.repo_root,
                args.stamp,
            )
            print(summary)
            return exit_code
        if args.command == "check-staged":
            ok, message = check_staged(
                args.manifest,
                args.persona,
                args.harness,
                args.repo_root,
                args.stamp,
            )
            print(message)
            return 0 if ok else 1
        if args.command == "activate-staged":
            exit_code, summary = activate_staged(
                args.manifest,
                args.persona,
                args.harness,
                args.repo_root,
                args.stamp,
            )
            print(summary)
            return exit_code
        if args.command == "rollback-staged":
            exit_code, summary = rollback_staged(
                args.persona,
                args.stamp,
                args.account_home,
            )
            print(summary)
            return exit_code
        exit_code, summary = run_apply(args.manifest, args.persona, args.harness, args.repo_root)
        print(summary)
        return exit_code
    except InstallerError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
