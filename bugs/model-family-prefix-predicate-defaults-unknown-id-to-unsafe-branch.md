---
symptom: "A model-capability predicate is a PREFIX ALLOWLIST (`REJECTING_PREFIXES` / accept-list of known ids) that DEFAULTS an unrecognized model id to one branch. When a new model family/version ships and you migrate to it, the predicate silently returns the wrong (default) answer for the new id — e.g. it sends `temperature`/`top_p`/`top_k` to a model that rejects them → HTTP 400 `invalid_request_error` on EVERY call on that path (a whole classify/gate breaks), or the inverse. The bug is invisible until the migrated deployment makes its first real call."
fix: "Add the new model's id PREFIX to the predicate's list at migration time (the surgical fix), AND extend the predicate's unit test with the new id. Structurally: treat 'unrecognized id defaults to <branch>' as a standing hazard — the default side must be the SAFE one for the current era (post-5-family, models increasingly REJECT sampling params, so default-accept is unsafe), and every model migration must include a predicate-audit step, not just a config-default swap."
domain: model-migration
---

# A model-family prefix-allowlist predicate silently gives the wrong answer for the next model id

## Symptom

A predicate decides some per-model API behavior by matching the model id against
a fixed list of PREFIXES, and DEFAULTS anything unmatched to one branch:

```rust
const REJECTING_PREFIXES: &[&str] = &[
    "claude-sonnet-5", "claude-opus-4-7", "claude-opus-4-8",
    "claude-opus-5", "claude-fable", "claude-mythos",
];
!REJECTING_PREFIXES.iter().any(|p| model.starts_with(p))  // default = ACCEPT
```

You migrate to a newer model whose id is NOT in the list. The predicate returns
the DEFAULT answer, which is wrong for that model. Nothing errors at build or
config time. The first live call on the migrated deployment fails — e.g. HTTP
400 `"temperature is deprecated for this model"` — and because the predicate
gates a hot path (the tiered classify gate that the whole conversation
address-gate hangs on), *every* call on that path breaks silently after cutover.

Live cases in echo-prismatic:
- **T-2437 (shipped):** `claude-opus-5` was not covered by the `claude-opus-4-7`
  /`-4-8` prefixes, fell through to default-accept, and would have 400'd every
  classify call on a migrated deployment. Fixed by adding its prefix.
- **T-3543 (latent, caught 2026-09-21 before it shipped):** the same predicate,
  `model_accepts_sampling_params()` at `crates/echo-adapter-claude/src/lib.rs:253`
  (gates the live classify path at `crates/echo-adapter-claude/src/classify.rs:829`),
  has NO Haiku-successor prefix. The operator ruled the Haiku-4.5 migration goes to
  the "newer haiku" (a 5-family model, which per the same file's doc rejects
  sampling params). Migrating to e.g. `claude-haiku-5` without adding its prefix
  → default-accept → `temperature` sent → 400 on every classify call → tiered
  gate dead. Recorded on T-3543 as the highest-risk migration step.

## Cause

The list enumerates the KNOWN ids on one side and lets the default side absorb
everything else — including ids that do not exist yet. When the API's behavior
for new models trends one way (post-Opus-4.7, the whole 5-family REJECTS
non-default `temperature`/`top_p`/`top_k`), the "unrecognized → default" branch
is on the UNSAFE side, so each new model is a live 400 waiting for a migration.
A config-default swap (`DEFAULT_MODEL = "<new id>"`) changes WHICH model is used
but does NOT touch the predicate, so the two drift apart silently.

## Fix

At migration time, in the same change that swaps the model-id defaults:

1. Add the new model's id prefix to the predicate list (surgical):
   ```rust
   const REJECTING_PREFIXES: &[&str] = &[ /* ... */, "claude-haiku-5" ];
   ```
2. Extend the predicate's unit test with the new id on the correct side
   (echo has `sampling_param_predicate_tests` at lib.rs:268 — add the case).
3. Make "audit every model-family predicate" a checklist item of the migration,
   not just the config default. Grep for the retiring family and for any
   `starts_with(` / prefix-list over model ids:
   ```
   grep -rnE 'REJECTING_PREFIXES|starts_with\("claude|_accepts_' crates/ --include='*.rs'
   ```

The file's own maintenance note already mandates this ("new 5-family ids and any
Opus id >= 4.7 must land on the reject side of this predicate — add their prefix
here, don't rely on the default") — the recurring miss is that migrations touch
the config default and forget the predicate.

## Verification

Re-runnable, in `~/echo-prismatic` (origin/main @d4f4d4f, 2026-09-21):
```
grep -n 'REJECTING_PREFIXES' crates/echo-adapter-claude/src/lib.rs   # no haiku-5 prefix
grep -n 'model_accepts_sampling_params' crates/echo-adapter-claude/src/classify.rs  # :829, live gate path
```
Witnessed: `REJECTING_PREFIXES` (lib.rs:254) lists sonnet-5/opus-4-7/opus-4-8/
opus-5/fable/mythos but no haiku successor; the predicate defaults unmatched ids
to accept (lib.rs:262 `!...any(starts_with)`); the accept-side test
`accepts_haiku_and_pre_4_7_opus_and_sonnet_families` (lib.rs:290) confirms
current haiku accepts. So `model_accepts_sampling_params("claude-haiku-5")`
returns `true` today → would send sampling params to a rejecting model. The
opus-5 instance (T-2437) is the same predicate having already shipped this class.

## Non-echo (Python) instances

- **T-4012 (pantheon-config, fixed 2026-09-26):** `tools/context_fill_monitor.py`
  `_CONSENT_FALLBACK_CAPPED_MODELS = frozenset({"claude-fable-5"})` was checked with
  exact membership (`original in ...`, detect_consent_fallback ~:1750). A seat
  re-pinned to the point-release `claude-fable-5-1` (or `claude-fable-5[1m]`) is the
  same capped family but fell to the default no-alert branch → the pin-durability
  consent-fallback nudge silently never fires. Dormant when caught (supervisors pin
  opus). First NON-echo (Python) occurrence of this class. Fixed: prefix match
  (`startswith`) + renamed `_CONSENT_FALLBACK_CAPPED_MODEL_PREFIXES` + isinstance
  guard for a None originalModel + prefix-regression tests.

Python migration-audit grep (any tool that keys behavior on a set of model-id
literals via exact `in` membership rather than a prefix — the same hazard):
```
grep -rnE 'frozenset\(\{"claude-|"claude-[a-z0-9-]+"\}?\)|_CAPPED_MODEL|CAPPED_MODEL_PREFIXES' tools/ --include='*.py'
```

## Discovered

- T-2437 (the opus-5 instance, fixed) — the doc comment at lib.rs:244-252 records it.
- T-3543 readiness audit, artisan 2026-09-21 (the latent haiku-successor instance).
- T-4012 (pantheon-config consent-fallback, fixed 2026-09-26) — first non-echo instance.

## Keywords

model migration, model id, prefix allowlist, REJECTING_PREFIXES, starts_with,
model_accepts_sampling_params, temperature is deprecated for this model, 400
invalid_request_error, classify gate, tiered gate, sampling params, top_p, top_k,
Anthropic model deprecation, claude-haiku, claude-opus-5, default branch unsafe,
T-2437, T-3543, echo-adapter-claude, unrecognized model defaults
