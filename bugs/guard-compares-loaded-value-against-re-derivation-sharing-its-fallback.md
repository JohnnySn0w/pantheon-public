---
symptom: "A completeness/sync guard passes green while the config loader is dead. TWO VECTORS, same root. (1) The guard compares a LOADED value against an INDEPENDENT re-derivation; both fall through to the same hardcoded fallback, so `loaded == re-derived` is satisfied by the source never being read. (2) The guard pins the module CONSTANTS while every consumer calls ACCESSORS; an accessor rewired to the fallback is invisible to every value comparison in the repo — the guard proved the config was read into a variable, not that anything reads the variable. Derived maps (invert//filter of the loaded one) are unguarded by both. Values stay correct today and drift silently the moment the config is edited."
fix: "Assert the PUBLIC SURFACE follows a PERTURBATION of the source, not just the constants: copy the config to a tmp dir, change every value, re-import the module rooted there, and require every accessor — including derived maps — to return perturbed values. Include the NEGATIVE direction (an accessor must return None for a real key absent from the perturbed config), because positive assertions alone are satisfied by a hardcoded map that over-answers. Make perturbed values impossible to coincide with the host, and assert across ALL entries, not a representative one. Add a hermetic case (synthetic config sharing NO key with the fallback) so the check stays falsifiable where no real config exists. Treat the threat list as a COVER, NOT A PARTITION — an enumeration authored by whoever builds against it reliably misses a limb; this one missed dead-delivery, the cheapest vector of all. PRECONDITION, narrowed 2026-07-29: this only applies where the fallback was RECONCILED to equal the live value (a completed mirror, a synced snapshot, a keep-in-step constant). A plain default that DIFFERS from the configured value cannot hide a dead read — the difference is the signal. Measured counter-example: knowledge_sweep.min_sweep_interval_days falls back to a default of 7 but returns 14 for an injected env, so a dead read stays visible."
domain: "python, config-loading, fallback, sync-guard, testing, silent-failure, instance.yaml, agent_role_map, pantheon"
---

# A guard that compares a loaded value to a re-derivation sharing its fallback cannot see the loader die

**Class:** a check that cannot distinguish healthy from broken (Amendment H2), created *by* the act of
making a fallback complete. Not specific to any one project — it appears wherever a config loader has a
fail-safe fallback and a test asserts the loaded result against a second computation of the same thing.

**Found:** 2026-07-29 (artisan, T-2316; surfaced by the T-2281 review). Generalisation prompted by orchestrator.

## The defect

`agent_role_map.py` loads persona→role-key and persona→binding maps from `instance.yaml`, with
hardcoded `_FALLBACK_*` dicts used when the manifest is absent or unreadable. The loaders are
**fail-safe by design** — they swallow exceptions, because raising would brick every ticket op, the
mailbox MCP and every fleet monitor on one bad edit. That contract is correct and should not change.

T-2256 and T-2281 completed both fallbacks from partial to 8-of-8, which was right on its own terms:
a partial fallback silently under-covers the fleet in manifest-less environments.

The side effect is the bug. Once the fallback is a *complete* mirror, `loaded == fallback`, and every
sync guard in the suite is satisfiable by the manifest never being read.

**The measurement that makes it concrete.** With the loader neutered (`_load_instance_role_map`
returning the fallback unconditionally) but the manifest perfectly healthy:

```
F1  loaded maps == independent parse of instance.yaml   -> 1 passed   <-- GREEN, misses it
F3  loaders read a hermetic synthetic manifest          -> FAILED
F2  module maps follow a perturbed copy of the manifest -> FAILED
```

F1 is the assertion that *reads as sufficient*. It compares the loaded map against a fresh, independent
parse — which sounds like exactly the right check. It stays green because `loaded == fallback == parse`:
all three agree, and they agree hardest when nothing is read.

## Why the obvious fix is the wrong one

