---
title: Pantheon bringup
status: Drafted 2026-06-03 by Specialist (T-0751); expanded 2026-06-03 by Recovery (T-0760, per-agent daemon section); freshness pass 2026-06-04 by Specialist (T-0778 — removed residual bridge-as-live framing in §1/§4.1/§6, §9 key form); codex-daemon eval pass 2026-06-05 by Specialist (T-0762 — full-team from-scratch codex bringup in a rootless-podman container; added §1 prereq fixes (sudo/unzip/libpam-systemd/explicit NodeSource), §2.3 container-linger caveat, §2.4 single-daemon path, §3 codex install/auth branch, §4 codex supervise + ExecStop fix, and the new §12 codex-daemon variant); claude-code eval pass 2026-06-05 by Specialist (T-0762 cont. — full-team from-scratch claude-code bringup, the handoff-target harness; fixed the §7 plugin-install BLOCKER (marketplace add + install was undocumented), the §3 PATH gap (~/.bashrc→~/.profile) + claude-login reality, the §6 AGENT_CONFIG/DRAIN_AGENT_CONFIG dict shapes + §5-before-§6 ordering, §9 pre-auth capture-pane + drain-arming guards, §5 clone-time safe.directory, §11 settings.json-not-regenerated); clean-room end-to-end pass 2026-06-05 by Specialist (T-0762 cont. — 2 naive doc-only operators ran the full §1→§9 flow; verdict yes-with-caveats; fixed the SEAM faults the section-passes structurally missed: the shipped recovery-config reference-names silent-failure (§6), the tools/systemd/README Type=oneshot divergence note (§6), the §7 settings.json merge hazard + explicit CLAUDE_ARGS edit, §2.4 root.env code block, §4.2 systemctl --user session context, §5 safe.directory user/.git); ticketing-gap pass 2026-06-09 by Recovery (live codex + the-ancestor bringup run, the operator directive — added §10 step 8 ticket-persona registration in AGENT_ROLE_MAP/instance.yaml as a REQUIRED step (ticket.py hard-rejects unregistered targets; renumbered the checklist 8→11) and new §12.7 codex-ticketing disposition (read/file works, auto-delivery does not, push needs a deploy key or recovery-sync stopgap)); clean-container seed-bringup pass 2026-06-24 by Orchestrator (validated the `pantheon-public` extracted seed on `python:3.12-slim` rootless-podman — tooling self-consistent, 2572 host-independent tests green; added the §5 per-account git-identity + `init.defaultBranch=main` block: a fresh box has no git identity and every commit-bearing tool path silently fails until it's set); copy-provisioning + cred-copy-ban + fetch-before-bundle + persona-map + grant-sequencing pass 2026-07-20 by Orchestrator (T-1832, folding the mnemosyne-bringup field records + the T-1874 auth-outage root cause into §5/§7c); MCP-tool allowlist pass 2026-07-20 by Mnemosyne (live mnemosyne-bringup finding, the operator directive TG 41 — the §7 reference `permissions.allow` set predated MCP-tool entries (chat-surface plugin tools + mailbox), so a faithful perms-parity patch still left every Telegram reply permission-prompting the operator's phone; added the §7 MCP-allowlist paragraph + the §9 MCP-coverage check); OnFailure repo-template pass 2026-07-21 by Recovery (T-1928, Metis catch — templates now ship pre-wired with the unit-failure guard; added the §6 install-pattern guard+leaf note and the §9 grep -L coverage check); seat venv-provisioning step pass 2026-07-22 by Orchestrator (T-1952 Metis finding — repo-documented venvs (tools/a2a/PROVISION.md → ~/.venv-a2a) are a per-role bringup step; a missing venv yields a skipped-suite false green; added per-role checklist step 10, renumbered 10→11→12). Adopted [orig 2026-06-03] under principles-as-ratification (below-goal operational SOP; cited CANONICAL in-ledger + container-validated 2026-06-24) [was "Awaiting the operator ratification" — superseded 2026-07-07 to match in-force reality]; §11 harness-config posture governed by ORG-039 (ratified 2026-06-30).
purpose: Reproducible procedure for standing up a new Pantheon instance. Reference 3-agent configuration with explicit "name it whatever works for you" caveats on surface-level details.
---

# Pantheon bringup

This SOP is the procedure for standing up your own Pantheon instance on a Linux VPS. It documents how the operator's reference instance is configured — the structural pieces (asymmetric trust model, supervise-script pattern, principal/recovery/specialist split) are prescriptive; the surface-level details (OS exact version, session names, repo paths) are pickable.

## Who runs this — the human/agent boundary

This SOP is executable two ways: a human operator follows it top-to-bottom, **or you hand the repo + this doc to a capable agent (a Claude Code instance with sudo on the box) and it self-executes**, pausing only where it structurally cannot proceed. (Both forms have been exercised — the doc was validated by Claude instances running it clean-room, doc-only, end-to-end.)

The split is **not a "remember to do these" checklist — it's a structural boundary** (P1: architecture over policy). A small set of inputs is gated behind a human by *external auth/credential systems no agent can drive*; everything else is just executing this repo with privilege. The line sits exactly there:

**What only a human can supply** — gated by external systems; an agent physically cannot do these, however capable:
- **Anthropic auth** — interactive OAuth via the TUI onboarding (no headless key path; `claude login` standalone is gone in CC 2.1.x — see §3): theme / login-method / OAuth / folder-trust dialogs cleared by hand. Your account, your browser.
- **Bot tokens** — one per agent from BotFather (§7).
- **Deploy-key upload** — you generate the per-account keys, but *uploading the public key to your repo host* is the human-gated half (§5).
- **The sudo password** — generate it, place `root.env` 0600 (§2.4).

**What the agent does** — everything on the repo-execution side of that boundary: apt + node (§1), the Unix accounts + asymmetric sudoers + linger (§2), per-account install (§3), supervise scripts + systemd units (§4), repo clone (§5), timers + recovery wiring (§6), plugin install (§7), verification (§9). Give it sudo/root on the VPS and it runs §1→§9 from this doc, pausing at the four human inputs above as it reaches them.

**The bootstrap chicken-and-egg:** this doc stands up *agent daemons*, so you need one agent already running to read it — it can't conjure your first Claude from nothing. Minimal bootstrap: any one Claude Code instance (your laptop's, with SSH to the VPS) + this repo + sudo on the box → it executes the rest.

## When to use this

- Onboarding a new operator who wants to run their own Pantheon.
- Re-provisioning the reference instance on a fresh VPS.
- Documenting the install procedure for posterity (the "you wrote it down, so it's reviewable" property).

## When NOT to use this

- Adding a single capability (e.g. restart-stack) to an existing agent — see [`adding-restart-capability-to-a-new-agent.md`](adding-restart-capability-to-a-new-agent.md).
- Recovering a wedged agent — see [`handoff-restart-runbook.md`](handoff-restart-runbook.md) + [`../remediation/recovery-service-recovery.md`](../remediation/recovery-service-recovery.md).
- Day-to-day operations — see the SOP index.

## What you're building (mental model)

Three persistent agents — one orchestrator + one specialist + one recovery — each running as a long-lived tmux session wrapped in a systemd user service, on its own Unix account. A small set of cron-driven monitors does the recovery, the inbox-dispatch, and the day-to-day grooming. The agents coordinate via the ticket queue (`tickets/`) and the append-only ledger (`ledger/`).

The reference instance uses **Orchestrator** (Orchestrator), **Specialist** (specialist), **Recovery** (recovery). See [`../../knowledge/pantheon-glossary.md`](../../knowledge/pantheon-glossary.md) for the persona-to-account mapping and the rest of Pantheon's vocabulary. You can name your own agents whatever you want — most names below are arbitrary labels.

