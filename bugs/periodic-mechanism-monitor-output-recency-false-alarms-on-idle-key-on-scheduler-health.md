---
symptom: "A monitor that answers 'did this periodic MECHANISM stop?' by checking its OUTPUT recency — last git commit, last file mtime, last row written, `LastTriggerUSec` — false-alarms on a HEALTHY mechanism whose INPUT is idle. Most fleet periodic jobs emit only on change (snapshot scripts `git diff --cached --quiet` before committing; sync jobs write only deltas), so a firing timer on a quiet seat produces NO output and its output-recency ages past any threshold while nothing is actually wrong. Paging on it trains alert-blindness."
fix: "Key the health check on the SCHEDULER's liveness, not the mechanism's output. For a systemd timer: healthy iff ActiveState=active AND UnitFileState=enabled AND NextElapseUSecRealtime is present and near-future (within ~2x the cadence, not in the past beyond a grace). This is idle-seat-proof (a healthy idle timer still counts down) and directly detects the 'timer stopped' failure. Keep output-recency as CORROBORATION in the log + as the FALLBACK signal only when the scheduler can't be introspected (fail-open WARN). Do NOT use `LastTriggerUSec` as the signal — it resets to empty on user-manager restart (read empty for a long-healthy specialist timer 2026-08-11)."
domain: pantheon, monitoring, systemd, timer, false-alarm, liveness, cron
---

# Periodic-mechanism monitor: output-recency false-alarms on idle — key on scheduler health

## Symptom

You are building "detect if periodic job X silently stopped" (the T-1871 failure class:
archivist's memory snapshots quietly ceased ~2026-07-29 and nobody noticed for ~12 days —
a stopped periodic job reads as healthy to every process/liveness check; the only witness
is that its output stopped landing). The obvious check — "alert if the newest output
(commit / mtime / row) is older than N days" — is WRONG, because the mechanism emits only
on change:

- **False-alarm (the trap):** a healthy timer on an idle seat produces no output, so its
  output-recency ages indefinitely. **Live proof 2026-08-11:** archivist's in-place memory
  git HEAD was **13 days old** while its `archivist-state-snapshot.timer` was firing
  **hourly and healthy** (last fire 44 min prior) — the seat simply hadn't changed its
  memory since 07-29. A commit-age check at the ticket's suggested N=3 would have paged on
  a non-problem. Same shape for artisan/mason (memory-only snapshot seats) whenever quiet.

## The correct signal — scheduler liveness, not output recency

For a systemd timer, classify on the timer unit, which is reliably introspectable
cross-account and idle-seat-proof:

```
systemctl --user show <timer> -p ActiveState -p UnitFileState -p NextElapseUSecRealtime
# healthy iff: ActiveState=active AND UnitFileState=enabled AND
#   NextElapseUSecRealtime present AND within ~2x cadence AND not far-past (grace ~2h)
```

- A stopped/disabled timer → ActiveState≠active / UnitFileState≠enabled / next-fire
  absent or far-past → the real "it stopped" signal, with zero idle-seat false-positive.
- **Do NOT key on `LastTriggerUSec`** ("when did it last fire"): it RESETS to empty when
  the account's user-manager restarts, so it reads empty for a long-healthy timer
  (observed empty for specialist + artisan 2026-08-11 despite both firing fine). `ActiveState`
  / `UnitFileState` / `NextElapseUSecRealtime` survive manager restarts; key on those.
- Keep output-recency (last commit age) as **corroboration** in the log and as the
  **fallback** signal only when the scheduler can't be read at all (fail-open: WARN, don't
  hard-page, and don't silently pass). Cross-account systemd reads go via sudo +
  `XDG_RUNTIME_DIR=/run/user/$(id -u)`.

## Verification

`tools/memory_snapshot_coverage_monitor.py --all` (T-2838) → 6/6 seats HEALTHY including
archivist (timer healthy, HEAD 13d) — the exact case output-recency would false-fire. Its
`test_a...` sibling `tools/test_memory_snapshot_coverage_monitor.py` pins
`test_idle_seat_stale_head_but_healthy_timer_is_HEALTHY`.

## Related

- T-2838 (this monitor), T-2736/T-2424 (the snapshot mechanism it covers), T-1871 (the
  archivist rot that motivated it).
- `tools/credential_expiry_monitor.py` — the structural template (per-item classify +
  throttle + always-exit-0), and itself an instance of the same principle: it deliberately
  does NOT alert on access-token near/post-expiry (routine auto-renew, would page nightly)
  — it keys on the actionable NO_REFRESH/MALFORMED signal, not the noisy recency one.
- `bugs/build-liveness-guard-cmdline-grep-false-positives-use-proc-cwd.md`,
  `bugs/watchdog-pgrep-pattern-matches-unrelated-daemons.md` — sibling "monitor keyed on
  the wrong observable" gotchas.
