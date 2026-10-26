---
aliases: [ORG-101]
id: ORG-101
title: "Git redundancy — one write authority, ff-only two-way sync, and a daily conformance sweep across GitHub and Forgejo"
status: "Ratified 2026-10-09 by the operator (TG 11549, after a plain-language summary in TG 11548). Direction approved 2026-10-07 (TG 11375/11376). Shaped with Recovery's steward critique (T-4690). Reversible via amend/supersede."
date: 2026-10-07
author: Orchestrator (head_engineering_orchestrator)
steward: recovery
appetite: 5
riskiest_assumption: "That a fast-forward-only reconciler plus GitHub-first gated pushes keeps the two hosts from diverging in normal operation, so a divergence alert is rare and means a real incident. If divergence turns out to be routine (for example ticket.py push races during reconciler lag), the alert becomes noise and the design needs a tighter push gate or a shorter lag."
build_disposition: built
scoping_ticket: T-4690
extends: [ORG-046]
relates: ORG-046, ORG-039, ORG-100, ORG-076, ORG-003
ratifiable_files: [pantheon-config:architecture/adr/ORG-101-git-redundancy-write-path-and-sync.md]
---

# ORG-101 — Git redundancy: write path, sync, and conformance

**Status:** Ratified 2026-10-09 by the operator (TG 11549). Direction approved 2026-10-07 (TG 11375/11376). **Steward:** recovery. **Extends:** [[ORG-046]], whose read failover this completes with the write path it left to T-1998.

## Intent

**Redundancy** (the operator, TG 11375). Every repo should exist, current, on two hosts: GitHub and the self-hosted Forgejo at git.your-self-hosted-git.example. Either host's outage should cost no work and no history. This serves the fleet's resilience goal ([[ORG-076]] is the runtime counterpart) and the [[ORG-046]] path toward a self-hosted primary.

## Context

