---
symptom: "A liveness guard of the form `pgrep -af 'cargo|rustc' | grep <worktree-or-ticket-string>` reports live builds that don't exist — it matches the string anywhere in a long compiler cmdline (dep paths, --extern flags) and can even match the guard's OWN grep pipeline — blocking safe emergency cache reclaims. Inverted risk too: a cargo invoked with plain `cargo test` and cwd inside the worktree carries NO worktree path in its cmdline, so cmdline-grep can also MISS real builds."
fix: "Resolve each candidate pid's working directory instead: `for pid in $(pgrep -d' ' 'cargo|rustc'); do readlink /proc/$pid/cwd; done` and bucket by worktree path. Empty readlink = other-uid process (e.g. the CI runner) — treat as not-yours. This is the required guard before ANY target/ deletion (merge-time cleanup or emergency reclaim)."
domain: pantheon, echo, cargo, disk-pressure, dispatch-pattern
---

# Build-liveness guard: cmdline grep false-positives — use /proc/pid/cwd

## Symptom

Two failure directions from the same naive guard (`pgrep -af "cargo|rustc" | grep X`):

1. **False-positive (blocks safe action):** the ticket/worktree string appears inside
   rustc dep paths or `--extern` args of an UNRELATED build, or the guard's own
   pipeline matches itself (`bash -c '... pgrep "cargo|rustc" ...'` contains the
   pattern). 2026-07-10 fleet-wedge: echo-fixarc's emergency reclaim of two idle 15G+
   targets was falsely aborted twice at 5.8G-free while disk was actively dropping.
2. **False-negative (permits unsafe action):** `cargo test` run WITH CWD in a worktree
   carries no worktree path in its cmdline at all — cmdline-grep declares the lane
   idle while it is mid-build.

## The correct guard

```
for pid in $(pgrep -d' ' "cargo|rustc"); do
  echo "$pid $(readlink /proc/$pid/cwd 2>/dev/null)"
done
```

Bucket by resolved cwd. Notes:
- Empty readlink ⇒ another uid's process (CI runner etc.) — not yours, not in your tree.
- Run it in the same breath as the deletion — a build can start between check and rm.
- Same-night validation: cwd-resolution showed only ONE of five worktrees genuinely
  building; the four "live" lanes flagged by cmdline-grep were all idle. Both HO and
  the orchestrator independently converged on this method during the same incident.

## Companion convention: one actor per declared emergency

During the same incident HO and the project-orchestrator deleted caches in PARALLEL
(overlapping lists, benign only because `rm -rf` is idempotent and the lists nearly
coincided). Standing rule: once a disk/resource emergency is DECLARED to the other
tier, exactly one actor executes deletions (declarer proposes, higher tier either
takes it or explicitly delegates); the other holds and re-polls. Prevents
double-delete races and split accounting of what was freed.

## Related

- `reference_pantheon_bugs_directory` (memory pin) — this file's discovery surface.
- T-1668 — structural disk-aware parallelism for multi-lane sprints (same night).
- T-1669 — exact-version rust-toolchain pin (the cold-rebuild multiplier that
  turned five warm caches into five concurrent 16G cold rebuilds).
- `feedback_active_team_worker_check` — the role-threshold clocks this guard refines.

**Pattern lifted to lesson** (ORG-012 criterion 3): `lessons/2026-07-21-cmdline-substring-process-predicates-unsound.md` (build-liveness instance).