The instinct is "compare against a re-derivation." That fails here for a structural reason worth naming:
**a witness that shares the accused's fallback cannot testify against it.** When the loader dies, the
loaded value and the re-derived value converge on the same constant, and their equality becomes evidence
of nothing.

Same reason a test must not import its expected values from the module under test — a tautology that
passes forever. This is that defect one layer out: the *fallback*, not the module, is the shared source.

## The surgical fix

Assert the loaded value **follows a change to the source**, which no fallback can fake:

1. Copy the config to a tmp dir; perturb one value (`peer-daemon-codex` → `peer-daemon-codex-PROBE`).
2. Re-import the module rooted at that copy.
3. Require the module-level constants to show the perturbed value.

Then cover the remaining threats. **This is a COVER, not a partition** — the original version of this
table claimed the vectors were disjoint and complete, and it was missing the cheapest one (DEAD DELIVERY,
below), which a reviewer found by attacking rather than auditing. Each row names a threat with at least one
test that detects it alone; do not read the list as exhaustive:

| threat | how it actually happens | assertion |
|---|---|---|
| source-side schema drift | a key rename (`persona:` → `agent:`) that still parses | loaded == independent parse |
| loader death | a fail-safe `except` swallowing a real error | hermetic synthetic config sharing no key with the fallback |
| path drift | repo layout moves, module or config relocates | resolved config path == the file the suite reads |
| post-load clobber | constants reassigned from the fallback after import | module constants follow a perturbed copy |
| DEAD DELIVERY | accessor rewired to the fallback; constants stay live | public API follows a perturbed copy (see second vector below) |
| fail-safe contract | — | absent config → fallback, no raise (pins what the skips rely on) |

**Do not make the guard fail when the config is legitimately absent.** The fallback exists so a
config-less environment still works; that is a feature. Skip explicitly there and keep at least one
hermetic assertion always-on, or the guard cries wolf on a supported path, gets deleted by whoever it
interrupts, and the gap returns with a tombstone over it.

## Second vector: the guard pins the CONSTANTS; every consumer reads the ACCESSORS

Same root, different limb — and this one is cheaper to hit than the first.

A liveness guard naturally asserts the module-level constants the loader populates
(`_PERSONA_TO_INSTANCE_ROLE`, `_PERSONA_TO_BINDING`). But callers do not read constants; they call
accessors (`resolve_instance_role()`, `instance_binding()`, `account_persona_map()`,
`fleet_accounts()`). Because the fallback is byte-identical to the loaded value, **an accessor
rewired to the fallback is invisible to every value comparison in the repo.**

Measured 2026-07-29 on a guard built specifically to close vector one. Rewiring only the two
accessors, leaving the loaded maps genuinely manifest-sourced:

```
guard file ............... 15 passed, 6 subtests    <-- fully green
account-persona suite .... 30 passed, 49 subtests   <-- fully green
manifest says codex role-key : peer-daemon-codex-PROBE
resolve_instance_role(codex) : peer-daemon-codex     <-- every caller got the fallback
```

**The one-line statement of it: the guard proved the config was read into a variable, not that
anything reads the variable.**

### The derived-axis corollary

If a second map is *derived* from the loaded one (`_ACCOUNT_TO_PERSONA = invert(bindings)`), a guard
that asserts only the source map leaves the derived one unguarded. Replacing the derivation with a
hand-written literal passed every test — including the derived axis's own test suite, which pinned
values against a hand-written literal and therefore agreed with a hardcoded map *by construction*.
Then adding an entry to the config:

```
_PERSONA_TO_BINDING has newpersona : True   <- live; the guard asserts exactly this
account_persona("newaccount")      : None   <- the derived axis never saw it
fleet_accounts() count             : 8      <- unchanged
```

That is a stale-fleet-list defect reproduced verbatim underneath the guard written to prevent it.

### Fix for both

