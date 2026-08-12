#!/usr/bin/env python3
"""T-2633: persona-generic identity resolver for tools/persona_cutover.sh and
tools/persona_rollback.sh.

Recovery's control-plane cutover (T-2590) was the first, hardened-in-place
instance: every account-specific fact (uid, home, bespoke service/session
names, expected model/channels) was hardcoded directly into
tools/t2590_recovery_cutover.sh. Specialist is the SECOND cutover -- The operator's steer
(T-2633) is that this is the moment to EXTRACT the generic mechanism, since
the underlying pieces (persona_installer / persona_bringup / t2611
persona_apply --persona / runtime_controller) are already persona-general;
only the cutover *flow* was still hardcoded to one persona.

This module answers ONE question -- "given a persona name and a target
manifest, what are the concrete identity facts tools/persona_cutover.sh needs
to safely cut that persona over?" -- by DELIBERATELY SPLITTING sourcing across
two existing, already-general readers rather than re-deriving everything from
scratch:

  - Bespoke PRE-cutover identity (account, uid, home, bespoke tmux session,
    bespoke systemd service name) comes from tools.agent_role_map
    (instance_binding() / resolve_instance_role()) plus stdlib `pwd` for
    uid/home -- the exact persona->role helper instance.yaml's own header
    points readers at. This reads THIS repo's own instance.yaml (agent_role_map
    has no manifest-path override on its public API). That is fine here: a
    persona's bespoke pre-cutover launch identity is stable infrastructure,
    not part of what a cutover verifies-as-synced.

  - POST-cutover harness_runtime shape (model, channels, tmux session,
    post_start hooks) comes from tools.persona_bringup.load_binding(), the
    ACTUAL function pantheon-persona-consume@<persona>.service invokes at
    fire time (persona_bringup.py consume --persona <persona>). Unlike
    agent_role_map, load_binding() takes an explicit manifest_path -- so
    pointing it at the TARGET persona's OWN clone (not this authoring repo's)
    reproduces exactly the "did the manifest edit actually sync to the
    account whose consume timer will read it" check t2590_recovery_cutover.sh's
    MANIFEST PRECONDITION comment describes, using real production code
    instead of a bespoke inline yaml parse.

A clean load_binding() call is NOT sufficient evidence the target is synced:
a stale/pre-edit clone (model: null, no channels/tmux/post_start) loads
without error and returns an inert PersonaBinding. resolve_persona() asserts
the loaded shape actually carries the control-plane launch values -- channels
== CONTROL_PLANE_CHANNELS, model set, tmux.session set AND matching the
persona's bespoke pre-cutover session (a real config-mismatch that would
otherwise create the WRONG pane), post_start carries both arm_pane_log.sh and
recovery_boot_kick.sh -- raising ManifestPreconditionError naming every gap if
not. (t2611_recovery_persona_apply.py is the person-general APPLY/PRECONDITION
step; this module answers what to check /before/ trusting the manifest at
all, one layer earlier.)
"""

from __future__ import annotations

import argparse
import json
import pwd
import re
import shlex
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import agent_role_map
from tools.persona_bringup import load_binding

REPO_ROOT = Path(__file__).resolve().parent.parent

# The one control-plane telegram channel value both live cutover targets
# (recovery, specialist) use today. Not derived per-persona: it is the fixed shape the
# control plane's telegram-bot launch contract expects (see instance.yaml's
# recovery/specialist stanzas), so a target whose channels differ is
# a real precondition failure, not a legitimate variant.
CONTROL_PLANE_CHANNELS = "plugin:telegram@claude-plugins-official"

_EXECSTART_RE = re.compile(r"^ExecStart\s*=\s*(.+)$", re.MULTILINE)
_TOKEN_RE = re.compile(r"""[^\s"']+""")


class ManifestPreconditionError(RuntimeError):
    """The target manifest does not carry a synced control-plane launch shape
    for the requested persona's role -- distinct from a yaml parse/schema
    error (which load_binding() itself already raises ValueError for)."""


def parse_supervise_wrapper(unit_text: str) -> str:
    """Extract the bespoke supervise-wrapper script's absolute path from a
    systemd unit file's ``ExecStart=`` line.

    Tolerates three shapes: a bare path (the REAL shape both recovery's and
    specialist's live units use, per the T-2633 cross-account sweep), a
    ``bash -c "..."``-wrapped command, and an env-var-prefixed command
    (``FOO=bar /path/to/script``). Returns the LAST whitespace-delimited
    token on the ExecStart line that is an absolute path ending in
    ``-supervise.sh``. Raises ValueError if no ExecStart line exists, or if
    none of its tokens look like a supervise-wrapper path.
    """
    match = _EXECSTART_RE.search(unit_text)
    if not match:
        raise ValueError("no ExecStart= line found in unit text")
    line = match.group(1).strip()
    tokens = _TOKEN_RE.findall(line)
    candidates = [t for t in tokens if t.startswith("/") and t.endswith("-supervise.sh")]
    if not candidates:
        raise ValueError(f"no *-supervise.sh absolute path found in ExecStart line: {line!r}")
    return candidates[-1]


