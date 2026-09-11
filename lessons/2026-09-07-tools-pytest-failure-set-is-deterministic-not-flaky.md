# tools/ pytest failure set is deterministic in main-checkout — the "run-to-run variance" premise did not reproduce

**Date:** 2026-09-07 · **Source:** T-3482 characterization (filed from T-3476 impl's final-report reflection) · **Tier:** worker discipline (smoke-verified claims stay legible) · **Ticket:** T-3482

## The premise being characterized

T-3476 impl's final report claimed that `python3 -m pytest tools/` shows a stable total failure count (~98) across runs BUT with the *identities* of ~20 non-baseline failures varying run-to-run in modules with no import/logic relationship to the arc under change. Named observed variance: `test_codex_telegram_bridge`, `test_forgejo_mirror_verify`, `test_germinate/runtime_effect_adapter`, `test_germinate/planner_sandbox`, `test_ups_capture`, `test_adr_reeval_walk`, `test_bugs_frontmatter_lint`, `test_mcp_list_guard`.

The premise implied test-order or fixture-hygiene flakiness that would make future workers' smoke-green evidence noisier than it should be for this repo.

## What actually reproduces in the main-checkout environment (2026-09-07)

**Zero variance across 2 sequential vanilla runs.** Full-suite `python3 -m pytest tools/` at commit `c29e809ae2` (before T-3483 review commits), two sequential runs, no test-order randomization plugin installed:

| | run1 | run2 |
|---|---|---|
| Duration | 1221.98s | 1206.98s |
| Failed | 96 | 96 |
| Passed | 9427 | 9427 |
| Skipped | 91 | 91 |
| Subtests passed | 1985 | 1985 |
| Failed-line identity | — | **byte-identical to run1** (`diff` of sorted-failures returned nothing) |

The 96 failures cluster in 7 modules — the same modules, same test IDs, same subtest IDs across both runs:

| Failed-count | Module |
|---|---|
| 40 | `tools/test_persona_installer.py` |
| 5 | `tools/test_codex_telegram_bridge.py` |
| 2 | `tools/test_bugs_frontmatter_lint.py` |
| 2 | `tools/hooks/test_ups_capture.py` |
| 2 | `tools/germinate/planner_sandbox/tests/test_admission_c1.py` |
| 1 | `tools/test_tempfile_hygiene_lint.py` |
| 1 | `tools/test_forgejo_mirror_verify.py` |

(Aside: the "96" count includes some `SUBFAILED` subtest lines within `test_persona_installer.py`; the failed-*test* count is fewer, but the sum of `FAILED` + `SUBFAILED` lines is 96 and is the number pytest's summary reports.)

## Explanation of the mismatch

Pytest test-collection order is deterministic (alphabetical file traversal); `pytest-randomly` is NOT installed in the main-checkout env (`importlib.util.find_spec('pytest_randomly')` returns None); `tools/conftest.py` does no randomization; `pyproject.toml` / `pytest.ini` / `tox.ini` / `setup.cfg` carry no `randomly` / `randomize` / `shuffle` configuration. There is no source of run-to-run variance for the same environment on the same commit.

Four plausible sources for the impl's variance perception, none reproducible here:

1. **Different environment.** T-3476 impl was in an agent worktree. Worktree may have had a different Python env, an installed randomizer plugin (via a global site-packages surface), or a different working directory whose relative-path assumptions differ. Not investigated further — the impl's env is gone.
2. **Fix-loop causation misread as flakiness.** As the impl landed incremental changes, different tests broke/fixed across their local runs — appearing as variance across a "same code" baseline, when actually the code was changing.
3. **Selective observation.** Failure output was truncated in the impl's rendering; different truncation windows surfaced different test IDs, mis-remembered as different failure sets.
4. **Shared-resource race.** A concurrent worker's build/test on the same host could contend for filesystem/socket resources. Not systematic; would produce non-deterministic failures across ALL modules, not the specific ~20 named — pattern-mismatch for this theory.

Explanations (1) and (2) are the most likely; (3) is possible; (4) is pattern-mismatched.

## What this means for future workers

**Smoke-green evidence in the main-checkout env IS legible.** A worker declaring "green apart from the 96 baseline reds" can be trusted at test-identity level: those 96 are stable and the 9427 passing tests are stable. The chronic-red baseline (T-3093 arc) is the residue to chase; there is no additional "smoke-flakiness" residue on top of it.

The impl's variance report should be treated as env-specific to whatever worktree/setup they had, not as a repo-wide flakiness signal.

**If a worker in a fresh dispatch sees non-baseline red variance:** first check `python3 -c "import importlib.util; print(importlib.util.find_spec('pytest_randomly'))"` in their env — if that returns anything non-None, they've inherited a randomizer plugin that main-checkout doesn't have, and their variance is env-config, not repo-flakiness. Second check: `ps -ef | grep -E 'pytest|cargo|node'` for concurrent contention.

## Method (reproducible)

```bash
cd /home/claude/pantheon-config
for i in 1 2; do
  python3 -m pytest tools/ --tb=no -q -p no:cacheprovider > /tmp/pytest-run$i.log 2>&1
  grep -E "^(FAILED|SUBFAILED)" /tmp/pytest-run$i.log | sort > /tmp/pytest-run$i.failures
done
diff /tmp/pytest-run1.failures /tmp/pytest-run2.failures  # expect: no output (identical)
```

Each run takes ~20 minutes on this VPS at current load. Two runs suffice for the identity claim; more only strengthen it.

## What this lesson does NOT settle

- **The 96 baseline reds themselves.** Those are T-3093's arc, not T-3482's — T-3093 chases specific baseline reds to green; this lesson only confirms the *set* is stable.
- **Environment-hardening.** If a worker's env genuinely has variance (per the check above), the underlying setup gap is real; a separate ticket could file "fresh-worktree pytest env should match main-checkout env for legibility." Out of T-3482's scope.
- **Chasing individual failures.** Explicitly out of scope per T-3482's Ask: this ticket characterizes variance, does not chase specific reds.
