---
symptom: "serde untagged enum deserialization fails on a JSON-RPC-ish protocol where one variant has no top-level `message` (or other discriminant) field — variants with the field deserialize fine but the field-less one always falls through to the catchall or errors"
fix: "implement custom `Deserialize` that two-stages: parse to `serde_json::Value`, branch on field presence/absence, then deserialize to the target variant — avoids the untagged-enum's first-match-wins ambiguity"
domain: rust, serde, json-rpc
---

## Symptom

A protocol like Speechmatics' realtime WebSocket sends server-to-client messages where MOST messages carry a `message` field naming the type, but ONE common variant doesn't:

```json
{"message": "RecognitionStarted", "id": "..."}
{"message": "AddTranscript", "metadata": {...}, "results": [...]}
{"seq_no": 42}                   // ← AudioAdded — no "message" field
{"message": "Error", "code": "...", "reason": "..."}
{"message": "EndOfTranscript"}
```

The natural Rust modeling is an internally-tagged enum:

```rust
#[derive(Deserialize)]
#[serde(tag = "message")]
enum ServerMessage {
    RecognitionStarted { id: String },
    AddTranscript { metadata: Metadata, results: Vec<Result> },
    AudioAdded { seq_no: u64 },        // ← can't tag this; no "message" field on wire
    Error { code: String, reason: String },
    EndOfTranscript,
}
```

This errors at runtime: `missing field 'message'` when AudioAdded frames arrive.

The next attempt is `#[serde(untagged)]`:

```rust
#[derive(Deserialize)]
#[serde(untagged)]
enum ServerMessage {
    RecognitionStarted { message: String, id: String },  // String discriminant via field equality
    // ...
    AudioAdded { seq_no: u64 },
}
```

This is fragile — untagged enums use **first-successful-deserialize-wins**, and `AudioAdded { seq_no }` will match any object with a numeric `seq_no` field even if other discriminating fields are absent. With overlapping field-shapes, ordering matters and silent misclassification creeps in.

## Cause

`#[serde(tag = "...")]` requires the discriminator field to exist on every variant on the wire. `#[serde(untagged)]` doesn't use a discriminator at all — it tries variants in declaration order and accepts the first that successfully deserializes, which is fragile when variants share field-shapes.

JSON-RPC-ish protocols where ONE variant deviates from the discriminator convention are a common middle case neither serde attribute handles cleanly.

## Fix

Implement custom `Deserialize` for the enum. Two-stage: parse to `serde_json::Value`, branch on field presence, then deserialize to the target variant.

```rust
use serde::{Deserialize, Deserializer};
use serde_json::Value;

#[derive(Debug)]
pub enum ServerMessage {
    RecognitionStarted { id: String },
    AddTranscript { metadata: Metadata, results: Vec<TranscriptResult> },
    AudioAdded { seq_no: u64 },
    Error { code: String, reason: String },
    EndOfTranscript,
}

impl<'de> Deserialize<'de> for ServerMessage {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = Value::deserialize(deserializer)?;

        // Branch on the presence-absence of the "message" discriminator.
        if let Some(message_type) = value.get("message").and_then(|v| v.as_str()) {
            match message_type {
                "RecognitionStarted" => {
                    let id = value.get("id").and_then(|v| v.as_str()).ok_or_else(|| {
                        serde::de::Error::custom("RecognitionStarted missing id")
                    })?.to_string();
                    Ok(ServerMessage::RecognitionStarted { id })
                }
                "AddTranscript" => {
                    let parsed: AddTranscriptInner = serde_json::from_value(value)
                        .map_err(serde::de::Error::custom)?;
                    Ok(ServerMessage::AddTranscript {
                        metadata: parsed.metadata,
                        results: parsed.results,
                    })
                }
                "Error" => { /* ... */ }
                "EndOfTranscript" => Ok(ServerMessage::EndOfTranscript),
                other => Err(serde::de::Error::custom(format!("unknown message type: {other}"))),
            }
        } else if value.get("seq_no").is_some() {
            // AudioAdded — no "message" field on wire
            let seq_no = value.get("seq_no").and_then(|v| v.as_u64()).ok_or_else(|| {
                serde::de::Error::custom("AudioAdded missing seq_no")
            })?;
            Ok(ServerMessage::AudioAdded { seq_no })
        } else {
            Err(serde::de::Error::custom(format!(
                "unrecognized server message shape: {}",
                value
            )))
        }
    }
}

// Helper struct for the AddTranscript parsing path so serde derives the inner shape.
#[derive(Deserialize)]
struct AddTranscriptInner {
    metadata: Metadata,
    results: Vec<TranscriptResult>,
}
```

Two-stage parse means: (1) untyped JSON → `Value`, (2) branch on field presence → typed variant. The pattern is verbose but explicit; serde's automatic-derive can't express "branch on field-set presence" without a custom impl.

## Discovered

- Echo `agent/echo-adapter-speechmatics` cycle 2026-05-08 (T-0134 implementer dispatch). Implementer surfaced as DECISIONS entry #13 in close report — Speechmatics' `AudioAdded` frame has no `message` field; an internally-tagged or untagged enum both produce ambiguity. Custom Deserialize is the minimum-correct impl. Saved a REBOUND.
- echo-prismatic commit 0436676 — `crates/echo-adapter-speechmatics/src/types.rs` implements this pattern.
- Brief authoring lesson (per orchestrator's surface flag): research notes documented the AudioAdded shape but didn't call out the missing-discriminant serde pitfall. Future briefs covering vendor JSON-RPC-ish protocols should include a one-liner on the discriminant pattern at brief-time, not implementer-surface-time.

## Pairs with

- `bugs/rust-blanket-from-for-boxerror-conflicts-with-explicit-impl.md` — adjacent Rust-mechanics class (stdlib blanket impl conflict).
- `bugs/rust-reqwest-json-returns-reqwest-error.md` — adjacent Rust-mechanics class (typed-error wrapping pitfall).

## Keywords

rust, serde, deserialize, untagged enum, internally-tagged enum, serde tag attribute, message discriminant, JSON-RPC, websocket protocol, custom Deserialize impl, two-stage parse, serde_json::Value branch, Speechmatics, AudioAdded, ServerMessage enum, missing discriminator field, first-successful-deserialize-wins, ambiguous variant, vendor protocol modeling