@dataclass(frozen=True)
class BespokeIdentity:
    """Every identity fact derivable WITHOUT touching any instance.yaml --
    persona/role/account/uid/home plus everything computed from them
    (bespoke tmux session, bespoke service unit, its parsed supervise
    wrapper, the settings.json path, the consume-unit names, PATH for the
    apply helper).

    This is deliberately its own type, not just a subset view of
    PersonaCutoverIdentity: tools/persona_rollback.sh has ZERO manifest
    dependency by design, matching the original tools/t2590_recovery_rollback.sh
    precedent exactly (grep-verified: that script never references MANIFEST
    at all) -- rollback restores captured files + bespoke systemd state and
    must keep working even when a target's instance.yaml is missing,
    unreadable, or (mid-incident) reverted to an inert pre-cutover shape.
    Coupling rollback's resolution to the manifest-precondition gate that
    PersonaCutoverIdentity carries would make rollback strictly LESS
    available than the tool it replaces.
    """

    persona: str
    role: str
    account: str
    uid: int
    home: Path
    xdg_runtime_dir: Path
    tmux_session: str
    service: str
    service_unit_path: Path
    settings_json_path: Path
    supervise_wrapper_path: Path
    consume_timer: str
    consume_service: str
    apply_path: str
    # T-2648: space-joined `unit=session` tokens -- systemd unit(s) that
    # share *persona*'s DEFAULT tmux socket with its bespoke launch (e.g.
    # specialist's acp-bridge, T-1689) and so must be stopped before / restarted
    # after tools/persona_cutover.sh fires _start_runtime. Empty string when
    # the persona declares none (every persona but specialist today). A BESPOKE
    # fact -- true both before and after cutover, independent of which
    # supervisor drives the main TUI pane -- which is why it lives here
    # rather than on PersonaCutoverIdentity's manifest-sourced fields:
    # tools/persona_rollback.sh needs it too, and rollback has zero manifest
    # dependency by design (see this class's own docstring above). Encoding:
    # see tools.agent_role_map._encode_companion_units's docstring for why
    # `unit=session` and not JSON.
    companion_units: str

    def as_env(self) -> dict[str, str]:
        """Render as shell-consumable KEY=VALUE strings, `PCX_`-prefixed and
        uppercased, the format tools/persona_cutover.sh and
        tools/persona_rollback.sh eval directly. Mirrors the existing pattern
        in the recovery templates of capturing python3 stdout into bash vars --
        no jq dependency added. Inherited unchanged by PersonaCutoverIdentity
        below (asdict() picks up subclass fields too).

        The PCX_ prefix is load-bearing, not decorative: two field names
        (`uid`, `home`) collide with bash's OWN special variables --
        `$UID` is bash-readonly (a bare `eval UID=1002` errors), and `$HOME`
        would silently redirect every subsequent `~`-relative path in the
        calling script to the wrong account. Every emitted key is prefixed
        specifically to make that class of collision structurally
        impossible, not just avoided for the two fields known to collide
        today.

        Every VALUE is `shlex.quote()`-d (T-2648) -- every prior field
        (paths, unit/session names, the `:`-joined apply PATH) happened to be
        free of shell metacharacters, so a bare unquoted `KEY=value` line
        never broke. `companion_units` (multiple `unit=session` tokens,
        SPACE-joined) is the first field that can legitimately contain a
        space: an unquoted `PCX_COMPANION_UNITS=unitA=sessionA unitB=session
        B` is not one assignment to `eval` -- bash reads it as TWO separate
        `NAME=VALUE` assignments (`unitB` silently becomes its own shell
        variable), corrupting both. `shlex.quote()` is a no-op for every
        existing value (none contain shell-unsafe characters) and makes this
        structurally safe for any future multi-token field, not just this
        one.
        """
        out: dict[str, str] = {}
        for key, value in asdict(self).items():
            out[f"PCX_{key.upper()}"] = shlex.quote(str(value))
        return out


@dataclass(frozen=True)
class PersonaCutoverIdentity(BespokeIdentity):
    """BespokeIdentity plus the post-cutover harness_runtime shape, which
    DOES require a manifest read + the synced-shape assertion (see
    resolve_persona()). Used by tools/persona_cutover.sh only."""

    model: str
    channels: str
    manifest_path: Path
    repo_root: Path


