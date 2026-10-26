"""tools/parity_render.py — ORG-099 seat-parity manifest renderer (T-3747 BT2).

Reads sop/standard/seat-parity/{axes.yaml,carve-outs.yaml} + instance.yaml and
emits (a) per-seat check scripts, (b) a fleet audit table. This is the
mechanical realization of ORG-099 §5's evergreen mechanism: axes are DECLARED
in a manifest, not accreted as ad-hoc scripts.

WHAT THIS TOOL DOES NOT DO: run the actual fleet audit for real, against the
live fleet, as part of landing this ticket (T-3747 BT4's job, run by recovery, who
holds the cross-account sudo grant these checks need). `--dry-run` is
side-effect-free by construction (zero subprocess calls); without it, this
tool DOES attempt real (read-only, `sudo -n` non-interactive) checks — which
is safe to invoke, but running it from an account without cross-account
grants (e.g. orchestrator) will correctly report most cross-account axes
UNMEASURABLE via sudo-denial, never a false PASS.

ALARM-BIASED BY DESIGN (ORG-099 §8): a check that cannot cleanly distinguish
"the axis is absent" from "I could not ask" reports UNMEASURABLE, never PASS.
See classify_outcome() for the exact per-check_method contract. A significant
consequence, stated plainly: most of the CURRENT manifest's systemd_query and
canary axes will render UNMEASURABLE when actually executed, because their
check_refs are prose/output-shape-dependent (e.g. `systemctl --user
list-timers ...`, whose "0 timers listed." footer prints on a clean rc=0 exit
regardless of whether the timer exists) rather than boolean-normalized
one-liners (e.g. `systemctl --user is-enabled ...; echo $?`). That is the
intended, honest outcome of this tool meeting the current manifest — not a
bug to route around with cleverer per-axis output-shape parsing. A follow-up
axes.yaml pass tightening check_refs to boolean-normalized commands would
sharpen this; that is manifest-authoring work, out of this tool's scope.

RENDERABLE vs NOT: a check_ref is only ever attempted as a literal shell
command if it doesn't match one of a small set of prose markers directly
observed in the current manifest (see _PROSE_MARKERS, each cited to the axis
that motivated it). This is deliberately narrow and evidence-based, not
general NLP — it exists to give a MORE SPECIFIC evidence string ("not a
literal command") than a generic execution failure would.

NO eval()/exec() OF MANIFEST CONTENT, anywhere. check_refs are only ever
(a) placeholder-substituted and run via subprocess, or (b) matched against a
small, named set of regex shapes for carve-out verifier assertions
(instance.yaml path lookup / ticket-state lookup / systemd-absence query) and
evaluated by hand in Python. Manifests are orchestrator-owned config, not code, and
treating them as code would let a manifest typo run arbitrary shell via
string-formatting tricks nobody intended.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml  # noqa: F401  (kept: a missing PyYAML still fails at import, as before T-4298)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AXES_PATH = REPO_ROOT / "sop" / "standard" / "seat-parity" / "axes.yaml"
DEFAULT_CARVE_OUTS_PATH = REPO_ROOT / "sop" / "standard" / "seat-parity" / "carve-outs.yaml"
DEFAULT_INSTANCE_PATH = REPO_ROOT / "instance.yaml"
DEFAULT_OUT_DIR = REPO_ROOT / "tools" / "parity_checks"

PROBE_TIMEOUT = 30

Verdict = Literal["PASS", "FAIL", "UNMEASURABLE", "CARVE_OUT_VERIFIED", "CARVE_OUT_BROKEN"]
SeatState = Literal["FULL", "PARTIAL", "BROKEN"]

_VERDICT_CODE = {
    "PASS": "P", "FAIL": "F", "UNMEASURABLE": "U",
    "CARVE_OUT_VERIFIED": "CV", "CARVE_OUT_BROKEN": "CB",
}

# Prose markers directly observed in the current axes.yaml — NOT general NLP.
# Each entry cites the axis that motivated it so a future reader can tell
# whether it is still needed once the manifest is tightened.
_PROSE_MARKERS = (
    "...",            # mailbox-can-send-to-peers ("first-send exercise" placeholder)
    " + ",            # mailbox-inbox-exists ("stat ... + getfacl check")
    "; per ",         # permissions-allow-covers-mcp-tools
    "; no ",          # chat-surface-first-live-turn-verified
    "log of ",        # chat-surface-first-live-turn-verified
    "presence of ",   # harness-cli-installed-authed trailing clause
    " (or ",          # hc-controller-service ("or documented pre-HC equivalent")
    " returns nothing",  # occasionally leaks into an axis check_ref via copy-paste
)


@dataclass(frozen=True)
class Axis:
    axis_id: str
    tier: str
    description: str
    applies_to: tuple[str, ...] | str  # "all-seats" or a tuple of role-ids/personas
    source_of_truth: str
    check_method: str
    check_ref: str
    gap_history: tuple[str, ...] = ()
    # T-3766 tranche 3 (2026-09-16): probe_on overrides the default sudo-wrap-
    # to-seat.account for axes that verify cross-account state. "seat" (default)
    # sudo-wraps to seat.account per resolve_sudo_account. "auditor" runs the
    # check as the audit-runner's own account (typically recovery for the scheduled
    # substrate), which is the correct target for centrally-hosted per-seat
    # templated systemd units (pantheon-<monitor>@<persona>.timer on recovery).
    probe_on: str = "seat"


@dataclass(frozen=True)
class CarveOut:
    seat: str
    axis: str
    verdict: str
    rationale: str
    ratifier: str
    ratified_date: str
    verifier: str
    review_trigger: str


@dataclass(frozen=True)
class SeatBinding:
    role_id: str
    persona: str
    account: str
    harness: str = ""
    domain: str = ""


@dataclass(frozen=True)
class ExecResult:
    rc: int | None
    stdout: str
    stderr: str


@dataclass(frozen=True)
class CheckOutcome:
    axis_id: str
    seat: str
    verdict: Verdict
    timestamp: str
    evidence: str
    tier: str = ""  # aggregation helper only — NOT part of the T-2372 envelope

    def to_envelope(self) -> dict:
        """The exact T-2372 shape: {axis_id, seat, verdict, timestamp, evidence}."""
        return {
            "axis_id": self.axis_id,
            "seat": self.seat,
            "verdict": self.verdict,
            "timestamp": self.timestamp,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class SeatReport:
    seat: str
    outcomes: tuple[CheckOutcome, ...]
    state: SeatState


class ManifestError(Exception):
    """Raised only for genuinely-fatal manifest problems (unreadable/non-mapping file)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _warn(msg: str) -> None:
    print(f"[parity_render] WARNING: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Manifest loading — degrade gracefully: one malformed entry is skipped with a
# warning, never a hard crash that blocks rendering for every other seat/axis.
# ---------------------------------------------------------------------------

_AXIS_REQUIRED = ("axis_id", "tier", "description", "applies_to", "source_of_truth",
                  "check_method", "check_ref")
_CARVE_OUT_REQUIRED = ("seat", "axis", "verdict", "rationale", "ratifier",
                       "ratified_date", "verifier", "review_trigger")


def _load_instance_document(path: Path) -> object:
    # Lazy: run-as-script has tools/ on sys.path; package import needs tools.
    try:
        from agent_role_map import load_instance_document
    except ImportError:
        from tools.agent_role_map import load_instance_document  # type: ignore
    return load_instance_document(path)


def _load_yaml(path: Path) -> dict:
    # T-4298: parse via agent_role_map's content-keyed cache (CSafeLoader) instead
    # of a pure-Python safe_load. main() reads instance.yaml twice (load_seats +
    # instance_raw); the second read is now a cache hit. load_instance_document
    # re-reads the file on EVERY call (an edit between calls is observed, as before)
    # and returns a DEEP COPY, so the dict handed to callers is private, as before.
    # Same exception TYPES and mapping as open()+safe_load: OSError (wrapped below,
    # as before), yaml.YAMLError and UnicodeDecodeError (propagate unwrapped, as
    # before). Diagnostic text may differ: CSafeLoader's YAMLError names
    # "<unicode string>", not the file path, and shows no caret snippet.
    try:
        data = _load_instance_document(path) or {}
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: expected a YAML mapping at top level")
    return data


def load_axes(path: Path = DEFAULT_AXES_PATH) -> list[Axis]:
    raw = _load_yaml(path)
    version = raw.get("schema_version")
    if version != 1:
        _warn(f"{path}: schema_version={version!r}, expected 1 — best-effort parse")
    out: list[Axis] = []
    for entry in raw.get("axes") or []:
        missing = [f for f in _AXIS_REQUIRED if f not in entry]
        if missing:
            _warn(f"{path}: axis {entry.get('axis_id', '<unknown>')!r} missing "
                  f"required field(s) {missing} — skipped")
            continue
        applies_to = entry["applies_to"]
        if applies_to != "all-seats":
            applies_to = tuple(applies_to) if isinstance(applies_to, list) else (applies_to,)
        out.append(Axis(
            axis_id=entry["axis_id"], tier=entry["tier"], description=entry["description"],
            applies_to=applies_to, source_of_truth=entry["source_of_truth"],
            check_method=entry["check_method"], check_ref=entry["check_ref"],
            gap_history=tuple(entry.get("gap_history") or ()),
            probe_on=entry.get("probe_on", "seat"),
        ))
    return out


def load_carve_outs(path: Path = DEFAULT_CARVE_OUTS_PATH) -> list[CarveOut]:
    raw = _load_yaml(path)
    version = raw.get("schema_version")
    if version != 1:
        _warn(f"{path}: schema_version={version!r}, expected 1 — best-effort parse")
    out: list[CarveOut] = []
    for entry in raw.get("carve_outs") or []:
        missing = [f for f in _CARVE_OUT_REQUIRED if f not in entry]
        if missing:
            _warn(f"{path}: carve-out for seat {entry.get('seat', '<unknown>')!r} missing "
                  f"required field(s) {missing} — skipped")
            continue
        out.append(CarveOut(**{k: entry[k] for k in _CARVE_OUT_REQUIRED}))
    return out


def load_seats(path: Path = DEFAULT_INSTANCE_PATH) -> list[SeatBinding]:
    raw = _load_yaml(path)
    roles = raw.get("roles") or {}
    out: list[SeatBinding] = []
    for role_id, cfg in roles.items():
        if not isinstance(cfg, dict) or "persona" not in cfg or "account" not in cfg:
            continue
        out.append(SeatBinding(
            role_id=role_id, persona=cfg["persona"], account=cfg["account"],
            harness=str(cfg.get("harness", "")), domain=str(cfg.get("domain", "")),
        ))
    return out


# ---------------------------------------------------------------------------
# applies_to filtering
# ---------------------------------------------------------------------------

def axis_applies_to_seat(axis: Axis, seat: SeatBinding) -> bool:
    if axis.applies_to == "all-seats":
        return True
    return seat.role_id in axis.applies_to or seat.persona in axis.applies_to


# ---------------------------------------------------------------------------
# Command rendering
# ---------------------------------------------------------------------------

def substitute_placeholders(check_ref: str, seat: SeatBinding) -> str:
    # G-k fix (T-3751 tranche 2): <role> substitution added because the G-k
    # tranche-1 axes (billing-model-declared, domain-scope-declared) look up
    # instance.yaml's roles: block keyed by role-id, not persona. Without this
    # substitution the check_ref literally reads d.get("roles",{}).get("<role>",{})
    # and returns {} for every seat → false FAIL.
    return (check_ref
            .replace("<account>", seat.account)
            .replace("<persona>", seat.persona)
            .replace("<role>", seat.role_id))


def looks_like_shell_command(check_ref: str) -> bool:
    """Deliberately narrow, evidence-based rejection list — see _PROSE_MARKERS."""
    return not any(marker in check_ref for marker in _PROSE_MARKERS)


def resolve_sudo_account(axis: Axis, seat: SeatBinding) -> str | None:
    """Avoids the T-2232 / delivery_reachability sudo-to-self-denial + double-sudo gotcha:
    if the raw (pre-substitution) check_ref already embeds its own `sudo -u <account>`,
    wrapping it again would nest a sudo call INSIDE a shell already running as that
    account — sudo-to-self, which is denied. Only add the wrap when check_ref doesn't
    already self-elevate.

    T-3766 (2026-09-16): file_grep now unconditionally sudo-wraps too. The prior
    `<account>-in-check_ref` gate missed per-account state where the check verb
    doesn't literally embed the path — e.g. mcp-python-package-pinned uses
    importlib.metadata to query the running python's site-packages, which
    depends on WHICH account's python it runs as, not on any literal path.
    Runner.execute() still elides the sudo call when sudo_account == self (per
    _me() self-detect), so the change is a no-op for self-audit paths.

    T-3766 tranche 3 (2026-09-16): probe_on: 'auditor' overrides the sudo-wrap
    unconditionally — axes that verify centrally-hosted per-seat state (e.g.
    recovery-run monitor timers templated per persona) MUST run as the audit-runner
    itself, not sudo-wrapped to the seat. Highest precedence: probe_on comes
    first so an explicit auditor-scope isn't defeated by check_ref-embedded sudo
    prose.

    T-4532 (2026-10-04): stale_check wraps too. check_stale.py checks the clone it
    is invoked from under /home/<account>, and a 0750 home (suborch2) denies the
    auditor's uid, so the axis read UNMEASURABLE instead of PASS/FAIL."""
    if getattr(axis, "probe_on", "seat") == "auditor":
        return None
    if "sudo" in axis.check_ref:
        return None
    if axis.check_method in ("systemd_query", "canary", "file_grep", "stale_check"):
        return seat.account
    return None


@dataclass(frozen=True)
class RenderedCheck:
    axis_id: str
    command: str
    sudo_account: str | None
    renderable: bool


def render_axis_command(axis: Axis, seat: SeatBinding) -> RenderedCheck:
    renderable = looks_like_shell_command(axis.check_ref)
    command = substitute_placeholders(axis.check_ref, seat)
    sudo_account = resolve_sudo_account(axis, seat) if renderable else None
    return RenderedCheck(axis_id=axis.axis_id, command=command,
                          sudo_account=sudo_account, renderable=renderable)


# ---------------------------------------------------------------------------
# Execution seam — the ONLY place that shells out. Mirrors
# delivery_reachability.Ops's self-account-skip + sudo -n pattern.
# ---------------------------------------------------------------------------

class Runner:
    def _me(self) -> str:
        try:
            import pwd
            return pwd.getpwuid(os.geteuid()).pw_name
        except Exception:
            return os.environ.get("USER", "")

    def execute(self, command: str, sudo_account: str | None,
                timeout: int = PROBE_TIMEOUT) -> ExecResult:
        if sudo_account and sudo_account != self._me():
            argv = ["sudo", "-n", "-u", sudo_account, "bash", "-c",
                    f"XDG_RUNTIME_DIR=/run/user/$(id -u) {command}"]
        else:
            argv = ["bash", "-c", command]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return ExecResult(rc=None, stdout="", stderr="execution failed or timed out")
        return ExecResult(rc=r.returncode, stdout=r.stdout, stderr=r.stderr)


# ---------------------------------------------------------------------------
# Verdict classification — ORG-099 §8 alarm-biased contract.
# ---------------------------------------------------------------------------

def classify_outcome(check_method: str, result: ExecResult) -> tuple[Verdict, str]:
    rc, stdout, stderr = result.rc, result.stdout, result.stderr

    if rc in (126, 127):
        return "UNMEASURABLE", (f"rc={rc}: not an executable command — check_ref may not be "
                                 f"literal shell text (needs manifest review)")
    if rc is None:
        return "UNMEASURABLE", "could not execute (timeout or exec failure)"

    boolish = stdout.strip().lower()
    if boolish in ("true", "false"):
        return ("PASS" if boolish == "true" else "FAIL"), f"stdout printed boolean: {boolish}"

    if check_method == "ticket_lookup":
        if rc == 0:
            return "PASS", "assertion passed (rc=0)"
        if "AssertionError" in stderr:
            return "FAIL", f"assertion failed: {stderr.strip()[-300:]}"
        return "UNMEASURABLE", (f"check_ref raised an unexpected error, not a clean assertion "
                                 f"failure — likely a broken check_ref: {stderr.strip()[-300:]}")

    if check_method == "file_grep":
        if rc == 0:
            return "PASS", (stdout.strip() or "command succeeded")[:300]
        if "FileNotFoundError" in stderr or "No such file or directory" in stderr:
            return "FAIL", f"required file/path absent: {stderr.strip()[-300:]}"
        if "PermissionError" in stderr or "Permission denied" in stderr:
            return "UNMEASURABLE", f"permission denied — could not ask: {stderr.strip()[-300:]}"
        return "UNMEASURABLE", f"rc={rc}, unrecognized failure shape: {stderr.strip()[-300:]}"

    if check_method == "stale_check":
        # Freshness detectors (persona_orientation/check_stale.py) with an EXPLICIT,
        # documented rc contract: 0 = all sources fresh (PASS), 2 = at least one
        # source drifted OR source-bindings.yaml missing (FAIL). Trusting rc here is
        # safe — UNLIKE systemd_query below — precisely because the contract is fixed
        # and purpose-built, not a wrapper-squashed shell shape (ORG-099 §8). The
        # report prints to stdout on fresh, stderr on stale. T-3813: this shape used
        # to be labelled file_grep and fell through that branch's "unrecognized
        # failure shape" -> UNMEASURABLE, burying real drift behind the >=4-run
        # persistent-unmeasurable path (a ~19h detection window on 2026-09-19).
        if rc == 0:
            return "PASS", (stdout.strip() or "all sources fresh (rc=0)")[:300]
        if rc == 2:
            return "FAIL", (stderr.strip() or stdout.strip() or "sources drifted (rc=2)")[:300]
        if "PermissionError" in stderr or "Permission denied" in stderr:
            return "UNMEASURABLE", f"permission denied — could not ask: {stderr.strip()[-300:]}"
        return "UNMEASURABLE", f"rc={rc}, unrecognized check_stale shape: {stderr.strip()[-300:]}"

    if check_method in ("systemd_query", "canary"):
        # NEVER auto-PASS/FAIL from rc alone: systemctl subcommand output shapes vary too
        # much (list-timers prints "0 timers listed." on a clean rc=0 exit) to trust rc==0
        # as PASS without a boolean-normalized check_ref. ORG-099 §8 names exactly this
        # class of "wrapper-squashed exit codes" as the ambiguous case to alarm on.
        return "UNMEASURABLE", (
            f"rc={rc}: systemd/canary output shape is not trusted for PASS/FAIL without a "
            f"boolean-normalized check_ref (ORG-099 §8) — stdout={stdout.strip()[:200]!r}")

    # G-l (T-3751, rev-t3749-parity-render nice-to-have #1): unrecognized
    # check_method → UNMEASURABLE unconditionally. Trusting rc==0 as PASS here was
    # a latent silent-green seam per ORG-099 §8 alarm-bias. Adding a new axis with
    # a novel check_method now requires adding a matching classify branch above —
    # the manifest-side amendment is bound to the code-side amendment. No live
    # axis uses gh_api or any other unhandled method today, so this fires only on
    # future underspec. Justifies-via P7 (favor cheap protective action) + ORG-099
    # §8 (ambiguous outputs alarm, never silent-green).
    return "UNMEASURABLE", (
        f"unrecognized check_method {check_method!r} (rc={rc}) — no PASS/FAIL "
        f"contract; add an explicit branch above when introducing a new method")


# ---------------------------------------------------------------------------
# Carve-out matching + verification — no eval(); three named structural shapes
# plus an honest UNMEASURABLE fallback for anything else.
# ---------------------------------------------------------------------------

def find_carve_out(seat: SeatBinding, axis_id: str, carve_outs: list[CarveOut]) -> CarveOut | None:
    for co in carve_outs:
        if co.seat == seat.persona and co.axis == axis_id:
            return co
    return None


# fullmatch (not search) is load-bearing: a compound verifier like consultant's
# "instance.yaml roles.peer-daemon-codex.harness == 'codex' + codex-supervisor unit
# present per §12" must NOT be silently reduced to just its first half — search()
# would happily match the instance.yaml portion and ignore the rest, reporting false
# confidence on an assertion only half-checked. An optional trailing parenthetical
# comment (e.g. "(still open)") is tolerated since it carries no assertion content.
_TRAILING_COMMENT = r"(?:\s*\([^)]*\))?"
_INSTANCE_PATH_RE = re.compile(
    r"instance\.yaml\s+roles\.([\w-]+)\.([\w.]+)\s+(==|includes)\s+'([^']*)'" + _TRAILING_COMMENT
)
_TICKET_STATE_RE = re.compile(
    r"(T-\d+)\s+state\s+==\s+(\w+)(?:\s+or\s+(\w+))?" + _TRAILING_COMMENT
)
_SYSTEMD_ABSENCE_RE = re.compile(
    r"systemctl\s+--user\s+list-unit-files\s+'([^']+)'\s+returns nothing" + _TRAILING_COMMENT
)


def _ticket_state(ticket_id: str) -> str | None:
    for state in ("inbox", "active", "done"):
        if (REPO_ROOT / "tickets" / state / f"{ticket_id}.md").exists():
            return state
    return None


def verify_carve_out(carve_out: CarveOut, instance_raw: dict, runner: Runner,
                      seats_by_persona: dict[str, SeatBinding], execute: bool) -> tuple[Verdict, str]:
    text = carve_out.verifier.strip()
    if not execute:
        return "UNMEASURABLE", f"dry-run: not executed; would verify: {text}"

    m = _INSTANCE_PATH_RE.fullmatch(text)
    if m:
        role_id, dotted, op, expected = m.groups()
        node = (instance_raw.get("roles") or {}).get(role_id)
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return "CARVE_OUT_BROKEN", f"instance.yaml roles.{role_id}.{dotted} does not exist"
            node = node[part]
        if op == "==":
            ok = str(node) == expected
        else:  # includes
            ok = expected in (node if isinstance(node, (list, tuple)) else str(node))
        return ("CARVE_OUT_VERIFIED" if ok else "CARVE_OUT_BROKEN",
                f"instance.yaml roles.{role_id}.{dotted} = {node!r}")

    m = _TICKET_STATE_RE.fullmatch(text)
    if m:
        tid, s1, s2 = m.groups()
        expected_states = {s for s in (s1, s2) if s}
        actual = _ticket_state(tid)
        if actual is None:
            return "UNMEASURABLE", f"{tid} not found in tickets/{{inbox,active,done}}"
        return ("CARVE_OUT_VERIFIED" if actual in expected_states else "CARVE_OUT_BROKEN",
                f"{tid} state={actual}")

    m = _SYSTEMD_ABSENCE_RE.fullmatch(text)
    if m:
        unit = m.group(1)
        seat = seats_by_persona.get(carve_out.seat)
        if seat is None:
            return "UNMEASURABLE", f"seat {carve_out.seat!r} not found in instance.yaml"
        result = runner.execute(f"systemctl --user list-unit-files '{unit}' --no-legend",
                                 sudo_account=seat.account)
        # systemctl list-unit-files returns rc=1 both when (a) no matching
        # units exist (the intended "absence-verified" outcome) and (b) the
        # user manager is unreachable ("Failed to connect to bus"). Empty
        # stdout+stderr distinguishes (a) from (b) — (a) leaves both blank.
        if result.rc != 0:
            if not result.stdout.strip() and not result.stderr.strip():
                return "CARVE_OUT_VERIFIED", f"list-unit-files: no matching units (rc={result.rc}, empty stderr)"
            return "UNMEASURABLE", (f"could not query {seat.account}'s user manager "
                                     f"(rc={result.rc}, stderr={result.stderr.strip()[:200]!r})")
        return ("CARVE_OUT_VERIFIED" if not result.stdout.strip() else "CARVE_OUT_BROKEN",
                f"list-unit-files output: {result.stdout.strip()[:200]!r}")

    return "UNMEASURABLE", f"verifier text does not match a recognized structural shape: {text[:200]!r}"


# ---------------------------------------------------------------------------
# Per-seat evaluation + aggregation (ORG-099 §2 contract)
# ---------------------------------------------------------------------------

_NON_PASSING = {"FAIL", "UNMEASURABLE", "CARVE_OUT_BROKEN"}


def run_check(axis: Axis, seat: SeatBinding, runner: Runner, execute: bool) -> CheckOutcome:
    rendered = render_axis_command(axis, seat)
    if not execute:
        evidence = (f"dry-run: not executed; would run: {rendered.command}" if rendered.renderable
                    else f"dry-run: check_ref is not a literal shell command: {axis.check_ref!r}")
        return CheckOutcome(axis.axis_id, seat.persona, "UNMEASURABLE", _now(), evidence, axis.tier)
    if not rendered.renderable:
        return CheckOutcome(axis.axis_id, seat.persona, "UNMEASURABLE", _now(),
                             f"check_ref is not a literal shell command: {axis.check_ref!r}", axis.tier)
    result = runner.execute(rendered.command, rendered.sudo_account)
    verdict, evidence = classify_outcome(axis.check_method, result)
    return CheckOutcome(axis.axis_id, seat.persona, verdict, _now(), evidence, axis.tier)


def evaluate_seat(seat: SeatBinding, axes: list[Axis], carve_outs: list[CarveOut],
                   instance_raw: dict, seats_by_persona: dict[str, SeatBinding],
                   runner: Runner, execute: bool) -> SeatReport:
    outcomes: list[CheckOutcome] = []
    for axis in axes:
        if not axis_applies_to_seat(axis, seat):
            continue
        carve_out = find_carve_out(seat, axis.axis_id, carve_outs)
        if carve_out is not None:
            verdict, evidence = verify_carve_out(carve_out, instance_raw, runner, seats_by_persona, execute)
            outcomes.append(CheckOutcome(axis.axis_id, seat.persona, verdict, _now(), evidence, axis.tier))
        else:
            outcomes.append(run_check(axis, seat, runner, execute))
    return SeatReport(seat.persona, tuple(outcomes), aggregate_seat_state(outcomes))


def aggregate_seat_state(outcomes: list[CheckOutcome]) -> SeatState:
    """Per ORG-099 §2. UNMEASURABLE never downgrades to PASS for aggregation purposes
    either — a HARD axis we couldn't measure is treated as BROKEN, not benefit-of-the-doubt
    FULL, matching the alarm-biased directional principle throughout this tool."""
    if any(o.tier in ("HARD", "BILLING", "DOMAIN") and o.verdict in _NON_PASSING for o in outcomes):
        return "BROKEN"
    if any(o.tier == "SOFT" and o.verdict in _NON_PASSING for o in outcomes):
        return "PARTIAL"
    return "FULL"


# ---------------------------------------------------------------------------
# Rendering: per-seat script text + fleet table
# ---------------------------------------------------------------------------

def _bash_single_quote(text: str) -> str:
    """Safe single-quoting for embedding arbitrary text in a generated bash line —
    several check_refs (e.g. ticket-persona-registered) already contain single quotes
    of their own (a python3 -c '...' invocation); naive f"'{text}'" wrapping breaks on
    those. Standard idiom: close the quote, emit an escaped quote, reopen."""
    return "'" + text.replace("'", "'\"'\"'") + "'"


def render_seat_script(seat: SeatBinding, axes: list[Axis], carve_outs: list[CarveOut]) -> str:
    """Portable, human-readable standalone artifact. Carve-outs are documented as
    comments rather than re-implemented in bash — the scheduled BT3 audit is expected
    to invoke this tool directly (matching delivery_reachability.py's own systemd-timer
    precedent), so the .sh file is a convenience/audit-trail artifact, not the primary
    execution path; duplicating the carve-out verifier logic in two languages would
    only create a second place for it to drift."""
    lines = [
        "#!/usr/bin/env bash",
        f"# Generated by tools/parity_render.py for seat={seat.persona} (role={seat.role_id})",
        "# ORG-099 seat-parity check script — DO NOT HAND-EDIT, regenerate from the manifest.",
        "set -u",
        "",
        "run_as() {",
        '  local account="$1"; shift',
        '  if [ "$(whoami)" = "$account" ]; then',
        '    XDG_RUNTIME_DIR="/run/user/$(id -u)" bash -c "$*"',
        "  else",
        '    sudo -n -u "$account" bash -c "XDG_RUNTIME_DIR=/run/user/\\$(id -u) $*"',
        "  fi",
        "}",
        "",
    ]
    for axis in axes:
        if not axis_applies_to_seat(axis, seat):
            continue
        carve_out = find_carve_out(seat, axis.axis_id, carve_outs)
        lines.append(f"# axis: {axis.axis_id} (tier={axis.tier})")
        if carve_out is not None:
            lines.append(f"#   carve-out declared: {carve_out.verdict} — verifier: {carve_out.verifier}")
            lines.append("")
            continue
        rendered = render_axis_command(axis, seat)
        if not rendered.renderable:
            lines.append(f"#   NOT RENDERABLE (prose check_ref, needs manifest refinement): {axis.check_ref}")
            lines.append("")
            continue
        if rendered.sudo_account:
            lines.append(f'run_as "{rendered.sudo_account}" {_bash_single_quote(rendered.command)}')
        else:
            lines.append(f"bash -c {_bash_single_quote(rendered.command)}")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_fleet_table_md(reports: list[SeatReport], axes: list[Axis]) -> str:
    header = "| seat | " + " | ".join(a.axis_id for a in axes) + " |"
    sep = "|---" * (len(axes) + 1) + "|"
    rows = [header, sep]
    for r in reports:
        by_axis = {o.axis_id: o for o in r.outcomes}
        cells = [_VERDICT_CODE.get(by_axis[a.axis_id].verdict, "?") if a.axis_id in by_axis else "-"
                 for a in axes]
        rows.append(f"| {r.seat} | " + " | ".join(cells) + " |")
    legend = ("\n\nLegend: P=PASS F=FAIL U=UNMEASURABLE CV=CARVE_OUT_VERIFIED "
              "CB=CARVE_OUT_BROKEN -=not-applicable")
    states = "\n".join(f"- {r.seat}: {r.state}" for r in reports)
    return "\n".join(rows) + legend + "\n\nSeat states:\n" + states + "\n"


def render_fleet_table_json(reports: list[SeatReport]) -> str:
    return json.dumps(
        {"seats": [{"seat": r.seat, "state": r.state,
                    "outcomes": [o.to_envelope() for o in r.outcomes]} for r in reports]},
        indent=2,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_EXIT_CODE = {"FULL": 0, "PARTIAL": 1, "BROKEN": 2}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--seat", help="render/run the check script for one persona")
    mode.add_argument("--fleet", action="store_true", help="render/run the fleet audit table")
    ap.add_argument("--dry-run", action="store_true", help="render only; never execute a check")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument("--report-path", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--axes-path", type=Path, default=DEFAULT_AXES_PATH)
    ap.add_argument("--carve-outs-path", type=Path, default=DEFAULT_CARVE_OUTS_PATH)
    ap.add_argument("--instance-path", type=Path, default=DEFAULT_INSTANCE_PATH)
    args = ap.parse_args(argv)

    axes = load_axes(args.axes_path)
    carve_outs = load_carve_outs(args.carve_outs_path)
    seats = load_seats(args.instance_path)
    seats_by_persona = {s.persona: s for s in seats}
    instance_raw = _load_yaml(args.instance_path)
    runner = Runner()
    execute = not args.dry_run

    if args.seat:
        seat = seats_by_persona.get(args.seat)
        if seat is None:
            print(f"[parity_render] unknown seat {args.seat!r}", file=sys.stderr)
            return 2
        if args.dry_run:
            print(render_seat_script(seat, axes, carve_outs))
            return 0
        report = evaluate_seat(seat, axes, carve_outs, instance_raw, seats_by_persona, runner, execute)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        script_path = args.out_dir / f"{seat.persona}.sh"
        script_path.write_text(render_seat_script(seat, axes, carve_outs))
        script_path.chmod(0o755)
        for o in report.outcomes:
            print(json.dumps(o.to_envelope()))
        print(f"[parity_render] seat={seat.persona} state={report.state}", file=sys.stderr)
        return _EXIT_CODE[report.state]

    # --fleet
    reports = [evaluate_seat(s, axes, carve_outs, instance_raw, seats_by_persona, runner, execute)
               for s in seats]
    output = (render_fleet_table_md(reports, axes) if args.format == "md"
              else render_fleet_table_json(reports))
    if args.report_path:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(output)
    print(output)
    if args.dry_run:
        # A dry run executed nothing, so every HARD axis reads UNMEASURABLE by
        # construction and the aggregate would always be BROKEN — that's a preview
        # artifact, not a verdict. Mirrors --seat's dry-run exit-0 convention.
        return 0
    return max((_EXIT_CODE[r.state] for r in reports), default=0)


if __name__ == "__main__":
    sys.exit(main())
