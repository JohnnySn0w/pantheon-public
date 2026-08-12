# Refreshing an ORG-039 release mirror (build leg + handoff)

## When to use this

You are the **build leg** (recovery) for one of the four pairs in `mirror-drift-pairs.yaml`, and you are about to change what a release mirror publishes — either porting an upstream delta, or landing a correction directly on the mirror's build branch.

Applies to all four pairs. They share one shape: a **genericized derivative** whose history is DISJOINT from its source, published by **orchestrator** (`publish_via`, per T-1673 push-is-HO-exclusive), built by **recovery** (`refresh_target` — the builder is the first actor, ORG-039 §3).

Do NOT use this for: reading a mirror to measure drift (the sweep is anchor-based and never reads a mirror — see `tools/mirror_state.py`), or changing a mirror's visibility/branding (operator-initiated only, standing boundary from T-1983).

## Why this SOP exists at all

The procedure was previously recoverable only by reading two resolve notes (T-1454's and T-1983's), and T-1983's own retarget note flagged that as worth fixing. Then T-1988 paid for it twice: once in a near-miss that would have deleted another agent's published work, and once in an anchor that went unrecorded across two consecutive publishes.

Note the second one carefully, because it is the more instructive failure. **The anchor rule was not missing.** `tools/mirror_state.py`'s module docstring states it explicitly: the build leg records a staged bundle via `record-staged --source-head <sha>`, and the source sha "MUST travel in the bundle handoff." The rule was written down in a tool the build-leg actor never opens. A rule filed where the actor does not look is, operationally, not a rule — which is the whole reason this file is a procedure and not another docstring.

## Procedure

### 1. Establish which tree you are building from, and fetch the mirror

```bash
cd <build-clone>                      # e.g. ~/work/pantheon-pwa
git fetch origin                      # the SOURCE repo
git fetch mirror                       # the MIRROR remote — do not skip
git log --oneline -3 mirror/main
```

**Verification:** you know the live mirror head sha before you construct anything.

**Why:** the publish commit is built with `commit-tree <your-tree> -p <mirror-head>`, which publishes YOUR tree wholesale. A stale local `mirror/main` is not a warning — it is a silent deletion waiting to happen (step 2).