def _default_manifest_path(home: Path) -> Path:
    """Where a persona's OWN pantheon-config clone puts instance.yaml -- the
    exact file pantheon-persona-consume@<persona>.service reads via its
    unit's Environment=PANTHEON_INSTANCE_MANIFEST=%h/pantheon-config/
    instance.yaml. Generalizes t2590_recovery_cutover.sh's hardcoded
    per-account MANIFEST default (there: literally
    "/home/claude-fallback/pantheon-config/instance.yaml"). Real fire-time
    invocations run this whole tools/ tree from within that same clone (or a
    /tmp copy of it, per the recovery template's own header), so this default is
    the common case; --manifest overrides it, matching the recovery template's
    own --manifest flag semantics."""
    return home / "pantheon-config" / "instance.yaml"


def _service_unit_text(unit_path: Path) -> str:
    """Reads the bespoke service unit at *unit_path*.

    Only readable at fire time BY the persona's own account -- both known
    unit dirs are mode 700 (verified: T-2633 authoring seat gets Permission
    denied on either). tools/persona_cutover.sh's own header instructs
    running AS the target account for exactly this reason, matching
    t2590_recovery_cutover.sh's existing execution model. See resolve_persona()'s
    `unit_text` parameter for the corresponding test seam.
    """
    return unit_path.read_text(encoding="utf-8")


def resolve_bespoke_identity(persona: str, *, unit_text: str | None = None) -> BespokeIdentity:
    """Resolve *persona*'s bespoke pre-cutover identity -- NO manifest access
    at all (see BespokeIdentity's docstring for why tools/persona_rollback.sh
    needs exactly this and nothing more).

    `unit_text`: test seam, see resolve_persona()'s matching parameter.
    """
    role = agent_role_map.resolve_instance_role(persona)
    binding = agent_role_map.instance_binding(persona)
    account = binding.get("account")
    if not account:
        raise ValueError(f"persona {persona!r} has no instance.yaml role binding (no account)")
    service = binding.get("restart_service")
    if not service:
        raise ValueError(f"persona {persona!r} binding carries no restart_service")
    restart_pane = binding.get("restart_pane") or ""
    tmux_session = restart_pane.split(":", 1)[0]
    if not tmux_session:
        raise ValueError(f"persona {persona!r} binding carries no restart_pane")

    pw = pwd.getpwnam(account)
    uid = pw.pw_uid
    home = Path(pw.pw_dir)

    service_unit_path = home / ".config" / "systemd" / "user" / service
    text = unit_text if unit_text is not None else _service_unit_text(service_unit_path)
    supervise_wrapper_path = Path(parse_supervise_wrapper(text))

    return BespokeIdentity(
        persona=persona,
        role=role,
        account=account,
        uid=uid,
        home=home,
        xdg_runtime_dir=Path(f"/run/user/{uid}"),
        tmux_session=tmux_session,
        service=service,
        service_unit_path=service_unit_path,
        settings_json_path=home / ".claude" / "settings.json",
        supervise_wrapper_path=supervise_wrapper_path,
        consume_timer=f"pantheon-persona-consume@{persona}.timer",
        consume_service=f"pantheon-persona-consume@{persona}.service",
        apply_path=f"{home}/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
        # T-2648: already encoded as `unit=session` tokens by
        # agent_role_map._load_instance_bindings; "" when the persona
        # declares none.
        companion_units=binding.get("companion_units", "") or "",
    )


