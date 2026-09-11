#!/usr/bin/env python3
"""T-2887: scripted, default-safe decommission of the watch/research pipeline
-- the ORG-068 "Remove" leg for the system Specialist stewards.

ORG-068 (universal system-lifecycle contract, ratified 2026-08-12) requires
every major system to carry a *proven* teardown/decommission with rollback, not
just an install. The watch/research pipeline's coverage-matrix row read
`Install ✓ | Configure ✓ | Health ◐ | Update ◐ | Remove ✗ -- no scripted
teardown` (gap #1, highest priority). This tool is that leg; ORG-067's
`mailbox_onboard.py teardown` is the cited worked example whose shape this
mirrors.

What "the watch pipeline" is, on the `claude-research` (Specialist) account -- the
install this inverts (see `tools/systemd/README.md`):

  * pantheon-watch.{service,timer}          -- watch_runner.py --file-watch-ticket (daily 08:00 UTC)
  * pantheon-knowledge-sweep.{service,timer}-- knowledge_sweep.py --file-sweep-ticket (daily 13:00 UTC)
  * pantheon-watch-digest.{service,timer}   -- watch_digest.py (daily 18:00 UTC)

  state:   ~/.local/state/pantheon/watch/state.json
           ~/.local/state/pantheon/knowledge_sweep/state.json
  reports: knowledge/watch/reports/<date>/  (committed to the repo -- git IS the archive)

The units are per-account and NOT version-controlled (only the `tools/systemd/`
TEMPLATES are); so decommission is a systemd + filesystem operation on this
account, never a repo edit. The reports are already committed, so the "archive /
hand off in-flight reports" ORG-068 clause is satisfied by git history; the only
loss-risk is a report generated but not yet committed/pushed, which the
`--archive-reports` leg surfaces rather than silently dropping.

Design (mirrors mailbox_onboard.teardown + the fleet-daemon-bringdown SOP):

  * DRY-RUN BY DEFAULT. Nothing mutates without `--execute`. A plain invocation
    prints the plan and exits 0.
  * Each destructive leg is INDEPENDENTLY OPT-IN. The default `--execute` does
    only the reversible core (disable --now the timers + reset-failed): a PAUSE
    that leaves every unit file, all state, and all reports in place, restorable
    with one `enable --now` per unit. The irreversible-r legs escalate from there:
      --remove-units   also `rm` the installed unit files (PAUSE -> full REMOVE;
                       the DISABLED-vs-MISSING line the delivery-reachability
                       monitor draws). Rollback: reinstall from tools/systemd/.
      --purge-state    also remove the state dirs (archived first). Rollback:
                       state rebuilds itself on the next fire, or restore the
                       archive.
      --archive-reports surface any in-flight (uncommitted) reports so they are
                       handed off, never silently lost.
  * ORDER IS LOAD-BEARING (fleet-daemon-bringdown SOP §3): record the manifest
    first (§3.0), stop TIMERS before anything else (§3.1 -- an armed timer fires
    a oneshot into a half-torn-down seat), let in-flight oneshots drain (§3.2),
    `disable --now` not `stop` (§3.4 -- else it returns on the next daemon-reload
    and the decommission silently un-does itself), `reset-failed` afterward
    (§3.5). Here all three wake units ARE `.timer`s whose services are `static`,
    so `disable --now <timer>` both disarms and stops the pair.
  * A NO-OP IS SURFACED, never silently skipped (T-2702): a leg that finds
    nothing to do says so in both the plan and the report.
  * DELIBERATELY NEVER does an irreversible-without-rollback step: it does not
    `git rm` committed reports, does not touch the repo's tracked tree, and does
    not remove the account. Those stay manual, matching the teardown precedent's
    refusal to `userdel` the hub owner.

Testability: all host mutation goes through the shared ORG-049
`_organ_lifecycle.CommandRunner` seam, so the test suite drives the whole
lifecycle against a `FakeCommandRunner` + a tmp fixture home and never touches
the live timers, state, or reports.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

# Make `from tools._organ_lifecycle import ...` work both as a script
# (`python3 tools/watch_pipeline_decommission.py`, how an operator invokes a
# teardown) and as a package import (`python3 -m unittest
# tools.test_watch_pipeline_decommission`). When invoked as a script, sys.path
# prepends `tools/` not the repo root, so the `tools` package isn't reachable;
# this injects the repo root in that case. In package mode it is a no-op.
# Mirrors the bootstrap in tools/watch_runner.py.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._organ_lifecycle import (  # noqa: E402  -- after sys.path bootstrap.
    CommandRunner,
    SubprocessCommandRunner,
    _current_account,
)

# The account this pipeline lives on. Decommission legs that mutate systemd or
# the account's state only fire when the invoking account matches -- same uid
# gate as the teardown precedent, not an internal `sudo -u` wrap.
PIPELINE_ACCOUNT = "claude-research"

# The wake units, in decommission order. Each entry is the unit STEM; the
# `.timer` is what carries the schedule (Install=WantedBy=timers.target) and the
# `.service` is `static` (pulled by its timer). Ordered watch -> sweep -> digest,
# matching the install order in tools/systemd/README.md.
WATCH_UNIT_STEMS: tuple[str, ...] = (
    "pantheon-watch",
    "pantheon-knowledge-sweep",
    "pantheon-watch-digest",
)

# State dirs the pipeline writes (relative to $HOME). Removed only under
# --purge-state, archived first.
STATE_DIRS: tuple[str, ...] = (
    ".local/state/pantheon/watch",
    ".local/state/pantheon/knowledge_sweep",
)

# The committed reports tree (relative to the repo root). Git IS its archive;
# this tool never deletes it. Used only by --archive-reports to find in-flight
# (uncommitted) reports.
REPORTS_SUBDIR = "knowledge/watch/reports"


@dataclass(frozen=True)
class DecommissionReport:
    """The outcome of a decommission run. `planned` is the ordered human-readable
    plan (always populated -- it is what a dry-run prints); `changed` records
    what actually happened under --execute (empty on a dry-run). `manifest`
    captures the pre-teardown state so a restore is possible (fleet-daemon SOP
    §3.0 -- restore is only as good as this list)."""

    dry_run: bool
    planned: tuple[str, ...]
    changed: tuple[str, ...] = ()
    manifest: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _timer(stem: str) -> str:
    return f"{stem}.timer"


def _record_manifest(
    runner: CommandRunner, home: Path
) -> tuple[str, ...]:
    """Enumerate, per unit, its current enabled/active state -- the restore list
    (fleet-daemon-bringdown SOP §3.0). Read-only; safe to run in dry-run."""

    lines: list[str] = [f"# watch-pipeline decommission manifest -- {datetime.now(timezone.utc).isoformat()}"]
    for stem in WATCH_UNIT_STEMS:
        timer = _timer(stem)
        enabled = runner.run(["systemctl", "--user", "is-enabled", timer])
        active = runner.run(["systemctl", "--user", "is-active", timer])
        lines.append(
            f"{timer}: enabled={enabled.stdout.strip() or '?'} active={active.stdout.strip() or '?'} "
            f"-- restore: systemctl --user enable --now {timer}"
        )
    for rel in STATE_DIRS:
        path = home / rel
        lines.append(f"state: {path} exists={path.is_dir()}")
    return tuple(lines)


def _installed_unit_files(home: Path) -> tuple[Path, ...]:
    """The per-account .timer + .service files for the pipeline that actually
    exist on disk, in decommission order."""

    unit_dir = home / ".config" / "systemd" / "user"
    found: list[Path] = []
    for stem in WATCH_UNIT_STEMS:
        for suffix in (".timer", ".service"):
            candidate = unit_dir / f"{stem}{suffix}"
            if candidate.is_file():
                found.append(candidate)
    return tuple(found)


def _uncommitted_reports(repo_root: Path) -> tuple[str, ...]:
    """In-flight reports = report files under REPORTS_SUBDIR that git does not
    yet have committed (untracked or modified). Returns their repo-relative
    paths. Empty tuple means every report is safely in git history."""

    result = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain", "--", REPORTS_SUBDIR],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return (f"(could not query git status: {result.stderr.strip()})",)
    dirty: list[str] = []
    for line in result.stdout.splitlines():
        # porcelain: 2 status chars, a space, then the path.
        path = line[3:].strip()
        if path:
            dirty.append(path)
    return tuple(dirty)


def decommission(
    runner: CommandRunner,
    *,
    home: Path,
    repo_root: Path,
    execute: bool = False,
    remove_units: bool = False,
    purge_state: bool = False,
    archive_reports: bool = False,
    current_account: str | None = None,
) -> DecommissionReport:
    """Plan (dry-run default) or perform the watch-pipeline decommission.

    The default leg (always on) is the reversible PAUSE: disable --now every
    timer + reset-failed. --remove-units / --purge-state / --archive-reports each
    escalate independently. All host mutation goes through `runner`; nothing
    fires unless `execute` is True AND the invoking account is PIPELINE_ACCOUNT.
    """

    current_account = current_account if current_account is not None else _current_account()
    manifest = _record_manifest(runner, home)
    planned: list[str] = []
    warnings: list[str] = []

    # --- Leg 0 (always): disable --now the timers, in order, then reset-failed.
    for stem in WATCH_UNIT_STEMS:
        planned.append(f"systemctl --user disable --now {_timer(stem)} (as {PIPELINE_ACCOUNT})")
    planned.append(
        "systemctl --user reset-failed " + " ".join(_timer(s) for s in WATCH_UNIT_STEMS)
        + " (clear any drained-oneshot residue)"
    )

    # --- Leg: --archive-reports -- surface in-flight (uncommitted) reports.
    if archive_reports:
        dirty = _uncommitted_reports(repo_root)
        if dirty:
            planned.append(
                f"HAND-OFF: {len(dirty)} uncommitted report path(s) under {REPORTS_SUBDIR} "
                "-- commit/push or copy these out before removing the pipeline: "
                + ", ".join(dirty)
            )
        else:
            planned.append(
                f"reports: no in-flight (uncommitted) files under {REPORTS_SUBDIR} "
                "-- all reports are in git history (the archive); nothing to hand off"
            )

    # --- Leg: --remove-units -- PAUSE -> full REMOVE (rm the installed files).
    unit_files = _installed_unit_files(home)
    if remove_units:
        if unit_files:
            for path in unit_files:
                planned.append(f"rm {path} (rollback: reinstall from tools/systemd/)")
            planned.append("systemctl --user daemon-reload (after unit removal)")
        else:
            planned.append(
                "remove-units: no installed pantheon-watch* unit files found "
                f"under {home}/.config/systemd/user -- nothing to remove"
            )

    # --- Leg: --purge-state -- archive then remove the state dirs.
    state_paths = [home / rel for rel in STATE_DIRS]
    if purge_state:
        present = [p for p in state_paths if p.is_dir()]
        if present:
            for path in present:
                planned.append(f"archive + rm {path} (rollback: state rebuilds on next fire, or restore archive)")
        else:
            planned.append("purge-state: no state dirs present -- nothing to purge")

    if not execute:
        return DecommissionReport(
            dry_run=True,
            planned=tuple(planned),
            manifest=manifest,
            warnings=tuple(warnings),
        )

    # ---- execute path ----
    if current_account != PIPELINE_ACCOUNT:
        warnings.append(
            f"wrong account: this pipeline lives on {PIPELINE_ACCOUNT!r} but the invoking "
            f"account is {current_account!r} -- refusing to mutate systemd/state for another account"
        )
        return DecommissionReport(
            dry_run=False,
            planned=tuple(planned),
            changed=(),
            manifest=manifest,
            warnings=tuple(warnings),
        )

    changed: list[str] = []

    # Leg 0: disable --now the timers (§3.1 timers first, §3.4 disable not stop).
    for stem in WATCH_UNIT_STEMS:
        timer = _timer(stem)
        res = runner.run(["systemctl", "--user", "disable", "--now", timer])
        if res.returncode == 0:
            changed.append(f"disabled --now {timer}")
        else:
            warnings.append(f"disable --now {timer} exited {res.returncode}: {res.stderr.strip()}")
    # §3.5 reset-failed to clear any residue from a drained mid-run oneshot.
    reset = runner.run(
        ["systemctl", "--user", "reset-failed", *(_timer(s) for s in WATCH_UNIT_STEMS)]
    )
    if reset.returncode == 0:
        changed.append("reset-failed on the three timers")
    else:
        # reset-failed of an already-clean unit is harmless; surface, don't fail.
        warnings.append(f"reset-failed exited {reset.returncode}: {reset.stderr.strip()}")

    if archive_reports:
        dirty = _uncommitted_reports(repo_root)
        if dirty:
            warnings.append(
                f"{len(dirty)} in-flight (uncommitted) report path(s) remain under {REPORTS_SUBDIR} "
                "-- NOT auto-committed (repo tree is off-limits to this tool); hand off manually: "
                + ", ".join(dirty)
            )
        else:
            changed.append(f"reports: no in-flight files under {REPORTS_SUBDIR} (all in git history)")

    if remove_units:
        if unit_files:
            for path in unit_files:
                path.unlink()
                changed.append(f"removed {path}")
            reload_res = runner.run(["systemctl", "--user", "daemon-reload"])
            if reload_res.returncode == 0:
                changed.append("daemon-reload after unit removal")
            else:
                warnings.append(f"daemon-reload exited {reload_res.returncode}: {reload_res.stderr.strip()}")
        else:
            changed.append("remove-units: no installed unit files found -- nothing removed")

    if purge_state:
        archive_root = home / ".local" / "state" / "pantheon" / f"watch-decommission-archive-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        present = [p for p in state_paths if p.is_dir()]
        if present:
            archive_root.mkdir(parents=True, exist_ok=True)
            for path in present:
                dest = archive_root / path.name
                shutil.copytree(path, dest)
                shutil.rmtree(path)
                changed.append(f"archived {path} -> {dest} and removed original")
        else:
            changed.append("purge-state: no state dirs present -- nothing purged")

    return DecommissionReport(
        dry_run=False,
        planned=tuple(planned),
        changed=tuple(changed),
        manifest=manifest,
        warnings=tuple(warnings),
    )


def _print_report(report: DecommissionReport) -> None:
    print("manifest (restore list):")
    for line in report.manifest:
        print(f"  {line}")
    print("plan:" if report.dry_run else "applied:")
    for item in report.planned if report.dry_run else report.changed:
        print(f"  {item}")
    if report.warnings:
        print("warnings:")
        for item in report.warnings:
            print(f"  ! {item}")


def main(
    argv: Sequence[str] | None = None,
    runner: CommandRunner | None = None,
    current_account: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the decommission (default is a dry-run plan only)",
    )
    parser.add_argument(
        "--remove-units",
        action="store_true",
        help="also rm the installed unit files (PAUSE -> full REMOVE); rollback: reinstall from tools/systemd/",
    )
    parser.add_argument(
        "--purge-state",
        action="store_true",
        help="also archive+remove the pipeline state dirs",
    )
    parser.add_argument(
        "--archive-reports",
        action="store_true",
        help="surface any in-flight (uncommitted) reports for hand-off",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="override $HOME (test hook; defaults to the invoking user's home)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="override the pantheon-config repo root (defaults to <home>/pantheon-config)",
    )
    args = parser.parse_args(argv)

    home = args.home or Path.home()
    repo_root = args.repo_root or (home / "pantheon-config")
    runner = runner or SubprocessCommandRunner()

    report = decommission(
        runner,
        home=home,
        repo_root=repo_root,
        execute=args.execute,
        remove_units=args.remove_units,
        purge_state=args.purge_state,
        archive_reports=args.archive_reports,
        current_account=current_account,
    )
    _print_report(report)
    # Non-zero only if an execute run hit a warning that means the decommission
    # did not fully land (wrong account, a failed disable). A dry-run is always 0.
    if not report.dry_run and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
