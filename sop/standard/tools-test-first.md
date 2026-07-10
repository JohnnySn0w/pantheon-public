# Test-first discipline for new tools/ Python scripts shipping to systemd

**Status:** **Ratified 2026-06-07 by the operator.** Authored 2026-05-17 by Orchestrator (HO) per Specialist T-0404 surfacing.

This SOP applies to **NEW** Python scripts under `pantheon-config/tools/` that will be invoked from systemd timers or cron. Existing scripts (watch_runner.py, ticket.py, ho_resumer.py, usage_monitor.py, inbox_consumer.py, etc.) already have established test suites and are NOT in scope — backfilling test-first discipline onto them is low-leverage.

The discipline: **before writing the new tool's first non-trivial code path, write `tools/test_<name>.py` covering its load-bearing behaviors. Watch the tests fail. Implement until green. Refactor.**

## When to use this

Fires when adding a new Python file to `tools/` that will be invoked by a systemd `.service` / `.timer` unit (or, by analogy, by any unattended scheduler). The trigger is the cron-fire shape, not the language — the asymmetric-failure-cost argument is what makes test-first load-bearing here.

**Asymmetric failure cost:** a bug in a daily / 5-minute-fire scheduled tool goes undetected on the fire path (no human in the loop). It gets compounded silently and surfaces in digests / dashboards hours or days later, often after several broken fires have accumulated state. Writing the test first pays for itself on the first prevented runtime fault.

**Do NOT use this for** (handle ad-hoc instead):

- Existing tools (per scope above).
- One-shot scripts not bound to a timer / cron (`tools/audit_<thing>.py` for a single forensic run; throwaway analysis scripts).
- Pure CLI wrappers that bottom out in shelling out (test the underlying logic; trust the shell-out boundary).

## Procedure

1. **Identify the load-bearing behaviors.** Before any code, enumerate what the tool MUST do correctly to be safe-to-fire-unattended. Bias toward enumerating boundary conditions: empty inputs, missing-state, stale-state, multi-fire idempotence, partial-failure mid-run.
2. **Write `tools/test_<name>.py` covering those behaviors.** Each test should fail (the tool doesn't exist yet); failing-with-the-right-error is the structural check.
3. **Implement the tool until the test suite is green.** Resist adding behavior the tests don't cover; if you find yourself wanting to add a path, add the test first.
4. **Run the smoke loop.** Per project convention: `python3 -m pytest tools/test_<name>.py -v` plus any existing project-wide test command. Tests pass green is the gate for landing.
5. **Land the tool + its tests in the same commit.** No separate "tests followup" commit — the tests are the contract, not an afterthought.

## Common failure modes

- **Tests-after-the-fact retrofit.** Writing tests after the implementation tends to test what the implementation does, not what the tool should do. The structural fix is to commit to writing the test first; the tactical patch (retrofit anyway) leaves the asymmetric-cost gap open.
- **Mocking the systemd / cron substrate.** Don't mock the timer firing. Mock the **inputs and side-effects** the tool consumes/produces; let the tool's main entrypoint run as it would under systemd.
- **Mocking external state without understanding it.** If the tool reads `~/.local/state/foo.jsonl`, write the fixture from a captured real-world file, not a hand-built struct of what you think the file looks like. (Per T-0381 retrospective in echo-prismatic: the same anti-pattern in a different language.)

## Boundary

This SOP does NOT cover:

- Echo-prismatic crates/ work — see `echo-prismatic/CLAUDE.md` §TDD-rigor discipline (pending ratification per T-0422).
- Backfilling test-first onto existing tools/ scripts (out of scope per intro).
- Non-Python systemd-fired tools (no current examples; revisit if a new substrate appears).
- One-shot scripts (excluded per §When to use this).

## Cross-references

- **Principles:** P4 (traceability — tests as the contract), D11 (fixture corollary — fixtures from real captured state), P7 (cheap action — write the test before the bug ships).
- **Directives:** D5 (smoke verification discipline — tests must pass green before landing), D6 (plan-first protocol — tests as the plan), D9 (orchestrator response contract — testable acceptance criteria).
- **Source ticket:** T-0404 (Specialist surfacing via T-0401 Arc 1).
- **Sibling discipline:** echo-prismatic CLAUDE.md §TDD-rigor (T-0422 ratification, pending).
- **Motivating retrospective:** T-0381 (echo-prismatic AddTranscript schema-mismatch — the same anti-pattern class).
