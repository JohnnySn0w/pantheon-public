#!/usr/bin/env python3
"""T-2683: convergent, manifest-driven full-lifecycle installer for the ORG-026
mailbox organ (hub-owner infra + per-persona provisioning).

Realizes ORG-049 (manifest-driven convergent install) + ORG-053 section 6 (every
organ gets install/modify/configure/monitor/remove) for the mailbox substrate,
which `tools/provision_mailbox.sh` covered only partially (4 of the 8 live
personas, no test coverage, its own hardcoded persona->account map drifting
from instance.yaml -- see notes/t2683-mailbox-arch-survey.md) and which had NO
removal path at all. This tool retires that script.

Two-step shape, mirroring the sibling `tools/consume_onboard.py` (T-2679):

  hub-setup  -- persona-INDEPENDENT (ORG-049 Layer-0/1). Convergently ensures
                the hub-owner account, its home, the mailbox hub root dir, the
                `pantheon-mailbox` group, linger, and verifies the wake-unit
                SOURCE templates exist in tools/systemd/ (a precondition check;
                installing per-account COPIES of those templates is `provision`'s
                job, not this step's). Dry-run by default; --execute performs
                privileged host mutation (useradd/groupadd/loginctl) via the
                CommandRunner seam.

  provision  -- manifest-driven, DYNAMIC-N (never a hardcoded persona count).
                Enumerates every persona instance.yaml `roles:` declares (via
                `_all_persona_bindings`, mirroring `Manifest.binding()`'s own
                iteration -- see the GAP-1 note below for why this reads
                `persona_installer.Manifest` directly rather than going through
                `tools/agent_role_map.py`). Per persona, two INDEPENDENTLY
                gated legs, each executing only when the invoking account
                matches the leg's required actor (a uid guard, not an internal
                `sudo -u` wrap -- same posture as consume_onboard.onboard()):

                  hub leg  (as the hub-owner): tools.mailbox.provision() the
                            persona's {tmp,new,cur} maildir + best-effort chgrp
                            to the mailbox group.
                  unit leg (as the persona's own account): install the
                            `pantheon-mailbox-wake@.{service,timer}` template
                            pair (with a MANAGED_MARKER provenance banner) into
                            that account's ~/.config/systemd/user/, daemon-
                            reload, enable --now the persona's instantiated
                            timer -- but ONLY for personas on the
                            `persona_installer.mailbox.wake_timer_personas`
                            allowlist (default-deny when absent). Consume-plane
                            seats (woken by the consume runtime kick) and
                            mnemosyne (whose wake pane is the operator's own chat
                            surface) are deliberately excluded; a non-allowlisted
                            persona's unit leg is skipped-and-surfaced (T-2702).

                Invoked once per relevant account (hub-owner, then each
                persona), the whole mailbox organ converges -- no single
                invocation needs cross-account privilege.

  verify     -- read-only. Hub subdir presence, group membership, wake-timer
                enabled state, per persona (or all personas if --persona is
                omitted).

  teardown   -- dry-run by default. Disables + removes one persona's wake
                unit (as that persona's account); `--purge-hub` additionally
                removes that persona's hub maildir tree (as the hub-owner);
                `--remove-hub-root` (further, independent opt-in, as the
                hub-owner) removes the hub root directory once empty. Closes
                the "Removable" lifecycle stage the survey found entirely
                absent. Deliberately does NOT userdel the hub-owner account --
                an irreversible, no-rollback op has no place behind a script
                flag; that stays a documented manual step.

GAP 1 (T-2683 plan-gate, HO-approved): persona enumeration goes through
`persona_installer.Manifest.roles` (mirroring `Manifest.binding()`'s own
per-role iteration), NOT through `tools/agent_role_map.py`. agent_role_map's
persona/account maps load a FIXED path (repo-root instance.yaml) at IMPORT
time -- not parameterized by a manifest path -- so they cannot be driven by a
fixture manifest in tests, which the manifest-driven-N test requires. Manifest
reads the SAME source (`roles:`) through the same testable, parameterized seam
consume_onboard.py already uses for its own single-persona lookup.

GAP 2 (T-2683 plan-gate, HO-approved): `persona_installer.mailbox.required_units`
in instance.yaml is left UNPOPULATED by this ticket. consume_onboard.py's
`check_mailbox_precondition()` checks each `required_units` entry as a LITERAL
unit-name string (no `{persona}` templating) -- a single shared entry can't
express "this seat's own wake timer" without either being wrong for every
seat or needing that templating added to consume_onboard.py, which this
ticket is barred from touching. Follow-up (to be filed by HO, not this
worker): add per-persona `{persona}` templating to consume_onboard's
required_units check, then populate `persona_installer.mailbox.required_units`
here.

Dry-run by default everywhere; `--execute` performs the mutation (same safety
shape as consume_onboard.py / persona_installer.py plan-then-apply).
"""

