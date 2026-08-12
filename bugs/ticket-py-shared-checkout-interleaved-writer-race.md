---
symptom: "Two processes writing to the same shared pantheon-config checkout via ticket.py (e.g. HO's serial claim/resolve chain and the S3* weekly ticket filer timer) can interleave git add/commit on one index so a commit captures whatever the OTHER writer had staged at that instant, leaving a ticket's git mv only partially applied (e.g. a claim commit with the metadata edit but not the inbox-to-active file move) — surfaced as tickets/inbox showing a deleted file (D) and tickets/active showing an untracked (??) copy, and the NEXT git-mv-based ticket.py op failing loudly with 'git mv failed: not under version control'."
fix: "Confirm local matches origin (git fetch, no unpushed/behind — the commits are usually fine, only the tree is half-staged), verify the on-disk ticket state is the desired end state, sweep both writers' remnants into one explicit git add -A tickets/ && git commit naming the race, push, then re-run the originally-failed ticket.py op against the repaired tree; treat as accept-and-monitor given the small race window and loud-on-next-op failure mode, escalating to a lockfile around ticket.py's add/commit/push critical section only if frequency rises."
domain: pantheon, ticket.py, git, race-condition, shared-checkout, tooling
---

# ticket.py on the shared checkout: interleaved-writer race leaves half-committed claims

**Observed:** 2026-07-13 ~08:30Z. HO ran a serial `claim → resolve` chain while the local S3* weekly filer (timer, same `claude` account, same `/home/claude/pantheon-config` checkout) filed T-1731 concurrently. Result: the `claim T-1713` commit landed WITHOUT the inbox→active file move (commit contained only the metadata edit), leaving the working tree with `D tickets/inbox/T-1713.md` + untracked `tickets/active/T-1713.md`, plus the filer's own work (T-1731 file + README row) uncommitted. The following `resolve` then failed loudly: `git mv failed: not under version control, source=tickets/active/T-1713.md`.

**Mechanism (probable):** two processes interleaving `git add`/`git commit` on one index — the second writer's `commit` captures whatever the first had staged at that instant, and vice versa; neither sees an error at the point of interleave. Only the NEXT git-mv-based op surfaces it.

**Detection signature:** `ticket.py` resolve/claim erroring `git mv failed: not under version control` on a file that visibly exists in the right directory; `git status` showing a mix of `D` (old path) + `??` (new path) for a ticket that HEAD claims to have moved, often alongside another writer's untracked files.

**Repair (worked, keep as the pattern):**
1. `git fetch` and confirm local == origin (no unpushed/behind) — the commits themselves are usually fine, it's the TREE that's half-staged.
2. Verify disk state is the DESIRED end state (ticket file in the right dir with the right `state:` frontmatter).
3. One `git add -A tickets/ && git commit` sweeping both writers' remnants, message naming the race; push.
4. Re-run the failed ticket.py op — it succeeds against the repaired tree.

**Posture:** accept-and-monitor. The race window is small (filer runs weekly + on watch actuation; HO batches are occasional) and the failure is loud-on-next-op, not silent. If frequency rises, the structural fix is a lockfile in ticket.py around the add/commit/push critical section (same shape as the mailbox `.consumer.lock`).

**Related:** `reference_ticket_wip_cap_serializes_claims` memory pin (verify every end-state — this is why); `feedback_shared_checkout_reconcile_pattern` (reset-to-origin variant is for REBASE divergence, not this — here local==origin and only the tree needs sweeping).

## Keywords

ticket.py, interleaved writer race, git mv failed, not under version control,
shared checkout, concurrent writers, S3 weekly filer, claim, resolve, inbox, active,
git add -A, git fetch, accept-and-monitor, lockfile, .consumer.lock, T-1713, T-1731,
WIP cap, half-committed, staged index race

**Sibling instance** (T-1917 cross-link): `ticket-py-scope-guard-refuses-prestaged-index.md` — same ticket.py-vs-real-git-state class, pre-staged-index variant. Related SOP arc: T-1770 (done).

**New instance 2026-07-21** (T-1827 pilot, specialist report): the ACP-bridge WRAPPER session as second writer — executed a ticket move concurrently with the live session on the shared checkout; first attempt aborted mid-rebase leaving UU debris + an abandoned claim commit (reflog 8bd0026d4); recovered only because the live session detected the race and yielded. New wrinkle vs the original instance: the second writer was machine-fired by a duplicate mailbox delivery (~80s retry, no idempotency key). Fix ticket filed (wrapper writer-lease + delivery dedupe).
