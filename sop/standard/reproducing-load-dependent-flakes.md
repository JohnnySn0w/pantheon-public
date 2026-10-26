# Reproducing a load-dependent flake on the shared host

**Status:** Adopted 2026-09-30 by Recovery (T-4349), under P1 (architecture over policy: the helper makes the confined way the easy way) and D12 (structural engineering over wall-clock). Methodology-tier, so no ratify-ask per amended D3. It goes in the digest.

## When to use this

A test fails only under CPU contention (a CI flake, a timing race, a watchdog that fires "when the box is busy"), and you want to reproduce it by generating load **on the fleet VPS**. That host has 4 cores shared by every seat: orchestrator's merges, every Telegram bridge, every mailbox wake.

## Procedure

1. Use the helper. It pins N burners **and** the command to one CPU, so the command gets exactly the contention of a saturated box while the other cores stay free:

   ```
   tools/confined_load.sh -n 3 -t 600 -- python3 -m pytest tools/test_x.py -k flaky_case
   ```

   `-c CPU` picks the core (default: the last one), `-n` is 1-8 burners (default 3), and `-t` caps each burner at 1-1800 s (default 600). The exit status is the command's. Burners die when the command exits, when the helper is interrupted, and (via their orphan check) even when the helper is SIGKILLed.

2. For N repeated runs, loop **inside** one helper call so the burners persist across runs:

   ```
   tools/confined_load.sh -n 3 -t 900 -- bash -c 'for i in $(seq 20); do python3 -m pytest tools/test_x.py -q || echo "FAIL run $i"; done'
   ```

3. More burners on the same core means heavier contention for the test. Raise `-n` before you ever consider more cores.

## Common failure modes

- **Full-host burners.** Unpinned `python3 -c 'while True: pass' &` in a loop, or `timeout 3600` burners. On 2026-09-30, 16 of them drove load to 35 and CPU PSI to 96% across the whole fleet until Recovery killed them (T-4349). Never do this on the VPS.
- **Hand-rolled cleanup via a `timeout` wrapper.** When cleanup TERMs `timeout` mid-fork, it can exit without signalling its child. That leaked uncapped burners in 2 of 40 fast runs. The helper's burner caps itself instead.
- **`pkill -f '<pattern>'` from the same shell.** It matches your own command line and kills your shell (exit 144). Bracket the first character, e.g. `'[w]hile True'`.

## Boundary

- The helper is for load **on the VPS**. The CI host (pve2 CT202) is disk-seek bound, not CPU bound. See the T-4344/T-4084 notes before reproducing CI-only flakes there.
- If a flake genuinely needs multi-core contention (e.g. a lock across CPUs), ask Recovery for a window rather than saturating the host.

## Cross-references

- `tools/confined_load.sh`, `tools/confined_load_burner.py`, `tools/test_confined_load.sh`
- T-4349 (the incident and the design), T-4287 (false wake failures caused by CPU pressure)