from __future__ import annotations

import argparse
import grp
import os
import pwd
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence

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

# Dual-identity-safe import (mirrors tools/inbox_consumer.py / tools/keep_going.py,
# T-1827 R1): `python -m unittest discover tools` (package identity tools.mailbox)
# and a bare run from inside tools/ both resolve to ONE module object.
try:
    from tools import mailbox
except ImportError:  # bare run with cwd=tools/
    import mailbox  # type: ignore

REPO_ROOT = Path(__file__).resolve().parent.parent
WAKE_UNIT_STEM = "pantheon-mailbox-wake"
MAILBOX_SUBDIRS = ("tmp", "new", "cur")
MAILBOX_DIR_MODE = 0o2770
# Code/seed default when instance.yaml carries no persona_installer.mailbox.hub_owner
# field. The LIVE value (this instance) is "the-ancestor", set explicitly in
# instance.yaml -- this default is reached only by a fresh seed with no field.
DEFAULT_HUB_OWNER = "post-office"


# --------------------------------------------------------------------------
# Host-identity seam (read-only lookups; separate from CommandRunner, which is
# for mutations). Generalizes consume_onboard.GroupMembership -- hub-setup
# needs "does this USER exist" and "does this GROUP exist" in addition to
# membership, which GroupMembership alone doesn't expose. NOT unified with
# GroupMembership itself (T-2696 drift-verification): a real 3-method vs
# 1-method Protocol difference with different Fake constructor shapes that
# both tools' test suites build directly -- forcing one Protocol would be an
# interface change, not a pure extraction. Only the shared inner algorithm
# behind user_in_group() (group_membership_lookup,
# tools/_organ_lifecycle.py) moved.
# --------------------------------------------------------------------------


class HostIdentity(Protocol):
    def user_exists(self, account: str) -> bool: ...
    def group_exists(self, group: str) -> bool: ...
    def user_in_group(self, account: str, group: str) -> tuple[bool, str]: ...


class RealHostIdentity:
    def user_exists(self, account: str) -> bool:
        try:
            pwd.getpwnam(account)
            return True
        except KeyError:
            return False

    def group_exists(self, group: str) -> bool:
        try:
            grp.getgrnam(group)
            return True
        except KeyError:
            return False

    def user_in_group(self, account: str, group: str) -> tuple[bool, str]:
        return group_membership_lookup(account, group)


class FakeHostIdentity:
    def __init__(
        self,
        users: Sequence[str] = (),
        groups: Sequence[str] = (),
        memberships: Mapping[tuple[str, str], tuple[bool, str]] | None = None,
    ) -> None:
        self.users = set(users)
        self.groups = set(groups)
        self.memberships = dict(memberships or {})

    def user_exists(self, account: str) -> bool:
        return account in self.users

    def group_exists(self, group: str) -> bool:
        return group in self.groups

    def user_in_group(self, account: str, group: str) -> tuple[bool, str]:
        return self.memberships.get((account, group), (False, f"{account} not in {group} (unscripted)"))


# --------------------------------------------------------------------------
# Manifest-derived, persona-INDEPENDENT context (hub-setup + shared by
# provision/verify/teardown for hub/group/hub_owner resolution).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MailboxContext:
    manifest: Manifest
    repo_root: Path
    mailbox_hub: Path
    mailbox_group: str
    hub_owner: str
    # T-2702: allowlist of personas whose SOLE architectural wake path is the
    # standalone pantheon-mailbox-wake@<persona>.timer. Absent/non-list field
    # in instance.yaml resolves to an EMPTY frozenset here (default-deny) --
    # see load_mailbox_context and the module's provision()/verify()/
    # teardown() gating.
    wake_timer_personas: frozenset[str]
    service_template: Path
    timer_template: Path
    revision: str


