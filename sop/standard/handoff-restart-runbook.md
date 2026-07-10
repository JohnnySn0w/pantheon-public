# Handoff-and-restart runbook

**Status:** Ratified 2026-05-10 (T-0197 #1 of 4; the operator msg_1220 — "green on both"). Authoritative for operator/HO citation.

This is the operator-facing surface for the `tools/handoff_and_restart.sh` wrapper — the structural mechanism for draining one HO session cleanly and bringing up a fresh one with state continuity. For the orthogonal cap-hit auto-resume mechanism (different trigger; different code path), see [`auto-resume-operator-runbook.md`](auto-resume-operator-runbook.md).

## When to use this

Three trigger shapes invoke this wrapper:

1. **Planned operator-fired restart.** Operator (the operator, Recovery) wants HO to drain + restart cleanly — typically before a scheduled config change, after a substantive deploy, or when HO's context has accumulated enough cruft that a fresh session would think more clearly. Manual invocation: `tools/handoff_and_restart.sh --service claude.service --mode user --target-pane main:0.0` (Orchestrator example).

2. **Auto-restart-on-decay (T-0184 monitor).** Recovery's `context_fill_monitor` polls Orchestrator's transcript JSONL every 5min and fires `handoff_and_restart.sh` automatically when Orchestrator crosses 60% context-fill (with a 30min cooldown). Same wrapper, same pipeline; just operator-replaced-by-cron. Per-agent extension landed via T-0189: monitor templates exist for Orchestrator + Specialist (Recovery self-monitors with the cooldown pattern).

3. **Recovery from a wedged session** that didn't cap-hit but is otherwise stuck (long-running tool that won't terminate, MCP eviction with no clean recovery, etc.). Operator-fired with the same mechanics; the wrapper handles the wedge case the same way it handles a clean working session.

**Do NOT use this for** Max-window cap-hits — those have a separate state-file-driven mechanism (`tools/ho_resumer.py` + `pantheon-ho-resumer.timer`); see auto-resume runbook for the boundary.

## Procedure

Invocation contract (`tools/handoff_and_restart.sh --help` for full flag list):

```
tools/handoff_and_restart.sh \
  --service <unit-name> \      # e.g. claude.service, claude-research.service
  --mode <user|system> \       # systemctl scope
  --target-pane <pane>         # tmux pane, e.g. main:0.0
```

