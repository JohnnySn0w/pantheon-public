"""The live-install orchestrator and the supervised C5 canary entry (T-6167 PR-4). DARK.

``cli_install`` is what ``germinate apply install --seat ACCOUNT --live`` calls. Order,
every step a typed refusal on failure and nothing later runs:

1. the launcher pins (``root_launcher.require_live``), before any other work;
2. the host-substrate system-scope leg (``root_leg``), because harness-manager depends on
   host-substrate (registry harness-manager.yaml ``depends_on``);
3. the seat sub-run through the root launcher (``seat_child install``).

``root_leg`` is injected. Production wiring is ``root_live_backend_unavailable``: no live
root backend exists for the host-substrate leg yet (its handlers use the simulated port),
so a production ``--live`` stops there with ``root-live-backend-unavailable``. PR-4 proves
the ordering and the refusal mapping with fakes; it does not make the root leg live.

Known gap, not solved here (follow-up F5): the seat sub-run cannot see the root leg's
system-scope install, so a real sub-run reports ``dependency-not-installed``. That is
mapped to its own typed refusal and exit code, not hidden.

The child's output is data. The parent reads the child's last ``refused: <code>:`` line only
to classify an exit-1 refusal for the exit code; it never authorises anything from it.

Module modes (not germinate verbs): ``c5-canaries --seat X [--gates-dir DIR]`` and
``selfcheck --seat X``, the supervised real-host step's entries.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from tools.germinate.host_substrate import root_launcher as RL

#: typed code -> exit code. Distinct on purpose: the operator reads the exit code.
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_DEPENDENCY_NOT_INSTALLED = 10
EXIT_POSTBOOT_UNMET = 11
EXIT_SEAT_PIN_DISABLED = 12
EXIT_UNIT_FAILED = 20
EXIT_RUN_FAILED = 21
EXIT_LAUNCH_FAILED = 22

#: systemd unit-failure exit codes (218 CAPABILITIES, 226 NAMESPACE, 255 signal/other).
UNIT_FAILURE_CODES = frozenset({218, 226, 255})

#: A child refusal code (from its stderr) -> (typed code, exit code).
CHILD_REFUSALS: Mapping[str, tuple[str, int]] = {
    "dependency-not-installed": ("seat-dependency-not-installed", EXIT_DEPENDENCY_NOT_INSTALLED),
    "postboot-unmet": ("seat-postboot-unmet", EXIT_POSTBOOT_UNMET),
    "seat-pin-disabled": ("seat-pin-disabled", EXIT_SEAT_PIN_DISABLED),
}
_REFUSED_LINE = re.compile(r"^refused: ([a-z0-9-]+):", re.MULTILINE)


class LiveRunRefused(RuntimeError):
    """A typed refusal with the exit code the CLI returns."""

    def __init__(self, code: str, message: str, exit_code: int = EXIT_REFUSED) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.exit_code = exit_code


def root_live_backend_unavailable(account: str) -> None:
    """Production ``root_leg``: there is no live host-substrate backend yet."""

    raise LiveRunRefused(
        "root-live-backend-unavailable",
        f"no live root backend exists for the host-substrate leg of seat-{account} (a HostSubstratePort-based "
        "backend with its own D15 review is a follow-up); the seat sub-run was not started",
    )


def classify_child(returncode: int, stderr: str) -> tuple[str, int]:
    """Map the seat sub-run's exit (and, for exit 1, its typed refusal line) to (code, exit)."""

    if returncode == 0:
        return "ok", EXIT_OK
    if returncode == 1:
        found = _REFUSED_LINE.findall(stderr or "")
        if found and found[-1] in CHILD_REFUSALS:
            return CHILD_REFUSALS[found[-1]]
        return "seat-run-refused", EXIT_REFUSED
    if returncode == 2:
        return "seat-run-usage", EXIT_USAGE
    if returncode in UNIT_FAILURE_CODES:
        return "seat-run-unit-failed", EXIT_UNIT_FAILED
    return "seat-run-failed", EXIT_RUN_FAILED


SeatLaunch = Callable[[str, str], "RL.LaunchResult"]


def run_install(
    account: str,
    plan_digest: str,
    *,
    root_leg: Callable[[str], Any],
    seat_launch: SeatLaunch,
    err: Any = None,
) -> int:
    """Pins, then the root leg, then the seat sub-run, then the exit mapping."""

    err = err if err is not None else sys.stderr
    try:
        RL.require_live(RL.SEAT_RUN)
        root_leg(account)
        try:
            result = seat_launch(account, plan_digest)
        except (OSError, TimeoutError, subprocess.SubprocessError) as exc:
            raise LiveRunRefused(
                "seat-launch-failed", f"{type(exc).__name__}: {exc}", EXIT_LAUNCH_FAILED
            ) from None
    except RL.LauncherRefused as exc:
        print(f"refused: {exc.code}: {exc.message}", file=err)
        return EXIT_REFUSED
    except LiveRunRefused as exc:
        print(f"refused: {exc.code}: {exc.message}", file=err)
        return exc.exit_code
    code, exit_code = classify_child(result.returncode, result.stderr)
    if exit_code != EXIT_OK:
        print(f"refused: {code}: seat sub-run exited {result.returncode}", file=err)
        if result.stderr:
            print(result.stderr.rstrip(), file=err)
    if result.stdout:
        sys.stdout.write(result.stdout)
    return exit_code


