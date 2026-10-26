---
aliases: [ORG-105]
id: ORG-105
title: "Pantheon threat model: what we protect, from whom, and the bar a security finding must clear"
status: "Ratified 2026-10-20 by the operator (TG 12174). Proposed by Orchestrator (HO) on request (TG 12161-12164); revised per TG 12170 (services in scope, coverage matrix)."
date: 2026-10-20
author: Orchestrator (head_engineering_orchestrator)
steward: orchestrator
appetite: 3
riskiest_assumption: "That attacker positions can be named crisply enough for agents to apply the bar the same way every time: that they won't re-inflate every finding into 'an attacker could…', and won't wave off a real one because it sounds internal."
build_disposition: needs-build
scoping_ticket: T-5288
relates: ORG-090, ORG-051
no_registry_row_waiver: "Org-wide policy ADR; registers no component. The wiring it calls for (a skill, a reviewer-checklist line, an orientation pointer) lands in existing rendered surfaces under T-5288."
ratifiable_files: [pantheon-config:architecture/adr/ORG-105-threat-model.md]
---

# ORG-105: Pantheon threat model

**Status:** Ratified 2026-10-20 by the operator (TG 12174). Requested TG 12161-12164; revised per TG 12170.

## Decision

1. **What we protect, in order.**

   The model covers the services Pantheon runs and is responsible for:
   - **The agent fleet:** the seat accounts on the VPS, their harnesses, the mailbox hub, tickets and the ledger.
   - **Outward channels:** the Telegram bots, the GitHub account and its repos, release mirrors, public sites, and mail once it exists.
   - **Hosted apps:** web apps and tools we serve, on the tailnet or in public.
   - **Voice and home control:** the voice service, its message broker, and its link into Home Assistant.
   - **Data stores:** git repos, event stores, and backups.
   - **Homelab hosts we administer:** the Proxmox nodes and what we run on them (git mirror, monitoring).

   Third-party platforms themselves (GitHub, Telegram, model providers, Home Assistant upstream) and the operator's own devices are not ours to secure. Our accounts on those platforms are, under "Our outward identity" below.

   The assets, in order:
   - **Money and credentials:** subscriptions, API keys, tokens, deploy keys, the sudo password.
   - **Our outward identity:** the GitHub account, the Telegram bots, any mail or public voice. Nobody else should speak or publish as us.
   - **The home:** the devices our voice and home-control services can operate.
   - **the operator's private data leaving the box:** anything that reaches GitHub, a public mirror, or a third party.
   - **The integrity of fleet code, config and automation.** Nobody should durably change what the fleet does, publishes, deploys or operates in the home.
   - **The fleet staying up.**

2. **Who we defend against (in-scope footholds).**
   - **Opportunistic internet attackers** probing anything reachable from outside the tailnet.
   - **Supply chain:** a compromised package, plugin, MCP server or model-provider tool we install or call.
   - **Untrusted content steering an agent:** web pages, downloaded files, email, Telegram messages from outside the allowlist, peer mail payloads. Prompt injection that turns an agent into the attacker's hands is our most distinctive threat.
   - **A compromised trusted account or endpoint:** The operator's Telegram, email or GitHub; an allowlisted sender; a tailnet or homelab device. These arrive already inside our outsider boundaries.
   - **Guests and other household voices** reaching voice or home-control tools above their tier.
   - **Provider policy and billing:** detectors or terms that turn our usage into money or a ban (the byte-sequence class).

3. **Who we do not defend against (out of scope).**
   - **Anyone who already has a shell on the VPS or root on any host, by means outside (2).** They own everything there.
     - Moving between agents (one unix user to another) IS in scope when the foothold came from (2), e.g. an injected agent reaching another seat's credentials.
     - Logs, journald, debug output, state files and taps on our own hosts are internal. Reading them is not a finding. Content in them matters only when a path carries it outward or across a seat boundary an in-scope foothold could exploit.
   - **Targeted, well-resourced attackers** (nation-state, long-running intrusion).
   - **Physical or provider-level access** to the hosts.
   - **the operator himself** (as distinct from his accounts being compromised, which is in scope).

