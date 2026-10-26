# MCP adoption + allowlist policy

**Status:** Ratified 2026-10-07 by the operator (Telegram 11297/11299, T-3276 ticket walk: "looks stringent. good for mcp, external servers aren't to be blindly trusted" / "it reads well enough, i say it'll work"). It was ratified together with the `sticker` allowlist row (§2) and yes to both §8 questions: build the tripwire as a follow-up, and the policy covers the codex harness (§0). Previously: PROPOSED, HO-landed 2026-08-26 from Specialist's draft at commit `9637847cc` (T-3137). **Author:** Specialist (draft) → Orchestrator (Orchestrator, landed under D2 policy-authorship). **Date:** 2026-08-26 **Re-evaluate when:** the next `mcp-supply-chain` watch report flags a new malware namespace; OR any seat gains a network-transport MCP server; OR any allowlisted server changes version; OR the codex harness's inactive plugin-marketplace templates (T-3171) are activated; OR the follow-up tripwire proposed in §8 lands, shifting §2's enforcement from procedural to structural. **Justifies via:** P1 (architecture over policy — the gate is structural: an unlisted server cannot be adopted, not "remember to audit"), D3 (autonomy bar — MCP *adoption* is goal-level/reserved; this policy is the below-goal methodology that governs the reserved decision when it arises), D11 (third-party surface verification — read the source, validate against the real transport), P4 (traceability — every allowlist entry carries its audit trail).

**Ticket:** T-3137 (target=specialist, resolved 2026-08-26) → T-3275 (target=orchestrator, this landing). **Driver:** `knowledge/watch/reports/2026-08-24/mcp-supply-chain.md` (§Follow-ups: "Establish MCP allowlist policy — require (1) security audit of source, (2) sandboxed execution / no STDIO passthrough, (3) OAuth/auth enforcement, (4) monitoring of tool invocations"). **Prior art (done, clean):** T-3135 + T-3136 + T-3171 — fleet-wide MCP-exposure audit; posture CONFIRMED structurally-minimal, zero exposure to the six named Aug-2026 threats, zero supply-chain compromise. This policy makes that already-true posture a *standing gate* so it stays true.

---

## 0. Scope + what this policy is NOT

**This policy governs:** the criteria and process under which any new MCP server (or MCP-distributed tool) may be added to any Pantheon seat's config. **It covers the codex harness too** (`~/.codex/config.toml` MCP entries, and enabling any of the inactive plugin-marketplace templates under `~/.codex/.tmp/plugins/`), as ratified 2026-10-07.

**This policy does NOT authorize adopting MCP.** Per the watch report's own framing and the trust-posture floor, *the decision to adopt any new MCP server is a reserved (goal-level) / account-and-access-grant action — the operator's ratification, not an orchestrator's.* This policy is the **gate the reserved decision passes through**, not a delegation of it. It exists so that when adoption IS considered, the answer to "what must be true first" is written down and structural, rather than reconstructed under time pressure against a live threat.

The distinction (act-vs-park): *writing this policy* is a reversible below-goal artifact (a proposal that can be ratified or discarded — deferred judgment). *Adding an MCP server* is an account-and-access grant with a supply-chain blast radius — reserved, hold-and-ping. The two must not be conflated.

---

## 1. The standing posture (the default this policy defends)

Pantheon's MCP posture as of 2026-08-26 (T-3171 fleet sweep): **every active MCP server fleet-wide is STDIO-transport with a FIXED, internally-controlled command, no bound network port, no external/remote config ingestion.** The three live classes:

- `mailbox` (ORG-026 hub) — stdio, per-project, 5 seats
- `pantheon-mcp-codex` (orchestrator only) — stdio, org-internal codex second-opinion wrapper
- `telegram` (via official first-party `claude-plugins-official` plugin) — outbound getUpdates poll, no listener

This posture is **structurally minimal by design**, and the April-2026 STDIO-RCE design-default class DOES NOT APPLY to it (that class requires the client to accept *external/attacker-settable* config; ours are fixed internal commands). The policy's job is to keep any *new* server from breaking that invariant.

## 2. The allowlist (structural gate — P1)

**No MCP server runs on any Pantheon seat unless it is on the allowlist below.** The allowlist is the enumeration of vetted servers; absence = not permitted. This is the structural form: the question "may I run server X" is answered by set-membership, not by per-adoption judgment.