# --- production wiring (the only place real seams are chosen) -------------------------


def production_stat(path: str) -> RL.StatInfo | None:
    import os
    import stat as st

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if st.S_ISLNK(info.st_mode):
        kind = "symlink"
    elif st.S_ISDIR(info.st_mode):
        kind = "dir"
    else:
        kind = "other"
    return RL.StatInfo(kind, info.st_uid, info.st_mode)


def production_framework_root() -> Path:
    """The rev directory the ``active`` pointer names. Only a candidate: ``check_framework_path``
    still demands the exact regex and a root-owned, non-writable chain, symlink-free."""

    import os

    return Path(os.path.realpath(RL.FRAMEWORK_BASE + "/active"))


def production_live_groups(
    *,
    getpwnam: Callable[[str], Any] | None = None,
    getgrouplist: Callable[[str, int], Sequence[int]] | None = None,
    getgrgid: Callable[[int], Any] | None = None,
) -> Callable[[str], tuple[str, frozenset[str]]]:
    """The live-group seam: (primary group name, supplementary names) as the kernel would give the unit.

    ``os.getgrouplist(account, pw_gid)`` is the initgroups set; every gid is mapped to a name with
    ``grp.getgrgid`` and a gid with no name fails closed (a group we cannot name cannot be shown
    non-root-equivalent). The primary group is returned apart from the supplementary set because
    instance.yaml declares supplementary groups only."""

    import grp
    import os
    import pwd

    pw_of = getpwnam or pwd.getpwnam
    groups_of = getgrouplist or os.getgrouplist
    name_of = getgrgid or grp.getgrgid

    def live_groups(account: str) -> tuple[str, frozenset[str]]:
        pw = pw_of(account)
        names: dict[int, str] = {}
        for gid in {*groups_of(account, pw.pw_gid), pw.pw_gid}:
            try:
                names[gid] = name_of(gid).gr_name
            except KeyError:
                raise RL.LauncherRefused(
                    "group-drift", f"{account!r}: gid {gid} has no group name; live membership cannot be verified"
                ) from None
        primary = names[pw.pw_gid]
        return primary, frozenset(n for g, n in names.items() if g != pw.pw_gid)

    return live_groups


def production_launcher(framework_root: Path) -> Callable[..., RL.LaunchSpec]:
    """A ``build_launch`` partial with the real seams, reading identities from the framework copy."""

    import os
    import pwd
    import uuid

    from tools.germinate.components import harness_manager_root as HMR

    try:
        doc = HMR.load_instance(framework_root / "instance.yaml")
        identities = HMR.identities(doc) if doc is not None else {}
    except HMR.IdentityRefused as exc:  # a ValueError; typed so the caller maps it, never an untyped crash
        raise LiveRunRefused("identity-refused", f"instance.yaml identities refused: {exc}") from None

    def build(profile: str, seat: str, mode: str, **kw: Any) -> RL.LaunchSpec:
        return RL.build_launch(
            profile,
            seat,
            scope=f"seat-{seat}",
            mode=mode,
            identities=identities,
            framework_root=str(framework_root),
            stat=production_stat,
            euid=os.geteuid,
            getpwnam=pwd.getpwnam,
            live_groups=production_live_groups(),
            nonce=uuid.uuid4().hex[:12],
            **kw,
        )

    return build


