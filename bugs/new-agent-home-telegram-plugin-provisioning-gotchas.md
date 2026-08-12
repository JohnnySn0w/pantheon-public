---
symptom: "A new agent home boots with the channels banner rendered but no bun server.ts child and no telegram MCP log dir - the bot is silently dark while the service looks green"
fix: "Set enabledPlugins in ~/.claude/settings.json and rewrite the copied plugins registry's absolute installPath and installLocation to the new home"
domain: claude-code-harness, telegram, plugins, provisioning, silent-failure
---
# Provisioning a new agent home for `claude --channels` telegram: the two silent blockers

Standing up the exocortex second-head daemon (2026-07-20, TG 6206 arc) by mirroring Recovery's shape hit two provisioning gaps that produce an identical, misleading symptom: **claude boots, the channels banner renders ("Channels (experimental) messages from plugin:telegram…"), but NO `bun server.ts` child ever spawns and no telegram MCP log dir appears** — the bot is silently dark while the service looks green.

1. **`enabledPlugins` lives in `~/.claude/settings.json`, not in the plugins dir.** Copying `~/.claude/plugins/` from a working agent is not enough; without `{"enabledPlugins": {"telegram@claude-plugins-official": true}}` in settings.json the plugin MCP is never spawned even with `--channels` on the CLI.
2. **The copied plugins registry carries absolute paths into the source home.** `~/.claude/plugins/installed_plugins.json` (`installPath`) and `known_marketplaces.json` (`installLocation`) point at `/home/<source-user>/.claude/plugins/...`, unreadable cross-user → plugin resolution fails with no visible error. `sed -i 's|/home/<src>/.claude/plugins|/home/<dst>/.claude/plugins|g'` both files.

Diagnostic ladder (extends `reference_telegram_channel_diagnosis` memory pin): if bun+`--channels`+trust are all OK but there's no bun child AND no `mcp-logs-*telegram*` dir under `~/.cache/claude-cli-nodejs/<cwd-slug>/`, the plugin was never attempted → check these two before anything else.

Full working provision set for a new daemon user (all verified): claude CLI via npm-global (pin fleet version), bun, copied `~/.claude/plugins` WITH the path rewrite, `settings.json` enabledPlugins, `.claude.json` with `hasCompletedOnboarding` + `hasTrustDialogAccepted` for cwd and plugin cache dir, `.credentials.json` (0600), `channels/telegram/.env` (token) + `access.json` (allowFrom), supervise script + systemd --user unit (Recovery's `recovery-bot-supervise.sh` pattern; `--permission-mode default` per T-1541/T-1544).
