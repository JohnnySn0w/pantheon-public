# SOP: Appending a ledger entry

**Status:** Adopted 2026-09-30 by Orchestrator (HO) under msg-3733 principles-as-ratification (below-goal org-practice methodology; ORG-012: HO authors SOPs). Filed under T-4465 so it can render as an auto-triggering skill for every seat that writes the ledger (HO, archivist, mason, specialist, recovery). It consolidates the HO memory pins on the append flow and timestamp discipline, corrected against the current tools by a 2026-09-30 accuracy review. Each rule exists because it was broken at least once: an orphaned jsonl commit (05-29), a printf-truncated entry (07-14), forward-drifting stamps (07-28), and a wrong-week shard (08-24). The operator override-available.

## When to use this

You're writing a decision, arc close, catch-up or post-cap-hit-resume record into the append-only audit chain at `pantheon-config/ledger/`. Ticket transitions are recorded by `ticket.py` itself; don't duplicate them as ledger entries.

## Procedure

1. **Build the entry as a Python dict and append with `ledger_shard.append_entry`,** from YOUR OWN pantheon-config clone. Never hand-write JSON, never use a heredoc of braces, never `printf` (a `%` in the rationale truncates the entry). `append_entry` picks the shard from the entry's OWN timestamp (ISO weeks roll on Monday; never hardcode the shard a HANDOFF named), refuses a duplicate `id`, and checks the entry against ORG-001's rules for new entries (T-4734; it prints `ledger: WARN ORG-001 …` lines until enforcement is switched on, then refuses).
   ```
   cd <your pantheon-config clone> && TS=$(date -u +%FT%TZ) python3 - <<'PY'
   import os, sys; sys.path.insert(0, 'tools')
   from pathlib import Path
   import ledger_shard as ls
   ts = os.environ['TS']
   entry = {
       "id": f"{ts}-<short-slug>",
       "ts": ts,
       "agent": "<your ORG-100 role id>",       # e.g. head_engineering_orchestrator, engineering_orchestrator
       "executor": "<your seat slot id>",       # instance.yaml slot, e.g. engineering-orchestrator-2
       "action": "<snake_case_verb_phrase>",
       "justifies_via": ["T-NNNN", "D1", "P4"],
       "rationale": "What happened, why, and the evidence (shas, run ids, TG ids).",
       "refs": ["T-NNNN", "repo#PR"],
       "cost_usd": None,
   }
   print(ls.append_entry(Path('ledger'), entry), entry["id"])
   PY
   ```
   Field names and rules follow ORG-001 (`ledger/README.md` defers to it):
   - `id` is `<ts>-<slug>`: the entry's own `ts`, then a lowercase kebab-case slug.
   - `agent` is your ORG-100 role id (`agent_role_map.resolve_role(<persona>)` gives it), or `human:operator` for an entry the operator writes. Never a persona name: persona names are kept out so the ledger can be published without a scrub.
   - `executor` is your seat slot id (the `instance.yaml` role key; `agent_role_map.resolve_instance_role(<persona>)` gives it). Always set it; ORG-001 asks for it whenever your role is shared.
   - `id`, `ts`, `agent`, `action`, `rationale` and `justifies_via` are required.
   - `refs` is the form in common use; use ORG-001's typed `links` where you need typed edges. A decision made well before you record it names that time in `decided_at`; `ts` stays the write time.
2. **Measure the time in the same command that writes it** (`date -u`, as above). ORG-001's `ts` is the write time, and the writer flags a `ts` more than five minutes from its own clock. Never extrapolate from an earlier stamp: forward drift compounds and inverts apparent causality against peer traffic.
3. **Validate, then commit, as one `&&` chain:**
   `python3 -c "import json; [json.loads(l) for l in open('<shard>') if l.strip()]" && git add <shard> && git commit -m "ledger: …" && git push origin main`.
   A heredoc on its own line breaks an `&&` chain. Keep the validation and the commit in one gated command.
4. **If the push is rejected (origin moved):** `git pull --rebase origin main && git push origin main`. If the rebase conflicts in the same shard (a concurrent append), keep BOTH lines, re-validate the JSON, and `git rebase --continue`. Don't let a `| tail` hide the rejection; check the push's exit code before step 5.
5. **Only after the jsonl is on origin, run the groomer:** `python3 tools/spine_index_groom.py --commit-prod`. It works from a fresh detached worktree off `origin/main` (so an unpushed entry is invisible to it), regenerates EVERY registered spine README (ledger INDEX + per-segment SPINE, and also tools, notes, bugs, incidents, watch reports, adr, lessons, rulebook, policies, sop, skills), and pushes one scoped commit per changed region. A manual run can therefore push unrelated regeneration commits; that's expected. Recovery's daily cron may already have done it.
6. **Pull and verify:** `git pull --ff-only`; `git status -sb` reads `## main...origin/main`; `grep -c <your id> ledger/INDEX.md` is 1.

## Common failure modes

- Running the groomer before the push: INDEX omits the entry and local `main` diverges. Fix: rebase, push, re-run the groomer.
- Hand-editing `INDEX.md` or `SPINE.md`. Never do it: the groomer rebuilds SPINE wholesale from the shard (its header carries an entry count) and appends only missing ids to INDEX, so a hand-edit is overwritten or leaves a noisy diff.
- A malformed line reaching origin (the ledger is shared by every seat). If it happens, repair the last line programmatically, re-validate, commit the fix, push.
- A placeholder timestamp (`T-boot`). The `Date.now()` ban is for Workflow scripts only; in bash, use `date -u`.
- Appending to last week's shard on a Monday (use `append_entry`, which shards by the entry's own ts).
- Copying another seat's `executor` value, which misattributes the entry (ORG-001 executor attribution, T-2276). Two seats of one role share an `agent`; only `executor` tells them apart.
- Writing a persona name or `orchestrator` as `agent` out of habit. Those are history-only values since 2026-10-08 (ORG-001, the operator TG 11474).

## Boundary

- One entry per arc or natural stopping point, not per step. The ledger is the audit chain, not a log.
- The specialist watch runner writes one actuation entry per cycle that files tickets (`watch_runner.write_actuation_ledger`); any other entry about its activity is the HO's.
- Workers (subagents) can't write `ledger/` (worker-guard). A worker's DECISIONS go into its report, and its dispatching orchestrator lifts them into the entry.

## Cross-references

- `ledger/README.md` and ORG-001 (entry schema; executor attribution).
- `tools/ledger_shard.py` (`append_entry`, `segment_for_ts`).
- `tools/spine_index_groom.py` (spine regeneration; `commit_scoped` in `tools/_git_utils.py`).
