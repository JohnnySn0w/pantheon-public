# Claude account-revocation disaster recovery

Use this path only for corroborated provider authentication loss: a
line-anchored provider banner (`API Error: 401 ... Invalid authentication
credentials`, `Please run /login`, or an explicit account/organization
suspended/revoked/disabled banner) in the pane's active region, i.e. its last
lines, never scrollback. At launch, two consecutive post-start samples must
carry it. Mid-session, the second hit must be a NEW banner line (a fresh
failed turn); a static pane never escalates. A usage limit remains a
subscription-cap; a launch timeout remains transient.

The runtime controller records `authentication_loss` with no expiry. It does
not retry Claude until an operator completes the OAuth recovery procedure and
explicitly clears that runtime's cooldown. It preserves the existing handoff,
mailbox, and controller state and selects the next viable runtime in manifest
order. The code does not require that runtime to be Codex. On today's
manifests the next viable runtime is the declared Codex fallback; confirm
that for the seat before a drill.

Receiver transfer uses the existing runtime-controller handoff: stop/prove the
Claude receiver absent, start/prove the Codex receiver present, and reject a
second poller. Do not start a receiver manually. Orchestrator retains dispatch
authority; Consultant and Watchman remain the out-of-band operator paths if the
shared provider is unavailable.

## Recovery and return

Follow `sop/remediation/agent-oauth-relogin.md`; never create, copy, reveal, or
revoke credentials. After successful login is independently verified, clear
the `authentication_loss` marker with
`tools/harness.py --operator <you> <seat> clear-cooldown claude/<profile> --reason ...`
(it resets the untimed failure class exactly like `runtime_blocked`).
The controller's ordinary fallback-return path then restores Claude. Verify
operator channel delivery, mailbox continuity, dispatch, and
`tools/verify_startup.sh --agent <seat> --no-dewedge` (rc 0).

## Rehearsal

First use a temporary state root and captured pane fixtures to prove two-hit
classification, permanent exclusion, Codex selection, and single-receiver
handoff. A supervised live drill may inject only the classification outcome;
it must not alter credentials. Stage the cooldown-clear rollback before the
drill and retain the four evidence classes above.
