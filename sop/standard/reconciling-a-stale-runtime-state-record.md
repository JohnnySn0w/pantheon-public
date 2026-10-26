---
title: Reconciling a stale pantheon-runtime-state/v1 runtime.json record
status: Adopted 2026-09-26 by Recovery (recovery) per T-4023. Below-goal-level methodology/infra SOP — same adopt-and-proceed convention as `hand-moving-an-established-seat-onto-the-control-plane.md` (sop/ is an HO-written surface per D2; not per-path ratification-gated). The reconcile ACTION on a specific live seat is reversible (backs up the prior record) and below the goal-level bar; running it is recovery-lane autonomy. This SOP is the how.
---

# Reconciling a stale pantheon-runtime-state/v1 runtime.json record

## Why this happens (the failure shape)

There are **two** runtime-state files per controller-managed persona:

- **`active-runtime.json`** — the CACHE (schema `pantheon-persona-active-runtime/v1`), written on every `controller_main` launch and cleared on observed death. Fast introspection only; tmux+pid is the real liveness authority.
- **`runtime.json`** — the RECORD (schema `pantheon-runtime-state/v1`), the durable description of the selected runtime (harness / profile / model / executable / tmux_target / scope_unit / source-provenance). At `<state_root>/<persona>/runtime-controller/runtime.json`.

Since T-4035 (2026-09-28, @71ebaf4bf), `controller_main` writes **both** on every launch. After the cache it calls `_persist_runtime_record_on_launch`, the same writer `ensure_runtime` uses. That persist is **best-effort**, so a launch never aborts on it, and that leaves one way for the record to go stale while the live session is correct:

- **The persist was skipped or failed.** It skips when source provenance is unavailable, almost always a **dirty controller worktree** (the clone the controller runs from has uncommitted changes). It leaves the old record in place on purpose rather than write a provenance-less one. Any exception there is swallowed the same way. Either way the controller's journal carries `runtime.json record not persisted on launch of <ref> -- <reason>`.
  `journalctl --user -u pantheon-runtime-controller@<persona> | grep 'record not persisted'`, run as the seat's account, shows it.

Before T-4035 the launch path wrote only the cache, so every relaunch that bypassed `ensure_runtime` left the record stale: a `handoff_and_restart.sh` restart_request, or a codex-fallback → claude recovery. Records last written before 09-28 can still carry that drift until the seat's next clean launch.

