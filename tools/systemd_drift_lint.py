#!/usr/bin/env python3
"""systemd_drift_lint — repo unit definitions vs the copies systemd actually loads.

THE FAILURE MODE THIS EXISTS FOR (T-2179): units under `tools/systemd/` are
INSTALLED BY COPY, not symlinked. So a repo edit looks applied — the file in git
says what you meant — while the running unit is the old bytes. Nothing detected
that, and the gap is silent by construction: `git log` shows your change, the
service keeps running the previous definition, and the two never disagree
anywhere a human looks.

It bit for real: the installed `pantheon-inbox-consumer@.timer` was missing
`OnActiveSec=5min`, so a reload more than OnBootSec after manager start left
NextElapse=infinity and ticket auto-delivery silently stopped (T-1096 precedent,
T-2179 recurrence).

WHY COSMETIC DRIFT IS NOT HARMLESS, which is the actual lesson: at the time of
T-2179 ten files differed, nine only in where an `OnFailure=` line sat. Those
nine made a manual `diff` return noise for almost everything — which is exactly
why nobody spotted the tenth. Then, sweeping the "nine cosmetic" ones, ONE of
them (`pantheon-bridge.service`) turned out to carry a real `ExecStart`
difference too. The noise hid a second signal inside the ticket that named the
problem. Hence SEMANTIC comparison here rather than byte comparison: comments and
ordering are normalised away so a reported difference is always worth reading.

HOST-COUPLING, HANDLED DELIBERATELY (the T-2324/T-2322 class): the installed side
lives in a per-account directory that does not exist on a CI runner or on a seat
that never installed these units. This lint therefore SKIPS units it cannot find
installed, and says so. Be precise about what that means:

    "it degrades correctly" and "it is covered in CI" are DIFFERENT CLAIMS
    and only the first is true.

A skipped unit is unverified, not verified. `check()` returns every skip as a
Finding so callers can never mistake an all-clear for an all-checked; the CLI
summarises them by count rather than listing all 66 (most repo units belong to
other accounts and are simply absent here — enumerating them buried the one line
that mattered). Summarising is fine; DROPPING the count is not, because that is
what turns "nothing to report" into "nothing was looked at".

Exit codes: 0 = no drift among comparable units. 1 = drift found.
Report-only; authors nothing, changes no unit, never reloads systemd.

Usage:
    python3 tools/systemd_drift_lint.py
    python3 tools/systemd_drift_lint.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_UNIT_DIR = _REPO_ROOT / "tools" / "systemd"
# T-2235 (archivist 2026-09-21): her 6 hand-made instance units (exocortex.*,
# claude-telegram-exocortex, archivist-state-snapshot.*, exocortex-data-snapshot.*)
# are versioned separately at hosts/swarm/systemd/ — scoped to HER units (not org
# pantheon-*, which live at tools/systemd/), placed at a new top-level to avoid
# tools/systemd/ glob-cross-contamination. Same failure-mode this lint catches:
# a repo edit that looks applied while the installed copy on-box says something
# else. Include it in the default sweep so a archivist unit going stale surfaces
# here too. `check()` retains its single-dir semantics for CI + cross-account
# callers; only the default-sweep list is wider.
_HOST_UNIT_DIRS: tuple[Path, ...] = (
    _REPO_ROOT / "hosts" / "swarm" / "systemd",
)
_DEFAULT_UNIT_DIRS: tuple[Path, ...] = (_UNIT_DIR,) + _HOST_UNIT_DIRS

# Unit-file suffixes this lint compares. Widened past .service/.timer after a
# tracked `.path` unit turned out to be invisible to the first version (T-2179 r1).
_UNIT_SUFFIXES = frozenset({
    ".service", ".timer", ".path", ".socket", ".target", ".mount", ".slice",
})

# Suffixes that are systemd units but that this lint does not compare — listed so
# an unrecognised one is REPORTED rather than dropped.
_UNITISH_SUFFIXES = _UNIT_SUFFIXES | frozenset({".automount", ".swap", ".device", ".scope"})


def _looks_like_unit(name: str) -> bool:
    return any(name.endswith(sfx) for sfx in _UNITISH_SUFFIXES)


# Units deliberately not compared, each with the reason it is exempt. An entry
# here is a claim that drift on this unit does not matter — state why, so the
# next reader can decide whether it is still true rather than inheriting it.
KNOWN_EXEMPT: dict[str, str] = {
    "pantheon-bridge.service": (
        "decommissioned (T-0767); unit is disabled+inactive and its installed "
        "ExecStart genuinely differs. Exempt so the real signal elsewhere is not "
        "buried — NOT because the difference is harmless. Remove both copies and "
        "delete this entry when someone decides the unit is gone for good."
    ),
}


# Directives systemd does not ACT on — pure metadata. A difference confined to
# these is real drift (the installed bytes genuinely differ) but behaviourally
# inert, and must not read the same as a scheduling/exec change. This is the set
# T-2444 recon added after the recon classifier flagged 2 of specialist's 3 diffs as
# SUBSTANTIVE when they were `Documentation=` only — a doc path that moved in the
# ORG-017 knowledge/ migration. Left as substantive, every doc reshuffle produces
# a wall of false-alarm drift, which is precisely the noise T-2179 warned hides
# the real one. A directive-vs-comment split was not enough; metadata directives
# needed naming too. Comments/whitespace/ordering are already dropped upstream in
# _semantic_lines — this is the next tier: directives that survive that
# normalisation but still carry no behaviour.
_INERT_DIRECTIVES = frozenset({"Documentation", "Description"})


# Per-UNIT directives whose drift is EXPECTED intentional per-account
# customization — distinct from _INERT_DIRECTIVES (globally metadata-inert).
# Environment DOES change behaviour in general, so it is NOT globally inert;
# but for a specific unit installed per-account with a required per-account
# override, a difference confined to it is expected, not a misconfiguration.
# A difference confined to these (∪ the global inert set) classifies 'cosmetic'
# for THAT unit ONLY — a substantive directive (ExecStart, etc.) still flags.
# State the reason per entry so the next reader can re-decide it.
_UNIT_EXPECTED_DRIFT: dict[str, frozenset[str]] = {
    # T-2846: the T-2424/T-2736 memory-snapshot unit is installed per-account
    # with a distinct Environment=PANTHEON_MEMORY_DIR (each seat's real memory
    # slug; the repo template defaults to orchestrator's) and, on the HANDOFF-less
    # sub-orchestrators (mason/artisan), Environment=PANTHEON_SNAPSHOT_REQUIRE_
    # HANDOFF=0. Those overrides are REQUIRED for the guard to snapshot the
    # right dir — syncing them to the template breaks the protection (wrong dir
    # + re-enabled HANDOFF hard-fail). Verified 2026-08-11 that all 4 installs
    # (recovery/specialist/mason/artisan) differ from the template ONLY in Environment +
    # Description, with a byte-identical ExecStart. An ExecStart/logic drift on
    # this unit still classifies substantive and pages.
    "pantheon-agent-state-snapshot.service": frozenset({"Environment"}),
    # T-3097's blanket OnUnitInactiveSec/RandomizedDelaySec exemption for
    # pantheon-persona-quota@.timer was REMOVED in T-4731: check() now renders the
    # template with the installer's own substitution, so those two keys are
    # compared on their rendered values and a genuine mismatch is substantive.
}


_QUOTA_TIMER = "pantheon-persona-quota@.timer"


def _quota_values_from_manifest() -> tuple[int, int] | None:
    """(cadence_minutes, randomized_delay_seconds) exactly as the installer would
    resolve them from the repo's instance.yaml, or None if that cannot be done
    (then the template is compared raw, as before). Reuses persona_installer's own
    manifest loader and _quota_check_install_config so defaults/validation cannot
    drift from what actually gets installed (T-4731)."""
    try:
        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))
        from tools import persona_installer as pi
        manifest = pi.Manifest.load(_REPO_ROOT / "instance.yaml")
        cfg = pi._quota_check_install_config(manifest, repo_root=_REPO_ROOT)
        return cfg.cadence_minutes, cfg.randomized_delay_seconds
    except Exception:  # noqa: BLE001 - fail open to raw compare, never to a false clean
        return None


def _render_repo_unit(name: str, text: str,
                      quota_values: tuple[int, int] | None) -> str:
    """Render install-time placeholders in a repo template the way the installer
    does, so the diff is rendered-vs-installed rather than template-vs-installed.
    Only the quota timer is a template today."""
    if name != _QUOTA_TIMER or "{quota_" not in text:
        return text
    values = quota_values if quota_values is not None else _quota_values_from_manifest()
    if values is None:
        return text
    from tools import persona_installer as pi
    return pi._render_quota_timer_template(
        text, cadence_minutes=values[0], randomized_delay_seconds=values[1])


@dataclass
class Finding:
    unit: str
    kind: str  # "drift" | "skipped"
    detail: str
    # Only meaningful for kind == "drift": "substantive" (a directive systemd acts
    # on differs) or "cosmetic" (the difference is confined to _INERT_DIRECTIVES).
    # Empty for skips. The cross-account sweep (T-2444) alerts on substantive drift
    # and merely reports cosmetic drift, so a doc-path reshuffle does not page.
    severity: str = ""

    def to_dict(self) -> dict:
        return {"unit": self.unit, "kind": self.kind, "detail": self.detail,
                "severity": self.severity}


def installed_dir() -> Path:
    """Where systemd --user reads unit files for the current account."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "systemd" / "user"


