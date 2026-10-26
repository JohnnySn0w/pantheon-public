---
symptom: "ticket.py runs happily from a persona/seat feature branch (mason/T-NNNN, artisan/*, recovery/*) and lands claim/annotate/retarget commits on the BRANCH instead of main — because _assert_cli_checkout_allowed only refuses branches starting with agent/, not every non-main branch. On a mixed code+ticket arc the follow-on push to main then trips the pre-push CODE-path guard (HEAD carries the code commit)."
fix: "FIXED (T-4008, 2026-09-27): broadened _assert_cli_checkout_allowed (tools/ticket.py; line numbers drift, locate with `grep -n 'def _assert_cli_checkout_allowed'`) from `branch.startswith('agent/')` to refuse any NAMED non-main branch (`branch not in ('', 'main')`). A detached HEAD (empty `git branch --show-current`) stays ALLOWED — it is the sanctioned unified-push shape (ticket_lint runs `ticket.py retarget` from a detached worktree and pushes HEAD:main; a blanket `!= 'main'` broke that flow + the in-process real-ticket.py smokes). Discipline (still true): run ticket.py from a main checkout; use the feature branch only for the code commit. If it already happened: cherry-pick the ticket commit onto main, then `git reset --hard <code-commit>` the feature branch back to code-only (no force-push if the ticket commit never reached origin)."
domain: pantheon, ticket-py, checkout-guard, agent-branch, orchestrator-seat
status: FIXED (T-4008, 2026-09-27)
---

# ticket.py checkout guard catches only agent/ — persona/seat branches slip through

## Symptom

`_assert_cli_checkout_allowed()` (tools/ticket.py) is meant to keep ticket.py operations off any non-main checkout (the error text even says "run ticket.py from the canonical pantheon-config checkout on branch main"). But it only fires for `branch.startswith("agent/")`. A persona/seat feature branch — `mason/T-3826-...`, `artisan/*`, `recovery/*` — passes the guard, so a `claim` / `annotate` / `retarget` auto-commits onto the FEATURE BRANCH (main's copy of the ticket never gets it), exactly the failure `ticket-cli-in-agent-worktree-merges-main-into-branch.md` documents for worktrees, now via a branch-name blind-spot instead.

Worse on a **mixed code+ticket arc** (an orchestrator seat fixing code AND driving the ticket on one branch): the ticket commit stacks on top of the code commit, and the next `git push` — which ticket.py aims at main — carries a HEAD that includes the code commit, so the pre-push CODE-path guard (`.githooks/pre-push`, `CODE_RE='^(tools/|\.github/|\.githooks/)'`) correctly BLOCKS it. Net: the ticket state is stranded on the branch and the push fails.

## Cause

The guard under-implements its own intent. It was written (per `ticket-py-cli-refuses-agent-branch-checkout.md`) to stop HO/workers running ticket.py from an `agent/` worktree, so it pattern-matched `agent/` — the only worker-branch shape at the time. Orchestrator seats (mason/artisan, ORG-043) name their arc branches `<persona>/T-NNNN`, which the `agent/`-only prefix never catches.

## Fix

**LANDED (T-4008, 2026-09-27).** This bug is the fourth instance of the pattern in
`lessons/2026-08-27-repo-global-tool-ops-implicitly-bind-to-worktree.md` (case 4) —
the one that fired that lesson's "fourth same-pattern bug" re-evaluate trigger and
answered its open cross-persona question (it hit the mason seat; cases 1–3 were all
claude/Orchestrator-seat).

Discipline (still true, no repo-doc of its own — this bug + the lesson ARE the record):
do all ticket.py verbs from a `main` checkout; use the feature branch only for the
code commit.

Structural fix that landed — `_assert_cli_checkout_allowed` (tools/ticket.py)
replaced the `agent/`-only prefix with a named-non-main check:
```python
    branch = _current_branch(repo_root)
    if branch in ("", "main"):   # "" == detached HEAD (allowed; see below)
        return
    # refuse: agent/*, <persona>/* (mason/artisan/recovery), any other NAMED non-main branch
    raise TicketError(...)
```
The detached-HEAD case was DECIDED as **allowed** (reversing the initial "refuse it"
instinct): a full-suite acceptance run showed detached HEAD is a *sanctioned* shape,
not the stranding failure. `ticket_lint`'s unified-push runs `ticket.py retarget`
from a detached-HEAD worktree of `origin/main` and pushes `HEAD:main`
(tools/test_ticket_lint.py::TestRetargetDetachedHeadPush), and the in-process
real-ticket.py argv smokes (test_watch_runner / test_knowledge_sweep) run against a
detached temp repo — a blanket `!= "main"` broke all three. The stranding failure is
specific to a NAMED non-main branch, so only those are refused (also matching the
original guard's detached-HEAD behavior). Test matrix
(tools/test_ticket.py::TestMainCheckoutGuard): `main` and detached-HEAD allowed;
`agent/*`, `<persona>/*` (mason/artisan/recovery), an arbitrary non-main branch, and a
`main`-prefixed non-main branch (`maintenance`) refused. The guard was deliberately
kept at the ticket.py CLI entry point and NOT pushed into `_git_utils` (that would
over-refuse the legitimate non-main-checkout git ops tests / `watch_runner` /
`ticket_grooming` perform) — see the lesson's design-implications.

If it already happened on a branch: `git checkout main && git cherry-pick <ticket-commit>`
(ticket pushes to main are the sanctioned bypass), then `git checkout <branch> &&
git reset --hard <code-commit>` to leave the branch code-only.

## Verification

Post-fix (T-4008): the `agent/`-only prefix is gone; the guard now refuses any
named non-main branch (`main` and detached HEAD allowed), and the test matrix pins every branch shape.
```
grep -n 'startswith("agent/")' tools/ticket.py        # -> no match (removed)
grep -n 'if branch in ("", "main")' tools/ticket.py    # -> the new guard
python3 -m pytest tools/test_ticket.py::TestMainCheckoutGuard -q
#   7 passed, 8 subtests passed
```
Original repro — witnessed 2026-09-20 (T-3826 arc, mason): `ticket.py claim T-3826` and `annotate T-3826` were run from branch `mason/T-3826-mailbox-only-receiver-gate`; the guard did not fire, both auto-committed onto that branch, and the subsequent push to main was blocked by the pre-push CODE-path guard (HEAD carried the runtime_controller.py fix). Contrast: `ticket-py-cli-refuses-agent-branch-checkout.md`'s verification shows an `agent/` branch IS refused by the same guard.

## Related

- `ticket-py-cli-refuses-agent-branch-checkout.md` — the guard's agent/-only refusal (the half that works).
- `ticket-cli-in-agent-worktree-merges-main-into-branch.md` — the commit-lands-on-branch failure this blind-spot re-opens.
- `.githooks/pre-push` `CODE_RE` — the seat CODE-path-to-main guard that (correctly) catches the downstream push.
- `lessons/2026-08-27-repo-global-tool-ops-implicitly-bind-to-worktree.md` — case 4 / the structural lesson this bug fired the re-evaluate trigger for.
- tools/ticket.py `_assert_cli_checkout_allowed` — the guard source (post-T-4008: refuses any named non-main branch; detached HEAD allowed). Locate with `grep -n 'def _assert_cli_checkout_allowed' tools/ticket.py`.