- **What exists.** [[ORG-046]] (ratified 2026-08-10) gives manifest-resolved remotes with **read** failover: try GitHub, fall back to Forgejo. Recovery has piloted it since 2026-09-28 (T-1998). Each seat already holds a Forgejo token (T-4051). Recovery runs 67 Forgejo **pull-mirrors** of GitHub repos, and pantheon-inventory is already Forgejo-native.
- **The gap.** ORG-046 deferred writes and sync (§2: "unsafe for writes"). Pull-mirrors refuse pushes. During the 2026-10-07 GitHub git-operations outage, seats could read from Forgejo but not write anywhere durable.
- **The trap** (Recovery's critique). The naive shape is two symmetric push URLs plus fetch from Forgejo. It diverges routinely, not just in outages. PRs squash-merge on GitHub's side, so a seat that rebased onto a Forgejo copy without that merge pushes, Forgejo accepts, and GitHub rejects. ticket.py pushes to `main` many times an hour.

## Decision

1. **One write authority at a time.** GitHub is the write authority while PRs merge there (Phase A). Forgejo becomes it only through the deliberate Phase B flip (§8). Two hosts never accept independent writes to the same branch as normal operation.
2. **Gated push order.** Manifest-aware push tooling (the `_git_utils` push path that ticket.py and the HO merge flow use) pushes to the write authority first, and to the other host only after the authority accepts. When the authority **cannot take the write**, the push goes to the other host, and §3 replays it once the authority is back. "Cannot take the write" means one of two things: it is unreachable (ORG-046's reachability definition), or it is reachable but **fails server-side** (5xx, `remote: fatal error`, RPC failure). The second is the shape of the 2026-10-07 outage. A **non-fast-forward or a permission/protection rejection is a real answer and never fails over.** Routing around it is exactly the split-brain §Context warns about. Ambiguous failures fail closed. Bare user `git push` is not the redundancy surface, matching ORG-046 §2's line for fetch.
3. **A two-way reconciler that never forces.** A reconciler is the only thing that moves refs between the hosts. Per repo and branch, it fast-forwards whichever side is behind. That carries GitHub-side PR merges to Forgejo and replays outage-time Forgejo pushes to GitHub. When neither side is an ancestor of the other, that is **true divergence**: it alerts and changes nothing. Resolving it is a steward procedure (`sop/remediation/`, written during the build). Typically the Forgejo-only commits are replayed onto GitHub through the gated push path, and the reconciler then fast-forwards Forgejo. Forgejo push-mirrors are not used, because their mirror/force semantics would overwrite GitHub-side squash merges. The reconciler replaces the current pull-mirror verify loop. In Phase A it reconciles `main` only. Other branches need deletion semantics (a merged-and-deleted PR branch must not be recreated), so they are deferred.
4. **Convert only under coverage.** Each Forgejo pull-mirror becomes a regular, pushable repo (Forgejo `convert`). The conversion is one-way and happens only after the reconciler covers that repo, so no repo ever stops tracking GitHub. Repos with no Forgejo copy are created. Rollout is pantheon-config first, then the rest in batches.
5. **Protection parity.** Each host enforces the same branch rules with the **same bypass set**. On GitHub today, pantheon-config `main` requires changes through a pull request, and the fleet's push identities are bypass actors. The split between ticket/ledger pushes and code is client-side discipline (worker-guard, hooks), not a server carve-out. Forgejo mirrors that: the same rule, with the same identities exempt. The reconciler is in that bypass set, because replaying outage-time commits onto `main` needs it. Parity is proven two ways. First, by **reading** both hosts' rules through their APIs and comparing them (hence the reconciler token's read-only Administration scope). Second, with live refusals: a push from a throwaway **non-bypass** Forgejo user, and a force-push attempt on both hosts. Force-push blocking is verified, not assumed, because it also binds bypass actors only when it is configured to. The client-side guards (no force-push, the byte-sequence guard, worker-guard) are unchanged because they run before any push.
6. **Scope: every repo** (the operator, TG 11376). That covers fleet, personal and client/business repos. Two layers of visibility: the Forgejo **instance** requires sign-in for everything, regardless of repo settings. Each repo's **private flag** mirrors its GitHub setting, and the sweep compares that flag (§7).
7. **Daily conformance sweep** (the operator, TG 11375). A read-only daily check flags any repo or seat clone that departs from this standard:
   - a GitHub repo with no Forgejo counterpart;
   - a Forgejo copy that is still a pull-mirror or outside the reconciler;
   - a protection or visibility mismatch;
   - a seat clone whose remotes don't match the manifest.

   It alerts the steward by mailbox and keeps a skip list for dispositioned exceptions. Its shape is the fleet clone-currency check.
8. **Phase B, the flip, is separate and deliberate.** Making Forgejo the write authority and the fetch default is ORG-046 Q1's one-line manifest reorder. It also needs PRs and CI on Forgejo (the CT202 runners exist). It happens on the operator's word, not by drift.

## Responsibilities ([[ORG-100]] roles)

| Responsibility | Role | Seat |
|---|---|---|
| Steward; Forgejo admin; conversion, reconciler, sweep; Forgejo team and per-seat tokens | `head_system_orchestrator` | Recovery |
| This ADR; the gated push path in `_git_utils`/ticket.py; PR and merge flow | `head_engineering_orchestrator` | Orchestrator |
| New credential: a GitHub fine-grained token for the reconciler and sweep (Contents RW, Workflows RW, Administration read-only) | Operator | the operator (approved with Phase A, TG 11375) |

## Non-goals

- Not off-host disaster recovery for the rest of the fleet's state; T-1871 covers that.
- Not a change to who may push or merge (T-1673: HO-exclusive merges stand).
- Not automatic failover for bare user git commands.
- Not the ORG-039 public-mirror publishing flow. Those mirrors are separate repos with their own publish leg, and the sweep only checks that they exist.

## Alternatives considered

- **Symmetric dual-push with fetch from Forgejo** (the request as first worded). Rejected because it diverges routinely while GitHub merges PRs (§Context). The intent, redundancy, is met by §1–§3 without that cost.
- **Forgejo push-mirrors to GitHub.** Rejected because force semantics would clobber GitHub-side merges.
- **Keep pull-mirrors only (read redundancy).** Rejected because it loses writes during a GitHub outage, which is the gap today's outage exposed.
- **Flip to Forgejo-primary now.** Deferred to Phase B, because PRs and CI live on GitHub today, and ORG-046's ratified Q1 is GitHub-first until the operator flips it.

## Build sequence

1. Reconciler (ff-only, divergence alert), proven on pantheon-config.
2. Convert pantheon-config's Forgejo copy; prove protection parity with a denied push on each side.
3. Gated push in `_git_utils`, with tests for authority-down failover and replay.
4. Batch conversion and creation for the remaining repos; per-seat remotes via the manifest.
5. Daily conformance sweep.

## Re-evaluate when

- Divergence alerts fire more than rarely (the riskiest assumption has failed).
- The operator calls the Phase B flip.
- A third host is added.
