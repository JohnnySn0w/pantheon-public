#!/usr/bin/python3 -I
"""codex-recovery broker — ORG-048 §5 privilege surface, increment 1 (T-2805),
re-homed to the recovery seat + repair lease (T-4884 B4).

INERT in this increment: this module is a Python file in the repo. Nothing is
installed and no sudoers entry points at it. It grants nothing until the
gated install (T-4884 B11: install to /usr/local/sbin/codex-recovery, the
generated sudoers.d fragment, the Recovery-supervised first-fire).

At install time this program is invoked as root via sudo. The sudoers
fragment is GENERATED from the manifest by tools/codex_recovery_sudoers.py
(no wildcards, ever):
    codex-fallback ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery
    claude-fallback ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery lease acquire <target>
    ... one exact line per (lease op, target) for claude-fallback ...
The invoker passes an enum verb + a target key; this broker validates against a
root-owned manifest, drops to the target account via a hardened root->target
transition, executes exactly one fixed action, and writes a tamper-resistant
audit record. It is the sole privileged surface — the proto-Action-Broker
(ORG-063) this ADR converges toward.

Invoker identity (T-4884 B4): the invoking account is taken from sudo's own
SUDO_UID, cross-checked against SUDO_USER and getpwuid(SUDO_UID), and must be
listed in the manifest's `invokers` map with the verb it is calling. `root`
and `codex` are never invokers. Path-override flags are refused under
privilege, so the caller cannot point the broker at other state.

Repair lease (T-4884 B4, design note §4.3, D-5): `lease {acquire|release|
status} <target>` keeps one root-owned, UID-bound lease per target under
<state dir>/leases/. Every destructive verb (`kill-session`, `unstick`, and
anything later added to `dispatch`) refuses unless the invoking uid holds a
live lease on that target; the check runs under the same flock that a
competing `lease acquire` needs, and the lock is held through the action.
Every lease transition (acquire, renew, release, an expiry observed by any
lease op or verb) is ALSO appended, best-effort and enum/int only, to the
readable export DEFAULT_LEASE_EXPORT_PATH (root:pantheon-health, 2750/0640)
so the unprivileged detective check tools/lease_audit.py can see lease state
(T-4884 B4b). The root-only audit log stays authoritative and fail-closed; an
export failure never blocks an action.

Verb set THIS increment: `lease`, `kill-session`, `unstick` (control-keys only,
`wake-drop-cancel` | `spend-modal-dismiss`), `reset-failed <target> <unit>`
(T-5099, B14 S1, below), `restart <target> <unit>` (T-5100, B14 S2, below),
`linger-enable <target>` (T-5059, below), and `selftest` (manifest preflight,
touches nothing). `capture` and `restore` are explicitly deferred (build spec
§Scope).

`reset-failed <target> <unit>` (T-5099, B14 S1): runs exactly
`setpriv <target> -- /usr/bin/env XDG_RUNTIME_DIR=/run/user/<uid>
/usr/bin/systemctl --user reset-failed -- <unit>` (setpriv --reset-env drops
XDG_RUNTIME_DIR, which `systemctl --user` needs to find the target's manager).
The unit must appear verbatim in the target's manifest `reset_failed_units`
list (exact names, .service only, no globs). An absent or empty list means the
verb refuses for that target. It is in DESTRUCTIVE_VERBS, so it takes the
repair lease, the rate limiter, and the fail-closed pre_action audit like
unstick. Install order: broker first, then a manifest that lists it.

`restart <target> <unit>` (T-5100, B14 S2): the same shape with
`systemctl --user restart`, gated by its own manifest list `restart_units`.
It must never end the live session: cycling the session is the runtime
controller's job, and kill-session covers a dead pane. So, regardless of the
manifest, a unit is refused if it is the target's `user_unit`, a
`pantheon-runtime-controller@` or `pantheon-persona-consume@` instance (the
consume oneshot can launch a runtime), or a bespoke `claude-telegram*` unit
(RESTART_DENY_PREFIXES). The manifest parser rejects such a unit in
`restart_units`, and dispatch re-checks it.

`linger-enable <target>` (T-5059, T-4711 grant #1; the T-5030 repair): runs
exactly `/usr/bin/loginctl enable-linger <manifest target account>` AS ROOT
(it does not drop to the target - enable-linger for another user needs root).
The account comes only from the root-owned manifest, never from argv; there is
no other argument. It is restorative and non-destructive, so it is NOT in
DESTRUCTIVE_VERBS and does not take the repair lease (the lease serializes
destructive actions on a pane). It is manifest-authorized per invoker, shares
the RateLimiter (key `linger-enable__<target>`), and is idempotent: an
already-lingering account is success and runs nothing. Linger state is read
before and after from /var/lib/systemd/linger/<account> (regular file, not a
symlink) and both states land in the audit record; a zero exit without the
linger file present afterwards is a refusal (exit 1), not a success.
Install order: the broker binary first, then the manifest that lists
`linger-enable`; an older broker refuses a manifest naming an unknown verb.

Third-seat support (T-5124, T-4924 H3a; plan notes/2026-10-12-t4924-...md §5.1/§6):
  - a target may be `"lease_only": true` (only `account` and `uid`): any invoker
    may lease it, so Recovery's and Watchman's detective lease audit has a lease to
    find, but NO verb may act on it (dispatch and linger-enable both refuse);
  - an invoker may carry a `precondition` of kind `higher-precedence-bad`: its
    destructive verbs run only while every listed repairer reads BAD from
    root-read evidence (an `export-record` from Recovery's /run/pantheon-health, or
    a `watcher-heartbeat` like /run/recovery-seat-health/watcher.json). Healthy
    or unknown refuses. It is checked under the lease lock, right before the
    rate limiter, and its enum states land in the refusal's audit record.
  - T-5128: the same precondition also gates `lease acquire` (new and renew)
    for an invoker that carries one, so a lower-precedence seat cannot squat a
    lease Recovery or Watchman needs. Unknown refuses here too (fail-closed, plan
    D-42). `release` and `status` stay ungated; invokers without a
    precondition are unchanged.
  - T-5129: both gates use a DEBOUNCED verdict. One BAD snapshot is not
    enough: each repairer must read BAD on two consecutive broker reads that
    see two different evidence snapshots at least
    PRECONDITION_DEBOUNCE_MIN_SECS apart (stale evidence cannot advance, so
    two stale reads that far apart confirm), with no HEALTHY or UNKNOWN read
    within PRECONDITION_DEBOUNCE_CONFIRM_GAP_SECS of the first; once
    confirmed, a run holds while each BAD read follows the previous one within
    PRECONDITION_DEBOUNCE_CONFIRMED_GAP_SECS. The
    per-invoker streak lives in <state dir>/precondition/<invoker>.json
    (root, 0600, dir 0700), read-modified-written under its own flock nested
    inside the target lease lock. Invokers without a precondition never
    open it.

Design precedent this module follows deliberately, not by accident:
  - tools/credential_broker.py — O_NOFOLLOW + fstat-after-open validation
    (never stat-by-path), fail-closed flock'd audit appends, bounded
    identifier regexes. The manifest-loading half of this module is that
    pattern applied to a different artifact.
  - tools/_pane_predicates.py — I/O-gathering kept separate from the pure
    decision predicate. The unstick foreground-process-group check is split
    the same way: `PaneForegroundProbe` gathers OS facts (best-effort — real
    cross-account /proc+tmux fidelity is proven live at the Recovery-supervised
    increment-2 first-fire, ADR ORG-048 install-gate item 3, not here);
    `_validate_foreground_snapshot` is the exhaustively-unit-tested decision.
  - tools/_lease.py — flock-based, daemon-safe state coordination is the
    model for the rate-limit counter below (simpler: no staleness/reaping
    needed here, since each invocation is a single short-lived process).

Revalidate-before-act (no parse-once-act-later TOCTOU, install-gate item 2):
each broker invocation is a ONE-SHOT CLI process — one sudoers exec runs
exactly one verb. The manifest load happens once, immediately before the
single action it authorizes; there is no long-lived cache that could go
stale between "parse" and "act". `build_kill_session_argv` /
`build_unstick_argv` additionally re-resolve the account's live uid via
`getpwnam` at argv-build time and refuse if it no longer matches the
manifest-declared uid, as a second, paranoid close of the same window.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Mapping, Protocol


# ---------------------------------------------------------------------------
# Absolute binary paths (contract item 1 — never resolved via $PATH search).
# ---------------------------------------------------------------------------

TMUX = "/usr/bin/tmux"
SETPRIV = "/usr/bin/setpriv"
LOGINCTL = "/usr/bin/loginctl"
SYSTEMCTL = "/usr/bin/systemctl"
ENV_BIN = "/usr/bin/env"
# logind's own persistent linger state: one empty file per lingering account.
LINGER_STATE_DIR = Path("/var/lib/systemd/linger")

# ---------------------------------------------------------------------------
# Defaults (all overridable — the real, root-owned paths at increment-2
# install time; tests inject tempdir equivalents throughout).
# ---------------------------------------------------------------------------

DEFAULT_MANIFEST_PATH = Path("/etc/codex-recovery/manifest.json")
DEFAULT_AUDIT_LOG_PATH = Path("/var/lib/codex-recovery/audit.jsonl")
DEFAULT_RATE_LIMIT_STATE_DIR = Path("/run/codex-recovery")
# Readable lease-event export (T-4884 B4b): the detective lease check
# (tools/lease_audit.py) runs unprivileged on both recovery seats and cannot
# read the root-only audit log. Root-owned, group pantheon-health (the same
# reader set as the health export), dir 2750, file 0640. Provisioned by root
# in B7/B11; best-effort, never gates an action.
DEFAULT_LEASE_EXPORT_PATH = Path("/var/lib/codex-recovery-public/lease-events.jsonl")
LEASE_EXPORT_GROUP = "pantheon-health"
LEASE_EXPORT_DIR_MODE = 0o2750
LEASE_EXPORT_FILE_MODE = 0o640
LEASE_EXPORT_OPS = ("acquire", "renew", "release", "expire_observed")

MANIFEST_EXPECTED_OWNER_UID = 0
MANIFEST_EXPECTED_MODE = 0o644
MANIFEST_PARENT_EXPECTED_MODE = 0o755
MAX_MANIFEST_BYTES = 65536

SUBPROCESS_TIMEOUT_SECS = 10
DEFAULT_UMASK = 0o077

RATE_LIMIT_WINDOW_SECS = 300
RATE_LIMIT_MAX_ACTIONS = 3

ENABLED_VERBS = ("kill-session", "unstick", "selftest", "lease", "linger-enable", "reset-failed", "restart")
# Verbs that act on a target. Every one of them is executed only through
# dispatch(), which refuses unless the invoker holds a live lease on the
# target. A future restart/restore verb is added HERE and inherits the check.
DESTRUCTIVE_VERBS = frozenset({"kill-session", "unstick", "reset-failed", "restart"})
MAX_RESET_FAILED_UNITS = 16
MAX_RESTART_UNITS = 16
# T-5100: units `restart` may never touch, whatever the manifest says. Each
# owns the live session, can launch a runtime, or (T-5112) can end or resume
# one when fired: restart-drain re-runs the tombstone drain, ho-resumer fires
# `claude --resume`, acp-bridge@ kill-sessions on stop, context-fill-monitor@
# fires session restarts, and the bun-child watchdog fires self_restart. Pokers
# that only inject behind their own idle gates (activity-monitor@, keep-going@,
# cap-modal-reaper, unknown-dialog-monitor) are not session cyclers and are not
# denied here, but none is listed in any restart_units today.
RESTART_DENY_PREFIXES = (
    "pantheon-runtime-controller@", "pantheon-persona-consume@", "claude-telegram",
    "pantheon-restart-drain", "pantheon-ho-resumer", "pantheon-acp-bridge@",
    "pantheon-context-fill-monitor@", "recovery-telegram-child-watchdog",
)
LEASE_OPS = ("acquire", "release", "status")
# Fixed TTL; deliberately not a CLI flag or a manifest field, so no caller
# can set it (T-4884 B4).
LEASE_TTL_SECS = 15 * 60
MAX_LEASE_RECORD_BYTES = 4096
LEASE_DIR_NAME = "leases"
LEASE_DIR_MODE = 0o700
LEASE_FILE_MODE = 0o600
# T-5129: precondition debounce (recovery_watcher DEBOUNCE_RUNS=2 equivalent).
PRECONDITION_DIR_NAME = "precondition"
PRECONDITION_DEBOUNCE_MIN_SECS = 60          # >= the health exporter's 1-min cadence
# Two windows (T-5129 review):
#  - CONFIRM_GAP bounds an UNCONFIRMED run: the read that would confirm it must
#    come within this many seconds of the run's FIRST read (about 3x the 60s
#    exporter cadence); an unconfirmed run older than that starts over, so two
#    BAD reads far apart cannot confirm across an unobserved recovery.
#  - CONFIRMED_GAP holds a CONFIRMED run (a read already returned `bad`): it
#    stays confirmed while each later read is BAD within this many seconds of
#    the previous read, so lease renewals and mid-repair verbs keep working.
PRECONDITION_DEBOUNCE_CONFIRM_GAP_SECS = 180
PRECONDITION_DEBOUNCE_CONFIRMED_GAP_SECS = LEASE_TTL_SECS
MAX_PRECONDITION_STATE_BYTES = 4096
STALE_SNAPSHOT = "stale"
UNSTICK_ACTIONS = ("wake-drop-cancel", "spend-modal-dismiss")

# Fixed, non-negotiable key sequences — control keys ONLY this increment (no
# retype-template; see ADR ORG-048 D3 + the access-model round-2 disposition).
# wake-drop-cancel mirrors the live wake-drop fix's first step (C-c) minus
# the deferred retype; spend-modal-dismiss is the live remediation recipe
# (sop/remediation/agent-spend-limit-wedge.md step 3: `tmux send-keys Escape`).
UNSTICK_KEY_SEQUENCES: dict[str, tuple[str, ...]] = {
    "wake-drop-cancel": ("C-c",),
    "spend-modal-dismiss": ("Escape",),
}

# Defense-in-depth on top of the allowlist: refuse outright if the foreground
# exe's basename is one of these, regardless of allowlist/ownership state.
DENYLIST_FOREGROUND_BASENAMES = frozenset({
    "bash", "sh", "zsh", "dash", "fish", "ksh", "tcsh", "csh",
    "vim", "vi", "nvim", "nano", "emacs", "pico",
    "less", "more", "most",
    "tmux", "screen",
    "ssh", "sudo", "su", "doas", "setpriv", "runuser",
    "python", "python3", "perl", "ruby", "node", "php", "lua", "irb",
})

# Root only by default. A real "deploy-owned, not root" wrapper install is a
# reconciliation the increment-2 install plan owns (see module docstring's
# sibling note in the T-2805 plan report): today's live claude.exe binaries
# are <account>:pantheon-shared 0755, NOT root-owned, so this check as
# written REFUSES against them — intentionally; it is not weakened to match.
DEFAULT_DEPLOY_OWNER_UIDS: frozenset[int] = frozenset({0})

NON_FLEET_ACCOUNT_NAMES = frozenset({"root", "codex"})
MIN_FLEET_UID = 1000
MAX_FLEET_UID = 60000  # excludes nobody/nogroup (65534) and other pseudo-uids

_AUDIT_FORBIDDEN_KEYS = frozenset({"pane_text", "pane_content", "cmdline", "stdout", "stderr"})


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------

class CodexRecoveryError(RuntimeError):
    """Base error for the codex-recovery broker."""


class ManifestValidationError(CodexRecoveryError):
    """The manifest failed structural, ownership, or content validation."""


class DispatchRefusedError(CodexRecoveryError):
    """A dispatch precondition failed; the caller must refuse to act."""


class AuditWriteError(CodexRecoveryError):
    """The audit sink could not be written — callers must fail closed."""


class LeaseStateError(CodexRecoveryError):
    """Lease state failed ownership/mode/symlink/content validation — refuse."""


# ---------------------------------------------------------------------------
# Bounded, metachar-free identifier / path validation.
# ---------------------------------------------------------------------------

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?$")
_MAX_IDENTIFIER_LEN = 64

_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._@-]+$")
_MAX_PATH_LEN = 4096


def _validate_identifier(label: str, value: object) -> None:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{label} must be a string")
    if len(value) > _MAX_IDENTIFIER_LEN or not _IDENTIFIER_RE.fullmatch(value):
        raise ManifestValidationError(f"{label} must be a bounded, safe identifier")


def _validate_unit_identifier(label: str, value: object) -> None:
    """A systemd unit name: a safe identifier, or two of them joined by exactly
    one '@' (a template instance, e.g. pantheon-runtime-controller@recovery.service).
    T-5039: recovery runs under such a unit, so B14's restart verbs need it."""
    if not isinstance(value, str):
        raise ManifestValidationError(f"{label} must be a string")
    parts = value.split("@")
    if (
        len(value) > _MAX_IDENTIFIER_LEN
        or len(parts) > 2
        or not all(_IDENTIFIER_RE.fullmatch(part) for part in parts)
    ):
        raise ManifestValidationError(f"{label} must be a bounded, safe unit name (at most one '@instance')")


