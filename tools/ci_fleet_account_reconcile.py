"""tools/ci_fleet_account_reconcile.py — ORG-066 Rung-1 fleet-account reconciler (T-2219).

WHY THIS EXISTS: pantheon-config's CI runs on CT202, where ~26 tests fail because the
fleet's daemon USER ACCOUNTS do not exist — `persona_cutover_resolve.py` calls
`pwd.getpwnam(account)` (unguarded) and KeyErrors, and the private-socket-tmux tests need a
`tmux` binary. main is honestly red the same way, so PRs merge via --admin over red. ORG-066
Rung 1 fixes the ENVIRONMENT (real accounts + tmux), not the tests (no getpwnam mock — D-1/D11).

WHAT IT DOES: reconciles a host's `/etc/passwd` to the fleet manifest
(`agent_role_map.fleet_accounts()`) — the 8 daemon accounts, idempotently. `--check` is a
strictly READ-ONLY drift detector (the pre-job hook); `--apply` is the explicit, idempotent
mutation. It reads the desired set from the manifest (single source of truth) and is
host-neutral (an `--exec` prefix routes account ops onto a target — e.g. CT202 via
`ssh pve2 pct exec 202 --` — so ONE reconciler serves a local pre-job hook AND a Rung-2/3
container/VM unchanged, per codex C3-e).

⭐ THE LOAD-BEARING RUNG-1 BOUNDARY (codex C1): passwd entries + `tmux` ONLY. This reconciler
NEVER enables `loginctl` linger, a user bus, or a user-manager for any account. The
bringup-acceptance real-`systemd --user` legs self-skip on clause 3
(`systemctl --user is-system-running` ∉ {running,degraded}); linger would satisfy clause 3,
UN-SKIP those legs, and turn them into NEW failures — destroying the "26 need only
accounts+tmux, systemd legs stay skipped" property this whole rung rests on. Real
`systemd --user` capability is a deliberate Rung-2/3 transition, NEVER a Rung-1 side effect.

PROVISIONING BOUNDARIES (codex C3), enforced structurally, not by convention:
  (a) MANIFEST-SET ONLY. It never creates/modifies/locks/deletes an account outside
      `fleet_accounts()` — never the runner account, `root`, or any unrelated user.
  (b) FAIL-LOUD ON CONFLICT, NEVER CLOBBER. UIDs are fixed; if a target uid/gid/name is held
      by an account that is NOT its intended fleet owner, the reconciler ERRORS and STOPS —
      it never reassigns or overwrites. Likewise a present fleet account whose uid/shell/home
      diverges from desired is reported as DRIFT and (in --apply) STOPS, never silently
      re-homed or re-uid'd (changing a live account's identity is not our call).
  (c) INERT POSTURE. Accounts are created LOCKED (`!` in shadow — useradd's default with no
      password), login-shell DISABLED (`/usr/sbin/nologin`), a scoped home `/home/<account>`
      owned by the account, a user-private group, and NO ownership of the runner workspace or
      any CI path. These are `getpwnam` fixtures, not interactive or service accounts.
  (d) --check NEVER MUTATES. In its drift role it only reads (`getent`) and reports; mutation
      is a separate explicit `--apply`, never a side effect of the check.
  (e) PORTABLE. Manifest-driven + host-neutral (the `--exec` seam); no CT202-only baked state.

UID POLICY (T-2219, orchestrator-blessed 2026-08-09 — cite in DECISIONS): the manifest pins NO uids
(name-only). Two accounts carry a HARD test contract — `test_persona_cutover_resolve.py`
asserts `identity.uid == 1001` for claude-fallback (recovery) and `== 1002` for claude-research
(specialist), and `persona_cutover_resolve` reads the real `pw_uid` — so those two are pinned to
match (derived-not-invented, P4). No test asserts any other account's uid, and mirroring the
live fleet uids would collide (claude's live uid 1000 == CT202's `runner`), so the remaining
six take FIXED, EXPLICIT uids (`_RESERVED_UIDS`, 6000/6003-6007 — frozen constants, NOT
index-arithmetic, so a future fleet-set change can never silently renumber an existing
account; a new unmapped account fails LOUD, forcing an explicit uid decision). UIDs 1001/1002
are free on CT202 (only `runner`=1000), so no collision. Fixed at provision time, not test
time (D-3).
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# The two accounts the suite hard-pins (test_persona_cutover_resolve.py:124/155/390/405 +
# the /run/user/<uid> assertions). These MUST match or the getpwnam-class tests fail.
_TEST_PINNED_UIDS = {"claude-fallback": 1001, "claude-research": 1002}
# The six uid-agnostic accounts (no test asserts their uid) take FIXED, EXPLICIT uids —
# frozen here as constants rather than index-computed (orchestrator T-2219 review 2026-08-09: this is
# a durable identity-provisioning tool, so a future fleet-set change must NEVER silently
# renumber an existing account's identity; an explicit map is identity-stable + self-documenting
# and matches D-3b "fixed UIDs"). Chosen to clear CT202's runner (1000) and the pinned
# 1001/1002. fail-loud still guards every one. The 6001/6002 gap is intentional — those slots
# belong to the test-pinned pair above; freezing today's values keeps the six numerically stable.
_RESERVED_UIDS = {
    "claude": 6000,
    "codex": 6003,
    "exocortex": 6004,
    "suborch1": 6005,
    "suborch2": 6006,
    "the-ancestor": 6007,
}

HOME_ROOT = "/home"
NOLOGIN = "/usr/sbin/nologin"
ACCOUNT_COMMENT = "ORG-066 CI fleet-mirror fixture (T-2219) — locked getpwnam fixture, no login"

# Verdicts.
MISSING, OK, DRIFT, CONFLICT = "MISSING", "OK", "DRIFT", "CONFLICT"
# --check exits non-zero on anything not OK; CONFLICT/DRIFT are hard stops for --apply.
NEEDS_ACTION = (MISSING,)
BLOCKING = (CONFLICT, DRIFT)


def _log(msg: str) -> None:
    print(f"[ci_fleet_account_reconcile] {msg}", file=sys.stderr)


def desired_accounts() -> "list[AccountSpec]":
    """The desired fleet-account set, from the manifest (single source of truth).

    Import is lazy + path-tolerant so the tool runs from a bare checkout on any host."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import agent_role_map as arm  # noqa: E402

    specs = []
    for account in sorted(arm.fleet_accounts()):
        uid = _TEST_PINNED_UIDS.get(account, _RESERVED_UIDS.get(account))
        if uid is None:
            # FORCING FUNCTION (orchestrator T-2219 review): a new fleet account with no explicit uid
            # must NOT be auto-numbered — surface the identity decision instead of guessing.
            raise SystemExit(
                f"unmapped fleet account {account!r}: no pinned or reserved uid assigned. "
                f"Add an explicit uid to _RESERVED_UIDS (or _TEST_PINNED_UIDS if a test asserts "
                f"it) before provisioning — identity uids are frozen deliberately, not derived."
            )
        specs.append(AccountSpec(name=account, uid=uid))
    # Guard: no two accounts may resolve to the same uid (defends an editing slip in the maps).
    uids = [s.uid for s in specs]
    if len(set(uids)) != len(uids):
        raise SystemExit(f"BUG: duplicate uid in desired set: {sorted(uids)}")
    return specs


