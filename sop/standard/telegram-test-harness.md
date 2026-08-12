# SOP — Telegram send-accept test harness

**Status:** Authored 2026-08-08 by Orchestrator (HO) per the operator (TG7320). Content derived from verified production state (2026-08-08 live round-trip: `@pantheon_sendtest_bot` → test group `-5356823915`, `message_id 6`, the operator visually confirmed). Parent T-2556 / T-2582. The operator override-available.

## When to use this

When a daemon-bringup acceptance run (or any Pantheon test) needs to prove a **genuine** Telegram outbound send-accept — a real `sendMessage` → `message_id` round-trip against the live Bot API — rather than the mocked-contract boundary that `tools/bringup_acceptance/telegram_outbound.py` exercises in the always-run suite. This upgrades T-2568's mocked row to a live-accepted proof.

**Do NOT use this to send to the operator.** The whole harness exists to prove a live send WITHOUT ever touching the operator chat `456474197`. The operator-refusal guard is structural (HardStop), but the discipline is yours too.

## The isolation model (why it's built this way)

Three isolation boundaries, each load-bearing:

1. **Separate bot, separate token.** A dedicated throwaway bot (`@pantheon_sendtest_bot`), NOT the live operating bot. Its token lives at `/home/pantheon-test/.config/telegram-test-bot/token` (mode 600). This is deliberately NOT `~/.claude/channels/telegram/.env` — writing a test token there would repoint the live operating channel and go comms-dark.
2. **Separate unix user.** The token is owned by unix user `pantheon-test` (uid 1009), mode 600 — the `claude` user cannot read it without sudo. The live leg runs AS `pantheon-test` via attended sudo; there is NO NOPASSWD sudoers entry (the leg is gated + attended, so it doesn't need one).
3. **Separate recipient chat.** A NON-operator test group, `chat_id -5356823915` (negative = group). Distinct from the operator chat `456474197`. A private DM to any bot carries the USER's id (456474197 for the operator) — so a test recipient MUST be a group/channel with a distinct negative/large id, never a DM.

## One-time setup (operator-provisioned; recorded so it's reproducible)

The bot + group provisioning is on the operator's Telegram account (surface, don't self-provision). Steps that were run:

1. **Create the throwaway bot** via BotFather (`/newbot`), name it (e.g. "Pantheon Send-Accept Test", `@pantheon_sendtest_bot`). BotFather returns a token.
2. **Create a test group**, add the bot to it, post one message.
3. **Capture the group chat_id.** Bots in privacy mode see only `/commands`, @mentions, and service events in groups — so post a `/cmd@pantheon_sendtest_bot` in the group, then read `getUpdates` for the bot and take the negative `chat.id` (`-5356823915`).
4. **Store the token isolated.** As root: create unix user `pantheon-test`, write the token to `/home/pantheon-test/.config/telegram-test-bot/token` mode 600 owned by `pantheon-test`. Never commit it; never write it to the operating `.env`.

## Usage — running the gated live leg

The leg is `tools/t2582_live_telegram_accept.py`.

- It is **gated** behind env `T2582_LIVE_TELEGRAM_ACCEPT` (∈ {`1`,`true`,`yes`}). Absent the gate its `main()` prints `SKIPPED: ...` and exits 0 — and the always-run `python -m pytest tools/ -q` NEVER sets that var, so the suite NEVER makes a real send (SEND-ISOLATION invariant). The unit tests exercise the pure logic through an injected fake `SendFn`; the live `make_live_send_fn` wiring is only ever constructed inside `main()` behind the gate.
- When fired with the gate set, it: reads the isolated token, sends one probe message to the dedicated test chat, asserts `ok` + a real `message_id` + the echoed `chat_id` matches (reusing `telegram_outbound.assert_chat_id_matches`), and **HardStops before sending** if the target chat_id is ever `456474197`.
- Defaults: `--chat-id -5356823915`, `--token-path /home/pantheon-test/.config/telegram-test-bot/token`.

**Invocation (attended, run AS `pantheon-test` so it can read the 600 token):**

```
tr -d '\n' < /home/claude/root.env | sudo -S -k -u pantheon-test \
  env T2582_LIVE_TELEGRAM_ACCEPT=1 \
  python3 /home/claude/pantheon-config/tools/t2582_live_telegram_accept.py
```

Expected on success: `ACCEPTED: message_id=<n> chat_id=-5356823915`. A real message lands in the test group — confirm it's the test group, never the operator chat.

To confirm the gate isolation without sending, run it WITHOUT the env var — it prints `SKIPPED: missing explicit live gate T2582_LIVE_TELEGRAM_ACCEPT ...` and exits 0.

## Gotchas

- **Never repoint the live channel.** The test token must never land in `~/.claude/channels/telegram/.env` — that is the live operating channel; overwriting it goes comms-dark.
- **Privacy-mode bots** see only `/commands` + @mentions + service events in groups — capture the chat_id via a `/cmd@botname`, not an ordinary message.
- **DM chat_id == the user's id** across ALL bots — so a test recipient must be a group/channel, never a DM.
- **The always-run suite never sends.** If a plain `pytest tools/ -q` ever makes a network call, the SEND-ISOLATION invariant is broken — the gate is the only thing that enables a real send.
- **`pantheon-test` must be able to read the script + `tools/` package.** The leg imports `from tools.bringup_acceptance...`; run it against a repo checkout `pantheon-test` can read (pantheon-shared group perms on `/home/claude/pantheon-config` cover this today). If a future perms change blocks it, either fix the group read or run from a checkout the test user owns.
- **No NOPASSWD sudoers.** The leg is gated + attended, so it does not need (and must not get) a passwordless sudoers entry for `pantheon-test`. Fire it by hand with the `root.env` password when you actually want a live proof.