Assert that the **public surface** follows the perturbation, not just the constants: import the
module against a perturbed copy and require every accessor to return perturbed values. Include the
**negative** direction — `account_persona(<a real account absent from the perturbed config>)` must be
`None` — because positive assertions alone are satisfied by a hardcoded map that over-answers.

Make the perturbed values impossible to coincide with the host (`role-key + "-PROBE"`), and assert
across **all** entries rather than a representative one. A test that hardcodes one identity asserts
one identity's answer, and on the machine where that identity is the runner it can pass for a second,
unrelated reason — the same witness-shares-something-with-the-accused shape as the tautology above.

## How to spot it elsewhere

Grep-shaped question: *is there a fallback that the loaded value and its check both fall through to?*
If yes, the check is satisfiable by the source being dead.

**NARROWED 2026-07-29 — the first version of this section over-claimed, and a sweep found it.** It
said the pattern "applies to any `load_config() or DEFAULTS` shape, any settings object with a
baked-in default layer, any `os.environ.get(k, DEFAULT)`". That is too broad, and stating it that
broadly makes the heuristic useless: it flags every defaulted config read in the repo, most of which
are immune.

**The extra condition, and it is the load-bearing one: the fallback must have been reconciled to
equal the live value.** A default that DIFFERS from the configured value cannot hide a dead read —
the difference is the signal. The defect needs `fallback == loaded`, which is why it was *created*
by two changes that correctly completed a mirror to 8-of-8.

Measured counter-example from an AST sweep of `tools/` for exception-paths returning a module
constant (5 hits, 4 not the shape — two exit codes, one sentinel, one already fixed):

```
knowledge_sweep.min_sweep_interval_days()  -> DEFAULT_MIN_SWEEP_INTERVAL_DAYS
  default (no env)    : 7
  injected MIN=14     : 14        <- a dead read WOULD be visible
```
Immune, for two reasons worth copying: the value is read at CALL time through an injectable `env`
argument rather than snapshotted at import, and its suite already asserts a NON-default (`"3"` → 3),
so the witness does not share the fallback.

So the real grep is narrower: *find fallbacks that were deliberately brought into agreement with the
live value* — completed mirrors, synced snapshots, "keep this in step with X" constants. A plain
default is not a candidate; a **reconciled** one is.

### The positive form: what a config read that cannot fall into this looks like

Stated as the shape to copy, not just the trap to avoid. `knowledge_sweep.min_sweep_interval_days`
has **two independent immunities, either of which alone would suffice**:

1. **Read at CALL time through an injectable parameter**, not snapshotted at import into a module
   constant. `min_sweep_interval_days(env=None)` takes the environment as an argument, so a test can
   hand it a value the fallback does not contain and observe the result follow it.
2. **Its suite already asserts a NON-default** (`{"MIN_SWEEP_INTERVAL_DAYS": "3"}` → `3`). The
   expected value does not come from the same place the code would fall back to, so the witness does
   not share the accused's fallback.

Note that (1) is the exact inverse of the failure in the *second vector* above and of the
import-time-snapshot defect it points at: consumers that cache a derived value at import can be
re-enumerated as a literal with every test green, precisely because nothing reads them at call time
against a perturbed source. Same axis, opposite ends — prefer call-time reads where the cost allows,
and where a snapshot is genuinely needed, assert that the snapshot followed the source.

The tell is that the check gets *weaker* the more complete the fallback becomes — a check whose
strength is inversely proportional to how well the fallback is maintained is measuring the wrong thing.

## Related

- `bugs/` sibling: any "completeness checker is only as good as the set it checks against" instance —
  here the reference set is the fallback itself.
- `sop/standard/testing-depth-and-scope.md` §Amendment H2 (a check that cannot tell healthy from broken
  is not an assertion) and H1's sabotage-the-threat-not-the-assertion refinement: the four-way split
  above came from enumerating how the loader actually dies, not from what a single assertion handled.