@dataclass(frozen=True)
class AccountSpec:
    name: str
    uid: int

    @property
    def gid(self) -> int:
        return self.uid  # user-private group, gid == uid

    @property
    def home(self) -> str:
        return f"{HOME_ROOT}/{self.name}"

    @property
    def shell(self) -> str:
        return NOLOGIN


@dataclass(frozen=True)
class PasswdEntry:
    name: str
    uid: int
    gid: int
    home: str
    shell: str


def classify(spec: AccountSpec, by_name: "dict[str, PasswdEntry]",
             by_uid: "dict[int, PasswdEntry]") -> dict:
    """Pure decision for one desired account against current /etc/passwd. No I/O.

    Returns {account, verdict, uid, detail}. CONFLICT (someone else holds our name/uid) and
    DRIFT (our account exists but wrong posture) are BLOCKING — never clobbered."""
    present_name = by_name.get(spec.name)
    present_uid = by_uid.get(spec.uid)

    # Someone ELSE already owns our target uid — never clobber (C3-b).
    if present_uid is not None and present_uid.name != spec.name:
        return {"account": spec.name, "verdict": CONFLICT, "uid": spec.uid,
                "detail": (f"target uid {spec.uid} is held by unrelated account "
                           f"'{present_uid.name}' — refusing to reassign/clobber")}

    if present_name is None:
        return {"account": spec.name, "verdict": MISSING, "uid": spec.uid,
                "detail": f"absent — will create uid={spec.uid} home={spec.home} shell={NOLOGIN} locked"}

    # Our account exists — verify posture; a mismatch is DRIFT (surfaced, never silently fixed).
    diffs = []
    if present_name.uid != spec.uid:
        diffs.append(f"uid {present_name.uid}!={spec.uid}")
    if present_name.home != spec.home:
        diffs.append(f"home {present_name.home}!={spec.home}")
    if present_name.shell != spec.shell:
        diffs.append(f"shell {present_name.shell}!={spec.shell}")
    if diffs:
        return {"account": spec.name, "verdict": DRIFT, "uid": spec.uid,
                "detail": "present but diverged: " + ", ".join(diffs)}
    return {"account": spec.name, "verdict": OK, "uid": spec.uid,
            "detail": f"present + correct (uid={spec.uid}, {NOLOGIN})"}