| Server | Seats | Transport | Command / source | Vetted |
|---|---|---|---|---|
| `mailbox` | orchestrator, specialist, archivist, mason, artisan | stdio | `python3 tools/mailbox_mcp.py` (org-internal) | ✓ standing |
| `pantheon-mcp-codex` | orchestrator | stdio | org-internal codex wrapper | ✓ standing |
| `telegram` | every Telegram-connected Claude seat (orchestrator, specialist, recovery, archivist), each with its own bot | outbound poll, no listener | official `claude-plugins-official` plugin, which bundles its MCP server and installs into each seat's `~/.claude/plugins/cache` | ✓ standing |
| `sticker` | orchestrator, specialist, recovery, archivist | stdio | `python3 tools/sticker_mcp.py` (org-internal, T-1024 #734) | ✓ ratified 2026-10-07. Audit: org-internal source with no new deps (the `mcp` SDK already used by `mailbox`); fixed command; outbound sendSticker only, never getUpdates; the token comes from the seat's existing telegram `.env` and is never returned; codex-reviewed (R1 REBOUND fixed, R2 APPROVE). |

Anything not on this table is **not adopted**. Adding a row is the reserved action gated by §3–§6.

## 3. Requirement 1 — Security audit of source (D11)

Before a server is added to the allowlist:
- **Read the actual source** of the server + its full dependency tree (not the README, not the marketing). D11: validate against the real transport + the real command it will exec.
- **Namespace malware sweep**: reject anything under the active malware vectors named in the driving watch report — `@copilot-mcp/*`, `mcp-server-*` (impersonation namespace), `*-mcp` matching a known malicious prefix (`postmark-mcp`, `mcp-remote` unpatched, `@copilot-mcp/apex`, the Oura-clone forks). Re-pull the current malware list from the latest `mcp-supply-chain` watch report at audit time — the list grows.
- **No install/post-install hooks**: reject any package with `preinstall`/`postinstall`/`install` lifecycle scripts that execute non-trivial code (the Aug-2026 npm-worm + Postmark-rugpull vector). Benign build-tool fixtures + the official `@anthropic-ai/claude-code` postinstall are the only standing exceptions.
- **Pin + lockfile**: the exact version is pinned and lockfile-committed; "latest"/floating is rejected (the rugpull class ships a malicious *point* release over a trusted name).
- **Provenance**: first-party (Anthropic/official) or a source whose maintainer chain is auditable. "Popular" is explicitly NOT a proxy for safe — the Canopii Aug-2026 index shows 100k+/mo-download servers scoring *worse* (79/100) than niche tools (86/100).

## 4. Requirement 2 — Sandboxed execution / no STDIO passthrough

- **No external config ingestion.** The server's `command` field must be fixed and internally controlled. A server whose command/args are settable from external/remote/attacker-influenceable input is rejected outright — that is the exact April-2026 STDIO-RCE design default Anthropic declined to fix; remediation is ours, at adoption time.
- **Transport preference**: stdio with a fixed command (our standing posture) over any network-transport server. A network-transport MCP server (HTTP/SSE, any bound port) is **above-bar by default** and requires explicit the operator ratification even if it clears §3 — it breaks the no-listener invariant and re-opens the CVE-2026-23744-class (`0.0.0.0` default-bind remote-exploit) surface.
- **Least-filesystem**: if the server takes a filesystem scope (the Filesystem/Git MCP class — GHSA path-traversal / symlink-bypass / `git_add`-outside-repo advisories), scope it to the minimum path set and verify no symlink escape. Prefer not adopting the Git/Filesystem MCP class at all given its patch history (most-patched in the ecosystem).

## 5. Requirement 3 — OAuth / auth enforcement

- Any server that exposes *any* authenticated surface must **enforce** auth, not merely declare it (the Aug-2026 index: 37% declare no auth, 2% claim-but-don't-enforce, 53% hard-coded static secrets). OAuth per the MCP spec recommendation where the server supports it.
- **No hard-coded static secrets** in the server config or source. Secrets come from the seat's existing secret-management path, never inline.
- For our stdio-internal servers this is trivially satisfied (no network auth surface); the requirement bites on any future network-transport adoption, which is reserved anyway (§4).

## 6. Requirement 4 — Monitoring of tool invocations

- Any adopted server's tool invocations must be **observable** — the seat's existing logging must capture MCP tool calls, so a poisoned tool description or an anomalous invocation pattern (tool-poisoning / shadow-MCP class) is detectable after the fact.
- **Tool-description review at adoption + on update**: the tool-poisoning class injects adversarial instructions into tool *descriptions* (WhatsApp/GitHub-issue prompt-injection precedent). Review descriptions at adoption and re-review on any version bump (a rugpull is a *version* event).
- **Re-audit trigger** (matches T-3171's close-out trigger): re-run the fleet sweep when the next `mcp-supply-chain` watch report flags a new malware namespace, OR any seat gains a network-transport MCP server, OR any allowlisted server changes version.

## 7. Process — how a server gets onto the allowlist

1. **Proposal** (any orchestrator): name the server, its use-case, transport, command/source.
2. **Audit** (§3–§6): the proposing orchestrator produces the audit evidence. Read-only; below-bar.
3. **Reserved decision**: because adoption is an account-and-access grant (trust-posture "never permitted": self-granting permission scope; §"requires ratification": account-and-access grants), the actual add — editing the seat's `~/.claude.json` / `settings.json` `mcpServers` — is **the operator's ratification**, surfaced as a lean with the audit attached. An orchestrator never self-adds an MCP server.
4. **Record**: on ratification, the allowlist table (§2) gains a row with its audit link (P4). On rejection, a one-line note of why (so it isn't re-proposed blind).

## 8. Open questions for HO / the operator at ratification

- **Landing path**: `rulebook/policies/mcp-adoption.md` (landed here). Alternative would have been folding into an existing security surface; kept separate because MCP adoption is a distinct class from the general trust-posture bar and deserves its own reference point.
- **Enforcement mechanism**: this policy is P1-structural in *intent* (allowlist = set-membership), but the *enforcement* is currently procedural (an orchestrator honoring the gate). A fully structural enforcement would be a tripwire audit (à la the T-0761 `repos.yaml` present-but-undeclared pattern) that flags any `mcpServers` entry not on the allowlist. Proposed as a follow-up, not built here — flagged for HO.
- **Codex marketplace templates**: T-3171 noted codex ships inactive plugin-marketplace templates with unpinned third-party commands (`npx -y xcodebuildmcp@latest`) under `~/.codex/.tmp/plugins/`. Inert unless enabled; enabling one is an §7 adoption event. Worth an explicit line if this policy covers the codex harness too (its config is `~/.codex/config.toml`, not Claude Code's).

---

## DECISIONS

1. **Landed as HO-authored PROPOSED, from Specialist's draft** — justifies-via CLAUDE.md write-scope (`rulebook/` is HO-domain, ORG-027 cross-domain routes Specialist's policy-authorship deliverable through HO for landing) + D2 (HO drafts policies). The act-vs-park test on the landing itself: the file IS the reversible below-goal artifact (a proposal that can be ratified or discarded); no adoption is enabled by landing.
2. **Adoption held as reserved, policy-landing taken as below-goal** — justifies-via D3 amended (below-goal methodology/doc-authorship is auto-approve; account-and-access grants are the reserved residue). T-3137 asked to *establish* the policy; T-3275 asks to *land* it. Both in-lane. §7 adoption remains the operator's tier.
3. **Structural allowlist (set-membership) over per-adoption judgment** — justifies-via P1 (architecture over policy): an unlisted server is not-permitted by construction; the residual procedural gap (enforcement is currently orchestrator-honored) is flagged as a follow-up tripwire in §8.
4. **Requirements mapped 1:1 to the watch report's four asks**, each grounded in a specific Aug-2026 threat class from the driver report (STDIO-RCE, npm-worm, no-auth majority, tool-poisoning) — justifies-via D11 (validate against the real surface) + P4 (each entry carries its audit trail).
5. **Content preserved verbatim from Specialist's draft with only landing-level metadata edits** (status line rewritten to reflect HO-landing + awaiting-operator-ratify state; ticket-ref updated to include T-3275; §8 landing-path bullet reworded to reflect the landed state) — justifies-via P4 (the substantive audit + threat mapping is Specialist's; the landing act is HO's; both attributed).
