# Refreshing an ORG-039 release mirror (build leg + handoff)

## When to use this

You are the **build leg** (recovery) for one of the four pairs in `mirror-drift-pairs.yaml`, and you are about to change what a release mirror publishes. That is either a refresh (typically a sweep-filed `mirror-refresh` ticket) or a correction to what the mirror ships.

Applies to all four pairs. They share one shape: a **genericized derivative** whose history is DISJOINT from its source, published by **orchestrator** (`publish_via`, per T-1673 push-is-HO-exclusive), built by **recovery** (`refresh_target` — the builder is the first actor, ORG-039 §3).

**Two pair classes, two build legs (T-4768 changed).** Check the pair's `generator:` key in `mirror-drift-pairs.yaml` before step 1:

| Class | Pairs | Where the mirror content comes from | Build leg |
|---|---|---|---|
| **Generator pair** | `claude-harness-mirror` (claude-code-config), `codex-harness-mirror` (codex-config-template) | GENERATED from one pinned pantheon-config sha by `tools/build_harness_mirror.py`, from `harness/<h>/mirror/manifest.yaml` (generated files, shadows, overlay). ORG-091 §2. | Steps marked **[G]** |
| **Legacy pair** | `pwa-release-mirror`, `pantheon-public-mirror` | Hand-built: pwa from its persistent `genericize` branch; pantheon-public from the weekly T-1327 scratch build plus deep-verify (`gated: true`). | Steps marked **[L]** |

Steps with no marker apply to both. The publish leg (steps 6-10: commit-tree orphan-squash, fast-forward, one-ref bundle, verify by cloning, anchor record, handoff) is the same for both classes.

Do NOT use this for: reading a mirror to measure drift (the sweep is anchor-based and never reads a mirror — see `tools/mirror_state.py`), or changing a mirror's visibility/branding (operator-initiated only, standing boundary from T-1983).

## Why this SOP exists at all

The procedure was previously recoverable only by reading two resolve notes (T-1454's and T-1983's), and T-1983's own retarget note flagged that as worth fixing. Then T-1988 paid for it twice: once in a near-miss that would have deleted another agent's published work, and once in an anchor that went unrecorded across two consecutive publishes.

Note the second one carefully, because it is the more instructive failure. **The anchor rule was not missing.** `tools/mirror_state.py`'s module docstring states it explicitly: the build leg records a staged bundle via `record-staged --source-head <sha>`, and the source sha "MUST travel in the bundle handoff." The rule was written down in a tool the build-leg actor never opens. A rule filed where the actor does not look is, operationally, not a rule — which is the whole reason this file is a procedure and not another docstring.

**Why generator pairs changed the build leg (T-4768, ORG-091 §2).** On the hand-ported path, the genericization lived as commits on a local `genericize` branch, and three of the failures below (a reverted mirror-only correction, a reverted previous refresh, a missed leak token) were the cost of keeping that layer correct by hand. For generator pairs it is now declared in pantheon-config: substitutions and literal rules with fail-closed counts, whole-file shadows pinned to the source blob they were reconciled against, and overlay files. The same source sha always yields the same payload tree. Recovery now runs the generator instead of porting by hand. The publish leg does not change.

## Procedure

### 1. Establish which tree you are building from, and fetch the mirror *(T-4768 changed: [G] pin a source sha; [L] unchanged)*

**[G] Pin the CURRENT pantheon-config source sha and fetch the mirror.**

```bash
git -C ~/pantheon-config fetch origin
SRC=$(git -C ~/pantheon-config rev-parse origin/main)     # full 40-char sha; origin/main tip, NOT your checkout's HEAD
git -C ~/pantheon-config cat-file -t "$SRC"               # MUST print "commit"
git -C ~/pantheon-config worktree add --detach /tmp/mirror-src-"${SRC:0:12}" "$SRC"
G=/tmp/mirror-src-"${SRC:0:12}"/tools/build_harness_mirror.py   # run the tools AS OF $SRC

```

**[G] Get the live mirror head from an orchestrator-handed bundle (the build leg has no read path to the generator mirrors).** Recovery holds no key for claude-code-config or codex-config-template, and `mirror-drift-pairs.yaml` records this ("Recovery has no read path to this mirror"). So the live head reaches the build leg as a bundle. Ruling: orchestrator 2026-10-09, T-4768 PR6 B1, option (a). It needs no new access grant and keeps the split of build=recovery and publish=orchestrator (T-1673/T-1475).

1. **Recovery** asks orchestrator (mailbox) for a live-mirror bundle for `<slug>`.
2. **Orchestrator** (publish leg, which has the mirror remote):

   ```bash
   git -C <mirror-clone> fetch origin
   git -C <mirror-clone> bundle create <shared path>/<mirror>-live-<date>.bundle origin/main
   git -C <mirror-clone> rev-parse origin/main          # mail recovery this sha + the bundle path
   ```

3. **Recovery** fetches the live head from the bundle and checks it against the mailed sha before running steps 2a, 6, 7 and 10:

   ```bash
   cd <mirror-clone>                     # recovery's local clone of the MIRROR, e.g. ~/work/claude-code-config (no remote needed)
   git bundle list-heads <shared path>/<mirror>-live-<date>.bundle   # one ref: refs/remotes/origin/main
   git fetch <shared path>/<mirror>-live-<date>.bundle 'refs/remotes/origin/main:refs/remotes/live/main'
   LIVE=$(git rev-parse live/main)
   [ "$LIVE" = "<sha from orchestrator's mail>" ] && echo LIVE-OK    # MUST print LIVE-OK; otherwise STOP and ask orchestrator
   git log --oneline -3 "$LIVE"
   ```

A bundle older than the live head would let step 6 publish on a stale parent. Orchestrator catches that at publish time, because `commit-tree -p $LIVE` must still be a fast-forward of the mirror's `origin/main` (step 9 names `$LIVE`). This read path also removes an old hidden dependency: before it, the build leg never had `$LIVE`, and orchestrator had to re-make publish commits (68efe43 vs bundle tip 5f05df75).

Record `$SRC` and `$LIVE`. Both travel in the handoff (step 9).

**Why `origin/main` and not the ticket's sha or your checkout's HEAD:** the drift sweep builds from the sweep host's checkout `HEAD` at filing time, and source moves on. Build the real tip or the mirror is stale on arrival (T-2465: the ticket said 4 commits, source had 12).

