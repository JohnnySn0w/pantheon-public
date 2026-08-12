# A real-account wet-run can only restore the behavior-changing state, not the whole tree

**Date:** 2026-08-08 · **Author:** Orchestrator (HO) · **Status:** Authored (`lessons/` is a
non-ratifiable class per ORG-015)
**Origin:** T-2599 — validating codex's model-migration-modal dismissal required a real-surface
wet-run, and the modal is only reachable against a real authenticated account (a hermetic
CODEX_HOME with a synthetic JWT can't render it — the modal reads a live authed RPC,
`bootstrap.available_models`, and a fake token gets HTTP 451). So the acceptance gate could
only be closed against the real `/home/claude/.codex`. I set a guardrail — "prove the whole
`~/.codex` byte-identical before==after" — that turned out to be both over-set and unachievable.
**Re-evaluate when:** a real-CLI tool ships a genuine transactional "dry-run / no-side-effect"
launch mode, which would make whole-tree byte-identical achievable and this lesson narrower.

## The rule

> **When a wet-run must run against a real account (because a hermetic home can't reach the
> surface), the achievable and correct restore standard is: snapshot + restore the
> BEHAVIOR-CHANGING state, and accept the launch's ordinary self-maintenance as the cost of a
> real-surface proof.** Do NOT set "restore the whole tree byte-identical" as the bar — a real
> CLI launch inherently does non-reversible startup housekeeping (skill/plugin/cache sync,
> session/state DB writes) that is *not* damage and *not* reversible, and chasing it compounds
> churn rather than cleaning up.

## Why

- **Behavior-changing vs housekeeping is the load-bearing distinction.** For T-2599 the
  behavior-changing state was exactly two files (`config.toml`'s migration-ack + default model,
  and `models_cache.json`) — the only state that would change how the account behaves for the
  next user or suppress a future proof of the same surface. Those were snapshotted and restored
  byte-identical (sha256-verified). Everything else that moved (a `skills/.system/` doc sync,
  five pruned *stale* plugin-cache dirs, two refreshed MCP-apps caches, four session sqlite DBs)
  was ordinary codex-startup self-maintenance.
- **The housekeeping isn't damage.** Pruned stale cache is self-healing (re-fetched on demand);
  reconstructing it from a registry would *re-add* entries the tool deliberately pruned — worse,
  not better. Session-state DB writes truthfully record that a session happened; fabricating a
  "no session" state is less accurate, not more.
- **It matches accepted precedent.** The existing real-cli benches (T-2572/T-2596) already
  launch real codex against this same home and leave the identical housekeeping footprint
  *without restoring anything*. A whole-tree-byte-identical bar is stricter than the practice
  the fleet already accepts — so it's the wrong bar to hold a wet-run to.

## How to apply

1. **Before the run:** identify the behavior-changing files (the ones that alter how the account
   behaves or would suppress re-proving the surface) and take **full byte backups** of *those*.
   Whole-tree hashes are fine for *detecting* what moved, but a hash can't restore — only a byte
   backup can, so back up only what you'll actually need to restore.
2. **After the run:** restore the behavior-changing files, verify byte-identical (sha256), and
   **enumerate the residual** honestly — name what moved and why it's ordinary self-maintenance,
   rather than claiming a clean whole-tree restore you can't prove.
3. **Dispatcher side:** don't set "restore the whole tree byte-identical" as a guardrail for a
   real-CLI wet-run. Set "restore the behavior-changing state + enumerate the residual." (This
   lesson exists because I set the over-strong bar; the implementer correctly couldn't meet it
   and paused honestly rather than round up.)

## See also

- `lessons/2026-08-06-subagent-negative-finding-is-a-prompt-not-a-result.md` — the sibling
  discipline the same arc exercised: the hermetic-infeasibility was proven by *running* it
  (HTTP 451), not argued.
- `bugs/harness-swap-bench-second-claude-live-poller-blank-pane.md` — the hermetic-home pattern
  for the surfaces that *can* be reached hermetically (login/trust/update modals), vs this
  server-auth-gated one that can't.