def _semantic_lines(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Directives grouped BY SECTION, sorted within each section.

    Comments, blank lines and intra-section ordering are dropped: those are how
    the T-2179 noise was generated and they carry no behaviour.

    SECTION BOUNDARIES ARE PRESERVED, and that correction is load-bearing. The
    first version sorted every line into one flat pool, which made a directive
    RELOCATED ACROSS SECTIONS invisible — and relocation is not cosmetic:
    `Restart=always` under `[Service]` is a restart policy, and the same line
    under `[Unit]` is ignored by systemd entirely. A reviewer demonstrated that
    exact case returning zero findings. The original docstring claimed this was
    intended and "reports only what systemd would act on differently"; that was
    simply wrong, and a lint whose comment asserts a property it does not have is
    the defect it exists to catch, one level up.
    """
    sections: list[tuple[str, list[str]]] = []
    current = "<preamble>"
    buf: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            sections.append((current, buf))
            current, buf = line, []
            continue
        buf.append(line)
    sections.append((current, buf))
    return [(name, tuple(sorted(lines))) for name, lines in sections if lines or name != "<preamble>"]


def _by_section(pairs) -> dict[str, set[str]]:
    """Collapse section-scoped directive pairs into {section: {directives}}.

    ACCUMULATE per section rather than overwrite. systemd allows a section header
    to appear more than once and merges the occurrences; a dict comprehension
    would keep only the last, so a real difference in an earlier occurrence would
    be DETECTED (check() compares the full list) but omitted from the text a human
    reads to decide what to do. A drift report that under-states the difference is
    the same misleading-artifact class this lint exists for. No file in
    tools/systemd/ repeats a header today; this costs one line and removes the
    path entirely. (r1 reviewer find.) Hoisted to module scope for _classify_drift
    (T-2444), which needs the same section-merged view.
    """
    out: dict[str, set[str]] = {}
    for sec, lines in pairs:
        out.setdefault(sec, set()).update(lines)
    return out


def _classify_drift(a, b, unit: str = "") -> str:
    """'cosmetic' if every DIRECTIVE that differs is behaviourally inert (or an
    expected per-account override for `unit` — T-2846), else 'substantive'.
    Operates on the section-merged view so a directive whose only change is which
    section it sits in still counts by its key (relocation is not inert — the r1
    Restart-across-sections case is substantive).

    The differing directives are the symmetric difference of each section's
    directive set: a value change puts BOTH the old and new line into the diff,
    and both reduce to the same key, so a changed `Documentation=` value is one
    inert key, not two. Empty diff cannot occur here (check() only calls this when
    a != b), but an all-inert diff is the cosmetic tier T-2444 exists to separate.

    `unit` scopes _UNIT_EXPECTED_DRIFT: Environment drift on the memory-snapshot
    unit is expected (per-account memory-dir override) and classifies cosmetic,
    but Environment drift on any OTHER unit stays substantive.
    """
    da, db = _by_section(a), _by_section(b)
    changed_keys: set[str] = set()
    for sec in set(da) | set(db):
        for line in da.get(sec, set()) ^ db.get(sec, set()):
            changed_keys.add(line.split("=", 1)[0].strip())
    allowed = _INERT_DIRECTIVES | _UNIT_EXPECTED_DRIFT.get(unit, frozenset())
    if changed_keys and changed_keys <= allowed:
        return "cosmetic"
    return "substantive"


def _describe(a, b) -> str:
    """Human-readable diff of two section-scoped directive maps.

    Reports the SECTION alongside each directive, because after the r1 fix a
    directive can differ by where it sits and not only by whether it is present.
    """
    da = _by_section(a)
    db = _by_section(b)
    bits: list[str] = []
    for sec in sorted(set(da) | set(db)):
        only_repo = sorted(da.get(sec, set()) - db.get(sec, set()))
        only_inst = sorted(db.get(sec, set()) - da.get(sec, set()))
        if only_repo:
            bits.append(f"{sec} repo-only: " + "; ".join(only_repo[:3]))
        if only_inst:
            bits.append(f"{sec} installed-only: " + "; ".join(only_inst[:3]))
    return " | ".join(bits) or "section structure differs"


class InstalledSource:
    """Read-only view of the units systemd actually loaded, for ONE account.

    The seam that lets check() compare against another account's installed units
    (the T-2444 cross-account sweep) without check() ever learning whether the
    bytes came from this filesystem or from `sudo -u <acct> cat`. The default
    implementation is _LocalInstalled (a directory on this fs — every pre-T-2444
    caller, unchanged); the sweep injects a remote source that reads via recovery's
    NOPASSWD sudo and lives in systemd_drift_sweep.py so this module keeps no
    subprocess/sudo surface and stays hermetically testable.

    THREE outcomes must stay distinct, because collapsing any two of them is how a
    seat that could not be read gets reported as a clean seat (the T-2232 trap):
      - reachable is False  -> the source itself could not be read at all
                               (denied sudo, dead manager, timeout). UNVERIFIED.
      - has(name) is False  -> the source was read; this unit is not installed.
      - read(name) is None  -> the source was read and lists this unit, but this
                               one file could not be read. UNVERIFIED for it alone.
    """

    reachable: bool = True
    reason: str = ""
    label: str = "<installed>"

    def has(self, name: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def read(self, name: str) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError


class _LocalInstalled(InstalledSource):
    """The pre-T-2444 behaviour: a directory read directly off this filesystem.

    Always `reachable` — a directory that does not exist simply lists nothing,
    which check() reports per-unit as `not installed`, exactly as before.
    """

    reachable = True

    def __init__(self, dest: Path):
        self.dest = dest

    @property
    def label(self) -> str:  # type: ignore[override]
        return str(self.dest)

    def has(self, name: str) -> bool:
        return (self.dest / name).exists()

    def read(self, name: str) -> str | None:
        try:
            return (self.dest / name).read_text(encoding="utf-8")
        except OSError:
            return None


def _resolve_source(dest: Path | None, installed: InstalledSource | None) -> InstalledSource:
    """An explicit source wins; otherwise wrap `dest` (or this account's own
    installed dir) in a local source. Keeps `check(repo, inst)` — the existing
    positional-Path call — working unchanged."""
    if installed is not None:
        return installed
    return _LocalInstalled(dest if dest is not None else installed_dir())


def check(unit_dir: Path = _UNIT_DIR, dest: Path | None = None,
          installed: InstalledSource | None = None,
          quota_values: tuple[int, int] | None = None) -> list[Finding]:
    """Compare every repo unit against its installed copy.

    Returns findings for BOTH drift and skips. A caller that reports only the
    drift half is making the all-clear/all-checked conflation this module's
    docstring warns about.

    `installed` injects an alternate source (the T-2444 cross-account sweep passes
    a sudo-backed remote source); when omitted, `dest` (or this account's own
    installed dir) is read locally, unchanged from before.
    """
    src_installed = _resolve_source(dest, installed)
    findings: list[Finding] = []
    if not src_installed.reachable:
        # The whole source is unaskable. ONE finding for the account, and NEVER an
        # empty (== clean-looking) list — an unread seat reported as clean is the
        # exact staleness this sweep exists to catch.
        findings.append(Finding(
            "*", "skipped",
            f"could not read {src_installed.label} — UNVERIFIED, NOT verified clean"
            + (f": {src_installed.reason}" if src_installed.reason else ""),
        ))
        return findings
    for src in sorted(unit_dir.glob("*")):
        if not src.is_file():
            continue
        name = src.name
        if src.suffix not in _UNIT_SUFFIXES:
            # NOT a silent `continue`. The first version skipped anything that was
            # not .service/.timer before creating a Finding, so a tracked
            # `.path` unit — pantheon-mailbox-wake@.path, same family as the timer
            # that actually broke — was absent from drift, absent from skips, and
            # absent from --json. Outside the data model is worse than skipped,
            # and it contradicted this module's own invariant that an all-clear
            # can never be mistaken for an all-checked.
            if _looks_like_unit(name):
                findings.append(Finding(
                    name, "skipped",
                    f"unit suffix '{src.suffix}' not compared by this lint — "
                    f"unverified, NOT verified clean",
                ))
            continue
        if name in KNOWN_EXEMPT:
            findings.append(Finding(name, "skipped", f"exempt: {KNOWN_EXEMPT[name]}"))
            continue
        if not src_installed.has(name):
            findings.append(Finding(
                name, "skipped",
                f"not installed at {src_installed.label} — unverified on this host, "
                f"NOT verified clean",
            ))
            continue
        inst_text = src_installed.read(name)
        try:
            repo_text = src.read_text(encoding="utf-8")
        except OSError as exc:
            findings.append(Finding(name, "skipped", f"repo unit unreadable: {exc}"))
            continue
        if inst_text is None:
            # Listed as installed but this one file could not be read. Unverified
            # for it alone — distinct from `not installed`, and never silent.
            findings.append(Finding(
                name, "skipped",
                f"installed at {src_installed.label} but unreadable — UNVERIFIED, "
                f"NOT verified clean",
            ))
            continue
        a = _semantic_lines(_render_repo_unit(name, repo_text, quota_values))
        b = _semantic_lines(inst_text)
        if a != b:
            findings.append(Finding(name, "drift", _describe(a, b),
                                    severity=_classify_drift(a, b, name)))
    return findings


def _comparable_count(unit_dir: Path = _UNIT_DIR, dest: Path | None = None,
                      installed: InstalledSource | None = None) -> int:
    """How many units were actually compared — the denominator behind an all-clear.

    An unreachable source compared NOTHING; it must return 0 so the sweep reads it
    as UNVERIFIED rather than as a green seat with no drift.
    """
    src_installed = _resolve_source(dest, installed)
    if not src_installed.reachable:
        return 0
    n = 0
    for src in sorted(unit_dir.glob("*")):
        if not src.is_file() or src.suffix not in _UNIT_SUFFIXES:
            continue
        if src.name in KNOWN_EXEMPT:
            continue
        if src_installed.has(src.name):
            n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    # T-2235: sweep the default org dir (tools/systemd/) AND any registered
    # host-specific dirs (hosts/<host>/systemd/, archivist's instance units).
    # check() keeps its single-dir contract for CI + cross-account callers;
    # aggregation happens here so the default CLI covers the full committed set.
    findings: list[Finding] = []
    compared = 0
    for unit_dir in _DEFAULT_UNIT_DIRS:
        if not unit_dir.is_dir():
            # A missing host-scoped dir on a checkout that doesn't have it yet is
            # a legitimate skip (e.g. before archivist's PR lands); NOT a silent
            # nothing — surface it so an all-clear can never be mistaken for an
            # all-checked (the same discipline this module's docstring names).
            findings.append(Finding(
                f"[{unit_dir.relative_to(_REPO_ROOT)}]*", "skipped",
                f"unit dir not present at {unit_dir} — no units to compare from this scope",
            ))
            continue
        findings.extend(check(unit_dir=unit_dir))
        compared += _comparable_count(unit_dir=unit_dir)
    drift = [f for f in findings if f.kind == "drift"]
    skipped = [f for f in findings if f.kind == "skipped"]
    # "checked 35, clean" and "checked 1, clean" must not read the same (r1).

    if args.json:
        print(json.dumps({"drift": [f.to_dict() for f in drift],
                          "skipped": [f.to_dict() for f in skipped]}, indent=2))
        return 1 if drift else 0

    if drift:
        print(f"systemd drift — installed copy differs from the repo ({len(drift)}):")
        for f in drift:
            tag = "" if f.severity == "substantive" else f" [{f.severity or 'drift'}]"
            print(f"  {f.unit}{tag}\n      {f.detail}")
        cosmetic = [f for f in drift if f.severity == "cosmetic"]
        if cosmetic:
            print(f"\n  ({len(cosmetic)} of these are cosmetic — an inert directive "
                  f"({'/'.join(sorted(_INERT_DIRECTIVES))}) only, or an expected "
                  f"per-account override (T-2846); behaviour is intended.)")
        print("\n  A repo edit does NOT reach a running unit: these are copies, not")
        print("  symlinks. Sync deliberately, then `systemctl --user daemon-reload`.")
    else:
        print(f"systemd drift: none among {compared} compared unit(s).")

    # Skips are reported ALWAYS, including on a clean run — an all-clear that hides
    # how much it did not look at is the defect this lint exists to catch. But they
    # are SUMMARISED, not enumerated:
    #
    # The first version of this printed all 66 skip lines. Most repo units belong to
    # OTHER accounts (recovery's, specialist's, the suborch seats') and are simply absent here,
    # so the full list buried the one line that mattered under expected absences —
    # recreating, inside the lint written to prevent it, the exact noise-hides-signal
    # failure of T-2179. Counts preserve the honesty; `--json` gives the full list to
    # anyone who wants it.
    if skipped:
        exempt = [f for f in skipped if f.detail.startswith("exempt:")]
        absent = [f for f in skipped if not f.detail.startswith("exempt:")]
        print(f"\n  not compared: {len(skipped)} — UNVERIFIED, not verified clean.")
        if absent:
            print(f"    {len(absent)} not installed on this account (expected: most units "
                  f"belong to other seats). Full list: --json")
        for f in exempt:
            print(f"    EXEMPT {f.unit}: {f.detail[len('exempt: '):].splitlines()[0][:120]}")

    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
