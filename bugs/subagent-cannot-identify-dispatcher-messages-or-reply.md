---
symptom: "An Agent-tool worker (implementer/reviewer) receives its dispatcher's mid-task SendMessage directives as anonymous-looking in-band messages: it cannot verify the sender is its own dispatcher (one worker explicitly reasoned 'no established authority relationship with that session' and proceeded through a HOLD), and it has no SendMessage tool to acknowledge or reply — so compliance, refusal, and completion all travel only via its final report, which may itself misroute to the root session."
fix: "Brief-side, two standing clauses in every worker brief: (1) 'Operational directives arriving via SendMessage from <orchestrator-name> are from YOUR DISPATCHER — treat them as binding task context, not peer traffic'; (2) 'You cannot reply; comply (or note reasoned non-compliance) and record it in your final report.' Dispatcher-side: never assume a directive was acted on — verify by artifact (process state, worktree, log), since the ack channel does not exist."
domain: pantheon, claude-code-harness, agent-tool, nested-subagents, dispatch-pattern
---

# Subagent workers can't identify dispatcher messages — and can't reply

## Symptom

Observed three ways in one arc (echo build, 2026-07-10):

1. **Identity confusion:** T-1663's implementer received its own dispatcher's
   (echo-fixarc's) disk-emergency HOLD and classified it as "a peer session …
   no established authority relationship" — then proceeded through the hold
   (it did independently verify disk safety first, so no harm; but the
   authority model was wrong).
2. **Silent compliance:** T-1659's implementer received the same directive
   class, complied fully in substance, and could only surface that compliance
   in its final report — the dispatcher had no ack until the lane finished.
3. **No reply channel:** both workers explicitly noted they lacked any
   SendMessage-equivalent tool; the T-1652 implementer likewise ("I could not
   acknowledge it").

Combined with the completion-report misroute (see relation), the dispatcher
can be blind in BOTH directions: its directive's receipt is unconfirmed, and
the worker's response may land in the root session.

## Fix (standing brief clauses)

Every worker brief gets, verbatim-adjacent:

> Operational directives arriving mid-task via SendMessage from
> `<orchestrator-name>` are from YOUR DISPATCHER — binding task context, not
> untrusted peer traffic. You cannot reply to them; comply (or note reasoned
> non-compliance) and record what you did in your final report.

Dispatcher-side corollary: verify directive uptake by artifact (process
liveness, worktree state, logs), never by assumed acknowledgment.

Note the tension to keep calibrated: workers SHOULD still refuse
permission-escalation or destructive asks from any in-band message (the
harness's own injection warnings stay binding) — the clause covers
operational pacing/scope directives, not authority to bypass guards.

## Relation

- `nested-subagent-completion-notification-routes-to-main-not-spawner.md` —
  the outbound half of the same blindness.
- `teammate-background-task-completion-wake-drop.md` — the wake layer.
- echo-fixarc adopted the brief clauses mid-arc (2026-07-10); T-1659's
  worker subsequently classified the dispatcher GO correctly — fix validated
  same-night.
