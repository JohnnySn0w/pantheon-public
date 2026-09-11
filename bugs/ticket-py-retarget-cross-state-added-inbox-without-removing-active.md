---
symptom: "ticket.py retarget on an active/ ticket to a new target leaves BOTH tickets/inbox/<TID>.md AND tickets/active/<TID>.md on disk — the git-mv branch fires internally but the mv is not part of the pushed commit, so `ticket.py list` shows the ticket twice"
fix: "state-consolidation via `git rm tickets/active/<TID>.md` then `python3 tools/ticket.py claim <TID>` — the claim wraps the rm-of-stale-active/ + mv-of-inbox-to-active into one atomic commit; if the retargeter isn't the correct claimant, just git rm the stale active/ copy and commit alone, leaving the fresh inbox/ for the new target to claim naturally"
domain: ticket.py, retarget, cross-state
---

# ticket.py retarget cross-state landed an inbox copy without removing the active copy

## Symptom

After `ticket.py retarget <TID> --to <NEW_TARGET>` on an `active/` ticket, both `tickets/inbox/<TID>.md` and `tickets/active/<TID>.md` exist. `ticket.py list` shows the ticket twice (one row per state). The `active/` copy is stale (old target, old transition log); the `inbox/` copy is the retargeted-fresh one.

Observed 2026-09-06 on T-3459 (retarget `codex → orchestrator` at commit `42bf062442c97ee687f8112c60dc80057f6f1db5`). `git show --name-status 42bf062442` lists only `A tickets/inbox/T-3459.md` (+ `M tickets/README.md`); the pre-existing `tickets/active/T-3459.md` is not touched. Expected shape for a cross-state retarget: `R tickets/active/T-3459.md → tickets/inbox/T-3459.md` (a `git mv`).

`tools/ticket.py cmd_retarget` (~L4470-4570 as of 2026-09-06) *has* the mv branch — `if new_state != old_state: ... res = _git("mv", rel_old, rel_new)` — and raises `TicketError` on non-zero rc, so a silent mv-failure is not the mechanism. Root cause is not yet identified (see §Investigation-needed below); the surgical fix is state-consolidation, no code change.

## Surgical fix

**Do NOT** edit the two files by hand or manually `git mv`. Use `ticket.py claim` — it consolidates cleanly in a single atomic commit:

```
# from the pantheon-config root, on main
git rm tickets/active/<TID>.md    # remove the stale pre-retarget copy
python3 tools/ticket.py claim <TID>  # moves inbox/<TID>.md → active/<TID>.md + adds a claim transition-log line
```

The `ticket.py claim` push wraps both changes (the `git rm` of the stale active/ + the mv of inbox → active) into one commit, so the state-consolidation is a single atomic landing. The resulting active/ file has the fresh (post-retarget) frontmatter + transition log.

*If* the retargeting actor is not the correct claimant (e.g. orchestrator retargeted-to-specialist and specialist should claim), skip the claim step; `git rm` the stale active/ copy alone, then `git commit -m "tickets: remove stale pre-retarget active/<TID> copy" && git push`. The fresh inbox/ copy remains for the new target to claim naturally.

## Verification

Repro post-mortem: `git log --oneline --all -- tickets/active/T-3459.md` vs `git log --oneline --all -- tickets/inbox/T-3459.md` on `origin/main` between commits `6a56741d93` (initial claim moving inbox → active) and `42bf062442` (the retarget). The retarget commit appears in the inbox-history only, not the active-history — confirming the `git mv` never fired. State-consolidation fix landed at `86223cbd81` (2026-09-06T10:22:09Z); post-fix `ticket.py list | grep T-3459` returns exactly one row.

## Root cause (confirmed post-hoc, hypothesis 1 → CONFIRMED)

