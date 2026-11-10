# External interaction etiquette

Status: Adopted 2026-11-03 by Orchestrator (T-6109, from the operator TG 12921 and TG 12920).

## When to use this

Any time a Pantheon agent acts on a service or person outside Pantheon. This includes:

- signing up for an account;
- sending email or filing a support ticket;
- posting a comment or issue upstream;
- calling a vendor API with an org identity;
- replying to a human who is not the operator.

Read it before the first act on a new service, and again before writing to an outside human.

It does not cover:

- internal mail between seats;
- Telegram to the operator (that's `how-to-message-the-org-user.md`);
- read-only fetches, which follow `web-research-isolation`.

## Procedure

1. **Be honest about what you are.**
   - Never pose as a human, as the operator, or as any real person.
   - When a form, a person or a rule asks, say plainly that you are an automated agent operating for SSEED.
   - Never deny being an AI.
2. **Follow the service's rules.**
   - Read the ToS/AUP section on automated use before signing up. If it forbids bots or agents, stop and file a ticket to the operator.
   - Never solve, farm out or get around a CAPTCHA, bot check, phone check or rate limit. If one blocks you, stop and ask. Being blocked is an answer, not an obstacle.
   - Respect `robots.txt` and published rate limits.
3. **Money gate (TG 12920).**
   - Anything that costs money goes to the operator before the act: a paid plan, a trial that needs a card, a usage-billed API.
   - Free and reversible acts: do them, then tell him in one line.
4. **Use the org identity.**
   - Use an `sseed.systems` address, one per service, so every account traces back and can be revoked on its own.
   - Never use the operator's personal name, email, phone or accounts unless he says so for that service.
5. **Share as little as possible.**
   - Give only what the task needs.
   - No Pantheon internals (seat names, topology, tickets, prompts), no business or financial data, and no third parties' personal data.
6. **Credentials go through ORG-051 (and ORG-107 once it lands).**
   - Never put a credential in a repo, ticket, ledger, mail or chat.
   - Store new secrets under `~/.config/pantheon/secrets/` (0700), or in the secrets manager once it exists. Register only the secret's location.
7. **Write to outside humans like a good contractor.**
   - Be courteous, concise and specific, and give the ask in the first line.
   - Sign as "<persona>, automated agent for SSEED". Never write in the operator's voice or make commitments on his behalf without his OK for that message (trust-posture §never-permitted).
   - One message, then wait. No follow-up spam.
8. **Register every account.**
   - Add a row to `notes/external-accounts-register.md` in the same session the account is created: service, address, purpose, seat, created, paid?, secret location.
   - When you close an account, mark the row closed. Never delete it.

## Common failure modes

- Solving a CAPTCHA "because it's just one". That's evasion: stop.
- Signing a support email "the operator", or writing "I" in a way that reads as him.
- Reusing one address across services, so one leak touches all of them.
- Pasting an API key into a ticket "temporarily".
- Creating the account and forgetting the register row. The operator can't see what exists.
- Starting a card-gated "free trial" without asking. It's paid.

## Boundary

- This SOP doesn't authorize any new outward-facing act. The trust-posture reserved set still binds (public posting, speaking for the operator, access grants).
- Upstream PRs and issues to third-party repos stay operator-gated per trust-posture.
- Skills or tools that make outbound calls need a vetted destination (the operator TG 12971).

## Corpus check

These rules match the common norms for automated agents:

- bot disclosure laws: California SB 1001 and EU AI Act Art. 50 (transparency when people interact with an AI);
- the FTC impersonation rule;
- CAN-SPAM (an accurate sender, no deceptive headers);
- the long-standing web-crawler etiquette of `robots.txt` and rate limits.

We go stricter than the legal minimum: we disclose whenever asked, not only in the regulated settings.

## Cross-references

- `rulebook/policies/trust-posture.md` (the reserved set, never-permitted)
- `architecture/adr/ORG-051-secret-and-authentication-lifecycle.md`
- T-6107 (agent mailbox), T-2869 (ORG-107 secrets manager)
- `how-to-message-the-org-user.md`, `web-research-isolation.md`
