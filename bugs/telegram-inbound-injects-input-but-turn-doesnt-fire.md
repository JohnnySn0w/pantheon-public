---
symptom: "a Telegram inbound reaches the agent's TUI input box (visible as `❯ <message>`) but the turn never fires — the agent sits idle/unresponsive; even a manual Enter/C-m into the pane does NOT submit the injected text. Only Ctrl-C-clear + re-type as fresh keystrokes + Enter fires it"
fix: "SHORT-TERM unstick: Ctrl-C the pane, re-type the message as real send-keys keystrokes, Enter (a plain Enter on the injected text does NOT submit — the re-type is load-bearing). ROOT fix is the telegram→session wake/submit path (harness-internal); test the manual-mode correlation first"
domain: "telegram-plugin / claude-code-harness / wake-path"
---

## Symptom

An agent stops replying to Telegram (operator reports it "unresponsive"), but it is **NOT crashed or
capped**:
- The agent's `claude` process **and** the telegram plugin (`bun server.ts`) are both alive.
- The pane shows the inbound message **sitting in the input box** (`❯ <operator's message>`) — so it
  WAS delivered — but **unsubmitted**; the agent is idle (footer `? for shortcuts`, not
  `esc to interrupt`).
- The turn never fired: no processing, no reply.

Observed 2026-07-26 on **both Recovery and Archivist simultaneously** (operator-escalated "neither is
responsive"). Both had a distinct operator message stuck this exact way.

## Cause — an INTERMITTENT wake/delivery drop (harness-internal), NOT manual mode

The telegram plugin injects the inbound into the TUI input, but the submit/wake that should fire the
turn intermittently drops. The injected text is displayed yet not registered as a fireable turn — a
manual `tmux send-keys Enter` / `C-m` into the pane does **not** submit it either; only Ctrl-C-clear +
re-type as fresh keystrokes fires it.

**Manual mode is RULED OUT as the deterministic cause** (isolated 2026-07-26, T-2040, without a
disruptive live test — the intermittency does the isolation):
- Manual mode is a **runtime state, not a launch flag** — all four telegram agents launch
  `--permission-mode default`, no manual-mode config; nothing sets it deterministically.
- The **same agents in the same `⏸ manual mode` processed SOME telegram messages** this session
  (Archivist replied "what's up?"; Recovery did queue work) **and stuck on OTHERS** (the de-cluster go,
  the MCP-land), both while idle. If manual mode deterministically held injected inputs, *nothing*
  would fire — some did. So the drop is **intermittent**, not a manual-mode block.

Mechanism is therefore **harness/plugin-internal best-effort delivery** — same character as the
SendMessage sibling (`team-pattern-sendmessage-intermittent-wake-failure`, agent↔agent), here on the
Telegram-inbound (operator→agent) surface. Not source-fixable from Pantheon (closed harness), so the
fix is **detection + auto-unstick**, not a config change.

## Fix

**Short-term unstick (verified working 2026-07-26 on both agents):**
1. `tmux send-keys -t <pane> C-c` — clear the stuck injected input.
2. `tmux send-keys -t <pane> -l "<the message text>"` — re-type it as real keystrokes.
3. `tmux send-keys -t <pane> Enter` — the turn fires.

A plain `Enter`/`C-m` on the *injected* text does **not** submit; the **re-type is load-bearing**.
(Run as the agent's user via sudo; confirm the footer flips to `esc to interrupt` = processing.)

**Root fix:** manual mode ruled out (see Cause) → this is an intermittent harness-internal wake-drop,
not source-fixable from Pantheon. The fix is **detection + auto-unstick** — a monitor that scrapes
panes for "non-empty input box + idle footer" and applies the Ctrl-C + retype recovery. Routed to
**T-2039** (the Codex out-of-band watcher): the pane-scrape + auto-unstick is a natural out-of-band
Codex capability (and the case that most needs it is exactly when the Claude fleet is capped and can't
unstick itself). Diagnostic ticket **T-2040** resolved into T-2039.

## Detection

The agent looks alive to `pgrep`/pollers (process up) but is unresponsive to the operator. The tell
is the **pane**: an inbound sitting in `❯ <text>` unsubmitted with the agent idle. A monitor scraping
for "non-empty input box + idle footer" catches it — the pane is the defining surface (same
pane-visible / proxy-silent shape as the idle spend-cap wedge and the ACP-bridge-context-full
failures).

## Related

- `team-pattern-sendmessage-intermittent-wake-failure.md` — sibling wake-drop, agent↔agent.
- The 2026-07-26 incident (ledger `2026-W30`) — operator-escalated; both agents stuck this way.
- **Ties directly to the Codex out-of-band watcher (T-2039):** a watcher that scrapes panes for stuck
  inputs and auto-unsticks (Ctrl-C + retype) is exactly the kind of out-of-band repair Codex could do
  when the Claude fleet is capped and can't unstick itself.
