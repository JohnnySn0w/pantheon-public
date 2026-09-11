---
symptom: "adding an event to echo-core event_registry! reddens spec_consistency_test and 404s at runtime"
fix: "declare the event under spec.aggregate_types.<agg>.events.<type> in echo-adapter-j17/spec/echo.json too, mirroring the closest sibling"
domain: "echo / j17"
---
## Symptom

After adding a new event to `crates/echo-core/src/events.rs` `event_registry!` (e.g. `HadSpeakerDeactivated => { event_type: "had_speaker_deactivated", aggregate_type: "speaker" }`), the workspace test fails:

```
---- every_registry_event_is_declared_in_deployed_spec stdout ----
echo.json is missing (aggregate_type, event_type) pairs present in event_registry!:
  [("speaker", "had_speaker_deactivated"), ("speaker", "had_speaker_reactivated")]
```

And at **runtime** j17 404s any `append` against a `(aggregate_type, event_type)` pair not declared in the deployed spec (`crates/echo-adapter-j17/spec/echo.json`) — the event is silently lost (best-effort appends are warn-not-fatal, so the surrounding op still succeeds).

## Cause

`echo-core`'s `event_registry!` is the single source of truth for `(event_type, aggregate_type)` pairs, but j17 validates every append against the **separately-deployed** spec `echo.json`. `spec_consistency_test.rs` (T-1765) asserts the forward direction: every registry entry must be declared in `echo.json`. Adding a registry event without the matching spec entry breaks that invariant.

**The trap:** scoped `cargo test -p echo-core` / `-p echo <filter>` / `-p echo-adapter-speechmatics` do NOT run the `echo-adapter-j17` crate's `spec_consistency_test`, so the defect passes every scoped gate (and even a diff-only review). Only the **full `cargo test --workspace`** (the authoritative pre-push gate) catches it.

## Fix

Add the event under `spec.aggregate_types.<aggregate_type>.events.<event_type>` in `crates/echo-adapter-j17/spec/echo.json`, mirroring the closest existing sibling's `schema` / `handler` / `allow_skip_occ` (e.g. `had_speaker_deleted` for a speaker-aggregate event):

```json
"had_speaker_deactivated": {
  "schema": { "type": "object", "additionalProperties": true },
  "handler": [ { "set": { "target": "", "value": "$.data" } } ],
  "allow_skip_occ": true
}
```

Then re-run the full workspace gate. **Also:** `echo.json` is the *deployed* control-plane spec — the running j17 server needs the updated spec PUT to it (per `notes/j17-deploy-procedure.md`, control-plane JWT) before the new event actually persists; until then the append 404s (graceful warn-not-fatal degrade).

## Verification

```
# RED before the echo.json entry, GREEN after:
cargo test -p echo-adapter-j17 --test spec_consistency_test
```
Confirmed on T-1657 (2026-09-08): adding `had_speaker_deactivated`/`had_speaker_reactivated` to the registry reddened this test; adding the two `echo.json` entries greened it. The scoped `-p echo-adapter-speechmatics` / `-p echo config_control` runs stayed green throughout — only the j17 crate's test / the full workspace gate surfaced it.
