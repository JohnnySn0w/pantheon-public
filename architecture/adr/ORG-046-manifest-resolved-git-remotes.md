---
aliases: [ORG-046]
id: ORG-046
title: "Manifest-resolved git remotes: decouple logical repo name from URL, with multi-remote failover and self-heal"
status: "Ratified 2026-08-10 by the operator via Telegram (msgs 7576-7578). DISPOSITIONS: Q1 (primary preference) = GitHub-primary first, flip to self-hosted later as a one-line manifest list-order change; Q3 (external rollout) = commit adoption instructions into the GitHub repo, THEN execute the T-0915 rename (the in-repo commit reaches the ~5 collaborators before their remotes break); Q2 (manifest integrity) = withdrawn, signing-as-default. HO-drafted 2026-07-23, codex ORG-041 review applied (15 findings), Recovery host-level-binding fold applied 2026-08-10. Not yet built — T-1998 (multi-remote wiring) now unblocked. Ratification gate per ORG-039-class (distribution/product axis = the operator ratifies, HO drafts)."
date: 2026-07-23
# elements: — ORG-065 machine index of ORG-046's "## Decision" section
# (T-3205 Tranche B, "numbered-sections-only" decomposition class).
# E1-E6 mirror the six native "### N." headings 1:1 (single group; no
# other numbered content in this ADR is elementized — "## Open
# questions for the operator (ratification)" is genuinely interrogative
# pre-ratification text whose resolution lives in the frontmatter
# status/addendum prose, not a declarative decision list, so it is NOT
# transcribed as obligation content this pass). Each entry's text opens
# with its native id verbatim. Added by HO; no obligation content
# changed.
elements:
  - {id: E1, text: "1. Manifest schema + invariants: each repo gains an ordered remotes: list; each remote has role, url, and an auth reference (by name/path, never inline — ORG-039)"}
  - {id: E2, text: "2. Resolver, reachability, and the failover execution contract: a resolver (tools/remote_resolve.py) generates each clone's git remotes from the manifest; the failover execution contract (findings 1, 7) is READ ONLY in this ADR"}
  - {id: E3, text: "3. Managed-remote model + bounded self-heal: sync/self-heal mutate ONLY remotes carrying pantheon-managed=true; self-heal repoints only on a CONFIRMED PERMANENT identity change"}
  - {id: E4, text: "4. Security model & trust boundary (findings 4, 5, 6): the manifest is authoritative for where credentials point; auth-to-host binding; no implicit trust of transport-layer signals"}
  - {id: E5, text: "5. Supported repository forms & git-feature scope (findings 12–15): submodules, Git LFS, shallow/partial clones + mirror completeness, repository forms are all declared and validated, never silently mis-handled"}
  - {id: E6, text: "6. Downstream propagation (T-1999): the resolver + a generic schema ship in the seed via neutralize_seed.py; the shipped manifest is a reference example with DISABLED / invalid-placeholder-domain entries"}
---

# ORG-046 — Manifest-resolved git remotes

**Status:** **Ratified 2026-08-10 by the operator via Telegram** (Q1 GitHub-primary-first; Q3 in-repo instructions commit then T-0915 rename; Q2 withdrawn). **Author of record:** Orchestrator (HO).
**Source of authority:** the TG 6404→6408 design exchange (2026-07-23) — the operator's "yee spec it"
on the self-hosted-git + manifest-resolved-remotes direction, out of the T-0915 rename downstream-
break analysis. **Reviewed:** Codex (gpt-5.5) ORG-041 planning-phase spec review, 2026-07-23 —
NEEDS-REWORK, 15 findings, all applied (see §7 + DECISIONS). **Keystone of:** T-1996; siblings
T-1997 (self-hosted git server) / T-1998 (multi-remote wiring + write/sync policy) / T-1999 (ship to
seed), all under **T-1871** (Proxmox-primary + dead-man-VPS-failover — this ADR is the resolution
mechanism that makes that real).