def load_mailbox_context(manifest_path: Path, repo_root: Path) -> MailboxContext:
    manifest = Manifest.load(manifest_path)
    installer = manifest.installer
    mailbox_config = installer.get("mailbox")
    if not isinstance(mailbox_config, dict):
        raise OnboardError(f"{manifest.path}: persona_installer.mailbox section missing")

    hub_value = mailbox_config.get("hub")
    if not isinstance(hub_value, str) or not Path(hub_value).is_absolute():
        raise OnboardError("persona_installer.mailbox.hub must be an absolute path")

    group = mailbox_config.get("group")
    if not isinstance(group, str) or not group:
        raise OnboardError("persona_installer.mailbox.group missing")

    hub_owner = mailbox_config.get("hub_owner")
    if not isinstance(hub_owner, str) or not hub_owner:
        hub_owner = DEFAULT_HUB_OWNER

    # T-2702: default-deny. A fresh seed with no field here (or a malformed
    # one) must enable the standalone wake timer for NOBODY -- the prior
    # unconditional-enable-for-every-persona behavior is exactly the bug this
    # ticket fixes, so silently falling back to "everyone" here would just
    # relocate it.
    wake_timer_personas_value = mailbox_config.get("wake_timer_personas")
    if wake_timer_personas_value is None:
        wake_timer_personas: frozenset[str] = frozenset()
    elif isinstance(wake_timer_personas_value, list) and all(
        isinstance(item, str) and item for item in wake_timer_personas_value
    ):
        wake_timer_personas = frozenset(wake_timer_personas_value)
    else:
        raise OnboardError("persona_installer.mailbox.wake_timer_personas must be a list of persona strings")

    # The mailbox wake-unit templates are an ORG-026-specific, fixed repo-relative
    # convention (mirroring provision_mailbox.sh's own UNIT_SRC), not yet promoted
    # to a manifest field the way the consume templates are -- no field exists to
    # read here, so this is a plain constant.
    service_template = repo_root / "tools" / "systemd" / f"{WAKE_UNIT_STEM}@.service"
    timer_template = repo_root / "tools" / "systemd" / f"{WAKE_UNIT_STEM}@.timer"

    return MailboxContext(
        manifest=manifest,
        repo_root=repo_root,
        mailbox_hub=Path(hub_value),
        mailbox_group=group,
        hub_owner=hub_owner,
        wake_timer_personas=wake_timer_personas,
        service_template=service_template,
        timer_template=timer_template,
        revision=_git_head(repo_root),
    )


def _all_persona_bindings(manifest: Manifest) -> tuple[PersonaBinding, ...]:
    """Every persona instance.yaml `roles:` declares, in manifest order.

    Mirrors `Manifest.binding()`'s own per-role iteration + validation (T-2683
    GAP 1 -- see module docstring for why this reads `Manifest.roles` directly
    rather than going through `tools/agent_role_map.py`)."""
    bindings: list[PersonaBinding] = []
    for raw in manifest.roles.values():
        if not isinstance(raw, dict):
            continue
        persona = raw.get("persona")
        if not isinstance(persona, str) or not persona:
            continue
        bindings.append(manifest.binding(persona))
    return tuple(bindings)


# --------------------------------------------------------------------------
# Filesystem helpers. _ensure_dir / _write_if_changed / _rendered_unit are
# shared (tools/_organ_lifecycle.py, T-2696). _unit_banner stays tool-local:
# its field set (organ=mailbox, no role/harness) differs from
# consume_onboard's (T-2696 drift-verification -- see that module's banner).
# --------------------------------------------------------------------------


def _unit_banner(instance_id: str, persona: str, revision: str) -> str:
    return f"# {MANAGED_MARKER}\n" f"# instance={instance_id} organ=mailbox persona={persona} revision={revision}\n"


def _best_effort_chgrp(path: Path, group: str) -> None:
    """Group ownership is left to the deployment layer's best effort (mirrors
    tools.mailbox.provision()'s own posture for mode): the caller may lack
    privilege (LookupError: unknown group on this host; PermissionError:
    caller not a member / not root), and that is expected, not an error."""
    try:
        shutil.chown(path, group=group)
    except (LookupError, PermissionError, OSError):
        pass


