---
symptom: "A codex-targeted ticket (Cairn) looks unclaimed on origin/main for 20+ minutes after the seat was nudged — no claim commit, no report, mailbox silent — while Cairn has in fact claimed AND finished it on his own clone. An origin-side watch (git fetch + cat-file on tickets/active/<id>.md) never fires."
fix: "Never infer Cairn's progress from origin. Check the codex clone directly: sudo -u codex git -C /home/codex/pantheon-config branch --show-current && git log --oneline -3 && ls tickets/active/. If an agent/<id> branch exists there, deliver it yourself: sudo -u codex git -C /home/codex/pantheon-config bundle create /tmp/<id>.bundle origin/main..agent/<id> (chmod 644), then git bundle verify + git fetch <bundle> agent/<id>:agent/<id> in your clone, byte-sequence audit, push, PR, admin-merge (push/PR stay HO-exclusive per T-1673). Also: his WIP-gate messages can rest on a stale read of a ticket that is already done on main (squash-merge => reviewed sha not an ancestor) — answer with the resolve commit + PR number, not a re-check request."
domain: pantheon, codex, cairn, cross-clone, git-bundle, ticket-flow, claim-watch, mailbox
---

# Codex seat: ticket ops never reach origin — an origin-side claim watch is blind

## Symptom

2026-08-25, T-3195 (Cairn general pass of the germination-engine draft, operator-requested). Sequence on the HO side:

- 04:36Z Cairn mailbox: WIP-gated behind T-2416 "still active / reviewed sha not an ancestor of origin/main".
- 04:42Z HO reply: T-2416 is done on main (`801e4dc2a`; PR #336 squash `6c1de23e8` explains the non-ancestor read) — claim T-3195.
- 04:53Z HO armed a background watch: `git fetch origin; git cat-file -e origin/main:tickets/active/T-3195.md` every 60 s, 20 min.
- 05:01Z the operator: "did Cairn get to the rev.4 review?" — origin still showed the ticket in `inbox/`, mailbox empty, no codex-authored commits in 2 h.
- 05:02Z HO checked the codex clone via sudo: branch `agent/T-3195` with `12f307e67 tickets: claim T-3195` and `9625c9c85 T-3195: review …` — committed at **04:46:52Z**, i.e. six minutes after the nudge and fifteen minutes before anyone on the origin side knew.

The codex seat is bundle-fed (`reference_cross_clone_worker_code_delivery`: codex = git bundle + Recovery pushes) and workers do not push (T-1673). So **nothing Cairn does — claim, annotate, commit — is visible on origin until a bundle is carried across.** A watch on origin cannot fire; the ticket's transition log on main lags reality by however long delivery takes.

## Verification

```
sudo -u codex git -C /home/codex/pantheon-config branch --show-current      # agent/T-NNNN ?
sudo -u codex git -C /home/codex/pantheon-config log --oneline -3
sudo -u codex ls /home/codex/pantheon-config/tickets/active/
```

(sudo password: `/home/claude/root.env`, bare line — `tr -d '\n' < /home/claude/root.env | sudo -S -k -u codex …`.)

## Fix

1. **Watch the seat, not origin**, for codex-targeted tickets: poll the codex clone's `agent/<id>` ref or `tickets/active/` (sudo read) instead of `origin/main`.
2. **Carry the bundle yourself** when Recovery is not in the loop: `bundle create origin/main..agent/<id>` on the codex clone → `bundle verify` → `fetch <bundle> agent/<id>:agent/<id>` → byte-sequence audit (`git log -p main..agent/<id> | grep -c 'HERME[S].md'` == 0) → push → PR → admin-squash-merge → pull → resolve with `ticket.py resolve <id> --with "<summary>"` (`resolve` takes `--with`, not `--note`).
3. **Stale WIP-gate reads:** Cairn's gate check compares his reviewed sha against origin ancestry; a squash-merged PR makes every reviewed sha a non-ancestor by construction. Reply with the resolve commit + PR number; do not ask him to re-check.

## Related

- `reference_cross_clone_worker_code_delivery` (memory pin) — the bundle-delivery model.
- T-1673 — push/PR/merge is HO-exclusive.
- T-3195 / T-2416 / T-2667 — the live case.
