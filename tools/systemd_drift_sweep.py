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

EXPECTED UNITS (T-4232). Drift only covers units that ARE installed; a unit that vanished
from its seat, or was disabled there, compared as nothing and read clean. The declarative
tools/systemd/expected-units.yaml (persona -> [{unit, enabled}]) names what each seat must
carry, and the sweep raises MISSING_EXPECTED (alerts) for an expected unit that is ABSENT
(no installed file; for `foo@x.timer` the template `foo@.timer` counts) or DISABLED
(`enabled: true` but the name is no entry in any `*.wants/` dir). The same single read
that fetches unit files also lists the `*.wants/` entry names as distinct `\0W\0` records
(names only, nothing is cat'ed), so there is no second cross-account read. "Enabled" is
the name appearing in ANY wants dir — the [Install] WantedBy= target is not resolved.
The manifest is FAIL-CLOSED: missing, unparseable or invalid (unknown persona, unit with
no repo template) yields one MANIFEST_UNKNOWN alert row, never "nothing expected".
Detect + alert only: nothing is installed, enabled or re-copied.

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
MISSING_EXPECTED = "MISSING_EXPECTED"  # read; a manifest-expected unit is absent/disabled — alert
MANIFEST_UNKNOWN = "MANIFEST_UNKNOWN"  # expected-units manifest unreadable/invalid — alert
ALERTING = frozenset({DRIFT, UNREACHABLE, MISSING_EXPECTED, MANIFEST_UNKNOWN})

EXPECTED_UNITS_PATH = sdl._UNIT_DIR / "expected-units.yaml"
# The account column of the synthetic row a bad manifest produces (it is fleet-wide).
MANIFEST_ROW_ACCOUNT = "expected-units.yaml"

# The delimiter framing one installed unit in the single-sudo read below. NUL cannot
# appear in a systemd unit file (text), so it cannot collide with unit content.
_REC = "\0F\0"
# A `*.wants/` entry-name record: `\0W\0<wantsdir>/<name>\0`. Every W record is emitted
# AFTER every F record, so splitting W off first leaves the F stream byte-identical to the
# pre-T-4232 shape; W records can never be read as (or appended to) a unit file.
_WREC = "\0W\0"
_NODIR = "__NODIR__"

# T-5464: this timer belongs to Claude's in-process Telegram receiver.  The
# runtime-controller deliberately disables it while Recovery runs Codex, after saving the
# prior unit state in an incomplete receiver-topology snapshot.  It must become expected
# again as soon as either half of that proof disappears (Claude selected, snapshot
# complete/malformed, or the snapshot does not say the timer was enabled).
_RECOVERY_CLAUDE_WATCHDOG = "recovery-telegram-child-watchdog.timer"


class RuntimeExpectations:
    """Read-only, fail-closed runtime context for conditional unit expectations."""

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home is not None else Path.home()
        self._recovery_watchdog_dormant: bool | None = None

    @staticmethod
    def _json(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def recovery_watchdog_dormant(self) -> bool:
        if self._recovery_watchdog_dormant is not None:
            return self._recovery_watchdog_dormant
        active = self._json(
            self.home / ".local/state/pantheon/personas/recovery/active-runtime.json"
        )
        snapshot = self._json(
            self.home / ".config/codex-telegram/receipts/receiver-topology-snapshot.json"
        )
        timer = (snapshot.get("units") or {}).get(_RECOVERY_CLAUDE_WATCHDOG, {})
        self._recovery_watchdog_dormant = bool(
            str(active.get("runtime", "")).startswith("codex/")
            and snapshot.get("schema") == "pantheon-receiver-topology-snapshot/v1"
            and snapshot.get("persona") == "recovery"
            and snapshot.get("account") == "claude-fallback"
            and snapshot.get("complete") is False
            and isinstance(timer, dict)
            and timer.get("enabled") == "enabled"
        )
        return self._recovery_watchdog_dormant

    def allows_disabled(self, persona: str | None, unit: str) -> bool:
        return (persona == "recovery" and unit == _RECOVERY_CLAUDE_WATCHDOG
                and self.recovery_watchdog_dormant())


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


def _split_stream(out: str) -> tuple[str, list[str]]:
    """Split the reader's output into (F-record stream, [wants entry paths])."""
    head, *wparts = out.split(_WREC)
    wants = [w.rstrip("\0") for w in wparts]
    return head, [w for w in wants if w]


def _parse_installed_stream(out: str) -> dict[str, str]:
    """Parse the NUL-framed `<name>\\0<content>` records the reader script emits.

    W (wants) records are stripped first, so unit files only ever come from F records."""
    head, _ = _split_stream(out)
    files: dict[str, str] = {}
    for part in head.split(_REC)[1:]:
        name, _, content = part.partition("\0")
        if name:
            files[name] = content
    return files


def _parse_wants_stream(out: str) -> set[str]:
    """The set of unit NAMES enabled via any `*.wants/` dir (`timers.target.wants/x.timer`
    -> `x.timer`). Which target wants it is deliberately not resolved (see docstring)."""
    _, wants = _split_stream(out)
    return {w.rsplit("/", 1)[-1] for w in wants if w.rsplit("/", 1)[-1]}


class Ops:
    """The side-effect seam. READ-ONLY — reads another account's installed unit dir via
    recovery's NOPASSWD sudo, and NEVER writes, enables, reloads or syncs anything."""

    def read_installed_dir(self, account: str) -> tuple[str, dict[str, str] | None]:
        """Return (reason, files) — the pre-T-4232 shape, kept for existing callers
        (scheduler_reconcile). Same single read as read_installed_state."""
        reason, files, _ = self.read_installed_state(account)
        return reason, files

    def read_installed_state(
        self, account: str,
    ) -> tuple[str, dict[str, str] | None, set[str] | None]:
        """Return (reason, files, wants) for `account`'s ~/.config/systemd/user in one call.

        wants is the set of unit names present as entries in any `*.wants/` dir (the
        enablement signal), None exactly when files is None.

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
            # rc latches ANY failed `cat`, not just the loop's last iteration: the loop's
            # own status is its last command's, and a trailing non-file entry (a *.wants
            # dir sorts last) would `continue` with 0 and mask an earlier unreadable unit.
            # A failed cat anywhere -> rc!=0 -> UNVERIFIED (files None), never a partial
            # "clean" read.
            'rc=0; for f in "$d"/*; do [ -f "$f" ] || continue; '
            'printf "\\0F\\0%s\\0" "$(basename "$f")"; cat "$f" || rc=$?; done; '
            # T-4232: enablement = an entry (usually a symlink; -L catches a dangling one)
            # in a *.wants/ dir. Names only, never content. Emitted after every F record.
            'for w in "$d"/*.wants/*; do { [ -L "$w" ] || [ -e "$w" ]; } || continue; '
            'printf "\\0W\\0%s/%s\\0" "$(basename "$(dirname "$w")")" "$(basename "$w")"; '
            'done; exit $rc'
        )
        if account == _me():
            argv = ["bash", "-c", script]
        else:
            argv = ["sudo", "-n", "-u", account, "bash", "-c", script]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return (f"probe error: {type(exc).__name__}", None, None)
        if r.returncode != 0:
            # ANY non-zero rc = could-not-ask. The empty stdout of a denied sudo is
            # byte-identical to a clean empty read; the rc is the ONLY thing that
            # separates them, so it is the only thing we trust here (the T-2232 trap).
            return (f"query failed rc={r.returncode}: {(r.stderr or '').strip()[:160]}", None, None)
        if r.stdout.strip() == _NODIR:
            return ("no ~/.config/systemd/user on this account", {}, set())
        return ("", _parse_installed_stream(r.stdout), _parse_wants_stream(r.stdout))


def _read_state(ops, account: str):
    """(reason, files, wants) from `ops`, tolerating a pre-T-4232 seam.

    A seam that overrides only read_installed_dir (a test fake or a subclass) must not
    fall through to the inherited real read_installed_state — that would shell out to a
    real sudo behind the fake's back. Such a seam yields wants=None ("enablement not
    readable"), which the expected-units check treats as NOT enabled, i.e. it alerts."""
    own_state = getattr(type(ops), "read_installed_state", None)
    own_dir = getattr(type(ops), "read_installed_dir", None)
    legacy = own_state is None or (own_dir is not Ops.read_installed_dir
                                   and own_state is Ops.read_installed_state)
    if legacy:
        reason, files = ops.read_installed_dir(account)
        return reason, files, None
    return ops.read_installed_state(account)


class _RemoteInstalled(sdl.InstalledSource):
    """An sdl.InstalledSource backed by one cross-account sudo read (via Ops).

    The read happens once, at construction; reachable/has/read all serve the cached
    result. `reachable is False` propagates the could-not-ask signal into check(), which
    turns it into a single non-empty UNVERIFIED finding rather than a clean empty list.
    """

    def __init__(self, account: str, ops: Ops):
        self.account = account
        self.reason, self._files, self.wants = _read_state(ops, account)
        self.label = f"{account}:~/.config/systemd/user"

    @property
    def reachable(self) -> bool:  # type: ignore[override]
        return self._files is not None

    def has(self, name: str) -> bool:
        return name in (self._files or {})

    def read(self, name: str) -> str | None:
        return (self._files or {}).get(name)


class ManifestError(Exception):
    """expected-units.yaml is missing, unparseable or invalid (fail-closed, D-3)."""


_MANIFEST_KEYS = frozenset({"unit", "enabled"})


def _template_name(unit: str) -> str | None:
    """`foo@bar.timer` -> `foo@.timer`; None for a non-instance name."""
    stem, dot, suffix = unit.rpartition(".")
    if "@" not in stem:
        return None
    prefix, _, instance = stem.partition("@")
    return f"{prefix}@.{suffix}" if instance else None


def load_expected_units(path: Path = EXPECTED_UNITS_PATH,
                        unit_dir: Path = sdl._UNIT_DIR) -> dict[str, list[dict]]:
    """Load + validate the expected-units manifest -> {persona: [{unit, enabled}]}.

    Raises ManifestError on ANY defect — never returns a partial or empty "nothing
    expected" map for a broken file (T-4232 D-3). Each persona must have an instance.yaml
    binding with an account (via agent_role_map); each unit must have a repo template in
    unit_dir (an instance `foo@x.timer` is satisfied by `foo@.timer`); a bare template
    cannot be `enabled: true` (templates are enabled per instance)."""
    try:
        import yaml
        text = Path(path).read_text(encoding="utf-8")
    except Exception as exc:
        raise ManifestError(f"cannot read {path}: {exc!r}") from exc
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        raise ManifestError(f"unparseable YAML in {path}: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise ManifestError(f"{path}: top level must be a non-empty persona mapping")

    errors: list[str] = []
    out: dict[str, list[dict]] = {}
    for persona, entries in data.items():
        if not isinstance(persona, str) or not arm.instance_binding(persona).get("account"):
            errors.append(f"unknown persona {persona!r} (no instance.yaml account binding)")
            continue
        account = arm.instance_binding(persona)["account"]
        if account not in arm.fleet_accounts():
            # The sweep iterates fleet_accounts(); an expectation on an account outside
            # it would never be checked — silent, so fail closed instead (D-3).
            errors.append(f"persona {persona!r} account {account!r} is not in the swept "
                          "fleet (agent_role_map.fleet_accounts)")
            continue
        if not isinstance(entries, list) or not entries:
            errors.append(f"{persona}: must be a non-empty list of {{unit, enabled}}")
            continue
        seen: set[str] = set()
        clean: list[dict] = []
        for e in entries:
            if not isinstance(e, dict) or set(e) != _MANIFEST_KEYS:
                errors.append(f"{persona}: entry {e!r} must have exactly keys unit, enabled")
                continue
            unit, enabled = e["unit"], e["enabled"]
            if not isinstance(unit, str) or not isinstance(enabled, bool):
                errors.append(f"{persona}: entry {e!r} needs unit: str, enabled: bool")
                continue
            if unit in seen:
                errors.append(f"{persona}: duplicate unit {unit}")
                continue
            seen.add(unit)
            tmpl = _template_name(unit)
            if not (Path(unit_dir) / unit).is_file() and not (
                    tmpl and (Path(unit_dir) / tmpl).is_file()):
                errors.append(f"{persona}: unit {unit} has no template in {unit_dir}")
                continue
            if enabled and unit.rpartition(".")[0].endswith("@"):
                errors.append(f"{persona}: bare template {unit} cannot be enabled: true")
                continue
            clean.append({"unit": unit, "enabled": enabled})
        out[persona] = clean
    if errors:
        raise ManifestError(f"{path}: " + "; ".join(errors))
    return out


def _missing_expected(expected: list[dict], files: dict[str, str] | None,
                      wants: set[str] | None, *, persona: str | None = None,
                      runtime_expectations: RuntimeExpectations | None = None) -> list[dict]:
    """[{unit, detail}] for each expected unit that is absent or not enabled."""
    files = files or {}
    missing: list[dict] = []
    for e in expected:
        unit = e["unit"]
        tmpl = _template_name(unit)
        if unit not in files and not (tmpl and tmpl in files):
            missing.append({"unit": unit, "detail": "absent: no installed unit file"
                            + (f" (nor template {tmpl})" if tmpl else "")})
        elif e["enabled"] and wants is None:
            missing.append({"unit": unit,
                            "detail": "enablement not readable — NOT verified enabled"})
        elif (e["enabled"] and unit not in wants
              and not (runtime_expectations
                       and runtime_expectations.allows_disabled(persona, unit))):
            missing.append({"unit": unit, "detail": "disabled: not in any *.wants/ dir"})
    return missing


def sweep_account(account: str, ops: Ops | None = None,
                  unit_dir: Path = sdl._UNIT_DIR,
                  expected: dict[str, list[dict]] | None = None,
                  runtime_expectations: RuntimeExpectations | None = None) -> dict:
    """Sweep one account and return its per-account verdict.

    {account, persona, status, compared, substantive, cosmetic, unverified,
    missing_expected, reason} — where substantive/cosmetic/unverified/missing_expected
    are lists of {unit, detail} and status is one of CLEAN / DRIFT / UNREACHABLE /
    NO_UNITS / MISSING_EXPECTED. `expected` is the loaded manifest ({persona: [...]});
    None means no expected-units check for this call (sweep_fleet/main load it).

    Precedence: UNREACHABLE > MISSING_EXPECTED > NO_UNITS/DRIFT/CLEAN. A MISSING_EXPECTED
    account may ALSO carry substantive drift; the alert text names both.
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

    missing = []
    if src.reachable and expected and persona in expected:
        missing = _missing_expected(
            expected[persona], src._files, src.wants, persona=persona,
            runtime_expectations=runtime_expectations,
        )

    if not src.reachable:
        status = UNREACHABLE
    elif missing:
        status = MISSING_EXPECTED
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
        "missing_expected": missing,
        "reason": src.reason,
    }


def _manifest_row(reason: str) -> dict:
    """The single fleet-wide alert row for a missing/invalid manifest (D-3)."""
    return {"account": MANIFEST_ROW_ACCOUNT, "persona": None, "status": MANIFEST_UNKNOWN,
            "compared": 0, "substantive": [], "cosmetic": [], "unverified": [],
            "missing_expected": [], "reason": reason}


def sweep_fleet(accounts=None, ops: Ops | None = None,
                unit_dir: Path = sdl._UNIT_DIR,
                manifest_path: Path | None = EXPECTED_UNITS_PATH,
                runtime_expectations: RuntimeExpectations | None = None) -> list[dict]:
    """Sweep every account; also load the expected-units manifest (fail-closed).

    manifest_path=None skips the expected-units check entirely (drift only). A manifest
    that fails to load appends one MANIFEST_UNKNOWN row (alerting) — the per-account
    drift verdicts are still computed, just without the expected-units half."""
    ops = ops or Ops()
    accounts = list(accounts) if accounts is not None else list(arm.fleet_accounts())
    expected = None
    manifest_rows: list[dict] = []
    if manifest_path is not None:
        try:
            expected = load_expected_units(manifest_path, unit_dir=unit_dir)
        except ManifestError as exc:
            manifest_rows.append(_manifest_row(str(exc)))
    runtime_expectations = runtime_expectations or RuntimeExpectations()
    results = [sweep_account(a, ops, unit_dir=unit_dir, expected=expected,
                             runtime_expectations=runtime_expectations)
               for a in accounts]
    return results + manifest_rows


def _alert_text(results: list[dict]) -> str:
    lines = []
    for r in results:
        who = f"{r['account']} ({r['persona'] or '?'})"
        if r["status"] == UNREACHABLE:
            lines.append(f"  UNREACHABLE {who}: {r['reason']}")
        elif r["status"] == MANIFEST_UNKNOWN:
            lines.append(f"  MANIFEST_UNKNOWN {r['account']}: {r['reason']} — "
                         "expected-units check did NOT run")
        if r.get("missing_expected"):
            units = ", ".join(f"{m['unit']} [{m['detail'].split(':')[0]}]"
                              for m in r["missing_expected"])
            lines.append(f"  MISSING_EXPECTED {who}: "
                         f"{len(r['missing_expected'])} — {units}")
        if r["status"] in (DRIFT, MISSING_EXPECTED) and r["substantive"]:
            units = ", ".join(d["unit"] for d in r["substantive"])
            lines.append(f"  DRIFT {who}: "
                         f"{len(r['substantive'])} substantive — {units}")
    body = "\n".join(lines)
    n_accounts = sum(1 for r in results if r["status"] != MANIFEST_UNKNOWN)
    manifest_note = (" + expected-units manifest invalid"
                     if any(r["status"] == MANIFEST_UNKNOWN for r in results) else "")
    return (f"[systemd-drift-sweep] {n_accounts} account(s){manifest_note} need a look "
            f"(report-only — decide, do not auto-sync or auto-install):\n{body}")


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
            elif r["status"] in (UNREACHABLE, MANIFEST_UNKNOWN):
                extra = f" — {r['reason']}"
            elif r["status"] == MISSING_EXPECTED:
                extra = " — " + ", ".join(m["unit"] for m in r["missing_expected"])
            _log(f"{r['status']:<12} {r['account']:<16} "
                 f"compared={r['compared']:<3}{extra}")

    if alerting and not args.dry_run:
        _emit_alert(alerting)

    # Exit 0 regardless — the alert IS the output. A non-zero exit fires OnFailure and
    # reports the monitor instead of the drift.
    return 0


if __name__ == "__main__":
    sys.exit(main())