**Mechanism:** `_find_ticket` (`tools/ticket.py:1157`) locates a ticket by iterating `STATES = ("inbox", "active", "done")` and returning the first `(TICKETS_ROOT / state / fname).exists()` match. **This is a disk-existence check, not a git-tracked-state check.** If an **untracked** file `tickets/inbox/<TID>.md` exists on disk at retarget time — even if it is NOT in the git index and NOT on origin/main — `_find_ticket` returns that path. Then `parse_front_matter` reads its frontmatter (`state: inbox`), so `old_state = "inbox"` and `new_state = "inbox"` (branch: `new_state = old_state if (keep_state or old_state == "inbox") else "inbox"`). The `if new_state != old_state:` mv branch is SKIPPED. `path.write_text(new_text)` overwrites the untracked inbox file with retargeted content, `_commit_and_push([path], ...)` stages ONLY that path — which appears as a fresh `A` since git didn't track it before — and the *actual* tracked `tickets/active/<TID>.md` is never touched.

**Verified via git-history analysis (2026-09-06 post-mortem):**
- Parent tree `42bf062442^` (== `8976944ba2`) — `git ls-tree -r 8976944ba2 | grep T-3459` → ONLY `tickets/active/T-3459.md` (blob `f0156491cd1a2eb5170ccea626c8a7e7d65f897a`). No inbox copy tracked.
- Retarget tree `42bf062442` — `git ls-tree -r 42bf062442 | grep T-3459` → BOTH `tickets/active/T-3459.md` (blob `f0156491...` — SAME BLOB, byte-identical, unchanged) AND `tickets/inbox/T-3459.md` (blob `76622c93...` — new). The active/ path was literally never touched by the retarget commit; the inbox/ path was added-fresh.
- This is inconsistent with the `_find_ticket → active → mv → inbox` code path (which would show a rename entry `R active → inbox` in `git show --name-status`). It IS consistent with `_find_ticket → inbox → mv-skipped → overwrite-untracked-file → stage-new-add`.

The untracked source: unknown for T-3459 (no reflog evidence surviving cleanup) but plausibly a stale leftover from an earlier ticket-op path — e.g. `_pull_if_needed` merged a state where inbox/T-3459 was deleted (the `claim` mv at `6a56741d93`), and if the local checkout retained the old inbox file as untracked residue from a prior state, git would leave it in place (git pull never removes untracked files).

## Structural remediation (candidates, YAGNI until frequency rises)

Filing here rather than a separate ticket per bugs/-README posture — a single instance in ~2 months of retarget traffic + surgical-fix known + root cause pinned = document + monitor, not fix-now.

1. **`_find_ticket` should reconcile with git-tracked state.** If both `TICKETS_ROOT/inbox/<TID>.md` (disk) AND `git ls-files tickets/active/<TID>.md` (index) exist for the same ID, surface `TicketError("ticket-multi-state-hazard", ...)` naming both paths so the actor triages before the retarget/claim/resolve mv silently no-ops on the wrong copy. Cost: one `git ls-files` call per lookup. Or, cheaper: after picking a path, assert `_git("ls-files", "--error-unmatch", path)` succeeds — refuse untracked matches.
2. **`cmd_retarget` post-condition assertion.** When `new_state != old_state`, verify both `old_path` and `new_path` are in `paths_to_stage` before `_commit_and_push`, AND assert `git status --porcelain` shows both an addition and a deletion for the ticket. If the diff shape doesn't match the intent, refuse the commit.
3. **Session-open sweep for untracked ticket files.** A session-start hook that runs `git status --porcelain tickets/` and reports untracked `tickets/*/T-*.md` — those are always suspect (real ticket files land through `ticket.py file`'s commit path, not raw disk drops).

None warranted for a single occurrence; a second one within a month tips this to the (1) fix.

## Cross-refs

- `bugs/ticket-py-shared-checkout-interleaved-writer-race.md` — related shared-checkout hazard class.
- `bugs/ticket-py-cli-refuses-agent-branch-checkout.md` — an adjacent worktree-CLI-refusal shape from a similar workflow surface.
- ORG-019 (blocked/ retirement) — the current 3-state model `(inbox, active, done)` that `_find_ticket` iterates.
- T-3459 (arc that surfaced this) — resolve note + ledger anchor `62fe992393` (2026-09-06).