def _validate_abs_path(label: str, value: object) -> None:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{label} must be a string")
    if len(value) > _MAX_PATH_LEN or not value.startswith("/"):
        raise ManifestValidationError(f"{label} must be a bounded absolute path")
    parts = value.split("/")[1:]
    if not parts or parts[-1] == "":
        raise ManifestValidationError(f"{label} must not be the filesystem root or end in a slash")
    for part in parts:
        if part in (".", ".."):
            raise ManifestValidationError(f"{label} must not contain traversal segments")
        if not _PATH_COMPONENT_RE.fullmatch(part):
            raise ManifestValidationError(f"{label} contains unsafe characters")


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _read_all(fd: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ManifestValidationError("manifest exceeds maximum size")
        chunks.append(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Manifest data model.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TargetSpec:
    key: str
    account: str
    uid: int
    tmux_session: str
    user_unit: str
    foreground_exe_allowlist: tuple[str, ...]
    reset_failed_units: tuple[str, ...] = ()
    restart_units: tuple[str, ...] = ()
    lease_only: bool = False

    def __post_init__(self) -> None:
        _validate_identifier("target key", self.key)
        _validate_identifier("account", self.account)
        if not isinstance(self.uid, int) or isinstance(self.uid, bool) or self.uid < 0:
            raise ManifestValidationError("uid must be a non-negative integer")
        if not isinstance(self.lease_only, bool):
            raise ManifestValidationError("lease_only must be a boolean")
        if self.lease_only:
            # T-5124: a lease-only target (deepseek-fallback, T-4924) exists so a
            # lease on it can be taken and audited; nothing may act on it, so it
            # carries no session, unit, exe allowlist or unit lists.
            if (self.tmux_session or self.user_unit or self.foreground_exe_allowlist
                    or self.reset_failed_units or self.restart_units):
                raise ManifestValidationError("a lease_only target carries no session, unit or exe fields")
            return
        _validate_identifier("tmux_session", self.tmux_session)
        _validate_unit_identifier("user_unit", self.user_unit)
        if not self.foreground_exe_allowlist:
            raise ManifestValidationError("foreground_exe_allowlist must not be empty")
        for entry in self.foreground_exe_allowlist:
            _validate_abs_path("foreground_exe_allowlist entry", entry)
        if len(self.reset_failed_units) > MAX_RESET_FAILED_UNITS:
            raise ManifestValidationError("reset_failed_units is too long")
        if len(set(self.reset_failed_units)) != len(self.reset_failed_units):
            raise ManifestValidationError("reset_failed_units contains duplicates")
        for unit in self.reset_failed_units:
            _validate_unit_identifier("reset_failed_units entry", unit)
            if not unit.endswith(".service"):
                raise ManifestValidationError("reset_failed_units entries must be .service units")
        if len(self.restart_units) > MAX_RESTART_UNITS:
            raise ManifestValidationError("restart_units is too long")
        if len(set(self.restart_units)) != len(self.restart_units):
            raise ManifestValidationError("restart_units contains duplicates")
        for unit in self.restart_units:
            _validate_unit_identifier("restart_units entry", unit)
            if not unit.endswith(".service"):
                raise ManifestValidationError("restart_units entries must be .service units")
            if _restart_denied(unit, self.user_unit):
                raise ManifestValidationError(
                    f"restart_units entry {unit!r} owns the session or can launch a runtime")


# ---------------------------------------------------------------------------
# Strict JSON parsing: reject duplicate keys structurally.
# ---------------------------------------------------------------------------

class _DuplicateKeyError(ValueError):
    pass


def _no_dup_object_pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKeyError(f"duplicate key {key!r} in manifest JSON")
        seen[key] = value
    return seen


# Reject any literal control byte other than the JSON whitespace set
# (tab/newline/CR — all of which are themselves disallowed *inside* JSON
# strings by the JSON spec, so this only ever trims structural padding).
_CONTROL_CHAR_RE = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_TOP_LEVEL_KEYS = frozenset({"targets", "invokers"})
_TARGET_KEYS = frozenset({"account", "uid", "tmux_session", "user_unit", "foreground_exe_allowlist"})
_TARGET_OPTIONAL_KEYS = frozenset({"reset_failed_units", "restart_units", "lease_only"})  # T-5099, T-5100, T-5124
_LEASE_ONLY_TARGET_KEYS = frozenset({"account", "uid", "lease_only"})  # T-5124
_INVOKER_KEYS = frozenset({"verbs"})
_INVOKER_OPTIONAL_KEYS = frozenset({"precondition"})  # T-5124
_PRECONDITION_KINDS = frozenset({"higher-precedence-bad"})
_REPAIRER_KINDS = frozenset({"export-record", "watcher-heartbeat"})
MAX_PRECONDITION_REPAIRERS = 4
MAX_EVIDENCE_BYTES = 8 * 1024
EVIDENCE_FUTURE_SKEW_SECS = 60
# T-5124: the watcher-heartbeat self_state values that mean "this repairer is out".
HEARTBEAT_BAD_SELF_STATES = frozenset({"capped", "dark"})


@dataclass(frozen=True)
class InvokerSpec:
    """A manifest-listed account allowed to invoke the broker via sudo.

    `uid` is the getpwnam-resolved uid, or None when parsed structurally only
    (the sudoers generator, which never runs privileged)."""

    account: str
    uid: int | None
    verbs: frozenset[str]
    precondition: "Precondition | None" = None


@dataclass(frozen=True)
class BrokerManifest:
    targets: dict[str, TargetSpec]
    invokers: dict[str, InvokerSpec]


def _parse_manifest_json(raw: bytes) -> dict[str, object]:
    """Strict top-level parse. Returns the whole validated root object."""
    if not raw:
        raise ManifestValidationError("manifest is empty")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestValidationError("manifest exceeds maximum size")
    if _CONTROL_CHAR_RE.search(raw):
        raise ManifestValidationError("manifest contains disallowed control bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestValidationError("manifest is not valid utf-8") from exc
    try:
        obj = json.loads(text, object_pairs_hook=_no_dup_object_pairs_hook)
    except _DuplicateKeyError as exc:
        raise ManifestValidationError(str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ManifestValidationError("manifest root must be a JSON object")
    unknown = set(obj) - _TOP_LEVEL_KEYS
    if unknown:
        raise ManifestValidationError(f"manifest has unknown top-level fields: {sorted(unknown)}")
    missing = _TOP_LEVEL_KEYS - set(obj)
    if missing:
        raise ManifestValidationError(f"manifest is missing required fields: {sorted(missing)}")
    targets_obj = obj["targets"]
    if not isinstance(targets_obj, dict) or not targets_obj:
        raise ManifestValidationError("manifest 'targets' must be a non-empty object")
    invokers_obj = obj["invokers"]
    if not isinstance(invokers_obj, dict) or not invokers_obj:
        raise ManifestValidationError("manifest 'invokers' must be a non-empty object")
    return obj


def _check_fleet_uid(label: str, account: str, uid: int) -> None:
    if uid < MIN_FLEET_UID or uid > MAX_FLEET_UID:
        raise ManifestValidationError(f"{label} account {account!r} is a system/pseudo account (uid {uid})")


def _parse_invoker(
    account: object, raw_invoker: object, *, pwnam: Callable[[str], object] | None
) -> InvokerSpec:
    _validate_identifier("invoker account", account)  # also guarantees a str
    if account in NON_FLEET_ACCOUNT_NAMES:
        raise ManifestValidationError(f"invoker {account!r} is not a fleet persona and can never invoke the broker")
    if not isinstance(raw_invoker, dict):
        raise ManifestValidationError(f"invoker {account!r} must be a JSON object")
    unknown = set(raw_invoker) - _INVOKER_KEYS - _INVOKER_OPTIONAL_KEYS
    if unknown:
        raise ManifestValidationError(f"invoker {account!r} has unknown fields: {sorted(unknown)}")
    missing = _INVOKER_KEYS - set(raw_invoker)
    if missing:
        raise ManifestValidationError(f"invoker {account!r} is missing fields: {sorted(missing)}")
    verbs = raw_invoker["verbs"]
    if not isinstance(verbs, list) or not verbs or not all(isinstance(v, str) for v in verbs):
        raise ManifestValidationError(f"invoker {account!r} verbs must be a non-empty list of strings")
    if len(set(verbs)) != len(verbs):
        raise ManifestValidationError(f"invoker {account!r} verbs contain duplicates")
    bad = [v for v in verbs if v not in ENABLED_VERBS]
    if bad:
        raise ManifestValidationError(f"invoker {account!r} lists unknown verbs: {sorted(bad)}")

    uid: int | None = None
    if pwnam is not None:
        try:
            pw = pwnam(account)
        except KeyError as exc:
            raise ManifestValidationError(f"invoker {account!r} does not resolve via getpwnam") from exc
        _check_fleet_uid("invoker", account, pw.pw_uid)
        uid = pw.pw_uid
    precondition = None
    if "precondition" in raw_invoker:
        precondition = _parse_precondition(account, raw_invoker["precondition"], pwnam=pwnam)
    return InvokerSpec(account=account, uid=uid, verbs=frozenset(verbs), precondition=precondition)


def _resolve_target_uid(key: str, account: object, uid_field: object, *,
                        pwnam: Callable[[str], object] | None) -> int:
    if not isinstance(account, str):
        raise ManifestValidationError(f"target {key!r} has non-string fields")
    if not isinstance(uid_field, int) or isinstance(uid_field, bool):
        raise ManifestValidationError(f"target {key!r} uid must be an integer")
    if account in NON_FLEET_ACCOUNT_NAMES:
        raise ManifestValidationError(f"target {key!r} account {account!r} is not a fleet persona")
    if pwnam is None:
        _check_fleet_uid(f"target {key!r}", account, uid_field)
        return uid_field
    try:
        pw = pwnam(account)
    except KeyError as exc:
        raise ManifestValidationError(f"target {key!r} account {account!r} does not resolve via getpwnam") from exc
    _check_fleet_uid(f"target {key!r}", account, pw.pw_uid)
    if pw.pw_uid != uid_field:
        raise ManifestValidationError(
            f"target {key!r} manifest uid {uid_field} does not match resolved uid {pw.pw_uid}")
    return pw.pw_uid


def _parse_lease_only_target(key: str, raw_target: dict, *,
                             pwnam: Callable[[str], object] | None) -> TargetSpec:
    """T-5124: `{"account", "uid", "lease_only": true}` and nothing else."""
    extra = set(raw_target) - _LEASE_ONLY_TARGET_KEYS
    if extra:
        raise ManifestValidationError(f"lease_only target {key!r} may not carry {sorted(extra)}")
    missing = {"account", "uid"} - set(raw_target)
    if missing:
        raise ManifestValidationError(f"target {key!r} is missing fields: {sorted(missing)}")
    uid = _resolve_target_uid(key, raw_target["account"], raw_target["uid"], pwnam=pwnam)
    return TargetSpec(key=key, account=raw_target["account"], uid=uid, tmux_session="", user_unit="",
                      foreground_exe_allowlist=(), lease_only=True)


# ---------------------------------------------------------------------------
# T-5124 (T-4924 H3a): invoker precedence precondition.
# A lower-precedence repairer (account deepseek-fallback) may act only while
# every higher-precedence repairer (Recovery, Watchman) reads BAD from root-read
# evidence. Healthy or unknown refuses. Evidence files are written by those
# seats' own accounts, so each is opened O_NOFOLLOW and must be a regular file
# owned by its writer's uid, not group/other-writable, and size-capped; the
# lower-precedence seat cannot write any of them.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RepairerEvidence:
    kind: str                 # "export-record" | "watcher-heartbeat"
    path: str                 # export-record: the health DIR; watcher-heartbeat: the heartbeat FILE
    writer: str               # the account that writes it (expected st_uid)
    stale_s: int
    persona: str = ""         # export-record: whose record
    activity_stale_s: int = 0
    min_signals: int = 2


@dataclass(frozen=True)
class Precondition:
    kind: str
    repairers: tuple[RepairerEvidence, ...]


def _bounded_positive_int(label: str, value: object, *, maximum: int = 7 * 24 * 3600) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= maximum:
        raise ManifestValidationError(f"{label} must be a positive integer <= {maximum}")
    return value


def _parse_precondition(account: str, raw: object, *, pwnam: Callable[[str], object] | None) -> Precondition:
    label = f"invoker {account!r} precondition"
    if not isinstance(raw, dict) or set(raw) != {"kind", "repairers"}:
        raise ManifestValidationError(f"{label} must be {{kind, repairers}}")
    if raw["kind"] not in _PRECONDITION_KINDS:
        raise ManifestValidationError(f"{label} kind must be one of {sorted(_PRECONDITION_KINDS)}")
    reps = raw["repairers"]
    if not isinstance(reps, list) or not 0 < len(reps) <= MAX_PRECONDITION_REPAIRERS:
        raise ManifestValidationError(f"{label} repairers must be a non-empty list (max {MAX_PRECONDITION_REPAIRERS})")
    out = []
    for i, r in enumerate(reps):
        rl = f"{label} repairer {i}"
        if not isinstance(r, dict) or r.get("kind") not in _REPAIRER_KINDS:
            raise ManifestValidationError(f"{rl} kind must be one of {sorted(_REPAIRER_KINDS)}")
        allowed = ({"kind", "path", "writer", "stale_s", "persona", "activity_stale_s", "min_signals"}
                   if r["kind"] == "export-record" else {"kind", "path", "writer", "stale_s"})
        required = ({"kind", "path", "writer", "stale_s", "persona", "activity_stale_s"}
                    if r["kind"] == "export-record" else allowed)
        if set(r) - allowed or required - set(r):
            raise ManifestValidationError(f"{rl} fields must be {sorted(required)} (+ optional {sorted(allowed - required)})")
        _validate_abs_path(f"{rl} path", r["path"])
        _validate_identifier(f"{rl} writer", r["writer"])
        if r["writer"] == account or r["writer"] in NON_FLEET_ACCOUNT_NAMES:
            raise ManifestValidationError(f"{rl} writer must be another fleet account")
        if pwnam is not None:
            try:
                pwnam(r["writer"])
            except KeyError as exc:
                raise ManifestValidationError(f"{rl} writer {r['writer']!r} does not resolve") from exc
        kw = dict(kind=r["kind"], path=r["path"], writer=r["writer"],
                  stale_s=_bounded_positive_int(f"{rl} stale_s", r["stale_s"]))
        if r["kind"] == "export-record":
            _validate_identifier(f"{rl} persona", r["persona"])
            kw.update(persona=r["persona"],
                      activity_stale_s=_bounded_positive_int(f"{rl} activity_stale_s", r["activity_stale_s"]),
                      min_signals=_bounded_positive_int(f"{rl} min_signals", r.get("min_signals", 2), maximum=3))
        out.append(RepairerEvidence(**kw))
    return Precondition(kind=raw["kind"], repairers=tuple(out))


def _read_evidence(path: str, *, writer_uid: int) -> dict | None:
    """One evidence file as a dict, or None (unknown) on ANY doubt."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != writer_uid
                or st.st_mode & 0o022 or st.st_size > MAX_EVIDENCE_BYTES):
            return None
        raw = os.read(fd, MAX_EVIDENCE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > MAX_EVIDENCE_BYTES:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        # T-5143: deep nesting can raise RecursionError (pure-Python decoder, or a lower C
        # recursion limit). That would escape check_precondition and crash the broker past
        # its refuse path, so any parse failure is unknown evidence, which refuses.
        return None
    return obj if isinstance(obj, dict) and obj.get("schema_version") == 1 else None


def _epoch(obj: dict, key: str, now: float) -> int | None:
    v = obj.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or v - now > EVIDENCE_FUTURE_SKEW_SECS:
        return None
    return v


def _repairer_reading(ev: RepairerEvidence, *, now: float,
                      pwnam: Callable[[str], object]) -> tuple[str, int | str | None]:
    """-> (state, snapshot). snapshot identifies the evidence a BAD verdict came
    from: the writer's own timestamp for a fresh BAD, STALE_SNAPSHOT for a
    stale one, None otherwise (T-5129 debounce)."""
    try:
        writer_uid = pwnam(ev.writer).pw_uid
    except KeyError:
        return "unknown", None
    if ev.kind == "watcher-heartbeat":
        hb = _read_evidence(ev.path, writer_uid=writer_uid)
        if hb is None:
            return "unknown", None
        written = _epoch(hb, "written_at", now)
        if written is None:
            return "unknown", None
        if now - written > ev.stale_s:
            return "bad", STALE_SNAPSHOT
        state = hb.get("self_state")
        if state in HEARTBEAT_BAD_SELF_STATES:
            return "bad", written
        return ("healthy" if state == "ok" else "unknown"), None
    # export-record: the exporter heartbeat, then the persona's record.
    exp = _read_evidence(os.path.join(ev.path, "_exporter.json"), writer_uid=writer_uid)
    if exp is None:
        return "unknown", None
    exported = _epoch(exp, "exported_at", now)
    if exported is None:
        return "unknown", None
    if now - exported > ev.stale_s:
        return "bad", STALE_SNAPSHOT   # the writer's account/exporter is down
    rec = _read_evidence(os.path.join(ev.path, f"{ev.persona}.json"), writer_uid=writer_uid)
    if rec is None or rec.get("persona") != ev.persona:
        return "unknown", None
    rec_at = _epoch(rec, "exported_at", now)
    if rec_at is None or now - rec_at > ev.stale_s:
        return "unknown", None
    activity = rec.get("activity_age_s")
    signals = (
        int(isinstance(activity, int) and not isinstance(activity, bool) and activity > ev.activity_stale_s)
        + int(rec.get("pane_state") == "modal")
        + int(rec.get("consume_unit") == "failed")
    )
    return ("bad", rec_at) if signals >= ev.min_signals else ("healthy", None)


def repairer_state(ev: RepairerEvidence, *, now: float, pwnam: Callable[[str], object]) -> str:
    """'bad' | 'healthy' | 'unknown' for one higher-precedence repairer (one read,
    not debounced; the gates use check_precondition_debounced)."""
    return _repairer_reading(ev, now=now, pwnam=pwnam)[0]


def check_precondition(pre: Precondition | None, *, now: float,
                       pwnam: Callable[[str], object]) -> tuple[bool, list[str]]:
    """-> (allowed, ['<writer>=<state>', ...]). No precondition allows."""
    if pre is None:
        return True, []
    states = [f"{ev.writer}={repairer_state(ev, now=now, pwnam=pwnam)}" for ev in pre.repairers]
    return all(s.endswith("=bad") for s in states), states


def _repairer_key(ev: RepairerEvidence) -> str:
    # Hashed so four repairers with 4 KiB paths still fit the state cap; a
    # manifest change to a repairer yields a new key, i.e. a fresh streak.
    ident = f"{ev.kind}|{ev.writer}|{ev.path}|{ev.persona}".encode("utf-8")
    return hashlib.sha256(ident).hexdigest()[:32]


def _valid_streak(entry: object) -> bool:
    if (not isinstance(entry, dict)
            or set(entry) != {"first_at", "first_snap", "seen_at", "confirmed"}
            or not isinstance(entry["confirmed"], bool)):
        return False
    ints = [entry["first_at"], entry["seen_at"]]
    snap = entry["first_snap"]
    if snap != STALE_SNAPSHOT:
        ints.append(snap)
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in ints):
        return False
    # Ordering: a run cannot have begun after its latest read ('confirmed' is
    # a bool and has no ordering of its own).
    return entry["first_at"] <= entry["seen_at"]


def check_precondition_debounced(pre: Precondition | None, *, invoker_user: str, now: float,
                                 pwnam: Callable[[str], object],
                                 lease_store: "LeaseStore") -> tuple[bool, list[str]]:
    """T-5129: the precondition with a two-read debounce per repairer.

    A BAD read CONFIRMS an unconfirmed run (by this invoker, clock not gone
    backwards) when the run began at least PRECONDITION_DEBOUNCE_MIN_SECS and
    at most PRECONDITION_DEBOUNCE_CONFIRM_GAP_SECS ago and this read saw a
    different evidence snapshot than the one that began it (stale evidence
    cannot advance, so stale-then-stale counts). A confirmed run stays `bad`
    while each BAD read follows the previous within
    PRECONDITION_DEBOUNCE_CONFIRMED_GAP_SECS. Otherwise a BAD read reports
    `bad-unconfirmed` and starts (or keeps) a run; HEALTHY or UNKNOWN clears
    it. The state
    is rewritten on every call, refusals included, so the second read can see
    the first. No precondition: (True, []) with no state read or written.
    Raises LeaseStateError/OSError on a state-store doubt (callers refuse)."""
    if pre is None:
        return True, []
    t = int(now)
    readings = [(ev, *_repairer_reading(ev, now=now, pwnam=pwnam)) for ev in pre.repairers]
    with lease_store.precondition_state(invoker_user) as handle:
        prev = handle.read()
        nxt: dict[str, dict] = {}
        states = []
        for ev, state, snap in readings:
            key = _repairer_key(ev)
            if state != "bad":
                states.append(f"{ev.writer}={state}")
                continue
            old = prev.get(key)
            ok_old = _valid_streak(old) and old["seen_at"] <= t
            if ok_old and old["confirmed"] and t - old["seen_at"] <= PRECONDITION_DEBOUNCE_CONFIRMED_GAP_SECS:
                entry = dict(old, seen_at=t)          # a confirmed run holds
            elif ok_old and not old["confirmed"] and t - old["first_at"] <= PRECONDITION_DEBOUNCE_CONFIRM_GAP_SECS:
                entry = dict(old, seen_at=t)          # an unconfirmed run, still confirmable
                entry["confirmed"] = (t - old["first_at"] >= PRECONDITION_DEBOUNCE_MIN_SECS
                                      and (snap == STALE_SNAPSHOT or snap != old["first_snap"]))
            else:
                entry = {"first_at": t, "first_snap": snap, "seen_at": t, "confirmed": False}
            nxt[key] = entry
            confirmed = entry["confirmed"]
            states.append(f"{ev.writer}={'bad' if confirmed else 'bad-unconfirmed'}")
        handle.write(nxt)
    return all(s.endswith("=bad") for s in states), states


def _parse_target(key: str, raw_target: object, *, pwnam: Callable[[str], object] | None) -> TargetSpec:
    if not isinstance(raw_target, dict):
        raise ManifestValidationError(f"target {key!r} must be a JSON object")
    unknown = set(raw_target) - _TARGET_KEYS - _TARGET_OPTIONAL_KEYS
    if unknown:
        raise ManifestValidationError(f"target {key!r} has unknown fields: {sorted(unknown)}")
    lease_only = raw_target.get("lease_only", False)
    if not isinstance(lease_only, bool):
        raise ManifestValidationError(f"target {key!r} lease_only must be a boolean")
    if lease_only:
        return _parse_lease_only_target(key, raw_target, pwnam=pwnam)
    missing = _TARGET_KEYS - set(raw_target)
    if missing:
        raise ManifestValidationError(f"target {key!r} is missing fields: {sorted(missing)}")

    account = raw_target["account"]
    uid_field = raw_target["uid"]
    tmux_session = raw_target["tmux_session"]
    user_unit = raw_target["user_unit"]
    allowlist = raw_target["foreground_exe_allowlist"]
    reset_units = raw_target.get("reset_failed_units", [])
    if not isinstance(reset_units, list) or not all(isinstance(u, str) for u in reset_units):
        raise ManifestValidationError(f"target {key!r} reset_failed_units must be a list of strings")
    restart_units = raw_target.get("restart_units", [])
    if not isinstance(restart_units, list) or not all(isinstance(u, str) for u in restart_units):
        raise ManifestValidationError(f"target {key!r} restart_units must be a list of strings")

    if not isinstance(account, str) or not isinstance(tmux_session, str) or not isinstance(user_unit, str):
        raise ManifestValidationError(f"target {key!r} has non-string fields")
    if not isinstance(uid_field, int) or isinstance(uid_field, bool):
        raise ManifestValidationError(f"target {key!r} uid must be an integer")
    if not isinstance(allowlist, list) or not allowlist or not all(isinstance(p, str) for p in allowlist):
        raise ManifestValidationError(f"target {key!r} foreground_exe_allowlist must be a non-empty list of strings")

    if account in NON_FLEET_ACCOUNT_NAMES:
        raise ManifestValidationError(f"target {key!r} account {account!r} is not a fleet persona")

    if pwnam is None:
        # Structural-only parse (sudoers generator): the declared uid must at
        # least be in the fleet range; the broker re-resolves at run time.
        _check_fleet_uid(f"target {key!r}", account, uid_field)
        resolved_uid = uid_field
    else:
        try:
            pw = pwnam(account)
        except KeyError as exc:
            raise ManifestValidationError(
                f"target {key!r} account {account!r} does not resolve via getpwnam"
            ) from exc

        _check_fleet_uid(f"target {key!r}", account, pw.pw_uid)
        if pw.pw_uid != uid_field:
            raise ManifestValidationError(
                f"target {key!r} manifest uid {uid_field} does not match resolved uid {pw.pw_uid}"
            )
        resolved_uid = pw.pw_uid

    return TargetSpec(
        key=key,
        account=account,
        uid=resolved_uid,
        tmux_session=tmux_session,
        user_unit=user_unit,
        foreground_exe_allowlist=tuple(allowlist),
        reset_failed_units=tuple(reset_units),
        restart_units=tuple(restart_units),
    )


# ---------------------------------------------------------------------------
# Open-time validation: O_NOFOLLOW + fstat-after-open, never stat-by-path.
# Mirrors tools/credential_broker.py's single-validated-parent pattern.
# ---------------------------------------------------------------------------

def _open_flags(extra: int = 0) -> int:
    flags = os.O_RDONLY | extra
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _open_validated_parent(parent_dir: Path, *, expected_owner_uid: int, expected_mode: int) -> int:
    flags = _open_flags(os.O_DIRECTORY if hasattr(os, "O_DIRECTORY") else 0)
    try:
        fd = os.open(parent_dir, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ManifestValidationError("manifest parent directory must not be a symlink") from exc
        raise ManifestValidationError(f"cannot open manifest parent directory: {exc}") from exc
    try:
        st = os.fstat(fd)
        errors = []
        if not stat.S_ISDIR(st.st_mode):
            errors.append("manifest parent is not a directory")
        if st.st_uid != expected_owner_uid:
            errors.append("manifest parent is not owned by the expected owner")
        if stat.S_IMODE(st.st_mode) != expected_mode:
            errors.append("manifest parent mode does not match the expected mode")
        if errors:
            raise ManifestValidationError("; ".join(errors))
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_validated_leaf(parent_fd: int, name: str, *, expected_owner_uid: int, expected_mode: int) -> int:
    try:
        fd = os.open(name, _open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ManifestValidationError("manifest file must not be a symlink") from exc
        raise ManifestValidationError(f"cannot open manifest file: {exc}") from exc
    try:
        st = os.fstat(fd)
        errors = []
        if not stat.S_ISREG(st.st_mode):
            errors.append("manifest is not a regular file")
        if st.st_uid != expected_owner_uid:
            errors.append("manifest is not owned by the expected owner")
        mode = stat.S_IMODE(st.st_mode)
        if mode != expected_mode:
            errors.append("manifest mode does not match the expected mode")
        if errors:
            raise ManifestValidationError("; ".join(errors))
        return fd
    except Exception:
        os.close(fd)
        raise


def parse_manifest_bytes(raw: bytes, *, pwnam: Callable[[str], object] | None) -> BrokerManifest:
    """Strict content parse of manifest bytes (no file/ownership checks).

    `pwnam=None` is a STRUCTURAL-only parse for the unprivileged sudoers
    generator; the broker itself always passes a real resolver."""
    obj = _parse_manifest_json(raw)
    targets: dict[str, TargetSpec] = {}
    for key, raw_target in obj["targets"].items():  # type: ignore[union-attr]
        _validate_identifier("target key", key)
        targets[key] = _parse_target(key, raw_target, pwnam=pwnam)
    invokers: dict[str, InvokerSpec] = {}
    for account, raw_invoker in obj["invokers"].items():  # type: ignore[union-attr]
        invokers[account] = _parse_invoker(account, raw_invoker, pwnam=pwnam)
    return BrokerManifest(targets=targets, invokers=invokers)


def load_manifest(path: Path, **kwargs) -> dict[str, TargetSpec]:
    """Back-compat view: the validated targets of load_broker_manifest()."""
    return load_broker_manifest(path, **kwargs).targets


def load_broker_manifest(
    path: Path,
    *,
    expected_owner_uid: int = MANIFEST_EXPECTED_OWNER_UID,
    expected_mode: int = MANIFEST_EXPECTED_MODE,
    expected_parent_mode: int = MANIFEST_PARENT_EXPECTED_MODE,
    pwnam: Callable[[str], object] = pwd.getpwnam,
) -> BrokerManifest:
    """Load + strictly validate the manifest. Fresh every call — see the
    module docstring's revalidate-before-act note; there is no cache."""
    if pwnam is None:
        raise ManifestValidationError("the broker manifest load always resolves accounts")
    path = Path(path)
    if not path.is_absolute():
        raise ManifestValidationError("manifest path must be absolute")
    if path.name in ("", ".", ".."):
        raise ManifestValidationError("manifest path must name a regular file")

    parent_fd = _open_validated_parent(
        path.parent, expected_owner_uid=expected_owner_uid, expected_mode=expected_parent_mode
    )
    try:
        leaf_fd = _open_validated_leaf(
            parent_fd, path.name, expected_owner_uid=expected_owner_uid, expected_mode=expected_mode
        )
    finally:
        os.close(parent_fd)
    try:
        raw = _read_all(leaf_fd, MAX_MANIFEST_BYTES)
    finally:
        os.close(leaf_fd)

    return parse_manifest_bytes(raw, pwnam=pwnam)


# ---------------------------------------------------------------------------
# unstick: foreground-process-group validation.
#
# Split per the tools/_pane_predicates.py convention: gathering (I/O, real
# tmux + /proc reads, best-effort — proven live at the Recovery-supervised
# increment-2 first-fire, NOT here) vs. deciding (pure, exhaustively tested).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForegroundSnapshot:
    """Facts about the pane's foreground process, already gathered."""

    pid: int
    pgid: int
    exe_path: str          # os.readlink(/proc/<pid>/exe) — already resolved
    exe_owner_uid: int
    exe_mode: int           # stat.S_IMODE of the exe file
    cmdline: tuple[str, ...]
    sid: int
    pane_sid: int            # the pane's own (session-leader) sid, for cross-check
    tty_matches_pane: bool


def _validate_foreground_snapshot(
    snapshot: ForegroundSnapshot,
    target: TargetSpec,
    *,
    deploy_owner_uids: frozenset[int] = DEFAULT_DEPLOY_OWNER_UIDS,
) -> None:
    """Raise DispatchRefusedError unless the pane's foreground process GROUP
    (never a descendant) is an exact, safely-owned, allowlisted wrapper."""
    if snapshot.pid != snapshot.pgid:
        raise DispatchRefusedError(
            "foreground pid is not its own process-group leader (a descendant, not the group)"
        )
    basename = os.path.basename(snapshot.exe_path)
    if basename in DENYLIST_FOREGROUND_BASENAMES:
        raise DispatchRefusedError(f"foreground process is denylisted: {basename}")
    if snapshot.exe_path not in target.foreground_exe_allowlist:
        raise DispatchRefusedError("foreground exe is not on the target's allowlist")
    if snapshot.exe_owner_uid not in deploy_owner_uids:
        raise DispatchRefusedError("foreground exe is not root/deploy-owned")
    if snapshot.exe_owner_uid == target.uid:
        raise DispatchRefusedError("foreground exe must not be owned by the target account")
    if snapshot.exe_mode & 0o022:
        raise DispatchRefusedError("foreground exe is group- or other-writable")
    if snapshot.sid != snapshot.pane_sid:
        raise DispatchRefusedError("foreground process session id does not match the pane session")
    if not snapshot.tty_matches_pane:
        raise DispatchRefusedError("foreground process controlling tty does not match the pane tty")


def _tty_match(foreground_tty_dev: int, pane_tty_dev: int) -> bool:
    """Compare two raw dev_t values by (major, minor), not by raw integer —
    the /proc/<pid>/stat tty_nr encoding and a stat()'d st_rdev use the same
    glibc dev_t convention, which os.major/os.minor decode correctly."""
    return (os.major(foreground_tty_dev), os.minor(foreground_tty_dev)) == (
        os.major(pane_tty_dev),
        os.minor(pane_tty_dev),
    )


class PaneForegroundProbe:
    """Best-effort gatherer of pane-foreground facts via tmux + /proc.

    Live fidelity against a real cross-account pane is proven at the
    Recovery-supervised increment-2 first-fire (ADR ORG-048 install-gate item 3),
    not here — this increment wires the structure and the (exhaustively
    tested) decision predicate above. Never invoked by the test suite against
    a real cross-account process; tests pass synthetic ForegroundSnapshot
    values straight to the predicate.
    """

    def __init__(
        self,
        *,
        tmux_bin: str = TMUX,
        proc_root: Path = Path("/proc"),
        runner: Callable[..., object] | None = None,
        pwnam: Callable[[str], object] = pwd.getpwnam,
    ):
        self._tmux_bin = tmux_bin
        self._proc_root = proc_root
        self._runner = runner
        self._pwnam = pwnam

    def snapshot(self, target: TargetSpec) -> ForegroundSnapshot:
        pane_pid, pane_tty = self._pane_facts(target)
        pane_fields = self._read_stat_fields(pane_pid)
        fg_pid = pane_fields["tpgid"]
        fg_fields = pane_fields if fg_pid == pane_pid else self._read_stat_fields(fg_pid)

        exe_path = os.readlink(self._proc_root / str(fg_pid) / "exe")
        exe_st = os.stat(exe_path)
        pane_dev = os.stat(pane_tty).st_rdev

        return ForegroundSnapshot(
            pid=fg_pid,
            pgid=fg_fields["pgrp"],
            exe_path=exe_path,
            exe_owner_uid=exe_st.st_uid,
            exe_mode=stat.S_IMODE(exe_st.st_mode),
            cmdline=self._read_cmdline(fg_pid),
            sid=fg_fields["session"],
            pane_sid=pane_fields["session"],
            tty_matches_pane=_tty_match(fg_fields["tty_nr"], pane_dev),
        )

    def _pane_facts(self, target: TargetSpec) -> tuple[int, str]:
        # The broker runs as root, so a bare tmux would query root's own
        # server (/tmp/tmux-0) and never see the target's pane (T-5046).
        # Drop to the target through the same setpriv transition the key
        # injection uses: tmux then resolves the target's default socket and
        # its own ownership check refuses a socket the target does not own.
        uid, gid = _revalidated_uid_gid(target, pwnam=self._pwnam)
        argv = _setpriv_prefix(uid, gid) + [
            self._tmux_bin, "display-message", "-p", "-t", target.tmux_session,
            # Space, not tab: display-message -p rewrites control chars to
            # "_", so a tab separator never survives the live call (T-5046).
            "#{pane_pid} #{pane_tty}",
        ]
        runner = self._runner or _real_runner
        out = runner(argv, env=_build_env(target, pwnam=self._pwnam), timeout=SUBPROCESS_TIMEOUT_SECS)
        if out.returncode != 0:
            raise subprocess.CalledProcessError(out.returncode, argv, out.stdout, out.stderr)
        pid_str, tty_path = out.stdout.strip().split(" ", 1)
        return int(pid_str), tty_path

    def _read_stat_fields(self, pid: int) -> dict[str, int]:
        text = (self._proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        # comm (field 2) is parenthesized and may itself contain ')' or
        # whitespace, so split on the LAST ')' to safely reach field 3+.
        after_comm = text.rsplit(")", 1)[1].split()
        # after_comm[0] is field 3 (state); fields below are 1-indexed from there.
        return {
            "ppid": int(after_comm[1]),     # field 4
            "pgrp": int(after_comm[2]),     # field 5
            "session": int(after_comm[3]),  # field 6
            "tty_nr": int(after_comm[4]),   # field 7
            "tpgid": int(after_comm[5]),    # field 8
        }

    def _read_cmdline(self, pid: int) -> tuple[str, ...]:
        raw = (self._proc_root / str(pid) / "cmdline").read_bytes()
        return tuple(p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p)


# ---------------------------------------------------------------------------
# Root -> target transition + argv construction (contract item 1).
# ---------------------------------------------------------------------------

def _resolve_account(account: str, *, pwnam: Callable[[str], object]):
    return pwnam(account)


def _build_env(target: TargetSpec, *, pwnam: Callable[[str], object] = pwd.getpwnam) -> dict[str, str]:
    pw = _resolve_account(target.account, pwnam=pwnam)
    return {
        "HOME": pw.pw_dir,
        "USER": target.account,
        "LOGNAME": target.account,
        "SHELL": pw.pw_shell or "/bin/bash",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "XDG_RUNTIME_DIR": f"/run/user/{pw.pw_uid}",
    }


def _setpriv_prefix(uid: int, gid: int) -> list[str]:
    return [
        SETPRIV,
        "--reuid", str(uid),
        "--regid", str(gid),
        "--init-groups",
        "--reset-env",
        "--no-new-privs",
        "--",
    ]


def _revalidated_uid_gid(target: TargetSpec, *, pwnam: Callable[[str], object]) -> tuple[int, int]:
    pw = _resolve_account(target.account, pwnam=pwnam)
    if pw.pw_uid != target.uid:
        raise DispatchRefusedError(
            f"account {target.account!r} uid changed since manifest load — refusing"
        )
    return pw.pw_uid, pw.pw_gid


def build_kill_session_argv(target: TargetSpec, *, pwnam: Callable[[str], object] = pwd.getpwnam) -> list[str]:
    uid, gid = _revalidated_uid_gid(target, pwnam=pwnam)
    return _setpriv_prefix(uid, gid) + [TMUX, "kill-session", "-t", target.tmux_session]


def build_linger_enable_argv(
    target: TargetSpec, *, pwnam: Callable[[str], object] = pwd.getpwnam
) -> list[str]:
    """Fixed argv; the only variable is the manifest account, re-resolved so a
    uid drift since manifest load refuses. Runs as root (no setpriv drop)."""
    _revalidated_uid_gid(target, pwnam=pwnam)
    return [LOGINCTL, "enable-linger", target.account]


def build_reset_failed_argv(
    target: TargetSpec, unit: object, *, pwnam: Callable[[str], object] = pwd.getpwnam
) -> list[str]:
    """T-5099: `systemctl --user reset-failed -- <unit>` as the target. The unit
    must be listed verbatim in the target's manifest reset_failed_units; the
    XDG_RUNTIME_DIR comes from the re-resolved uid, never from argv."""
    if not isinstance(unit, str) or unit not in target.reset_failed_units:
        raise DispatchRefusedError(f"unit not in the target's reset_failed_units: {unit!r}")
    uid, gid = _revalidated_uid_gid(target, pwnam=pwnam)
    return _setpriv_prefix(uid, gid) + [
        ENV_BIN, f"XDG_RUNTIME_DIR=/run/user/{uid}",
        SYSTEMCTL, "--user", "reset-failed", "--", unit,
    ]


def _with_service_suffix(unit: str) -> str:
    """systemd reads a suffixless name as `<name>.service`; normalise so an exact
    match can't be dodged by the suffix (T-5112)."""
    return unit if "." in unit.rsplit("@", 1)[-1] else f"{unit}.service"


def _restart_denied(unit: str, user_unit: str) -> bool:
    """T-5100/T-5112: True if restarting `unit` could end, launch or resume a session."""
    return (_with_service_suffix(unit) == _with_service_suffix(user_unit)
            or unit.startswith(RESTART_DENY_PREFIXES))


def build_restart_argv(
    target: TargetSpec, unit: object, *, pwnam: Callable[[str], object] = pwd.getpwnam
) -> list[str]:
    """T-5100: `systemctl --user restart -- <unit>` as the target. The unit must be
    listed verbatim in restart_units AND pass the session-owner denylist again
    (defence in depth against a manifest parsed by an older validator)."""
    if not isinstance(unit, str) or unit not in target.restart_units:
        raise DispatchRefusedError(f"unit not in the target's restart_units: {unit!r}")
    if _restart_denied(unit, target.user_unit):
        raise DispatchRefusedError(f"unit owns the session or can launch a runtime: {unit!r}")
    uid, gid = _revalidated_uid_gid(target, pwnam=pwnam)
    return _setpriv_prefix(uid, gid) + [
        ENV_BIN, f"XDG_RUNTIME_DIR=/run/user/{uid}",
        SYSTEMCTL, "--user", "restart", "--", unit,
    ]


def real_linger_probe(account: str, *, state_dir: Path = LINGER_STATE_DIR) -> bool:
    """True iff logind's linger file for `account` exists as a regular file.
    lstat, so a symlink planted there does not count."""
    try:
        st = os.lstat(state_dir / account)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode)


def _root_env() -> dict[str, str]:
    return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


def build_unstick_argv(
    target: TargetSpec, action: str, *, pwnam: Callable[[str], object] = pwd.getpwnam
) -> list[str]:
    if action not in UNSTICK_KEY_SEQUENCES:
        raise DispatchRefusedError(f"unknown unstick action: {action!r}")
    uid, gid = _revalidated_uid_gid(target, pwnam=pwnam)
    keys = UNSTICK_KEY_SEQUENCES[action]
    return _setpriv_prefix(uid, gid) + [TMUX, "send-keys", "-t", target.tmux_session, *keys]


# ---------------------------------------------------------------------------
# Audit — append-only, flock-serialized, fail-closed for the action it gates.
# Never logs pane content (contract §Audit).
# ---------------------------------------------------------------------------

class AuditSink:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.monotonic):
        self.path = Path(path)
        self._clock = clock

    def write(self, **fields: object) -> None:
        forbidden = _AUDIT_FORBIDDEN_KEYS & set(fields)
        if forbidden:
            raise ValueError(f"audit record must not carry pane/output content: {sorted(forbidden)}")
        record = {"monotonic_ts": self._clock(), **fields}
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise AuditWriteError(f"cannot prepare audit directory: {exc}") from exc
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise AuditWriteError(f"cannot open audit log: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = (json.dumps(record, sort_keys=True, default=str) + "\n").encode("utf-8")
            _write_all(fd, payload)
            os.fsync(fd)
        except OSError as exc:
            raise AuditWriteError(f"cannot write audit log: {exc}") from exc
        finally:
            os.close(fd)


class LeaseExportError(CodexRecoveryError):
    """The readable lease-event export could not be written (never fatal)."""


class LeaseExportSink:
    """Best-effort, readable lease-event export (T-4884 B4b).

    One JSON line per lease transition, enum/int fields only:
    {op, target, holder_user, holder_uid, ts_wall, ts_mono, expires_at_wall}.
    The root-only AuditSink stays authoritative and fail-closed; this sink
    never raises out of write() and never blocks an action. The directory is
    root-provisioned (never created here); the directory and file are opened
    O_NOFOLLOW and validated by fstat (owner, group, exact mode, regular file,
    single link) before any byte is appended."""

    _KEYS = ("op", "target", "holder_user", "holder_uid", "ts_wall", "ts_mono", "expires_at_wall")

    def __init__(
        self,
        path: Path,
        *,
        expected_owner_uid: int = MANIFEST_EXPECTED_OWNER_UID,
        group_gid: int | None = None,
        mono: Callable[[], float] = time.monotonic,
    ):
        self.path = Path(path)
        self.expected_owner_uid = expected_owner_uid
        self._group_gid = group_gid
        self._mono = mono

    def _gid(self) -> int:
        if self._group_gid is not None:
            return self._group_gid
        try:
            return grp.getgrnam(LEASE_EXPORT_GROUP).gr_gid
        except KeyError as exc:
            raise LeaseExportError(f"group {LEASE_EXPORT_GROUP} does not exist") from exc

    def _check(self, fd: int, label: str, *, gid: int, mode: int, is_dir: bool) -> None:
        st = os.fstat(fd)
        if (stat.S_ISDIR(st.st_mode) if is_dir else stat.S_ISREG(st.st_mode)) is False:
            raise LeaseExportError(f"{label} has the wrong file type")
        if st.st_uid != self.expected_owner_uid or st.st_gid != gid:
            raise LeaseExportError(f"{label} has the wrong owner or group")
        if stat.S_IMODE(st.st_mode) != mode:
            raise LeaseExportError(f"{label} mode {oct(stat.S_IMODE(st.st_mode))} is not {oct(mode)}")
        if not is_dir and st.st_nlink != 1:
            raise LeaseExportError(f"{label} has extra hard links")

    def _append(self, payload: bytes) -> None:
        gid = self._gid()
        try:
            dfd = os.open(self.path.parent, _open_flags(os.O_DIRECTORY))
        except OSError as exc:
            raise LeaseExportError(f"cannot open export dir: {exc}") from exc
        try:
            self._check(dfd, "export dir", gid=gid, mode=LEASE_EXPORT_DIR_MODE, is_dir=True)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                fd = os.open(self.path.name, flags, LEASE_EXPORT_FILE_MODE, dir_fd=dfd)
            except OSError as exc:
                raise LeaseExportError(f"cannot open export log: {exc}") from exc
            try:
                st = os.fstat(fd)
                if st.st_size == 0 and st.st_nlink == 1 and stat.S_ISREG(st.st_mode):
                    # Fresh (or empty) file: pin group and mode umask-proof.
                    if st.st_gid != gid:
                        os.fchown(fd, -1, gid)
                    os.fchmod(fd, LEASE_EXPORT_FILE_MODE)
                self._check(fd, "export log", gid=gid, mode=LEASE_EXPORT_FILE_MODE, is_dir=False)
                fcntl.flock(fd, fcntl.LOCK_EX)
                _write_all(fd, payload)
            finally:
                os.close(fd)
        except OSError as exc:
            raise LeaseExportError(f"cannot write export log: {exc}") from exc
        finally:
            os.close(dfd)

    def write(self, *, op: str, target: str, holder_user: str, holder_uid: int,
              ts_wall: float, expires_at_wall: int) -> bool:
        """Append one event; True on success. Never raises."""
        try:
            if op not in LEASE_EXPORT_OPS:
                raise LeaseExportError(f"unknown export op {op!r}")
            _validate_identifier("target", target)
            _validate_identifier("holder_user", holder_user)
            record = {
                "op": op, "target": target, "holder_user": holder_user,
                "holder_uid": int(holder_uid), "ts_wall": int(ts_wall),
                "ts_mono": int(self._mono()), "expires_at_wall": int(expires_at_wall),
            }
            payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
            self._append(payload)
            return True
        except Exception as exc:  # noqa: BLE001 - best-effort by contract
            print(f"codex-recovery: lease export write failed (action not affected): "
                  f"{exc.__class__.__name__}", file=sys.stderr)
            return False


def _export(export: "LeaseExportSink | None", op: str, target: str, record: "LeaseRecord", now: float) -> None:
    if export is not None:
        export.write(op=op, target=target, holder_user=record.holder_user, holder_uid=record.holder_uid,
                     ts_wall=now, expires_at_wall=record.expires_at)


def _best_effort_audit(audit: "AuditSink | Callable", **fields: object) -> None:
    """For refusal paths: no action is happening either way, so an audit
    write failure here doesn't change the outcome — attempt it, don't crash."""
    try:
        audit.write(**fields)
    except AuditWriteError as exc:
        print(f"codex-recovery: audit write failed (refusal already applied): {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Rate limiting — flock-serialized sliding window, root-owned runtime dir.
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(
        self,
        state_dir: Path,
        *,
        window_secs: int = RATE_LIMIT_WINDOW_SECS,
        max_actions: int = RATE_LIMIT_MAX_ACTIONS,
        clock: Callable[[], float] = time.time,
    ):
        self.state_dir = Path(state_dir)
        self.window_secs = window_secs
        self.max_actions = max_actions
        self._clock = clock

    def check_and_record(self, key: str) -> bool:
        _validate_identifier("rate-limit key", key)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_path = self.state_dir / f"{key}.json"
        fd = os.open(state_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = _read_all(fd, 1_000_000)
            try:
                history = json.loads(raw.decode("utf-8")) if raw else []
            except (json.JSONDecodeError, UnicodeDecodeError):
                history = []
            if not isinstance(history, list):
                history = []
            now = self._clock()
            history = [t for t in history if isinstance(t, (int, float)) and now - t < self.window_secs]
            if len(history) >= self.max_actions:
                return False
            history.append(now)
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            _write_all(fd, json.dumps(history).encode("utf-8"))
            os.fsync(fd)
            return True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


# ---------------------------------------------------------------------------
# Invoker identity (T-4884 B4). Taken from sudo's own SUDO_UID/SUDO_USER
# (sudo always overwrites both), cross-checked against getpwuid, then
# authorized against the manifest's `invokers` map per verb.
# ---------------------------------------------------------------------------

_SUDO_UID_RE = re.compile(r"^[0-9]{1,10}$")


@dataclass(frozen=True)
class Invoker:
    uid: int
    user: str


def resolve_invoker(
    environ: Mapping[str, str],
    *,
    getuid: Callable[[], int] = os.getuid,
    pwuid: Callable[[int], object] = pwd.getpwuid,
) -> Invoker:
    """Identify the sudo caller, or raise DispatchRefusedError.

    Production requires real uid 0 (the broker runs only via sudo). The
    caller identity is never self-reported text: SUDO_UID is the anchor and
    SUDO_USER must agree with getpwuid(SUDO_UID)."""
    if getuid() != 0:
        raise DispatchRefusedError("broker must run with real uid 0 (via sudo)")
    sudo_uid = environ.get("SUDO_UID")
    sudo_user = environ.get("SUDO_USER")
    if not sudo_uid or not sudo_user:
        raise DispatchRefusedError("SUDO_UID and SUDO_USER must both be set")
    if not _SUDO_UID_RE.fullmatch(sudo_uid):
        raise DispatchRefusedError("SUDO_UID is not a bounded decimal uid")
    try:
        _validate_identifier("SUDO_USER", sudo_user)
    except ManifestValidationError as exc:
        raise DispatchRefusedError(str(exc)) from exc
    uid = int(sudo_uid)
    try:
        pw = pwuid(uid)
    except KeyError as exc:
        raise DispatchRefusedError("SUDO_UID does not resolve via getpwuid") from exc
    if pw.pw_name != sudo_user:
        raise DispatchRefusedError("SUDO_USER does not match getpwuid(SUDO_UID)")
    if uid == 0 or sudo_user in NON_FLEET_ACCOUNT_NAMES:
        raise DispatchRefusedError(f"{sudo_user!r} can never invoke the broker")
    if uid < MIN_FLEET_UID or uid > MAX_FLEET_UID:
        raise DispatchRefusedError("invoker is a system/pseudo account")
    return Invoker(uid=uid, user=sudo_user)


def _authorize_invoker(manifest: BrokerManifest, invoker: Invoker, verb: str) -> None:
    spec = manifest.invokers.get(invoker.user)
    if spec is None:
        raise DispatchRefusedError(f"invoker {invoker.user!r} is not listed in the manifest")
    if spec.uid != invoker.uid:
        raise DispatchRefusedError(f"invoker {invoker.user!r} uid does not match the manifest-resolved uid")
    if verb not in spec.verbs:
        raise DispatchRefusedError(f"invoker {invoker.user!r} is not allowed verb {verb!r}")


def _validate_target_arg(target_key: object) -> None:
    """Reject option-like and malformed target args before any state is touched."""
    if not isinstance(target_key, str) or target_key.startswith("-"):
        raise DispatchRefusedError("target must not be option-like")
    try:
        _validate_identifier("target", target_key)
    except ManifestValidationError as exc:
        raise DispatchRefusedError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Repair lease (T-4884 B4; design note §4.3, D-5).
#
# Layout under the root-owned state dir:
#   leases/            0700, owner-validated via O_NOFOLLOW + fstat
#   leases/<t>.lock    0600, never replaced — the flock target
#   leases/<t>.json    0600, the record, replaced atomically (tmp + rename)
# The lock file is separate from the record because an atomic rename gives
# the record a new inode, and a flock on a replaced inode would not exclude a
# process that opened the new one.
# ---------------------------------------------------------------------------

_LEASE_RECORD_KEYS = frozenset({"holder_uid", "holder_user", "acquired_at", "expires_at"})


@dataclass(frozen=True)
class LeaseRecord:
    holder_uid: int
    holder_user: str
    acquired_at: int
    expires_at: int

    def live(self, now: float) -> bool:
        return now < self.expires_at

    def to_json(self) -> bytes:
        return json.dumps({
            "holder_uid": self.holder_uid,
            "holder_user": self.holder_user,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
        }, sort_keys=True).encode("utf-8")


def _iso_utc(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _parse_lease_record(raw: bytes) -> LeaseRecord:
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup_object_pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKeyError) as exc:
        raise LeaseStateError(f"lease record is corrupt: {exc}") from exc
    if not isinstance(obj, dict) or set(obj) != _LEASE_RECORD_KEYS:
        raise LeaseStateError("lease record has the wrong fields")
    for key in ("holder_uid", "acquired_at", "expires_at"):
        value = obj[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise LeaseStateError(f"lease record {key} must be a non-negative integer")
    try:
        _validate_identifier("holder_user", obj["holder_user"])
    except ManifestValidationError as exc:
        raise LeaseStateError(str(exc)) from exc
    return LeaseRecord(
        holder_uid=obj["holder_uid"], holder_user=obj["holder_user"],
        acquired_at=obj["acquired_at"], expires_at=obj["expires_at"],
    )


class _LeaseHandle:
    """Read/write one target's lease record. Only exists while the target's
    flock is held (LeaseStore.locked)."""

    def __init__(self, store: "LeaseStore", dir_fd: int, target_key: str):
        self._store = store
        self._dir_fd = dir_fd
        self._name = f"{target_key}.json"
        self._tmp_name = f"{target_key}.json.tmp"

    def now(self) -> int:
        return int(self._store.clock())

    def read(self) -> LeaseRecord | None:
        try:
            fd = os.open(self._name, _open_flags(), dir_fd=self._dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise LeaseStateError("lease record must not be a symlink") from exc
            raise LeaseStateError(f"cannot open lease record: {exc}") from exc
        try:
            self._store._check_file(fd, "lease record")
            try:
                raw = _read_all(fd, MAX_LEASE_RECORD_BYTES)
            except ManifestValidationError as exc:
                raise LeaseStateError("lease record exceeds maximum size") from exc
        finally:
            os.close(fd)
        return _parse_lease_record(raw)

    def write(self, record: LeaseRecord) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._tmp_name, dir_fd=self._dir_fd)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(self._tmp_name, flags, LEASE_FILE_MODE, dir_fd=self._dir_fd)
        try:
            os.fchmod(fd, LEASE_FILE_MODE)
            _write_all(fd, record.to_json())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(self._tmp_name, self._name, src_dir_fd=self._dir_fd, dst_dir_fd=self._dir_fd)
        os.fsync(self._dir_fd)

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._name, dir_fd=self._dir_fd)
        os.fsync(self._dir_fd)


class _PreconditionStateHandle:
    """Read/write one invoker's debounce streaks (T-5129). Only exists while
    that invoker's precondition flock is held (LeaseStore.precondition_state)."""

    def __init__(self, store: "LeaseStore", dir_fd: int, invoker_user: str):
        self._store = store
        self._dir_fd = dir_fd
        self._name = f"{invoker_user}.json"
        self._tmp_name = f"{invoker_user}.json.tmp"

    def read(self) -> dict:
        """The streak map; {} when absent or unparseable (no streak: fail-closed,
        the next BAD read starts over). Integrity doubts raise LeaseStateError."""
        try:
            fd = os.open(self._name, _open_flags(), dir_fd=self._dir_fd)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise LeaseStateError("precondition state must not be a symlink") from exc
            raise LeaseStateError(f"cannot open precondition state: {exc}") from exc
        try:
            self._store._check_file(fd, "precondition state")
            try:
                raw = _read_all(fd, MAX_PRECONDITION_STATE_BYTES)
            except ManifestValidationError:
                return {}
        finally:
            os.close(fd)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):   # incl. UnicodeDecodeError, JSONDecodeError; deep nesting
            return {}
        if not isinstance(obj, dict) or obj.get("schema_version") != 1 or not isinstance(obj.get("streaks"), dict):
            return {}
        return obj["streaks"]

    def write(self, streaks: dict) -> None:
        payload = json.dumps({"schema_version": 1, "streaks": streaks}, sort_keys=True).encode("utf-8")
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._tmp_name, dir_fd=self._dir_fd)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(self._tmp_name, flags, LEASE_FILE_MODE, dir_fd=self._dir_fd)
        try:
            os.fchmod(fd, LEASE_FILE_MODE)
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(self._tmp_name, self._name, src_dir_fd=self._dir_fd, dst_dir_fd=self._dir_fd)
        os.fsync(self._dir_fd)


class LeaseStore:
    """Root-owned, UID-bound, one-per-target repair lease (TTL fixed)."""

    def __init__(
        self,
        state_dir: Path,
        *,
        expected_owner_uid: int = MANIFEST_EXPECTED_OWNER_UID,
        clock: Callable[[], float] = time.time,
    ):
        self.state_dir = Path(state_dir)
        self.lease_dir = self.state_dir / LEASE_DIR_NAME
        self.expected_owner_uid = expected_owner_uid
        self.clock = clock

    def _check_dir(self, fd: int, label: str, *, exact_mode: int | None) -> None:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise LeaseStateError(f"{label} is not a directory")
        if st.st_uid != self.expected_owner_uid:
            raise LeaseStateError(f"{label} is not owned by the expected owner")
        mode = stat.S_IMODE(st.st_mode)
        if exact_mode is not None and mode != exact_mode:
            raise LeaseStateError(f"{label} mode {oct(mode)} is not {oct(exact_mode)}")
        if mode & 0o022:
            raise LeaseStateError(f"{label} is group- or other-writable")

    def _check_file(self, fd: int, label: str) -> None:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise LeaseStateError(f"{label} is not a regular file")
        if st.st_uid != self.expected_owner_uid:
            raise LeaseStateError(f"{label} is not owned by the expected owner")
        if stat.S_IMODE(st.st_mode) != LEASE_FILE_MODE:
            raise LeaseStateError(f"{label} mode is not {oct(LEASE_FILE_MODE)}")
        if st.st_nlink != 1:
            raise LeaseStateError(f"{label} has extra hard links")

    def _open_dir(self, name: str | Path, *, dir_fd: int | None, label: str) -> int:
        flags = _open_flags(os.O_DIRECTORY)
        try:
            return os.open(name, flags, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise LeaseStateError(f"{label} must be a real directory, not a symlink") from exc
            raise LeaseStateError(f"cannot open {label}: {exc}") from exc

    def _open_lease_dir(self) -> int:
        return self._open_subdir(LEASE_DIR_NAME, "lease dir")

    def _open_subdir(self, subdir: str, label: str) -> int:
        with contextlib.suppress(FileExistsError):
            os.mkdir(self.state_dir, LEASE_DIR_MODE)
        state_fd = self._open_dir(self.state_dir, dir_fd=None, label="lease state dir")
        try:
            self._check_dir(state_fd, "lease state dir", exact_mode=None)
            created = False
            try:
                os.mkdir(subdir, LEASE_DIR_MODE, dir_fd=state_fd)
                created = True
            except FileExistsError:
                pass
            sub_fd = self._open_dir(subdir, dir_fd=state_fd, label=label)
        finally:
            os.close(state_fd)
        try:
            if created:
                os.fchmod(sub_fd, LEASE_DIR_MODE)  # umask-proof; only on our own fresh dir
            self._check_dir(sub_fd, label, exact_mode=LEASE_DIR_MODE)
            return sub_fd
        except Exception:
            os.close(sub_fd)
            raise

    @contextlib.contextmanager
    def _flocked(self, dir_fd: int, name: str, label: str) -> Iterator[None]:
        base = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        created = False
        try:
            try:
                lock_fd = os.open(name, base | os.O_CREAT | os.O_EXCL, LEASE_FILE_MODE, dir_fd=dir_fd)
                created = True
            except FileExistsError:
                lock_fd = os.open(name, base, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise LeaseStateError(f"{label} must not be a symlink") from exc
            raise LeaseStateError(f"cannot open {label}: {exc}") from exc
        try:
            if created:
                os.fchmod(lock_fd, LEASE_FILE_MODE)  # umask-proof; only on our own fresh file
            self._check_file(lock_fd, label)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(lock_fd)  # releases the flock

    @contextlib.contextmanager
    def precondition_state(self, invoker_user: str) -> Iterator[_PreconditionStateHandle]:
        """T-5129: hold one invoker's debounce-state lock for the whole block.
        The streak is per invoker, not per target, so it has its own flock;
        callers take it only while already holding a target lease lock, never
        the reverse (fixed order: no deadlock)."""
        try:
            _validate_identifier("invoker", invoker_user)
        except ManifestValidationError as exc:
            raise LeaseStateError(str(exc)) from exc
        dir_fd = self._open_subdir(PRECONDITION_DIR_NAME, "precondition state dir")
        try:
            with self._flocked(dir_fd, f"{invoker_user}.lock", "precondition state lock file"):
                yield _PreconditionStateHandle(self, dir_fd, invoker_user)
        finally:
            os.close(dir_fd)

    @contextlib.contextmanager
    def locked(self, target_key: str) -> Iterator[_LeaseHandle]:
        """Hold the target's lease lock (blocking flock) for the whole block."""
        _validate_target_arg(target_key)
        dir_fd = self._open_lease_dir()
        try:
            with self._flocked(dir_fd, f"{target_key}.lock", "lease lock file"):
                yield _LeaseHandle(self, dir_fd, target_key)
        finally:
            os.close(dir_fd)


def format_lease_status(target_key: str, record: LeaseRecord | None, now: int) -> str:
    """The fixed-format `lease status` line (one line, key=value, no free text)."""
    if record is None:
        return f"lease target={target_key} state=free holder=- holder_uid=- expires_at=- remaining_secs=-"
    state = "held" if record.live(now) else "expired"
    remaining = max(0, record.expires_at - now)
    return (
        f"lease target={target_key} state={state} holder={record.holder_user} "
        f"holder_uid={record.holder_uid} expires_at={_iso_utc(record.expires_at)} "
        f"remaining_secs={remaining}"
    )


def dispatch_lease(
    *,
    op: str,
    target_key: str,
    invoker: Invoker,
    manifest_path: Path,
    audit: AuditSink,
    lease_store: LeaseStore,
    manifest_loader: Callable[..., BrokerManifest] = load_broker_manifest,
    manifest_kwargs: dict | None = None,
    pwnam: Callable[[str], object] = pwd.getpwnam,
    export: LeaseExportSink | None = None,
) -> DispatchResult:
    """`lease {acquire|release|status} <target>`. Every op is audited
    fail-closed: if the audit write fails, the lease state is not changed."""
    manifest_kwargs = dict(manifest_kwargs or {})
    manifest_kwargs.setdefault("pwnam", pwnam)
    who = {"verb": "lease", "op": op, "target": target_key,
           "invoker_uid": invoker.uid, "invoker_user": invoker.user}

    def _refuse(reason: str) -> DispatchResult:
        _best_effort_audit(audit, event="refuse", reason=reason, **who)
        return DispatchResult(outcome="refused", detail=reason)

    if op not in LEASE_OPS:
        return _refuse(f"unknown lease op: {op!r}")
    # Before any manifest read or state touch (design note §4.3).
    try:
        _validate_target_arg(target_key)
    except DispatchRefusedError as exc:
        return _refuse(str(exc))
    try:
        manifest = manifest_loader(manifest_path, **manifest_kwargs)
    except ManifestValidationError as exc:
        return _refuse(f"manifest invalid: {exc}")
    try:
        _authorize_invoker(manifest, invoker, "lease")
    except DispatchRefusedError as exc:
        return _refuse(str(exc))
    if target_key not in manifest.targets:
        return _refuse("unknown target")

    try:
        with lease_store.locked(target_key) as lease:
            now = lease.now()
            current = lease.read()
            live = current is not None and current.live(now)
            if current is not None and not live:
                _export(export, "expire_observed", target_key, current, now)

            if op == "status":
                line = format_lease_status(target_key, current, now)
                state = "free" if current is None else ("held" if live else "expired")
                try:
                    audit.write(event="lease_status", state=state, **who)
                except AuditWriteError as exc:
                    return DispatchResult(outcome="refused", detail=f"audit sink unwritable: {exc}")
                return DispatchResult(outcome="executed", detail=line)

            if op == "acquire":
                if live and current.holder_uid != invoker.uid:
                    return _refuse(
                        f"lease on {target_key} held by {current.holder_user} "
                        f"until {_iso_utc(current.expires_at)}"
                    )
                # T-5128: a precondition-bearing invoker may not take OR renew
                # a lease while its precondition fails, so it cannot squat a
                # lease a higher-precedence repairer needs. Same three-state
                # check as dispatch(); unknown refuses (fail-closed, plan
                # D-42). Invokers with no precondition pass unchanged; release
                # and status stay ungated, so a holder can always let go.
                # T-5129: debounced (two BAD reads, two snapshots); a state-
                # store doubt raises LeaseStateError and refuses below.
                allowed, states = check_precondition_debounced(
                    manifest.invokers[invoker.user].precondition, invoker_user=invoker.user,
                    now=now, pwnam=pwnam, lease_store=lease_store)
                if not allowed:
                    return _refuse("precedence precondition not met: " + " ".join(states))
                record = LeaseRecord(
                    holder_uid=invoker.uid, holder_user=invoker.user,
                    acquired_at=now, expires_at=now + LEASE_TTL_SECS,
                )
                try:
                    audit.write(event="lease_acquire", renew=live, expires_at=record.expires_at, **who)
                except AuditWriteError as exc:
                    return DispatchResult(outcome="refused", detail=f"audit sink unwritable: {exc}")
                lease.write(record)
                _export(export, "renew" if live else "acquire", target_key, record, now)
                verb_word = "renewed" if live else "acquired"
                return DispatchResult(
                    outcome="executed",
                    detail=f"lease on {target_key} {verb_word} by {invoker.user} until {_iso_utc(record.expires_at)}",
                )

            # op == "release"
            if live and current.holder_uid != invoker.uid:
                return _refuse(
                    f"lease on {target_key} held by {current.holder_user} "
                    f"until {_iso_utc(current.expires_at)}; only the holder may release it"
                )
            try:
                audit.write(
                    event="lease_release",
                    previous_holder=current.holder_user if current else None,
                    was_live=live,
                    **who,
                )
            except AuditWriteError as exc:
                return DispatchResult(outcome="refused", detail=f"audit sink unwritable: {exc}")
            lease.clear()
            if live:
                _export(export, "release", target_key, current, now)
            return DispatchResult(
                outcome="executed",
                detail=f"lease on {target_key} released" if current else f"lease on {target_key} already free",
            )
    except (LeaseStateError, OSError) as exc:
        return _refuse(f"lease state error: {exc}")


# ---------------------------------------------------------------------------
# Dispatch — the single entry point every destructive verb runs through.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispatchResult:
    outcome: str  # "executed" | "dry_run" | "refused"
    detail: str
    exit_code: int | None = None


class SubprocessRunner(Protocol):
    def __call__(self, argv: list[str], *, env: dict[str, str], timeout: float): ...


def _real_runner(argv: list[str], *, env: dict[str, str], timeout: float):
    return subprocess.run(
        argv,
        env=env,
        timeout=timeout,
        umask=DEFAULT_UMASK,
        close_fds=True,
        capture_output=True,
        text=True,
        shell=False,
    )


def dispatch(
    *,
    verb: str,
    target_key: str,
    action: str | None,
    invoker: Invoker,
    lease_store: LeaseStore,
    manifest_path: Path,
    audit: AuditSink,
    rate_limiter: RateLimiter,
    runner: SubprocessRunner = _real_runner,
    foreground_probe: Callable[[TargetSpec], ForegroundSnapshot] | None = None,
    deploy_owner_uids: frozenset[int] = DEFAULT_DEPLOY_OWNER_UIDS,
    dry_run: bool = False,
    manifest_loader: Callable[..., BrokerManifest] = load_broker_manifest,
    manifest_kwargs: dict | None = None,
    pwnam: Callable[[str], object] = pwd.getpwnam,
    export: LeaseExportSink | None = None,
) -> DispatchResult:
    """Run one destructive verb. `invoker` and `lease_store` are REQUIRED
    (no default), so no caller can reach an action without the invoker
    authorization and the lease check."""
    manifest_kwargs = dict(manifest_kwargs or {})
    manifest_kwargs.setdefault("pwnam", pwnam)
    who = {"verb": verb, "target": target_key, "action": action,
           "invoker_uid": invoker.uid, "invoker_user": invoker.user}

    def _refuse(reason: str) -> DispatchResult:
        _best_effort_audit(audit, event="refuse", reason=reason, **who)
        return DispatchResult(outcome="refused", detail=reason)

    if verb not in DESTRUCTIVE_VERBS:
        return _refuse(f"verb not dispatchable: {verb!r}")

    try:
        _validate_target_arg(target_key)
    except DispatchRefusedError as exc:
        return _refuse(str(exc))

    try:
        manifest = manifest_loader(manifest_path, **manifest_kwargs)
    except ManifestValidationError as exc:
        return _refuse(f"manifest invalid: {exc}")

    try:
        _authorize_invoker(manifest, invoker, verb)
    except DispatchRefusedError as exc:
        return _refuse(str(exc))

    target = manifest.targets.get(target_key)
    if target is None:
        return _refuse("unknown target")
    if target.lease_only:
        return _refuse(f"target {target_key} is lease-only: no verb may act on it")

    if verb == "kill-session":
        try:
            argv = build_kill_session_argv(target, pwnam=pwnam)
        except DispatchRefusedError as exc:
            return _refuse(str(exc))
    elif verb == "unstick":
        if action not in UNSTICK_ACTIONS:
            return _refuse(f"unknown unstick action: {action!r}")
        if foreground_probe is None:
            return _refuse("no foreground probe configured")
        try:
            snapshot = foreground_probe(target)
            _validate_foreground_snapshot(snapshot, target, deploy_owner_uids=deploy_owner_uids)
        except DispatchRefusedError as exc:
            return _refuse(str(exc))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # The real PaneForegroundProbe reads /proc + shells out to tmux —
            # a vanished pane, a non-tty pid, or a tmux failure raises here,
            # not as DispatchRefusedError. Still fail-closed either way (no
            # action would run), but this keeps it a clean, audited refusal
            # instead of an uncaught traceback out of dispatch()/main().
            return _refuse(f"foreground probe failed: {exc}")
        try:
            argv = build_unstick_argv(target, action, pwnam=pwnam)
        except DispatchRefusedError as exc:
            return _refuse(str(exc))
    elif verb == "reset-failed":
        try:
            argv = build_reset_failed_argv(target, action, pwnam=pwnam)
        except DispatchRefusedError as exc:
            return _refuse(str(exc))
    elif verb == "restart":
        try:
            argv = build_restart_argv(target, action, pwnam=pwnam)
        except DispatchRefusedError as exc:
            return _refuse(str(exc))
    else:  # pragma: no cover — DESTRUCTIVE_VERBS exhausted above
        return _refuse(f"unhandled verb: {verb!r}")

    with contextlib.ExitStack() as stack:
        # The target's lease lock is taken HERE and held through the action:
        # a competing `lease acquire` blocks on the same flock, so the holder
        # cannot change between the lease check and the subprocess (no TOCTOU).
        try:
            lease = stack.enter_context(lease_store.locked(target_key))
            current = lease.read()
            now = lease.now()
        except (LeaseStateError, DispatchRefusedError, OSError) as exc:
            return _refuse(f"lease state error: {exc}")
        if current is None:
            return _refuse(f"no lease held on {target_key}; `lease acquire` first")
        if not current.live(now):
            _export(export, "expire_observed", target_key, current, now)
            return _refuse(f"lease on {target_key} expired at {_iso_utc(current.expires_at)}")
        if current.holder_uid != invoker.uid:
            return _refuse(
                f"lease on {target_key} held by {current.holder_user} "
                f"until {_iso_utc(current.expires_at)}"
            )

        # T-5124: a lower-precedence invoker acts only while every higher-
        # precedence repairer reads bad, evaluated under the lease lock.
        # T-5129: debounced, same as the lease-acquire gate.
        try:
            allowed, states = check_precondition_debounced(
                manifest.invokers[invoker.user].precondition, invoker_user=invoker.user,
                now=now, pwnam=pwnam, lease_store=lease_store)
        except (LeaseStateError, OSError) as exc:
            return _refuse(f"precondition state error: {exc}")
        if not allowed:
            return _refuse("precedence precondition not met: " + " ".join(states))

        rate_key = f"{verb}__{target_key}"
        if not rate_limiter.check_and_record(rate_key):
            return _refuse("rate limit exceeded")

        env = _build_env(target, pwnam=pwnam)

        if dry_run:
            _best_effort_audit(audit, event="dry_run", argv=argv, **who)
            return DispatchResult(outcome="dry_run", detail="validated; not executed")

        # Fail-closed (contract item 5): the pre-action audit write MUST succeed
        # before we act. If it can't, refuse — never execute unaudited.
        try:
            audit.write(event="pre_action", argv=argv, **who)
        except AuditWriteError as exc:
            return DispatchResult(outcome="refused", detail=f"audit sink unwritable: {exc}")

        result = runner(argv, env=env, timeout=SUBPROCESS_TIMEOUT_SECS)

        try:
            audit.write(event="post_action", exit_code=result.returncode, **who)
        except AuditWriteError as exc:
            # Best-effort: the action already ran and can't be un-done. Loud on
            # stderr so this is never a silently-swallowed gap.
            print(f"codex-recovery: post-action audit write failed: {exc}", file=sys.stderr)

        outcome = "executed" if result.returncode == 0 else "refused"
        return DispatchResult(outcome=outcome, detail="subprocess completed", exit_code=result.returncode)


def dispatch_linger_enable(
    *,
    target_key: str,
    invoker: Invoker,
    manifest_path: Path,
    audit: AuditSink,
    rate_limiter: RateLimiter,
    runner: SubprocessRunner = _real_runner,
    linger_probe: Callable[[str], bool] = real_linger_probe,
    dry_run: bool = False,
    manifest_loader: Callable[..., BrokerManifest] = load_broker_manifest,
    manifest_kwargs: dict | None = None,
    pwnam: Callable[[str], object] = pwd.getpwnam,
) -> DispatchResult:
    """`linger-enable <target>` (T-5059). No lease: restorative, non-destructive
    (see module docstring). Audit is fail-closed before the action."""
    manifest_kwargs = dict(manifest_kwargs or {})
    manifest_kwargs.setdefault("pwnam", pwnam)
    who = {"verb": "linger-enable", "target": target_key, "action": None,
           "invoker_uid": invoker.uid, "invoker_user": invoker.user}

    def _refuse(reason: str) -> DispatchResult:
        _best_effort_audit(audit, event="refuse", reason=reason, **who)
        return DispatchResult(outcome="refused", detail=reason)

    try:
        _validate_target_arg(target_key)
    except DispatchRefusedError as exc:
        return _refuse(str(exc))
    try:
        manifest = manifest_loader(manifest_path, **manifest_kwargs)
    except ManifestValidationError as exc:
        return _refuse(f"manifest invalid: {exc}")
    try:
        _authorize_invoker(manifest, invoker, "linger-enable")
    except DispatchRefusedError as exc:
        return _refuse(str(exc))
    target = manifest.targets.get(target_key)
    if target is None:
        return _refuse("unknown target")
    if target.lease_only:
        return _refuse(f"target {target_key} is lease-only: no verb may act on it")
    try:
        argv = build_linger_enable_argv(target, pwnam=pwnam)
    except DispatchRefusedError as exc:
        return _refuse(str(exc))

    before = bool(linger_probe(target.account))
    if before:
        try:
            audit.write(event="linger_already", before_linger=True, after_linger=True, **who)
        except AuditWriteError as exc:
            return DispatchResult(outcome="refused", detail=f"audit sink unwritable: {exc}")
        return DispatchResult(outcome="executed", detail="already lingering; nothing to do", exit_code=0)

    if not rate_limiter.check_and_record(f"linger-enable__{target_key}"):
        return _refuse("rate limit exceeded")

    if dry_run:
        _best_effort_audit(audit, event="dry_run", argv=argv, before_linger=False, **who)
        return DispatchResult(outcome="dry_run", detail="validated; not executed")

    try:
        audit.write(event="pre_action", argv=argv, before_linger=False, **who)
    except AuditWriteError as exc:
        return DispatchResult(outcome="refused", detail=f"audit sink unwritable: {exc}")

    try:
        result = runner(argv, env=_root_env(), timeout=SUBPROCESS_TIMEOUT_SECS)
        exit_code, runner_error = result.returncode, False
    except (subprocess.SubprocessError, OSError):
        exit_code, runner_error = -1, True
    after = bool(linger_probe(target.account))

    try:
        audit.write(event="post_action", exit_code=exit_code, runner_error=runner_error,
                    before_linger=False, after_linger=after, **who)
    except AuditWriteError as exc:
        print(f"codex-recovery: post-action audit write failed: {exc}", file=sys.stderr)

    if runner_error or exit_code != 0:
        return DispatchResult(outcome="refused", detail="loginctl enable-linger failed", exit_code=exit_code)
    if not after:
        return DispatchResult(outcome="refused", detail="linger not verified after enable-linger",
                              exit_code=exit_code)
    return DispatchResult(outcome="executed", detail="linger enabled and verified", exit_code=exit_code)


# ---------------------------------------------------------------------------
# selftest — manifest preflight only, touches nothing, needs no privilege.
# ---------------------------------------------------------------------------

def cmd_selftest(
    *,
    manifest_path: Path,
    pwnam: Callable[[str], object] = pwd.getpwnam,
    manifest_kwargs: dict | None = None,
) -> tuple[bool, dict]:
    manifest_kwargs = dict(manifest_kwargs or {})
    manifest_kwargs.setdefault("pwnam", pwnam)
    try:
        targets = load_manifest(manifest_path, **manifest_kwargs)
    except ManifestValidationError as exc:
        return False, {"ok": False, "error": str(exc)}
    binaries_present = all(os.path.exists(b) for b in (TMUX, SETPRIV, LOGINCTL, SYSTEMCTL, ENV_BIN))
    report = {
        "ok": binaries_present,
        "targets": sorted(targets),
        "binaries_present": binaries_present,
    }
    return report["ok"], report


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-recovery",
        description="ORG-048 scoped cross-account fleet-repair broker (inert until the gated install).",
        allow_abbrev=False,
    )
    # Path overrides exist for unprivileged selftest only; they are refused
    # under privilege (see main()), so a sudo caller cannot redirect state.
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--audit-log", type=Path, default=None)
    parser.add_argument("--rate-limit-dir", type=Path, default=None)
    parser.add_argument("--lease-export", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("selftest", allow_abbrev=False)
    p_lease = sub.add_parser("lease", allow_abbrev=False)
    p_lease.add_argument("op", choices=LEASE_OPS)
    p_lease.add_argument("target")
    p_kill = sub.add_parser("kill-session", allow_abbrev=False)
    p_kill.add_argument("target")
    p_linger = sub.add_parser("linger-enable", allow_abbrev=False)
    p_linger.add_argument("target")
    p_unstick = sub.add_parser("unstick", allow_abbrev=False)
    p_unstick.add_argument("target")
    p_unstick.add_argument("action", choices=UNSTICK_ACTIONS)
    p_reset = sub.add_parser("reset-failed", allow_abbrev=False)
    p_reset.add_argument("target")
    p_reset.add_argument("action", metavar="unit")
    p_restart = sub.add_parser("restart", allow_abbrev=False)
    p_restart.add_argument("target")
    p_restart.add_argument("action", metavar="unit")
    return parser


@dataclass
class BrokerContext:
    """Everything main() takes from the process. Production builds it from
    the real process (BrokerContext()); tests inject a fake one. It is only
    reachable in-process, never from argv."""

    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    getuid: Callable[[], int] = os.getuid
    geteuid: Callable[[], int] = os.geteuid
    pwuid: Callable[[int], object] = pwd.getpwuid
    pwnam: Callable[[str], object] = pwd.getpwnam
    manifest_path: Path = DEFAULT_MANIFEST_PATH
    audit_log_path: Path = DEFAULT_AUDIT_LOG_PATH
    lease_export_path: Path = DEFAULT_LEASE_EXPORT_PATH
    lease_export_gid: int | None = None  # None: resolve group pantheon-health
    state_dir: Path = DEFAULT_RATE_LIMIT_STATE_DIR
    state_owner_uid: int = MANIFEST_EXPECTED_OWNER_UID
    manifest_kwargs: dict = field(default_factory=dict)
    runner: SubprocessRunner = _real_runner
    foreground_probe: Callable[[TargetSpec], ForegroundSnapshot] | None = None
    linger_probe: Callable[[str], bool] = real_linger_probe
    clock: Callable[[], float] = time.time


def _emit(result: DispatchResult) -> int:
    print(json.dumps({"outcome": result.outcome, "detail": result.detail, "exit_code": result.exit_code}))
    return 0 if result.outcome in ("executed", "dry_run") else 1


def main(argv: list[str] | None = None, *, ctx: BrokerContext | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    ctx = ctx if ctx is not None else BrokerContext()
    privileged = ctx.getuid() == 0 or ctx.geteuid() == 0

    overrides = [a for a in (args.manifest, args.audit_log, args.rate_limit_dir, args.lease_export)
                 if a is not None]
    if privileged and overrides:
        return _emit(DispatchResult(outcome="refused", detail="path overrides are refused under privilege"))
    manifest_path = args.manifest or ctx.manifest_path

    if args.verb == "selftest" and not privileged:
        # Unprivileged manifest preflight: reads only, needs no invoker.
        ok, report = cmd_selftest(manifest_path=manifest_path, pwnam=ctx.pwnam,
                                  manifest_kwargs=ctx.manifest_kwargs)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if ok else 1

    audit = AuditSink(args.audit_log or ctx.audit_log_path)
    state_dir = args.rate_limit_dir or ctx.state_dir
    target_key = getattr(args, "target", None)

    try:
        invoker = resolve_invoker(ctx.environ, getuid=ctx.getuid, pwuid=ctx.pwuid)
    except DispatchRefusedError as exc:
        _best_effort_audit(audit, event="refuse", verb=args.verb, target=target_key,
                           reason=f"invoker: {exc}")
        return _emit(DispatchResult(outcome="refused", detail=f"invoker refused: {exc}"))

    if args.verb == "selftest":
        try:
            manifest = load_broker_manifest(manifest_path, **{"pwnam": ctx.pwnam, **ctx.manifest_kwargs})
            _authorize_invoker(manifest, invoker, "selftest")
        except (ManifestValidationError, DispatchRefusedError) as exc:
            _best_effort_audit(audit, event="refuse", verb="selftest", reason=str(exc),
                               invoker_uid=invoker.uid, invoker_user=invoker.user)
            return _emit(DispatchResult(outcome="refused", detail=str(exc)))
        ok, report = cmd_selftest(manifest_path=manifest_path, pwnam=ctx.pwnam,
                                  manifest_kwargs=ctx.manifest_kwargs)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if ok else 1

    lease_store = LeaseStore(state_dir, expected_owner_uid=ctx.state_owner_uid, clock=ctx.clock)
    export = LeaseExportSink(args.lease_export or ctx.lease_export_path,
                             expected_owner_uid=ctx.state_owner_uid, group_gid=ctx.lease_export_gid)

    if args.verb == "lease":
        result = dispatch_lease(
            op=args.op, target_key=args.target, invoker=invoker,
            manifest_path=manifest_path, audit=audit, lease_store=lease_store,
            manifest_kwargs=ctx.manifest_kwargs, pwnam=ctx.pwnam, export=export,
        )
        if args.op == "status" and result.outcome == "executed":
            print(result.detail)
            return 0
        return _emit(result)

    if args.verb == "linger-enable":
        return _emit(dispatch_linger_enable(
            target_key=args.target, invoker=invoker, manifest_path=manifest_path, audit=audit,
            rate_limiter=RateLimiter(state_dir, clock=ctx.clock), runner=ctx.runner,
            linger_probe=ctx.linger_probe, dry_run=args.dry_run,
            manifest_kwargs=ctx.manifest_kwargs, pwnam=ctx.pwnam,
        ))

    probe = ctx.foreground_probe
    if probe is None and args.verb == "unstick":
        probe = PaneForegroundProbe(pwnam=ctx.pwnam).snapshot
    result = dispatch(
        verb=args.verb,
        target_key=args.target,
        action=getattr(args, "action", None),
        invoker=invoker,
        lease_store=lease_store,
        manifest_path=manifest_path,
        audit=audit,
        rate_limiter=RateLimiter(state_dir, clock=ctx.clock),
        runner=ctx.runner,
        foreground_probe=probe,
        dry_run=args.dry_run,
        manifest_kwargs=ctx.manifest_kwargs,
        pwnam=ctx.pwnam,
        export=export,
    )
    return _emit(result)


if __name__ == "__main__":
    sys.exit(main())
