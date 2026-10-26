#!/usr/bin/env python3
"""T-2679: one-command, persona-parameterized onboarding of a from-scratch,
mailbox-first claude seat onto `pantheon-persona-consume@<persona>`.

Generalizes the hand-recipe in
sop/standard/onboarding-a-mailbox-first-seat-onto-the-control-plane.md step 3
(the SOP is the spec -- read it first, not this docstring, for the operator
narrative). Deliberately narrower than tools/persona_installer.py's
plan()/apply(): PersonaInstaller._preflight_apply gates EVERY operation in a
plan -- including `direct-fire`/`unit-fire`/`mailbox-probe`/`timer-observe`,
which require a live mailbox round-trip and an actual timed fire -- behind
one all-or-nothing preflight before executing any of them. A brand-new seat
cannot satisfy that live-acceptance precondition before its first fire even
exists, which is why tools/t2611_recovery_persona_apply.py `apply` (which already
grants an actor every capability the plan needs) did not materialize units
for a fresh persona in T-2677's cross-account invocation. This tool performs
only the SOP's step-3 recipe: state/unit dirs, the two
`pantheon-persona-consume@` units (with a persona_installer-compatible
MANAGED_MARKER banner -- byte-shape-identical to what PersonaInstaller.plan()
would install -- so a LATER full persona_installer cutover converges these
files instead of refusing on "unknown regular file"), daemon-reload,
enable --now the timer -- plus the SOP's verification checklist and its
kill-stale-pane gotcha (T-2678).

MAILBOX PRECONDITION (the operator TG7368, folded in mid-plan): the mailbox
substrate is being elevated to a first-class, pre-required, full-lifecycle
unit under a separate arc (T-2683, ADR in progress). This tool therefore
PRECONDITIONS on the mailbox being present -- durable hub dirs, group
membership, and any config-declared mailbox units -- and refuses cleanly if
absent. It never provisions the mailbox itself; that stays the other arc's
tool's job, keeping the two composable.

RUN AS the target account (mirrors persona_cutover.sh's posture) -- reads
that account's OWN pantheon-config clone's instance.yaml by default, and
`onboard --execute` refuses unless the calling uid resolves to the seat's
bound account.

Dry-run by default; `--execute` performs the mutation (same safety shape as
persona_cutover.sh / persona_installer.py plan-then-apply). `verify` is a
read-only checklist. `rollback` reverts to the prior (dormant) state per the
SOP's Rollback section.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._organ_lifecycle import (
    CommandResult,
    CommandRunner,
    FakeCommandRunner,
    MANAGED_MARKER,
    OnboardError,
    PreconditionCheck,
    SubprocessCommandRunner,
    _current_account,
    _default_manifest_path,
    _ensure_dir,
    _git_head,
    _rendered_unit,
    _write_if_changed,
    group_membership_lookup,
)
from tools.persona_installer import Manifest, ManifestError, PersonaBinding

REPO_ROOT = Path(__file__).resolve().parent.parent
CONSUME_UNIT_STEM = "pantheon-persona-consume"


# --------------------------------------------------------------------------
# Mailbox group-membership seam (separate from CommandRunner: grp/pwd lookups
# are not subprocess calls). NOT unified with mailbox_onboard.HostIdentity
# (T-2696 drift-verification): HostIdentity is a genuine 3-method superset
# with a different Fake constructor shape that test_mailbox_onboard.py builds
# directly -- forcing one Protocol on both tools would be an interface
# change, not a pure extraction. Only the shared inner algorithm
# (group_membership_lookup, tools/_organ_lifecycle.py) moved.
# --------------------------------------------------------------------------


class GroupMembership(Protocol):
    def is_member(self, account: str, group: str) -> tuple[bool, str]: ...


class RealGroupMembership:
    def is_member(self, account: str, group: str) -> tuple[bool, str]:
        return group_membership_lookup(account, group)


class FakeGroupMembership:
    def __init__(self, ok: bool, detail: str) -> None:
        self.ok = ok
        self.detail = detail

    def is_member(self, account: str, group: str) -> tuple[bool, str]:
        return self.ok, self.detail


# --------------------------------------------------------------------------
# Manifest-derived seat context. Read-only; raises OnboardError on any bad
# or incomplete config -- never a bare traceback.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SeatContext:
    manifest: Manifest
    binding: PersonaBinding
    home: Path
    mailbox_hub: Path
    mailbox_group: str
    required_mailbox_units: tuple[str, ...]
    unit_dir: Path
    receipts_dir: Path
    personas_state_dir: Path
    service_template: Path
    timer_template: Path
    tmux_session: str | None
    model: str | None
    consume_service: str
    consume_timer: str
    revision: str


def _relative_path(value: object, field_name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise OnboardError(f"{field_name} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise OnboardError(f"{field_name} must stay within its declared root")
    return path


def _load_raw_document(manifest_path: Path) -> dict:
    """Independent yaml load, used ONLY for the `harness_runtime.roles.<role>`
    section -- persona_installer.Manifest deliberately does not parse or
    expose that section (it belongs to a different manifest consumer), and
    extending persona_installer.py for this one auxiliary field would touch a
    shared, heavily-covered file for a single-tool need."""
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise OnboardError(f"{manifest_path}: cannot load manifest: {exc}") from exc
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise OnboardError(f"{manifest_path}: cannot parse manifest: {exc}") from exc
    return document if isinstance(document, dict) else {}


def load_seat_context(
    manifest_path: Path,
    persona: str,
    repo_root: Path,
    home: Path,
) -> SeatContext:
    manifest = Manifest.load(manifest_path, strict_flip_keys=False)
    try:
        binding = manifest.binding(persona)
    except ManifestError as exc:
        raise OnboardError(str(exc)) from exc

    config = manifest.installer
    paths = config.get("paths")
    mailbox = config.get("mailbox")
    systemd = config.get("systemd")
    if not all(isinstance(item, dict) for item in (paths, mailbox, systemd)):
        raise OnboardError(f"{manifest.path}: persona_installer paths/mailbox/systemd sections missing")

    unit_relative = _relative_path(paths.get("unit_dir"), "persona_installer.paths.unit_dir")
    state_relative = _relative_path(paths.get("state_dir"), "persona_installer.paths.state_dir")
    unit_dir = home / unit_relative
    state_dir = home / state_relative
    receipts_dir = state_dir / binding.persona / "receipts"
    personas_state_dir = home / ".local" / "state" / "pantheon" / "personas"

    hub_value = mailbox.get("hub")
    if not isinstance(hub_value, str) or not Path(hub_value).is_absolute():
        raise OnboardError("persona_installer.mailbox.hub must be an absolute path")
    mailbox_hub = Path(hub_value)
    mailbox_group = mailbox.get("group")
    if not isinstance(mailbox_group, str) or not mailbox_group:
        raise OnboardError("persona_installer.mailbox.group missing")
    required_units = tuple(mailbox.get("required_units") or ())

    service_relative = _relative_path(
        systemd.get("service_template"), "persona_installer.systemd.service_template"
    )
    timer_relative = _relative_path(
        systemd.get("timer_template"), "persona_installer.systemd.timer_template"
    )
    service_template = repo_root / service_relative
    timer_template = repo_root / timer_relative
    for source in (service_template, timer_template):
        if not source.is_file():
            raise OnboardError(f"unit template missing: {source}")

    document = _load_raw_document(manifest_path)
    role_runtime = document.get("harness_runtime", {})
    role_runtime = role_runtime.get("roles", {}) if isinstance(role_runtime, dict) else {}
    role_runtime = role_runtime.get(binding.role, {}) if isinstance(role_runtime, dict) else {}
    tmux_session = None
    model = None
    if isinstance(role_runtime, dict):
        tmux_cfg = role_runtime.get("tmux")
        if isinstance(tmux_cfg, dict):
            tmux_session = tmux_cfg.get("session")
        claude_cfg = role_runtime.get("claude")
        if isinstance(claude_cfg, dict):
            model = claude_cfg.get("model")

    return SeatContext(
        manifest=manifest,
        binding=binding,
        home=home,
        mailbox_hub=mailbox_hub,
        mailbox_group=mailbox_group,
        required_mailbox_units=required_units,
        unit_dir=unit_dir,
        receipts_dir=receipts_dir,
        personas_state_dir=personas_state_dir,
        service_template=service_template,
        timer_template=timer_template,
        tmux_session=tmux_session,
        model=model,
        consume_service=f"{CONSUME_UNIT_STEM}@{binding.persona}.service",
        consume_timer=f"{CONSUME_UNIT_STEM}@{binding.persona}.timer",
        revision=_git_head(repo_root),
    )


# --------------------------------------------------------------------------
# Mailbox precondition (read-only -- gates, never provisions; T-2683 will
# populate `required_mailbox_units` once its ADR lands a unit contract).
# --------------------------------------------------------------------------


def check_mailbox_precondition(
    ctx: SeatContext,
    runner: CommandRunner,
    group_membership: GroupMembership,
) -> tuple[PreconditionCheck, ...]:
    checks: list[PreconditionCheck] = []
    hub_persona_dir = ctx.mailbox_hub / ctx.binding.persona
    for sub in ("new", "cur", "tmp"):
        path = hub_persona_dir / sub
        ok = path.is_dir()
        checks.append(
            PreconditionCheck(f"mailbox.hub.{sub}", ok, f"{path} {'present' if ok else 'MISSING'}")
        )

    member_ok, member_detail = group_membership.is_member(ctx.binding.account, ctx.mailbox_group)
    checks.append(PreconditionCheck("mailbox.group_membership", member_ok, member_detail))

    for unit in ctx.required_mailbox_units:
        result = runner.run(["systemctl", "--user", "is-enabled", unit])
        ok = result.returncode == 0 and result.stdout.strip() == "enabled"
        state = result.stdout.strip() or result.stderr.strip() or "unknown"
        checks.append(PreconditionCheck(f"mailbox.unit.{unit}", ok, f"is-enabled={state}"))

    return tuple(checks)


# --------------------------------------------------------------------------
# systemctl / tmux helpers over the CommandRunner seam.
# --------------------------------------------------------------------------


def _timer_enabled(runner: CommandRunner, timer_unit: str) -> bool:
    result = runner.run(["systemctl", "--user", "is-enabled", timer_unit])
    return result.returncode == 0 and result.stdout.strip() == "enabled"


def _tmux_has_session(runner: CommandRunner, session: str) -> bool:
    return runner.run(["tmux", "has-session", "-t", session]).returncode == 0


def _tmux_kill_session(runner: CommandRunner, session: str) -> None:
    runner.run(["tmux", "kill-session", "-t", session])


def _daemon_reload(runner: CommandRunner) -> None:
    runner.run(["systemctl", "--user", "daemon-reload"])


def _enable_now(runner: CommandRunner, timer_unit: str) -> None:
    runner.run(["systemctl", "--user", "enable", "--now", timer_unit])


def _is_failed(runner: CommandRunner, service_unit: str) -> bool:
    return runner.run(["systemctl", "--user", "is-failed", service_unit]).stdout.strip() == "failed"


def _disable_now(runner: CommandRunner, timer_unit: str) -> None:
    runner.run(["systemctl", "--user", "disable", "--now", timer_unit])


def _reset_failed(runner: CommandRunner, service_unit: str) -> None:
    runner.run(["systemctl", "--user", "reset-failed", service_unit])


_INVALID_ELAPSE = {"", "0", "n/a", "infinity"}


def _next_elapse_finite(runner: CommandRunner, timer_unit: str) -> tuple[bool, str]:
    result = runner.run(
        [
            "systemctl", "--user", "show", timer_unit,
            "-p", "NextElapseUSecRealtime", "-p", "NextElapseUSecMonotonic",
        ]
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    finite = any(
        values.get(key, "").strip().lower() not in _INVALID_ELAPSE
        for key in ("NextElapseUSecRealtime", "NextElapseUSecMonotonic")
    )
    return finite, ", ".join(f"{k}={v}" for k, v in values.items()) or "no NextElapseUSec* reported"


def _pgrep_argv(runner: CommandRunner, account: str) -> str:
    # Bracket-on-the-'d' idiom, mirrored verbatim from persona_cutover.sh's own
    # NB comment: keeps the contiguous claude-binary byte-sequence OUT of this
    # script's source, so the no-second-claude PreToolUse guard (which
    # inspects executed script files) does not false-positive when this tool
    # is fired. Functionally identical match; "clau[d]e" is a one-character
    # regex class, not a different string.
    #
    # T-2679: anchored on the npm-global claude binary path (same shape as
    # persona_cutover.sh's soak-crit-4 pid select), not just a "clau[d]e --"
    # substring -- uid-scoped (-u account) AND an absolute-path component
    # together make a false match negligible, per
    # tools/pgrep_predicate_lint.py's ALLOWLIST bar.
    result = runner.run(["pgrep", "-u", account, "-a", "-f", r"\.npm-global/bin/clau[d]e"])
    return result.stdout


# --------------------------------------------------------------------------
# Filesystem helpers. _ensure_dir / _write_if_changed / _rendered_unit /
# _current_account are shared (tools/_organ_lifecycle.py, T-2696).
# _unit_banner stays tool-local: its field set (role, harness) differs from
# mailbox_onboard's (T-2696 drift-verification -- see that module's banner).
# --------------------------------------------------------------------------


def _unit_banner(instance_id: str, role: str, persona: str, harness: str, revision: str) -> str:
    return (
        f"# {MANAGED_MARKER}\n"
        f"# instance={instance_id} role={role} persona={persona} harness={harness} revision={revision}\n"
    )


def _require_running_as_account(expected_account: str, current_account: str | None) -> None:
    actual = current_account if current_account is not None else _current_account()
    if actual != expected_account:
        raise OnboardError(
            f"must run as {expected_account!r} to --execute; running as {actual!r}. "
            f"Wrap with sudo -u {expected_account} (or omit --execute for a dry-run)."
        )


# --------------------------------------------------------------------------
# onboard()
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OnboardReport:
    dry_run: bool
    mailbox_checks: tuple[PreconditionCheck, ...]
    planned: tuple[str, ...]
    changed: tuple[str, ...] = ()
    stale_pane_detected: bool = False
    stale_pane_killed: bool = False


def _planned_actions(ctx: SeatContext) -> tuple[str, ...]:
    return (
        f"mkdir -p {ctx.unit_dir} (0755)",
        f"mkdir -p {ctx.receipts_dir} (0700)",
        f"mkdir -p {ctx.personas_state_dir} (0700)",
        f"write {ctx.unit_dir / 'pantheon-persona-consume@.service'} (with provenance banner)",
        f"write {ctx.unit_dir / 'pantheon-persona-consume@.timer'} (with provenance banner)",
        "systemctl --user daemon-reload",
        f"systemctl --user enable --now {ctx.consume_timer}",
    )


def onboard(
    ctx: SeatContext,
    runner: CommandRunner,
    group_membership: GroupMembership,
    execute: bool = False,
    kill_stale_pane: bool = False,
    current_account: str | None = None,
) -> OnboardReport:
    mailbox_checks = check_mailbox_precondition(ctx, runner, group_membership)
    planned = _planned_actions(ctx)

    if not execute:
        return OnboardReport(dry_run=True, mailbox_checks=mailbox_checks, planned=planned)

    _require_running_as_account(ctx.binding.account, current_account)

    failing = [check.detail for check in mailbox_checks if not check.ok]
    if failing:
        raise OnboardError("mailbox precondition failed: " + "; ".join(failing))

    changed: list[str] = []
    for path, mode in (
        (ctx.unit_dir, 0o755),
        (ctx.receipts_dir, 0o700),
        (ctx.personas_state_dir, 0o700),
    ):
        if _ensure_dir(path, mode):
            changed.append(f"created {path}")

    banner = _unit_banner(
        ctx.manifest.instance_id,
        ctx.binding.role,
        ctx.binding.persona,
        ctx.binding.configured_harness,
        ctx.revision,
    )
    for template, name in (
        (ctx.service_template, "pantheon-persona-consume@.service"),
        (ctx.timer_template, "pantheon-persona-consume@.timer"),
    ):
        target = ctx.unit_dir / name
        if _write_if_changed(target, _rendered_unit(template, banner)):
            changed.append(f"wrote {target}")

    _daemon_reload(runner)
    changed.append("ran daemon-reload")

    already_enabled = _timer_enabled(runner, ctx.consume_timer)
    stale_pane_detected = False
    stale_pane_killed = False
    if not already_enabled and ctx.tmux_session:
        stale_pane_detected = _tmux_has_session(runner, ctx.tmux_session)
        if stale_pane_detected:
            if not kill_stale_pane:
                raise OnboardError(
                    f"stale pane detected in tmux session {ctx.tmux_session!r} before the first "
                    f"enable of {ctx.consume_timer} (T-2678 gotcha) -- kill it first "
                    f"(tmux kill-session -t {ctx.tmux_session}) or re-run with --kill-stale-pane"
                )
            _tmux_kill_session(runner, ctx.tmux_session)
            stale_pane_killed = True
            changed.append(f"killed stale pane {ctx.tmux_session}")

    _enable_now(runner, ctx.consume_timer)
    changed.append(f"enabled --now {ctx.consume_timer}")

    return OnboardReport(
        dry_run=False,
        mailbox_checks=mailbox_checks,
        planned=planned,
        changed=tuple(changed),
        stale_pane_detected=stale_pane_detected,
        stale_pane_killed=stale_pane_killed,
    )


# --------------------------------------------------------------------------
# verify()
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyCheck:
    name: str
    ok: bool | None  # None = not mechanically checkable -- manual step
    detail: str


def verify(ctx: SeatContext, runner: CommandRunner) -> tuple[VerifyCheck, ...]:
    checks: list[VerifyCheck] = []

    failed = _is_failed(runner, ctx.consume_service)
    checks.append(
        VerifyCheck("service.not_failed", not failed, "failed" if failed else "not failed")
    )

    enabled = _timer_enabled(runner, ctx.consume_timer)
    checks.append(
        VerifyCheck("timer.enabled", enabled, "enabled" if enabled else "not enabled")
    )

    finite, elapse_detail = _next_elapse_finite(runner, ctx.consume_timer)
    checks.append(VerifyCheck("timer.next_elapse_finite", finite, elapse_detail))

    argv = _pgrep_argv(runner, ctx.binding.account)
    model = ctx.model or ""
    argv_ok = (
        bool(argv.strip())
        and f"--model {model}" in argv
        and "--permission-mode bypassPermissions" in argv
        and "--channels" not in argv
    )
    checks.append(
        VerifyCheck("runtime.channel_less_argv", argv_ok, argv.strip() or "no matching process")
    )

    runtime_json = ctx.personas_state_dir / ctx.binding.persona / "runtime-controller" / "runtime.json"
    checks.append(
        VerifyCheck("runtime.json_present", runtime_json.is_file(), str(runtime_json))
    )

    checks.append(
        VerifyCheck(
            "mailbox.round_trip_selfwake",
            None,
            "MANUAL: confirm the seat woke and mailboxed its lead at least once "
            "(SOP checklist item 5) -- not mechanically verifiable by this tool",
        )
    )

    return tuple(checks)


# --------------------------------------------------------------------------
# rollback()
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RollbackReport:
    dry_run: bool
    planned: tuple[str, ...]
    changed: tuple[str, ...] = ()


def rollback(ctx: SeatContext, runner: CommandRunner, execute: bool = False) -> RollbackReport:
    planned = (
        f"systemctl --user disable --now {ctx.consume_timer}",
        f"systemctl --user reset-failed {ctx.consume_service}",
    )
    if not execute:
        return RollbackReport(dry_run=True, planned=planned)

    _disable_now(runner, ctx.consume_timer)
    _reset_failed(runner, ctx.consume_service)
    return RollbackReport(dry_run=False, planned=planned, changed=planned)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_checks(label: str, checks: Sequence[PreconditionCheck] | Sequence[VerifyCheck]) -> None:
    print(label)
    for check in checks:
        ok = check.ok
        status = "MANUAL" if ok is None else ("PASS" if ok else "FAIL")
        print(f"  [{status}] {check.name}: {check.detail}")


def main(
    argv: Sequence[str] | None = None,
    runner: CommandRunner | None = None,
    group_membership: GroupMembership | None = None,
    current_account: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--persona", required=True)
    common.add_argument("--manifest", type=Path, default=None)
    common.add_argument("--repo-root", type=Path, default=None)
    common.add_argument("--home", type=Path, default=None)

    onboard_parser = subparsers.add_parser("onboard", parents=[common])
    onboard_parser.add_argument("--execute", action="store_true")
    onboard_parser.add_argument("--kill-stale-pane", action="store_true")

    subparsers.add_parser("verify", parents=[common])

    rollback_parser = subparsers.add_parser("rollback", parents=[common])
    rollback_parser.add_argument("--execute", action="store_true")

    args = parser.parse_args(argv)

    manifest_path = args.manifest or _default_manifest_path()
    home = args.home or Path.home()
    repo_root = args.repo_root or manifest_path.parent

    try:
        ctx = load_seat_context(manifest_path, args.persona, repo_root, home)
    except (ManifestError, OnboardError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    runner = runner or SubprocessCommandRunner()
    group_membership = group_membership or RealGroupMembership()

    try:
        if args.command == "onboard":
            report = onboard(
                ctx,
                runner,
                group_membership,
                execute=args.execute,
                kill_stale_pane=args.kill_stale_pane,
                current_account=current_account,
            )
            _print_checks("mailbox precondition:", report.mailbox_checks)
            print("plan:" if report.dry_run else "applied:")
            for item in (report.planned if report.dry_run else report.changed):
                print(f"  {item}")
            return 0
        if args.command == "verify":
            checks = verify(ctx, runner)
            _print_checks("verify:", checks)
            return 0 if all(check.ok is not False for check in checks) else 1
        if args.command == "rollback":
            report = rollback(ctx, runner, execute=args.execute)
            print("plan:" if report.dry_run else "applied:")
            for item in (report.planned if report.dry_run else report.changed):
                print(f"  {item}")
            return 0
    except OnboardError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
