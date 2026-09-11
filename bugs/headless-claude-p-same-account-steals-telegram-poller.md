---
symptom: "interactive session's mcp__plugin_telegram_telegram__* tools vanish mid-session while the poller subprocess shows alive; coincides with a headless claude -p run fired under the same unix account (second plugin poller → Telegram getUpdates 409 conflict)"
fix: "every headless claude -p on an agent seat MUST carry --setting-sources \"\" --strict-mcp-config (no plugins → no second poller; verified live 2026-08-12, T-2809). Do NOT use --bare — it disables OAuth and breaks keyless-seat sub-billing"
domain: "telegram-channel / headless-claude"
---

# Headless `claude -p` under the HO account steals the Telegram poller → MCP binding drop

**Symptom:** the interactive session's `mcp__plugin_telegram_telegram__*` tools vanish mid-session ("known flap"), while the poller subprocess still shows alive. Coincides with any headless `claude -p` run fired under the same unix user/account.

**Mechanism:** a bare headless run loads user-scope settings, which include the telegram channel plugin. The plugin spawns its own Bot-API poller; Telegram allows exactly one `getUpdates` consumer per bot token, so the two pollers conflict (409) and the interactive session's binding loses. First confirmed 2026-08-12: GEPA probe fired 13:22Z, binding dropped ~13:22Z (the operator spotted the correlation, TG 7793).

**Fix (verified live 2026-08-12, T-2809):** every headless `claude -p` call on this box MUST carry:

```
claude -p "..." --setting-sources "" --strict-mcp-config
```

`--setting-sources ""` loads no user/project settings → no plugins → no second poller. `--strict-mcp-config` ignores all MCP server configs. Verified: isolated probe ran 5s ok and the interactive MCP binding survived (post-probe reply TG 7795 delivered over the normal channel).

**Trap:** do NOT reach for `--bare` — it skips OAuth/keychain entirely (auth strictly `ANTHROPIC_API_KEY`), which breaks the keyless-seat sub-billing model that headless-under-Max depends on.

**Blast radius:** any bulk headless executor (GEPA Phase 2 rollouts, future eval loops) repeats the collision per call if unflagged. Recovery from a dropped binding historically required a session restart; the Bot-API curl fallback (`reference_telegram_botapi_sendmessage_fallback`) covers outbound in the meantime.