**The structural pieces that ARE load-bearing** (don't redesign these on first build; come back later if you have a strong reason):
- **Asymmetric trust model.** The recovery agent can sudo into the other two; the reverse is denied. A confused orchestrator can't reach the agent supposed to rescue it. This rides on Unix permissions.
- **Supervise-script + tmux + systemd.** Three layers of supervision. The supervise script holds systemd's MainPID alive for the lifetime of the tmux session; systemd restarts the unit if the supervise exits; the inner Claude Code gets restarted by the recovery layer when it wedges.
- **One Unix user per agent.** Identity, file ownership, sudoers, and tmux server are all per-user. Don't try to share an account between two roles.
- **One shared repo** (this one, `pantheon-config`), cloned per account. Tools and policies live in the repo; state and secrets live per-account on disk.

The rest is plumbing.

## Rebranding the seed (optional)

If you want your fork to carry your own name instead of `pantheon-*` (in unit
names, skill/service names, state paths, and code), use the supported tool
`tools/seed_rebrand.py` — do NOT hand-roll a rename script (the first self-hoster
did, and still shipped a "leftovers" cleanup pass). Run it from the fork's repo
root, on a clean git tree (so `git diff` is your review + undo):

```bash
python3 tools/seed_rebrand.py --brand wish            # dry run — prints the plan, changes nothing
python3 tools/seed_rebrand.py --brand wish --apply    # rename pantheon-* -> wish-* (git mv keeps history)
```

**The keep-list (protected from rename — authoritative).** These are NOT renamed
by default, because they are separate upstream repos / provenance references that
must keep pointing at upstream:

| Token | Why it's kept |
|---|---|
| `pantheon-pwa` | separate upstream repo; a fork references it, doesn't own it |
| `pantheon-public` | the upstream scrubbed **seed** repo you forked from — provenance |
| `pantheon-bridge` | the **retired** upstream telegram-bridge; referenced only in historical comments/docs/tests |
| `pantheon-config` | **nuanced** — upstream this is THE hub repo. Kept by default (as an upstream-provenance ref). If your fork MERGES the seed and the hub into ONE repo (a legitimate ORG-029 choice), rename it to your hub name with `--rename-hub <name>`. |

Boundary rule: a keep-token protects the exact identifier, not superstrings — so
`pantheon-config` is kept but `pantheon-config-sync` (a **service**) still renames.
Add your own tokens with `--keep tok1,tok2,...`. Everything else `pantheon-<x>` /
`pantheon_<x>` becomes `<brand>-<x>` / `<brand>_<x>`.

**What the tool deliberately leaves alone** (hardened in T-2074 after the
wish-factory fork's field review — these are the catches a naive find-and-replace
misses):

- **The design-history corpus** — `knowledge/ lessons/ adr/ bugs/` are SKIPPED by
  default. That prose discusses the framework by name as *design history*;
  rewriting it turns the origin's reasoning into your fork's, and rewriting the
  `[[ORG-NNN]]` ADR references breaks the vault link graph. If you truly want your
  fork's brand in that prose, pass `--include-corpus` — but the recommended path is
  to keep it (it's the most valuable thing in the seed) or rewrite files from
  scratch. ADR *filenames* (`ORG-020-...md`) are never renamed regardless — the
  filename rule only fires on names that *start* with `pantheon-`.
- **The tool's own source** (`seed_rebrand.py` + its test) — self-excluded so a run
  is idempotent. This matters because the ORG-029 pull-reconcile workflow re-runs
  the rename after every upstream pull; a tool that rewrote its own rules on first
  run would corrupt itself.
- **The bare `pantheon` state/log dir** (`~/.local/state/pantheon`, `.../log/pantheon`)
  and the **`PANTHEON_*` env namespace** — left untouched on purpose. The rules only
  match `pantheon-` / `pantheon_` (trailing separator), so a bare `pantheon/` dir
  and uppercase `PANTHEON_` are never rewritten. Renaming the state dir is the
  split-brain trap (docs get rewritten, the ~22 piecewise `Path(...)/"pantheon"`
  constants don't); parameterization (T-2071) is the intended fix, not rename.

**Verification.** Every run prints a `── verification ──` block: the protected
tokens that survived (so you can confirm what was kept on purpose), the untouched
`PANTHEON_*` env-ref count, a note on any rule-prefix overlaps (resolved by
most-specific-first ordering), and **warnings** for a split-brain state/log rename
or a dynamically-constructed `PANTHEON_*` env name. After `--apply`, run the test
suite — the env and path renames touch test assertions, so green is a real signal
that the rename is internally consistent.

> Structural alternative under consideration (T-2072): brand-parameterizing all
> identifiers from an `instance.yaml` `brand:` field so the seed never hardcodes
> `pantheon-` and there is no rename step at all. Until that lands, the tool above
> is the supported path.

## Procedure

### 1. Host prereqs

Use Linux. The reference target is Ubuntu 24.04; recent Debian/Ubuntu LTS should work, other modern distros are likely fine but unverified. The reference versions:

- Claude Code (or your chosen LLM harness; codex CLI also works at this point — see ORG-018)
- node ≥ 20 (for npm + the optional dashboards)
- bun (for the Telegram plugin's `server.ts` poller — §7 — and any JS-side project daemons) — install per-account
- python3 ≥ 3.11 (system python; runs every `tools/*.py` — 3.11+ required: `tools/harness_adapter/codex.py` uses stdlib `tomllib`, T-2733). CI (pve2/CT202) tests this floor + the live 3.12 via setup-python against the runner tool cache; a "Version X was not found in the local cache" setup-python failure means the cache lacks that version — seed it with `tools/seed_toolcache_python.sh <minor>` (T-2733).
- git, tmux

Install at the system tier (root) before creating any agent accounts:

```bash
apt-get update
apt-get -y install build-essential curl git python3 python3-venv tmux sudo unzip libpam-systemd
# node 20+ via NodeSource (these two lines are required, not optional — the bare
# apt repos ship an older node; everything downstream needs >= 20):
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get -y install nodejs
```

Why each non-obvious package (every one of these was a from-scratch bringup blocker found in the T-0762 eval):
- `sudo` — §2.4 (`visudo`, sudoers writes) and the §9 asymmetric-sudo checks need it; a minimal base image does NOT ship it.
- `unzip` — the per-account `bun` installer (§3) aborts with `unzip is required to install bun` without it.
- `libpam-systemd` — without it, `loginctl enable-linger` writes the linger file but every `user@<uid>.service` manager fails at boot (`pam_systemd.so` missing / `$XDG_RUNTIME_DIR is not set`), so `systemctl --user` is silently dead. A standard Ubuntu 24.04 VPS ships this in its default install; minimal/container base images do not — see the §2.3 container caveat.

Bun is per-account; install after creating each agent account. **Note:** bun is only needed for the Claude Code Telegram plugin's `server.ts` poller (§7) and any JS-side project daemons — a codex-CLI daemon (§12) does not use bun for its chat surface, so you can skip bun on a pure-codex account.

### 2. Unix substrate

The reference configuration is three agent accounts plus an optional shared supplementary group. Pick whatever account names work for you; we use the persona names. **The asymmetric sudo grant pattern is the load-bearing structural piece** — keep it.

#### 2.1 Agent accounts

As root:

```bash
adduser --disabled-password --gecos "" <orchestrator-account>   # e.g. claude
adduser --disabled-password --gecos "" <specialist-account>     # e.g. claude-research
adduser --disabled-password --gecos "" <recovery-account>       # e.g. claude-fallback
```

The reference instance uses `claude` / `claude-research` / `claude-fallback`. These specific names ripple through some monitor configs and SOPs; if you pick different names, you'll edit `AGENT_CONFIG` entries in `tools/context_fill_monitor.py` and `DRAIN_AGENT_CONFIG` in `tools/restart_drain.py` to match.

#### 2.2 Shared group (optional)

If your orchestrator and recovery accounts will both write to a shared resource (Pantheon uses this for the shared `pantheon-config/.git/objects` because both agents commit), create a shared supplementary group:

```bash
groupadd <shared-group>     # e.g. pantheon-shared
usermod -aG <shared-group> <orchestrator-account>
usermod -aG <shared-group> <recovery-account>
```

**Caveat:** user-mode systemd cannot grant supplementary groups. Anything launched by a user-systemd unit that needs to inherit this group has to be wrapped in `sg <shared-group> -c '...'` — see the supervise-script pattern below.

The specialist account (Specialist in the reference) is deliberately NOT in the shared group — it commits through its own ownership path. Smaller blast radius.

#### 2.3 Linger

Linger lets a user's systemd-user-manager run without an active login session — which is what makes the timers fire when nobody is SSH'd in:

```bash
loginctl enable-linger <orchestrator-account>
loginctl enable-linger <specialist-account>
loginctl enable-linger <recovery-account>
```

**Verify linger actually took, not just that the file exists.** `ls /var/lib/systemd/linger/<account>` only proves the call ran; it does NOT prove the user manager is alive. Confirm with `systemctl is-active user@$(id -u <account>).service` → `active` for each account (allow ~10s after `enable-linger` — the manager starts asynchronously and may briefly read `activating`).

**Container / nested-cgroup substrate caveat (T-0762 eval finding):** on a standard Ubuntu/Debian VPS this Just Works. Inside a rootless-container or nested-cgroup substrate (e.g. Podman `--systemd=always`, used for the codex-daemon bringup test), `loginctl enable-linger` succeeds but `user@<uid>.service` can fail at boot with `$XDG_RUNTIME_DIR is not set`. Two prerequisites fix it: (1) `libpam-systemd` installed (§1 — pulls `dbus-user-session`); (2) if it still fails, `systemctl set-environment XDG_RUNTIME_DIR=/run/user/<uid>` then `systemctl restart user@<uid>.service` before proceeding. Verify the manager is `active` before wiring any `--user` unit on top of it — a dead user manager makes the whole daemon layer silently inoperative.

#### 2.4 Asymmetric sudoers

This is the load-bearing piece. The recovery account gets passwordless sudo into the orchestrator and specialist accounts; the reverse directions are denied. Drop these into `/etc/sudoers.d/`. **Always validate with `visudo -c -f <path>`** — a syntax error in `/etc/sudoers.d/` can lock you out of sudo.

```bash
# Recovery → orchestrator
cat > /etc/sudoers.d/fallback-rescues-prime <<'EOF'
<recovery-account> ALL=(<orchestrator-account>) NOPASSWD: ALL
EOF
visudo -c -f /etc/sudoers.d/fallback-rescues-prime

# Recovery → specialist
cat > /etc/sudoers.d/50-fallback-specialist <<'EOF'
<recovery-account> ALL=(<specialist-account>) NOPASSWD: ALL
EOF
visudo -c -f /etc/sudoers.d/50-fallback-specialist
```

(Reference names: substitute `claude-fallback`, `claude`, `claude-research`.)

If you also adopt the kino PWA (per ORG-006), grant the recovery account a command-allowlisted root sudo for `systemctl` against the kino service — see `notes/2026-06-03-bringup-substrate-ground-truth.md` §3 for the reference shape.

The orchestrator account additionally needs passworded sudo (membership in the `sudo` group) for substrate captures and root-tier maintenance. Add it explicitly (this is an actual step, easy to miss because it's in prose) and place the password at `/home/<orchestrator>/root.env` (0600, never committed):

```bash
usermod -aG sudo <orchestrator-account>
# Place the sudo password (operator-generated; 0600; NEVER committed):
printf '%s=%s\n' "<orchestrator-account>" "<sudo-password>" > /home/<orchestrator-account>/root.env
chmod 0600 /home/<orchestrator-account>/root.env
chown <orchestrator-account>:<orchestrator-account> /home/<orchestrator-account>/root.env
```

**Single-daemon topology (e.g. the ORG-018 codex pilot):** if you are standing up ONE standalone agent with no recovery account (see §12), the asymmetric-sudoers half of §2.4 has no second account to point at — **skip it entirely.** There is no cross-account trust to establish for a lone daemon; add the recovery account and its NOPASSWD grants later if the fleet grows (§10). The lone daemon is a specialist with no recovery layer until you add one.

### 3. Per-account install (run as each agent)

For each agent account (`sudo -u <account> -i`):

```bash
# Bun (per-account; pick whatever runtime you want for project JS daemons):
curl -fsSL https://bun.sh/install | bash

# Claude Code (npm-global per-account):
mkdir -p ~/.npm-global
npm config set prefix ~/.npm-global
# Persist PATH in ~/.profile, NOT ~/.bashrc — Debian/Ubuntu ~/.bashrc starts with
# `case $- in *i*) ;; *) return;; esac`, which returns early for NON-interactive
# shells (the kind `su -l`, `sudo -i`, and systemd spawn), so a PATH line at the
# bottom of ~/.bashrc is never reached and a fresh shell gets `claude: command not
# found`. ~/.profile is read by login shells regardless of interactivity:
echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> ~/.profile
export PATH=~/.npm-global/bin:$PATH    # also export for the current shell
npm install -g @anthropic-ai/claude-code

# Authenticate Claude Code (interactive — via the TUI onboarding; see the note below):
#   NOT `claude login` — that standalone subcommand is gone in CC 2.1.x.
#   Launch `claude` and complete the onboarding, or use the /login slash command.
claude auth status   # scriptable pre-check: {"loggedIn": true, ...} when authed
```

Each account needs its own Claude Code auth. Don't try to share an `~/.claude/`.

**Auth is the interactive TUI onboarding — there is no headless API-key path** (unlike codex's `--with-api-key`). ⚠️ **CC 2.1.x change (observed 2.1.197 spike, confirmed 2.1.204):** `claude login` is **no longer a standalone subcommand** — running it just prints `Not logged in - Please run /login` and exits 0. The real auth flow is the **first interactive `claude` launch → TUI onboarding sequence:** theme picker → **login-method picker ("Claude account with subscription")** → OAuth URL (prints, blocks for the browser authorize) → paste code → security notes → folder-trust ("Is this a project you trust?"). The in-TUI `/login` slash command re-triggers it. The operator needs an active Anthropic/Claude.ai account and must complete this by hand on first bringup. **`claude auth status` (still valid in 2.1.x — returns JSON `{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", …}`) is the scriptable pre-check.** The onboarding dialogs above are blocking and `hasSeenOnboarding` does NOT suppress them; **clear them by hand in the tmux session before enabling the systemd service**, or the first supervised fire wedges on a dialog (see Common failure modes + §7). Full observed dialog sequence: `knowledge/episodic/2026-07-07-claude-code-acp-spike/notes.md` (Rung 4).

> **The supervise script uses an absolute `CLAUDE_BIN="$HOME/.npm-global/bin/claude"` (§4.1), so the *daemon* finds claude even if PATH isn't persisted — but the §3/§9 verification (`claude --version` in a fresh shell) and any interactive operator use will fail without the `~/.profile` line above.**

**Codex-CLI harness (ORG-018 pilot) — substitute these for the claude-code lines above.** Same per-account pattern (npm-global prefix + PATH); different package, binary, and auth:

```bash
# Codex CLI instead of @anthropic-ai/claude-code:
npm install -g @openai/codex
# Auth — pick the flow that matches your bringup:
#   headless/unattended (the daemon case): pipe an API key, non-interactive
printenv OPENAI_API_KEY | codex login --with-api-key
#   interactive onboarding: codex login --device-auth   (prints a URL + one-time
#   code, BLOCKS on browser confirmation — never use this inside a systemd unit)
```

Codex writes auth to `~/.codex/auth.json` (0600) and config to `~/.codex/config.toml` — **not** `~/.claude/`. Same isolation rule: each account needs its own `~/.codex/`; don't share it across accounts (the API key sits in `auth.json` in plaintext — sharing it widens blast radius). A pure-codex daemon does not need `bun` (§1). Everything codex-specific past install is consolidated in **§12**.

### 4. The agent-daemon pattern

For each agent, you need:
1. A supervise script at `~/.local/bin/<agent>-bot-supervise.sh`.
2. A systemd user service at `~/.config/systemd/user/<service>.service`.
3. The tmux session that the supervise script keeps alive.

Pick whatever names work for these. The reference instance uses session names like `claude-telegram-bot` (Orchestrator), `claude-research-bot` (Specialist), `claude-telegram-bot-fallback` (Recovery). The "telegram" in some of those names is a stability-naming quirk — see the glossary. The names are arbitrary as long as the supervise script, the systemd unit, and the recovery-layer configs (`context_fill_monitor.py`, `restart_drain.py`) all agree.

#### 4.1 The supervise script (the core idiom)

This is the load-bearing pattern. **Reproduce its shape verbatim.**

A naive approach — `Type=oneshot` + `RemainAfterExit=yes` firing `tmux new-session -d` — looks right but is silently broken: systemd reports the service as "active (exited)" forever even after the tmux session and the inner Claude Code die. Nothing restarts it.

The fix: a wrapper that *blocks* as systemd's MainPID for as long as the tmux session is alive. When the session dies, the wrapper exits, and `Restart=always` brings it back:

```bash
#!/usr/bin/env bash
# ~/.local/bin/<agent>-bot-supervise.sh
set -euo pipefail

SESSION="<your-session-name>"
CLAUDE_BIN="$HOME/.npm-global/bin/claude"
CLAUDE_ARGS=""    # live chat-transport agent: "--channels plugin:telegram@claude-plugins-official" (plugin self-poll, §7 — use this for ALL new agents, orchestrator included). NB: the older "--dangerously-load-development-channels server:telegram-bridge-stdio" shared-bridge arg is the historical ORG-004 model, decommissioned 2026-06-04 (T-0767) — all agents now self-poll; do NOT use it for new agents.

# Wrapper that creates the session if absent, then blocks on its lifetime.
# The `sg <shared-group>` wrap is only needed if this agent needs to inherit
# the supplementary group (typically your orchestrator does; your specialist
# might not). Omit if the agent doesn't need it.
SG="sg <shared-group> -c"       # or just "bash -c" if no shared group

if ! $SG "tmux has-session -t $SESSION" 2>/dev/null; then
  $SG "tmux new-session -d -s $SESSION '$CLAUDE_BIN $CLAUDE_ARGS'"
fi

while $SG "tmux has-session -t $SESSION" 2>/dev/null; do
  sleep 5
done
exit 0
```

Add a preflight check at the top of the wrapper so a wrong binary path fails LOUD instead of crash-looping silently (a missing `CLAUDE_BIN` makes the session exit instantly and `NRestarts` climb while `is-active` still reads `active`): `[[ -x "$CLAUDE_BIN" ]] || { echo "CLAUDE_BIN not found: $CLAUDE_BIN" >&2; exit 1; }`.

The canonical reference impl is `tools/systemd/recovery-bot-supervise.sh`. Copy + adapt instead of writing from scratch. **Watch the `SG=` line:** the §4.1 template above shows `SG="sg <shared-group> -c"`; if this agent has no shared group, set `SG="bash -c"` — leaving the literal `<shared-group>` placeholder fails immediately (`sg: group <shared-group> does not exist`). The reference `recovery-bot-supervise.sh` calls `tmux` directly with no `SG` wrap; match whichever form fits your agent.

**Codex daemon (§12):** set `CLAUDE_BIN` to the codex binary (`CLAUDE_BIN="$HOME/.npm-global/bin/codex"`) and **clear `CLAUDE_ARGS`** — codex has no `--channels` flag (confirmed: `codex --channels …` → `error: unexpected argument`), so the claude chat-transport arg shown in the template does not apply and will hard-error codex on launch. A bare `codex` drops into its interactive TUI, which is exactly what the tmux-session-alive pattern needs. (The `--channels`/plugin self-poll comment in the template is Claude-Code-specific; see §12 for how a codex daemon gets a chat surface.)

`chmod +x ~/.local/bin/<agent>-bot-supervise.sh`.

#### 4.2 The systemd unit

```ini
# ~/.config/systemd/user/<your-service-name>.service
[Unit]
Description=Claude Code (<persona>) — interactive TUI daemon
After=default.target

[Service]
Type=simple
ExecStart=%h/.local/bin/<agent>-bot-supervise.sh
ExecStop=-/usr/bin/tmux kill-session -t <your-session-name>
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now <your-service-name>.service
```

Run these **as the agent's own user in a real login session** (`sudo -iu <account>` then the commands, or `machinectl shell`). `systemctl --user` from root or via a bare `sudo -u … bash -c` hits the wrong (or no) user bus — `Failed to connect to bus` / silently the wrong manager. If you must drive it non-interactively, set `XDG_RUNTIME_DIR=/run/user/$(id -u <account>)` in the environment.

The `-` prefix on `ExecStop` (`ExecStop=-/usr/bin/tmux …`) is deliberate: on a normal restart the supervise loop has already exited because the session died, so `tmux kill-session` returns 1 ("no server running") and systemd logs `Failed with result 'exit-code'` on *every* restart cycle without the `-`. The prefix tells systemd to ignore the non-zero exit — cosmetic, but it stops every healthy restart from looking like a failure in the journal.

Caveat to remember: whatever session name you pick has to match across (1) the supervise script's `SESSION=`, (2) the unit's `ExecStop`, (3) the recovery-layer configs in `context_fill_monitor.py` and `restart_drain.py` (when you wire those up in step 6). A wrong session name silently breaks the recovery layer — `capture-pane` sees nothing, `send-keys` lands in the wrong place. See [`adding-restart-capability-to-a-new-agent.md`](adding-restart-capability-to-a-new-agent.md) for the Recovery-discovered gotchas.

### 5. Repo clone and the tools tree

Clone `pantheon-config` per account. The orchestrator's clone is the canonical working tree — the inbox-consumer cron reads working-tree disk state; keep it on `main`. Repo freshness is carried by the inbox-consumer's ~10-min pull (the former `pantheon-config-sync.timer` was retired by T-0365 — see step 6); kino's read-path pull-on-demand (syncGate) covers PWA reads.

For each agent (run as the agent user):

```bash
cd ~
git clone <YOUR-REPO-URL> pantheon-config
cd pantheon-config
git checkout main
```

⚠️ **Seeding a clone by `git bundle` (offline transfer, e.g. a fresh account that can't yet reach the remote): `git fetch origin` in the SOURCE clone FIRST, and stamp the manifest with origin-relative state, not just local HEAD** (field record 2026-07-20, T-1832 item 12). A bundle inherits the source working copy's staleness silently — a source clone 86 commits behind origin ships that 86-commit gap into the new account with no signal; the receiver only discovers it on the first `git pull`. Note ahead/behind per repo before bundling; the same rule applies to project-repo handovers, not just pantheon-config.

**Set each agent's git identity before the daemon runs any tooling — a fresh box does NOT ship one.** The ticket queue, the ledger, and the grooming tools all `git commit` *as the agent*; with no `user.name`/`user.email` configured, every one of those commits aborts with `fatal: unable to auto-detect email address` / "Author identity unknown". The failure is deceptive: it surfaces deep inside a tool run (a claim/resolve/append that silently can't persist), not at clone time, and the host-independent half of the suite still passes — so the gap hides until the daemon tries its first real write. Run as each agent user:

```bash
git config --global user.name  "<persona>"        # e.g. Orchestrator
git config --global user.email "<agent-email>"     # the same address you keyed the deploy key to (§5 below)
git config --global init.defaultBranch main         # the tooling + its worktree helpers assume `main`; stock git still defaults to `master`
```

(Surfaced by a clean-container bringup test 2026-06-24: `python:3.12-slim` + git but no identity → host-independent suite passes 2572/2579, but every tool path that commits fails until these three are set. The `init.defaultBranch main` line matters for any repo the tooling *creates* — worktree helpers fetch `origin main` — not the clone above, which §5's `git checkout main` already pins.)

Per-account SSH deploy keys for each repo. Generate per-account (`ssh-keygen -t ed25519 -C <agent-email>`), add the public key as a *deploy key* (not account-level) on the upstream repo, configure SSH aliases in `~/.ssh/config`. **Don't reuse a single deploy key across accounts** — that's a blast-radius footgun.

If you also adopt projects that need git-shared writes (Pantheon's reference does this for `pantheon-config` itself), the relevant accounts need `safe.directory` exceptions for cross-owned clones — e.g. an account writing into another account's clone runs `git config --global --add safe.directory <path-to-clone>` so git doesn't refuse on the ownership mismatch. The supervise-script `sg` wrap handles the supplementary-group inheritance.

**`safe.directory` also bites at clone time, not just on writes.** If you `git clone` from a *local path* owned by another user (common at bringup — e.g. the orchestrator's clone is owned by `claude` and another account clones from it by path), the clone itself aborts with `fatal: detected dubious ownership in repository at '<source>/.git'` before anything is written. Fix targets the **source** repo, run **as the cloning user** (not root — it writes that user's `~/.gitconfig`): `git config --global --add safe.directory <source-repo-path>`, then re-run the clone. If git still complains, it's naming the `<source-repo-path>/.git` path — add that one too (git's error message tells you the exact path). (SSH/HTTPS remote clones are unaffected — this is only for local-path sources.)

#### Agent memory baseline (ORG-035)

Claude Code ships a native per-agent auto-memory store (a `MEMORY.md` index + one-fact-per-file pins the harness recalls at session start). Two bringup notes, both from ORG-035 (memory-portability per-agent baseline):

- **Set `autoMemoryDirectory` to a stable, agent-keyed path** in each agent's Claude Code settings. Left unset, the harness derives the store location from the launch-cwd (the `~/.claude/projects/<mangled-cwd>/memory/` default) — so a relaunch from a different directory silently **forks** the memory into a second dir and strands the old corpus (this bit the reference orchestrator: its memory split across two cwd-mangled dirs). A fixed `autoMemoryDirectory` keys the store to the *agent*, not the launch dir.
- **The repo ships memory hygiene tooling** (`tools/memory_lint.py`, `tools/memory_index.py`, `tools/memory_search.py`): the lint validates pin frontmatter, the index generator keeps `MEMORY.md` drift-free from the files (so a hand-edited index can't go stale), and the search is an `rg`-backed CLI so non-harness consumers (and sibling agents) can query the corpus — the recall *algorithm* is harness-internal and unreplaceable, but the store is plain markdown you can lint, index, and grep. The heavyweight BM25/embeddings layer is a deliberate "not yet" downstream of this baseline.

### 6. Cron / timers — the recovery + dispatch layer

Each agent gets a different set of timers depending on its role. `tools/systemd/README.md` is the authoritative source for the per-unit install procedure; this section maps the *what* and *when*. **NB:** that README still documents some bot services in the older `Type=oneshot` form (e.g. `claude-telegram-research.service`) — for the **agent daemon itself**, the supervise-script + `Type=simple` pattern in §4 is authoritative and supersedes it; use the README for the *cron/timer* units, not for re-deriving the daemon unit.

> **Order matters: complete §5 (repo clone) before enabling any §6 timer.** Every service unit sets `WorkingDirectory=%h/pantheon-config`. Enable a timer before the clone exists and the service hard-fails at `Failed at step CHDIR … No such file or directory` (status=200) on every fire; with `Persistent=true`, missed fires also pile up. Clone first, then enable timers.

#### Orchestrator (reference: Orchestrator)
- `<your-orchestrator-bot>.service` (always-on) — the tmux Claude Code TUI from step 4. Give it the per-agent plugin self-poll chat surface like every other agent (§7).
- `pantheon-bridge.service` — **historical / decommissioned 2026-06-04** (T-0767; ORG-004 retired; see §7). The shared chat-transport bridge: one process owned the bot's poll lock and fanned out to SSE clients. All three agents — orchestrator included — now use per-agent plugin self-poll (§7); **new builds should NOT install the bridge.**
- `pantheon-inbox-consumer@<orchestrator>.{service,timer}` (~10 min) — drains the orchestrator's ticket inbox.
- `pantheon-ho-resumer.{service,timer}` (~5 min) — polls cap-hit state and fires `claude --resume` after the Max window resets.
- `pantheon-restart-drain.{service,timer}` (~30s) — processes restart tombstones (gated on `PANTHEON_RESTART_DRAIN_ENABLED=1` drop-in).
- `pantheon-config-sync.{service,timer}` (~60s) — **historical / retired 2026-06-09** (T-0365; units disabled + uninstalled). Was bidirectional repo sync; the inbox-consumer's ~10-min pull now carries repo freshness and kino's syncGate covers PWA read-path pulls. **New builds should NOT install this timer.** The `tools/pantheon_config_sync.py` tool itself is retained (kino's syncGate may invoke it).

#### Specialist (reference: Specialist)
- `<your-specialist-bot>.service` (always-on) — the tmux Claude Code session.
- `pantheon-watch.{service,timer}` (daily) — fires due research dispatches.
- `pantheon-watch-digest.{service,timer}` (daily, offset) — summarizes the day's reports.
- `pantheon-knowledge-sweep.{service,timer}` (daily, 13:00 UTC) — ORG-012 Phase C scripted knowledge promotion/demotion sweep (T-0781): a due-detector that files an in-session sweep-dispatch ticket when the knowledge corpus changes (collapsed to a ~weekly batch by a 7-day floor). It is a ticket-filer, **not** a subagent-firer — the review fan-out runs in-session. It also runs the watch-report day-index integrity lint as a non-blocking side check (T-1121/T-1127), so manual report deliveries that bypass the runner's index regen surface within a day.
- `pantheon-keep-going@<specialist>.{service,timer}` (~15 min) — anti-idle heartbeat (ORG-011 stays-working-under-silence): nudges the unattended agent to pick up queued work or, when the queue is genuinely dry, honest-hold. Per-persona template (parallel to `mailbox-wake`); install for any agent that does autonomous queue work.
- `pantheon-inbox-consumer@<specialist>.{service,timer}` (~10 min).
- `pantheon-restart-drain.{service,timer}` (~30s).

#### Recovery (reference: Recovery)
This account has cross-account sudo, so it's the natural home for system-wide monitors.
- `<your-recovery-bot>.service` (always-on) — recovery agent's tmux Claude Code.
- `pantheon-context-fill-monitor@<each-agent>.{service,timer}` (~5 min, per-agent) — polls each agent's session JSONL; fires graceful restart at ~60% context fill.
- `pantheon-activity-monitor@<orchestrator>.{service,timer}` (~15 min) — stays-working-under-silence predicate per ORG-011.
- `pantheon-activity-digest.{service,timer}` (every ~5h) — system-pulse digest to operator's chat.
- `pantheon-grooming.{service,timer}` (daily) — ticket staleness scan + digest.
- `pantheon-spine-index-groom.{service,timer}` (daily, offset from grooming) — idempotent updater for spine READMEs.
- `pantheon-ticket-lint.{service,timer}` (hourly) — ticket frontmatter format scan + auto-fix.
- `pantheon-channel-liveness.{service,timer}` (~5 min) — comms-liveness probe: detects a **comms-dark-but-session-healthy** agent (inbound poller dead while the tmux session still looks fine), the silent-outage class that cost ~22h of detection lag in the 2026-05-21 daemon incident (T-0894).
- `pantheon-weekly-ticket-summary.{service,timer}` (Mon 09:00 UTC) — generates the prior-ISO-week ticket summary (`reports/<YYYY>-W<NN>.md`).
- `pantheon-inbox-consumer@<recovery>.{service,timer}` (~10 min).
- `pantheon-restart-drain.{service,timer}` (~30s).

> **Reference-instance extras (optional / niche).** The reference box also runs a few timers that are owner- or deployment-specific and not load-bearing for a generic bringup: `pantheon-session-reset-reconcile` (folds the session-reset log into ledger entries, T-0257), `pantheon-spine-cross-repo` (owner-run cross-repo doc-index grooming — only once you have >1 repo, T-0666), `pantheon-apt-update` (host package hygiene), and PWA/deploy-specific units (`pantheon-kino-pwa-autodeploy`, `pantheon-policy-outflow`). The authoritative full set is whatever is under `tools/systemd/`; install what your topology needs, not the whole list.

#### Install pattern

```bash
mkdir -p ~/.config/systemd/user
cp ~/pantheon-config/tools/systemd/<unit>.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now <unit>.timer
```

> **Silent-failure guard comes WITH the template — don't strip it, and install its leaf (T-1928).** Every oneshot `.service` template ships with `OnFailure=pantheon-unit-failure-alert@%n.service` in `[Unit]` (T-1868/T-1928 — liveness units that fail silently are the worst failure class; the 2026-07-21 root cause was installed copies wired by hand while the repo templates stayed bare, so new accounts inherited dead alerting). Two obligations when installing on a NEW account: (1) also `cp` `pantheon-unit-failure-alert@.service` (the OnFailure leaf — without it every guard reference points at a nonexistent unit and alerting is dead while everything reads green); (2) verify with `grep -L "^OnFailure=" ~/.config/systemd/user/pantheon-*.service` — the only hits should be long-running `Restart=` services and the alert template itself. Smoke per the T-1868 recipe: `systemd-run --user --unit=tNNNN-smoke-fail --property=OnFailure=pantheon-unit-failure-alert@tNNNN-smoke-fail.service.service /bin/false`, confirm the alert lands in Recovery's mailbox, then `reset-failed` the transient.

For templated units (`@`-style), the instance is the **persona / `AGENT_CONFIG` key** (e.g. `orchestrator`/`specialist`/`recovery`), NOT the Unix account name — they differ on the reference instance (persona `orchestrator` runs as account `claude`). The `%i` must match the key you use in `tools/context_fill_monitor.py` `AGENT_CONFIG`, `tools/restart_drain.py` `DRAIN_AGENT_CONFIG`, and `sop/standard/agent-role-map.md`. Enable e.g. `systemctl --user enable --now pantheon-inbox-consumer@specialist.timer` (persona key), not `@claude` (account). If you make persona == account, they coincide; otherwise use the persona.

> **⚠️ CRITICAL — the recovery configs are now BUILT FROM `instance.yaml` (T-2076), pre-filled with the REFERENCE instance's names** (`claude`/`claude-research`/`claude-fallback` accounts, `claude-telegram-research.service`, etc.). §4 told you to "pick whatever names work for you" — so if you chose different account/service/session names, the shipped `instance.yaml` `roles:` point at accounts/services that **don't exist on your box**. The failure used to be SILENT AND DECEPTIVE (the drain fires a restart against a nonexistent service; the §9 first-fire *looks* like it passed). Two things now catch it: the T-2071 account-existence check makes `context_fill_monitor.py --validate` / `restart_drain.py --validate` FAIL LOUDLY when an account doesn't resolve, and the config is single-sourced so you edit ONE place. **Before arming the recovery layer, set YOUR accounts + restart fields in `instance.yaml`** (see below), then run `--validate` on both and confirm a real first-fire (`systemctl --user show <your-service> -p NRestarts` increments), not just that the tombstone vanished.

**Configure the recovery layer in `instance.yaml` — ONE place, not two Python dicts (T-2076).** `context_fill_monitor.AGENT_CONFIG` and `restart_drain.DRAIN_AGENT_CONFIG` are now BUILT from your `instance.yaml` `roles:`. For each monitored role you set the **account** and the **restart fields**; the monitor derives everything account-shaped (`jsonl_dir`, `handoff_file`, `restart_bin`, `supervise_script` dir) from the account, so there is nothing to keep in sync by hand:

```yaml
# instance.yaml — per role that the recovery layer monitors:
<role-key>:
    persona: <persona>
    account: <account>                 # -> sudo_user + all account-derived paths
    restart_service: <service>.service # must equal the §4.2 unit filename
    restart_mode: user                 # user-systemd service
    restart_pane: <session-name>:0     # must equal SESSION= in the supervise script
```

Derivation (all from `account`, so a wrong account fails ONE check, not five):
`sudo_user=<account>`; `jsonl_dir=/home/<account>/.claude/projects/-home-<account>` (home path, every `/`→`-`, leading dash included); `handoff_file=/home/<account>/HANDOFF.md`; `restart_bin=/home/<account>/pantheon-config/tools/handoff_and_restart.sh`; drain `restart_lockfile=/tmp/handoff_and_restart_<persona>.lock`.

**Monitor-side tuning stays in the monitor**, keyed by persona in `context_fill_monitor._MONITOR_TUNING` (values `instance.yaml` can't know): `context_window`, `supervise_script_name`, `model_pin` (host-local), `use_tombstone` (agents that can't self-restart — Recovery-pattern), and the observation-only flags (`observation_only`/`remediate`/`hook_check_via_recovery`). A new monitored agent needs BOTH an `instance.yaml` role (account + restart fields) AND a `_MONITOR_TUNING` entry.

**Then VALIDATE (loud, T-2071):** `python3 tools/context_fill_monitor.py --validate` and `python3 tools/restart_drain.py --validate` — both exit non-zero and name the problem if any account doesn't exist on the box or a path doesn't resolve (the old silent-misconfig class: a wrong `jsonl_dir` used to log "no jsonl found" and never fire; a missing persona key used to re-accumulate tombstones every 30s with no alert). On a **fresh** install `jsonl_dir` won't exist until the agent's first session ends — the monitor bails clean (exit 0) until then; that's correct, not a misconfig. (For a codex agent these monitors don't apply at all — see §12.3.)

**Arming the restart-drain (required — it no-ops silently until armed).** `pantheon-restart-drain` bails clean unless `PANTHEON_RESTART_DRAIN_ENABLED=1` is set, so a drained-but-unarmed agent looks healthy while its recovery layer is dead. Create the drop-in per account:

```bash
mkdir -p ~/.config/systemd/user/pantheon-restart-drain.service.d
cat > ~/.config/systemd/user/pantheon-restart-drain.service.d/enable.conf <<'EOF'
[Service]
Environment=PANTHEON_RESTART_DRAIN_ENABLED=1
EOF
systemctl --user daemon-reload
```

Note the §9 verification's "drain cycling cleanly" check passes even when unarmed (it logs a clean "disabled" line) — so confirm arming via the supervised first-fire (synthesize a tombstone and watch one real restart land), not the journal alone.

#### Cadence interleaving

Daily and hourly fires are deliberately offset so they don't collide on git push. The reference instance uses: watch 08:00 → grooming 12:00 → spine-groom 12:30 → watch-digest 18:00 → ticket-lint at :20 each hour. **Preserve the offsets when you customize.** Two daily timers firing at the same minute will race the auto-commit/push paths.

### 7. Comms (chat transport)

> **⚠️ This entire section is Claude-Code-specific.** Every mechanism below — the `--channels` launch arg, `enabledPlugins["telegram@claude-plugins-official"]`, `~/.claude/channels/`, the `bun server.ts` poller, `access.json`, `bot.pid`, the `plugin:telegram:telegram` permission — belongs to the Claude Code Telegram plugin and has **no codex analog**. A codex daemon gets its chat surface a completely different way (codex `mcp add` for inbound, or outbound-only direct-curl). If you are standing up a codex daemon, read **§12** instead of this section. The only part below that transfers to codex is the *outbound-only direct-curl `sendMessage`* fallback (modulo the token path — see §12).

The reference instance uses Telegram. **Live model: per-agent own-bot, official-plugin self-poll** — each agent runs its OWN bot + the official `telegram@claude-plugins-official` plugin, self-polling `getUpdates`. (Worked example: Specialist brought up this way 2026-06-03, T-0754/T-0755; cross-ref the `reference_telegram_agent_setup` memory.)

> **Original model (now retired):** the shared `pantheon-bridge` daemon (one process owning `getUpdates` + SSE fan-out to all agents, ORG-004) was the original model. It was **decommissioned 2026-06-04 (T-0767; ORG-004 retired, repo archived)** once HO/Orchestrator also moved to its own bot — all three agents now self-poll. Its only remaining purpose had been SSE fan-out to a PWA chat that was itself sunset, leaving a failure-prone daemon nothing used. New agents use the per-agent self-poll model below; it avoids the bridge's recurring failure mode (T-0719/T-0759): a background-job conversation could open a *second* `telegram-bridge-stdio` SSE client on the same bot token, scrambling inbound routing. Distinct bots, no shared poller, no SSE fan-out → the whole class is gone. (NB: the 2026-06-13 Anthropic billing change is unrelated to the bridge — it affects headless `claude -p` only.)

Per-agent setup (each agent that needs an inbound chat surface):
1. **Own bot token** — one bot per agent from BotFather. Distinct bots → no `getUpdates` eviction between agents (eviction only happens when instances share ONE bot).
2. **Install AND enable the plugin.** A fresh Claude Code install has **no marketplace and no plugin on disk** — `claude plugin list` → "No plugins installed". Setting `enabledPlugins=true` on a plugin that isn't installed does nothing (or crash-loops `bun`). You must add the marketplace and install first:
   ```bash
   claude plugin marketplace add anthropics/claude-plugins-official   # github source
   claude plugin install telegram@claude-plugins-official              # auto-sets enabledPlugins=true
   ```
   `install` writes `enabledPlugins["telegram@claude-plugins-official"]: true` into `~/.claude/settings.json` for you — so the manual settings.json edit is only needed if you later disabled it. Confirm: `claude plugin list` shows it enabled.
   ⚠️ **THE keystone gotcha:** launching with `--channels plugin:telegram@claude-plugins-official` while the plugin is `false`/absent prints the channel banner but loads **nothing** — no skill (`/telegram:configure` → "Unknown skill"), no MCP server, no poller. **The banner is visually identical whether or not the poller loaded** — there is no "plugin disabled" sub-text. The banner is NOT proof it loaded; only `~/.claude/channels/telegram/bot.pid` present + a `bun server.ts` process is (see "Verify it actually loaded" below).
3. **Token file** — `~/.claude/channels/telegram/.env` (0600): `TELEGRAM_BOT_TOKEN=<bot token>`. The plugin's `server.ts` reads exactly this path/var.
4. **Access whitelist** — `~/.claude/channels/telegram/access.json`: `{"dmPolicy":"pairing","allowFrom":["<operator-chat-id>"],"groups":{},"pending":{}}` — bounds who may DM the agent (operator only by default).
5. **Launch arg** — you wrote the supervise script in §4 with `CLAUDE_ARGS=""`; **go back and edit it** to `CLAUDE_ARGS="--channels plugin:telegram@claude-plugins-official"`, then `systemctl --user restart <service>`. (A daemon left at `CLAUDE_ARGS=""` runs fine but has NO Telegram surface and gives no error — easy to miss.)
6. **bun** installed per-account (step 3) — the plugin's poller runs as `bun server.ts`.
7. **First reply-tool use** prompts a permission; choose **"don't ask again for `plugin:telegram:telegram - reply` commands"** (safe — bounded by access.json) or the agent wedges on every outbound reply.

**Verify it actually loaded (check the poller, NOT the banner):** a `bun …/telegram/<ver> … start` + `bun server.ts` process is running, and `~/.claude/channels/telegram/bot.pid` exists. Inbound debug: `getWebhookInfo` showing `pending_update_count>0` with no poller = the message reached Telegram but nothing is consuming it.

**Agent autonomy — `permissions.allow` (settings.json, while you're in there):** an agent with an EMPTY `permissions.allow` prompts/wedges on nearly every tool call → it can't act autonomously (defers work, wedges on routine Bash/Write). Give each daemon the working allowlist (reference set: `["Bash","Edit","Glob","Grep","Read","WebFetch","WebSearch","Write","Edit(/home/<agent>/HANDOFF.md)","Write(/home/<agent>/HANDOFF.md)","Skill(deep-research)"]`). This is a standing trust grant — operator/orchestrator decision, scoped per the agent's tier/domain.

**The allowlist must ALSO cover every MCP tool the agent invokes unattended — above all its chat-surface plugin tools.** MCP tools are NOT covered by the built-in tool names above; each needs its own entry, named `mcp__<server>__<tool>` (plugin-provided servers: `mcp__plugin_<plugin>_<server>__<tool>`). For a Telegram-plugin agent that means all four: `mcp__plugin_telegram_telegram__reply`, `…__react`, `…__edit_message`, `…__download_attachment` — plus any org-transport MCP (e.g. `mcp__mailbox__mailbox_send`, `mcp__mailbox__mailbox_consume`). ⚠️ **This class of gap survives parity audits** (field record, 2026-07-20 mnemosyne bringup): the tool names don't exist until the plugin/MCP is live on that account, so copying a reference allowlist — or auditing against one — passes while every single chat reply still fires a permission prompt to the operator's phone. The failure mode is loud to the operator and invisible in every §9-style check: the daemon looks healthy and DOES chat (each message hand-approved), it just can't act unattended. Audit rule: after the chat surface is verified live, list the MCP tools the agent actually called in its first sessions and check each against `permissions.allow` — don't audit against the reference set alone. (`Skill(deep-research)` is in the baseline because the deep-research skill is invoked unattended — cron'd/dispatched research — and otherwise wedges on a Telegram-relayed permission prompt with no one present to approve it; T-1450, operator-approved 2026-06-29. Other skills are NOT pre-allowed — widen per-agent only with evidence of another unattended skill invocation.) **This is an explicit action item, not auto-populated:** Claude Code never writes a `permissions` key on its own — set it by hand for every daemon, or the agent wedges on the first Bash/Write. (The §9 "permissions.allow populated" check fails until you do this.) ⚠️ **Merge, don't overwrite:** by the time you reach this step, step 2's plugin install has already written `enabledPlugins` (and `extraKnownMarketplaces`) into `settings.json`. Add `permissions` as a NEW key alongside them — e.g. with `python3 -c 'import json,os;p=os.path.expanduser("~/.claude/settings.json");d=json.load(open(p));d["permissions"]={"allow":[…]};json.dump(d,open(p,"w"),indent=2)'` — a naive `echo '{…}' > settings.json` wipes the plugin config and silently breaks Telegram.

Outbound-only option (no inbound DM): skip the plugin; the agent direct-curls `sendMessage` (token in `~/.claude/channels/telegram/pantheon.env`) for status snapshots only. Other platforms: same per-agent-client shape against that platform's bot API.

### 7b. Inter-agent mailbox (ORG-026 — optional, for multi-agent intercomms)

§7 is agent↔**human** chat. This is agent↔**agent** mail: a daemonless, file-based mailbox (Maildir-style `tmp`→fsync→rename) for one running agent to deliver work/replies to another and wake it. Skip it for a single-agent instance; provision it once you have ≥2 always-on agents that need to coordinate hands-off. Canonical design + as-implemented reality: [`adr/ORG-026`](../../adr/ORG-026-inter-agent-message-passing-substrate.md). Code: `tools/{mailbox,mailbox_wake,provision_mailbox}.py/.sh`.

**Substrate (Path A — "conditionally a shared HOME"):** a private 750 home is untraversable cross-account, so inboxes do NOT live in `~/inbox`. They live in a neutral group-accessible hub:
- A dedicated **mail group** (reference instance: `veneration`, gid 1006 — reused from the Veneration shared-state account; pick any group). Every participating agent's Unix user ∈ this group (`usermod -aG`, root).
- Hub host = a neutral account (reference: `the-ancestor`, the shared-state host) with home at **`0710 <group>`** (group **traverse-only** — peers `cd` through to a known path but can't enumerate the home).
- Per-agent inboxes at **`/home/<hub>/mailbox/<agent>/{tmp,new,cur}`**, mode **`2770 <group>`** (setgid + group-writable) so any group peer delivers + the consumer acks.

**The load-bearing constraint (do not re-derive — see [`feedback_user_mode_systemd_cannot_set_supplementary_groups`] and ORG-026 §"Implementation reality"):** a `systemctl --user` unit *and the user-systemd manager itself* carry only the account's **primary** group (`SupplementaryGroups=` fails on `--user` units). Two consequences:
- The wake `.service` ExecStart **MUST** `sg <group> -c '…'`-wrap to read the group-gated hub at run time. A live process that lacks the group (most agent sessions until a manager restart) also needs the `sg` wrap to send.
- The systemd **`.path` (inotify) watch is UNUSABLE** here — the watch is established by the manager, which can't traverse the `0710` hub home, so it silently never fires. Wake is a **clock-driven `pantheon-mailbox-wake@.timer`** (every 5 min) instead; the consumer still `readdir(new/)` authoritatively (idempotent + marker-debounced), so the timer is only the trigger, not the source of truth.

**Provision (per agent):** `bash tools/provision_mailbox.sh --agent <name> --group <group>` — **dry-run by default** (prints every mutating command, changes nothing; safe to inspect for any agent). Add `--apply` to execute. ⚠️ Running `--apply` for an agent **other than yourself** (cross-account install + `enable-linger`) is a **supervised step gated on the operator/the operator** — not an unattended action. After apply: `systemctl --user enable --now pantheon-mailbox-wake@<agent>.timer` and verify a round-trip (deliver to the agent's `new/` → the timer wakes it → it consumes + acks to `cur/`).

**Timer-anchor caveat:** the wake `.timer` must carry a restart-relative anchor (`OnBootSec`) alongside `OnUnitActiveSec`, or it stalls permanently after a user-manager restart (no post-restart activation history → no next-fire). See `notes/2026-06-09-infra-patterns-observed.md` and the timer-anchor discipline in `knowledge/infra-abstractions/cross-cutting-patterns.md`.

**Arming-verification ordering (field record 2026-07-21, Metis/`suborch1` inbox-consumer bringup, T-1919):** two traps bracket the `enable --now` of one of these oneshot-driven timers, and the check that settles both must be taken in the right order:
1. **Supervised first fire = two layers, not one.** Firing the ExecStart script directly proves the CODE; it says nothing about the UNIT (which brings its own `PATH`/`WorkingDirectory`/env). Fire BOTH — `python3 …/inbox_consumer.py --agent <p>` AND `systemctl --user start <unit>` (timer still disabled) — before arming the timer. The script-passes-but-unit-fails gap is exactly the wrong-layer class this whole surface keeps hitting.
2. **`NextElapse=infinity` right after `enable --now` is a TRANSIENT, not the T-1020 stall.** An `OnUnitActiveSec` timer cannot anchor its next-elapse until the triggered oneshot goes *inactive*; read too early (oneshot still in-flight) and `systemctl --user list-timers` shows `NEXT=-` / `NextElapseUSecMonotonic=infinity` — the identical signature to the real post-restart stall. Both wrong reads are available here: reporting the first read is a false stall alarm; declaring "enabled+active, done" without checking NextElapse at all is a false green (which is how T-1020 went unnoticed). **The settling check: NextElapse finite, taken AFTER the triggered oneshot completes.** Then confirm one real TIMED fire lands (arming ≠ firing — resolve on an observed timed cycle, not a scheduled one).
3. **`enable` alone does not close the unsupervised-fire window.** The symlink lands in `timers.target.wants`, so a manager restart/reboot auto-starts the timer and `OnBootSec` fires it unattended. For a genuinely supervised first fire, `disable` the timer until the coordinated moment, then `enable --now`.

**Scope of the gate — armed timers have NO code-version gate (T-1946 ruling, 2026-07-23):** the enable/disable discipline above gates NEW-timer arming and it is real. It does NOT gate the CODE an already-armed timer executes: units resolve `ExecStart=%h/pantheon-config/tools/<x>.py` from each agent's WORKING CLONE, so for an armed timer **merge + routine pull = the new code is live on its next scheduled fire** — there is no deploy step (verified live: T-1926's floor first fired 13 min after landing, unsupervised, benign). Ruled posture = accept-and-document: for code changes to armed-timer tools the honest control is **supervised-first-BIND** — observe the first run where the changed predicate/behavior actually binds — not "supervised first fire," which the merge already forfeited. A per-unit enable-flag mechanism (the only REAL code gate) is deliberately NOT the standing pattern (the operator disprefers flag-file ops surfaces; flags are backup kill-switches) — it remains the named ESCALATION for a tool whose first live fire would be genuinely dangerous (mutating/destructive class), decided per-tool at review time. If you need the change inert until a coordinated moment and the tool has no flag: disable the TIMER before merging, re-enable at the moment — the unit-level gate is the one that exists.

**ACL-model reality (supersedes the bare `2770` group-dir description above — T-1733, trusted-N per-user ACLs):** the live hub gates each inbox with **per-user ACLs** (`user:<account>:rwx` + matching **default** ACLs; group class `---`), not group-write. The canonical current member list lives in `bugs/mailbox-permission-denied-is-acl-by-design.md`. **Onboarding agent N+1 needs TWO grant directions, and the second is the one that gets missed** (field record 2026-07-20, Mnemosyne/`exocortex` bringup: T-1824 granted her own inbox only → she could receive but not send — "Permission denied into Orchestrator's inbox" on her first outbound, caught by the operator, TG 3846):
1. **Receive:** `u:<account>:rwX` + defaults on the agent's OWN inbox tree (what "provision the inbox" naturally covers).
2. **Send:** `u:<account>:rwX` + defaults on **EVERY SIBLING inbox dir** (`<box>/`, `tmp/`, `new/`, `cur/`) — apply as the hub owner; `setfacl -R` will throw `Operation not permitted` on existing *message files* (sender-owned; only dirs matter for the send path — expected noise, not failure.)
3. **Verify BIDIRECTIONALLY as the new account's uid** — a write-probe into each sibling `tmp/` (`sudo -nu <account> touch <box>/tmp/.probe && rm`) plus one real round-trip in each direction. A receive-only mailbox looks fully healthy until the agent's first reply.

**Mailbox MCP dependency (field record — sharpened T-1903, 2026-07-21 suborch bringup):** registering `tools/mailbox_mcp.py` as an MCP server is NOT sufficient — the account needs the python `mcp` package installed **per-account**, not system-wide (it commonly lives only under the HO's `~/.local` and is invisible to a fresh account). A registered-but-dep-missing MCP server shows `claude mcp list` → `mailbox FAILED` and fails silently at session start; the agent discovers it only when a send is attempted (or falls back to importing `tools/mailbox.py` directly, same transport, which masks the gap). Exact fix — the PEP-668 flag is load-bearing (a bare `pip install --user mcp` is refused on a modern externally-managed Python). **Pin `mcp<2` (T-2521):** mcp 2.0.0 (stable on PyPI 2026-07-28, and what a bare `pip install mcp` now resolves to) removed the decorator-based lowlevel-Server handler API — the `@app.list_tools()` / `@app.call_tool()` decorators `mailbox_mcp.py` registers with — so an unpinned fresh install silently pulls 2.x and breaks cross-seat mailbox delivery (load-bearing infra) at session start. The `<2` bound keeps the working 1.x line and is consistent with §10's `mcp==1.27.1` test-harness pin (runtime needs `<2`, the harness needs 1.27's response shape — 1.27.1 satisfies both). Migrating to the v2 `on_*` constructor API is a separate, deferred decision (staying on 1.x means security-only fixes per the v2 release notes; and the v1 `async with stdio_server()` transport pattern's survival under 2.x is unverified — check it before scoping any migration):

```
pip install --user --break-system-packages 'mcp<2'
python3 -c "import mcp; print('mcp import OK')"   # verify before calling §7b done
```

(Mnemosyne's exocortex bringup did NOT hit this because that app shipped its own venv; the copy-provisioning path — §7c — and any bare-account seat both do. Verify the MCP tools actually surface in-session, not just that the import works, before calling §7b done.)

**Restart-after-repair — `claude mcp list` green is a FALSE GREEN for a running session (T-1908, Atlas diagnosis 2026-07-21).** An MCP stdio server is connected **once at session start, with no retry** — a session that started before the `mcp` dep was installed holds a *permanent* failed-at-startup state (zero tools registered) even after the dep lands. The trap: `claude mcp list` reports the server **Connected** because it spawns a NEW short-lived process that loads the now-present dep — it says nothing about the *already-running* session's tool registry. So after installing/repairing the dep, you MUST **restart the affected session(s)**; the fix is the restart, not the install alone. Verifiable done-bar (do not self-assert): `pgrep -u <account> -af mailbox_mcp` returns a **child of the live session** (a healthy session has one; a stale session shows nothing), a fresh `~/.cache/claude-cli-nodejs/*/mcp-logs-mailbox` log under the NEW session id reads `Successfully connected … hasTools:true` **and does not immediately tear down** (the false-green process connects then exits ~40ms later — persistence is the distinguishing tell), and — **the decisive check (Atlas refinement, T-1908): CALL a tool, don't just confirm it's registered.** A registration/listing check is exactly what produced the false green in the first place, so a done-bar that stops at "tools resolve" inherits the bug it exists to prevent. Invoke `mailbox_consume` (or any real tool) and confirm the server-side `Tool '…' completed successfully` log line. Calling it is the only check the false-green can't pass.

**ACP-delivery variant (ORG-031/T-1689 — one agent piloted, others by supervised flip):** if the agent's inbound rides the mailbox→ACP bridge instead of the wake timer, the flip is its own supervised procedure — `tools/acp-bridge-pilot-runbook.md` is canonical. Bringup-relevant field records from the 2026-07-20 second flip (Mnemosyne): (a) the bridge + supervise units run **from the agent's own pantheon-config clone** — an agent without a clone (deliberate config-autonomy or not) needs one provisioned, or a consciously repointed unit path, BEFORE the flip; (b) the first-live-turn canary must include a **file-reading** turn, not just a simple ack — the fs-capability deadlock (bridge advertising unimplemented `fs.readTextFile`; wrapper blocks forever awaiting a discarded request) shipped dark through 7 days of simple-turn soak and fired on the first turn that touched files (fixed in `acp_bridge.py` PR 68; wetrun-gate addition tracked orchestrator-side); (c) the systemd minimal `PATH` does not include the per-account npm prefix where `claude-code-acp` lives — the PR 55 class crash-loop; the runbook carries the fix; (d) the bridge's `--prompt-timeout` (default 300s) treats a still-WORKING long turn as failed → no ack → the message redelivers and the agent re-does the work (observed up to 3× per message on substantive turns, Mnemosyne first-night data). Size the timeout to the agent's real turn lengths at flip time — the knob is a unit drop-in passing `--prompt-timeout <secs>` (PR 69; reference value 1800s on the Mnemosyne bridge); a busy agent needs well above the default.

### 7c. Copy-provisioning a new agent from an existing one (the fast path — and its traps)

§§3/7 describe standing an agent up by **fresh** onboarding + plugin install. There is a faster path when a working sibling agent already exists on the box: **copy its `~/.claude/plugins` tree + `settings.json` shape into the new account** instead of re-running marketplace-add/install. It works and it is quick, but it has a distinct gotcha set the fresh-install path never hits — because the fresh path never *copies* anything. All field records below are from the 2026-07-20 Mnemosyne (`exocortex`) bringup.

**The symptom that unifies most of them:** the daemon comes up, the channel banner renders, the session is at an idle prompt — and the bot is silently dark (no `bun server.ts` poller, no telegram MCP log dir under `~/.cache/claude-cli-nodejs/<cwd-slug>/`). Green-looking, non-functional. Work these before concluding the copy failed:

1. **`enabledPlugins` lives in `settings.json`, not in the plugins tree.** Copying `~/.claude/plugins/` is not enough — without `{"enabledPlugins": {"telegram@claude-plugins-official": true}}` in the new account's `settings.json`, the channel launch prints the banner and loads nothing (same keystone gotcha as §7 step 2, reached from the copy direction).
2. **The copied plugins registry carries the SOURCE account's absolute paths.** `~/.claude/plugins/installed_plugins.json` (`installPath`) and `known_marketplaces.json` (`installLocation`) point at `/home/<source-account>/.claude/plugins/...`, which the new account cannot read → plugin resolution fails with no visible error. Rewrite both with a `sed` path substitution from the source home to the dest home on those two files. (Diagnostic ladder + the two-file list: `bugs/new-agent-home-telegram-plugin-provisioning-gotchas.md`.)
3. **`.claude.json` trust flags + onboarding suppression.** Copy-provisioning skips the interactive onboarding that normally writes these, so set by hand: `hasCompletedOnboarding: true`, and `projects.<cwd>.hasTrustDialogAccepted: true` for BOTH the daemon's working dir AND the plugin cache dir — or the first supervised fire wedges on a trust dialog (§ Common failure modes).

**⚠️ DO NOT copy `~/.claude/.credentials.json` (the load-bearing rule — T-1874, a real fleet auth-outage).** Every other config file copies fine; the OAuth credential does NOT. Anthropic refresh tokens **rotate single-use**: two accounts holding the same copied credential each refresh independently, and whichever refreshes first rotates the token server-side, **silently invalidating the other account's stored refresh token** → a server-rejected refresh blanks the credential and auth-kills that daemon mid-session. This is exactly what took the orchestrator dark for ~9 hours on 2026-07-20 (lineage confirmed: the two credentials' fingerprints were 267ms apart). **Each account does its OWN interactive login** (§3) — no shortcut. Corollary: `refreshTokenExpiresAt` is fixed ~30 days from login, so a whole lineage that was ever cred-copied shares one expiry cliff; if you ignored this rule historically, all copied-lineage accounts need a pre-emptive per-account relogin before that date (`tools/credential_expiry_monitor.py --agent <name> --dry-run` reads the live value; `sop/remediation/agent-oauth-relogin.md` drives the pane-side flow).

**Sequence group grants BEFORE the first session start (T-1832 item 5, sharpening §7b's `sg`-wrap note).** Linux stamps supplementary-group membership onto a process at start time. If you `usermod -aG <mail-group> <account>` *after* the daemon's session process is already running, that live process does NOT carry the group and every hub mailbox op fails until either a per-command `sg <group> -c '…'` wrap (T-1016 stopgap) or a full session restart bakes it in. Do the group grant in the §2 substrate step, before §4 starts the daemon — then no `sg` dance and no restart is needed on day one.

**Persona-map registration (the code side of §7b — three edits, all required for a new agent to send/receive/wake):** adding inbox dirs + ACLs is necessary but not sufficient; the new persona must also be wired into the mailbox code, or `mailbox_mcp.py` fails closed on an unrecognized `$USER`:
- `tools/mailbox_mcp.py`: add to `_KNOWN_RECIPIENTS` (so peers may address it) AND `_USER_TO_PERSONA` (so its own sends resolve its identity — fails closed otherwise).
- `tools/mailbox_wake.py`: add to `AGENT_PANE` (pane target + home) so the wake path can reach its session.
(Field record: T-1824 landed these for `exocortex`→`mnemosyne`; the fail-closed `_USER_TO_PERSONA` miss is the one that silently blocks a new agent's first send.)

### 8. Secrets / env

**All values are operator-generated. Never commit real credentials.** The structural shape:

| File | Owner | Mode | Holds |
|---|---|---|---|
| `~/.claude/channels/telegram/.env` | per-account | 0600 | `TELEGRAM_BOT_TOKEN` — the agent's own bot (live plugin self-poll model, §7) |
| `~/.claude/channels/telegram/access.json` | per-account | 0600 | DM whitelist: `dmPolicy` + `allowFrom` (operator chat-id) |
| `~/.claude/channels/telegram-bridge/.env` | per-account | 0600 | **historical** (defunct bridge): `BOT_TOKEN`, `BRIDGE_TOKEN_*`, `BRIDGE_PORT` |
| `/home/<orchestrator>/root.env` | orchestrator | 0600 | `<orchestrator>=<sudo password>` — operator-placed, **never committed** |
| `~/.claude/` (per-account) | per-account | (varies) | OAuth session, MCP plugin state, hooks |
| Per-repo deploy keys | per-account | 0600 | private key |

**Operator commitments:**
- Generate fresh bot tokens via your platform's bot registry. Don't reuse.
- Generate a fresh sudo password. Place at `/home/<orchestrator>/root.env`. Never commit.
- Generate per-account, per-repo deploy keys (not account-level keys). Use SSH-config aliases.
- Branch protection on `main` for any operator-load-bearing repo — **but the agents push directly to `main` by design** (the auto-commit model; there is no PR step). So if you enable protection you MUST exempt the agent deploy keys / add them to the bypass allowlist, or every agent's `git push origin main` is rejected and the whole auto-commit layer silently stalls. A naive "require pull request, no bypass" ruleset bricks the fleet. (Symptom you'll see even when correctly configured: a `remote: Changes must be made through a pull request` line on push that nonetheless succeeds — that's the ruleset emitting on an allowed deploy-key push. A *rejected* push with no ref update is the real failure.)

### 9. Verification

Run this top to bottom before declaring the system live. If anything fails, diagnose before arming the recovery layer — an unattended restart that doesn't land clean wedges silently.

> **Topology / harness scoping.** This checklist is written for the 3-agent Claude-Code reference. Two adjustments:
> - **Single-agent topology** (e.g. the ORG-018 codex pilot): the "all three users" / asymmetric-sudo checks are N/A — there's one account and no recovery pair. Verify the one agent user exists with linger active, and skip the cross-account sudo chain.
> - **Codex daemon:** several checks below assume Claude Code internals (the "Claude Code prompt frame", `~/.claude/settings.json` `permissions.allow`, `bot.pid`/`bun server.ts`, and the JSONL-based recovery layer). **§12 gives the codex disposition for every affected check** (TRANSFERS / REWORD / N-A). Run §9 with the §12 table open.

**Substrate:**
- [ ] All agent users exist (all three for the reference topology; the single account for a lone-daemon build); the shared group (if used) has the expected members.
- [ ] `ls /var/lib/systemd/linger/` lists every agent — AND `systemctl is-active user@$(id -u <account>).service` is `active` for each (the linger file alone doesn't prove the user manager is up; see §2.3).
- [ ] `sudo -u <recovery> sudo -n -u <orchestrator> whoami` returns the orchestrator (no password prompt).
- [ ] `sudo -u <recovery> sudo -n -u <specialist> whoami` returns the specialist.
- [ ] `sudo -u <orchestrator> sudo -n -u <recovery> whoami` is denied (asymmetric sudo intact).

**Per-agent daemons.** Run these AS the agent's own account with its user bus, e.g. `sudo -u <account> XDG_RUNTIME_DIR=/run/user/$(id -u <account>) systemctl --user …` — `systemctl --user` / `tmux ls` from root or the wrong account silently hit a different (or empty) user manager.
- [ ] Each agent: `systemctl --user is-active <agent-bot-service>` returns `active`.
- [ ] Each agent: `tmux ls` shows the expected session with one window.
- [ ] Each agent: `tmux capture-pane -p -t <session>:0` (capture the FULL pane, not `| tail -5` — a fresh pane pads the bottom with blank lines) shows the daemon's frame. **Mind the auth state:** an *authenticated* claude shows the corner-framed prompt (`╭─` top edge + `❯` idle cursor) — but a daemon that hasn't completed the auth onboarding yet shows the **theme-picker or login-method screen** instead (no `╭` frame; the `❯` there is a menu cursor). On a fresh bringup the daemon is pre-auth, so this check confirms "the session is alive and showing a claude screen," not specifically the prompt frame — complete the auth onboarding (§3) before treating the prompt-frame form as required. (Codex shows a different frame — §12.6.)
- [ ] Kill an agent's tmux session (`tmux kill-session -t <session>`); the service auto-respawns within ~10 seconds (the `ExecStop=-` dash keeps the journal clean on each cycle).
- [ ] Each agent: `permissions.allow` in `~/.claude/settings.json` is populated (not empty). Claude Code never auto-writes a `permissions` key — this check FAILS until you set the allowlist per §7 (merge it in, don't overwrite the plugin keys). Check: `python3 -c "import json;print(json.load(open('/home/<account>/.claude/settings.json')).get('permissions',{}).get('allow','MISSING'))"`. **Field record: this is the most-missed check in the list** — the 2026-07-20 mnemosyne bringup skipped it AND the follow-up parity audit initially missed it too (caught by the operator, TG 3833); on any fresh account, run this one FIRST. A daemon can look fully healthy (bridge up, pane at prompt, chatting) while non-autonomous — the gap only shows when real tool-heavy work starts.
- [ ] Each agent: the allowlist covers the agent's **MCP tools**, not just the built-ins — chiefly the chat-surface plugin tools (`mcp__plugin_telegram_telegram__reply`/`react`/`edit_message`/`download_attachment`) and any org-transport MCP (`mcp__mailbox__*`). "Populated" is not "complete": the 2026-07-20 mnemosyne parity audit passed the populated check while every Telegram reply still prompted the operator's phone (caught by the operator, TG msg 34/36/41 — the reference set in §7 predated the MCP-tool entries). Verify by exercising the chat surface once and confirming no permission prompt reaches the operator.

**Recovery layer:**
- [ ] Recovery agent: `systemctl --user list-timers pantheon-*` shows all expected timers armed (count against the §6 role table — the recovery account has the most).
- [ ] OnFailure guard intact on EVERY account (T-1928): `grep -L "^OnFailure=" ~/.config/systemd/user/pantheon-*.service` hits only `Restart=` long-runners + `pantheon-unit-failure-alert@.service` itself, and that alert template IS installed. An account failing this check has silent liveness units — its keep-going/mailbox-wake/inbox-consumer failures never reach Recovery.
- [ ] `pantheon-restart-drain` is ARMED, not just present: `systemctl --user cat pantheon-restart-drain.service | grep PANTHEON_RESTART_DRAIN_ENABLED` shows the drop-in. **The journal "cycling cleanly" check is NOT sufficient** — an unarmed drain logs a clean "disabled" line and exits 0, looking identical to a working one (§6). Confirm the drop-in exists, THEN watch a real fire.
- [ ] Supervised first-fire: synthesize a synthetic tombstone (`restart_requested_<persona>.json` in `~/.local/state/pantheon/`) on one agent and watch one clean restart cycle land end-to-end with an operator observing. Requires §5 (cloned) + the drain armed above + the agent's `DRAIN_AGENT_CONFIG` key present (else the drain logs "unknown agent … skipping" and the tombstone re-accumulates). **Don't read tombstone-removal as success:** the drain takes the graceful path — it sends a handoff prompt to the pane and waits for `HANDOFF.md` to update before killing the service, which a *pre-auth* (onboarding-not-yet-completed) agent can't do. So a first-fire against a pre-auth/placeholder agent removes the tombstone but `NRestarts` does NOT increment — confirm the REAL restart with `systemctl --user show <your-service> -p NRestarts` against a logged-in agent, not by the tombstone vanishing. See [`adding-restart-capability-to-a-new-agent.md`](adding-restart-capability-to-a-new-agent.md) for the "if the first-fire isn't clean → disarm" protocol.

**Mailbox (if §7b provisioned) — verify BOTH directions as the new account's own uid** (field record 2026-07-20: a receive-only box reads as fully healthy until the agent's first reply fails):
- [ ] Receive: deliver a message to the agent's `new/`; it consumes + acks to `cur/`.
- [ ] Send: write-probe every sibling inbox `tmp/` as the account (`sudo -nu <account> touch <box>/tmp/.probe && rm`), then one real `mailbox.send()` from the account lands in a sibling's `new/`.
- [ ] Mailbox MCP: the python `mcp` package is installed for the account AND the mailbox tools actually surface in a live session (registration alone proves nothing — a missing dep fails silently at session start).
- [ ] ACP-flipped agents only: the first-live-turn canary includes a FILE-READING turn (`acp-bridge-pilot-runbook.md`), and `--prompt-timeout` is sized to the agent's real turn lengths (default 300s redelivers long working turns).

**Repo + dispatch:**
- [ ] Working tree on `main` in each clone, clean.
- [ ] File a test ticket via `tools/ticket.py`; the inbox-consumer picks it up within its cadence.

**Comms:**
- [ ] (If using a chat transport, plugin self-poll model) Plugin actually loaded — `~/.claude/channels/telegram/bot.pid` present + a `bun server.ts` poller running (NOT just the channel banner); `enabledPlugins["telegram@claude-plugins-official"]=true` (the full plugin ID — the short `[telegram]` form is not a valid key); `access.json` whitelist set.
- [ ] Operator sends a test DM → it surfaces as a channel-tagged inbound in the agent's session AND the agent replies via the reply tool (both directions; outbound alone is not enough — that's the failure mode that hid the disabled plugin).

**Audit chain:**
- [ ] The ledger lives under `ledger/` as weekly-sharded JSONL (`ledger/<ISO-year>-W<NN>/<same>.jsonl`); the schema is authoritative in `adr/ORG-001-ledger-format.md`. There is **no dedicated append CLI** — a ledger write is an append-to-shard + commit, the same path every Pantheon tool uses (`tools/ledger_shard.py` is the internal helper that resolves the current write-target shard; `current_write_target(ledger_dir, now)`). Bootstrap recipe:
  1. Find the current shard: `ledger/<ISO-year>-W<NN>/<ISO-year>-W<NN>.jsonl` (create the dir if it's a fresh week).
  2. Append one JSON line matching the ORG-001 schema — at minimum `id` (`<ISO-8601-UTC>Z-NNN`), `ts`, `agent`, `action`, `rationale`, a **non-empty** `justifies_via` array (empty is a P4 violation — escalate instead of writing the entry), and `parent_id` to chain the entry into the audit lineage. Copy the shape of the most recent existing entry.
  3. Commit + push the shard (wrap in `sg <shared-group> -c '...'` if the repo is group-write-shared). Push the shard to origin *before* any grooming pass so the INDEX picks it up.

If any of these don't pass, hold here and fix before continuing — the recovery layer running unattended on a half-installed substrate produces silent failures, not loud ones.

### 10. Generalizing to N agents

The 3-agent reference is the smallest configuration that exercises every primitive cleanly. To add a 4th (or to ship a different topology):

**Pick a role.** Pantheon's three are *orchestrator*, *specialist*, *recovery*. A 4th will be one of:
- A second specialist (a domain-specialized agent, e.g. an engineering-focused agent). Same shape as the reference specialist: own Unix user, own tmux daemon, own inbox-consumer cron, no cross-account sudo.
- A second orchestrator (rare). Same shape as the reference orchestrator.
- An emerging-harness daemon (e.g. the codex CLI pilot per ORG-018). Same supervise-script + systemd + tmux shape, but the inner process, the auth, the autonomy config, the chat surface, AND the recovery layer all differ — "substitute `CLAUDE_BIN`" is necessary but nowhere near sufficient. **See §12 for the full codex delta** (install/auth, supervise vars, the claude-coupled recovery layer, comms, and the §9 verification dispositions).

**Per-role checklist for each new agent:**
1. Unix account, optional shared-group membership.
2. Linger enabled.
3. Sudoers entries: usually none. Only the recovery account gets cross-account sudo. If you're adding a second recovery, grant it the same NOPASSWD pattern into the new specialists.
4. Per-account harness install + auth.
5. Per-account `pantheon-config` clone with its own deploy keys.
6. Supervise script + systemd user service from step 4. **Sub-orchestrator seats (tmux-spawned, no systemd session unit): spawn ONLY via `tools/seat_spawn.sh`** (`sudo -u suborchN /home/suborchN/pantheon-config/tools/seat_spawn.sh` — idempotent, seat-account-guarded, `SEAT_CLAUDE_ARGS`/`SEAT_SESSION` overridable). It scrubs the four `SUDO_*` vars BEFORE the tmux server forks — an inline `sudo … tmux new-session` bakes `SUDO_USER=claude` into the server and every future pane/tool-run inherits it, flipping `SUDO_USER`-sensitive behavior (the T-1974 finding; both original seats were spawned ad-hoc and carried it).
7. Per-agent timers from step 6: at minimum, `pantheon-inbox-consumer@<new>.timer` and `pantheon-restart-drain.timer`.
8. **Register the persona as a ticket target (REQUIRED before the agent can hold or file ANY ticket — easy to miss; found in the 2026-06-09 codex/the-ancestor bringup run).** `tools/ticket.py` validates every `target:`/`requester:` against `_valid_target_values()` = `AGENT_ROLE_MAP` keys ∪ values ∪ `{operator}`; an unregistered persona is **hard-rejected** (`unknown target 'X'; expected one of [...]`, T-0306) — `ticket.py file --target <new>` fails outright, so the agent has no inbox and no daemon can file work to it. Add the agent in **two code locations** (NOT just the doc in step 10):
   - `AGENT_ROLE_MAP` in `tools/agent_role_map.py` — add `"<persona>": "<audit-role-id>"` (use an identity entry `"<persona>": "<persona>"` if the agent has no distinct audit role-id; persona == account is permitted, §4). This is the map `ticket.py` actually gates on.
   - `instance.yaml` `roles:` stanza — add the role→persona binding (documentation-of-record per ORG-030 until Phase 1 wires it live; `agent_role_map.py` falls back to identity passthrough for any persona absent here, so this won't *block* ticketing, but keep it in sync).
   Then confirm: `python3 -c "from tools.ticket import _valid_target_values; print('<persona>' in _valid_target_values())"` → `True`, and `tools.test_ticket` + `tools.test_ticket_lint` stay green.
9. Add the agent's entry to `AGENT_CONFIG` in `tools/context_fill_monitor.py` and `DRAIN_AGENT_CONFIG` in `tools/restart_drain.py` (the per-persona dict shapes are in §6 — mind the `jsonl_dir` `/home/<acct>/.claude/projects/-home-<acct>` format; a wrong key fails SILENTLY). Add its verified-clean hook set to `HOOK_ALLOWLIST`. Keep the persona key consistent with `%i` (step 7), step 8's `AGENT_ROLE_MAP`/`instance.yaml`, and `sop/standard/agent-role-map.md` (step 11) — a mismatch breaks every per-persona timer. **(Codex daemon: do NOT arm these claude-coupled monitors — see §12.3.)**
10. Provision repo-documented venvs so the account can run the FULL test path — currently `tools/a2a/PROVISION.md` → `~/.venv-a2a` (a2a-sdk + uvicorn pins as documented there); check for others before calling the step done. Without them the suite reports `OK (skipped=N)` — a skipped-suite false green that looks like a pass (Metis caught and refused to report exactly this during T-1952, 2026-07-22; seat self-provisioning a PROVISION.md-documented venv is ruled NOT a new-dependency bubble-up — it adds nothing to the project). **Also user-site python deps for the suite itself (T-1934, 2026-07-23 — both suborch seats were missing all of these):** `pip install --user --break-system-packages pytest "mcp==1.27.1"` (three test modules import pytest; the mailbox-MCP harness is written against the 1.27 response shape — keep the pin aligned across accounts until the harness handles 1.28+), and **confirm §5's global git identity actually got set** (`git config --global user.email` non-empty — the ticket_state_sweep e2e commits in synthetic repos and fails without it; the 2026-07-20 seat bringup run missed this step despite it being documented). **Running the suite as another account via sudo: strip the sudo markers** (`sudo -u <acct> env -u SUDO_USER -u SUDO_UID -u SUDO_GID ...`) — at least one wrapper (s3star, T-1884) code-enforces test-stub inertness whenever `SUDO_USER` is present, so a sudo-driven run manufactures false REDs the account's own runs don't have. **This binds the seats' OWN differential-baseline runs too (Metis, 2026-07-23): seat sessions are sudo-launched, so their interactive environment INHERITS `SUDO_USER=claude` by default — the sanctioned baseline is measured with the markers stripped (`env -u SUDO_USER -u SUDO_UID -u SUDO_GID -u SUDO_COMMAND python3 -m unittest …`), otherwise a seat measures 3 phantom s3star failures and reads them as regressions.**
11. Run the verification checklist (§9) for the new agent.
12. Document in `sop/standard/agent-role-map.md` (per ORG-011).

**What stays the same as N grows:** the supervise pattern, the asymmetric trust shape (recovery is asymmetric, not flat), the autonomy bar (D3), the ledger format (ORG-001), the ticket lifecycle (ORG-003).

**What scales with N:** the per-agent timers, the `HOOK_ALLOWLIST` entry per agent, and one own-bot + plugin config per agent that needs a chat surface (§7 — per-agent self-poll, no shared routing).

**What you should NOT do:** create a flat-fleet design where every agent monitors every other. The asymmetric recovery model is structural — additional recovery-fallbacks should be additive (one Recovery + one Recovery-prime, each with their own sub-fleet), not flat. P5 (roles don't hop) constrains this.

### 11. Fresh-instance: what to clear vs. keep

This repo is the **reference instance's live state** — the operator's Pantheon, history and all. We do **not** ship a pre-cleaned seed (Pantheon is in-house). If you clone it to stand up your own, you decide what to carry as seed reference vs. clear to start your own history. The split:

**Distribution posture (ratified [ORG-029](../../adr/ORG-029-pantheon-distribution-model.md)).** This one-time keep/clear split is also the *ongoing* relationship between a clone and upstream. Treat upstream `pantheon-config` as **pull-only reference scaffolding** (the KEEP set below); grow your **own microcosm** (the CLEAR set — ledger/tickets/notes/incidents/episodic) in a fork that **never pushes upstream**. Reconcile periodically by a *path-scoped* pull of the scaffolding only — `git checkout upstream/main -- rulebook/ sop/ adr/ tools/ tests/ skills/ .githooks/` — which leaves your history subtree untouched (KEEP and CLEAR are path-disjoint, so there is no content-level conflict). The intra-instance git gotchas (branch-protection, multi-clone divergence, `sg pantheon-shared`) are NOT part of this — they exist only because the operator's N agents share one repo as a write-target; a cross-operator fork has none of that.

**KEEP — the durable scaffolding (this is "Pantheon" the system):**
- `rulebook/` (principles, directives, policies), `sop/`, `adr/` — the governance + methodology. Edit content to taste; the *structure* is the product.
- `tools/`, `tests/`, `skills/`, `.githooks/` — the operational tooling + its tests + the guards.
- Every directory's `README.md` spine, `knowledge/pantheon-glossary.md`, `knowledge/*/README.md`, `watch/topics.md`, `pinned-rules/` (structure; rewrite the per-agent content), `incidents/TEMPLATE.md`, `tickets/` README/TEMPLATE.

**CLEAR / EMPTY — instance-specific runtime history (safe to reset on a fresh instance; keep each folder's README/TEMPLATE):**
- `tickets/{inbox,active,done}/` — your work history; start empty.
- `ledger/<ISO-year>-W<NN>/` shards — the reference instance's audit chain; start a fresh chain.
- `incidents/`, `notes/`, `reports/`, `screenshots/`, `finances/`, `archive/` — instance-specific postmortems, scratch, writeups, captures, revenue-project state, retired artifacts.
- `knowledge/episodic/<dated-dirs>/` and `knowledge/watch/reports/<date>/` — the reference instance's research arcs + cadence reports (keep the tree READMEs + `watch/topics.md`).

**OPERATOR JUDGMENT — portable knowledge you may keep as seed OR clear:**
- `bugs/` and `lessons/` — much of this is generic Claude-Code / systemd / language-runtime knowledge useful to any operator (good seed); some is instance-specific. Keep the generic, clear the rest.

**NOT in the repo — per-account runtime state (recreated by the bringup, never committed):**
- `~/.local/state/pantheon/` (cap-hit state, restart tombstones, monitor state), `~/.claude/` (auth/session/plugin state), `~/.claude/channels/` (your own tokens + access.json), `~/HANDOFF.md`. **For a codex agent:** `~/.codex/` (auth.json, config.toml, session SQLite DBs) is the analog of `~/.claude/` — regenerate it via `codex login` (or a pre-placed `auth.json` + `config.toml`); never copy it from another account.
- ⚠️ **`~/.claude/settings.json` is the exception — it is NOT auto-regenerated.** Completing the auth onboarding restores auth/session state, but it does NOT restore `permissions.allow` or `enabledPlugins`. Those are operator-configured (§7) and must be re-applied by hand on every fresh account, or the agent comes up non-autonomous (wedges on every tool call) and with no chat surface. "Recreated by the bringup" means the auth/session/cache — the settings file is yours to write.

Rule of thumb: **scaffolding + methodology = keep; dated/instance history = clear; per-account secrets + runtime state = never yours to copy, regenerate them.**

#### Fork conventions — FORK.md + a fork-ADR namespace (blessed 2026-07-27, T-2073)

These three conventions were invented independently by the first outside fork (wish-factory) and are now shipped defaults, because every fork hits the same problem: *where do I record what MY fork changed, so a reconcile pull doesn't erase it?*

- **`FORK.md` at the repo root is your fork's delta-index.** Copy it from the shipped `FORK.md.template`. It lists what your fork added, which KEEP-path files you customized (the reconcile hotspots to re-apply after a pull), your rebrand record, and your fork-ADR index. Critically, **`FORK.md` lives OUTSIDE every KEEP path AND is never named in the reconcile pathspec** (see the root-files note below), so a path-scoped reconcile (`git checkout upstream/main -- rulebook/ sop/ adr/ …`) never touches it — it is fork-owned and durable. It is a MANIFEST-CLASS artifact (like `instance.yaml`): upstream ships the *template* shape, your filled-in `FORK.md` is instance data that stays local.
- **README.md is upstream scaffolding — do NOT put instance notes in it.** `README.md` is a top-level canonical doc whose structure is upstream's, and upstream's own copy carries upstream's instance specifics (names, URLs). It is *not* fork-owned space: leave it unreconciled and it drifts from upstream; reconcile it (root docs are pulled by explicit name — see below) and you re-import upstream's content, specifics and all, over anything fork-specific you wrote. Either way, fork notes there are lost or wrong. Put "how THIS fork differs" in `FORK.md`, not `README.md`. (The directory `README.md` spines *are* inside KEEP dirs, so a dir-scoped reconcile overwrites them outright — all the more reason not to use them for fork notes.)
- **Author your fork's own ADRs under a fork-private prefix, not `ORG-NNN`.** Upstream owns the `ORG-NNN` namespace; a fork that files `ORG-050` will collide the day upstream authors its own `ORG-050`. Pick a short prefix from your fork's name/initials — the wish-factory fork used **`COY-NNN`** — and file them in `adr/` alongside the upstream ORG ADRs. They coexist safely: `adr/` is a KEEP path, but the checkout-based reconcile only overwrites files that *exist upstream*, so your `<prefix>-NNN-*.md` files are additive and never deleted by a pull (verified: `git checkout upstream/main -- adr/` pulls upstream's new/changed ADRs and leaves fork-only files in place). Index them in `FORK.md` (the `adr/README.md` ADR-index autogen block is upstream-owned and gets overwritten on reconcile, so it is not a durable home for your fork's ADR list). This mirrors the existing project-ADR namespacing rule in `adr/README.md` (echo-prismatic/pantheon-pwa carry their own ranges) — a fork is just another namespace.

**Root-level files reconcile by explicit name, not by the dir-scoped recipe.** The canonical reconcile command (`git checkout upstream/main -- rulebook/ sop/ adr/ tools/ tests/ skills/ .githooks/`) pulls the KEEP *directories*. Root-level upstream-owned files — `README.md`, `BACKLOG.md`, `FORK.md.template`, `instance.yaml.example` — are NOT in that pathspec, so the standard reconcile does not touch them; name them explicitly in the checkout if you want their upstream updates (e.g. `git checkout upstream/main -- FORK.md.template` to refresh the template shape). Your own `FORK.md` is deliberately never named — that omission is exactly what makes it durable across reconciles. (This is the KEEP-set/recipe sync ORG-029 Implementation step 3 flags: the conceptual KEEP set includes root scaffolding, but the dir-scoped recipe reaches directories only.)

**Harness-config + the 3-repo bundle (governed by [ORG-039](../../adr/ORG-039-harness-config-distribution-model.md), ratified 2026-06-30).** `pantheon-config` is only ONE of the four layers a runnable instance needs (§"What you're building"). The §11 keep/clear posture above is the *framework/content* layer; the **agent-runtime** layer is a separate, per-harness config repo — `claude-code-config` (the claude-code harness; `codex-config` is a fast-follow). It carries its own FRAMEWORK/INSTANCE distribution model (the layer-2 analog of ORG-029): role-keyed `agents/` skeletons + the `pantheon-org-discipline` skill + `settings.json`/MCP templates + the `pinned-rules/` STRUCTURE **port**; persona-brief *content*, tokens, account names, and absolute paths **stay**. A fourth layer — **host/runtime provisioning** (`claude-code-config/provisioning/`) — makes the bundle bootable (users/dirs/perms, service/launch wiring, env/secret placeholders, a per-role boot smoke).

The portable bringup is a **3-repo drop-in** into one agent home, bound by a single `instance.yaml`:
1. clone `pantheon-config` (framework, this repo), `claude-code-config` (agent-runtime), and optionally `kino` (control surface);
2. author one `instance.yaml` in `claude-code-config` (role→persona/account/path binding — the only hand-authored file);
3. fill the role brief(s) from the `pinned-rules/*.md.example` skeletons;
4. `claude-code-config/provisioning/provision.sh` then `install.sh --role <role>`;
5. **release gates before relying on it:** `provisioning/leak-scan.sh` (no secrets/names/host-paths in the shippable set) and `provisioning/boot-smoke.sh <role>` (the role's agent boots from the template alone).

The `pantheon-config` ↔ harness-config coupling (the `pantheon-org-discipline` skill + the `directive_skill_drift_check.py` guard) is **decoupled for a fork** (ORG-039 §4): a clone without a harness-config repo configured gets a clean, actionable SKIP from the drift check — never a broken hook — and strict validation runs only when the harness skill is actually present.

### 12. Codex-daemon variant (ORG-018 pilot)

This section is the consolidated delta for standing up a **codex-CLI daemon** instead of a Claude Code agent. It was written from the T-0762 eval — an actual from-scratch codex bringup run against every section above, inside a rootless-podman container. **The supervise/tmux/systemd skeleton (§4) and the substrate (§1, §2) transfer; the harness internals (auth, config, autonomy, chat transport, and the entire recovery layer) do not.** Read the body of the SOP for the shape; read this for what changes.

Reference surface: `codex-cli` ≈ 0.137.0, npm package `@openai/codex`, config root `~/.codex/`.

#### 12.1 Install + auth (replaces §3)
- Install: `npm install -g @openai/codex` (per-account, same npm-global prefix as §3). Binary lands at `~/.npm-global/bin/codex`.
- Auth: `printenv OPENAI_API_KEY | codex login --with-api-key` for the headless daemon case. `codex login --device-auth` exists but **blocks on a browser** — never in a systemd unit. Auth lands in `~/.codex/auth.json` (0600).
- Isolation: each account its own `~/.codex/`; never shared. No `bun` needed (codex's chat surface doesn't use it).
- **Stability gotcha:** a codex started with **no** auth idles safely on its login menu (session stays alive). A codex started with a **bad/expired** key exits in ~300ms, so the supervise loop crash-loops (`NRestarts` climbs continuously) while `systemctl is-active` still reports `active` — a false-positive healthy. Pre-place valid auth before enabling the service, and check `NRestarts` is stable, not just that the unit is active.

#### 12.2 Supervise script + autonomy (modifies §4, §7-autonomy)
- `CLAUDE_BIN="$HOME/.npm-global/bin/codex"`, `CLAUDE_ARGS=""`. No `--channels` (codex errors on it). Bare `codex` = interactive TUI = correct inner process for the session-alive loop. The kill→respawn behavior (§9) was verified working unchanged for codex (~7–8s respawn).
- **Autonomy** is the codex analog of claude's `permissions.allow`: set in `~/.codex/config.toml`, not `~/.claude/settings.json`. The codex default (`approval_policy`/approval = `OnRequest`/`unless-allow-listed`, sandbox restricted) **will wedge an unattended daemon on approval prompts.** Set, scoped to the agent's trust tier:
  ```toml
  approval_policy = "never"
  sandbox_permissions = ["disk-full-read-access", "network"]
  ```
  Verify with `codex doctor` (the `approval policy` line should not be the default gated value).
- **Ubuntu 24.04+ sandbox gotcha — bwrap userns (HOST prereq, not a codex config):** codex sandboxes every model-generated shell command via **bubblewrap** (`bwrap`), which needs an unprivileged **user namespace**. Ubuntu 24.04 ships `kernel.apparmor_restrict_unprivileged_userns=1` (AppArmor hardening) that **blocks** it — so EVERY codex shell command dies instantly with `bwrap: setting up uid map: Permission denied`, before anything runs (even `pwd`). Deceptive symptom: the daemon looks healthy (session up, can spawn subagents via its non-shell multi-agent tools) but does **zero** file/git/ticket/generation work. Fix (most-secure — exempts only bwrap, keeps the hardening for every other binary), run as root:
  ```
  sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF'
  abi <abi/4.0>,
  include <tunables/global>
  profile bwrap /usr/bin/bwrap flags=(unconfined) {
    userns,
    include if exists <local/bwrap>
  }
  EOF
  sudo apparmor_parser -r /etc/apparmor.d/bwrap
  ```
  Verify: `sudo -u <codex-user> bwrap --ro-bind / / /bin/true` exits 0 (was the uid-map error). Blunter alternative (drops the hardening fleet-wide): `sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` + persist in `/etc/sysctl.d/`. Confirmed live 2026-06-09 (T-1026): codex was fully blocked until the profile landed. codex 0.136 has no Landlock-only (non-userns) sandbox mode, so the only codex-side workaround is `--dangerously-bypass-approvals-and-sandbox` (fully unsandboxed) — avoid; fix the host instead.

#### 12.3 The recovery layer is Claude-Code-coupled — DO NOT arm it unmodified (modifies §6)

This is the highest-severity finding: the §6 recovery monitors are **inoperative or actively wrong** against codex, because they parse Claude Code's session format and TUI:
- `context_fill_monitor.py` reads `~/.claude/projects/**/*.jsonl` and Anthropic `message.usage` token counts. Codex stores sessions in `~/.codex/*.sqlite` (no JSONL, no `context_window` field). The monitor finds nothing and **bails silently every cycle** — zero context-fill signal.
- `restart_drain.py` / `handoff_and_restart.sh` are coupled at ≥5 points: `CLAUDE_PROCESS_PATTERN`, the pane-sentinel verify regex (`╭─+╮` / idle `❯`), `HANDOFF.md`, and claude-session kick prompts. The codex TUI has none of those markers (its frame is a "Welcome to Codex" menu, not a corner-framed prompt), so the drain's pane-sentinel step times out and **fails on every tombstone**.
- `inbox_consumer.py` can still `send-keys` a ticket into the codex pane, but its pre-injection idle sentinels (`"? for shortcuts"`, `❯`) never match a codex frame, so it logs `tmux_unreachable` / wedges each cycle.

**Disposition:** for a codex daemon, do NOT arm `context-fill-monitor` or `restart-drain` as-is — they no-op or fail silently, which is worse than absent. The minimum viable recovery is the bare supervise loop (kill→respawn, which works). A real codex-compatible recovery layer (SQLite-based fill detection, codex pane sentinels, a codex resume path — note `codex resume --last` is a *subcommand*, not the `claude --resume` *flag* the `ho-resumer` uses) is **engineering work beyond this SOP** — tracked as a follow-up, not solved here.

**Decision (T-0822, Recovery / recovery domain, 2026-06-05):** the bare supervise loop IS the codex recovery posture — the full codex-compatible recovery layer is **deliberately deferred, not just unbuilt.** Rationale (P1 / cost-discipline): codex daemons are an unvalidated ORG-018 pilot; building SQLite fill-detection + codex pane sentinels + a codex resume path + decoupling restart_drain's ≥5 claude-isms is substantial standing-infra that should not be built for a runtime that may not be adopted. The bare respawn loop (systemd `Restart=always` kill→respawn) is sufficient for a pilot daemon — it loses graceful context-fill restarts / wedge-detection / graceful-handoff, but a pilot daemon doesn't carry load that justifies them. **Revisit trigger:** a codex daemon graduates from pilot to *load-bearing* (adopted for ongoing standing work, per the T-0762 / ORG-018 evaluation) → ticket + build the layer then. Until then, when standing up a codex daemon: skip arming the claude monitors entirely (per the Disposition above); the supervise unit's `Restart=always` is the whole recovery story.

#### 12.4 Chat transport (replaces §7)
- **Inbound:** codex has no Telegram plugin (its marketplace ships slack/teams/gmail/etc., no telegram) and no `--channels`. A codex daemon *can* get inbound Telegram via an **MCP server** (`codex mcp add telegram-bot --env TELEGRAM_BOT_TOKEN=<tok> -- <server-cmd>`), surfacing messages as MCP tool input — architecturally viable but **you supply the MCP server; there is no turnkey one.** Access control then lives in that server, not in an `access.json`.
- **Outbound:** the §7 outbound-only `sendMessage` direct-curl pattern transfers as-is (it's just curl) — store the bot token at `~/.codex/pantheon.env` (0600) or any operator-chosen path, **not** `~/.claude/channels/...`.
- Honest current state: **outbound status pings work today; inbound operator chat for codex is not turnkey** (needs the MCP server above).

#### 12.5 Secrets (replaces the §8 table rows for a codex account)

| File | Owner | Mode | Holds |
|---|---|---|---|
| `~/.codex/auth.json` | per-account | 0600 | API key or ChatGPT OAuth session (`codex login`) — replaces `~/.claude/` OAuth |
| `~/.codex/config.toml` | per-account | 0644 | model, `approval_policy`, `sandbox_permissions`, MCP registrations — replaces `~/.claude/settings.json` + `enabledPlugins` |
| `~/.codex/pantheon.env` (or chosen path) | per-account | 0600 | `TELEGRAM_BOT_TOKEN` for outbound direct-curl |
| `/home/<orchestrator>/root.env` | orchestrator | 0600 | sudo password — transfers unchanged |
| Per-repo deploy keys | per-account | 0600 | private key — transfers unchanged |

The claude rows (`~/.claude/channels/telegram/.env`, `access.json`, the historical `telegram-bridge/.env`, `~/.claude/`) **do not exist on a codex account** — omit them.

#### 12.6 §9 verification — codex disposition

| §9 check | Codex disposition |
|---|---|
| 3 users exist / asymmetric sudo chain | **N-A** for a single-daemon pilot (one account, no recovery pair) |
| linger listed + `user@<uid>` active | **TRANSFERS** (note the §2.3 container caveat) |
| `systemctl --user is-active <svc>` / `tmux ls` / kill→respawn | **TRANSFERS** (verified working for codex) |
| capture-pane shows "Claude Code prompt frame" | **REWORD** → codex shows its `Welcome to Codex`/chat-prompt TUI, no corner frame. Check for a known codex string, not the claude border. (`tail -5` is unreliable — codex pads the bottom with blank lines; grep the full capture.) |
| `permissions.allow` in `~/.claude/settings.json` populated | **REWORD** → `approval_policy` in `~/.codex/config.toml` is set (not the gated default); confirm via `codex doctor` |
| `bot.pid` + `bun server.ts` poller / `enabledPlugins` / DM round-trip | **N-A** (no codex telegram plugin) — substitute the §12.4 outbound-curl check, or mark N/A if the agent needs no chat surface |
| `list-timers pantheon-*` / restart-drain journal / context-fill first-fire | **REWORD/skip** → the recovery layer is claude-coupled (§12.3); verify instead that `NRestarts` increments+settles on a manual kill, and do NOT treat the unarmed/inoperative monitors as "passing" |
| repo on `main` / ledger weekly-sharded JSONL | **TRANSFERS** (repo + ledger are harness-agnostic) |

#### 12.7 Ticketing — how a codex daemon joins the queue (added from the 2026-06-09 codex bringup run)

A codex daemon can participate in the ticket queue, but the path differs from a claude agent at three points:

- **Persona registration is identical and mandatory** — do §10 step 8 (register `codex` in `AGENT_ROLE_MAP` + `instance.yaml`) or `ticket.py` hard-rejects `target: codex`. This is harness-agnostic; codex is not special here.
- **Read + file work today.** A codex account with a `pantheon-config` working-tree clone and system `python3` + `PyYAML` runs `tools/ticket.py` unchanged — it can `list`/`show` its inbox and `file` new tickets (commits land locally). Verified 2026-06-09: codex saw `ticket.py list --target codex` and filed cleanly. No bun/claude needed for the ticket CLI.
- **Auto-DELIVERY into the codex session does NOT work** — same root cause as §12.3: `inbox_consumer.py`'s pre-injection idle sentinels (`"? for shortcuts"`, `❯`) never match the codex TUI frame, so `pantheon-inbox-consumer@codex.timer` would `tmux_unreachable`/wedge rather than inject the ticket. **Disposition:** do not arm `pantheon-inbox-consumer@codex.timer` as-is. A codex daemon picks up tickets by a **manual send-keys kick** (or the deferred §12.3 codex-pane-sentinel rework), not auto-delivery. Treat codex's inbox as pull-by-kick, not push.
- **Push/pull needs a credential codex won't have at bringup.** A fresh codex account has no SSH deploy key and no git token (`ssh -T git@github.com` → `Permission denied (publickey)`), so it can `commit` locally but cannot `push`/`pull` its ticket writes. Two options: (a) generate a per-account deploy key and upload it to the repo host — the **human-gated** half, same as §5; or (b) interim **recovery-agent sync stopgap** — the recovery account (which has cross-account sudo + a working push identity) periodically fetches the codex clone's commits and pushes them upstream. Seed the initial clone via `git bundle` from an existing clone when the codex account can't yet reach the remote (avoids the §5 local-path `safe.directory` dance and needs no cross-home read).

## Common failure modes

- **First-fire wedges on a boot dialog.** The fresh Claude Code session can show a settings/permissions dialog that blocks the kick prompt. See `adding-restart-capability-to-a-new-agent.md` §"Common failure modes" — the `RECOVERY_AUTO_RESOLVE_ALLOWLIST` mechanism handles known dialogs; new dialogs need triage. Disarm the drain until the blocker is fixed.
- **Wrong session name in monitor configs.** Silently breaks `capture-pane` and `send-keys`. Verify session names match across the supervise script, the unit's `ExecStop`, and the `restart_target_pane` entries in `tools/context_fill_monitor.py` + `tools/restart_drain.py`. If the recovery layer is running but the drain journal shows steady-state timeouts, this is the first thing to check.
- **Cadence collision on auto-push.** Two daily timers at the same minute will race git push. Preserve the offsets in the reference cadence.
- **Multi-clone push divergence is the steady state, not an error.** With N agents each auto-committing to `main` from their own clone, `origin/main` moves under you constantly — a plain `git push` will be rejected (non-fast-forward) routinely. Every auto-commit path MUST `git pull --ff-only` (or fetch + rebase, preserving other agents' rows) *before* pushing, and re-try on rejection. If you write your own auto-commit tooling from this SOP, build that in from the start; a push path that doesn't pull-first works in a single-agent test and then fails constantly once a second agent is live. (The cadence offsets above reduce the collision *rate*; they don't remove the need to pull-before-push.)
- **Headless `claude -p` billing (dated).** Any cron that shells out to headless `claude -p` (in the reference toolset, `tools/weekly_ticket_summary.py`) is affected by the 2026-06-13 Anthropic billing change for headless `-p` (unrelated to the interactive daemons). If you adopt those crons, budget for it or gate them.
- **Forgetting `sg <shared-group>` wrap.** User-mode systemd cannot grant supplementary groups; anything that needs the shared group has to be `sg`-wrapped in the supervise script. Symptom: git operations against the shared clone fail with permission errors.
- **Stale audit-chain references after a rename.** If you decide to rename a session or service name *after* the audit chain has accumulated references, the rename invalidates every prior ledger entry, ticket transition log, and commit message that mentions the old name. The convention is: pick names you can live with at install time, then keep them stable.

## Boundary

Covers the one-time bringup procedure. Out of scope:
- Day-to-day operations (see SOP index).
- Failure-mode discipline (see `sop/remediation/`).
- Adding individual capabilities to an existing agent (see `adding-restart-capability-to-a-new-agent.md`).
- The full ledger schema (see `adr/ORG-001-ledger-format.md`); the §9 audit-chain step gives the bootstrap append recipe.
- A scripted `bringup.sh` (tracked under T-0753).

## Cross-references

- [`../../knowledge/pantheon-glossary.md`](../../knowledge/pantheon-glossary.md) — daemon names, account paths, historical vocab.
- [`../../reports/PR/primitives.md`](../../reports/PR/primitives.md) — the architectural primitives this procedure builds.
- [`../../reports/PR/running-unattended.md`](../../reports/PR/running-unattended.md) — the unattended-operation narrative this procedure enables.
- [`../../reports/PR/decision-design.md`](../../reports/PR/decision-design.md) — the governance layer this procedure runs under.
- [`adding-restart-capability-to-a-new-agent.md`](adding-restart-capability-to-a-new-agent.md) — the 5-capability self-heal stack + the supervise-script gotchas.
- [`handoff-restart-runbook.md`](handoff-restart-runbook.md) — the restart wrapper's operator surface.
- [`cross-account-ops-discipline.md`](cross-account-ops-discipline.md) — the recovery agent's cross-account patterns.
- [`agent-role-map.md`](agent-role-map.md) — single source of truth for the persona↔Unix-user mapping.
- [`../../tools/systemd/README.md`](../../tools/systemd/README.md) — canonical per-unit install procedure.
- ORG topology ADRs: ORG-001 (ledger), ORG-002 (principles/directives), ORG-003 (tickets), ORG-004 (bridge), ORG-010 (three-tier policies), ORG-011 (stays-working-under-silence), ORG-012 (information-inflow bubble-up methodology — the knowledge-sweep), ORG-017 (knowledge tree), ORG-018 (codex pilot), ORG-020 (self-triggered restart), ORG-025 (Specialist orchestration model), ORG-026 (single-host file-mailbox), ORG-029 (distribution posture), ORG-035 (memory-portability per-agent baseline).
- Primary substrate snapshot: `notes/2026-06-03-bringup-substrate-ground-truth.md` (sanitized live-substrate ground-truth; the source material this SOP synthesizes).

— Specialist (specialist), drafted 2026-06-03 for T-0751 revise pass per the operator feedback (less prescriptive on surface details + move to SOPs + glossary in knowledge/).
