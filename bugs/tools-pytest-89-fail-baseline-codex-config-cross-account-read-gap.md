---
symptom: "Running `pytest tools/` on the pantheon-config main worktree from the orchestrator (or recovery, or any non-codex) seat shows ~89 failing tests concentrated in 4 files: test_persona_installer.py (~74), test_codex_telegram_bridge.py (~13), test_bringup_acceptance_harness_codex_ready.py (1), test_bringup_acceptance_harness_codex_rc_ready.py (1). Every implementer touching tools/ *.py from a non-codex seat hits this cluster and can misattribute it to their own diff, burning ~17min per stash-and-rerun differential."
fix: "Do NOT attempt to fix — this is the pre-existing baseline pending the HC multi-copy design landing (T-3102 → T-3104, superseding the earlier T-3095 setfacl option per the operator TG 8320: 'Everyone should have a copy of codex-config and claude-config as part of HC design'). Under that design each seat carries a local codex-config copy and cross-account reads become moot. Attribution: fixtures under those 4 files need to open codex-config paths (/home/codex/codex-config/...) whose parent dir is mode 700 and NOT traversable by non-codex uids. Confirm orthogonality-to-your-diff before proceeding: `git stash && pytest tools/test_persona_installer.py tools/test_codex_telegram_bridge.py tools/test_bringup_acceptance_harness_codex_ready.py tools/test_bringup_acceptance_harness_codex_rc_ready.py -q` (should print an identical fail count / same test names as the same run with your diff applied), then `git stash pop`. Only then commit + report."
domain: "pytest, pantheon-config, tools, codex-config, cross-account, permissions, baseline, smoke"
---

# `pytest tools/` shows ~89 failing tests from any non-codex seat — codex-config cross-account read gap

## Symptom

Running the CI smoke command `python3 -m pytest tools/ -q` on the main pantheon-config worktree from the `claude` (orchestrator), `claude-fallback` (recovery), or `claude-research` (specialist) seat produces:

```
89 failed, 8469 passed, 86 skipped, 1468 subtests passed
```

Failures concentrated in 4 files:

- `tools/test_persona_installer.py` — ~74 FAILED/SUBFAILED lines
- `tools/test_codex_telegram_bridge.py` — ~13 lines
- `tools/test_bringup_acceptance_harness_codex_ready.py` — 1
- `tools/test_bringup_acceptance_harness_codex_rc_ready.py` — 1

Zero failures anywhere in tools/test_closure_gate.py, tools/test_ticket_lint.py, tools/test_typed_edges.py, or the other ~200 test files.

## Cause

Fixtures under those 4 files stage or read from paths under `/home/codex/codex-config/...`. That directory is mode 700 and NOT traversable by any uid other than `codex`. So a `Path("/home/codex/codex-config/...").read_text()` inside the fixture raises `PermissionError: [Errno 13]` from any non-codex seat. From the codex seat (which owns the dir) the tests are green.

This is the same class as `bugs/echo-ci-runner-cannot-read-claude-home-paths.md` — mode-750 home dir + fixture accessing an absolute path across accounts. Same shape, different account pair.

## Fix

**Do NOT try to fix as an implementer.** The resolution belongs to the HC multi-copy design (T-3102 → T-3104, per the operator TG 8320 2026-08-23 superseding T-3095's earlier setfacl option): under HC each seat will carry a local copy of codex-config, retiring the cross-account read need entirely. Until that lands, the cluster is the standing baseline for every non-codex seat.

**What TO do** when your `pytest tools/` sweep shows ~89 F's and you're not sure they're yours:

1. Isolate via git stash — falsify the "your diff caused this" hypothesis:

   ```
   git stash
   python3 -m pytest tools/test_persona_installer.py tools/test_codex_telegram_bridge.py \
     tools/test_bringup_acceptance_harness_codex_ready.py tools/test_bringup_acceptance_harness_codex_rc_ready.py -q
   git stash pop
   ```

   Expected pre-stash and post-stash: identical fail counts + identical test names. If they diverge, the delta IS your diff and needs handling.

2. Confirm zero failures in the files your diff actually touched (`test_closure_gate.py`, `test_ticket_lint.py`, etc — grep the sweep output for their names).

3. In your report, name this cluster explicitly as "pre-existing HC-multi-copy-pending baseline (T-3102/T-3104, formerly T-3095 setfacl)" so the reviewer doesn't re-triage from scratch.

## Verification

Reproduce today (2026-08-24) from the orchestrator seat:

```
$ id -un
claude
$ ls -ld /home/codex/codex-config
drwx------ ... codex codex /home/codex/codex-config
$ python3 -m pytest tools/test_persona_installer.py -q 2>&1 | tail -3
… 74 failed …
```

Retire this bug when either (a) HC multi-copy design lands (T-3102/T-3104) and each seat carries a local /home/{claude,recovery,specialist}/codex-config-copy that fixtures can read locally, OR (b) less-preferred: the earlier T-3095 setfacl option is revived and `ls -ld /home/codex/codex-config` shows a `+`. In either case, the SAME `python3 -m pytest tools/test_persona_installer.py tools/test_codex_telegram_bridge.py tools/test_bringup_acceptance_harness_codex_ready.py tools/test_bringup_acceptance_harness_codex_rc_ready.py -q` from a non-codex seat must return 0 failures. Both mechanism-landed AND smoke-clean must hold.

## Discovered

- T-2926 (2026-08-24) impl arc, on the receipt-id uniqueness scan work — impl saw 89-F on first sweep, isolated via git stash correctly. Documented here so the next tools/ implementer grep-finds the attribution in 5s instead of the 17-min stash-rerun cycle.
- Same class as T-3093 audit Cat A (87 CI-red, pending T-3095 setfacl decision).

## Retirement trigger

**RETIRE** when the HC multi-copy design lands (T-3102 → T-3104: each seat carries a local codex-config copy) AND `python3 -m pytest tools/test_persona_installer.py tools/test_codex_telegram_bridge.py tools/test_bringup_acceptance_harness_codex_ready.py tools/test_bringup_acceptance_harness_codex_rc_ready.py -q` returns 0 failures from a non-codex seat. Fallback trigger if T-3095 setfacl is revived instead: same smoke condition. Banner in place if a partial fix ships (e.g. only test_persona_installer's fixtures are refactored).

## Keywords

pytest, tools, 89 failed, persona_installer, codex_telegram_bridge, bringup_acceptance_harness_codex_ready, bringup_acceptance_harness_codex_rc_ready, PermissionError, /home/codex/codex-config, cross-account, orchestrator, recovery, specialist, T-3095, T-3093, Cat A, setfacl, pantheon-shared, baseline, smoke, orthogonal, git stash, isolation, false-attribution
