---
symptom: "A unit whose failure discriminator is a 'consecutive soft failures' streak (page only at N in a row) pages on a SINGLE transient blip: systemd shows `status=6/NOTCONFIGURED` (or whatever the hard code is) after a run of clean-looking fires, and the seat self-recovers on the very next fire. Recovery's count reads '1 failed / 500 ok'. The streak state file holds a count far above N (specialist 7, metis 3, recovery 35 on 2026-08-25) with an mtime equal to the page."
fix: "The streak only clears on the FULL success path, and the seat's steady state never reaches it (every fire returns early via a policy skip such as RuntimeViabilityBlocked / backoff / quota-open, which neither bumps nor clears). Count per STAGE and clear each stage's counter when THAT stage recovers (T-3209, PR #346: diagnostic-class counter cleared whenever diagnostics pass; attempt-stage counter cleared only on genuine consume success). Never add the hard code to SuccessExitStatus (it guts the discriminator). Reset leaked state files by deleting them; the file is recreated on the next bump."
domain: "pantheon, persona-consume, systemd, alerting, cry-wolf, streak, false-positive, runtime-controller"
---

## Symptom

`pantheon-persona-consume@<persona>.service` exits `6/NOTCONFIGURED` once every few hundred fires and pages Recovery via `OnFailure`, although the two preceding fires were clean and the next fire is clean again. "NOTCONFIGURED" is only systemd's LSB label for exit status 6 — it is `EXIT_SOFT_PERSISTENT` in `tools/persona_bringup.py`, the T-2586 discriminator that is supposed to fire only after `SOFT_FAILURE_ALERT_STREAK = 3` consecutive soft failures.

Two readings that both looked plausible and were both wrong: (a) "a single check failure short-circuits to exit 6" — there is no such path, the code bumps a streak and returns 6 only at ≥ 3; (b) "there must have been three failing fires in the ~9 minutes before the page" — the journal showed the preceding fires passed the check.

## Cause

The streak is cleared ONLY at the end of the full consume/wake path (`clear_soft_streak` after a genuine success). On seats whose steady state is a **policy skip** — every fire on specialist and metis returned via `RuntimeViabilityBlocked` (exit 5, no bump, no clear), the same shape exists for backoff-skip, `RuntimeTransitionPending`, `ProviderQuotaExhausted` — the clear point is never reached. So each isolated soft blip is a permanent +1. The blip itself was the `claude.executable` diagnostic flipping during the ~7-second npm auto-update window (dangling symlink; see `claude-code-auto-update-transient-native-binary-not-installed.md`), which the 2-minute timer hits about once per 500 fires. Once the count passed 3, EVERY blip paged — and the "consecutive" promise was silently false for weeks.

Generalisation: any health streak that is only cleared by the *terminal* success of a multi-stage pipeline leaks on every seat where an *intermediate* stage legitimately short-circuits. "Consecutive" then means "consecutive among the fires that reached the end", which on a skip-heavy seat is none of them.

## Fix

Count per stage, clear per stage (T-3209, PR #346, squash `c971f3190`):

- `tools/persona_bringup.py`: `<persona>.diagstreak.json` (`load/bump/clear_diag_streak`) backs the diagnostic-fail path and is cleared as the first statement of the consume block — i.e. whenever diagnostics pass, regardless of what the attempt stage does next. The attempt-stage counter (`*_soft_streak`, exit 4/5) is unchanged: cleared only on genuine success, preserved across backoff skips (that preservation is deliberate — `test_backoff_skip_preserves_streak` — a sustained transport outage must still page).
- Do NOT add the hard code to the unit's `SuccessExitStatus` — that removes the only page a sustained failure produces (`test_unit_success_exit_status_matches_soft_codes` pins the list).
- Do NOT age the streak by wall clock: backoff delays reach 6 h, so a window short enough to forgive a stale blip is short enough to forgive a real outage under backoff.
- Leaked state: delete the stale `*.softstreak.json` files (the loader treats absence as 0; the file is recreated on the next bump). A fleet sweep is one loop over `/home/<acct>/.local/state/pantheon/personas/`.

## Reproduction evidence (2026-08-25)

- `journalctl _UID=<seat>` for `pantheon-persona-consume@metis`: 27/27 fires in 08:50–09:30Z `"skipped": "runtime_viability_blocked"`; exactly one `status=6/NOTCONFIGURED` at 09:22:04Z whose diagnostics carried `"check_id": "claude.executable", "status": "fail"`; the fires at 09:16 and 09:19 carried `"status": "pass"` for the same check.
- `cat /home/suborch1/.local/state/pantheon/personas/metis.softstreak.json` → `{"count": 3}`, mtime 09:22:04.918Z (the page); unchanged after the clean 09:23/09:25 fires. Same on specialist (`{"count": 7}`, mtime 05:58:05Z) and recovery (`{"count": 35}`, mtime 2026-08-21).
- Failing-first test: `python3 -m unittest tools.test_harness_runtime.ConsumeSoftFailureAlertStreak.test_leaked_streak_isolated_diag_blips_do_not_page` → `AssertionError: 6 == 6` on the pre-fix wiring, `OK` after.