## Why this ADR exists

An agent's git remote today is a **hardcoded URL**. That couples every clone to one host and one
name, and breaks in three ways we've hit or reasoned through:

1. **Rename-break (T-0915).** A GitHub rename is safe *until the old name is reoccupied* — then the
   redirect is overridden and clones on the old name silently resolve to the wrong repo or hit
   permission-denied. The claude-config→claude-code-config swap does this; it would break the ~5
   external collaborators' clones.
2. **No sovereignty / no failover.** GitHub is a single dependency. The operator is standing up self-
   hosted git on the Proxmox node (T-1997); clones then need to *prefer* self-hosted and *fall back*
   to GitHub — the T-1871 dead-man. A hardcoded single remote can't.
3. **Credential-scope churn.** A fine-grained PAT loses push on an org transfer (the T-1977 hazard);
   resolution that carries the current per-host URL insulates the fleet from that churn.

The fix is one indirection: **resolve remotes through the manifest instead of hardcoding them.**
Pantheon already ships a manifest (`repos.yaml`/`instance.yaml`, ORG-028) — today a registry. This
ADR makes it **load-bearing for git remote resolution**, with the security and execution specifics
tightened so a builder can implement `tools/remote_resolve.py` without guessing.

## Decision

**A logical repo name resolves, through the *trusted* manifest, to an ordered list of remotes; only
Pantheon-managed remotes are ever mutated; failover is read-only until writes are specified (T-1998);
and self-heal repoints only on a confirmed permanent identity change, never on a transient error or a
bare redirect.**

### 1. Manifest schema + invariants

Each repo gains an ordered `remotes:` list; each remote has `role`, `url`, and an `auth` reference
(by name/path, never inline — ORG-039). Auth references are **bound to an allowed host pattern**
(finding 6) — repo-scoping is not enforced by the resolver; it rides on the credential's own
forge-side scope (a fine-grained PAT or a per-repo deploy key is repo-scoped at the forge), so this
manifest binding is host-level defense-in-depth, not the repo gate:

```yaml
repos:
  - name: pantheon-config          # logical name — stable, never the break surface
    remotes:
      # WHICH physical host is `primary` is Q1 — the addendum recommends GitHub-primary
      # AT FIRST, flipped to git.your-self-hosted-git.example-primary later by reordering this list (one line).
      # The assignment shown reflects that recommendation; hosts/URLs are AS-BUILT 2026-07-24
      # (see §7-REVISED of the T-1997 spec).
      - role: primary              # exactly one primary; preference = list order
        url: git@github.com:example-org/pantheon-config.git   # GitHub, until self-hosted has a track record (Q1)
        auth: deploy-key:pantheon-config
      - role: mirror               # zero or more mirrors
        url: https://git.your-self-hosted-git.example/pantheon/pantheon-config.git          # self-hosted: public + auth-to-pull, HTTPS (SSH §8 deferred, so https not ssh)
        auth: token:forgejo-pantheon-config
auth_refs:
  deploy-key:pantheon-config:
    key: <path>
    allowed_hosts: [github.com]                  # a per-repo SSH host-alias — the deploy key is repo-scoped BY the alias; resolver REFUSES url/auth whose host is outside this
  token:forgejo-pantheon-config:
    token: <path>                                            # Forgejo access token (HTTPS auth-to-pull)
    allowed_hosts: [git.your-self-hosted-git.example]                           # a shared host — repo-scoping rides on the token's forge-side scope, not this host binding
```

