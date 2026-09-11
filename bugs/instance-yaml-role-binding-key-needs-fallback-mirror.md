---
symptom: "After a config-only commit that adds or changes a key under an instance.yaml `roles.<role>` binding (restart_service / restart_mode / restart_pane / supervisor / companion_units / expected_policy_skip), `tools.test_instance_role_grants.TestBindingFallbackStaysInSync.test_binding_fallback_never_contradicts_instance_yaml` goes red on main: `monitor-binding fallback disagrees with instance.yaml (persona: (fallback, manifest)): {'specialist': ({...}, {..., 'expected_policy_skip': 'runtime_viability_blocked'})}`."
fix: "A role-binding key is a TWO-surface change: the manifest line AND the same key in tools/agent_role_map.py `_FALLBACK_PERSONA_TO_BINDING` for that persona (exact dict equality). Land the fallback line in the code PR; smoke `python3 -m unittest tools.test_instance_role_grants tools.test_agent_role_map` in the working tree BEFORE committing the manifest line. If the red is already on main, add the fallback key via the PR flow (2026-08-25: `agent/T-3210-fallback`)."
domain: "pantheon, instance.yaml, agent_role_map, fleet-monitor bindings, guard tests, configure-leg"
---

## Symptom

`python3 -m unittest tools.test_instance_role_grants` on `main` right after a manifest-only commit:

```
FAIL: test_binding_fallback_never_contradicts_instance_yaml (tools.test_instance_role_grants.TestBindingFallbackStaysInSync...)
AssertionError: {'specialist': ({'account': 'claude-research', ...}, {..., 'expected_policy_skip': 'runtime_viability_blocked'}), 'metis': ({'account': 'suborch1'}, {'account': 'suborch1', 'expected_policy_skip': 'runtime_viability_blocked'})} != {}
: monitor-binding fallback disagrees with instance.yaml ... The fleet monitor config would then depend on whether the manifest loaded. Keep _FALLBACK_PERSONA_TO_BINDING in sync ...
```

The code PR that introduced the key was green (its tests ran against a manifest that did not yet carry the key); the red appears only once the manifest line lands — typically as HO's separate "configure leg" commit, which nobody smokes because it is "just config".

## Cause

`tools/agent_role_map.py` keeps `_FALLBACK_PERSONA_TO_BINDING`, a code-side mirror of the manifest bindings used when `instance.yaml` is absent/unreadable, so the fleet monitors (`fleet_health.py`, `context_fill_monitor.py`, `restart_drain.py`) never depend on whether the manifest loaded. `TestBindingFallbackStaysInSync` enforces exact dict equality per persona (T-2256 closed the other direction too: every manifest persona must exist in the fallback). Any binding key that reaches the loaded dict — every key `_load_instance_bindings` passes through — must therefore also appear in the fallback. Implementer plans and reviewer checklists tend to cover the *test-side* witness mirror in `test_instance_role_grants.py` (its docstring demands it) and miss this *code-side* table, because the manifest line itself is deferred to HO under D2.

## Fix

1. In the same PR that teaches `_load_instance_bindings` a new key, add the value to `_FALLBACK_PERSONA_TO_BINDING` for every persona whose manifest line HO will apply (the brief should say which personas and which value).
2. HO's configure-leg commit: edit `instance.yaml`, then run `python3 -m unittest tools.test_instance_role_grants tools.test_agent_role_map` in the working tree, THEN commit. A green run is the gate; do not smoke after the push.
3. Already red on main: branch `agent/<ticket>-fallback`, add the key to the fallback entries, run the same two modules (plus `tools.test_fleet_health` if the key feeds a monitor row), PR + squash merge. Do not "fix" it by removing the manifest line — the manifest is the source of truth; the fallback follows it.

## Verification

Reproduced 2026-08-25 (T-3210): configure-leg commit `9b45e1530` added `expected_policy_skip: runtime_viability_blocked` to `roles.specialist` + `roles.sub-orchestrator-1`; `python3 -m unittest tools.test_instance_role_grants` on main → `FAILED (failures=1)` with the assertion above. Fix branch `agent/T-3210-fallback` @ `7f342f3b7` (two fallback lines) → the five modules `Ran 162 tests ... OK`. Re-check: `git grep -n "expected_policy_skip" tools/agent_role_map.py instance.yaml` shows both surfaces.
