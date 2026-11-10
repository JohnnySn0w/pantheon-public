# Orchestrator standing rules — the HO playbook for any orchestrator seat

**Status:** Adopted 2026-09-28 by Orchestrator (HO) under T-2892, which is ORG-075 D2/E2 deliverable (b), "the HO playbook". This is an HO-drafted SOP per ORG-012 and is below goal level, so it is autonomous per msg-3733. The source is Orchestrator's pinned rules (`pinned-rules/orchestrator.md`), cut down to the durable, role-general subset.

## When to use this

Read this at session start if you are an orchestrator of any kind:
- the head engineering orchestrator (orchestrator);
- an engineering orchestrator seat (mason, artisan);
- an in-session project-orchestrator.

The `pantheon-role-engineering-orchestrator` skill renders this whole file, and the project-orchestrator skill takes its chain-of-justification section from here.

This file is the repo-stampable, seat-loadable form of the rules an HO-level task needs (ORG-075 D2: a seat's gap is information, not competence). Seat-specific matters stay in each seat's own `pinned-rules/<seat>.md`: identity, reporting line, and whether an operator-facing channel exists (only Orchestrator has one).

Throughout, "your dispatcher" means the tier directly above you:
- a seat's dispatcher is Orchestrator;
- Orchestrator's is the operator;
- a project-orchestrator's is whoever spawned it.

## Chain of justification

Every dispatch carries the discipline forward. P3 (decisions justify upward) and P4 (traceability) already require a `DECISIONS` section on every non-obvious call. The lift at orchestrator tier is to **carry that forward across every dispatch boundary you create**:

- Every brief you write (implementer, reviewer, researcher, sub-orchestrator) **explicitly requires** the dispatchee to:
  - (a) emit DECISIONS justifying-via P/D ids on every material call;
  - (b) recursively require the same of anyone they dispatch;
  - (c) report back with the chain intact.
- Don't rely on inheritance by reference. A restart-fresh dispatchee may not load a file the brief merely names, so spell the requirement out.
- **Intent first (ORG-034).** A brief OPENS with its arc-intent line: one sentence naming the outcome and what it serves. DECISIONS cite the intent served first ("goal-neutral maintenance" is the honest cite for hygiene), with P/D ids as the how. On a genuine mid-arc conflict, intent binds and the deviation is FLAGGED (D6).
- **Partition every brief.** Say what the dispatchee decides autonomously (with justifies-via) and what bubbles up. Most back-and-forth stays inside the tier; that is why the tier exists.

**The bubble-up list is complete.** Only these go to your dispatcher:
1. Locked-decision deviation: a previously ratified decision becomes structurally infeasible.
2. Scope creep beyond the dispatched arc's boundary.
3. New dependencies (security or maintenance surface).
4. The 3-REBOUND budget is exhausted on one ticket. First inspect, then re-brief or re-dispatch; bubble only if it is still stuck.
5. Production-enable or supervised first-fire, **conditioned on reversibility**:
   - hold when the action is irreversible, has no proven rollback, or is genuinely novel;
   - surface-and-act when it is notable but reversible with a staged rollback;
   - just act when it is routine and reversible.

Sanity checks, routine clarifications and unclear tests stay at your tier.

**Smell tests before firing any dispatch:**
- Is the chain-of-justification rule explicit in this brief, or am I assuming the dispatchee will infer it?
- Does the first sentence say what the work is FOR, or only how to do it?

## Dispatch staging shapes

Workers bind their main-path guard to the **session-start cwd**, not to later `cd`s, so staging is decided before the spawn. The shape differs by dispatch type:

1. **Implementer (Agent tool).**
   - Dispatch with `isolation: "worktree"` from a shell cwd *inside the target repo*. The harness worktree follows the cwd repo.
   - Brief the worker to verify that `git rev-parse --show-toplevel` is not the main checkout, then run `git branch -m agent/<ticket>`.
   - A plan-first brief must make the plan turn `touch .plan-first-keepalive` (untracked, deleted before the final commit). Otherwise the harness auto-cleans the unchanged worktree at the turn boundary.
   - Do NOT pre-stage a `/tmp` worktree on `agent/<ticket>` for an implementer; the rename collides with it.
2. **Reviewer (Agent tool).**
   - Pre-stage `git worktree add --detach /tmp/<repo>-<ticket>-review agent/<ticket>` (no `-b`; `--detach` because the branch is usually checked out in the implementer's worktree and git refuses a second checkout). Brief the reviewer that `git branch --show-current` is empty by design and to verify `git rev-parse HEAD` == `git rev-parse agent/<ticket>` instead.
   - In the SAME tool call as the spawn, `cd` your own shell out of any project main path.
   - Dispatch WITHOUT `isolation`. The brief's first action is `cd /tmp/<repo>-<ticket>-review && pwd && git rev-parse --show-toplevel`.
   - Reviewers stay read-only: no branch rename, commit or push.
   - Remove the worktree after the arc.
3. **Project-orchestrator, or your own direct work.**
   - Run `git worktree add /tmp/<repo>-<ticket> -b agent/<ticket>`, confirm the branch starts with `agent/`, and name the path in the brief.
4. **All shapes:**
   - name the branch explicitly in the brief;
   - `cd` out of a project main path immediately before EVERY spawn (spawned workers inherit your shell cwd);
   - after the arc, remove any stray `.claude/worktrees/agent-<id>/`.

The per-dispatch-type shapes above are authoritative; they match `sop/standard/staging-a-worker-dispatch.md` (skill `pantheon-sop-dispatch-staging`). `.claude/skills/pantheon-dispatch/SKILL.md` §6 keeps the isolation-rule history: its rules (a)–(c) ("pre-stage every worktree by hand; `isolation: "worktree"` is untrusted") were **superseded on 2026-09-30 by T-4476** and are kept only for the record. Evidence for the implementer shape (the harness worktree follows the dispatcher's cwd repo; a pre-staged `agent/<ticket>` worktree collides with the implementer's branch rename) is in `bugs/dispatched-subagent-inherits-main-path-cwd-preflight-guard-bails.md`.

**Commit and push flow.**
- Workers commit on `agent/<task>` and never push. Every REBOUND before APPROVE would otherwise leave a stale ref on origin.
- In pantheon-config, a seat pushes its own branch (never main), then asks Orchestrator by mailbox for the PR and merge. Orchestrator opens the PR, gates on CI, merges, and confirms the SHA. The seat then resolves its ticket.
- Push, PR and merge by any *subagent* is structurally blocked by the worker-guard (T-1673). A seat is not a subagent: it is its own top-level session on its own account, so the guard doesn't cover the seat's own branch push. It does cover every worker the seat spawns.
- Other repos follow their own conventions. Check `repos.yaml` grants before taking a repo's work.

## Ticket ops on main, code on branches

- Every `ticket.py` operation (file, claim, annotate, block, resolve) runs from a synced main checkout and pushes to main immediately. Never do these on an `agent/` branch (T-1944).
- Agent branches are code-only.
- WIP cap 1 per persona. A ticket whose own work is done and only awaits an external merge stays claimed: run `ticket.py await-merge T-NNNN --ref <repo>#<pr>` (T-4119); never a bare `unclaim`, which makes the auto-consumer re-deliver it as false work. Unclaim with a note only when you are blocked on an unbounded peer event BEFORE your work is done.
- To wake another persona, target a ticket at it. A mailbox wake is deferred for as long as the recipient is mid-turn, by design. Use mail for the brief and the ticket for the wake.

Detail: `sop/standard/ticket-flow.md` and `sop/standard/retask-dispatch.md` §Push/merge boundary.

## Block discipline — ORG-019

`tickets/blocked/` is retired, and a block is frontmatter only:
- **calendar gate:** `not_before: YYYY-MM-DD`;
- **dependency gate:** `depends_on: [T-NNNN]`. The cascade removes a resolved id in place.

`ticket.py block` sets these, and the ticket stays in `inbox/` or `active/`.

Smell test: is this a calendar gate or a dependency gate? Does the ticket need to move directories? No, it never does.

## Worker silence — end-of-turn check

While you have live teammates, check each one at the end of every turn. The signal is an `~/.claude/teams/*/inboxes/` subdir; a bare `teams/` dir no longer counts, because every session now has one.

```
ls -lat ~/.claude/teams/*/inboxes/ 2>/dev/null
```

| Role | Threshold |
|---|---|
| orchestrator | 30 min |
| implementer | 15 min |
| reviewer | 5 min |

If a worker exceeds its threshold and you did not message it this turn, status-check it now via SendMessage. Delivery can drop silently: a success return does not mean the recipient woke.

When you dispatch-and-wait, also arm a self-wakeup (`sop/standard/dispatch-and-wait-liveness.md` Layer 1). Before an arc ends, leave no teammate mid-task.

## Keep working while you wait

Never idle in an await-state while the queue holds completable work. When an item blocks on your dispatcher's input (a merge, a ratification, a decision):
1. record progress on the item;
2. shelve it (`ticket.py block` or annotate);
3. swap to the next completable item.

Repeat until the queue is empty, or every remaining item genuinely needs the dispatcher.

The terminal state names three things:
- what is blocked, and on whom;
- what was shelved;
- that nothing else is completable.

Vague-await gestures ("let me know when…", "happy to pick up whatever next") are not a terminal state.

When you finish an arc, pull the next one in the same breath. An "arc-quiet" report wastes a wake cycle.

Orchestrator-side form: `sop/standard/ho-absent-operator-keep-working.md`.

## Act-vs-park

Before parking anything on your dispatcher, ask: **can I produce an artifact that IS a reversible version of the act?** Examples: a draft committed, a change behind a flag, work staged but not pushed.
- **Yes:** it is a deferred judgment. Reason it through, proceed reversibly, surface a lean, and note it post-hoc.
- **No:** it is genuinely reserved, so park it.

Source: `rulebook/policies/trust-posture.md` §Act-vs-park calibration, and the procedure in `sop/standard/agent-autonomy-act-vs-park.md`. At seat altitude, "park" means bubbling to Orchestrator, never to the operator.

## SOPs — index first

Before any non-trivial action (deploy, restart, remediation, cross-account op, multi-step maneuver), scan the indexes for an applicable SOP:
- `sop/standard/README.md` covers how to do X correctly;
- `sop/remediation/README.md` covers what to do when X breaks.

Read the specific SOP only if it applies.

At the start of an arc, skim `notes/recent-policy-changes.md` for recently ratified policy. Before designing anything a doc marks "pending", grep `tickets/done/` and `tools/` for prior art first.

## Shared-repo awareness

- **Research auto-commits.** `claude-research` auto-commits to pantheon-config main. The signature is subject `research(watch): land <date> reports`, scoped to `knowledge/watch/reports/<date>/`, with no Co-Authored-By trailer. Commits of that shape are expected, not a regression.
- **Forbidden bytes.** The forbidden-byte rule is in the base layer (`pantheon-persona-orientation`, "Forbidden-byte guard"). It applies to file content AND commit messages. Brief it explicitly to any worker whose output may touch it.
- **Hand-curated docs (D2).** `CLAUDE.md`/pinned-rules, READMEs, `notes/`, `architecture/adr/`, `rulebook/` and principles are proposal-only below the orchestrator that holds ratification.

## Report upward concisely

Your dispatcher's bandwidth is the scarce resource (D1, recursively):
- Lead with what they must decide or do, numbered. If nothing, say so in one line.
- Routine status is a few short lines.
- Cite the ticket id and let it carry the depth.
- Send one digest per arc at a natural stopping point, not one per step.

Thoroughness belongs in the ticket resolve note, not the report.

## What stays out of this playbook

These are per-seat or Orchestrator-only:
- identity and naming;
- the operator-facing channel and Telegram reply discipline (Orchestrator only);
- auto-resume and harness-controller operator detail;
- Recovery-recovery particulars;
- ORG-012 primary authorship of lessons/bugs/SOPs/ADRs (Orchestrator drafts; seats propose).

When running a team, also load `pantheon-role-project-orchestrator` for the dispatch checklist and brief structure.

## Maintenance

- `pinned-rules/orchestrator.md` restates most of these rules, because it is auto-loaded where their failure rate is highest. When a rule here changes, change it there in the same arc, or the two copies drift.
- The rendered skills stamp this file's commit. Re-render per `rulebook/persona-orientation/README.md` after any edit, and propagate to peer seats.

## Cross-references

- `architecture/adr/ORG-075-seat-ability-model.md` D2 (why this exists)
- ORG-043 (the seats)
- ORG-034 (intent-first)
- ORG-019 (blocks)
- `rulebook/principles.md` P3/P4
- `sop/standard/retask-dispatch.md`
- `sop/standard/team-orchestrator-brief-construction.md`
- `sop/standard/plan-first-dispatch.md`
- T-2892 (this playbook)
- T-4065 (the pin-seeding tool that delivers the memory half of ORG-075 D2)
