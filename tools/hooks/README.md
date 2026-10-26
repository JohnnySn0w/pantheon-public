# tools/hooks/

Claude Code session hooks owned by pantheon-config. Symlinked into `~/.claude/hooks/` at HO setup; the symlink pattern mirrors `claude-config/agents/` ↔ `~/.claude/agents/`.

## Install

For each script here, the user-scope runtime symlink is:

```bash
ln -sfn ~/pantheon-config/tools/hooks/<script>.sh ~/.claude/hooks/<script>.sh
```

Hook entries in `~/.claude/settings.json` reference the symlink path (`/home/claude/.claude/hooks/<script>.sh`), so future runs resolve through the symlink to the version-controlled source.

## Hooks

| Hook | Event | Purpose | Status |
|---|---|---|---|
| [`handoff-auto-read.sh`](handoff-auto-read.sh) | `SessionStart` | Auto-injects "Read /home/claude/HANDOFF.md and follow it" on fresh sessions when the handoff was modified within the last hour. No-op otherwise. Part of T-0008 deliverable 1. | shipped 2026-05-02 |
| [`backlog-surface.sh`](backlog-surface.sh) | `SessionStart` | Surfaces open BACKLOG.md items (B-ids) into the new session's `additionalContext` so fresh-HO sees the queue without grepping. Part of T-0011 / T-0023 (Tier-1 synthesis item 2). | shipped 2026-05-02 |
| [`prune-stale-teams.py`](prune-stale-teams.py) | `SessionStart` | Removes `~/.claude/teams/session-<id>/` dirs left by dead sessions (T-3074): candidate must not be the current session, have no file or directory under it modified within 6h, AND a matching transcript that is absent or stale. Conservative — any doubt (including an unresolvable current-session id) keeps the dir; symlinks skipped. Fail-open, reports pruned dirs to stderr, never blocks startup. Session id from stdin JSON, then `CLAUDE_CODE_SESSION_ID`. | shipped 2026-08-25 |
| [`recall-feedback.py`](recall-feedback.py) | `PreToolUse` (Edit/Write/MultiEdit/NotebookEdit/Bash) | Narrow high-precision footgun guard (T-1090, Dei recall narrowing): surfaces ONLY `feedback`/`project` memories whose **description** names an EXACT token in the tool args (file path, `T-NNNN`, `ORG-NNN`, or a multi-word command signature like `git stash`/`tailscale serve`). Read-only Bash commands skipped; per-session dedup; cap 2/call; fail-open. Injects a model-only "recalled feedback (verify it still applies)" `additionalContext` nudge. | **wired live on the claude seat** (`~/.claude/settings.json` PreToolUse entry, `"timeout": 5000`, `HOOK_ALLOWLIST`-registered; NOTE the hook `timeout` unit is SECONDS on Claude Code, not ms, so 5000 is ~83 min — corrected per orchestrator's T-4392 ratification note of 2026-09-30T07:04Z; firing verified 2026-08-25 — the ORG-073 §6 coexistence target for T-3006). Built + tested 2026-06-12. |
| [`worker-guard.py`](worker-guard.py) + [`worker_guard_impl.py`](worker_guard_impl.py) | `PreToolUse` (Bash/Edit/Write/MultiEdit/NotebookEdit/Read/Grep) | Subagent restriction guard: denies HO-exclusive ops (push/PR/merge, bot-token and protected-doc access) for any payload carrying an `agent_type`; the main thread is never restricted. `worker-guard.py` is a zero-import entry shim (T-4177): a payload with no `agent_type` text and no `\u` escape exits 0 before importing anything; everything else goes to `worker_guard_impl.py` (the former monolith body, byte-identical decisions). Proof: `tools/test_worker_guard_shim.py` + `tools/worker_guard_cutover_smoke.py` (prints the rollback command). | **wired live on the claude seat**; shim merged 2026-09-29 (#528, @57054d95c), live smoke PASS. Invoked as `python3 -S …/worker-guard.py` (T-4207: skipping `site` halves the fast path, 41→21 ms child CPU, and gives identical decisions over the 1416-case smoke corpus; the impl imports stdlib only). Other seats via Recovery (T-4194). |
| [`urgent-mail-inject.py`](urgent-mail-inject.py) | `PreToolUse` | T-1641 mailbox-severity Phase 2: recipient-side urgent-mail preempt. When the recipient's `inbox/.urgent_pending` marker is present, injects an URGENT `additionalContext` nudge at the next tool-call boundary (non-destructive mid-turn preempt — does not abort in-flight generation) and clears the marker (one nudge). Per-sender rate-limit downgrade + unconsumed-urgent escalation ladder live in [`../urgent_escalation.py`](../urgent_escalation.py). Fail-open, read-cheap. ENTIRELY gated behind default-OFF `PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED` (flag-off = byte-identical). codex has no PreToolUse parity → escalation+poller only (documented limit). | **built + tested 2026-08-05; NOT yet wired** (flag flip + settings.json wiring + HOOK_ALLOWLIST is the supervised-first-fire activation, gated on the operator) |
| [`_failclosed.py`](_failclosed.py) | (library) | Shared fail-closed runner for security-gating PreToolUse hooks (T-4392): exception / ambiguity / own 15s deadline counted from process start / input cap -> exit 2 deny; worker-untriggerable auto-expiring break-glass at /var/lib/pantheon-breakglass/<account>/flag (see the module docstring). Not a wired hook; imported from its own directory by mcp-list-guard.py (2a), background-pipe-gate.py (2c) and continuity_candidate_guard.py (2d). | shipped with T-4392 phase 2a (#622), wrapped hooks added in 2c (#625) and 2d (#630); row ratified by orchestrator 2026-09-30 |

## Why version-control hooks

A hook script is load-bearing infrastructure: if it disappears or drifts, fresh sessions silently regress. Storing the canonical copy in `pantheon-config/tools/hooks/` gives audit trail (git history), durability (lost VPS state recoverable from clone), and consistency across the three VPS users (claude / claude-fallback / claude-research) — each can install the same hook by checkout + symlink.

Failure modes the symlink pattern handles:
- HO's local `~/.claude/hooks/` was nuked: `git checkout` recreates the source, `ln -sfn` recreates the symlink.
- Drift between machines: `git pull` + smoke-test gates updates.
- Settings watcher caveat: the `~/.claude/settings.json` reference points to the symlink path; the watcher follows it.

## Git hooks (pantheon-config repo)

Distinct from the Claude Code session hooks above. These live in `.githooks/` (not `~/.claude/hooks/`) and activate via `tools/install_hooks.sh` setting `core.hooksPath = .githooks` per-clone. Each clone (Orchestrator's, Recovery's, Specialist's) needs the install run once.

| Hook | Event | Purpose | Status |
|---|---|---|---|
| `pre-commit` | `git commit` (pre) | Refuses commits whose staged blobs contain the unescaped HERMES byte sequence. Per `~/CLAUDE.md` §HERMES discipline. | shipped 2026-05-03 |
| `commit-msg` | `git commit` (msg) | Same HERMES guard applied to the commit message body. | shipped 2026-05-03 |
| `pre-push` | `git push` (pre) | Fetches remote + rebases local onto tip before push. Prevents the ID-collision class on auto-numbered files (tickets, ledger entries) when parallel writers (Orchestrator + Recovery + Specialist) push without first pulling. Skips on detached HEAD; fails-open on fetch failure. Per T-0147 / the operator msg_1121+1124. | shipped 2026-05-09 |

Install (idempotent):

```bash
bash ~/pantheon-config/tools/install_hooks.sh
```

Cross-account: each of `claude` / `claude-fallback` / `claude-research` runs the install on their own clone. Hook discipline travels with the repo, not with the user.
