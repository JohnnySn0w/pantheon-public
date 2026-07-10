---
aliases: [ORG-040]
title: Authority provenance — substance anchor, accidental-drift check, and the documented accepted-risk boundary
status: Adopted (HO, under amended D3; operator-digest-surfaced)
date: 2026-07-06
author: Orchestrator (Orchestrator)
ticket: T-1576
relates: ORG-002 (two-tier split), ORG-015 (ratifiable-class allow-list), T-1550 (12-bug gap-check), claude-code#54393 (external post-mortem)
---
# ORG-040 — Authority provenance: how Pantheon distinguishes operator-authority from agent authorship

**Status:** **Adopted** — org-shape below the goal-level bar (amended D3, msg 3733); surfaced on the operator's digest with the explicit note that upgrading to signed attestation is his door to open, since every stronger scheme costs *his* workflow.

## Context

The claude-code#54393 post-mortem's one genuinely novel gap for Pantheon (T-1547 cross-check → T-1576): a filesystem cannot distinguish an agent-authored file *claiming* human authority from an actual human ratification, and shape-checks (filename/regex gates) teach bypass rather than enforce intent. Pantheon's entire below-goal autonomy model (principles-as-ratification) rests on "operator-ratified" markers whose enforcement is prose convention. Nothing mechanical stops an agent from writing `Ratified by the operator` into a file.

Threat model honestly stated: our instances are trusted (same operator, distinct unix users, audit norms). The realistic failure is **accidental authority-confusion** — a good-faith agent, post-compaction or off a stale handoff, treating its own draft as ratified — not malice. #54393 documents exactly this: good-faith agents + no substance check = drift into forged authority.

## Decision — three tiers, each named for what it actually provides

### 1. The substance root already exists: out-of-band Telegram anchorage (recognized, now REQUIRED)

Every genuine the operator ratification originates in a channel agents cannot write: his Telegram messages. The convention of citing `msg_NNNN` / TG ids in ratification markers is hereby **required form**: an authority claim without an out-of-band anchor (Telegram msg id, or an equivalently agent-unwritable source) is not a ratification marker, it is a draft note. This is what #54393's system lacked — our filesystem claims are *pointers to* substance, not the substance itself. Verification against Telegram history is operator-side and expensive — which is why tier 2 exists.

### 2. Accidental-drift check: claim ↔ ledger cross-verification (structural, cheap, filed as T-1589)

The ledger independently records each ratification same-day (P4 discipline, written in the receiving session while the Telegram message is live context). An **accidental** post-compaction misclaim produces a file asserting `Ratified … msg_NNNN` with **no agreeing ledger entry** — two independent records that should corroborate, don't. The control: a lint/groomer check that parses authority markers in rulebook/adr/policies surfaces and greps the ledger for a corroborating entry (same msg id or same artifact + date window); mismatch = warn-tier flag, human-routed. This catches the realistic failure class mechanically without any signing ceremony. Warn-only: corroboration gaps include benign historical formats, so it flags, never blocks.

### 3. The malice class: documented ACCEPTED RISK (not an accident of omission)

An agent with repo write access could forge both the file claim *and* the ledger entry. Cryptographic attestation (the operator signing artifacts) would close this — and is **deliberately not adopted**: it taxes the operator's finite bandwidth (D1) to defend against a threat our model excludes (trusted instances, one operator, cross-uid separation, incident norms). If the org ever admits less-trusted writers (external contributors, untrusted models), this ADR's re-evaluate trigger fires and tier 3 upgrades from accepted-risk to signed attestation. Until then: the boundary is chosen, recorded, and operator-visible — the #54393 failure was that the equivalent boundary was *unexamined*.

On shape-gates generally (the HERMES guard being itself a content-regex): shape-gates stay acceptable for *cost/noise* controls (their bypass wastes money, not authority); they are not accepted as *authority* controls. Authority = tiers 1–2.

## Re-evaluate

- Any non-trusted writer gains commit access to governance repos → tier 3 upgrade becomes mandatory before their first commit.
- A confirmed instance of authority-confusion (accidental or otherwise) → revisit whether tier 2 warn-only should harden.
- The operator opts into any signing mechanism → fold it in as tier 0 above all of this.

## DECISIONS

- **Recognize Telegram anchorage as the substance root rather than inventing a new one** — justifies-via P1 (the structural control that already exists beats a new ceremony) + P4 (ratification markers become pointers to verifiable substance).
- **Claim↔ledger corroboration over signed attestation** — justifies-via D1 (zero the operator cost) + proportionality to the actual threat (accidental drift, per both #54393's data and our own threat model); the two-independent-records shape mirrors how the T-1586 stale-draft check caught a real severed link on day one.
- **Document the malice boundary as accepted-risk instead of leaving it implicit** — justifies-via P6 (the tension between trust-model simplicity and integrity guarantees is preserved with a named rebalance trigger, not collapsed silently) — UNDECIDED→DECIDED was the ticket's whole ask.
- **Adopted, not ratify-gated** — justifies-via amended D3 (org-shape/methodology below goal-level); the one dimension that would be his (a scheme costing his workflow) is explicitly NOT adopted, and the digest line hands him the door.