The wrapper executes 7 steps (each is also the script's exit code on failure — `exit 4` = ACK timeout, `exit 7` = sentinel mismatch, etc.):

1. **Acquire flock** on `/tmp/handoff_and_restart.lock` (non-blocking; second concurrent fire bails immediately).
2. **Read pre-prompt mtime** of `HANDOFF.md` (default `/home/claude/HANDOFF.md`).
3. **Inject the prompt** into the target tmux pane. Default prompt asks HO to write a fresh HANDOFF.md + say "handoff written" and stop.
4. **Poll mtime until it changes** (HO ack-via-write — the structural witness, not a flag-file). Default ACK timeout: 600s (10min — wide enough for a substantive HANDOFF write).
5. **`systemctl restart <unit>`** — drains the old session, brings up the fresh one.
6. **`systemctl is-active` fast-fail** — poll until active or timeout (default 60s).
7. **Capture target pane + grep for steady-state sentinel.** Default sentinel: BOTH `(╭─+╮|─{20,})` (Claude Code prompt frame) AND `\? for shortcuts|ctrl\+t to hide tasks` (idle marker per T-0172). Both must match for steady-state declared.

**Post-step-7 kick (T-0172, default-on):** A canned directive is injected into the fresh pane to trigger first-turn action — read HANDOFF.md + post a status snapshot to the operator's Telegram. Disable via `--no-kick`.

## Common failure modes

- **ACK timeout (exit 4).** HO didn't write the handoff within 10min. Causes: (a) HO genuinely busy with a deep dispatch chain; (b) MCP or tool eviction silenced HO; (c) HO acknowledged but didn't actually write the file. Diagnostic: `tmux capture-pane -t <pane>` to see what HO is doing. Fix per cause: extend `--ack-timeout`; restart MCP; or just retry.

- **`systemctl restart` failed (exit 5/6).** Service unit broken — check `journalctl -u <unit> --since '5 min ago'`. Often: a misconfigured hook in `~/.claude/settings.json`, a missing dependency, or an environment-file path that disappeared. Fix the unit, re-run.

- **Sentinel mismatch (exit 7).** Restart succeeded but the fresh pane never reached the steady-state shape. Common causes: (a) Claude Code at a confirmation dialog (e.g., "trust this folder?"); (b) startup hook printing un-bordered output that shifts the prompt frame; (c) MCP eviction during startup. Diagnostic: capture the pane manually + visually inspect. Fix the underlying state, re-run from step 5.

- **HO mid-cycle when T-0184 monitor fires.** Auto-restart fires regardless of HO's current work; the in-progress dispatch chain is interrupted. The HANDOFF.md HO writes captures what was inflight, but a worker dispatch may need re-issuing in the fresh session. Fresh-HO reads the latest ledger entry first to confirm state.

- **Substrate-shift wedge.** If the systemd unit, the tmux pane, or the wrapper itself is fundamentally broken (e.g., post-OS-upgrade config drift, stale lockfile, tmux server died), the wrapper can't recover. **Escape hatch:** Recovery (or the operator) manually `systemctl restart <unit>` + the fresh HO reads HANDOFF.md cold from disk on session start (the auto-load hook handles this). The wrapper is the smooth path; the manual path is the fallback.

## Boundary

- **Cap-hit auto-resume** (separate mechanism; sibling runbook): `tools/ho_resumer.py` + `~/.local/state/pantheon/ho_limit_hit.json` + `pantheon-ho-resumer.timer`. Different trigger (StopFailure hook with `error_type: rate_limit`), different state path, different timing. See [`auto-resume-operator-runbook.md`](auto-resume-operator-runbook.md).
- **Agent-initiated self-restart.** Originally "no such mechanism" — superseded 2026-05-21: an agent CAN now restart its own session on demand and land the kick, via a detached `systemd-run --user` transient unit that survives its own death. See [`agent-self-restart-with-kick.md`](agent-self-restart-with-kick.md). (External triggers — operator, cron, systemd — remain the other restart sources.)
- **Cross-instance ops discipline** (when Recovery is acting cross-account on Orchestrator or Specialist): see [`cross-account-ops-discipline.md`](cross-account-ops-discipline.md) once authored (T-0197 #3).
- **Subagent dispatch resumption** (figuring out whether to re-issue a worker dispatch interrupted by restart): not in scope; HO judges per-cycle from the ledger entry + dispatch transcript.

## Cross-references

- **Source / scripts:** `tools/handoff_and_restart.sh` (the wrapper — primary source; `--help` for full flag list); `tools/context_fill_monitor.py` (T-0184 — fires this wrapper on context decay); systemd unit templates per T-0189 (per-agent monitors).
- **Principles + directives:** P1 (architecture over policy — the wrapper exists to replace ~5min of manual broker dance per restart with one structural command); D1 (the operator's bandwidth — the load-bearing why); D8 (HO self-discipline — fresh-HO reads latest ledger entry post-restart per the discipline).
- **CLAUDE.md sections:** §Auto-resume on Max-window cap-hits (defines the boundary against which this runbook sits); §Recovery context (Recovery's role in restart-firing); §Auto-restart-on-decay (T-0184 LIVE rule pinned at HO-level).
- **ADRs:** ORG-009 (kino PWA — long-term home for some operator triggers; some restart shapes will eventually surface there).
- **Tickets:** T-0008 (the wrapper's original deliverable, 2026-04-XX); T-0172 (post-step-7 kick + sentinel-regex tightening — currently retargeted to Recovery); T-0184 (auto-restart-on-decay monitor — Orchestrator-target initial); T-0179 (multi-agent extension — Orchestrator + Recovery + Specialist); T-0189 (per-agent monitor systemd template); T-0197 (this SOP's authoring ticket).
- **Telegram refs:** msg_876 (T-0160 ratification of the standard-bucket extension scope); msg_1169 (autonomy expansion + work-indefinitely posture context — auto-restart-on-decay is what makes "work indefinitely" structurally safe); msg_1174 (work-indefinitely posture activation explicit).
