---
symptom: "A watchdog stall alarm never fires - its pgrep -f activity predicate matches standing daemons and the watchdog's own command text, so the lane always reads busy"
fix: "Prefer artifact evidence (file mtimes, commit presence, log growth) over pgrep; if process-matching is unavoidable use the narrowest discriminating string with a character-class self-match guard, and test the predicate's zero state before arming"
domain: pantheon, watchdog, monitoring, pgrep, silent-failure
---
# Watchdog pgrep activity-predicate matches unrelated daemons → stall alarm structurally blind

**Date:** 2026-07-16 (discovered after a 24h silent gap)
**Severity:** high (silent-failure class — the exact class watchdogs exist to catch)

## What happened

HO armed a worker-lane watchdog (Monitor tool, persistent) whose activity predicate was:

```
act=$(pgrep -u claude -c -f 'rustc|cargo (build|test|clippy)|vitest|bun')
```

Intended: "any build/test process running → worker is alive." Actual: the pattern permanently matched (a) the Telegram plugin's two long-running `bun` processes (`bun run … telegram … start`, `bun server.ts` — up for days by design), and (b) the watchdog's OWN bash command line, which contains the literal pattern text (`pgrep -f` matches full command lines, including the shell that embeds the regex in its argv).

Result: `act >= 2` forever → the quiet-counter never accumulated → the 20-min STALL alarm could never fire. The worker (impl-t1745) died silently at 18:45Z Jul 15; the watchdog reported nothing for 24 hours. The session only woke when the ticket auto-consumer's lease-retry re-delivered an unrelated ticket.

Compounding false positive at diagnosis time: a manual `pgrep -c -f 'rustc|cargo…|bun'` health-check read "5 processes running" — all five were the plugin's bun pair, the watchdog itself, and the diagnostic's own shell. "Build activity present" was fabricated by the measurement.

## The rule

1. **Never use broad `pgrep -f` name-matching as a liveness predicate.** Prefer artifact evidence: file mtimes in the worktree/target dir (`find <paths> -newermt '-N minutes' | head -1`), commit presence, or log-file growth. Processes lie (daemons share names; your own probe matches itself); artifacts don't.
2. If a process match is unavoidable: match the narrowest discriminating string (e.g. binary path `/.rustup/toolchains/.*/bin/rustc`), exclude your own pid family, and NEVER include a string that appears in a standing daemon's argv (`bun`, `node`, `python`) or in the watchdog's own command text (self-match: `pgrep -f` sees the embedded regex literal — the classic fix is a character-class wrapper, e.g. `rust[c]`, which matches the target but not the pattern string itself).
3. Test the predicate's ZERO state before arming: run it when you know the lane is idle — if it doesn't read 0, the alarm is dead on arrival. (Same shape as feedback pin "know the success-path signature before verifying negative claims from logs.")

## Cross-refs

- `feedback_watchdogs_must_be_harness_tracked` memory pin (harness-tracked is necessary but NOT sufficient — the predicate must also be sound; this incident is the sufficiency counterexample).
- bugs/subagent-idles-without-sending-final-report.md (the failure class the watchdog was compensating for).
- Ledger 2026-W29: entry `…-ho-24h-gap-watchdog-blind` (the incident record).

**Pattern lifted to lesson** (ORG-012 criterion 3, T-1916): `lessons/2026-07-21-cmdline-substring-process-predicates-unsound.md` (watchdog instance).