def plan(specs: "list[AccountSpec]", passwd: "list[PasswdEntry]") -> "list[dict]":
    """Classify every desired account. Pure — the reconciliation decision, no mutation."""
    by_name = {e.name: e for e in passwd}
    by_uid = {e.uid: e for e in passwd}
    return [classify(s, by_name, by_uid) for s in specs]


class Ops:
    """The side-effect seam. `exec_prefix` routes account ops onto a target host (e.g.
    ['ssh','pve2','pct','exec','202','--']); empty = local. Read methods for --check; the
    ONE write method (create_account) is reached only from --apply."""

    def __init__(self, exec_prefix: "list[str] | None" = None):
        self.exec_prefix = list(exec_prefix or [])

    def _run(self, argv: "list[str]", *, check: bool = False) -> subprocess.CompletedProcess:
        # When exec_prefix routes through a transport that re-parses on a REMOTE shell — ssh
        # flattens its command args into ONE string handed to the remote login shell — the argv
        # tokens must be shell-quoted so the remote shell reconstructs them intact. Without this,
        # a --comment carrying spaces + parens ("(T-2219)") splits/syntax-errors remotely (caught
        # at the first CT202 --apply). Local exec (empty prefix) runs argv directly with no shell
        # in the path, so no quoting is applied — and getent's args are plain, so --check is
        # unaffected under either branch.
        if self.exec_prefix:
            full = self.exec_prefix + [shlex.quote(a) for a in argv]
        else:
            full = list(argv)
        return subprocess.run(full, capture_output=True, text=True, timeout=60, check=check)

    def read_passwd(self) -> "list[PasswdEntry]":
        """Full `/etc/passwd` on the target, parsed. Read-only."""
        r = self._run(["getent", "passwd"])
        if r.returncode != 0:
            raise SystemExit(f"could not read passwd on target (rc={r.returncode}): {r.stderr.strip()}")
        entries = []
        for line in r.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 7:
                continue
            try:
                entries.append(PasswdEntry(name=parts[0], uid=int(parts[2]), gid=int(parts[3]),
                                           home=parts[5], shell=parts[6]))
            except ValueError:
                continue
        return entries

    def create_account(self, spec: AccountSpec) -> None:
        """Create ONE inert, locked, nologin fleet account with a scoped home + private group.
        useradd with no `-p` leaves the shadow password `!` (locked). NO linger, NO systemd."""
        argv = [
            "useradd",
            "--uid", str(spec.uid),
            "--user-group",              # private group, gid == uid
            "--home-dir", spec.home,
            "--create-home",
            "--shell", NOLOGIN,
            "--comment", ACCOUNT_COMMENT,
            spec.name,
        ]
        r = self._run(argv)
        if r.returncode != 0:
            raise SystemExit(f"useradd failed for {spec.name} (rc={r.returncode}): {r.stderr.strip()}")


