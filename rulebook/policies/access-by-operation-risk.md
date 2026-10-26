# Access by Operation Risk — which credential may live on which account

**Status:** Proposed (drafted 2026-09-28 by Orchestrator for the operator's ratification; the carrier ticket names this file).
**Author:** Orchestrator (Orchestrator), under T-2893.
**Operationalizes:** ORG-075 decision D3: access is gated by the operation's blast radius and reversibility, not by the actor — least privilege, answered the same way for every actor including the HO.
**Stays inside:** `trust-posture.md`'s reserved set. New credentials, wider scopes, access grants, anything that mints or reveals a secret, money, and outward-facing acts remain the operator's. This policy does not move any of them to the HO; where it proposes a carve-out, the row says **decide**.
**Yields to the broker:** when the ORG-052 host broker lands, its approval tiers govern wherever they are stricter than this file (its §5 puts cross-seat repair and credential install/rotation behind the operator's approval, which then overrides rule 5's "no gate" and row 15).
**Re-evaluate when:** a new operation class appears; the ORG-052 host broker or ORG-051 credential references land (they replace several manual controls below); a credential is exposed; a seat or service account is added or retired.

## What this policy decides

For each **operation class** we actually perform, it fixes how bad a mistake is (blast radius × reversibility → **tier**), **which account may hold the credential** for it, and **which controls attach**. The answer depends on the operation, never on which agent performs it. Nobody "earns" a credential: an account holds one because it is the designated performer of that operation class, and only while that class needs it.

Where credentials physically live and how they are referenced is ORG-051's job; the approval tiers of the future host broker are ORG-052 §5's. This file decides *who holds what*. `repos.yaml` grants should end up as a rendering of it. The evidence base is `notes/2026-09-28-t2893-credential-inventory.md` (names and paths only, no values).

## Account kinds

| Kind | Accounts today |
|---|---|
| HO seat | `claude` (Orchestrator) |
| Orchestrator seats | `suborch1` (mason), `suborch2` (artisan); more may be added |
| Recovery seat | `claude-fallback` (Recovery, head system orchestrator) |
| Research seat | `claude-research` (Specialist) |
| Memory seat | `exocortex` (Archivist) |
| Failover seat | `codex` (Consultant): HO-outage authority per `trust-posture.md` |
| Service accounts | `kino` (PWA), `ci-runner` (self-hosted CI). Today the Echo daemon runs as `claude` (see row 12) |
| Dispatched workers | none of their own: they run under their dispatcher's account and must never use its tier B/C/D credentials (see row 13 for what the guard enforces today) |

## Tiers

| Tier | Meaning | Who may hold the credential | Controls that attach |
|---|---|---|---|
| **A — contained, reversible** | A mistake stays inside one branch, worktree or local build | Any seat, with its own repo-scoped credential | Commit and pre-push hooks (byte-sequence audit, smoke gate) |
| **B — shared, reversible with a proven rollback** | Touches a shared branch or a live service, and a known rollback exists (revert commit, previous binary, backup) | The named holder set for that repo or service | Automated gates (CI, hooks) and an independent review where code changes; rollback staged before acting; ledger entry for anything beyond routine |
| **C — cross-boundary, hard to reverse** | Root, another account's home, the network edge | Head engineering (HO) and head system (Recovery) orchestrators jointly, per D15; scheduled automation only through its installed unit | Interactive use: ledgered in the same session with its justification. Automation: the reviewed unit file is the standing justification. Prefer scoped sudoers or the ORG-052 broker over blanket root |
| **D — irreversible or outward** | Leaks, publishes internal content, spends, speaks outside our repos, moves physical things, or creates/reveals a secret | Exactly one account per credential, never copied; the decision is the operator's, per event or per class where this table pre-decides it | Independent pre-check (leak gate, adversarial pass); supervised first fire when novel or rollback-unproven; alarms |

## Operation classes

| # | Operation | Tier | Holder (account kind) | Credential shape |
|---|---|---|---|---|
| O1 | Local work: branches, commits, builds, tests | A | any | none beyond the account |
| O2 | Coordination writes to pantheon-config `main`: tickets, ledger, notes (pushed directly, not by PR) | B | every seat (named set) | pantheon-config deploy key per seat; rollback = revert commit |
| O3 | Push `agent/*` branches for review | A | orchestrator seats + HO | per-repo deploy key, write |
| O4 | Merge to a protected `main` | B | HO (T-1673; seats push, the HO merges — `sop/standard/retask-dispatch.md:22`); failover seat during an HO outage | HO's GitHub credential with branch-protection bypass; the failover seat's merge path: see row 3 |
| O5 | Deploy or restart a service we run | B | the account the service runs as, or its installed deploy automation (e.g. Recovery's kino autodeploy timer) | own account; sudo only through a scoped rule |
| O6 | Host mutation as root; writes into another account's home or into shared group paths | C | HO + recovery seat (D15) | sudo |
| O7 | CI and build infrastructure: runner accounts, the CT202 runner host | B (C for runner-host root) | HO + recovery seat (D15) | runner registration; setting or rotating Actions secrets themselves is O14 |
| O8 | Network edge and self-hosted services: DNS, tunnels, tailnet, the Forgejo host | C | recovery seat; HO per D15 | service-scoped tokens (DNS-edit, write:repository); issuing or rotating them is O14 |
| O9 | Messages to the operator (Telegram) | B, with message controls instead of review: one token per bot, workers cannot read it, allowlisted chats only | HO, recovery seat, research seat and memory seat, each with its own bot; the orchestrator seats have none (ORG-043); alert units send through a designated bot | one token per bot, never shared |
| O10 | Publishing internal content outward: release/public mirror refresh, first publish of a new mirror, repo visibility or admin changes, posting or commenting outside our repos | D | one designated publisher per mirror; visibility/admin = the operator | publish-scoped credential; no standing admin |
| O11 | Deploying our own public sites through their normal pipeline (e.g. GitHub Pages builds) | B | the repo's normal push/merge holders | none extra: the pipeline publishes |
| O12 | Metered spend: API keys, billing | D | the one service account whose workload spends | one key per workload, each with a usage alarm |
| O13 | Physical actuation and home infrastructure: Home Assistant, UniFi, device management | D | one credential per workload **listed here**: the Echo daemon (voice control) and the recovery seat (home-infra maintenance) | each workload its own non-admin HA user and token (an HA token carries all of its user's rights); Echo's `tool_policy` + admin tier is its in-app control |
| O14 | Secret custody: create, rotate, reveal, copy or move a credential between accounts | D | the operator decides; HO or recovery seat executes | — |
| O15 | Model-provider subscription sessions (Claude and Codex OAuth logins) | B | each account its own, never copied or shared | the provider's login on that account |

## Rules

1. **Least privilege.** Repo-scoped deploy keys over account-wide tokens; read-only unless the operation writes; no credential without a current operation class that needs it (no speculative grants — the ORG-075 consequence that retired the T-2010 PAT).
2. **One holder for tier D.** A tier-D credential lives on exactly one account. Moving custody is a move, not a copy, and is itself O14.
3. **Controls attach to the operation.** Enforce with operation-keyed guards (hooks, the broker, CI) that fire for every actor. A rule that applies to some accounts but not others for the same operation is a gap to close, not a design.
4. **Widening is the operator's; narrowing splits in two.** New credentials, wider scopes and any access grant are the operator's (reserved set). Removing a spare copy or a redundant grant that can be restored **without minting a new credential** is surface-and-act: do it, ledger it, report it. A revocation that could only be undone by minting a new credential (at GitHub, BotFather or a provider) is the operator's, like the grant it undoes. On suspected exposure, see row 15.
5. **Cross-boundary work follows D15.** The HO and the recovery seat both own tier C, with no approval gate between them. If the work touches a live seat, inform that seat first (D15). Every interactive tier-C act is ledgered in the same session.
6. **Workers hold nothing.** Dispatched workers never use their dispatcher's tier B/C/D credentials. Where the guard does not yet enforce this, row 13 is the gap.

## Reconciliation — today's holdings against the table

Ratifying this file ratifies the "Proposed" column. **decide** rows need the operator's pick; the rest follow from the rules and are executed under follow-up tickets.

**How to answer:** reply "leans" to accept every lean, or name the row numbers you reject (e.g. "leans except 3").

**Order of execution** (rows depend on each other): row 2 (the no-bypass ruleset) and the row-1 alert can land in either order; the HO PAT keeps the Administration they need. Row 3 waits until the recovery seat has its own sudo grant and the failover seat's merge path is confirmed.

| # | Today | Conflicts with | Proposed |
|---|---|---|---|
| 1 | HO's GitHub PAT covers **all repositories** with **Administration**. That is what lets the HO create repos for the operator (a routine task he wants kept viable, TG 10497), but it can also flip visibility, delete repos, and push to every release mirror | rule 1; O10 | **Keep the scope** (repo creation needs all-repos + Administration). Compensating control instead of narrowing: an alert to the operator on any visibility change, repo deletion, or ruleset/deploy-key change (GitHub audit events or a poll), and the O10 rules below bind the HO like any actor. **decide:** Lean: yes |
| 2 | pantheon-config's ruleset lets every deploy key **and the operator's own GitHub user** (which the HO PAT signs in as) bypass it at all times, so main's deletion and force-push rules likely bind nobody | rule 3; force-push is reserved (trust-posture) | **decide:** add a second ruleset on `main` with only "block deletion" and "block force-push" and **no bypass actors**. Normal direct pushes keep working. Lean: yes |
| 3 | The failover seat (Consultant) has passwordless root; the recovery seat's broad root currently runs **through Consultant's account**; the HO keeps root via its sudo password; how Consultant merges during an HO outage is undocumented | rule 1; tier C holders; the ratified Consultant exception | **decide:** (a) give the recovery seat its own sudo grant on its own account; (b) confirm Consultant's outage merge path, and if it relies on reading the HO's PAT through root, give Consultant its own scoped merge credential; then (c) move Consultant to scoped sudoers for its failover duties (T-4027 recommendation 3). Lean: yes, in that order |
| 4 | Echo **prod** event-store (j17) keys exist on two accounts (`claude`, `suborch2`) | rule 2 | Keep prod only where the daemon runs; artisan keeps staging/test. A later need for prod comes back as a new grant |
| 5 | Mirror publishing is assigned to Orchestrator in the SOP; Recovery published three mirrors on 2026-09-11 with its GitHub deploy key; the HO PAT can also publish every mirror | rule 2; rule 3 | **decide:** one designated publisher per mirror. Lean: Recovery, holding a per-mirror deploy key, with Orchestrator's go/no-go as the decision point. A **refresh** of an existing mirror through the SOP's gates is pre-decided as a class; a **first** publish or a visibility change is the operator's. Because the HO PAT keeps its reach (row 1), "one publisher" is enforced by the publish gates (leak gate, independent pass, anchor record) and the row-1 alert, not by credential exclusivity |
| 6 | Cross-account repair is written as "Recovery only" in the cross-account SOP | D15 | Update the SOP to D15's joint ownership (no decision needed; D15 is ratified) |
| 7 | Metered API keys (Anthropic, OpenAI, Speechmatics) sit in `echo-prismatic/.env` with no spend control | O12 | Holder: the Echo workload only, no copies to seats; add a per-key usage alarm under `cost-monitoring-thresholds.md` |
| 8 | Home Assistant access on two accounts: Echo's token, and Recovery's (recorded as a password file, likely an admin login) | rule 1; O13 | **decide:** keep both workloads, but give each its own non-admin HA user and token, and retire Recovery's admin password from routine use. Lean: yes. Echo's never-route list is T-4113 |
| 9 | Retired account-level SSH key still authenticates as the whole GitHub account | rule 1 | Revoke at GitHub (T-4132, the operator's click), then delete the local file |
| 10 | Keys not in `repos.yaml` (Rodeo repos, Pantheon-memory, the Windows-machine key, MEBx), and an HO deploy key for codex-config although `repos.yaml` grants that repo only to codex and Recovery | rule 1 | Register each with its task in `repos.yaml`, or bring the revocation to the operator if no task needs it |
| 11 | The retired bridge's `.env` holds a **copy of the live Orchestrator bot token** (verified 2026-09-28: identical value, bot answers) plus the bridge's own local token | rule 2 | Delete the file: it is a spare copy, so this is surface-and-act. Do **not** revoke the bot token, which would cut the live Telegram channel |
| 12 | The Echo daemon runs as `claude`, so its tier-D keys (O12, O13) live on the HO seat, and the HO can spend them from its own session (cf. T-4128) | rule 3 | **decide:** move Echo to its own service account so its keys stop living on an orchestrator seat. Lean: yes, as a scheduled project, not urgent |
| 13 | `worker-guard.py` blocks workers from `git push`, `gh pr create/merge`, the Telegram token, git identity and protected docs, but not from `gh api …/merge`, `gh repo edit --visibility`, `sudo`, or reading credential files (`gh_token.env`, `.env`). Codex-harness workers may not run the hook at all (unverified) | rule 6 | Extend the guard to those operations and verify codex-harness coverage (follow-up ticket) |
| 14 | Group and filesystem write paths let a seat reach `claude`-owned code: seat → `/opt/pantheon-pwa` → kino → `claude` (T-4027) | tier C; rule 3 | Close under T-4106 / T-4027 (in progress: kino moves out of `pantheon-shared`) |
| 15 | No stated response to a suspected credential exposure | rule 4 | **decide:** on suspected exposure, the HO or recovery seat revokes and rotates immediately, then reports to the operator, treating speed as outweighing the reservation. Lean: yes |
| 16 | Alert units send through the HO's bot token; eight of them point at a missing env file and skip silently | O9 | Fix under T-4131; designate which bot alerts use |

## Related

- `architecture/adr/ORG-075-seat-ability-model.md` decision D3 — what this operationalizes.
- `rulebook/policies/trust-posture.md` — the reserved set and the act-vs-park tiers that rule 4 applies; the Consultant failover exception.
- `rulebook/directives.md` D15 — joint Orchestrator/Recovery ownership of infrastructure and cross-account substrate (tier C).
- `architecture/adr/ORG-051-secret-and-authentication-lifecycle.md` — where credentials live and how they are referenced; `architecture/adr/ORG-052-harness-neutral-host-capability-broker.md` §5 — the broker approval tiers that will replace the manual controls for O6–O8 and O14.
- `repos.yaml` — the grants registry; follow-up: render it from this table.
- `sop/standard/refreshing-an-org-039-release-mirror.md` — the O10 publish controls (leak gate, independent pass, fast-forward only).
- `notes/2026-09-28-t2893-credential-inventory.md` — the evidence base.
