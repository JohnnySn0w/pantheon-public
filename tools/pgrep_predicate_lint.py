#!/usr/bin/env python3
"""pgrep_predicate_lint — flag cmdline-substring process predicates in repo scripts.

Moves lessons/2026-07-21-cmdline-substring-process-predicates-unsound.md from
prose to structural enforcement (T-1930, deferred there by the T-1916 promotion
which took the lessons tier as its conservative floor).

THE CLASS. `pgrep -f` / `pkill -f` match against a process's full command line,
so the predicate answers "does this string appear in some process's argv" when
the question was "is MY process running". Those diverge in three directions,
each of which has cost the fleet an incident:

  - FALSE POSITIVE, unrelated process — a substring that appears in another
    account's or another tool's cmdline (T-0557 pkill-own-shell; the 2026-07-10
    build-liveness guard matching dep paths).
  - FALSE POSITIVE, self-match — the probing shell's own argv contains the
    pattern literal, so the predicate matches itself. Reproduced live during
    T-1930: `pgrep -af "claude.*--resume"` typed at a shell matches that
    shell. (The classic workaround is a character class: `clau[d]e`.)
  - FALSE NEGATIVE — the discriminating string simply is not in the cmdline
    (a `cargo test` run from inside a worktree carries no worktree path), so a
    live process reads as absent and the watchdog goes blind (2026-07-16, 24h).

Sound predicates, per the lesson's preference order: systemd unit state; a
recorded PID verified through /proc; `pgrep -x` exact-name match; /proc/<pid>/cwd
bucketing.

WHAT IS DELIBERATELY NOT FLAGGED (each is a real construct in this repo, and
flagging any of them would make the lint noise):
  - `pkill -P <pid>` — parent-PID based. That is kernel ground truth, i.e. one
    of the SOUND predicates the lesson prefers. tools/handoff_and_restart.sh
    uses it four times, correctly.
  - `pgrep -x` / `pkill -x` — exact process-NAME match, also sound.
  - The word "ripgrep", which CONTAINS the substring "pgrep" (r-i-`pgrep`).
    tools/memory_search.py trips a naive scan; word-boundary matching is
    mandatory here, not stylistic.
  - Comments and docstrings — prose describing the trap (including this file
    and the lesson) must not be flagged as committing it.

ALLOWLIST. Curated deliberately, never auto-derived (the T-1945 rule). The
justified shape, shared by every current entry: `-u <user>` scoping PLUS a
specific binary path, for a process that has no systemd unit and no
discriminating process name (a tmux-spawned agent session is the canonical
case — `pgrep -x` would match every node/codex on the box). Scoping to one uid
and one absolute binary path makes a false match negligible. Add an entry only
with a reason that meets that bar; "it is existing code" is not a reason.

A KNOWN-VIOLATION entry (a defect parked so a guard can land born-green,
NOT a justification) must additionally carry a machine-readable tracker
marker `tracked: T-NNNN`. The marker is parsed, not merely pattern-matched:
a test fails if that ticket has already RESOLVED while the entry survives,
so a debt cannot outlive its tracker and quietly become permanent (T-1958;
lessons/2026-07-22-allowlist-needs-its-own-guard.md). A bare "T-NNNN"
elsewhere in the prose is deliberately NOT treated as the tracker — a reason
that mentions some other (possibly long-resolved) ticket in passing must not
trip the check.

IF YOU ARE WRITING A FAKE-PROCESS FIXTURE to test a predicate (yours or a
replacement for one this lint flagged), read
`bugs/bash-c-tail-call-exec-replaces-argv-in-fake-process-fixtures.md` first.
`bash -c 'sleep 600' <marker-args>` is invisible to `pgrep -f`: a single simple
command triggers bash's tail-call exec, bash replaces its own process image and
the marker args vanish from /proc/<pid>/cmdline — so the regression test goes
VACUOUS, passing while asserting nothing. Make the script compound
(`bash -c 'sleep 600; :'`) to keep bash resident with its original argv. Noted
here because the failure looks like a green test, which is the same class of
trap this lint exists to catch.

Report-only / read-only: scans committed sources and authors nothing — same
exemption class as the other tools/ lints.

Usage:
    python3 tools/pgrep_predicate_lint.py            # scan, exit 1 on findings
    python3 tools/pgrep_predicate_lint.py --quiet    # exit code only
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    from tools._tree_walk import walk_paths
except ImportError:  # bare run with cwd=tools/ (tools/ on sys.path, repo root not)
    from _tree_walk import walk_paths  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Files scanned: executable/script surfaces. Prose dirs are excluded wholesale —
#: a lesson describing the trap must not be flagged for quoting it.
SCAN_SUFFIXES = (".sh", ".py", ".service", ".timer", ".bash")

#: Test sources are OUT of scope, for two independent reasons. (1) The class
#: targeted here is a PRODUCTION process predicate; a fixture that spawns a
#: uniquely-marked `sleep` and then greps for it is a controlled process it owns,
#: and tools/test_ho_resumer.py asserts on the argv shape without invoking
#: anything at all. (2) Structural: this lint's OWN tests must contain synthetic
#: `pgrep -f` samples, so scanning tests would make the live-tree guard
#: unsatisfiable — the guard could never be green while the detector was tested.
TEST_PREFIXES = ("test_",)
SKIP_DIR_NAMES = frozenset({
    ".git", "lessons", "bugs", "notes", "knowledge", "incidents", "tickets",
    "adr", "reports", "archive", "sop", "rulebook", "pinned-rules", "skills",
    "promotion-candidates", "dispatches", "finances", "assets", "screenshots",
})

#: Path prefixes that are not this tree at all. `.claude/worktrees/` is where an
#: isolation-worktree agent checks out ANOTHER commit (gitignored); scanning it
#: lints stale code and turns any seat with such an agent red (T-4513).
SKIP_PATH_PREFIXES: tuple[tuple[str, ...], ...] = ((".claude", "worktrees"),)

#: (path relative to repo root, reason). Curated — see ALLOWLIST above.
ALLOWLIST: tuple[tuple[str, str], ...] = (
    (
        "tools/s3star_audit_runner.py",
        "T-5666 --await-audit: `pgrep -u codex -af 'codex --profile s3star-audit-p2'` "
        "detects the interactive profiled auditor, which is typed into a tmux pane "
        "(no systemd unit, no discriminating process name). uid-scoped + the "
        "audit-specific profile argv makes a false match negligible; the caller's own "
        "argv does not contain the pattern (pgrep excludes itself), and a probe "
        "ERROR (rc other than 0/1) fails closed, and the gate is read-only, so a "
        "wrong answer can only delay or hasten a human's landing decision.",
    ),
    (
        "tools/stage-echo-worktree.sh",
        "T-1932 auth-smoke wrapper: `pgrep -af 'cargo|rustc'` feeds a WARNING "
        "LINE only — no behavior is gated on the match. The wrapper isolates "
        "the run's CARGO_TARGET_DIR regardless; the pgrep merely tells the "
        "operator that sibling builds will contend for CPU. A false positive "
        "costs one spurious stderr line; a false negative costs nothing (the "
        "isolation is unconditional). Broad name-match is therefore the RIGHT "
        "shape here: the lint's unsound-predicate concern applies to matches "
        "that gate decisions, and this one decides nothing. Caught by this "
        "lint's live-tree guard within the hour of landing (T-1934 triage) — "
        "the guard working as designed on its own author's commit.",
    ),
    (
        "tools/pantheon_oom_shield.sh",
        "T-1927 OOM shield: `pgrep -u <user> -f <binary-path>` identifies tmux-spawned "
        "agent SESSION processes, which have no systemd unit and no discriminating "
        "process name (`pgrep -x` would match every node/codex on the box). "
        "uid-scoped + absolute binary path makes a false match negligible.",
    ),
    (
        "harness/codex/codex_self_restart.sh",
        "T-4756 / ORG-091: imported VERBATIM from codex-config at T-4755 (C1); "
        "Consultant-owned runtime. `pgrep -u \"$(id -u)\" -f 'codex-linux-x64.*/bin/codex$'` "
        "is uid-scoped and anchored on the codex binary path suffix, the same shape "
        "as the pantheon_oom_shield.sh entry above, so a false match is negligible. "
        "It is listed per-file, not as a harness/ skip, so the rest of harness/ stays "
        "linted. Re-examined when T-4750 C4b reworks the codex runtime.",
    ),
    (
        "tools/ho_resumer.py",
        "T-1956 fix landed: `pgrep -u <uid> -f \"claude.*--resume\"` scoped to "
        "os.getuid(). This script only ever runs as the HO account (its own "
        "systemd user unit), so os.getuid() here IS the HO account's uid — "
        "killing the cross-account false positive this line used to have (nine "
        "agent accounts on this box each run `claude`; unscoped, any sibling's "
        "`claude … --resume` used to match and wrongly decline the HO's own "
        "resume). No binary-path anchor, unlike the other entries here: the npm "
        "`claude` wrapper execs a native binary target, so an absolute-path "
        "component in the pattern would be FRAGILE against that wrapper-vs-"
        "native cmdline shape, not just unnecessary. Accepted residual: on the "
        "HO's OWN account, any process whose cmdline happens to match the "
        "regex — e.g. a stray `grep`/`journalctl` invocation quoting the "
        "literal pattern string — still matches. That's tolerable because the "
        "decline path bails WITHOUT rotating the state file, so a transient "
        "same-account false positive costs exactly one 5-minute poll retry and "
        "self-heals; the defect this fixes was the PERSISTENT cross-account "
        "block, not a same-account regex self-match.",
    ),
    (
        "tools/systemd/recovery-telegram-child-watchdog.sh",
        "Same justified shape, twice: `pgrep -u $(id -u) -f` against a specific "
        "npm-global claude path and a specific `bun run --cwd` path. uid-scoped to the "
        "invoking account, and both patterns carry an absolute path component.",
    ),
    (
        "tools/recovery_boot_kick.sh",
        "T-2453 recovery boot-kick: `pgrep -u \"$(id -u)\" -f "
        "\"handoff_and_restart.*${PANE%%:*}\"` — uid-scoped to the invoking recovery "
        "account, pattern anchored on the handoff_and_restart helper AND the "
        "specific pane id, so a false match is negligible; the script's own argv "
        "does not carry that pattern, so no self-match. Pre-existing since T-2453; "
        "allowlisted by Orchestrator 2026-08-05 on discovery via the T-2572 full sweep "
        "and surfaced to Recovery. Same justified shape as the entries above.",
    ),
    (
        "tools/persona_cutover.sh",
        "T-2633 GENERIC extraction of tools/t2590_recovery_cutover.sh: three "
        "`pgrep -u \"$PCX_ACCOUNT\" -f`/`-af` checks (the soak-crit-4 claude-pid "
        "select, plus the T-2624 comms-dark diagnostic's best-effort bun-poller "
        "dump) carry the identical `\\.npm-global/bin/clau[d]e` "
        "no-second-claude-guard-avoiding bracket, and are uid-scoped via "
        "$PCX_ACCOUNT -- a variable, not a literal, because this script is "
        "persona-generic, but resolved from tools/persona_cutover_resolve.py's "
        "identity resolution before any of these lines run, so at execution "
        "time it is exactly as concretely scoped as the recovery original's literal "
        "claude-fallback. Same negligible-false-match reasoning as the recovery "
        "entry above; none of the three gate a destructive action (verify "
        "PASS/FAIL echo lines and a best-effort diagnostic capture only). Added "
        "by Orchestrator 2026-08-07.",
    ),
    (
        "tools/persona_rollback.sh",
        "T-2633 GENERIC extraction of tools/t2590_recovery_rollback.sh: two "
        "`pgrep -u \"$PCX_ACCOUNT\" -f` liveness checks run AFTER the service "
        "restart, same shape and same $PCX_ACCOUNT scoping rationale as the "
        "persona_cutover.sh entry directly above. Both only feed a verify "
        "PASS/FAIL(WARN) echo line and gate no destructive action. Added by "
        "Orchestrator 2026-08-07.",
    ),
    (
        "tools/consume_onboard.py",
        "T-2679 mailbox-first consume-onboarding tool: verify()'s "
        "runtime.channel_less_argv check runs "
        "`pgrep -u <account> -a -f \"\\.npm-global/bin/clau[d]e\"`, the identical "
        "justified shape as the persona_cutover.sh entry above -- uid-scoped to "
        "the seat's bound account (SeatContext.binding.account, resolved from "
        "instance.yaml before this call runs) AND anchored on the npm-global "
        "claude binary path, so a false match is negligible. Same probe, same "
        "reason: confirm the launched claude's argv carries no --channels flag, "
        "the defining property of a mailbox-first seat (SOP verification "
        "checklist item 3). Feeds only a verify PASS/FAIL line; gates no "
        "destructive action. This is a genuine justification, not a parked "
        "defect -- no `tracked:` marker per this file's own KNOWN-VIOLATION-only "
        "rule (~line 48-56); T-2679 is cited here for traceability only.",
    ),
)


#: Python invocation shape: the command and the flag are each their OWN quoted
#: argv element, as in `subprocess.run(["pgrep", "-f", PATTERN])`.
_PY_CMD_RE = re.compile(r"""["'](pgrep|pkill)["']""")
_PY_FLAG_RE = re.compile(r"""["']-(?!-)[a-zA-Z]*f[a-zA-Z]*["']|["']--full["']""")

#: Shell invocation shape: the command is a bare word and the flag is a bare token.
_SH_CMD_RE = re.compile(r"(?<![\"'\w-])(pgrep|pkill)\b")
_SH_FLAG_RE = re.compile(r"(?<=\s)-(?!-)[a-zA-Z]*f[a-zA-Z]*(?=\s|$|[\"'])|(?<=\s)--full\b")


def _is_comment(line: str, col: int) -> bool:
    """True if the match at `col` sits after a `#` on the same line."""
    return "#" in line[:col]


def scan_text(text: str, rel: str) -> list[tuple[int, str]]:
    """Return [(lineno, stripped_line)] for cmdline-substring predicate uses.

    Discriminates an INVOCATION from PROSE ABOUT one. That distinction is not
    cosmetic: this repo documents the trap heavily (the lesson, the bug entries,
    this file's own docstring and allowlist reasons), and a scanner that cannot
    tell a real argv list from a sentence merely mentioning the flag flags its
    own documentation — which is how a lint becomes noise and gets ignored.

    An invocation requires the command AND the -f-bearing flag to each be their
    own argv token: separately quoted in Python, bare words in shell. Prose
    embeds the whole phrase inside one string or comment, so it never qualifies.
    """
    is_python = rel.endswith(".py")
    cmd_re, flag_re = (_PY_CMD_RE, _PY_FLAG_RE) if is_python else (_SH_CMD_RE, _SH_FLAG_RE)
    findings: list[tuple[int, str]] = []
    for n, line in enumerate(text.splitlines(), start=1):
        for m in cmd_re.finditer(line):
            if _is_comment(line, m.start()):
                continue
            if flag_re.search(line[m.end():]):
                findings.append((n, line.strip()))
                break
    return findings


def scan(root: Path, allowlist: tuple[tuple[str, str], ...] = ALLOWLIST) -> list[tuple[str, int, str]]:
    """Return [(relpath, lineno, line)] for every non-allowlisted finding."""
    allowed = {p for p, _reason in allowlist}
    out: list[tuple[str, int, str]] = []
    # walk_paths, not rglob: tolerate entries vanishing mid-walk (T-4450).
    for path in walk_paths(root):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        parts = path.relative_to(root).parts
        if any(part in SKIP_DIR_NAMES for part in parts[:-1]):
            continue
        if any(parts[: len(prefix)] == prefix for prefix in SKIP_PATH_PREFIXES):
            continue
        if path.name.startswith(TEST_PREFIXES):
            continue
        rel = str(path.relative_to(root))
        if rel in allowed:
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in scan_text(text, rel):
            out.append((rel, lineno, line))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Flag pgrep -f / pkill -f used as process predicates."
    )
    ap.add_argument("--root", type=Path, default=REPO_ROOT, help="tree to scan")
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    args = ap.parse_args(argv)

    findings = scan(args.root)
    if args.quiet:
        return 1 if findings else 0
    if not findings:
        print(
            f"[pgrep_predicate_lint] clean — no cmdline-substring process predicates "
            f"outside the {len(ALLOWLIST)} allowlisted file(s)"
        )
        return 0

    print(
        f"[pgrep_predicate_lint] {len(findings)} cmdline-substring process "
        f"predicate(s) — these match argv, not identity:\n"
    )
    for rel, lineno, line in findings:
        print(f"  {rel}:{lineno}\n      {line}")
    print(
        "\nA cmdline substring answers 'does this string appear in some process's argv',\n"
        "not 'is my process running'. Prefer, in order: systemd unit state; a recorded\n"
        "PID verified via /proc; `pgrep -x <name>`; /proc/<pid>/cwd bucketing.\n"
        "See lessons/2026-07-21-cmdline-substring-process-predicates-unsound.md.\n"
        "If a use is genuinely justified (uid-scoped AND a specific binary path, for a\n"
        "process with no unit and no discriminating name), add it to ALLOWLIST with a\n"
        "reason meeting that bar."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