**Why run the tools from a worktree at `$SRC`:** the LEAKGATE verdict line's `tool=` field hashes the `build_harness_mirror.py` and `mirror_leak_gate.py` files on disk, not at `$SRC`. Running them from `$SRC` makes the verdict cite the gate version that was actually in force at the pinned sha. The manifest, shadows, overlay and leak-gate pattern sources b-e are always read from git at `--source-sha`. Never pass `--manifest-path` for a publish build: it reads the mirror config from disk instead of the pinned sha.

**[L] Fetch both remotes in the build clone.**

```bash
cd <build-clone>                      # e.g. ~/work/pantheon-pwa
git fetch origin                      # the SOURCE repo
git fetch mirror                       # the MIRROR remote — do not skip
git log --oneline -3 mirror/main
```

**Verification:** you know the live mirror head sha before you construct anything.

**Why:** the publish commit is built with `commit-tree <your-tree> -p <mirror-head>`, which publishes YOUR tree wholesale. A stale local `mirror/main` is not a warning — it is a silent deletion waiting to happen (step 2).

**[L] Where `<your-tree>` comes from — the `genericize` branch, not raw source (pwa; T-2465).** The mirror's genericization is NOT a script you re-run; for the pwa pair it lives as commits on a persistent local branch `genericize` = `source@last-anchor` + the genericization layer on top (e.g. `a072965` "genericize auth + strip instance internals" renames `src/auth/tailscale.ts` → a proxy-agnostic `src/auth/identity.ts`, plus generic `DEPLOY.md`/`.env.example`, an `EXAMPLE` `scripts/sudoers/kino`, and a public README rewrite). To refresh you **merge the source delta INTO `genericize`** (`git merge origin/main` on a working branch off it) and resolve conflicts so the genericization survives; `commit-tree` then publishes that merged tip's tree. A wholesale `commit-tree` of `origin/main` would REVERT the entire genericization (reintroducing Tailscale coupling + stripped instance internals into the public repo) — a leak, not just a regression. The 3-way merge preserves genericization structurally on its own (our deletions/additions hold where source didn't touch them); your job is the *content* conflicts and the leak scan (step 4). `mirror-publish` is the separate disjoint orphan-squash lineage (= `mirror/main`); do not build on it.

**[L] pwa: run the option-2 scrub on the merged tree, every refresh (T-3316, the operator's T-2131 ruling).** After the merge into the working branch off `genericize` and before any gate, run `python3 ~/pantheon-config/tools/pwa_mirror_scrub.py <build-tree>`. It maps persona names to roles (Orchestrator/Specialist/Recovery -> orchestrator/researcher/recovery, and the seed's words for the others), drops `pantheon-config@<sha>` shas, `msg_NNNN` refs and the `swarm` hostname, maps `orchestrator` -> orchestrator, keeps T-/ADR-/ORG- refs, and applies its fail-closed `FIXUPS` for tests whose data the rename changes. It must print every class at 0 and exit 0. Exit 2 means an upstream change moved a fixup: update `FIXUPS` by PR, never by hand on the build tree. Re-check with `--check` on the bundle clone at step 7.

**[L] Build from the ACTUAL current source head, not the ticket's.** The drift-sweep stamps `current source head` when it FILES the ticket; source moves on. Read `git rev-parse origin/main` fresh — on T-2465 the ticket said 4 commits / `3112ff7` but source had advanced to 12 commits / `a3b7ce9`. Build the real head or the mirror is stale on arrival.

**[G] Retired build branches (W5).** The claude `genericize` branch (`5f05df7`) and the codex local `main` lineage (`a313006`) are NOT build inputs for generator pairs. They stay in place, untouched, until the first generated release reproduces the published trees AND is published (PR7). After that recovery retires them and keeps the archive bundles. Until then, do not build on them and do not advance them.

### 2. Drift checks against the live mirror *(T-4768 changed: [G] 2a/2b run the generator; [L] unchanged)*

Both checks are mandatory. They catch different things, and the second one is the one people skip.

**2a [G]. Does the live mirror still equal what the recorded anchor builds?** This is the structural catch for a mirror-side hand-landing. Run it BEFORE building at `$SRC`:

```bash
MS=~/pantheon-config/tools/mirror_state.py
ANCHOR=$(python3 "$MS" get <slug> | jq -r .source_head_at_last_publish)
LIVE_TREE=$(git -C <mirror-clone> rev-parse "$LIVE^{tree}")
python3 "$MS" get <slug> | jq -r .published_payload_tree   # must equal $LIVE_TREE
python3 "$G" check --mirror <claude|codex> --source-sha "$ANCHOR" \
    --expect-tree "$LIVE_TREE" --against-repo <mirror-clone>
```

**Verification:** `check` exits 0 (`OK tree …`, or for claude, `OK built … vs expected …` with the one `ALLOWLISTED agents/researcher.md` line while the live tree is still `aea962e7`). Also, the recorded `published_payload_tree` equals the live tree.

- **Exit 1 (`MISMATCH <path>` lines), or a recorded tree that differs from the live tree:** the live mirror carries something the anchor does not build. That is a hand-landing (§Hand-landings) or an unrecorded publish. **STOP.** Fold it into pantheon-config first, as a shadow or overlay edit in `harness/<h>/mirror/` on an `agent/<ticket>` PR, so that the build at `$SRC` reproduces it. Otherwise the publish at step 6 erases it with no conflict and no warning. A generator change between `$ANCHOR` and `$SRC` can also show up here. Tell the two apart with `git log $ANCHOR..$SRC -- tools/build_harness_mirror.py tools/mirror_leak_gate.py`, and name the cause in the handoff.
- **Exit 2 / 4:** see §Sweep outcomes.

Then diff the build at `$SRC` against the live head. **Account for every path listed:**

```bash
python3 "$G" diff --mirror <claude|codex> --source-sha "$SRC" \
    --against "$LIVE" --against-repo <mirror-clone> > /tmp/<slug>-diff.txt
```

`diff` prints the LEAKGATE verdict to stderr, then `+++ only in build:`, `--- only in <live>:`, `mode …` and unified-diff hunks for each changed file on stdout. It exits 0 whether or not there are differences, and 0 on STALE too. It reports; it does not gate, so the gate is your accounting of every line. Each listed path should trace to a source commit in `git log $ANCHOR..$SRC -- <payload paths>`. A path that traces to nothing is a mirror-side change: go back to the first check.

*Live case (T-1988):* mirror head `4b04dc7` carried orchestrator's T-1987 provenance note; the build branch predated it. The diff surfaced it; it was carried forward verbatim. Nothing in git would have objected otherwise.

**2a [L]. Commit-level — is anything on the mirror absent from your build branch?**

```bash
git add -A                                    # stage your intended change
git diff --cached mirror/main --stat          # every line here must be intended
```

Account for **every** file listed. Anything you did not intend to touch is a mirror-side commit your branch never had, and publishing would erase it with no conflict and no warning.

*Live case (T-1988):* mirror head `4b04dc7` carried orchestrator's T-1987 provenance note; the build branch predated it. The diff surfaced it; it was carried forward verbatim. Nothing in git would have objected otherwise.

**2b [G]. Content-level — build at `$SRC` with no stale override; the STALE lines ARE the content-level check.**

```bash
python3 "$G" build --mirror <claude|codex> --source-sha "$SRC"     # stdout: payload tree; stderr: LEAKGATE line
echo "exit=$?"
```

On a generator pair, a mirror-only correction is not a commit that a port can overwrite. It is a **shadow**: a whole-file mirror version pinned to the source blob it was reconciled against, and copied byte-exact. When that source blob moves, the build reports `STALE <path>: source <spath> is <blob>, shadow pinned to <blob>` and exits 3. The divergence can no longer be reverted silently. The tool names it instead.

- **Exit 0:** no shadow is stale. Record the printed payload tree as `TREE` (it is recomputed at step 6).
- **Exit 3 (STALE):** for each STALE line, read the source change (`git diff <pinned_blob> <new_blob>`) and do one of two things in pantheon-config, on an `agent/<ticket>` PR that orchestrator merges:
  - **port** the change into `harness/<h>/mirror/shadows/<file>` and re-pin `source_blob` to the new blob; or
  - **re-pin** `source_blob` with no content change. That means "reviewed, not ported" and records the reviewed decision in the manifest. It replaces the ad-hoc anchor-only advances (`record-published` with an unchanged mirror head) that pre-T-4768 used for the same purpose.

  After the merge, restart at step 1 with the new `origin/main` tip. **Never pass `--allow-stale` to a publish build.** It exists for CI's HEAD leak check (`harness.yml`), where STALE belongs to the sweep.
- **Exit 2 / 4:** see §Sweep outcomes.

**The structural fix still stands:** push corrections UPSTREAM rather than curating a shadow. A shadow is a standing patch set with a half-life, and the shadow count is the metric to burn down by genericizing the source (C5 plan, Risks). Carry a shadow only as long as the source cannot be made generic, and track the burn-down in a ticket.

**Shadow and overlay files named `README.md` are worker-guarded (T-1345).** A subagent cannot write them. The seat commits them itself. Do not route around the guard (T-4768 T2).

**2b [L]. Content-level — are there mirror-only divergences inside files this refresh rewrites?**

A commit-level diff is structurally blind to this. When a refresh ports an upstream delta, it *legitimately* rewrites files — so a correction that exists only on the build branch, and not upstream, gets rebuilt from a source that never had it. The loss looks like intended work.

Before porting any upstream delta, enumerate the mirror-only divergences (corrections not yet upstream) and re-verify each one **survives** the port.

*Live case (T-1988 → T-2133):* six doc-vs-code corrections were authored on the build branch while the source head never moved, so the mirror is currently MORE correct than its upstream. The next real delta port can revert all six.

**The structural fix, not the workaround:** push corrections UPSTREAM rather than curating a mirror-side patch set. A patch set is a liability with a half-life — it is the version someone eventually drops in a hurry. Carry a divergence only for as long as it takes to get it upstream, and track it in a ticket while it exists.

### 3. Run the gates on the build tree *(T-4768 changed: [G] gates run on the materialized payload)*

**[G]** The generator's `build` already enforces default-deny coverage (`UNCLASSIFIED`), fail-closed literal rules (`expect_count`), shadow pins (STALE) and the leak gate. Its exit 0 is the first gate. Then run the mirror's OWN shipped gates against the materialized payload, not against pantheon-config:

```bash
rm -rf /tmp/<slug>-payload
python3 "$G" build --mirror <claude|codex> --source-sha "$SRC" --out-dir /tmp/<slug>-payload
( cd /tmp/<slug>-payload && bash provisioning/leak-scan.sh )            # both mirrors
( cd /tmp/<slug>-payload && python3 -m pytest -q tests/ )               # codex: the shadow-forked poller/keepgoing tests
```

The same baseline, absolute-`cd` and pass/fail-set rules below apply. Re-run these on the fresh bundle clone at step 7.

**[L]**

```bash
<backend suite>          # pwa: cd <root> && bun test
<frontend suite>         # pwa: cd <root>/web && bunx vitest run
<production build>       # pwa: bun run build:web && bun run smoke:build
```

**Always prefix suite commands with an absolute `cd`.** Tool-issued shell calls share one persistent working directory, so a sibling `cd` can silently relocate a suite run — in a repo with two runners the wrong-cwd run *succeeds* and returns a credible wrong number. A T-1988 run reported a regression that did not exist for exactly this reason.

**Establish the baseline BEFORE editing**, and record known-failing tests by name. "N pass / M fail" is meaningless without it; a pre-existing failure read as a regression costs an hour, and a real regression hidden among pre-existing noise costs more. On T-2465 the sole backend failure was a pre-existing integration test that hardcodes `/home/claude/pantheon-config` (EACCES cross-account) and ignores `PANTHEON_CONFIG_ROOT` — identical on the pristine source baseline, so not a refresh regression; a baseline worktree at `origin/main` is the fastest way to prove that.

**`bun x tsc` EMITS `.js` files next to the sources** (the T-2129 tsconfig has no `noEmit`). If you run a typecheck as an extra gate, a later `git add -A` will sweep ~50 compiled `.js` artifacts into your commit — a huge, leaky bloat (minified strings trip the `claude` scan). After any tsc run: `git clean -f` the untracked `.js` (verify nothing non-`.js` is untracked first), or stage explicit paths instead of `-A`. The tell is an added-line count that jumps an order of magnitude between scans.

**For a change gated on a test outcome** (e.g. deleting fixture setup), the load-bearing evidence is a **pass/fail set identical before and after**, not a green run. Stash the change, re-run, compare test *names*. T-1988 deleted a dead fixture on exactly this evidence in a suite that was never green.

### 4. Leak gate *(T-4768 changed: [G] the generator's fail-closed gate, cited by its verdict line; [L] unchanged)*

**[G] The gate is `tools/mirror_leak_gate.py`, invoked through the generator. It has no CLI of its own.** Every `build`, `check` and `diff` runs it over the WHOLE output (generated, shadows, overlay, file paths too) before printing anything. `scan --tree` runs the same gate over an existing tree. At step 6 you run it on the exact publish commit, and at step 7 on the bundle clone. Its pattern set is a UNION, and a missing source is a hard fail (exit 2), never "absent, skipped" (the T-4396 lesson):

- a: the mirror's own `overlay/provisioning/leak-scan.sh` patterns;
- b: `verify_seed.py` identity classes;
- c: identity derived from `instance.yaml` + `agent_role_map` (personas, aliases, accounts, `/home` paths, operator, ids);
- d: `harness/leak-gate/instance-leak-patterns.txt` (recovery's instance patterns; W1/W7);
- e: the byte-sequence check.

The verdict is ONE line. Quote it verbatim in the handoff:

```
LEAKGATE PASS mirror=<claude|codex> source=<full $SRC> tree=<payload tree> files=<n> leaks=0 allowlisted=<n> tool=<12hex>,<12hex> a=<sha256> b=<sha256> c=<sha256> d=<sha256> e=<sha256> d_file=<sha256> union=<sha256>
```

**Check before you quote it:** `source=` is `$SRC` and `tree=` is the tree you are publishing. The `union=` digest identifies the exact pattern set the tree passed.

**A leak (exit 4, `LEAKGATE FAIL`, redacted `LEAK <path>:<line> [<class>]` lines on stderr):** STOP. The tool prints only the pattern class and `path:line`; a path that is itself the hit shows as `<path withheld: path-name hit>` (T-4821, the same redaction as the sweep's alert). Cite those lines as printed. **Never run `--show-matches` in this procedure, and never paste its output into a mail, a ticket or the handoff;** it exists for local debugging only. Fix the cause in pantheon-config, through a subs/rule/shadow/overlay edit on an `agent/<ticket>` PR, then restart at step 1. Use `leak_allowlist` only for a proven false positive: exact `{path, literal, count, reason}`, never a glob. A path hit can never be allowlisted. If you find a NEW leak class (the gate passed and a human or the adversarial pass caught it), add its pattern to `harness/leak-gate/instance-leak-patterns.txt` by PR in the same arc. The next refresh must fail on it.

**The independent adversarial pass still applies to the diff.** Hand a fresh agent the step-2a `diff` output (the added lines) and the name-map, and tell it to be adversarial. Skip it only when the diff is empty or only re-pins a shadow with no content change. A self-authored gate inherits its author's blind spot. That was true of hand greps (T-1983, T-2465, T-2964, T-4396) and it is true of a pattern file.

**[L] Scan the ADDED LINES, and include the blind spot.**

```bash
git diff --cached | grep '^+' | grep -v '^+++' > /tmp/added.txt
```

Sweep for: operator name (`operator`), the role label **`orchestrator`**, real email, **bare `claude`**, `claude-fallback` / `claude-research` / `claude-telegram` / `codex` / `exocortex` / `suborch[12]` / `the-ancestor`, `/home/<account>` paths, host names (`boxon`/`pve2`/`gpu-ml`/…), tailnet `100.x` addresses, private IPs, key material.

**The genericization name-map (pwa), so you know what "clean" means:** `operator` → `operator`; `orchestrator` → `orchestrator`; agent codenames `specialist` / `recovery` / `orchestrator` are **KEPT** (public-safe); `T-NNNN` / `ADR-NNN` / `ORG-NNN` / `msg_NNNN` refs are **KEPT** (provenance). A de-genericization slip reads as source RE-INTRODUCING a mapped token on a line the mirror had already genericized (the `-` line is correct, the `+` line regresses it) — most likely in an **auto-merged** file you never conflict-resolved. On T-2465 the self-scan omitted `orchestrator` as a class entirely and missed two such slips in `web/tests/e2e/fixtures.ts`; the independent adversarial pass (below) caught both. Add every mapped token to your scan, not just the ones you happened to hand-edit.

**A renamed SYMBOL survives in more forms than its identifier — scan its derived strings too.** When the mirror renames a symbol (e.g. `requireTailscaleUser`→`requireUser`, module `auth/tailscale`→`auth/identity`, error reason `no_tailscale_identity`→`no_identity`), grepping only the identifier/import is not enough. On T-2964 the self-scan checked `requireTailscaleUser` / `auth/tailscale` / `TailscaleIdentity` and reported clean, but the adversarial pass caught two survivors the port had left in **comments**: the old error-code STRING literal `no_tailscale_identity` and the bare module FILENAME `tailscale.ts` (the shipped code emits `no_identity` from `identity.ts`). For each renamed symbol also scan the old (a) error-code / reason string it used to produce and (b) bare `<oldname>.ts` filename references — most often in comments and test names, which no compiler or test failure will surface.

**`claude` bare is mandatory and is the documented blind spot.** T-1983's own identity sweep checked `claude-fallback` and `claude-research` and never checked bare `claude`; an independent adversarial pass found four survivors, one rendered into shipped UI. A self-authored gate inherits its author's blind spot — which is what the done-bar's independent-verification requirement is actually for. **Run the independent pass** (a fresh agent, given the name-map and the diff, told to be adversarial) — it is not optional theater; it earned its keep on both T-1983 and T-2465.

**A template's own `leak-scan.sh` can PASS without checking identity at all (codex; T-4396).** The codex-config-template scan reads its instance-identity patterns from the gitignored `provisioning/instance-leak-patterns.txt`. When that file is absent (every fresh build clone), it prints a one-line `note: … absent — instance-identity scan skipped` and still ends `✓ leak-scan PASS`. On T-4396 the operator's real Telegram chat id came over from the source delta into 6 test lines. The scan passed, and so did an added-line sweep that didn't list the chat id; the independent pass caught it. Before running the scan, create the patterns file locally (see its `.example`) with the operator's chat id, name, and persona/account/host names. Put the chat id in your added-line sweep too. Since the fix must not survive in history, amend the build commit instead of stacking a fix commit on top, and prune the pre-amend object (`git reflog expire --expire-unreachable=now --all && git gc --prune=now`). *(For the generator codex pair, the generator's source d replaces this file, and step 3's `leak-scan.sh` run is a second, weaker check.)*

`T-NNNN` / `ADR-NNN` refs are **NOT leaks** — T-1987 ratified them as provenance-only, treated by a standing README note. Do not add them to the fail gate; gating on non-secret provenance noise blocks refreshes for nothing.

### 5. Ensure the T-1987 provenance note is present (idempotent) *(T-4768 changed: [G] it lives in the README shadow)*

Every refresh must leave the standing "A note on references" section in the mirror's top-level `README.md`. Add if missing, leave if present. Canonical text: `notes/2026-07-27-mirror-dangling-refs-convention.md` (adjust the `adr/` clause per mirror).

**[G]** The top-level `README.md` is a shadow on both generator pairs (`harness/<h>/mirror/shadows/README.md`), and both carry the note today. It can only go missing through a pantheon-config edit to that shadow, so check the step-2a `diff` output for a `README.md` hunk that removes it. If one does, fix the shadow by PR (the seat commits it; README-path files are worker-guarded).

### 6. Construct the publish commit — orphan-squash, fast-forward, never force *(T-4768 changed: [G] the tree comes from `build --write-tree`, and the gate re-runs on the commit)*

**[G]** Write the payload objects INTO THE MIRROR CLONE, then build the commit there:

```bash
cd <mirror-clone>
TREE=$(python3 "$G" build --mirror <claude|codex> --source-sha "$SRC" \
         --write-tree --object-repo "$(pwd)")          # loose objects only: no index, no ref touched
PUB=$(git commit-tree "$TREE" -p "$LIVE" -m "<message citing $SRC>")
git merge-base --is-ancestor "$LIVE" "$PUB" && echo FF-SAFE
python3 "$G" scan --mirror <claude|codex> --source-sha "$SRC" --tree "$PUB" --tree-repo "$(pwd)"
```

`--object-repo` defaults to the pantheon-config checkout. Leave it out and `commit-tree` in the mirror clone fails with "not a valid object". `TREE` must equal the tree that step 2b printed, because the same sha gives the same tree. `scan` must print `LEAKGATE PASS … tree=$TREE` and exit 0. That line is the one you quote in the handoff.

**[L]**

```bash
TREE=$(git rev-parse <your-commit>^{tree})
PUB=$(git commit-tree "$TREE" -p <live-mirror-head> -m "<message>")
git merge-base --is-ancestor <live-mirror-head> "$PUB" && echo FF-SAFE
```

**Verification:** `--is-ancestor` passes, so the mirror fast-forwards and **no force-push is ever needed**. If it fails, stop — do not reach for `--force`.

**Why `commit-tree` and never a branch bundle:** the source repo's build branch carries ~160 commits authored under the operator's real GitHub identity in git *metadata*. A branch bundle would publish that identity ~160 times plus every pre-genericization file state, and **no content scan would catch it**, because every scan reads the working tree and the working tree is clean. `commit-tree` against the mirror head publishes one commit with a generic author onto the disjoint mirror lineage. For generator pairs the source is pantheon-config itself (thousands of commits, every persona, the ledger), so the rule matters even more.

Use the generic author already established on the mirror lineage (`recovery <recovery@pantheon.local>` for the pwa pair) — check `git log --format='%an <%ae>'` on the mirror, do not assume.

### 7. Bundle with a single ref, and verify by CLONING it *(T-4768 changed: [G] the clone's tree and gate verdict are checked)*

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

**[G]** Also, on the clone:

```bash
git -C /tmp/verify rev-parse 'main^{tree}'                 # must equal $TREE
python3 "$G" scan --mirror <claude|codex> --source-sha "$SRC" --tree main --tree-repo /tmp/verify
```

The second must print the same `LEAKGATE PASS` line as step 6 (the same `tree=` and `union=`).

Then **re-run the gates against the fresh clone**, not the working tree. A working tree can pass on state that never made it into the bundle.

**Warm the clone's deps before reading a test result — a cold `bun install` gives a FALSE all-fail (pwa; T-3375).** A fresh clone has no `web/node_modules`; running `( cd web && bun install && bunx vitest run )` back-to-back can start vitest before the install finishes *linking* (jsdom especially), so vitest collects **zero** tests and reports every file failed — `27 failed (27) / Tests no tests / tests 0ms` (note `tests 0ms` while `environment` is nonzero: the files never executed, they failed to *load*). That looks exactly like a content regression and is not one. Re-run `bunx vitest run` once `bun install` reports `no changes`; on T-3375 the cold run showed the false all-fail and the warm re-run was `235 passed`. The build passing on the same clone is a second tell the tree is complete.

**Second cold-clone cause, same signature: the build must run BEFORE vitest (T-4130).** `web/tsconfig.json` `extends` `./.svelte-kit/tsconfig.json`, which is gitignored and only generated by `svelte-kit sync` (run by `vite build`). On a fresh clone, vitest before any build fails every file with `Failed to load tsconfig for 'tests/setup.ts': Tsconfig not found`, even with deps fully installed (`bun install` said `no changes`). Order the clone gates as backend, then `bun run build:web` + `smoke:build`, then `( cd web && bunx vitest run )`. Or run `( cd web && bunx svelte-kit sync )` first. To tell the two causes apart, grep the cold log for `Tsconfig not found`.

Drop the bundle at a path the publisher can read (`/tmp/<name>.bundle`, mode `0644` — the established convention; `/tmp` is world-readable and this matches prior cross-agent bundle drops).

### 8. Record the STAGED bundle — build-leg step, do not skip *(T-4768 changed: [G] add `--payload-tree`, commit the state file)*

```bash
cd ~/pantheon-config                          # the checkout the weekly sweep reads (on main)
git pull --ff-only origin main
python3 tools/mirror_state.py record-staged <slug> \
    --source-head <verified-sha> --bundle-path /tmp/<name>.bundle \
    [--payload-tree "$TREE"]                  # [G] REQUIRED on generator pairs
git add mirror-published-state.yaml
git commit -m "mirror-state: record-staged <slug> (T-NNNN, source <sha[:8]>, tree <TREE[:8]>)"
git push origin main                          # the established direct state-commit convention
```

**[G]** `--source-head` is `$SRC` and `--payload-tree` is `$TREE`. The sweep's built-but-unpushed test for a generator pair compares TREES (`staged payload_tree == build(HEAD) tree != published_payload_tree`). A staged record without `--payload-tree` cannot raise that alert. Commit the `mirror-published-state.yaml` change and push it, so the weekly sweep (which reads the committed file in recovery's checkout) sees it.

**This is the step that was missed on both T-1983 and T-1988.** It powers the built-but-unpushed alert. Skipping it does not fail loudly — it just means the drift sweep is blind until the publisher records.

The `--source-head` is the **SOURCE repo commit the mirror content was built from**, and it must be a VERIFIED commit:

```bash
git rev-parse origin/main                     # full 40-char sha
git cat-file -t <sha>                         # MUST print "commit"
```

**A checksum is not a sha.** On T-1988 the publisher went looking for the source head in T-1983's resolve note, found a 40-hex string, and nearly recorded it as the anchor — it was the truncated bundle sha256. Both are 40 hex characters; only one is a commit. `git cat-file -t` is the one-command discriminator, and it is why the source sha and the bundle checksum should never be adjacent in prose without labels. **[G]** The same goes for the payload tree: `git cat-file -t "$TREE"` in the mirror clone must print `tree`.

If the source head has NOT moved (you authored on the build branch and ported no upstream delta), the correct value is the **unchanged** existing anchor. That is not staleness. Verify zero drift from the other direction too: `git log --oneline <build-branch>..origin/main` should be empty. *([L] only. A generator pair always builds from a pinned pantheon-config sha. A correction there is a pantheon-config commit, so the anchor moves.)*

### 9. Hand off to the publisher, with the loose ends visible *(T-4768 changed: required fields)*

Mailbox orchestrator (`/home/the-ancestor/mailbox/orchestrator`) with:

- bundle path + **sha256** + single-ref sha
- the **verified source head**, labelled as such and stated separately from the checksum
- **[G] the built payload tree** (`$TREE`), labelled, and confirmation that the bundle clone's `main^{tree}` equals it
- **[G] the LEAKGATE verdict line, verbatim** (the step-6 `scan` of the publish commit), with a note that the step-7 clone scan printed the same `tree=` / `union=`
- **[G] the diff-against-live summary:** the anchor-reproduces-live result (step 2a `check` exit and any `ALLOWLISTED` lines); counts of files only-in-build / only-in-live / modified, the paths, and the source commits they trace to; any STALE shadow ported or re-pinned on the way (with the PR)
- confirmation `--is-ancestor` passes against the CURRENT mirror head (name the sha)
- gate results **from the fresh clone**, with pre-existing failures named
- **bundle verification:** one ref (`list-heads`), the clone's author set, and the orphan-squash history length
- anything you carried forward from the mirror side (step 2a)
- **[G] the exact publish-leg record command,** ready to run: `record-published <slug> --source-head $SRC --mirror-head <PUB> --payload-tree $TREE`
- **unresolved loose ends, explicitly.** An unreproduced anomaly reported is a datum; the same anomaly omitted is a trap for whoever hits it next.

### 10. Confirm the anchor landed *(T-4768 changed: [G] the payload tree is part of the anchor)*

The publisher runs `record-published <slug> --source-head <sha> [--mirror-head <sha>] [--payload-tree <tree>]` (`--payload-tree` REQUIRED on generator pairs). **Do not resolve your ticket without checking it happened:**

```bash
python3 tools/mirror_state.py get <slug>
```

**Verification:** `published_mirror_head` is the new head, `source_head_at_last_publish` is your verified source sha, `staged_bundle` is `null`. **[G]** `published_payload_tree` is `$TREE`. Then a dry-run sweep (`python3 tools/mirror_drift_sweep.py --no-digest`) shows the pair `no_drift`. Resolving without this read is half of why the anchor went unrecorded twice — the recording lives on the publisher's leg, but the consequence lands on the drift sweep, which is the build leg's.

**[G] Re-pin the manifest's `acceptance` block in the SAME commit as `record-published` (T-5594).** `test_mirror_drift_sweep.py::test_real_state_generator_pairs_carry_the_published_tree` requires each generator manifest's `acceptance` (`source_sha`, `published_tree`, `published_entries`) to equal the pair's recorded publish in `mirror-published-state.yaml`. So the order is fixed:

1. The refresh PR (overlay/shadow/rule edits) leaves `acceptance` at the LIVE publish. Do not re-pin it there: CI goes red on any main, because the state still names the old publish. The frozen-input test (`test_build_harness_mirror.py`, `test_acceptance_frozen_input_reproduces_published_tree`) builds content AND mirror config from `acceptance.source_sha`, so a pending overlay edit does not break it.
2. Build, bundle and publish as above.
3. Orchestrator does NOT run `record-published` on main for a generator pair. The build leg opens one PR that runs `record-published … --by orchestrator` and re-pins `acceptance` to `$SRC` / `$TREE` / the new `ls-tree` entries, including the frozen-test literals in `test_build_harness_mirror.py`. Both mirror test files must pass, and the dry-run sweep must show the pair current, before the merge ask.

### 11. Advance the persistent build branch — [L] pwa only *(T-4768 changed: [G] no build branch)*

**[G] Nothing to advance.** A generator pair's only build state is the recorded anchor (`source_head_at_last_publish` + `published_payload_tree`, step 10) and the manifest in pantheon-config. The next refresh reads both and needs no local branch. Do not advance or rebuild the retired `genericize` / codex local `main` lineages (W5, step 1).

**[L] pwa:** once the publish is confirmed (step 10), fast-forward the local persistent build branch to the tip you just published so it carries the new genericized tree:

```bash
git branch -f genericize <your-build-tip>          # or: git reset --hard <tip> if genericize is checked out
git rev-parse genericize^{tree}                     # must EQUAL the live mirror/main tree
git merge-base genericize origin/main               # must be your verified SOURCE head (this refresh's anchor)
```

**Verification:** `genericize`'s tree equals `mirror/main`'s tree, and `merge-base(genericize, origin/main)` is the source head you just published from.

**Why:** step 1 builds on a *working branch off* `genericize`; publishing via `commit-tree` never touches `genericize` itself. If you leave it at the old anchor, the NEXT refresh 3-way-merges from a base that lacks this refresh's genericization — so the next build tree silently *reverts* everything you just shipped, surfacing only as a large unexplained `git diff --cached mirror/main` in the next refresh's step 2a (T-2964). The merge-based lineage (a merge commit whose parents are the prior `genericize` and this refresh's source head) is what makes the next refresh's merge-base land on this anchor. `genericize` is local build state (never pushed) — the fix is a local branch move, but skipping it hands the next builder a landmine.

## Sweep outcomes: STALE, leak, drift *(T-4768 new)*

`tools/mirror_drift_sweep.py` (recovery's weekly `pantheon-mirror-drift` unit, `--commit-prod`) classifies a generator pair by payload TREE: `build(HEAD)` against `published_payload_tree`. A commit that does not change the tree is not drift. Precedence on one pair: leak > config error > stale > drift > built-unpushed. Across pairs the worst exit wins, ranked 4 > 2 > 13 > 10 > 0. The unit sets `SuccessExitStatus=10 13`, so only 1 (a crash), 2 and 4 page recovery through `OnFailure`. The Telegram digest is sent on every run.

| Sweep exit / status | What it filed | What the build leg does |
|---|---|---|
| **0** `no_drift` (incl. "via the acceptance allowlist") | nothing | nothing |
| **10** `drift` | ticket `Mirror refresh: <slug> payload tree drifted (<n> payload-path commit(s) since anchor)`, labels `mirror-refresh,pair:<slug>,infra,mirror`, target recovery; body lists anchor, head, both trees, up to 20 MISMATCH lines | run this SOP from step 1 at the CURRENT `origin/main`, not the ticket's head |
| **10** `built_unpushed` | nothing (digest alert) | the staged tree is current but unpublished: chase orchestrator on the open handoff. Rebuild only if `origin/main` has moved the tree. |
| **13** `stale` | ticket `Mirror refresh: <slug> has <n> STALE shadow(s)`, same labels; body lists every `STALE` line | step 2b [G]: port into the shadow or re-pin (reviewed, not ported) by PR, then refresh. A re-pin with no tree change needs no publish: after the merge the sweep returns to 0, and the ticket resolves on that evidence. |
| **2** config / run error | nothing (pages recovery) | a manifest, rule, coverage or `LEAKGATE <source>` (missing pattern source) error, an unreadable head, a failure to file/commit/push a ticket, or every pair errored. Read the unit journal; fix the config by PR. No refresh until the sweep is green. |
| **4** `leak` | **never filed** (W8); the digest carries the pattern class and `file:line` only | file the fix ticket by hand without the matched string, reproduce with `build --source-sha <head>`, fix by PR (step 4 [G]). A leak at HEAD blocks every refresh of that pair. |
| `coverage_gap` | nothing | no `published_payload_tree`. Run `mirror_drift_sweep.py --rebase-anchor <slug> [--published-tree <live ^{tree}>]` (dry-run; exit 0 found, 1 none found, 2 config), and `--write` only after reading the match (`exact` or `allowlisted`). |

Each pair has at most ONE open refresh ticket (`mirror-refresh` + `pair:<slug>`), so a still-open ticket suppresses re-filing. Resolve it only after step 10.

The legacy pairs keep the pre-T-4768 commit-anchor sweep: `drift` when the source head moved past the anchor and any staged head. pantheon-public is `gated: true` (detect-only, never filed, never a 10/13), and the weekly T-1327 pass owns its refresh.

## Before any visibility flip: scan the FULL history, not the tree (T-4396)

Every gate above reads the working tree or the added lines. A mirror's **history** can still carry what an earlier commit removed, and a flip to public publishes all of it. Before ANY visibility flip (operator-initiated only; see §Boundary), whoever prepares it runs the instance-leak patterns against the whole history and the author metadata:

```bash
# In a fresh clone of the MIRROR. Strip comments and blanks first, because a blank
# line given to `grep -f` matches every line.
grep -vE '^[[:space:]]*(#|$)' <patterns-file> > /tmp/flip-patterns.txt
git log -p --all | grep -cE -f /tmp/flip-patterns.txt    # content, every commit (count; then -n to locate)
git log --all --format='%an <%ae> | %cn <%ce>' | sort -u  # author/committer metadata
```

A hit means **republish as a fresh orphan** (one `commit-tree` of the current, clean tree with no parent). It does not mean a history rewrite on the live mirror. **Known today (2026-09-30):** codex-config-template's history holds the operator's chat id and GitHub handle in `61fb0bb` / `9237a38`, early leak-scan pattern lists that `ac34de0` removed. It is private, so nothing is exposed. A flip would expose it. pantheon-public's history holds all internals (T-1327).

**[G]** The generator's `scan --tree` reads one tree only. It is not a history scan.

## Hand-landings — anchor discipline symmetry *(T-4768 changed: [G] fold back into pantheon-config)*

A **hand-landing** is a commit landed directly on the public mirror outside the refresh flow — for example, the operator's 2026-07-27 T-1987 dangling-refs README note on claude-code-config (commit `efb502c`). The refresh flow's `record-published` step (§10) is the anchor's only structural update; a hand-landing that skips it leaves the recorded anchor lagging live mirror reality until the next full refresh reconciles it.

**Why this matters:** every drift-sweep leg on a no-read-path pair starts from the *recorded* anchor as the assumed live base, and relies entirely on the next refresh's step 2a (fresh-clone reconciliation) to catch the divergence. Recovery's warm-recon on T-2963 (2026-08-28) surfaced this exact class — the recorded anchor said mirror head was `2e8bd87` when the live head was actually `efb502c` (the T-1987 hand-landing). No harm this arc because step 2a caught it, but the anchor-vs-reality gap is latent risk.

**[G] On a generator pair, prefer not to hand-land at all.** Make the change in pantheon-config (a shadow or overlay edit) and run a refresh. The generator reproduces only what pantheon-config declares, so the next generated publish ERASES a hand-landing that was never folded back. Step 2a [G] is the structural catch, and it STOPS the refresh rather than letting the erase happen.

**Checklist when hand-landing a commit on a public mirror:**

1. Land the commit on the mirror.
2. Immediately update the anchor: `python3 tools/mirror_state.py record-published <slug> --source-head <original-source-sha> --mirror-head <new-mirror-head>` — keep `--source-head` at whatever source anchor produced the pre-hand-landing tree (a hand-landing is a mirror-only edit; it does not advance the source anchor unless it also folds a source change). **[G]** Add `--payload-tree <new-mirror-head>^{tree}`. The sweep then sees `build(HEAD) != published tree` and files drift until the fold-back lands, which is the intended pressure.
3. Verify with `python3 tools/mirror_state.py get <slug>` — `published_mirror_head` should be the new hand-landed sha; `source_head_at_last_publish` unchanged.
4. **[G]** File a ticket to fold the change into `harness/<h>/mirror/` (shadow or overlay) before the next refresh.

**Applies to:** T-1987-style dangling-refs README notes, provenance annotations on the mirror, any operator note landed straight on the mirror. Does NOT apply to the refresh flow itself (§10 covers that) or to source-side edits that flow through a refresh (those advance the source anchor via §10 in the normal way).

## Common failure modes *(T-4768 changed: [G] rows added, fixes split by class)*

| Failure | Early signal | Fix |
|---|---|---|
| Publish erases a mirror-side commit | none — clean FF, no conflict | step 2a, every time ([G]: anchor-reproduces-live `check` STOPs it) |
| Upstream port reverts a mirror-only correction | none — looks like intended rewrite | [L] step 2b + get it upstream; [G] structurally named as STALE (generator exit 3 / sweep exit 13) |
| Branch bundle leaks operator identity via git metadata | none — content scans read a clean tree | step 6 `commit-tree` only |
| Checksum recorded as source head | anchor "looks" set | step 8 `git cat-file -t` |
| Anchor never recorded | sweep silently blind, no error | steps 8 + 10 |
| Generator anchor recorded without `--payload-tree` | sweep reports `coverage_gap`, or misses built-unpushed | steps 8 + 10 [G] |
| Hand-landing bypasses anchor update | none — refresh flow works; next build leg silently starts from stale base | §Hand-landings checklist |
| Suite run in the wrong directory reads as a regression | plausible wrong test count | absolute `cd`, always |
| Leak gate misses bare `claude` | gate reports clean | [L] step 4, explicitly; [G] the adversarial pass + add the class to source d |
| `--show-matches` output pasted into a mail or ticket | the handoff now carries the literal | step 4 [G]: paste the default redacted lines only |
| Pre-existing failure read as a regression | "my change broke it" | baseline BEFORE editing |
| Next refresh reverts this one | none this refresh — a huge step-2a diff next time | [L] step 11: advance `genericize` after publish; [G] n/a |
| Publish build passed `--allow-stale` | a STALE shadow ships unreviewed | step 2b [G]: never on a publish build |
| Acceptance re-pinned in the refresh PR, before the publish | CI red in `test_real_state_generator_pairs_carry_the_published_tree` (looks like "main moved"; it is not) | step 10 [G]: re-pin with `record-published`, in one commit |
| `commit-tree` "not a valid object" | the tree's objects went into pantheon-config | step 6 [G]: `--object-repo <mirror-clone>` |
| Built from the checkout's HEAD instead of `origin/main` | verdict `source=` is not the `origin/main` tip | step 1 [G] |
| Template leak-scan PASSes with identity unchecked | a one-line "patterns absent" note above the PASS | step 4: create the patterns file first ([G]: generator source d hard-fails instead) |
| Visibility flip publishes a leak that an old commit removed | none, since tree scans are clean | §Before any visibility flip: full-history scan |

## Boundary *(T-4768 changed)*

Covers the **build + gate + bundle + handoff** legs and the anchor contract. Does NOT cover: the publish push itself (orchestrator's leg — `git push mirror`), visibility flips or public branding (**operator-initiated only**, standing boundary), the weekly drift sweep's own operation (`tools/mirror_drift_sweep.py`, T-1994; this SOP only says what to do with its outcomes), merging the pantheon-config PRs that change a manifest, shadow or overlay (`merging-an-agent-branch-pr.md`, HO leg), or genericization *substitutions* for the legacy pairs (`tools/seed_genericize.py` is the **claude-config seed's** gate with its own leak classes — do not misapply its `T-NNNN`/date patterns to the pwa mirror, which deliberately keeps those). The generator pairs' substitutions are the manifest's `protect` / `subs` / `rules`. They borrow only `neutralize_seed`'s boundary helpers, not its tables.

Recovery holds a working read-write deploy key for the pwa mirror (T-1983). **Capability is not authorization** — pushing an outward-facing repo trips the external-facing prong of the act-vs-ask test, and the T-1475 division routes the publish through orchestrator regardless.

## Cross-references

- [[ORG-039]] §3 — harness-config distribution model, genericization + release gates
- [[ORG-091]] §2 — mirror as generated artifact from a pinned pantheon-config sha
- `tools/build_harness_mirror.py` (`build` / `check` / `diff` / `scan`; exit 0 ok, 1 mismatch, 2 config, 3 STALE, 4 leak) + `harness/<h>/mirror/manifest.yaml` — the generator and its declarations
- `tools/mirror_leak_gate.py` — the fail-closed leak gate (no CLI; invoked through the generator) + `harness/leak-gate/instance-leak-patterns.txt`
- `mirror-drift-pairs.yaml` — the four pairs, owners, routing, gates, `generator:`
- `mirror-published-state.yaml` + `tools/mirror_state.py` — the anchor + its contract (`--payload-tree`, `rebase-anchor`)
- `notes/2026-07-27-mirror-dangling-refs-convention.md` — T-1987 provenance-note convention
- T-1454 (initial pwa mirror build), T-1983 (first refresh; the orphan-squash finding), T-1987 (dangling refs), T-1988 (the erase near-miss + anchor gap), T-1994 (drift sweep), T-2133 (port the divergence upstream), T-1673 (push-is-HO-exclusive), T-1475 (build/publish division), T-4750 / T-4768 (generated mirrors: C5 PR1-PR7)
- `sop/standard/cross-account-ops-discipline.md` — cross-account read/write patterns
