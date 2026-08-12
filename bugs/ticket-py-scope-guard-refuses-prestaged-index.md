---
symptom: "Any ticket.py subcommand (file/claim/resolve/block) errors 'scope guard refused: staged set [...] != expected [...]' when unrelated files were git add'ed beforehand, because ticket.py's auto-commit enforces a strict staged-set allowlist (the ticket file plus tickets/README.md); the abort leaves the ticket-file mutation already applied on disk but nothing committed, including the caller's own pre-staged files, which end up unstaged again — a chained manual git commit afterward may then commit only a subset, producing a commit message that claims more than it contains."
fix: "Never git add anything before calling ticket.py — run all ticket.py operations first (each auto-commits on its own), then stage and commit your own edits (ledger appends, ticket-body hand-edits) as a separate commit; if the guard is hit, run git status to see the true mixed state, then explicitly stage-and-commit everything relevant in one commit and verify via git status -sb clean plus git log -1 --stat that the commit's claimed contents match its actual contents."
domain: pantheon, ticket.py, git, scope-guard, tooling, ledger
---

# ticket.py refuses to commit when unrelated files are pre-staged (scope guard), leaving mixed uncommitted state

**First observed:** 2026-07-14 (twice in one session).
**Symptom:** `error: scope guard refused: staged set [...] != expected [...]` from any `ticket.py` subcommand (file/claim/resolve/block), followed by the invoked operation's FILES existing on disk but NOTHING committed — including your own pre-staged files, which the guard's abort leaves unstaged again. A chained `git commit` after it may then commit a subset (or nothing), producing commit messages that claim more than they contain.

## Cause

Every ticket.py mutation runs its own auto-commit with a strict staged-set allowlist (the ticket file + tickets/README.md state table). Anything YOU had `git add`ed beforehand (a ledger append, a hand-edited ticket body) lands in the staged set and trips the guard. The guard aborts the commit but the ticket-file mutation has already happened on disk.

## Rule

- **Never `git add` anything before a ticket.py call.** Sequence: run ALL ticket.py operations first (each auto-commits), THEN stage + commit your own edits (ledger lines, ticket-body appends) as a separate commit.
- If you hit the guard: `git status` to see the true mixed state, then stage-and-commit everything relevant yourself in one explicit commit — and verify the ledger/edits actually made it in (`git status -sb` clean + `git log -1 --stat`), since the failure mode is a commit message that lies about its contents.

## Cross-refs

2026-07-14 session: T-1756 filing (guard trip #1, ledger left uncommitted and separately committed as b92de9d3), T-1598 append (guard trip #2, cleaned up in d6142be1).

## Keywords

ticket.py, scope guard, staged set, expected set, auto-commit, allowlist,
tickets/README.md, git add, mixed uncommitted state, commit message lies,
pre-staged files, ledger append, ticket-body edit, git status -sb, git log -1 --stat,
T-1756, T-1598, b92de9d3, d6142be1, scope-guard-refused

**Sibling instance** (T-1917 cross-link): `ticket-py-shared-checkout-interleaved-writer-race.md` — same ticket.py-vs-real-git-state class, racing-writer variant. Related SOP arc: T-1770 (done).