> **As-built note (2026-07-24, updated at ratify-time — transparent, not a silent edit):** the example
> above was updated from its original `git@git.<tailnet>:…` tailnet-SSH placeholder to the now-LIVE
> public identity. `git.your-self-hosted-git.example` resolves, serves HTTPS behind caddy with a valid Let's Encrypt cert,
> and is verified 200 from swarm. The change is illustrative-only: the **mechanism** this ADR
> ratifies — manifest resolution, the `allowed_hosts` binding (finding 6), redirect-never-authority,
> self-heal — is unchanged; only the example's scheme/host move from a superseded placeholder to
> reality. Two substantive facts the update encodes: (a) the self-hosted remote speaks **HTTPS with a
> Forgejo token**, not an SSH deploy key, because git-over-SSH (§8) is **deferred** — manifest remotes
> for it are `https://`, not `ssh://`, until the built-in SSH server is stood up; (b) the example
> assigns `role: primary` to **GitHub**, matching Q1's recommendation (self-hosted stays a `mirror`
> until it has a track record, then the two swap by reordering the list) — it is an illustration of
> that recommendation, **not** a pre-decided ratification of it; Q1 is still the operator's call.

**Schema invariants (hard-fail on violation — finding 9):** exactly one `primary`; zero+ `mirror`;
unique generated git-remote names; valid URL schemes; deterministic entry→remote-name mapping; every
`auth` ref resolvable and its `url` host inside the ref's `allowed_hosts`. An invalid manifest is a
loud error, never a silent partial-apply.

### 2. Resolver, reachability, and the failover execution contract

A resolver (`tools/remote_resolve.py`, built under T-1996) generates each clone's git remotes from
the manifest: `origin` = the primary, plus named `mirror-N` remotes, each tagged
`remote.<name>.pantheon-managed=true`. Regenerated idempotently on config sync.

**Reachability is defined per operation (finding 10)** — TCP/DNS is insufficient for choosing an
*authoritative* git remote: fetch-reachability = `git ls-remote <remote>` returns the expected refs;
push-reachability = a permission dry-run where the host supports it, else deferred to T-1998.

**Failover execution contract (findings 1, 7) — READ ONLY in this ADR.** Manifest-aware tooling
(not bare user `git`) performs fetch/read operations as: try `origin`; on a *reachability* failure,
try each `mirror-N` in order; the fallback is **ephemeral for that operation** and does NOT persist a
remote change. Bare `git fetch origin` is explicitly **not** auto-failed-over — direct user git is
out of scope; the tooling is the failover surface. **Writes/push and primary↔mirror sync are
deferred to T-1998** — until then, §3 covers read failover only, because "keep working off the
mirror" is unsafe for writes (divergence/lost-update on primary recovery).

### 3. Managed-remote model + bounded self-heal

**Managed-only mutation (finding 2).** Sync/self-heal mutate ONLY remotes carrying
`pantheon-managed=true`. A user-added remote, a fork remote, a temp test remote — untouched. If
`origin` exists but is unmanaged, the tooling **fails with a diagnostic** rather than clobbering it
(no silent data-loss of local config, which matters most for external collaborators).

**Self-heal repoints only on a CONFIRMED PERMANENT identity change (findings 3, 4)** — the trigger
set is split by class:
- **Permanent (may repoint):** the manifest `url` changed since last applied (version/hash compared),
  OR an explicit stale-remote detection confirmed against trusted manifest metadata.
- **Transient/auth (fall back for the current op, NEVER persist a repoint):** permission-denied
  (could be expired creds / revoked key / SSO / rate-limit), connection-refused, network blip. These
  surface the error after an ephemeral fallback; they do not rewrite remotes.
- **Redirects are NEVER trusted as authority (finding 4, security-critical).** A rename-redirect is
  the *original* attack surface (reoccupied name → wrong repo). A redirect is at most a *signal to
  re-read the trusted manifest*; the resolver repoints only if the manifest confirms the new
  location, and validates the destination host/namespace/repo identity (and preferably remote HEAD)
  against manifest metadata before trusting it.

Bounded: one re-resolve + retry per operation, then surface — a genuinely-gone remote must error, not
be papered over (the false-RED discipline). **Concurrency (finding 8):** manifest sync, remote
mutation, and self-heal retry take a **repo-local lock**, so two operations can't split-brain the
remotes mid-op.

### 4. Security model & trust boundary (findings 4, 5, 6)