# --------------------------------------------------------------------------
# hub-setup() -- persona-independent.
# --------------------------------------------------------------------------


def _template_checks(ctx: MailboxContext) -> tuple[PreconditionCheck, ...]:
    checks = []
    for name, path in (("service_template", ctx.service_template), ("timer_template", ctx.timer_template)):
        ok = path.is_file()
        checks.append(PreconditionCheck(f"wake_unit.{name}", ok, f"{path} {'present' if ok else 'MISSING'}"))
    return tuple(checks)


@dataclass(frozen=True)
class HubSetupReport:
    dry_run: bool
    template_checks: tuple[PreconditionCheck, ...]
    planned: tuple[str, ...]
    changed: tuple[str, ...] = ()


def _planned_hub_setup_actions(ctx: MailboxContext) -> tuple[str, ...]:
    return (
        f"ensure group {ctx.mailbox_group!r} exists (groupadd if missing)",
        f"ensure user {ctx.hub_owner!r} exists (useradd -m -G {ctx.mailbox_group} if missing)",
        f"mkdir -p {ctx.mailbox_hub} (mode 02770, best-effort group {ctx.mailbox_group})",
        f"loginctl enable-linger {ctx.hub_owner}",
    )


def hub_setup(
    ctx: MailboxContext,
    runner: CommandRunner,
    host_identity: HostIdentity,
    execute: bool = False,
) -> HubSetupReport:
    checks = _template_checks(ctx)
    planned = _planned_hub_setup_actions(ctx)

    if not execute:
        return HubSetupReport(dry_run=True, template_checks=checks, planned=planned)

    failing = [check.detail for check in checks if not check.ok]
    if failing:
        raise OnboardError("hub-setup precondition failed: " + "; ".join(failing))

    changed: list[str] = []
    if not host_identity.group_exists(ctx.mailbox_group):
        runner.run(["groupadd", ctx.mailbox_group])
        changed.append(f"groupadd {ctx.mailbox_group}")
    if not host_identity.user_exists(ctx.hub_owner):
        runner.run(["useradd", "-m", "-G", ctx.mailbox_group, ctx.hub_owner])
        changed.append(f"useradd -m -G {ctx.mailbox_group} {ctx.hub_owner}")
    if _ensure_dir(ctx.mailbox_hub, MAILBOX_DIR_MODE):
        changed.append(f"created {ctx.mailbox_hub}")
    _best_effort_chgrp(ctx.mailbox_hub, ctx.mailbox_group)
    runner.run(["loginctl", "enable-linger", ctx.hub_owner])
    changed.append(f"enable-linger {ctx.hub_owner}")

    return HubSetupReport(dry_run=False, template_checks=checks, planned=planned, changed=tuple(changed))


# --------------------------------------------------------------------------
# provision() -- manifest-driven, dynamic-N, two independently-gated legs.
# --------------------------------------------------------------------------


def _persona_hub_dir(ctx: MailboxContext, persona: str) -> Path:
    return ctx.mailbox_hub / persona


def _persona_unit_dir(account: str, execute: bool, current_account: str | None, home: Path) -> Path:
    """`home` resolution only matters when this invocation is actually running
    AS that account (the only case any I/O happens there); otherwise this
    returns a descriptive `/home/<account>/...` path for planning/reporting
    text only. `home` is caller-supplied (CLI defaults to Path.home(), tests
    inject a tempdir) rather than read from Path.home() in here, so a test
    running "as" a fixture account never touches the real process home."""
    if execute and current_account == account:
        return home / ".config" / "systemd" / "user"
    return Path(f"/home/{account}/.config/systemd/user")


@dataclass(frozen=True)
class PersonaProvisionResult:
    persona: str
    account: str
    hub_leg: str
    unit_leg: str
    changed: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProvisionReport:
    dry_run: bool
    personas: tuple[PersonaProvisionResult, ...]


