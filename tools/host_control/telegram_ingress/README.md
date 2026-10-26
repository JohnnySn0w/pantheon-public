# Telegram human-approval ingress (ORG-052 Unit 7, T-2919)

This is the only path by which a human approval enters the host approval service. It runs as a separately supervised process with its own Unix identity, its own Telegram bot (credential obtained through an ORG-051 lease) and its own update cursor. It shares nothing with any agent-facing Telegram integration or persona.

## Flow

1. **Recording the request.** An approver asks for human review (`RequestHuman`). The approval service records one pending request in its journal: the exact plan digest, target seat, operation families, redacted effects, an expiry and a single-use random nonce.
2. **Presenting the card.** The approval service calls `org.pantheon.HostApprovalTelegram1.Ingress.Present`. Only the approval service's uid may call it.
3. **Sending the card.** The ingress sends exactly one plain-text card. Its Approve and Deny buttons carry a SHA-256 tag over (review handle, nonce, decision). The nonce itself never reaches Telegram.
4. **Accepting a tap.** The ingress long-polls `getUpdates` and has no listener. It accepts a callback only if every check in `core.CALLBACK_CHECKS` passes; the first failure refuses it. The checks are:
   - the sender is not a bot (the bot's own id comes from `getMe`);
   - the user and chat are on the allowlist, and the chat is private (chat id equals user id);
   - the callback is not a replay;
   - the message is the stored, unedited card (compared against the text Telegram returned);
   - the tag matches;
   - the card has not expired.

   Every callback is answered. If the service has not yet recorded the card as presented, the decision stays pending and is retried rather than discarded. The card itself is already closed locally (`decision_pending`, so no second tap is accepted); only the Telegram message is left untouched until the decision lands.
5. **Recording the decision.** The ingress calls `HumanDecision`, which only the ingress uid may call. The service maps that uid to the human subject from root-owned configuration, never from request fields. In one durable transaction it consumes the nonce, records the provider attestation, decides the review and, if approved, issues the grant.

## Calls between the two services (T-5518)

Neither service blocks its single-threaded loop waiting on the other.

- **Present** (approval service → ingress) is sent asynchronously. `RequestHuman` replies once the first attempt settles: `presented` if the card was delivered, otherwise `pending`.
- **Present retry.** If a Present fails or times out, the approval service retries it itself, starting at 2s and doubling to a 60s cap. It stops when the request is presented, expired, withdrawn or decided, or on a permanent Invalid/Conflict refusal. Pending requests are re-armed after a restart, so recovery never depends on an agent replaying `RequestHuman`. Every attempt resends the same nonce and digest, so the ingress deduplicates them.
- **HumanDecision** (ingress → approval service) is also asynchronous, so `Present` stays servable while a decision is in flight. Retryable refusals back off from 1s, doubling to a 30s cap, and are logged only on attempts 1, 2, 4, 8 and so on. After the card expires, the ingress never closes a decision itself. It makes one final idempotent call, and the service returns any decision it already committed. If the service is unreachable, it retries at most 5 times within 120s and then marks the card "decision status unknown", never "expired".
- **What can still block the ingress loop.** A `getUpdates` poll (up to 5s) and individual Bot API calls (up to 10s each) still run on the loop thread, which keeps all state single-threaded. If that latency makes a Present time out, the approval-side retry and the idempotent replay recover it.
- **Withdrawn or expired while a Present is in flight.** The card can still reach Telegram. A tap on it is refused (`human_request_closed` or expired), because the ingress interface has no close-card call.
- **Callback answers.** Every callback is answered at tap time ("Received."). The decision's outcome is shown by editing the card.

## Modules

| Module | Role |
|---|---|
| `config` | Strict parser for the root-owned config |
| `bot_api` | The Bot API methods it uses (getMe, getUpdates, sendMessage, editMessageText, answerCallbackQuery). The token appears only in the URL path, and errors are redacted. |
| `card` | Rendering, bounds, and the callback tag |
| `state` | Its own SQLite, mode 0600: cursor, cards, seen callbacks, decision intents |
| `core` | present, process_update, recover |
| `dbus_service` | The `Present` object and the `HumanDecision` client |
| `main` | Entry point; takes `--config <path>` only |

## Isolation

The package imports only the standard library, `tools.credential_broker` and its own modules. A `sys.modules` closure test enforces this. The bus names are copied constants, and a test pins them equal to the approval wire.

The API base is a compiled constant. No configuration can redirect the token.

## What is and is not proven (ORG-052 gate 8)

**Proven** at fixture and private-bus tier, against a stdlib fake Bot API on 127.0.0.1:

- the spoof matrix, red before the real path goes green;
- nonce single-use, and recovery from crashes;
- the uid gates;
- leak canaries;
- isolation of mailbox content.

**Unsupported until the HO's live proof (T-2924, gate 38):**

- real Telegram delivery and callback semantics;
- keyboard removal on real Telegram;
- the real bot, token, account, system-bus policy and unit hardening;
- a human tapping a real card.

Staged artifacts are in `tools/host_control/dist/`. Nothing here is installed or enabled.
