---
symptom: "The echo daemon's tracing/stdout output never appears in journald despite echo.service having StandardOutput=journal and RUST_LOG=debug set by run-echo.sh. journalctl --user -u echo shows ONLY systemd meta lines (Started/Stopped); zero app lines by unit, by _COMM=echo, or by _PID=<MainPID>. Confirmed across multiple restarts and builds, 2026-07-04."
fix: "UNRESOLVED — no live root cause yet. Practical consequences + workarounds are the durable knowledge: (1) never claim 'no errors in the daemon logs' from journalctl — there are NO daemon logs there; absence is the broken pipe, not health; (2) live pipeline state comes from the MQTT taps (echo/+/tap/#) — full per-stage utterance JSON incl. classify verdict/action results/system_response; (3) app-log lines come from the ADR-214 debug stream at INFO level only (DEBUG self-loops, see sibling bug). Root-cause candidates to check when this gets its ticket: tracing fmt layer's pretty() writer buffering under non-tty stdout; the DebugLayerGate registry composition swallowing the fmt layer; stale echo-data/echo.log (dead since May 30) suggesting a historical file-writer path; systemd user-unit journal namespace quirks."
domain: echo, logging, journald, systemd, tracing, observability
---

## Symptom

`systemctl --user cat echo` shows `StandardOutput=journal` / `StandardError=journal`; `run-echo.sh` sets `RUST_LOG` (debug for first-party crates) and execs the binary. Yet:

- `journalctl --user -u echo` → only systemd Start/Stop meta lines, across the unit's whole history.
- `journalctl --user _COMM=echo`, `_PID=<MainPID>` → empty.
- `systemctl --user status echo` (shows recent journal tail) → only systemd lines.

`main.rs::init_tracing` composes `tracing_subscriber::registry().with(fmt pretty layer).with(mqtt debug layer)` — the fmt layer SHOULD write to stdout → journald. It observably doesn't (or writes nowhere).

## Why it matters

Every "check the daemon logs" instinct silently returns nothing, which reads as "no errors" — a false-negative trap (see `feedback_verify_negative_claims_from_logs`: know the success-path log signature before claiming absence). During the 2026-07-04 wet-test this forced the decline-vs-error question on the success pass to be answered by out-of-band API replays instead of one grep.

## Where the real signals live (until fixed)

| Need | Surface |
|---|---|
| Pipeline state per utterance | `mosquitto_sub -v -t 'echo/+/tap/#'` — stt/speaker/auth/intent/action/utterance stages, full JSON |
| App log lines, live | ADR-214 stream: `{"action":"start","level":"INFO"}` → `echo/debug/log` (INFO only — DEBUG self-loops) |
| Service health | `systemctl --user is-active/show echo` (NRestarts, timestamps) |