The manifest is now authoritative for where credentials point — so its integrity is load-bearing:
- **Manifest source + integrity:** the resolver loads `repos.yaml` only from the trusted config
  clone; config-pull **verifies commit signatures** (or a pinned hash) before a manifest change takes
  effect. A tampered manifest must not be able to point clones-with-credentials at an attacker URL.
- **Auth-to-host binding:** every `auth` ref carries `allowed_hosts`; the resolver **refuses** any
  `url`/`auth` pair whose host is outside the binding — so a manifest entry can't pair a real key/PAT
  with an arbitrary URL to exfiltrate via the git protocol. This binding is **host-level by design**:
  same-host / different-repo misuse (a repo-A credential pointed at repo-B on the same host) is gated
  not here but by the credential's own forge-side scope — a per-repo deploy key reached through a
  repo-specific host-alias, or a fine-grained/Forgejo token scoped to its repo at the forge. So
  `allowed_hosts` is defense-in-depth against a tampered manifest repointing a credential at an
  attacker *host*, layered on top of the credential's repo scope — it is not itself the repo gate.
- **No implicit trust of transport-layer signals:** redirects, DNS, and proxy hints are never
  authority (see §3); only the signed manifest is.

### 5. Supported repository forms & git-feature scope (findings 12–15)

To keep the mechanism honest, the ADR declares what it covers and **fails validation loudly** on the
rest rather than silently mis-handling it:
- **Submodules:** `.gitmodules` URLs are resolved from the manifest too (or anchored as relative URLs
  to the resolved parent remote). A repo with unresolved/hardcoded submodule URLs fails validation.
- **Git LFS:** LFS endpoint/auth is resolved alongside the git remote, or LFS is declared unsupported
  and validation fails on its presence — never "source from mirror, LFS silently stuck on primary."
- **Shallow/partial clones + mirror completeness:** a `mirror` must advertise the configured
  protected refs/tags; fallback verifies the requested ref is available on the mirror before using
  it. Shallow-clone ancestry checks are treated as best-effort with explicit reporting.
- **Repository forms:** normal working clones are supported; bare repos, linked worktrees, and
  detached-HEAD/no-upstream states are enumerated with defined behavior (or declared out of scope +
  validated), so blind remote regeneration can't mutate the wrong git config.

### 6. Downstream propagation (T-1999)

The resolver + a generic schema ship in the seed via `neutralize_seed.py` (generator-side, T-1981
discipline). The shipped manifest is a **reference example with DISABLED / invalid-placeholder-domain
entries (finding 11)** — never real Pantheon/GitHub URLs — and first-run validation must pass before
any git operation, so an operator who hasn't replaced the URLs cannot clone/push against the wrong
upstream. Resolver + self-heal ship generic; no operator URLs baked in. This closes the T-0915
external-user gap: once the ~5 collaborators resolve via manifest, a future rename is zero-touch.

## How this dissolves the rename problem

For anyone resolving via manifest, a GitHub rename is **cosmetic** — GitHub is a `mirror`, and a
one-line manifest `url:` update (or the redirect, used only as a re-read-the-manifest signal) keeps
it reachable. The internal fleet (self-hosted `primary`) doesn't notice. So T-0915 is sequenced
*after* this (T-1996) for the internal fleet and after T-1999 for external users — a non-event, not a
break requiring notification.

## Scope boundaries (NOT decided here)

- **Self-hosted git server choice** (Gitea/Forgejo/…) — T-1997.
- **Writes/push failover + primary↔mirror sync/divergence policy** — T-1998 (this ADR is read
  failover only; §2).
- **The rename itself** — T-0915, still the operator's go.
- **Self-hosted host credential provisioning** — bringup/T-1997.

## Open questions for the operator (ratification)

1. **Preference default:** self-hosted-primary + GitHub-mirror is proposed. Confirm — or GitHub-
   primary during a transition period?
