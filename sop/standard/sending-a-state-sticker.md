# Sending a state sticker to the operator on a real state change

**Status:** Adopted 2026-10-07 by Recovery under T-1037, at the operator's request: the sticker arc (TG 4120-4122, via specialist), and the steer to ship the "when" as a skill (TG 11256, via orchestrator). Design source: `notes/2026-06-29-sticker-state-method-design.md`.

## When to use this

You are talking to the operator on Telegram, and your own state just CHANGED in a way he would want to see at a glance:

| Reaction | Fire when | Examples |
|---|---|---|
| `shipped` | a deliverable landed | a PR merged, a ticket resolved with work done, a deploy verified |
| `approved` | a gate passed or the operator approved something you then acted on | review APPROVE, the operator's "go" executed |
| `investigating` | you START a diagnosis or a recovery | an alert you are now triaging, a root-cause hunt |
| `thinking` | you START a substantial task, sparingly | a multi-step arc the operator just asked for, not every turn |
| `idle` | you park with nothing actionable | queue empty, hold in force, waiting on the operator |
| `wedged` | you are blocked and know it | a cap you can see, an access you lack, a dependency that is down |

A sticker marks a TRANSITION. It goes alongside your text reply, never instead of it. The text says what happened; the sticker is the at-a-glance status.

## Procedure

1. **Is this a transition?** Same state as your last sticker → do not send. Most turns send nothing.
2. **Send your own persona's sticker** in the chat you are replying in (the inbound `chat_id`, normally the operator's DM):
   `mcp__sticker__send_sticker(chat_id=<inbound chat_id>, reaction=<one of the six>)`.
   Your persona comes from your account, and you can only send your own avatar. An unknown reaction errors with your available list.
3. **Order:** send the substantive text reply first, then the sticker. A sticker with no explanation is noise.
4. **Verify:** the tool returns the sent message id. On an error (for example "wrong file identifier" because your bot needs re-minted file ids), keep going without stickers and note it once to orchestrator. Never retry in a loop.

## Common failure modes

| Failure | Fix |
|---|---|
| A sticker every turn: the language turns into noise | Send on transitions only. Two of the same state in a row means skip. |
| A sticker instead of an answer | Text first; the sticker only marks the status. |
| A `wedged` sticker for a peer | You can only send your own avatar. A dead or wedged peer gets a text alert through the normal route (`route_failure_alert`). |
| Sending in a chat the operator is not in, or to a group | Use the inbound `chat_id` only. |
| The tool prompts for permission | `mcp__sticker__send_sticker` belongs in `permissions.allow` (bring-up SOP §7). Add it on that seat. |

## Boundary

- Covers WHEN and HOW a Claude seat fires its own state sticker. It does not cover building or re-minting the sticker set (T-1002/T-1024), or the server itself (`tools/sticker_mcp.py`).
- Codex (Consultant) has no skills or `send_sticker` MCP tool. Its rule lives in its AGENTS.md and uses `tools/send_sticker.py` with the minted `codex` persona set and the same six reactions. The Consultant set (waypoint / verified / received / boundary / waiting) has no file ids yet, so it cannot send.
- A reflexive Stop-hook firing layer (the design note's first choice) is not built. This SOP is the remembered layer, surfaced as a skill at the decision moment.

## Cross-references

- `notes/2026-06-29-sticker-state-method-design.md`: the state taxonomy, the own-token principle and the wedged carve-out.
- `tools/sticker_mcp.py` (T-1024, #734), `assets/stickers/manifest.json`, `tools/send_sticker.py`.
- T-1002 (the set + rollout), T-1037 (this SOP), the concise-with-operator pin (D1).