def _provision_hub_leg(
    ctx: MailboxContext, hub_dir: Path, execute: bool, current_account: str | None
) -> tuple[str, tuple[str, ...]]:
    if not execute:
        return (
            f"planned: provision {hub_dir}/{{tmp,new,cur}} (mode 02770, group {ctx.mailbox_group})",
            (),
        )
    if current_account != ctx.hub_owner:
        return f"skipped: wrong account (need {ctx.hub_owner!r})", ()
    mailbox.provision(hub_dir, mode=MAILBOX_DIR_MODE)
    for sub in MAILBOX_SUBDIRS:
        _best_effort_chgrp(hub_dir / sub, ctx.mailbox_group)
    return "done", (f"provisioned {hub_dir}",)


def _provision_unit_leg(
    ctx: MailboxContext,
    persona: str,
    account: str,
    runner: CommandRunner,
    execute: bool,
    current_account: str | None,
    home: Path,
) -> tuple[str, tuple[str, ...]]:
    # T-2702: default-deny gate, checked before the dry-run branch so both
    # planning and execution surface the skip -- a dry-run plan that shows
    # "enable --now" for a seat that will never get it is a misleading plan,
    # not a safe default.
    if persona not in ctx.wake_timer_personas:
        return (
            "skipped: not a standalone-wake seat (woken via consume runtime / N-A)",
            (),
        )

    unit_dir = _persona_unit_dir(account, execute, current_account, home)
    service_path = unit_dir / f"{WAKE_UNIT_STEM}@.service"
    timer_path = unit_dir / f"{WAKE_UNIT_STEM}@.timer"
    timer_unit = f"{WAKE_UNIT_STEM}@{persona}.timer"

    if not execute:
        return f"planned: write {service_path}, {timer_path}; enable --now {timer_unit}", ()
    if current_account != account:
        return f"skipped: wrong account (need {account!r})", ()

    banner = _unit_banner(ctx.manifest.instance_id, persona, ctx.revision)
    changed: list[str] = []
    for template, target in ((ctx.service_template, service_path), (ctx.timer_template, timer_path)):
        if _write_if_changed(target, _rendered_unit(template, banner)):
            changed.append(f"wrote {target}")
    runner.run(["systemctl", "--user", "daemon-reload"])
    changed.append("ran daemon-reload")
    runner.run(["systemctl", "--user", "enable", "--now", timer_unit])
    changed.append(f"enabled --now {timer_unit}")
    return "done", tuple(changed)


def provision(
    ctx: MailboxContext,
    personas: Sequence[PersonaBinding],
    runner: CommandRunner,
    execute: bool = False,
    current_account: str | None = None,
    home: Path | None = None,
) -> ProvisionReport:
    home = home if home is not None else Path.home()
    # Resolve real OS identity when the caller (main()'s real-CLI default) did
    # not inject one -- tests always inject an explicit account, so this path
    # is only reached by an actual `provision --execute` invocation. Without
    # this, current_account stayed None and every per-leg `current_account ==
    # expected` comparison was vacuously False, silently skipping every leg
    # (T-2683 REBOUND #1).
    current_account = current_account if current_account is not None else _current_account()
    results = []
    for binding in personas:
        hub_dir = _persona_hub_dir(ctx, binding.persona)
        hub_leg, hub_changed = _provision_hub_leg(ctx, hub_dir, execute, current_account)
        unit_leg, unit_changed = _provision_unit_leg(
            ctx, binding.persona, binding.account, runner, execute, current_account, home
        )
        results.append(
            PersonaProvisionResult(
                persona=binding.persona,
                account=binding.account,
                hub_leg=hub_leg,
                unit_leg=unit_leg,
                changed=hub_changed + unit_changed,
            )
        )
    return ProvisionReport(dry_run=not execute, personas=tuple(results))


# --------------------------------------------------------------------------
# verify() -- read-only.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyCheck:
    persona: str
    name: str
    ok: bool
    detail: str


