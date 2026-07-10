---
symptom: "compile error E0631 / type mismatch when constructing a thiserror enum variant intended to wrap `serde_json::Error` in a `map_err` after `reqwest::Response::json::<T>()` — variant expects serde_json::Error but reqwest gives reqwest::Error"
fix: "make the variant wrap `reqwest::Error` (not `serde_json::Error`); reqwest bundles response-body deserialization failures into its own error type"
domain: rust, reqwest, error-handling
---

## Symptom

A typed error enum has a variant intended for "decode failure when reading the HTTP response body":

```rust
#[derive(Debug, thiserror::Error)]
pub enum J17Error {
    #[error("network: {0}")]
    Network(#[from] reqwest::Error),
    #[error("decode: {0}")]
    Decode(#[from] serde_json::Error),  // <-- intended for body deserialization
}
```

Calling `.json::<T>()` on a `reqwest::Response` and wrapping the error:

```rust
let body: AppendResponse = res.json().await.map_err(J17Error::Decode)?;
```

produces a compile error like:

```
error[E0631]: type mismatch in function arguments
  --> src/store.rs:NN:NN
   |
NN |     let body: AppendResponse = res.json().await.map_err(J17Error::Decode)?;
   |                                              ^^^^^^^ expected `reqwest::Error`, found `serde_json::Error`
   |
   = note: expected fn pointer `fn(reqwest::Error) -> _`
              found fn pointer `fn(serde_json::Error) -> _`
```

Both `From` impls — for `reqwest::Error` and for `serde_json::Error` — exist on the enum, but `?` would now route the body-decode through the `Network` variant (semantically misleading), and the explicit `map_err` won't compile.

## Cause

`reqwest::Response::json::<T>()` returns `Result<T, reqwest::Error>` — NOT `Result<T, serde_json::Error>`. reqwest's error type bundles transport, decoding, and HTTP-status failures into a single `reqwest::Error` with internal classification. When the body fails to deserialize as `T`, reqwest constructs a `reqwest::Error` whose `is_decode()` returns `true` and which wraps the underlying serde error internally (via `Error::source()`).

This is reqwest's deliberate API choice — callers don't have to import `serde_json` or pattern-match on a different error type for body parsing. The downside is that `serde_json::Error` is unreachable through this path.

[primary: reqwest docs — `Response::json`](https://docs.rs/reqwest/latest/reqwest/struct.Response.html#method.json)

## Fix

Make the decode variant wrap `reqwest::Error`:

```rust
#[derive(Debug, thiserror::Error)]
pub enum J17Error {
    #[error("network: {0}")]
    Network(#[from] reqwest::Error),
    #[error("decode: {0}")]
    Decode(reqwest::Error),  // body-decode failures from reqwest, not serde directly
}
```

Note: with both variants wrapping `reqwest::Error`, `#[from]` only on `Network` — pick which variant `?` should route to (Network), and explicit-construct `Decode` only when you can distinguish at the callsite (e.g. via `err.is_decode()`). Or drop the distinction entirely and have a single `Network(reqwest::Error)` variant.

If you genuinely need the underlying serde error for inspection, `reqwest::Error::source()` returns the wrapped cause chain.

## Discovered

- Echo `agent/echo-adapter-j17` cycle 2026-05-07 (T-0122 implementer dispatch) — orchestrator surfaced as DECISIONS entry #10 in the close report. Resolved at code-tier; saved a future REBOUND.
- echo-prismatic commit 6790b1a — j17 adapter implementation lands the variant correctly.

## Pairs with

- `bugs/rust-blanket-from-for-boxerror-conflicts-with-explicit-impl.md` — separate Rust-adapter mechanics gotcha from the same cycle.

## Keywords

rust, reqwest, Response::json, serde_json::Error, reqwest::Error, E0631, type mismatch in function arguments, thiserror, map_err, decode error variant, body deserialization, fn pointer mismatch, From impl, error-handling pattern, adapter implementation
