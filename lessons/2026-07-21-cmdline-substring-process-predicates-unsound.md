# Cmdline-substring process predicates are unsound — never `pgrep -f`/`pkill -f` as liveness, count, or kill logic

**Date authored:** 2026-07-21 (T-1916, ORG-012 sweep promotion — criterion 1, third independent incident in ~2 months, three unrelated contexts)
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015)
**Re-evaluate when:** the repo-script lint lands (structural enforcement retires the prose rule to a pointer); or a fourth incident shows a failure mode the sound-predicate list below doesn't cover.

## The pattern

A script needs a process predicate — "is X running," "how many X," "kill X" — and reaches for command-line substring matching (`pgrep -f`, `pkill -f`, `ps | grep <string>`). The predicate is unsound in both directions:

- **False positives:** `-f` matches the full cmdline of *every* process — including the operator's interactive shell that typed the string, the predicate's own grep pipeline, an unrelated daemon whose argv happens to contain the token, and compiler cmdlines carrying the string in dep paths or `--extern` flags.
- **False negatives:** the process you care about can carry *none* of the expected string in its argv (`cargo test` run with cwd inside the worktree has no worktree path in its cmdline).

Both directions shipped real damage:

1. **Kill/count context** — `bugs/pgrep-x-process-name-not-pgrep-f-cmdline-daemon-count.md` (T-0557, 2026-05-22): `pkill -f` killed the operator's own shell; `pgrep -f` counts inflated by the observer, causing a phantom duplicate-daemon respawn saga.
2. **Build-liveness guard** — `bugs/build-liveness-guard-cmdline-grep-false-positives-use-proc-cwd.md` (2026-07-10): cmdline-grep false-positived twice (dep-path matches + its own pipeline), aborting a safe emergency disk reclaim; the same shape can also MISS real builds (cwd-relative invocation).
3. **Watchdog activity-predicate** — `bugs/watchdog-pgrep-pattern-matches-unrelated-daemons.md` (2026-07-16): the pattern matched the Telegram plugin's bun processes AND its own command line, leaving the stall alarm structurally blind for 24h while the watched worker was dead — a wrong-layer-GREEN sibling (`lessons/2026-07-21-wrong-layer-green-liveness-checks.md`): the predicate layer read alive while the capability was gone.

## The rule

**Never use cmdline-substring as a process predicate.** Sound predicates, in preference order:

1. **systemd unit state** (`systemctl is-active <unit>`, `systemctl show -p MainPID`) — when the process is managed, the manager is the ground truth (mind the `active (exited)` oneshot trap from the wrong-layer-GREEN lesson).
2. **PID files / recorded PIDs** + `/proc/<pid>` verification (comm, cwd, uid) — assert identity from kernel ground truth, not argv.
3. **`pgrep -x <name>` / `pkill -x <name>`** — exact process-name match when name is discriminating.
4. **`/proc/<pid>/cwd` bucketing** for per-worktree/per-instance questions (`readlink /proc/$pid/cwd`; empty readlink = other-uid, treat as not-yours).

If a substring match feels unavoidable, the predicate is under-specified — find the discriminating fact (unit, pid, cwd, uid) and test that instead.

## Structural follow-up

Directive-tier was considered at promotion time (the rule is lintable: flag `pgrep -f`/`pkill -f` used as predicates in repo scripts). Taken as lessons-tier floor at promotion; the lint was ticketed separately.

**LANDED (T-1930, 2026-07-22) — enforcement is now structural (P1), not prose.** `tools/pgrep_predicate_lint.py` flags cmdline-substring predicates across repo scripts, with a live-tree test (`tools/test_pgrep_predicate_lint.py`, T-1309 precedent) that turns a new one red. The re-evaluate trigger above has fired: this section is the record of it.

What the lint deliberately does NOT flag, because each is a real construct here and flagging it would make the guard noise: `pkill -P` (parent-PID — kernel ground truth, one of the *sound* predicates preferred above, used correctly four times in `tools/handoff_and_restart.sh`); `pgrep -x` (exact name, also sound); the word "ripgrep", which contains the literal substring `pgrep`; and prose/comments describing the trap — including this lesson.

Allowlist entries are `(path, reason)` pairs, curated deliberately and never auto-derived (the T-1945 rule). The justified shape is `-u` uid-scoping **plus** a specific binary path, for a process that has no systemd unit and no discriminating process name — a tmux-spawned agent session being the canonical case, where `pgrep -x` would match every `node`/`codex` on the box.

One entry is a **known defect rather than a justification**: `tools/ho_resumer.py` gates HO recovery on an *unscoped* `pgrep -f "claude.*--resume"` on a box with nine agent homes, so another account's `claude … --resume` can make the resumer decline to resume the orchestrator. It is allowlisted with a `KNOWN VIOLATION … tracked T-1956` reason purely so the guard could land born-green; the reason field is what keeps that distinguishable from a blessing, and the entry is removed when T-1956 fixes it.