def verify_persona(
    ctx: MailboxContext,
    binding: PersonaBinding,
    runner: CommandRunner,
    host_identity: HostIdentity,
) -> tuple[VerifyCheck, ...]:
    checks: list[VerifyCheck] = []
    hub_dir = _persona_hub_dir(ctx, binding.persona)
    for sub in MAILBOX_SUBDIRS:
        path = hub_dir / sub
        ok = path.is_dir()
        checks.append(VerifyCheck(binding.persona, f"hub.{sub}", ok, f"{path} {'present' if ok else 'MISSING'}"))

    member_ok, member_detail = host_identity.user_in_group(binding.account, ctx.mailbox_group)
    checks.append(VerifyCheck(binding.persona, "group_membership", member_ok, member_detail))

    timer_unit = f"{WAKE_UNIT_STEM}@{binding.persona}.timer"
    result = runner.run(["systemctl", "--user", "is-enabled", timer_unit])
    enabled = result.returncode == 0 and result.stdout.strip() == "enabled"
    detail = result.stdout.strip() or result.stderr.strip() or "unknown"
    # T-2702: the assertion direction flips with the allowlist -- for an
    # allowlisted (standalone-wake) persona, healthy means enabled; for
    # everyone else, healthy means NOT enabled (their wake path is the
    # consume runtime, or -- for mnemosyne -- no wake path at all). Distinct
    # check names (rather than reusing "wake_timer.enabled" with an inverted
    # `ok`) keep a FAIL on e.g. mnemosyne unambiguous: it reads as "this
    # non-wake-seat's timer is anomalously enabled", not "should be enabled
    # but isn't".
    if binding.persona in ctx.wake_timer_personas:
        checks.append(VerifyCheck(binding.persona, "wake_timer.enabled", enabled, detail))
    else:
        checks.append(VerifyCheck(binding.persona, "wake_timer.not_enabled", not enabled, detail))

    return tuple(checks)


def verify(
    ctx: MailboxContext,
    personas: Sequence[PersonaBinding],
    runner: CommandRunner,
    host_identity: HostIdentity,
) -> tuple[VerifyCheck, ...]:
    all_checks: list[VerifyCheck] = []
    for binding in personas:
        all_checks.extend(verify_persona(ctx, binding, runner, host_identity))
    return tuple(all_checks)


# --------------------------------------------------------------------------
# teardown() -- dry-run default; destructive legs each independently opt-in
# AND independently uid-gated. Deliberately never userdel's the hub-owner
# account (irreversible, no proven rollback -- stays a manual step).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TeardownReport:
    dry_run: bool
    planned: tuple[str, ...]
    changed: tuple[str, ...] = ()


def teardown(
    ctx: MailboxContext,
    binding: PersonaBinding,
    runner: CommandRunner,
    execute: bool = False,
    purge_hub: bool = False,
    remove_hub_root: bool = False,
    current_account: str | None = None,
) -> TeardownReport:
    # Resolve real OS identity when the caller did not inject one -- same fix
    # and same reasoning as provision() above (T-2683 REBOUND #1).
    current_account = current_account if current_account is not None else _current_account()
    timer_unit = f"{WAKE_UNIT_STEM}@{binding.persona}.timer"
    hub_dir = _persona_hub_dir(ctx, binding.persona)

    # T-2702: a non-allowlisted persona never had this unit installed/enabled
    # by provision(), so disabling it is a no-op at best -- but a no-op must
    # still be SURFACED, not silently skipped or silently attempted. The
    # skip line REPLACES (not appends to) the disable line for both the
    # dry-run plan and the execute report, mirroring _provision_unit_leg's
    # gating: a planned action that will never fire is misleading, not
    # informative.
    is_wake_seat = binding.persona in ctx.wake_timer_personas
    if is_wake_seat:
        planned = [f"systemctl --user disable --now {timer_unit} (as {binding.account})"]
    else:
        planned = [f"skipped: {binding.persona!r} is not a standalone-wake seat (woken via consume runtime / N-A) -- no unit disable for {timer_unit}"]
    if purge_hub:
        planned.append(f"rm -rf {hub_dir} (as {ctx.hub_owner})")
    if remove_hub_root:
        planned.append(f"rmdir {ctx.mailbox_hub} if empty (as {ctx.hub_owner})")

    if not execute:
        return TeardownReport(dry_run=True, planned=tuple(planned))

    changed: list[str] = []
    if not is_wake_seat:
        changed.append(f"skipped unit-leg: {binding.persona!r} is not a standalone-wake seat (woken via consume runtime / N-A)")
    elif current_account == binding.account:
        runner.run(["systemctl", "--user", "disable", "--now", timer_unit])
        changed.append(f"disabled --now {timer_unit}")
    else:
        changed.append(f"skipped unit-leg: wrong account (need {binding.account!r})")

    if purge_hub:
        if current_account == ctx.hub_owner:
            if hub_dir.is_dir():
                shutil.rmtree(hub_dir)
                changed.append(f"removed {hub_dir}")
        else:
            changed.append(f"skipped hub-purge: wrong account (need {ctx.hub_owner!r})")

    if remove_hub_root:
        if current_account == ctx.hub_owner:
            if ctx.mailbox_hub.is_dir() and not any(ctx.mailbox_hub.iterdir()):
                ctx.mailbox_hub.rmdir()
                changed.append(f"removed {ctx.mailbox_hub}")
        else:
            changed.append(f"skipped hub-root-removal: wrong account (need {ctx.hub_owner!r})")

    return TeardownReport(dry_run=False, planned=tuple(planned), changed=tuple(changed))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_checks(label: str, checks: Sequence[PreconditionCheck] | Sequence[VerifyCheck]) -> None:
    print(label)
    for check in checks:
        status = "PASS" if check.ok else "FAIL"
        prefix = f"{check.persona}." if hasattr(check, "persona") else ""
        print(f"  [{status}] {prefix}{check.name}: {check.detail}")


