---
symptom: "ticket.py CLI refuses to run from any branch starting with agent/ (_assert_cli_checkout_allowed). A subprocess call to ticket.py from an agent worktree (e.g. from a pre-commit hook that runs on staged files in the branch's own checkout) hard-fails."
fix: "Import ticket in-process and call the target function directly, matching the pre-commit hook's existing _main_guard_lint_findings/_ledger_validity_findings convention. Never subprocess `python3 tools/ticket.py <verb>` from anywhere that might execute from an agent/ branch worktree — the branch guard fires unconditionally."
domain: pantheon, ticket-py, pre-commit-hook, agent-branch, checkout-guard
---

# ticket.py CLI hard-refuses agent/ branch checkouts — subprocess self-defeats

## Symptom

Any subprocess invocation of `python3 tools/ticket.py <verb>` from a working directory whose current git branch starts with `agent/` will exit non-zero immediately with an error from `_assert_cli_checkout_allowed()` (ticket.py:5274-5281 as of 2026-08-22).

This is by-design safety — the CLI guard exists to prevent HO from accidentally running ticket.py operations from a worker's isolated worktree (which would land ticket state mutations on the wrong branch). But it becomes a footgun in two shapes:

1. **Pre-commit hooks** that subprocess ticket.py to validate a staged change from within the branch's own checkout.
2. **CI hooks / cron scripts** that pull the current branch's checkout + shell out to ticket.py to check something.

Both fire from the agent/ branch's own checkout by construction — the guard trips, the operation false-fails, and the safety gate becomes a self-defeat.

## Verification

```
git checkout -b agent/test-guard
python3 tools/ticket.py list --state inbox 2>&1 | head -3
# Exits non-zero; stderr names _assert_cli_checkout_allowed refusal.
git checkout main
python3 tools/ticket.py list --state inbox 2>&1 | head -3
# Exits 0; ticket listing renders normally.
```

## Fix

Import ticket in-process and call the target function directly. Matches the existing `.githooks/pre-commit` convention already used by `_main_guard_lint_findings` and `_ledger_validity_findings` (also in-process imports, not subprocess). Example, from T-3020 BT4's pre-commit hook extension:

```python
# pre-commit hook, invoked from agent/ branch checkout
from tools.ticket import _priority_list_validate_report

def _priority_list_validate_findings(staged):
    if 'rulebook/priority-list.md' not in staged:
        return None
    exit_code, messages = _priority_list_validate_report()  # in-process, no subprocess
    if exit_code == 1:  # parse fail = blocker
        return messages
    if exit_code == 2:  # dead-label warning = non-blocker
        print("\n".join(messages))
    return None
```

The in-process call reads the working-tree file directly via `priority_list_parse.parse_file()`, exercising the same validation logic without triggering the branch guard.

## When you need the CLI shape

If a downstream consumer genuinely needs the CLI (e.g. for cross-language interop, an out-of-process automation, a human operator), constrain the caller to main-branch execution paths only — or add an env-var bypass to `_assert_cli_checkout_allowed` gated to the specific known-safe caller (with tests + documentation of the exception).

## Related

- T-3020 BT4 (`priority-list validate` subcommand + hook extension) — where this trap was surfaced by the impl during plan-first review, in time to design around it.
- ticket.py:5274-5281 (`_assert_cli_checkout_allowed`) — the guard source.
- `.githooks/pre-commit` — the in-process-import convention this fix aligns with.