def resolve_persona(
    persona: str,
    *,
    manifest_path: Path | None = None,
    repo_root: Path | None = None,
    unit_text: str | None = None,
) -> PersonaCutoverIdentity:
    """Resolve every identity fact tools/persona_cutover.sh needs for
    *persona*, verifying *manifest_path* (the TARGET persona's own
    instance.yaml clone) actually carries a synced control-plane launch
    shape for that persona's role. Built on resolve_bespoke_identity() (the
    bespoke half) plus a manifest read (the post-cutover-shape half) --
    tools/persona_rollback.sh does not need the latter and calls
    resolve_bespoke_identity() directly instead (see BespokeIdentity).

    `manifest_path`: when omitted, defaults to the persona's OWN clone (see
    `_default_manifest_path`) -- computed from the resolved account home, so
    the CLI's default behavior matches the real fire-time invocation shape
    without hardcoding a per-account path. Pass explicitly (as the
    correctness-gate tests do) to verify a specific clone, e.g. this
    authoring repo's own instance.yaml.

    `unit_text`: test seam. When None (the real invocation shape), the
    bespoke service unit is read from disk at the resolved
    `service_unit_path` -- which only succeeds when running as the target
    persona's own account. Tests inject the real, cross-account-verified
    ExecStart content directly (see tools/test_persona_cutover_resolve.py)
    rather than attempting a read this seat cannot perform.
    """
    bespoke = resolve_bespoke_identity(persona, unit_text=unit_text)
    if manifest_path is None:
        manifest_path = _default_manifest_path(bespoke.home)

    persona_binding = load_binding(manifest_path, persona)
    launch = persona_binding.launch
    controller = persona_binding.controller

    model = launch.model
    channels = launch.native.get("channels") if isinstance(launch.native, dict) else None
    post_start_argv0 = [cmd.argv[0] for cmd in controller.post_start if cmd.argv]

    problems: list[str] = []
    if channels != CONTROL_PLANE_CHANNELS:
        problems.append(f"claude.native.channels={channels!r} want {CONTROL_PLANE_CHANNELS!r}")
    if not model:
        problems.append(f"claude.model is unset ({model!r}) -- manifest not yet cut over")
    if not controller.tmux.session:
        problems.append("tmux.session is unset")
    elif controller.tmux.session != bespoke.tmux_session:
        # DELIBERATE CURRENT-DESIGN INVARIANT, not a mechanism requirement
        # (T-2633 team-lead note): today's kill-bespoke-then-create-
        # control-plane flow works fine even if the two names differ -- both
        # recovery's and specialist's stanzas simply happen to reuse the bespoke
        # session name. This is the line to relax (drop this elif, keep only
        # the "is unset" check above) the day a persona legitimately needs a
        # DIVERGENT control-plane session name; don't silently bake a
        # same-name requirement into the generic swapper without a reason to
        # (P6 -- name the invariant, don't collapse it).
        problems.append(
            f"tmux.session={controller.tmux.session!r} differs from {persona}'s bespoke "
            f"pre-cutover session {bespoke.tmux_session!r} -- both live personas today "
            "reuse the same name; if this persona intentionally diverges, relax this check"
        )
    if not any("arm_pane_log.sh" in s for s in post_start_argv0):
        problems.append("post_start missing arm_pane_log.sh")
    if not any("recovery_boot_kick.sh" in s for s in post_start_argv0):
        problems.append("post_start missing recovery_boot_kick.sh")
    if problems:
        raise ManifestPreconditionError(
            f"{manifest_path}: harness_runtime.roles.{bespoke.role} not synced for persona "
            f"{persona!r} -- " + "; ".join(problems)
        )

    return PersonaCutoverIdentity(
        **asdict(bespoke),
        model=model,
        channels=channels,
        manifest_path=manifest_path,
        repo_root=repo_root if repo_root is not None else manifest_path.resolve().parent,
    )


def _emit(identity: BespokeIdentity, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps({k: str(v) for k, v in asdict(identity).items()}))
    else:
        for key, value in identity.as_env().items():
            print(f"{key}={value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --unit-file is a real operator escape hatch, not merely a test seam:
    # it lets an operator dry-run identity resolution (incl. the
    # supervise-wrapper parse) against a DRAFT or backed-up copy of the
    # bespoke unit -- e.g. one scp'd out for inspection, or a candidate edit
    # not yet installed -- without needing to already be running as the
    # target account. Mirrors --manifest's existing override role. It also
    # happens to be what makes tools/test_persona_cutover_verify.sh able to
    # exercise persona_cutover.sh's/persona_rollback.sh's OWN arg-parsing/
    # uid-guard/dry-run logic hermetically, one layer past the resolver's own
    # test seam (`unit_text=`) -- see that test file's header.
    unit_file_help = "path to a LOCAL copy of the bespoke unit file (skips the real cross-account read)"

    resolve_parser = subparsers.add_parser(
        "resolve", help="full identity incl. post-cutover shape (persona_cutover.sh)"
    )
    resolve_parser.add_argument("--persona", required=True)
    resolve_parser.add_argument("--manifest", type=Path, default=None)
    resolve_parser.add_argument("--unit-file", type=Path, default=None, help=unit_file_help)
    resolve_parser.add_argument("--format", choices=("env", "json"), default="env")

    bespoke_parser = subparsers.add_parser(
        "bespoke-identity",
        help="bespoke identity ONLY, no manifest access (persona_rollback.sh)",
    )
    bespoke_parser.add_argument("--persona", required=True)
    bespoke_parser.add_argument("--unit-file", type=Path, default=None, help=unit_file_help)
    bespoke_parser.add_argument("--format", choices=("env", "json"), default="env")

    args = parser.parse_args(argv)

    try:
        unit_text = args.unit_file.read_text(encoding="utf-8") if args.unit_file else None
        if args.command == "resolve":
            identity: BespokeIdentity = resolve_persona(
                args.persona, manifest_path=args.manifest, unit_text=unit_text
            )
        else:
            identity = resolve_bespoke_identity(args.persona, unit_text=unit_text)
    except (ValueError, ManifestPreconditionError, OSError) as exc:
        print(f"RESOLVE-FAIL: {exc}", file=sys.stderr)
        return 1

    _emit(identity, args.format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
