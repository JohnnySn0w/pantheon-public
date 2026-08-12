---
aliases: [ORG-026]
id: ORG-026
title: Inter-agent message-passing substrate — daemonless file-mailbox (Maildir) + systemd --user .path wake
status: Ratified 2026-06-05 by the operator (amendment — §4 resolves to LIFT design/DIY, not adopt-dependency; attack-surface minimization)
build_disposition: built
---

# ORG-026 — Inter-agent message-passing substrate: daemonless file-mailbox (Maildir) + systemd `--user .path` wake

**Status:** **Ratified 2026-06-05 by the operator** (Telegram review, via ratify ticket [[T-0836]]). Per ORG-015 `adr/` is the ratifiable substrate. **Ratification amendment:** §4 resolves to **LIFT the design (DIY reimplementation), NOT adopt the `avivsinai/agent-message-queue` package wholesale** — the operator's call, to minimize attack/supply-chain surface. T-0773 is therefore a *design study to lift from*, not a vendoring.

**Ticket:** [[T-0770]] (Orchestrator design epic — Pantheon inter-agent message-passing system; deliverable 3 = "fold survey in, pick a direction, promote to an ADR"). Survey delivered under [[T-0771]] (Specialist).

**Related:** [[ORG-004]] (the Telegram transport bridge, decommissioned 2026-06-04 via T-0767 — the cautionary prior-art: a single-consumer, single-purpose message layer whose SSE competing-client class we are designing out), the Veneration epic [[T-0765]]/[[T-0766]] (Recovery — cross-instance resume harness; **sit-beside, shared lease primitive — see §Relationship to Veneration**), `bugs/team-pattern-sendmessage-intermittent-wake-failure.md` (the wake-drop failure class this must design out). Source survey: `knowledge/episodic/2026-06-04-agent-message-passing-survey/notes.md`. Design note (requirements): `notes/2026-06-04-inter-agent-message-passing-design.md`.

## Context

Pantheon runs three long-lived, independent agent instances as separate OS users on one VPS sharing one filesystem — Orchestrator (HO, `claude`), Specialist (specialist, `claude-research`), Recovery (recovery, `claude-fallback`). They must coordinate, but there is **no reliable, bidirectional, persistent, wake-capable channel between standing instances**. Today's three improvised mechanisms each fail a different requirement:

