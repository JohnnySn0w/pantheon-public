#!/usr/bin/env python3
"""tools/systemd_drift_sweep.py — cross-account systemd unit-drift, from ONE seat (T-2444).

WHY THIS EXISTS, and why the per-seat lint could not close it. `systemd_drift_lint`
compares a seat's installed units against the repo, and a live-tree guard runs it on
every seat that runs the suite. But a seat's drift is only visible when THAT seat runs
the suite, and the seats least likely to run it — paused, low-activity, non-dev, or
simply not today's agent — are exactly the seats most likely to be stale. Coverage is
proportional to activity; staleness is inversely proportional to it. Recovery's own units
sat 7 weeks stale (T-1096) and were found only when someone ran the full suite for an
unrelated reason; specialist is paused and would not have run it at all.

So the missing piece is not "run it on a schedule" — it is "run it ACROSS ACCOUNTS from
one seat", and recovery is the only seat that can: it holds NOPASSWD sudo to every pantheon
account. This module reuses systemd_drift_lint.check() wholesale — a single comparison
path, not a second copy — by injecting a sudo-backed InstalledSource per account.

REPORT, NEVER SYNC. Copying a repo unit over a live seat's installed unit is a mutation
of somebody else's substrate, and the repo is canonical for CONTENT but is not
automatically CORRECT: the T-2444 recon found ONE unit (pantheon-watch-digest on specialist)
whose INSTALLED copy was right and whose REPO copy was stale — an auto-syncing sweep
would have pointed specialist's watch digest at a path inside orchestrator's 700-mode home while
reporting that it was fixing drift. The sweep names drift; a human decides.

THREE TRAPS, each already paid for once — honoured here, not rediscovered:
  (a) `sudo -n -u <self>` FROM that same account is DENIED, and the denial leaves stdout
      EMPTY — byte-identical to a successful read that found nothing. So we skip sudo for
      our OWN account, and treat ANY non-zero rc as UNVERIFIED, never as "no drift".
  (b) "no tracked units installed" is UNVERIFIED, never CLEAN — else a fleet of empty
      seats reads as a green fleet. Reachable-but-zero-compared is its own status.
  (c) a seat's units are cp's, not symlinks, so a repo edit is inert until re-copied.
      The sweep diffs CONTENT (that inert-copy IS the whole failure mode), which
      check()'s semantic comparison already does — we feed it the installed bytes.

SEVERITY. check() now tags each drift substantive vs cosmetic (a difference confined to
inert metadata directives — Documentation=/Description=). The sweep ALERTS on substantive
drift and on unreachable seats; it merely REPORTS cosmetic drift and no-tracked-unit
seats, so a doc-path reshuffle on a paused peer does not page and does not get tuned out.

Exit 0 regardless — the ALERT is the output. A non-zero exit would fire the unit's
OnFailure and report the monitor instead of the drift (the same-second-timeout trap).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import systemd_drift_lint as sdl  # noqa: E402
import agent_role_map as arm  # noqa: E402

# Route to RECOVERY's TRIAGE lane, not direct-to-operator. route_failure_alert() sends
# recovery-OWNED failures straight to the operator (Recovery cannot self-remediate) and everything
# else to Recovery's pane for triage (the operator gets an audit-only ping, D1). A cross-account
# drift report is the SECOND kind: it is an observation about OTHER seats' substrate that
# Recovery decides on (re-copy vs retask ticket), NOT a recovery-self failure — and pre-existing
# drift must never page the operator on a daily timer. Any non-"recovery" agent selects the triage
# lane; "orchestrator" names the fleet HO as the nominal owner of a fleet-wide config finding.
# The agent value only picks the lane — _send_alert_via_recovery delivers to Recovery regardless.
ROUTE_AGENT = "orchestrator"
PROBE_TIMEOUT = 45  # 8 accounts, one sudo read each; generous but bounded (see the unit).

# Per-account status vocabulary. The split between the two UNVERIFIED kinds is the
# finding — one is "I could not look" (alert), the other "I looked, found no tracked
# units" (report). Collapsing them is trap (a)+(b) merged.
CLEAN = "CLEAN"            # read, every tracked unit matches (cosmetic-only allowed)
DRIFT = "DRIFT"           # read, at least one SUBSTANTIVE difference
UNREACHABLE = "UNREACHABLE"  # could not read the account at all — alert
NO_UNITS = "NO_UNITS"     # read, but zero tracked units installed — report, do not alert
ALERTING = frozenset({DRIFT, UNREACHABLE})

# The delimiter framing one installed unit in the single-sudo read below. NUL cannot
# appear in a systemd unit file (text), so it cannot collide with unit content.
_REC = "\0F\0"
_NODIR = "__NODIR__"


def _me() -> str:
    """This process's own account. sudo-to-self is a grant nobody made (trap a), so the
    reader must recognise its own seat and skip the wrapper."""
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return os.environ.get("USER", "")


def _log(msg: str) -> None:
    print(f"[systemd-drift-sweep] {msg}", file=sys.stderr)


def _parse_installed_stream(out: str) -> dict[str, str]:
    """Parse the NUL-framed `<name>\\0<content>` records the reader script emits."""
    files: dict[str, str] = {}
    for part in out.split(_REC)[1:]:
        name, _, content = part.partition("\0")
        if name:
            files[name] = content
    return files


class Ops:
    """The side-effect seam. READ-ONLY — reads another account's installed unit dir via
    recovery's NOPASSWD sudo, and NEVER writes, enables, reloads or syncs anything."""

    def read_installed_dir(self, account: str) -> tuple[str, dict[str, str] | None]:
        """Return (reason, files) for `account`'s ~/.config/systemd/user in one call.

        files is a {unit_name: content} dict, or None when the directory could not be
        READ — a denied sudo, a dead manager, a timeout. None is the UNVERIFIED signal
        the caller must never flatten into a clean empty seat (trap a). An empty dict
        means asked-and-found-nothing (NODIR or a genuinely empty dir), distinguishable
        by files is not None.
        """
        # One sudo round-trip: list + cat every regular file, NUL-framed so unit content
        # (newlines, '=', '#') survives intact. NODIR is emitted (not an error) so a seat
        # with no user-unit dir reads as "asked, nothing" rather than "could not ask".
        script = (
            'd="$HOME/.config/systemd/user"; '
            f'[ -d "$d" ] || {{ printf "%s" "{_NODIR}"; exit 0; }}; '
            'for f in "$d"/*; do [ -f "$f" ] || continue; '
            'printf "\\0F\\0%s\\0" "$(basename "$f")"; cat "$f"; done'
        )
        if account == _me():
            argv = ["bash", "-c", script]
        else:
            argv = ["sudo", "-n", "-u", account, "bash", "-c", script]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return (f"probe error: {type(exc).__name__}", None)
        if r.returncode != 0:
            # ANY non-zero rc = could-not-ask. The empty stdout of a denied sudo is
            # byte-identical to a clean empty read; the rc is the ONLY thing that
            # separates them, so it is the only thing we trust here (the T-2232 trap).
            return (f"query failed rc={r.returncode}: {(r.stderr or '').strip()[:160]}", None)
        if r.stdout.strip() == _NODIR:
            return ("no ~/.config/systemd/user on this account", {})
        return ("", _parse_installed_stream(r.stdout))