def _resolve_personas(manifest: Manifest, persona_filter: str | None) -> tuple[PersonaBinding, ...]:
    personas = _all_persona_bindings(manifest)
    if persona_filter is None:
        return personas
    filtered = tuple(b for b in personas if b.persona == persona_filter)
    if not filtered:
        raise OnboardError(f"persona {persona_filter!r} is not bound")
    return filtered


def main(
    argv: Sequence[str] | None = None,
    runner: CommandRunner | None = None,
    host_identity: HostIdentity | None = None,
    current_account: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--manifest", type=Path, default=None)
    common.add_argument("--repo-root", type=Path, default=None)

    hub_setup_parser = subparsers.add_parser("hub-setup", parents=[common])
    hub_setup_parser.add_argument("--execute", action="store_true")

    provision_parser = subparsers.add_parser("provision", parents=[common])
    provision_parser.add_argument("--persona", default=None)
    provision_parser.add_argument("--execute", action="store_true")
    provision_parser.add_argument("--home", type=Path, default=None)

    verify_parser = subparsers.add_parser("verify", parents=[common])
    verify_parser.add_argument("--persona", default=None)

    teardown_parser = subparsers.add_parser("teardown", parents=[common])
    teardown_parser.add_argument("--persona", required=True)
    teardown_parser.add_argument("--execute", action="store_true")
    teardown_parser.add_argument("--purge-hub", action="store_true")
    teardown_parser.add_argument("--remove-hub-root", action="store_true")

    args = parser.parse_args(argv)
    manifest_path = args.manifest or _default_manifest_path()
    repo_root = args.repo_root or manifest_path.parent

    try:
        ctx = load_mailbox_context(manifest_path, repo_root)
    except (ManifestError, OnboardError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    runner = runner or SubprocessCommandRunner()
    host_identity = host_identity or RealHostIdentity()

    try:
        if args.command == "hub-setup":
            report = hub_setup(ctx, runner, host_identity, execute=args.execute)
            _print_checks("template checks:", report.template_checks)
            print("plan:" if report.dry_run else "applied:")
            for item in (report.planned if report.dry_run else report.changed):
                print(f"  {item}")
            return 0

        personas = _resolve_personas(ctx.manifest, getattr(args, "persona", None))

        if args.command == "provision":
            home = args.home or Path.home()
            report = provision(
                ctx, personas, runner, execute=args.execute, current_account=current_account, home=home
            )
            for result in report.personas:
                print(f"{result.persona}: hub={result.hub_leg} unit={result.unit_leg}")
                for item in result.changed:
                    print(f"  {item}")
            return 0

        if args.command == "verify":
            checks = verify(ctx, personas, runner, host_identity)
            _print_checks("verify:", checks)
            return 0 if all(check.ok for check in checks) else 1

        if args.command == "teardown":
            report = teardown(
                ctx,
                personas[0],
                runner,
                execute=args.execute,
                purge_hub=args.purge_hub,
                remove_hub_root=args.remove_hub_root,
                current_account=current_account,
            )
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