4. **The bar for a security or privacy finding.** A finding earns work only if it names a complete chain, with three parts:
   - **a foothold** from (2);
   - **a path** that foothold can actually reach, which may run through our own outputs (for example a secret written to a log, then pasted into a ticket that reaches GitHub);
   - **an impact** on an asset in (1).

   An incomplete finding goes back to its author to complete; it isn't rated. If no in-scope chain can be named, it is noted on its ticket as out of scope and left alone. "Defense in depth" alone doesn't make a chain. A finding with a complete chain is ranked by the asset at stake, not by how clever it is.

5. **Transparency by default.** Observability (logs, taps, error text, debug output) stays readable, so people can troubleshoot their own systems. A privacy or redaction feature ships as an opt-in setting, default off, unless a finding that clears (4) says otherwise.

6. **Always present.** This model is loaded wherever a security or privacy question is raised or ruled on. That covers the agent writing it up, the reviewer rating it, and the orchestrator deciding it. How it is wired in is build detail (T-5288).

7. **Coverage matrix.** Rows are the in-scope footholds from (2); columns are the assets from (1). Each cell says whether that foothold can realistically reach that asset through a service in (1):
   - ● **covered**, a primary concern: findings on this chain get ranked and worked;
   - ○ **covered**, a secondary concern: reachable only through a narrower path;
   - — **no realistic path**: a finding here must first show the path, per (4).

   | Foothold ↓ / Asset → | Money & credentials | Outward identity | The home | Private data leaving | Integrity | Uptime |
   |---|---|---|---|---|---|---|
   | Internet attacker | ○ | — | — | ○ | ○ | ● |
   | Supply chain | ● | ● | ○ | ● | ● | ○ |
   | Untrusted content steering an agent | ● | ● | ○ | ● | ● | ○ |
   | Compromised trusted account or endpoint | ● | ● | ● | ● | ● | ● |
   | Guest or household voice | — | — | ● | ○ | — | ○ |
   | Provider policy and billing | ● | ○ | — | — | — | ● |

   Out of scope by (3), whichever asset they touch: an existing shell or root foothold, targeted well-resourced attackers, physical or provider-level access, and the operator himself.

   The matrix is a starting map, not a verdict. A finding still needs its full chain under (4). A finding that shows a real path where the matrix marks "—" is grounds to amend this ADR.

## Why

- **2026-10-20 redaction run** (T-5169, T-5252, T-5273, T-5275, T-5280; dropped T-5281 and T-5284):
  - Four rulings in a row redacted Home Assistant names, error text and transcript text from echo logs, taps and debug output, each citing "cheap protective action".
  - None named an attacker. The logs never leave the VPS.
  - The result dimmed the troubleshooting surface the taps exist for. The operator: the threat model was "misdialed".
  - Rolled back to an opt-in setting under T-5286.
- **The pattern is fleet-wide.** The operator: "the team notices things that are *technically* security issues, but in the right threat model, they're not worth effort nor real concern" (TG 12161).
- **The governance already exists; the threat model didn't.** `rulebook/policies/trust-posture.md` and `access-by-operation-risk.md` say who may touch secrets, money and outward acts. Nothing said which attackers matter, so every reviewer used their own.

## Alternatives considered

- **No model; judge case by case.** That's the status quo that produced the pattern.
- **A detailed STRIDE-style matrix per component.** Too heavy for a one-operator org, and it goes stale. The foothold-by-asset matrix in (7) gives the same at-a-glance coverage at the altitude this ADR works at. Per-component detail can come later where an asset warrants it.
- **Treat in-host agents as adversaries by default.** This would justify most of the redaction work. Rejected: the agents run as us, with our credentials, and our own logs aren't a leak. The real agent risk is untrusted content or a compromised account steering one, and what it can reach from there. That's in scope under (2), including movement from one seat to another.

## Consequences

- **Positive:** fewer, better security tickets. Reviewers rate privacy and security findings against one yardstick, and observability stops eroding.
- **Negative / accepted:** completing the chain costs the author a little effort on every finding. A real issue someone fails to frame is sent back, not buried. Out-of-scope positions are accepted risks: if one changes (say the VPS gains another tenant), this ADR needs amending.

## Appetite

3/10: one page of decisions, plus three small wiring changes to existing rendered surfaces (T-5288).

## Riskiest assumption

That attacker positions can be named crisply enough for agents to apply the bar the same way every time. Cheap test: apply it to the next five security or privacy findings across reviewers. Check that internal-only findings go to out of scope, while untrusted-content, compromised-account, cross-seat and outward-leak chains are ranked by asset. If they drift, sharpen the lists in (2) and (3) with the misfiled examples.
