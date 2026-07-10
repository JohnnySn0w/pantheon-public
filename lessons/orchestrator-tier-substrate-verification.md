# Orchestrator-tier substrate verification

**Status:** Drafted 2026-05-11 by HO; HO-autonomous creation per D2 (lessons are HO-authored unless they assert a rule touching principle/directive/ADR surfaces — this is an observational pattern, not a behavioral rule).

## The pattern

Ticket specs are authored under assumptions about the implementation substrate that may not match reality. The orchestrator tier is structurally positioned to catch these mismatches at dispatch time — before the implementer is in flight + before the substrate-discovery is mid-code-tier.

The orchestrator's pre-relay verification step (D4 / D5) is conventionally framed as "verify named symbols against source before locking the brief." Extending that frame: the orchestrator should ALSO verify the *substrate assumptions* embedded in the spec, even when those assumptions aren't named symbols.

## How it surfaced

In the b36 / T-0206 cycle (2026-05-11), the project orchestrator caught four spec-vs-substrate mismatches before they reached the implementer:

1. **§6 "channel tag" wire shape** — Spec said "each image gets a `<channel ... image_path="...">` attribute" on the outbound wire. Substrate: kino's wire is plain JSON NDJSON; no XML tags exist there today. The `<channel>` tag is what Claude Code's runtime *renders* from MCP-notification content (telegram-bridge case), not a wire format kino emits. Orchestrator bubbled the structural gap to HO; HO ratified Option A (embed XML in user content + empirical wet-run validation).

2. **§7 "JSON storage" persistence** — Spec said "each message gains optional `images` field" assuming JSON-blob message storage. Substrate: SQL columns. Adding the field needs a migration. HO REBOUND'd the orchestrator's initial composed-string-in-DB lean (which would have entangled wire format with display format); the right shape was migration 004 + repository changes to split content from images.

3. **§4 "MessageInput.svelte" component path** — Spec said "likely `src/components/MessageInput.svelte` — verify path at implementation time." Substrate: actual file is `web/src/components/InputBar.svelte`. Orchestrator caught this in reconnaissance + locked InputBar verbatim in the brief, preventing implementer from hunting.

4. **§10 "~4-5 files for frontend" scope estimate** — Spec estimated ~4-5 files. Actual scope (with substrate discovery — sendMessage signature change, AgentPane wiring, separate Lightbox component) was 8 files. Not a defect; the spec was a guidance estimate, but the discrepancy was real + worth flagging.

All four were caught at orchestrator dispatch-tier or earlier, NOT at implementer code-tier. Each would have been more expensive to fix mid-cycle (implementer mid-edit, plan-tier rebound, code rework).

## The mechanism

The orchestrator tier IS the right tier for substrate verification because:

- **Orchestrators read more context than implementers do, by design.** Project orchestrators load the ticket spec + the relevant project's CLAUDE.md + the existing code surface; they're literate in the substrate. Implementers receive a tight brief + their files; they trust the brief.

- **The verification cost is bounded** — typically 1-5 minutes of grep + file reads per spec section. The cost of NOT verifying compounds at implementer-tier (plan rebound + code rework + reviewer drift on a misaligned PR).

- **D4 already mandates this for named symbols.** The extension is: substrate ≡ named-symbols-plus-the-assumed-environment-around-them. A spec naming `MessageInput.svelte` makes a substrate assumption (the file exists at that path); a spec naming "the `images` field on the message" makes a substrate assumption (the message has fields, not columns); etc.

## Operational guidance

When orchestrators receive a dispatch brief and prepare per-unit briefs for implementers, they should run a substrate-verification pass on the spec's load-bearing claims:

1. **File paths**: every named file path in the spec → `Read` or `ls` to confirm. If wrong, lock the canonical path in the brief (don't repeat the spec's wrong path with hope-it-works framing).
2. **Wire formats / IPC shapes**: every "outbound to X" or "inbound from Y" claim → grep for the actual call sites + serialization code. Substrate mismatches here often surface as composable-pure-functions need at the wire layer.
3. **Persistence shapes**: "field on X" / "row in Y" / "key in Z" → grep schema files or migration histories. SQL vs JSON-blob is the classic mismatch.
4. **Component paths / module boundaries**: if a spec says "implement in module M" → check that M actually exists + has the assumed shape. Sometimes the spec was authored from a guess; sometimes the codebase has refactored since the spec was written.
5. **Scope estimates**: spec's "~N files" → orchestrator's own estimate from substrate read should match within ±2. If 4× or more off, flag at dispatch (the spec was authored without the substrate visibility).

For substrate mismatches discovered: lock the canonical reality in the per-unit brief as a `§Locked answer`, and batch-flag the spec deviations for HO's resolve-summary at cycle close. Don't edit the ticket spec mid-cycle (D2 — workers + orchestrators don't curate hand-curated docs); leave the resolve-summary trail.

## Why pinned here

This generalizes the discipline that D4 (brief quality bar) introduced. D4 names "verify named symbols at source"; this lesson names "verify substrate assumptions, which includes named symbols and their environment." The b36 cycle hit four substrate mismatches in one ticket — that's enough to count as a pattern, not a one-off. The lesson is also an asset for future-orchestrators: "before you dispatch the implementer, run the substrate-verification pass — here's the 5-item checklist."

## Cross-references

- **D4** (brief quality bar — the directive this lesson generalizes).
- **D5** (smoke discipline — orchestrator pre-relay is the verification surface at code-tier; this lesson extends to dispatch-tier).
- **D6** (plan-first protocol — substrate-verification belongs upstream of plan-tier dispatch).
- **bugs/** entries surfaced from substrate mismatches (none specifically yet; could be a future catalog).
- **T-0206 / b36 cycle** (2026-05-11) — origin context.
- **Lesson siblings:** `structural-enforcement-retires-the-prose-rule.md` (different layer; this lesson is about pre-dispatch verification, that one is about post-implementation discipline).
