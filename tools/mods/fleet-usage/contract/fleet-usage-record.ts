// T-4510 contract fixture: the per-seat file the fleet-usage mod writes, and that
// tools/fleet_usage_gate.py reads. ONE committed copy, read by both sides:
//   - the mod test (tests/fleet-usage.test.ts) imports it and asserts the mod produces it
//     from the live specialist rate-limit shape;
//   - tools/test_fleet_usage_gate.py parses everything after the `export default` marker
//     below as strict JSON and asserts the gate accepts it.
// It is a .ts file only because the engine loads (and a mod test can import) files named like
// code, not .json. Keep the object after `export default` STRICT JSON (quoted keys, no
// trailing commas, no comments inside it) or the Python side fails.
//
// Provenance (D11): seven_day / five_hour are the values observed live on specialist
// 2026-10-04T18:2xZ (Orchestrator): {"five_hour":{"resets_at":1791151200,"used_percentage":13},
// "seven_day":{"resets_at":1791738000,"used_percentage":3}} -- integer percentages, epoch
// seconds. ts is fixed (2026-10-04T18:53:20Z) so the file is reproducible.
export default {
  "schema": "pantheon-fleet-usage/v1",
  "ts": 1791140000,
  "seat": "specialist",
  "seven_day": {
    "used_percentage": 3,
    "resets_at": 1791738000
  },
  "five_hour": {
    "used_percentage": 13,
    "resets_at": 1791151200
  }
}
