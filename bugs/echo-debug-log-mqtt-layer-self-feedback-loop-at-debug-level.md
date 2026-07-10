---
symptom: "Enabling Echo's ADR-214 MQTT debug-log streaming at DEBUG level ({\"action\":\"start\",\"level\":\"DEBUG\"} on echo/debug/log/control) triggers a self-amplifying feedback loop: rumqttc logs its own publishes of echo/debug/log messages at DEBUG, each of which becomes another debug-log publish. Observed 2026-07-04: 4.5M log lines in ~3 minutes, all rumqttc::state 'Publish. Topic = echo/debug/log' noise, drowning every real log line and loading broker+CPU."
fix: "Immediate: only use level INFO or higher on the control channel ({\"action\":\"start\",\"level\":\"INFO\"}) — rumqttc's publish logs are DEBUG, so INFO breaks the loop. Send {\"action\":\"stop\"} to kill a runaway loop. Real fix (unfiled follow-up): the mqtt debug layer's filter must structurally exclude the rumqttc/log-forwarding targets (log.target=rumqttc::*) regardless of requested level — a target-based exclusion in debug_log.rs's layer filter, so DEBUG level becomes safe for first-party crates."
domain: echo, debug-log, adr-214, mqtt, rumqttc, tracing, feedback-loop
---

## Symptom

During the 2026-07-04 live wet-test debugging, enabling the ADR-214 debug stream at DEBUG level produced a runaway: the capture file grew to 4,500,000+ lines in about three minutes. Sampling showed essentially every line was `rumqttc::state` logging its own `Publish. Topic = echo/debug/log` — i.e., the act of forwarding a log line over MQTT generates a DEBUG log line, which gets forwarded, which generates another.

Real diagnostic content (the `generate_success_response` decline/warn lines being hunted at the time) was unfindable in the flood — `grep -c "success_response"` over the full capture returned 0 against 4.5M rumqttc lines.

## Why it matters

The debug stream is the ONLY live app-log surface while the stdout→journald path is broken (see `echo-daemon-stdout-never-reaches-journald.md`) — and at the level you'd most want (DEBUG), it self-destructs. This burned the one live-repro window during the wet-test; root-causing had to fall back to out-of-band probe replays.

## Workaround discipline

- Start the stream at INFO: `mosquitto_pub -t 'echo/debug/log/control' -m '{"action":"start","level":"INFO"}'`
- Always stop it when done: `{"action":"stop"}` — the gate persists across your subscriber exiting.
- If a DEBUG loop is already running: publish stop; the gate check kills it immediately.

## Real fix shape (for the eventual ticket)

`debug_log.rs`'s MQTT layer should drop events whose target starts with `rumqttc` (and any other transport-of-the-transport targets) BEFORE the level gate — a structural self-exclusion, making DEBUG safe for the first-party `echo*` crates it exists to observe. The RUST_LOG env filter on the fmt layer already scopes debug to first-party crates; the MQTT layer needs the equivalent.