2. **Manifest integrity mechanism:** signed commits on the config repo vs a pinned-hash check — do
   you have a preference, or leave the choice to T-1996's build with signing as the default lean?
3. **External-collaborator rollout:** ship via the seed (T-1999) + a one-time direct nudge to the ~5
   to adopt the manifest-aware config. Acceptable, or a different distribution path to them?

### Addendum 2026-07-24 (Orchestrator) — the three questions, re-read after the server actually went live

The list above was written on 2026-07-23, **before** the primary existed. It stood up on 2026-07-24
(T-1997: Forgejo v16.0.1, CT 200 on pve2, verified HTTP 200 from swarm). Two of the three look
different in that light. **No design above changes** — this only sharpens what is being asked, so
the ratify is cheaper. Design under review is unmodified.

**Q1 — my lean has FLIPPED to: GitHub stays primary at first. Self-hosted-primary is the right end
state, just not on day one.** Three facts, all post-stand-up:

- **Read-failover protects reads, not writes.** This ADR is explicitly read-failover-only (writes
  deferred to T-1998). So a primary outage degrades reads gracefully and **blocks writes**. Naming
  self-hosted primary on day one puts every agent's push behind the newest, least-proven component
  in the fleet.
- ~~**Its reachability is still being built.**~~ **RETIRED 2026-07-28 — this bullet's premise is
  FALSE and the objection is withdrawn** (Specialist flagged it, T-2193; I verified at the surface:
  `curl https://git.your-self-hosted-git.example/` returns **HTTP 303 in 0.30s**). The front door **does** exist. Per
  the TG-4078 ruling the primary goes public-facing with auth-to-pull fronted by caddy, and the
  manifest `url:` becomes that public DNS identity (still identity-not-IP, so finding-6's binding is
  satisfied unchanged; see §7-REVISED of the T-1997 spec) — and that identity now resolves and
  answers. So "the only path to CT 200 is CT 101's subnet route, the SPOF that already dropped once"
  no longer holds.
  **Direction of the correction:** this bullet argued FOR GitHub-primary-on-day-one; retiring it
  removes an argument for the conservative order rather than adding one. **Q1's lean does not flip
  back**, because bullets 1 and 3 are independent of reachability and both still hold — writes are
  still unprotected by a read-failover design, and the server still has near-zero operational
  history with a new public attack surface. The lean now rests on two facts instead of three, and
  one former objection is discharged.
- **Zero operational history, and now a public attack surface.** The server is hours old, and going
  public means it needs hardening a tailnet-only service never did (registration disabled,
  login rate-limiting, TLS at caddy, and a deliberate answer on whether anything is anonymously
  readable — default no). Nothing is wrong with it; there is simply no evidence yet either way, and
  the ruling **strengthens** this bullet rather than weakening it: a brand-new *public* service is
  less proven than a brand-new internal one.

**Why this costs nothing:** under this very design, preference is *list order* in the manifest, so
flipping GitHub→self-hosted later is a **one-line change, reversible by construction**. The
architectural win (name↔URL decoupling) lands immediately and is independent of which remote is
primary. So the conservative order is free — we are not trading away the sovereignty goal, only
sequencing it behind evidence. Proposed sequence: ratify → wire both remotes with **GitHub primary**
→ prove read-failover on the pilot → stand up the public front door (T-2007, reshaped) → **then** flip preference as
its own supervised-first-fire, which is what T-1998 already promises for the cutover. If you'd
rather take self-hosted-primary immediately, that is a legitimate call — it buys sovereignty sooner
at the cost of writes riding an unproven path — but I would not choose it for you.

**Q2 — withdrawn as a question; I am adopting signing as the default and will not ask.** On
re-reading, this is a build-tier implementation detail below the goal-level bar (msg 3733: below-
goal-level ratify asks are retired; I adopt under P/Ds + ledger). The honest trade for the record:
signed commits are the stronger guarantee but carry real ongoing key-management cost across
orchestrator/specialist/recovery/the seats; a pinned-hash check is cheaper but needs an out-of-band trusted channel
for the hash. Default = signing, with the mechanism finalised at T-1996 build; if the key-management
cost turns out to bite at implementation, T-1996 may land pinned-hash instead and justify it in its
DECISIONS. **Nothing needed from you here — this is one fewer thing in your queue.**