class _RemoteInstalled(sdl.InstalledSource):
    """An sdl.InstalledSource backed by one cross-account sudo read (via Ops).

    The read happens once, at construction; reachable/has/read all serve the cached
    result. `reachable is False` propagates the could-not-ask signal into check(), which
    turns it into a single non-empty UNVERIFIED finding rather than a clean empty list.
    """

    def __init__(self, account: str, ops: Ops):
        self.account = account
        self.reason, self._files = ops.read_installed_dir(account)
        self.label = f"{account}:~/.config/systemd/user"

    @property
    def reachable(self) -> bool:  # type: ignore[override]
        return self._files is not None

    def has(self, name: str) -> bool:
        return name in (self._files or {})

    def read(self, name: str) -> str | None:
        return (self._files or {}).get(name)


def sweep_account(account: str, ops: Ops | None = None,
                  unit_dir: Path = sdl._UNIT_DIR) -> dict:
    """Sweep one account and return its per-account verdict.

    {account, persona, status, compared, substantive, cosmetic, unverified, reason}
    — where substantive/cosmetic/unverified are lists of {unit, detail} and status is
    one of CLEAN / DRIFT / UNREACHABLE / NO_UNITS.
    """
    ops = ops or Ops()
    persona = arm.account_persona(account)
    src = _RemoteInstalled(account, ops)
    findings = sdl.check(unit_dir=unit_dir, installed=src)
    compared = sdl._comparable_count(unit_dir=unit_dir, installed=src)

    drift = [f for f in findings if f.kind == "drift"]
    substantive = [f for f in drift if f.severity != "cosmetic"]
    cosmetic = [f for f in drift if f.severity == "cosmetic"]
    # Skips that are genuine unknowns (unreadable / unaskable), NOT the expected
    # "belongs to another seat" absences — those are not a per-account concern here.
    unverified = [f for f in findings if f.kind == "skipped"
                  and "NOT verified clean" in f.detail
                  and not f.detail.startswith("not installed at")]

    if not src.reachable:
        status = UNREACHABLE
    elif compared == 0:
        status = NO_UNITS
    elif substantive:
        status = DRIFT
    else:
        status = CLEAN

    return {
        "account": account,
        "persona": persona,
        "status": status,
        "compared": compared,
        "substantive": [{"unit": f.unit, "detail": f.detail} for f in substantive],
        "cosmetic": [{"unit": f.unit, "detail": f.detail} for f in cosmetic],
        "unverified": [{"unit": f.unit, "detail": f.detail} for f in unverified],
        "reason": src.reason,
    }


