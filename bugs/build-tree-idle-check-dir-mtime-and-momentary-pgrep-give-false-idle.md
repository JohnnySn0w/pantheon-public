---
symptom: "Before reclaiming another agent's build cache (a Rust `target/`, a `.cache/*-target`) under disk pressure, you 'verify it's idle' with (a) the top-level directory's mtime and (b) a one-shot `pgrep cargo|rustc`. BOTH give FALSE-IDLE. A dir mtime does not change when a build writes files *inside* it (incremental artifacts land in `target/<profile>/…`, leaving `target/`'s own mtime stale for days), and a momentary pgrep misses a live-but-paused build session (between compiler invocations, or an agent mid-arc that will resume). Acting on the false-idle clobbers active work — a `cargo clean` forces a ~20min full rebuild of a live agent's in-progress build."
fix: "Idle = BOTH (1) no recent writes ANYWHERE in the tree — `find <tree> -type f -newermt '-6 hours' -print -quit` empty (or check the newest inner file: `find <tree> -type f -printf '%T+\\n' | sort | tail -1`), NOT the dir's own mtime; AND (2) the OWNING SESSION is not live — process `etime` + the agent's transcript mtime, not a one-shot cargo/rustc pgrep. For ANOTHER agent's project, these checks are necessary but NOT sufficient: coordinate-first (propose the reclaim to the owner / its dispatcher; one-actor-per-emergency) rather than auto-cleaning. Reserve unilateral cleaning for YOUR OWN regenerable caches."
domain: pantheon, disk-pressure, disk-reclaim, build-liveness, cargo, coordination
---

# Build-tree idle check: dir mtime + momentary pgrep give false-idle

## Symptom

Disk-pressure triage (Recovery's lane) needs to know whether a big build cache is safe to
reclaim. The tempting "idle" proof is fast and WRONG:

1. **Directory mtime** — `stat -c %y /home/suborch2/echo-prismatic/target` → `2026-08-09`,
   read as "untouched for 2 days." But a build writing incremental artifacts into
   `target/debug/…` does NOT bump `target/`'s own mtime. **Live case 2026-08-11:** that
   same target's *newest inner file* was `18:04 today` (voice_inject incremental
   artifacts) while its dir mtime was 2 days stale.
2. **One-shot pgrep** — `pgrep -u suborch2 cargo|rustc` → empty, read as "no build."
   But the owning agent's session was LIVE (process etime 2d21h, transcript mtime
   *minutes* ago) — a build paused between invocations / an agent mid-arc that resumes.

Both signals said "idle"; the 28G was an **active incremental build cache**. A reclaim
would have forced a ~20min full rebuild of live work.

## The correct idle check

- **Recent writes anywhere in the tree**, not the dir mtime:
  `find <tree> -type f -newermt '-6 hours' -print -quit` (non-empty ⇒ NOT idle), or read
  the newest inner file: `find <tree> -type f -printf '%T+ %p\n' 2>/dev/null | sort | tail -1`.
- **Owning-session liveness**, not a one-shot pgrep: the owner process `etime` (still up?)
  + the agent's transcript/home mtime (active *now*?). A paused build is still a live session.
- **For another agent's tree, coordinate-first regardless.** Even a genuinely-idle cache on
  a peer's project: propose the reclaim to the owner or its dispatcher (one-actor-per-
  emergency), don't auto-clean. Unilateral cleaning is for your OWN regenerable caches.

## Verification / what saved it (2026-08-11)

Recovery asserted "idle-verified" on dir-mtime + momentary-pgrep and armed a 92%-FS fallback to
auto-reclaim. It never fired — **coordinate-first** (proposed to orchestrator instead of cleaning) +
a **de-escalation stand-down** (the driver build stopped, so the fallback disarmed) + orchestrator's
**independent verify** (it checked the newest-inner-file + session liveness and caught the
false-idle) prevented clobbering an active agent. The disciplines are the fix; the check is
the lesson. Ref T-2710.

## Related

- `bugs/build-liveness-guard-cmdline-grep-false-positives-use-proc-cwd.md` — the *process*-side
  sibling (cmdline-grep vs `/proc/pid/cwd`); this entry is the *tree/cache*-side idle check.
- `bugs/periodic-mechanism-monitor-output-recency-false-alarms-on-idle-key-on-scheduler-health.md`
  — same shape of error inverted: there, output-recency false-*alarms* on a healthy idle
  mechanism; here, a stale dir-mtime false-*clears* an active one.
- T-2710 (the disk-reclaim ticket + this near-miss), `tools/disk_usage_monitor.py`.
