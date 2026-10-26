---
aliases: [ORG-103]
id: ORG-103
title: "Codex seats receive input through the codex app-server, not by typing into the TUI"
status: "Ratified 2026-10-15 by the operator (TG 11877, T-5013). Proposed 2026-10-14 by Recovery (TG 1887). Requested in TG 8350; canary on consultant per TG 8354; trimmed to the decision per TG 8358."
date: 2026-10-14
author: Recovery (head_system_orchestrator)
steward: recovery
appetite: 6
riskiest_assumption: "That a codex app-server can host a seat's live session for days while the TUI stays attached and usable. The surface is marked [experimental], and only short runs have been observed (T-4988 probe). The consultant canary must prove it before other seats move."
build_disposition: needs-build
scoping_ticket: T-4988
relates: ORG-026, ORG-031, ORG-053, ORG-091
ratifiable_files: [pantheon-config:architecture/adr/ORG-103-codex-seat-delivery-via-app-server.md]
---

# ORG-103: Codex seats receive input through the codex app-server

**Status:** Ratified 2026-10-15 by the operator (TG 11877, T-5013). Proposed 2026-10-14 by Recovery.

## Decision

1. **Codex seats take input through codex's own API, not by typing into the screen.** Each codex seat's session runs on a codex app-server. Telegram DMs, mail, ticket pokes and controller prompts are delivered as API turns. The TUI pane stays attached to the same session, so it works for watching and typing exactly as it does today.
2. **Delivery is verified, not guessed.** A message counts as delivered only when the agent has demonstrably received it. A delivery that can't be confirmed is retried or raised as an alert, never silently dropped.
3. **Canary on consultant first**, then the other codex seats. The current typing path stays as a logged fallback until each seat passes.

## Why

- **Today's approach keeps failing.** Every message into a codex seat is typed into its TUI with tmux, and the system guesses from the screen whether it was sent. That guess fails in practice: T-4792, T-4983 and T-4987, including the operator's DMs to specialist sitting unsent for up to an hour (TG 8346/8348).
- **Claude seats don't have this problem**, because Claude Code delivers channel messages into the session natively.
- **Codex already has a proper API for this.** Its app-server can start and resume sessions, start turns, add input mid-turn, interrupt, and report when a turn starts and finishes. A TUI can attach to the same session. All of this was verified on codex 0.147.0 (T-4988 probe).

## Alternatives considered

- **Keep typing and patch each bug.** This still guesses from the screen, and each new screen state reopens the bug. It stays only as the interim fallback.
- **One `codex exec` per message.** Reliable, but each message loses the seat's live session.
- **Move seats off codex.** That's a cost decision for the operator, and not what this ADR decides.

## Consequences

- Messages to codex seats become reliable and auditable, and most of the screen-scraping for codex can be retired.
- It's a real build: the controller and monitors currently treat a seat's runtime as a tmux pane.
- The app-server is labelled experimental. Its version is pinned and contract-tested on every codex upgrade.

## Build

All operational detail lives in the build note `notes/2026-10-14-t4988-codex-appserver-build.md` (T-4988), not in this ADR: delivery ids and acknowledgements, writer rules, message priority, the safety-setting ceiling, socket security, the recovery contract, dialog handling, and the canary acceptance checklist. It also contains the adversarial review's findings and how each is addressed.