def cli_install(account: str, plan_digest: str) -> int:
    """Production entry for ``apply install --seat A --live``."""

    try:
        RL.require_live(RL.SEAT_RUN)  # before any module-level reach: nothing below runs while dark
    except RL.LauncherRefused as exc:
        print(f"refused: {exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_REFUSED

    def seat_launch(acct: str, digest: str) -> RL.LaunchResult:
        from tools._organ_lifecycle import SubprocessCommandRunner

        spec = production_launcher(production_framework_root())(RL.SEAT_RUN, acct, "install", plan_digest=digest)
        return RL.launch(spec, SubprocessCommandRunner())

    return run_install(account, plan_digest, root_leg=root_live_backend_unavailable, seat_launch=seat_launch)


# --- C5 canaries (the supervised step's entry) ---------------------------------------

CANARY_PREFIX = "CANARY_RESULT:"
#: Canary names in the ORG-080 C5 order (C5-1 .. C5-5).
CANARY_ORDER: tuple[str, ...] = RL.CANARY_MODES
_KNOWN_PLAN_IDS = frozenset({"noop"})


@dataclass(frozen=True)
class CanaryOutcome:
    name: str
    verdict: str  # green | red | skip
    detail: str


def parse_canary(name: str, result: RL.LaunchResult) -> CanaryOutcome:
    """Green only on a clean run whose single outcome line is DENIED (or, for C5-5, a plan
    whose unknown op id the stand-in executor refuses). ALLOWED, a missing marker, a start
    failure, or anything ambiguous is red."""

    if result.returncode != 0:
        return CanaryOutcome(name, "red", f"unit exited {result.returncode}: {result.stderr.strip()[:200]}")
    if name == "data-only-return":
        import json

        try:
            ops = json.loads(result.stdout)
        except ValueError:
            return CanaryOutcome(name, "red", "launcher output is not JSON")
        if not isinstance(ops, list) or not all(isinstance(o, dict) and "id" in o for o in ops):
            return CanaryOutcome(name, "red", "launcher output is not a list of {id,...}")
        accepted = [o for o in ops if o["id"] in _KNOWN_PLAN_IDS]
        refused = [o for o in ops if o["id"] not in _KNOWN_PLAN_IDS]
        if accepted and refused:
            return CanaryOutcome(name, "green", f"executor stand-in accepted {len(accepted)}, refused {len(refused)} unknown id")
        return CanaryOutcome(name, "red", "no unknown op id was refused by the stand-in executor")
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith(CANARY_PREFIX)]
    if len(lines) != 1:
        return CanaryOutcome(name, "red", f"expected exactly one {CANARY_PREFIX} line, got {len(lines)}")
    outcome = lines[0][len(CANARY_PREFIX):]
    if outcome.startswith("DENIED:"):
        return CanaryOutcome(name, "green", outcome)
    return CanaryOutcome(name, "red", outcome)


def run_c5_canaries(
    seat: str,
    *,
    build: Callable[..., RL.LaunchSpec],
    runner: Any,
    gates_dir: str | None = None,
    out: Any = None,
) -> list[CanaryOutcome]:
    """Print each argv first, then run the canaries in order. A refusal from the launcher is red."""

    out = out if out is not None else sys.stdout
    results: list[CanaryOutcome] = []
    for name in CANARY_ORDER:
        if name == "authority-holding" and gates_dir is None:
            results.append(CanaryOutcome(name, "skip", "no --gates-dir given"))
            continue
        try:
            spec = build(RL.C5_CANARY, seat, name, **({"gates_dir": gates_dir} if name == "authority-holding" else {}))
            print("ARGV " + " ".join(spec.argv), file=out)
            results.append(parse_canary(name, RL.launch(spec, runner)))
        except RL.LauncherRefused as exc:
            results.append(CanaryOutcome(name, "red", f"{exc.code}: {exc.message}"))
        except (OSError, TimeoutError, subprocess.SubprocessError) as exc:
            results.append(CanaryOutcome(name, "red", f"launch failed: {type(exc).__name__}: {exc}"))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="live_run")
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("c5-canaries")
    p.add_argument("--seat", required=True)
    p.add_argument("--gates-dir")
    p.add_argument("--framework-root", help="the rev-pinned copy (default: what 'active' points at)")
    p = sub.add_parser("selfcheck")
    p.add_argument("--seat", required=True)
    p.add_argument("--framework-root", help="the rev-pinned copy (default: what 'active' points at)")
    args = parser.parse_args(argv)
    from tools._organ_lifecycle import SubprocessCommandRunner

    try:
        RL.require_live(RL.SEAT_RUN if args.mode == "selfcheck" else RL.C5_CANARY)  # before any read
        build = production_launcher(Path(args.framework_root) if args.framework_root else production_framework_root())
        runner = SubprocessCommandRunner()
        if args.mode == "selfcheck":
            result = RL.launch(build(RL.SEAT_RUN, args.seat, "selfcheck"), runner)
            sys.stdout.write(result.stdout)
            return EXIT_OK if result.returncode == 0 else EXIT_RUN_FAILED
        outcomes = run_c5_canaries(args.seat, build=build, runner=runner, gates_dir=args.gates_dir)
    except RL.LauncherRefused as exc:
        print(f"refused: {exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_REFUSED
    except LiveRunRefused as exc:
        print(f"refused: {exc.code}: {exc.message}", file=sys.stderr)
        return exc.exit_code
    for o in outcomes:
        print(f"{o.name}: {o.verdict}: {o.detail}")
    return EXIT_OK if all(o.verdict == "green" for o in outcomes) else EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover - the supervised step's entry
    sys.exit(main())