def reconcile(*, ops: Ops, apply: bool) -> "tuple[list[dict], int]":
    """Plan, then (if apply) create MISSING accounts. Returns (results, exit_code).

    BLOCKING verdicts (CONFLICT/DRIFT) stop --apply before any mutation — fail-loud, never
    clobber (C3-b). --check mutates nothing regardless (C3-d)."""
    specs = desired_accounts()
    results = plan(specs, ops.read_passwd())

    blocking = [r for r in results if r["verdict"] in BLOCKING]
    missing = [r for r in results if r["verdict"] in NEEDS_ACTION]

    if not apply:
        # Read-only drift report. Non-zero exit iff anything needs action or is blocked.
        return results, (1 if (blocking or missing) else 0)

    # --apply: refuse to touch the box at all if anything is blocking (C3-b fail-loud).
    if blocking:
        for r in blocking:
            _log(f"BLOCKED {r['verdict']} {r['account']}: {r['detail']}")
        _log("refusing to apply — resolve conflicts/drift first; no account was mutated.")
        return results, 2

    by_name = {s.name: s for s in specs}
    for r in missing:
        spec = by_name[r["account"]]
        ops.create_account(spec)
        r["applied"] = True
        _log(f"created {spec.name} uid={spec.uid} home={spec.home} shell={NOLOGIN} (locked)")
    return results, 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="READ-ONLY drift report (default); non-zero exit on drift. Never mutates.")
    mode.add_argument("--apply", action="store_true",
                      help="create MISSING fleet accounts (idempotent); fail-loud on conflict/drift.")
    ap.add_argument("--exec", default="",
                    help="exec prefix to route account ops onto a target host, e.g. "
                         "'ssh pve2 pct exec 202 --' (default: local host)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    exec_prefix = shlex.split(args.exec) if args.exec else []
    ops = Ops(exec_prefix=exec_prefix)
    results, code = reconcile(ops=ops, apply=args.apply)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        target = args.exec or "local"
        _log(f"target: {target}  mode: {'apply' if args.apply else 'check'}")
        for r in results:
            mark = " [APPLIED]" if r.get("applied") else ""
            _log(f"{r['verdict']:<8} {r['account']:<16} {r['detail']}{mark}")
        # Actionable remedy on a failing --check (the CI pre-job hook, T-2219): name the
        # exact fix so a red job reads as one line, not 26 buried getpwnam KeyErrors.
        if code != 0 and not args.apply:
            n_missing = sum(1 for r in results if r["verdict"] in NEEDS_ACTION)
            n_block = sum(1 for r in results if r["verdict"] in BLOCKING)
            exec_suffix = f" --exec {shlex.quote(args.exec)}" if args.exec else ""
            if n_block:
                _log(f"DRIFT: {n_block} blocking (CONFLICT/DRIFT) + {n_missing} missing. "
                     "REMEDY: resolve the blocking account(s) by hand first — --apply REFUSES "
                     "while any conflict/drift stands (it never clobbers).")
            else:
                _log(f"DRIFT: {n_missing} fleet account(s) MISSING on this host. REMEDY: provision "
                     f"as root:  python3 tools/ci_fleet_account_reconcile.py --apply{exec_suffix}")
        _log(f"exit {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