**Q3 — genuinely yours, and it should be decided together with T-0915.** This one touches the ~5
external collaborators, and externals are reserved to you. Flagging the coupling: **T-0915 (the repo
rename) hits the same population** — a rename breaks their remotes, and this question asks how they
adopt manifest-aware config. Deciding them separately risks nudging the same five people twice, or
worse, renaming under them before they hold the manifest that would have absorbed it. If the
manifest reaches them *first*, the rename becomes a one-line manifest update on their side rather
than a broken remote — which is the entire point of the indirection, and an argument for
sequencing Q3 ahead of the rename.

**Net: two questions for you (Q1, Q3), not three — and Q1 arrives with a recommendation that
reversed itself on evidence.**

## DECISIONS

- **Manifest indirection over hardcoded remotes** — justifies-via **P1** (structural logical-name→URL
  decoupling) + **D10** (references-not-transclusion; manifest is the single URL source). Reuses the
  ORG-028 manifest.
- **Clone-time generation + managed-remote markers + self-heal** — justifies-via **P1** (native git
  semantics, inspectable state) + **P6** (the stale-between-syncs tension handed to a *bounded,
  managed-only* self-heal, not collapsed) + the false-RED discipline (surface a gone remote).
- **Self-heal repoints only on confirmed-permanent identity change; redirects never authority** —
  justifies-via **D11**/**P4** (verify at the defining surface — the trusted manifest — not a
  transport-layer signal) + the security model §4. *This is the finding-4 fix: the ADR must not
  reintroduce the redirect-ambiguity it exists to solve.*
- **Read-failover only; writes/sync deferred to T-1998** — justifies-via **P6** (write divergence is
  a real tension, named + sequenced, not hand-waved as "keeps working").
- **Manifest integrity + auth-to-host binding** — justifies-via **ORG-039** (secrets-by-reference) +
  **P4** — a load-bearing manifest needs a trust boundary or it's a credential-misuse vector.
- **Codex ORG-041 review before ratification** — justifies-via **ORG-041** (ratified codex-as-spec-
  reviewer) — NEEDS-REWORK verdict taken seriously; all 15 findings applied, not waved off.
- **HO-drafts / operator-ratifies** — justifies-via **D3** (distribution/product axis).

## §7. Codex review disposition (2026-07-23)

Codex (gpt-5.5) returned NEEDS-REWORK with 15 falsifiable findings. All applied: failover execution
contract + reachability (1,10 → §2); managed-remote markers, no-data-loss (2 → §3); trigger-class
split (3 → §3); redirect-never-authority (4 → §3/§4, security-critical); manifest integrity (5 → §4);
auth-to-host binding (6 → §1/§4); read-failover-only, writes→T-1998 (7 → §2); repo-local lock (8 →
§3); schema invariants (9 → §1); reachability algorithm (10 → §2); seed placeholder/disabled +
first-run validation (11 → §6); submodules/LFS/shallow/repo-forms (12–15 → §5). The high-level
indirection goal and the bounded-one-retry rule were confirmed sound; the rework was in the security,
execution, and edge-case specificity — which is what a builder needs to implement without guessing.

## Relationship to existing ADRs

- **ORG-028** gave the manifest; this makes its `repos.yaml` load-bearing for remote resolution.
- **ORG-039** gave the distribution model; §6 rides its manifest-is-instance-data + secrets-by-
  reference rules.
- **ORG-041** gave the codex spec-review loop; §7 is its application here.
- **T-1871** is the Proxmox-primary/dead-man umbrella; §2–§3 are the resolution mechanism that makes
  its failover real.
