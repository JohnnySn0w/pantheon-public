// T-4536 contract fixture: ONE per-turn ledger row as the fleet-usage mod appends it to
// <fleet-usage-dir>/turns/<seat>/<YYYY-MM-DD>.jsonl, and as tools/fleet_usage_ledger.py reads
// it. ONE committed copy, read by both sides:
//   - the mod test (tests/turn-ledger.test.ts) imports it and asserts the mod produces exactly
//     this row from the turn.complete event built there (TURN_EVENT);
//   - tools/test_fleet_usage_ledger.py parses everything after the `export default` marker as
//     strict JSON, asserts the reader accepts it, and asserts its key list equals the reader's
//     CLAUDE_ROW_KEYS (so the two key allowlists cannot drift).
// Keep the object after `export default` STRICT JSON (quoted keys, no trailing commas, no
// comments inside it) or the Python side fails.
//
// Provenance (D11): the event shape is Claude Code 2.1.316's declared turn.complete input
// (TurnCompleteFields: turnId, agentId?, durationMs, reason, usage?: TurnUsage = the four
// ModelUsage counts + model), read from the plugin-authoring types; the values are synthetic.
// No live turn.complete capture exists yet: the first live row after rollout is the check.
// ts is NOW_MS of the mod test (2026-10-04T18:53:20Z), so the file is reproducible.
export default {
  "schema": "pantheon-fleet-usage-turn/v1",
  "ts": 1791140000,
  "harness": "claude",
  "seat": "specialist",
  "session_id": "5f0c2a8e-1d4b-4c1e-9a7f-2b6d3e8f9a01",
  "turn_id": "turn_01HZX9Q2V7K3M8N4P5R6S7T8U9",
  "agent_id": null,
  "agent_type": null,
  "parent_agent_id": null,
  "model": "claude-opus-5-5",
  "model_source": "usage",
  "input_tokens": 1200,
  "output_tokens": 850,
  "cache_read_input_tokens": 48000,
  "cache_creation_input_tokens": 3100,
  "duration_ms": 14250,
  "reason": "answer"
}