- **Ticket queue** — durable + auditable + any↔any, but async with no wake (grooming-cycle latency); it is coordination, not signalling.
- **`tmux send-keys`** — instant wake, but one-directional by sudo design (Recovery→Orchestrator only), brittle, no delivery guarantee.
- **Team `SendMessage`** — bidirectional + instant, but the wake-hook drops intermittently (issue #23415, closed *not planned*; cost a 5h silent stall, b36), is team-scoped, and does not survive restart.

The T-0767 bridge cutover is what surfaced the gap concretely: HO could not signal Recovery to drive the cutover — coordination had to route through the operator. The requirements (full text in the design note §Requirements) are: **(1) bidirectional, (2) reliable delivery + wake, (3) persistent across restart, (4) not team-scoped, (5) auditable, (6) low-ceremony for the common case.**

Specialist's 4-prong landscape survey (T-0771) scored 14 candidate substrates against these six requirements. Two findings are load-bearing for this decision:

1. **No transport wakes an LLM.** Convergent across 3 of 4 research prongs: brokers / MCP / sockets move and persist bytes, but a Claude Code session only acts when a turn is injected *locally* (a consumer does `tmux send-keys` / a SessionStart kick). **"Wake" is therefore a Pantheon-side adapter concern, identical no matter which transport is chosen** — and Pantheon already owns that adapter. This collapses the decision to *pick the simplest durable transport*. On a single host with a shared FS, that is files.
2. **Exactly one candidate scores ✓ on all six requirements: the file-mailbox** (Maildir-style), and it is **daemonless** — which directly answers the design note's open question ("is daemonless viable, given we just deleted a bespoke daemon for cleanliness?") with *yes, and it is the best fit*.

## Decision

Adopt a **per-agent file-mailbox as the inter-agent message-passing substrate**, with the following shape:

1. **Transport — Maildir-style per-agent inbox.** Each instance owns an `inbox/{tmp,new,cur}` directory, group-readable/writable via `pantheon-shared`. Delivery is atomic: write to `tmp/`, `fsync`, `rename` into `new/` (atomic on same FS), then `fsync` the dir for power-loss safety. A message *is a file in `new/`*. Consumption acks by moving `new/`→`cur/`. Multi-writer-safe by unique-name + rename (no locks); ordering via sortable names (single host = one clock). The one-line poke for the common case is "write a file" to the target's inbox — symmetric, no elevated/asymmetric sudo.

2. **Wake — daemonless, via systemd `--user .path` unit (inotify under the hood).** A per-user `.path` unit stays `active(waiting)` holding the watch with no process running; on `PathChanged=` it triggers a oneshot `.service` that fires the existing Pantheon kick (`tmux send-keys` / SessionStart `initialUserMessage`). Requires `loginctl enable-linger`. This **reuses Pantheon's existing wake mechanism rather than inventing new wake tech** — consistent with finding (1).

3. **REQ2 acceptance bar (the precondition that makes "reliable wake" honest): "inotify is a hint, `readdir(new/)` is the truth."** The consumer rescans `new/` authoritatively on every wake + a slow backstop timer + SessionStart, and handles `IN_Q_OVERFLOW`. Acks are at-least-once via ack-by-move; consumers must be idempotent. This designs out the exact silent-drop failure that killed both the team `SendMessage` path and the retired Telegram-SSE bridge — and it is the concrete acceptance gate for the PoC.

4. **Lift the design, do not adopt the dependency** (RATIFIED RESOLUTION, the operator 2026-06-05 — supersedes the original "adopt-or-lift" framing). [`avivsinai/agent-message-queue`](https://github.com/avivsinai/agent-message-queue) is a direct hit — daemonless Maildir (`tmp`→fsync→rename), DLQ, delivery receipts, experimental inotify wake, "no server/daemon/database." **It is a design to study and re-implement ourselves (DIY), NOT a package to vendor** — the operator's call, minimizing attack/supply-chain surface (no external code path into the inter-agent substrate). T-0773 is the design-lift study (extract the patterns: atomic-write durability, DLQ shape, ack semantics), feeding our own implementation; it is not an adoption/evaluation-to-vendor.

5. **Runner-up recorded, not adopted: MQTT on the already-running mosquitto** (persistent session `clean_session=false`, QoS≥1). It is the fallback *if a broker is ever preferred over files* — it adds network/broker indirection where files need none, and carries two gaps to close (no native audit → add a `#`-subscriber logger; protocol-ACK ≠ agent-acted → app-level ack on a reply topic after the kick lands). Spiked under T-0775 to keep the option warm, **not** on the build path unless the file-mailbox PoC fails its acceptance bar.

6. **Rejected with reasons (so they are not re-litigated):** team `SendMessage` as sole path (unfixed wake bug, team-scoped, no ack/retry), all LLM agent-frameworks (in-process; fail REQ3/REQ4 by construction), ZeroMQ (brokerless, no persistence — rebuilding a bespoke transport repeats the bridge mistake harder), unix sockets / FIFOs (both-ends-live, no store-and-forward), MCP-as-bus (pull-only — solves transport but not wake, re-adds a daemon), Google A2A (per-agent HTTP daemon + still needs a local webhook→kick; built for cross-vendor/cross-network, architecturally mismatched to same-host/3-user). Captured in full in the rejected-options note (T-0776).

## Implementation reality — amends Decision items 1 & 2 (Recovery, 2026-06-09; deployed + proven; Adopted-as-deployed-reality 2026-06-16 per principles-as-ratification — impl-reality amendment to an already-ratified ADR, below the goal-level bar)

First live cross-account deployment surfaced two structural facts that revise items 1–2 as built. Both trace to one root: a `systemctl --user` unit (and the user-systemd *manager* itself) carries ONLY the account's primary group — user systemd cannot grant supplementary groups (`SupplementaryGroups=` fails on `--user` units). Recorded here so they are not re-derived:

1. **Inbox location = the shared hub, group `veneration` (amends item 1's per-home `~/inbox` + `pantheon-shared`).** Per-home `~/inbox` is **untraversable cross-account** — agent homes are mode 750 (private, primary-group), so no peer can reach another's `~/inbox` to deliver. Took the §"Conditionally a shared HOME" option: inboxes live at **`/home/the-ancestor/mailbox/<agent>/{tmp,new,cur}`** (mode `2770`, group **`veneration`** — gid 1006, the mail group; NOT `pantheon-shared`), with the hub home at `0710 veneration` (group traverse-only). Senders write under `sg veneration` (their live process lacks the group). the-ancestor + codex were added to `veneration` for this (root usermod, T-1010).

2. **Wake = clock-driven timer, NOT the `.path` inotify watch (amends item 2).** The `.path` watch is **dead under the group-gated hub**: the watch is established by the agent's user-systemd *manager*, which lacks `veneration` and so cannot traverse the `0710` hub home to watch the subdir (confirmed: a hands-off delivery never fired it; the manager can't be `sg`-wrapped). Replaced by **`pantheon-mailbox-wake@.timer`** (`OnUnitActiveSec=5min`) — it fires on a clock (no hub access needed), triggering the oneshot wake `.service`, which **MUST `sg veneration -c`-wrap** so it has the group to read the hub at run time. REQ2 (item 3) is unchanged — the timer is only the trigger; the waker still `readdir(new/)` authoritatively, so it is idempotent + marker-debounced (re-runs never re-kick). Trade-off: ≤5 min worst-case wake latency, acceptable for inter-agent mail. The `.path` unit file is kept (DEPRECATED header) for history; it is no longer installed.

**Status (2026-06-09):** Recovery↔Specialist provisioned on the hub; autonomous wake **proven end-to-end hands-off** (delivered-while-idle → timer woke recipient → consumed + acked, no human in loop). codex + the-ancestor are delivery-capable (in `veneration`, hub subdirs exist) but their **consume** wiring is Phase 2 (codex TUI sentinel mismatch; the-ancestor consumes via its Veneration daemon, not a pane-kick). See `tools/{mailbox,mailbox_wake,provision_mailbox}.*` and memory `project_veneration_build_state`.

**Rollout status (2026-06-12, [[T-1017]]):** the 2026-06-09 line above originally read "Recovery↔Specialist↔**Orchestrator** provisioned" — that was inaccurate at the time (Orchestrator's *earlier* provisioning pointed at the now-dead per-home `~/inbox` + `.path` design, NOT the hub; the hub re-provision was blocked behind a stale clone lacking `MAILBOX_HUB` until 2026-06-12). **Now corrected as built:** Orchestrator is **hub-provisioned + autonomous-wake proven** (2026-06-12, T-1017 Part 1, operator-supervised): `provision_mailbox.sh --apply` installed the wake timer + linger; a Recovery→Orchestrator round-trip confirmed deliver → 5-min timer fired → `sg veneration` wake → pane-kick → Orchestrator consumed (`new/`→`cur/`), with Orchestrator's RT ack returned through the hub. So the **proven** hub triad is now **Recovery↔Specialist↔Orchestrator**. **Remaining (Phase 2, [[T-1106]]):** wire codex + the-ancestor to *consume* — both are delivery-capable but lack working wake/consume. Needs the wake-kick made harness-aware (`send_keys_submit.sh` is hardcoded to the claude-code paste-sentinel, which false-passes on codex's TUI — the T-1046/T-1054 class) + codex (sandboxed, run-bare) and the-ancestor (`ancestor_consume.py` daemon) provision/verify.

## Relationship to Veneration (sit-beside, not subsume)

Per T-0777 (Recovery, 2026-06-04), message-passing and Recovery's Veneration epic are **distinct subsystems, orthogonal problems**: message-passing = live↔live signalling between RUNNING instances (deliver + wake + persist); Veneration = a live instance reconstructing context from a DEAD predecessor (`claude --resume` on a dead session's jsonl). Different objects, different ops; subsuming either distorts it. **What they share — and where "don't build two overlapping things" actually bites:**

- **The single-holder `flock` lease + T-0584 stale-takeover primitive.** Both Veneration's mutex and a file-mailbox consumer need it → **factor it out once as a shared util; both consume it.**
- **Conditionally, a shared HOME.** Because this ADR takes the daemonless file+watch direction, the mailbox *can* co-locate in `/home/the-ancestor` (the neutral group-accessible shared-STATE account) alongside the Veneration lease. (Had we chosen a broker/daemon, it would have needed its own home to preserve the-ancestor's clean channel-less-passive property — another point in favour of the daemonless choice.)

**Sequencing:** Veneration (T-0765/0766) proceeds first; it defines the lease primitive + the-ancestor account that message-passing then reuses. No message-passing *build* starts before the lease primitive lands and the T-0777 coordination is closed.

## Justifies via

- **P1 (architecture over policy).** The structural facts of the topology — one host, shared FS, restart-prone, an already-trusted on-disk queue — make files the lowest-entropy durable transport. A broker/framework adds a moving part that buys nothing the structure does not already give. This is a structural read, not a preference.
- **D11 (verify the real surface) + P6 (preserve the tension).** "No transport wakes an LLM" is the load-bearing correction: wake is scored as a separate, transport-independent adapter concern in every matrix row rather than letting a broker's "push" masquerade as waking the agent.
- **P4 (traceability) + P3 (justify upward).** REQ2 is accepted only *conditionally* on the "inotify-is-a-hint, readdir-is-truth" rule — recorded as the concrete acceptance bar for the PoC, not an unqualified claim. Messages-as-files are natively auditable (`ls`/`git`/`cat`), matching the substrate the org already trusts.
- **P7 (cheap completeness) + P5 (triage stays at tier).** Rejected options are recorded so they are not re-proposed; runner-up kept warm without being on the build path.
- **ORG-012 (HO authorship of ADRs/notes).** This ADR is HO's deliverable-3 synthesis of Specialist's survey + the requirements note.

## Implementation (sequenced; build gated on Veneration lease + T-0777)

Already filed by Specialist per the research-dispatch-filing SOP (T-0772–T-0776, target=orchestrator; T-0777 coordination, target=recovery):

| Ticket | Work | Gate |
|---|---|---|
| T-0773 | Evaluate / lift `avivsinai/agent-message-queue` | first — informs whether to adopt or build |
| T-0774 | Spec: trustworthy-wake + atomic-write durability (the REQ2 acceptance bar) | parallel with T-0773 |
| T-0772 | PoC: Maildir mailbox + `systemd --user .path` wake; verify offline-queue survival across a simulated cap-hit | after lease primitive lands |
| T-0775 | MQTT runner-up spike + mosquitto durability audit | conditional — only if the file-mailbox PoC fails its bar |
| T-0776 | Record rejected options (short note) | anytime |
| T-0777 | Coordinate with Veneration substrate (subsume-vs-sit-beside → **sit-beside**, shared lease) | **blocks build start** |

On ratification: the PoC (T-0772) is the acceptance-gated proof; promotion from PoC to standing substrate is a follow-on decision once the REQ2 bar is demonstrably met across a simulated cap-hit.
