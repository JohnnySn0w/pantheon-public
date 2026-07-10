---
symptom: "test setup using `std::env::set_var(...)` fails to compile under Rust 2024 edition + workspace `unsafe_code = \"forbid\"` lint — `set_var` was promoted to `unsafe fn` in Rust 2024; `unsafe { std::env::set_var(...) }` then trips the workspace's forbid-unsafe lint"
fix: "for env-var values that need to be predictable in tests, use a runtime-already-set var (e.g. `HOME`, which is always set in a test process and has a predictable readable value) instead of mutating env state at all. If you genuinely need to mutate, escape the lint at module-level via a per-test-file allow-attribute that documents why — but prefer not mutating."
domain: rust, rust-2024-edition, lints, testing
---

## Symptom

Workspace declares `unsafe_code = \"forbid\"` (per Echo + many Pantheon Rust crates' `[workspace.lints.rust]`). A test wants to set an env var to verify a config-resolution path:

```rust
#[test]
fn config_resolves_external_env_var() {
    std::env::set_var("ECHO_TEST_FOO", "bar");
    // ... assert resolution returns "bar" for the field referencing ECHO_TEST_FOO
}
```

Compile error under Rust 2024 edition:

```
error[E0133]: call to unsafe function `set_var` is unsafe and requires unsafe block
   |
   |     std::env::set_var(\"ECHO_TEST_FOO\", \"bar\");
   |     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
   |
   = note: items used inside an unsafe block require an `unsafe` keyword
```

Wrapping in `unsafe { ... }` then trips the workspace lint:

```
error: usage of an `unsafe` block
   = note: `-D unsafe-code` implied by `-D forbid` from workspace lints
```

Catch-22: function call is unsafe (Rust 2024); using `unsafe` is forbidden (workspace lint).

## Cause

Rust 2024 edition (stable rustc 1.85+) promoted `std::env::set_var` and `std::env::remove_var` to `unsafe fn` because env-var mutation is process-global and racy across threads. See [the 2024 edition guide § Changes to `set_var`](https://doc.rust-lang.org/edition-guide/rust-2024/newly-unsafe-functions.html). The intent is to surface the latent thread-safety hazard.

Workspaces that pre-emptively forbid unsafe code (a common Pantheon convention for stricter safety posture) inherit this collision. Both pieces are correct individually; their combination forces a refactor at the test-setup layer.

## Fix

**Preferred**: avoid the mutation entirely. Pick a value-test setup pattern that doesn't touch env state:

- Reference an env-var that is **always set in a test process** with a **predictable value**:
  - `HOME` is always present, points to a real path, and is a reasonable stand-in for "an env-var that resolves to a real string."
  - On Linux: `USER`, `PATH`, `LANG` are also reliable.
- Build the `*_env` field in the test's synthetic config to point at one of these and assert the resolved value matches `std::env::var("HOME")` (or whichever you chose).

```rust
#[test]
fn config_resolves_external_env_var() {
    let expected = std::env::var(\"HOME\").expect(\"HOME is set in test process\");
    let config = build_minimal_config_with_env_field(\"HOME\");
    let resolved = config.resolve_secrets().unwrap();
    assert_eq!(resolved.field_referencing_HOME(), expected);
}
```

This is what `crates/echo/tests/config_tests.rs` (echo-prismatic@7677f08) does for the `echo_config_external_env_var_resolved_at_startup` test.

**If you genuinely need to mutate** (rare; usually a smell that the test isn't decoupled from env state): escape the lint at the module top with a comment explaining why. Don't blanket-allow at the workspace level.

```rust
// SAFETY: test-only env mutation; serial test prevents concurrent-access races.
#[allow(unsafe_code)]
mod env_mutation_tests {
    #[test]
    fn ... {
        unsafe { std::env::set_var(\"FOO\", \"bar\"); }
        ...
    }
}
```

This is the escape hatch; reach for it only after confirming the test can't be restructured to read existing state.

## Discovered

- Echo `agent/echo-binary-wiring` cycle 2026-05-08 (T-0139 implementer dispatch). Tests 5 + 6 (env-var fail-fast + external-env-var-resolved) initially used `set_var`; surfaced as a clippy/compile failure during the smoke loop. Implementer routed test 6 through `HOME` and asserted-against `std::env::var(\"HOME\")` to clear the lint cleanly. Test 5 (missing-env-var fail-fast) only needs to *check* env state, not mutate it — used a deliberately-impossible env-var name so `std::env::var` returns `Err` regardless.
- echo-prismatic commit 7677f08 — `crates/echo/tests/config_tests.rs` lines covering `echo_config_external_env_var_resolved_at_startup` + `echo_config_missing_required_env_var_fails_fast_at_startup`.

## Pairs with

- `bugs/rust-blanket-from-for-boxerror-conflicts-with-explicit-impl.md` — adjacent Rust mechanics gotcha (stdlib invariants colliding with project conventions); same flavor of \"library or edition mechanic interacts unexpectedly with project policy.\"

## Brief-quality lesson

When dispatching tests against config-resolution paths in Rust 2024 + workspace-`forbid-unsafe` workspaces, the brief should suggest:

> Tests that need to verify env-var resolution: use `HOME` (or another always-set predictable env-var) and assert against `std::env::var(\"HOME\")` — do NOT use `std::env::set_var` (Rust 2024 edition makes it `unsafe fn`; workspace `unsafe_code = \"forbid\"` blocks the wrapping `unsafe` block; the workaround is decoupling the test from env mutation, which is also cleaner).

This single sentence in the brief would have saved the implementer the discovered-at-code-time iteration.

## Keywords

rust, rust 2024, rust 2024 edition, unsafe_code, set_var, std::env::set_var, env mutation, workspace lints, forbid unsafe, lint policy, testing patterns, config resolution, env-var resolution, HOME env, predictable env, integration tests
