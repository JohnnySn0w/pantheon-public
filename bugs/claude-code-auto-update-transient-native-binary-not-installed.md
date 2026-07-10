---
symptom: "A flood of `Error: claude native binary not installed` (with postinstall guidance: 'Run the postinstall manually... node node_modules/@anthropic-ai/claude-code/install.cjs') appears in output/journal, including bleeding into an unrelated command's output. Easy to mistake for a broken claude setup that needs recovery."
fix: "Usually NONE — it's the transient mid-update window of an unattended claude-code auto-update. Confirm with `claude --version` (and `claude mcp list`): if the binary runs and reports a version, the update completed and the errors were transient. Only intervene (`node <pkg>/install.cjs` to re-run postinstall) if `claude --version` STILL fails after the update window."
domain: "pantheon, claude-code, auto-update, false-positive-recovery-trigger"
---

## Symptom

`Error: claude native binary not installed` repeated many times, e.g.:

```
Error: claude native binary not installed.
Either postinstall did not run (--ignore-scripts, some pnpm configs)
or the platform-native optional dependency was not downloaded (--omit=optional).
Run the postinstall manually (adjust path for local vs global install):
  node node_modules/@anthropic-ai/claude-code/install.cjs
```

Observed 2026-06-07 ~05:52Z bleeding into the output of an unrelated `git pull` + grep command (i.e. the errors were NOT from the command being run — a concurrent `claude`-invoking process/timer hit the binary mid-update). The running HO session was unaffected (it kept producing results).

## Cause

**Unattended claude-code auto-update.** The npm-global `claude` symlink + `cli-wrapper.cjs` get recreated during an update (mtime jumps to the update instant). For the brief window while postinstall is running, the native binary / `claude.exe` target is momentarily absent, so any process that invokes `claude` during that window (a fired timer — keep-going, inbox-consumer, ho-resumer — or a manual call) prints the "native binary not installed" error. The window is seconds long; once postinstall finishes, the binary works.

Confirmed benign in the 2026-06-07 occurrence: immediately after the error flood, `claude --version` → `2.1.168 (Claude Code)`, `claude mcp list` showed telegram + pantheon-mcp-codex both ✓ Connected, and `systemctl --user show -p Result` for pantheon-ho-resumer / keep-going@orchestrator / inbox-consumer@orchestrator all reported `Result=success ExecMainStatus=0` (no timer actually failed).

## Fix

Diagnose-don't-panic. The symptom **looks** like the broken-setup signal Recovery's recovery domain watches for, but it is a **known false positive** when it coincides with an auto-update:

```bash
claude --version        # runs + reports a version  → update completed, errors were transient → NO action
claude mcp list         # MCP servers reconnected?   → confirm telegram/codex still ✓
```

Only if `claude --version` *still* errors after a minute (genuine half-installed state) run the postinstall manually:

```bash
node /home/claude/.npm-global/lib/node_modules/@anthropic-ai/claude-code/install.cjs
```

**Latent risk — ADDRESSED by T-0909 (2026-06-07):** if an auto-update's transient window coincides with a cap-hit moment when `pantheon-ho-resumer` fires, the resume could fail on the half-installed binary. The resumer used to rotate the cap-hit state file unconditionally after the attempt, so a transient failure consumed the state with no next-poll retry → HO stayed unresumed. T-0909 added one bounded in-poll retry on a non-zero exit (after a 30s sleep that outlasts a typical postinstall window) before rotating, capping re-fire at 1 (preserving the deliberate anti-hammer). See `tools/ho_resumer.py` (`RESUME_RETRY_SLEEP_SECS`) + `tools/test_ho_resumer.py`. Not observed in the wild. Sibling: `RECOVERY.md` (the broader broken-setup runbook).
