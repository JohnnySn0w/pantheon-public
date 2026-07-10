---
symptom: "compile error E0119 conflicting impl when defining `impl From<MyError> for Box<dyn Error + Send + Sync>` — stdlib already provides a blanket impl for any `Error + Send + Sync + 'static`"
fix: "delete the explicit impl; the stdlib blanket impl is already there; `?` operator and `Into<BoxError>` work transparently via the blanket"
domain: rust, error-handling, stdlib
---

## Symptom

A typed error enum is meant to convert into `Box<dyn std::error::Error + Send + Sync>` (commonly aliased as `BoxError`) at trait boundaries:

```rust
type BoxError = Box<dyn std::error::Error + Send + Sync + 'static>;

#[derive(Debug, thiserror::Error)]
pub enum J17Error { /* variants */ }

// Explicit impl — looks innocent
impl From<J17Error> for BoxError {
    fn from(e: J17Error) -> Self {
        Box::new(e)
    }
}
```

Compile error:

```
error[E0119]: conflicting implementations of trait `From<J17Error>` for type `Box<dyn std::error::Error + Send + Sync>`
  --> src/error.rs:NN:1
   |
NN | impl From<J17Error> for BoxError {
   | ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
   |
   = note: conflicting implementation in crate `alloc`:
           - impl<E> From<E> for Box<dyn std::error::Error + Send + Sync>
             where E: std::error::Error + Send + Sync + 'static;
```

## Cause

stdlib provides a blanket impl in `alloc` (re-exported through `std`):

```rust
impl<E> From<E> for Box<dyn Error + Send + Sync + 'static>
where
    E: Error + Send + Sync + 'static
```

This already covers any user-defined error type that implements `Error + Send + Sync + 'static` (which thiserror-derived enums do automatically). Adding an explicit `impl From<MyError> for Box<dyn Error + Send + Sync + 'static>` overlaps with the blanket and fails coherence.

The blanket also means `?` works transparently — a function returning `Result<_, BoxError>` can `?` a `Result<_, J17Error>` directly without any explicit `From` impl.

[primary: stdlib `Box` impls — search "From<E> for Box<dyn Error"](https://doc.rust-lang.org/std/boxed/struct.Box.html#impl-From%3CE%3E-for-Box%3Cdyn+Error+%2B+Send+%2B+Sync%3E)

## Fix

Delete the explicit impl. Rely on the stdlib blanket:

```rust
type BoxError = Box<dyn std::error::Error + Send + Sync + 'static>;

#[derive(Debug, thiserror::Error)]
pub enum J17Error { /* variants */ }

// No explicit From impl — stdlib blanket covers it.
```

`?` works at trait boundaries:

```rust
fn append_event(...) -> Result<(), BoxError> {
    let result: Result<_, J17Error> = adapter.do_thing();
    let v = result?;  // J17Error → BoxError via stdlib blanket; no explicit impl needed
    Ok(())
}
```

Same applies to any other `Box<dyn Trait>` bounds where `Trait` has a blanket impl in stdlib — defining your own `From<MyType>` will conflict.

## Discovered

- Echo `agent/echo-adapter-j17` cycle 2026-05-07 (T-0122 implementer dispatch) — orchestrator surfaced as DECISIONS entry #11 in close report. Resolved at code-tier; saved a future REBOUND.
- echo-prismatic commit 6790b1a — j17 adapter relies on the blanket throughout.

## Pairs with

- `bugs/rust-reqwest-json-returns-reqwest-error.md` — separate Rust-adapter mechanics gotcha from the same cycle.

## Keywords

rust, From trait, Box<dyn Error>, BoxError, blanket impl, E0119, conflicting implementations, coherence, Error + Send + Sync, stdlib trait conflict, ? operator, error conversion, thiserror, adapter trait boundary