**Where `<your-tree>` comes from — the `genericize` branch, not raw source (pwa; T-2465).** The mirror's genericization is NOT a script you re-run; for the pwa pair it lives as commits on a persistent local branch `genericize` = `source@last-anchor` + the genericization layer on top (e.g. `a072965` "genericize auth + strip instance internals" renames `src/auth/tailscale.ts` → a proxy-agnostic `src/auth/identity.ts`, plus generic `DEPLOY.md`/`.env.example`, an `EXAMPLE` `scripts/sudoers/kino`, and a public README rewrite). To refresh you **merge the source delta INTO `genericize`** (`git merge origin/main` on a working branch off it) and resolve conflicts so the genericization survives; `commit-tree` then publishes that merged tip's tree. A wholesale `commit-tree` of `origin/main` would REVERT the entire genericization (reintroducing Tailscale coupling + stripped instance internals into the public repo) — a leak, not just a regression. The 3-way merge preserves genericization structurally on its own (our deletions/additions hold where source didn't touch them); your job is the *content* conflicts and the leak scan (step 4). `mirror-publish` is the separate disjoint orphan-squash lineage (= `mirror/main`); do not build on it.

**Build from the ACTUAL current source head, not the ticket's.** The drift-sweep stamps `current source head` when it FILES the ticket; source moves on. Read `git rev-parse origin/main` fresh — on T-2465 the ticket said 4 commits / `3112ff7` but source had advanced to 12 commits / `a3b7ce9`. Build the real head or the mirror is stale on arrival.

### 2. TWO drift checks against the live mirror — commit-level AND content-level

Both are mandatory. They catch different things, and the second one is the one people skip.

**2a. Commit-level — is anything on the mirror absent from your build branch?**

```bash
git add -A                                    # stage your intended change
git diff --cached mirror/main --stat          # every line here must be intended
```

Account for **every** file listed. Anything you did not intend to touch is a mirror-side commit your branch never had, and publishing would erase it with no conflict and no warning.

*Live case (T-1988):* mirror head `4b04dc7` carried orchestrator's T-1987 provenance note; the build branch predated it. The diff surfaced it; it was carried forward verbatim. Nothing in git would have objected otherwise.

**2b. Content-level — are there mirror-only divergences inside files this refresh rewrites?**

A commit-level diff is structurally blind to this. When a refresh ports an upstream delta, it *legitimately* rewrites files — so a correction that exists only on the build branch, and not upstream, gets rebuilt from a source that never had it. The loss looks like intended work.

Before porting any upstream delta, enumerate the mirror-only divergences (corrections not yet upstream) and re-verify each one **survives** the port.

*Live case (T-1988 → T-2133):* six doc-vs-code corrections were authored on the build branch while the source head never moved, so the mirror is currently MORE correct than its upstream. The next real delta port can revert all six.

**The structural fix, not the workaround:** push corrections UPSTREAM rather than curating a mirror-side patch set. A patch set is a liability with a half-life — it is the version someone eventually drops in a hurry. Carry a divergence only for as long as it takes to get it upstream, and track it in a ticket while it exists.

### 3. Run the gates on the build tree

```bash
<backend suite>          # pwa: cd <root> && bun test
<frontend suite>         # pwa: cd <root>/web && bunx vitest run
<production build>       # pwa: bun run build:web && bun run smoke:build
```

**Always prefix suite commands with an absolute `cd`.** Tool-issued shell calls share one persistent working directory, so a sibling `cd` can silently relocate a suite run — in a repo with two runners the wrong-cwd run *succeeds* and returns a credible wrong number. A T-1988 run reported a regression that did not exist for exactly this reason.

**Establish the baseline BEFORE editing**, and record known-failing tests by name. "N pass / M fail" is meaningless without it; a pre-existing failure read as a regression costs an hour, and a real regression hidden among pre-existing noise costs more. On T-2465 the sole backend failure was a pre-existing integration test that hardcodes `/home/claude/pantheon-config` (EACCES cross-account) and ignores `PANTHEON_CONFIG_ROOT` — identical on the pristine source baseline, so not a refresh regression; a baseline worktree at `origin/main` is the fastest way to prove that.

**`bun x tsc` EMITS `.js` files next to the sources** (the T-2129 tsconfig has no `noEmit`). If you run a typecheck as an extra gate, a later `git add -A` will sweep ~50 compiled `.js` artifacts into your commit — a huge, leaky bloat (minified strings trip the `claude` scan). After any tsc run: `git clean -f` the untracked `.js` (verify nothing non-`.js` is untracked first), or stage explicit paths instead of `-A`. The tell is an added-line count that jumps an order of magnitude between scans.

**For a change gated on a test outcome** (e.g. deleting fixture setup), the load-bearing evidence is a **pass/fail set identical before and after**, not a green run. Stash the change, re-run, compare test *names*. T-1988 deleted a dead fixture on exactly this evidence in a suite that was never green.

### 4. Leak gate — scan the ADDED LINES, and include the blind spot

```bash
git diff --cached | grep '^+' | grep -v '^+++' > /tmp/added.txt
```

Sweep for: operator name (`operator`), the role label **`orchestrator`**, real email, **bare `claude`**, `claude-fallback` / `claude-research` / `claude-telegram` / `codex` / `exocortex` / `suborch[12]` / `the-ancestor`, `/home/<account>` paths, host names (`boxon`/`pve2`/`gpu-ml`/…), tailnet `100.x` addresses, private IPs, key material.

**The genericization name-map (pwa), so you know what "clean" means:** `operator` → `operator`; `orchestrator` → `orchestrator`; agent codenames `specialist` / `recovery` / `orchestrator` are **KEPT** (public-safe); `T-NNNN` / `ADR-NNN` / `ORG-NNN` / `msg_NNNN` refs are **KEPT** (provenance). A de-genericization slip reads as source RE-INTRODUCING a mapped token on a line the mirror had already genericized (the `-` line is correct, the `+` line regresses it) — most likely in an **auto-merged** file you never conflict-resolved. On T-2465 the self-scan omitted `orchestrator` as a class entirely and missed two such slips in `web/tests/e2e/fixtures.ts`; the independent adversarial pass (below) caught both. Add every mapped token to your scan, not just the ones you happened to hand-edit.

**`claude` bare is mandatory and is the documented blind spot.** T-1983's own identity sweep checked `claude-fallback` and `claude-research` and never checked bare `claude`; an independent adversarial pass found four survivors, one rendered into shipped UI. A self-authored gate inherits its author's blind spot — which is what the done-bar's independent-verification requirement is actually for. **Run the independent pass** (a fresh agent, given the name-map and the diff, told to be adversarial) — it is not optional theater; it earned its keep on both T-1983 and T-2465.

`T-NNNN` / `ADR-NNN` refs are **NOT leaks** — T-1987 ratified them as provenance-only, treated by a standing README note. Do not add them to the fail gate; gating on non-secret provenance noise blocks refreshes for nothing.

### 5. Ensure the T-1987 provenance note is present (idempotent)

Every refresh must leave the standing "A note on references" section in the mirror's top-level `README.md`. Add if missing, leave if present. Canonical text: `notes/2026-07-27-mirror-dangling-refs-convention.md` (adjust the `adr/` clause per mirror).

### 6. Construct the publish commit — orphan-squash, fast-forward, never force

```bash
TREE=$(git rev-parse <your-commit>^{tree})
PUB=$(git commit-tree "$TREE" -p <live-mirror-head> -m "<message>")
git merge-base --is-ancestor <live-mirror-head> "$PUB" && echo FF-SAFE
```

**Verification:** `--is-ancestor` passes, so the mirror fast-forwards and **no force-push is ever needed**. If it fails, stop — do not reach for `--force`.

**Why `commit-tree` and never a branch bundle:** the source repo's build branch carries ~160 commits authored under the operator's real GitHub identity in git *metadata*. A branch bundle would publish that identity ~160 times plus every pre-genericization file state, and **no content scan would catch it**, because every scan reads the working tree and the working tree is clean. `commit-tree` against the mirror head publishes one commit with a generic author onto the disjoint mirror lineage.

Use the generic author already established on the mirror lineage (`recovery <recovery@pantheon.local>` for the pwa pair) — check `git log --format='%an <%ae>'` on the mirror, do not assume.

### 7. Bundle with a single ref, and verify by CLONING it

```bash
git init -q --bare /tmp/pub.git
git push -q /tmp/pub.git <publish-commit>:refs/heads/main
git --git-dir=/tmp/pub.git bundle create /tmp/<name>.bundle main
git bundle list-heads /tmp/<name>.bundle      # must show exactly ONE ref
sha256sum /tmp/<name>.bundle
git clone -q /tmp/<name>.bundle /tmp/verify
cd /tmp/verify && git log --format='%an <%ae>' | sort -u   # authors, FULL history
```

**Verification:** read the CLONE's log and author set — do not trust the construction. Confirm the history is the short orphan-squash lineage, not the source repo's.

Then **re-run the gates against the fresh clone**, not the working tree. A working tree can pass on state that never made it into the bundle.

Drop the bundle at a path the publisher can read (`/tmp/<name>.bundle`, mode `0644` — the established convention; `/tmp` is world-readable and this matches prior cross-agent bundle drops).

### 8. Record the STAGED bundle — build-leg step, do not skip

```bash
python3 tools/mirror_state.py record-staged <slug> \
    --source-head <verified-sha> --bundle-path /tmp/<name>.bundle
```

**This is the step that was missed on both T-1983 and T-1988.** It powers the built-but-unpushed alert. Skipping it does not fail loudly — it just means the drift sweep is blind until the publisher records.

The `--source-head` is the **SOURCE repo commit the mirror content was built from**, and it must be a VERIFIED commit:

```bash
git rev-parse origin/main                     # full 40-char sha
git cat-file -t <sha>                         # MUST print "commit"
```

**A checksum is not a sha.** On T-1988 the publisher went looking for the source head in T-1983's resolve note, found a 40-hex string, and nearly recorded it as the anchor — it was the truncated bundle sha256. Both are 40 hex characters; only one is a commit. `git cat-file -t` is the one-command discriminator, and it is why the source sha and the bundle checksum should never be adjacent in prose without labels.

If the source head has NOT moved (you authored on the build branch and ported no upstream delta), the correct value is the **unchanged** existing anchor. That is not staleness. Verify zero drift from the other direction too: `git log --oneline <build-branch>..origin/main` should be empty.

### 9. Hand off to the publisher, with the loose ends visible

Mailbox orchestrator (`/home/the-ancestor/mailbox/orchestrator`) with:

- bundle path + **sha256** + single-ref sha
- the **verified source head**, labelled as such and stated separately from the checksum
- confirmation `--is-ancestor` passes against the CURRENT mirror head (name the sha)
- gate results **from the fresh clone**, with pre-existing failures named
- anything you carried forward from the mirror side (step 2a)
- **unresolved loose ends, explicitly.** An unreproduced anomaly reported is a datum; the same anomaly omitted is a trap for whoever hits it next.

### 10. Confirm the anchor landed

The publisher runs `record-published <slug> --source-head <sha> [--mirror-head <sha>]`. **Do not resolve your ticket without checking it happened:**

```bash
python3 tools/mirror_state.py get <slug>
```

**Verification:** `published_mirror_head` is the new head, `source_head_at_last_publish` is your verified source sha, `staged_bundle` is `null`. Resolving without this read is half of why the anchor went unrecorded twice — the recording lives on the publisher's leg, but the consequence lands on the drift sweep, which is the build leg's.

## Common failure modes

| Failure | Early signal | Fix |
|---|---|---|
| Publish erases a mirror-side commit | none — clean FF, no conflict | step 2a, every time |
| Upstream port reverts a mirror-only correction | none — looks like intended rewrite | step 2b + get it upstream |
| Branch bundle leaks operator identity via git metadata | none — content scans read a clean tree | step 6 `commit-tree` only |
| Checksum recorded as source head | anchor "looks" set | step 8 `git cat-file -t` |
| Anchor never recorded | sweep silently blind, no error | steps 8 + 10 |
| Suite run in the wrong directory reads as a regression | plausible wrong test count | absolute `cd`, always |
| Leak gate misses bare `claude` | gate reports clean | step 4, explicitly |
| Pre-existing failure read as a regression | "my change broke it" | baseline BEFORE editing |

## Boundary

Covers the **build + gate + bundle + handoff** legs and the anchor contract. Does NOT cover: the publish push itself (orchestrator's leg — `git push mirror`), visibility flips or public branding (**operator-initiated only**, standing boundary), the weekly drift sweep's own operation (`tools/mirror_drift_sweep.py`, T-1994), or genericization *substitutions* (`tools/seed_genericize.py` is the **claude-config seed's** gate with its own leak classes — do not misapply its `T-NNNN`/date patterns to the pwa mirror, which deliberately keeps those).

Recovery holds a working read-write deploy key for the pwa mirror (T-1983). **Capability is not authorization** — pushing an outward-facing repo trips the external-facing prong of the act-vs-ask test, and the T-1475 division routes the publish through orchestrator regardless.

## Cross-references

- [[ORG-039]] §3 — harness-config distribution model, genericization + release gates
- `mirror-drift-pairs.yaml` — the four pairs, owners, routing, gates
- `mirror-published-state.yaml` + `tools/mirror_state.py` — the anchor + its contract
- `notes/2026-07-27-mirror-dangling-refs-convention.md` — T-1987 provenance-note convention
- T-1454 (initial pwa mirror build), T-1983 (first refresh; the orphan-squash finding), T-1987 (dangling refs), T-1988 (the erase near-miss + anchor gap), T-1994 (drift sweep), T-2133 (port the divergence upstream), T-1673 (push-is-HO-exclusive), T-1475 (build/publish division)
- `sop/standard/cross-account-ops-discipline.md` — cross-account read/write patterns
