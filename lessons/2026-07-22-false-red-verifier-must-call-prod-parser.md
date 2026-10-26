# A verifier that reimplements production's parsing manufactures the defect it hunts

**Promoted from T-1966 (Mason, ORG-012 criterion-1: three instances, two agents, one day.)**

## The class

A checker that RE-IMPLEMENTS or APPROXIMATES the parsing/resolution its production counterpart
uses will drift from it — and the drift **manufactures a false RED**: the verifier reports a
defect that exists only in its own lookalike logic. This is the inverse direction of the
wrong-layer-GREEN family. Same root (checking something other than the defining surface),
opposite failure sign, and a **different remedy** — you cannot derive one rule from the other:

| direction | mechanism | outcome | remedy |
|---|---|---|---|
| false GREEN (`wrong-layer-green`, `pipeline-exit-code`) | a proxy is read in place of the defining signal | real defect MISSED | identify the defining signal and read THAT |
| false RED (this lesson) | the verifier's own reimplementation diverges from production | non-defect REPORTED | call the production function; do not re-derive it |

## The instances (all 2026-07-22, verified at source)

1. **Designed-against in advance (T-1912):** `tools/bugs_frontmatter_lint.py` imports
   `_parse_frontmatter` from `spine_index_groom` rather than reimplementing it — because a second
   parser could drift from the generator's, producing lint-green-while-the-index-renders-dashes.
   The decision IS this lesson, made before the class had a name.
2. **Caught pre-report (Mason, T-1965 scoping):** a HOOK_ALLOWLIST staleness sweep assumed all
   hooks live in `tools/hooks/` and falsely flagged `adaptive_finish_detect.py` (lives in
   `tools/`). Publishing it would have cost Recovery an investigation into nothing.
3. **Caught at build time (Recovery, T-1965):** a naive whitespace tokenizer falsely flagged
   `hook-handler.js` as stale — the orchestrator account wires it via a QUOTED path that only the
   prod `_script_basenames` parser handles. The shipped guard now shares that exact parser, with a
   regression test pinning the trap (`context_fill_monitor.py:2682-2685`).

## Why the cost cuts harder than a false green

A false green hides one defect. A false red **erodes trust in the guard until someone deletes
it** — a guard that cries wolf gets removed, and then the surface it watched is unguarded
entirely (this is why T-1930 cut its no-pipe rule rather than ship it born-red, and why instance 3
mattered: a staleness alert firing on a correctly-wired hook, on a security surface, is the
fastest way to teach people to ignore that surface's alerts).

## The rule

A verifier must **invoke the production parser/resolver**, not a lookalike. If it structurally
cannot (different process, different account, different language), it must **state the limitation
rather than approximate** — an approximation is a standing source of manufactured findings. And
before publishing any finding produced by your own checker: **check your own check** against at
least one known-good case, so your drift costs you a minute instead of costing someone else an
investigation.

## Cross-references

- `lessons/2026-07-21-wrong-layer-green-liveness-checks.md` — the false-GREEN sibling (parent
  family: proxy-vs-defining-surface).
- `lessons/2026-07-22-pipeline-exit-code-masks-command-failure.md` — false-GREEN sibling.
- `lessons/2026-07-22-allowlist-needs-its-own-guard.md` §Scope caveat — where a checker
  structurally cannot see the target, guard where the runtime checker runs instead of
  approximating (the same state-the-limitation rule from the account-scoped side).
- Disposition note: filed standalone (findability-by-symptom — "my checker cried wolf" is what
  the next reader searches) with the umbrella proxy-vs-defining-surface synthesis (parked pending
  T-1944/T-1946) to absorb it as its false-RED section when written.