def sweep_fleet(accounts=None, ops: Ops | None = None,
                unit_dir: Path = sdl._UNIT_DIR) -> list[dict]:
    ops = ops or Ops()
    accounts = list(accounts) if accounts is not None else list(arm.fleet_accounts())
    return [sweep_account(a, ops, unit_dir=unit_dir) for a in accounts]


def _alert_text(results: list[dict]) -> str:
    lines = []
    for r in results:
        if r["status"] == DRIFT:
            units = ", ".join(d["unit"] for d in r["substantive"])
            lines.append(f"  DRIFT {r['account']} ({r['persona'] or '?'}): "
                         f"{len(r['substantive'])} substantive — {units}")
        elif r["status"] == UNREACHABLE:
            lines.append(f"  UNREACHABLE {r['account']} ({r['persona'] or '?'}): {r['reason']}")
    body = "\n".join(lines)
    return (f"[systemd-drift-sweep] {len(lines)} account(s) need a look "
            f"(report-only — decide, do not auto-sync):\n{body}")


def _emit_alert(results: list[dict]) -> None:
    """Route the alert through the standard fleet seam (route_failure_alert), same as
    delivery_reachability. Failure to deliver the alert must not crash the sweep — the
    verdicts are already computed and printed."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from inbox_consumer import route_failure_alert
        route_failure_alert(ROUTE_AGENT, _alert_text(results), vocab="systemd-drift-sweep")
    except Exception as exc:  # pragma: no cover - alert seam
        _log(f"alert delivery FAILED ({exc!r}); verdicts still returned")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--dry-run", action="store_true", help="report; never alert")
    ap.add_argument("--account", action="append",
                    help="limit to these accounts (repeatable; default: the whole fleet)")
    args = ap.parse_args(argv)

    results = sweep_fleet(args.account)
    alerting = [r for r in results if r["status"] in ALERTING]

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            extra = ""
            if r["status"] == CLEAN and r["cosmetic"]:
                extra = f" (+{len(r['cosmetic'])} cosmetic)"
            elif r["status"] == DRIFT:
                extra = f" — {len(r['substantive'])} substantive"
            elif r["status"] == UNREACHABLE:
                extra = f" — {r['reason']}"
            _log(f"{r['status']:<12} {r['account']:<16} "
                 f"compared={r['compared']:<3}{extra}")

    if alerting and not args.dry_run:
        _emit_alert(alerting)

    # Exit 0 regardless — the alert IS the output. A non-zero exit fires OnFailure and
    # reports the monitor instead of the drift.
    return 0


if __name__ == "__main__":
    sys.exit(main())