Two concrete harms from a stale/empty record (observed T-3995 / T-4022 on specialist):
1. `context_fill_monitor.active_harness_monitor_gate` can misread the harness (an empty `{}` record → "missing schema" → **disables** Claude-only continuity monitors for a seat actually on claude — when the gate is evaluated in a context that reads the seat's own record).
2. A future `ensure_runtime` / hand-move / cutover **safety-blocks** on the pane-vs-record mismatch (`_state_matches` False → see [[reference_handmove_fails_on_stale_recorded_runtime_scope]]).

Note the sibling case: `hand-moving-an-established-seat-onto-the-control-plane.md` **phase Q** (section 4, "quarantine ALL control-plane residue") deliberately moves the record aside, along with every other control-plane-written file, so a consume takeover bootstraps fresh. Its section 8 says why phase Q replaced the older step 3b. That is for a seat you are **tearing down + re-consuming**. THIS SOP is the opposite: a seat that **stayed running** whose record drifted — repair it to match the live pane, no restart.

## The tool

`tools/runtime_controller.py reconcile` (T-4023) re-derives the record from the LIVE pane matched to a configured binding and writes it with the controller's own writer (`_runtime_payload`) — the tooled form of the T-4022 hand-reconcile.

- **Dry-run by default.** `--apply` is required to write; a write backs up the prior record to `runtime.json.reconcile-bak-<UTC>` first.
- **Fail-closed.** It REFUSES (exit 1, prints the reason, writes nothing) rather than write a guess when: there is no live pane at the binding's tmux target; the pane pid is unreadable/dead; the pane argv does not UNIQUELY match one configured binding (0 or >1); or the controller worktree is dirty (source-provenance requires a clean worktree, same as ensure_runtime).
- **Idempotent.** A record that already `_state_matches` the live binding is an `already-consistent` no-op (exit 0, no write).
- **Out-of-band repair.** The durable auto-heal (persist on launch, T-4035) now exists, so this tool is the fallback for a skipped or failed persist. Fix the cause first: clean the controller's worktree, or the next launch will skip again.

## Procedure

Run it **as the persona's own account** (so `/proc`, the tmux pane, and the `_persona_state` path all resolve to that seat), from a repo clone that has the code and a **clean worktree**:

```bash
# 1. DRY-RUN first — see what it would write, confirm the matched binding.
sudo -u <account> bash -c 'cd ~/pantheon-config && python3 -m tools.runtime_controller reconcile --persona <persona>'
#   → status:"dry-run", matched_ref:"claude/interactive-subscription", changed:true  → good, proceed
#   → status:"already-consistent"                                                      → nothing to do, stop
#   → status:"refused", reason:"..."                                                   → DO NOT --apply; resolve the reason (below)

# 2. APPLY — writes the record + a timestamped backup.
sudo -u <account> bash -c 'cd ~/pantheon-config && python3 -m tools.runtime_controller reconcile --persona <persona> --apply'
#   → status:"reconciled", backup_path:".../runtime.json.reconcile-bak-<ts>"

# 3. VERIFY the two harms are closed:
#    a. the record now matches the live binding (a future hand-move/cutover won't safety-block):
sudo -u <account> bash -c 'cd ~/pantheon-config && python3 -c "import sys; sys.path.insert(0,\".\"); from tools import runtime_controller as rc; from tools import persona_bringup as pb; b=rc._manifest_bindings(pb.DEFAULT_MANIFEST, \"<persona>\")[0]; import json; s=json.load(open(rc._persona_state(\"<persona>\")/\"runtime.json\")); print(\"state_matches:\", rc._state_matches(b, s))"'
#    b. the continuity-monitor gate reads the right harness:
python3 -c "import sys; sys.path.insert(0,'.'); import tools.context_fill_monitor as cfm; g=cfm.active_harness_monitor_gate('<persona>', {}); print(g.status, g.active_harness, g.source)"
```

Account map (persona → account): orchestrator→claude, specialist→claude-research, recovery→claude-fallback, mason→suborch1, artisan→suborch2, archivist→exocortex.

## Resolving a refusal

- **`no live pane at <target>`** — the seat is not running (or its session/pane moved). Reconcile only repairs a running seat; bring the seat up first, or (for a cutover) use the hand-move SOP's phase Q quarantine instead.
- **`live pane argv did not UNIQUELY match one configured binding`** — the pane is running something the manifest doesn't describe (or two bindings are indistinguishable). Do NOT force it; reconcile by hand (load the correct binding, `_runtime_payload`, `_atomic_json`) as in T-4022, and file a ticket if the manifest/pane genuinely disagree.
- **`source provenance unavailable ... clean controller worktree`** — the clone you ran from has uncommitted changes. Run from a clean clone (or commit/stash first), then retry. This mirrors ensure_runtime's own clean-worktree requirement.

## Cross-references

- **T-4023** — this SOP + the reconcile subcommand. **T-4022** — the hand-reconcile this tools (specialist, 2026-09-25). **T-3995** — the codex-fallback split-brain whose record-reconcile remainder T-4022 closed.
- `hand-moving-an-established-seat-onto-the-control-plane.md` **phase Q** — the *quarantine-for-cutover* sibling (reset, not repair). It replaced the older step 3b (that SOP's section 8).
- [[reference_handmove_fails_on_stale_recorded_runtime_scope]] — the safety-block a stale record causes.
- **T-4035** (done, 2026-09-28, @71ebaf4bf): `controller_main` persists the record on launch (`_persist_runtime_record_on_launch`). It is best-effort and skips on a dirty controller worktree, which is the case this SOP still repairs.
