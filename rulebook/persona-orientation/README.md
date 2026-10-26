# rulebook/persona-orientation/

Canonical, version-bound source for what every Pantheon persona/role sees at
spawn time, on both the Claude and Codex harnesses (T-3339). Rendered by
`tools/persona_orientation/render.py` — never hand-edit the generated
skill files under `~/.claude/skills/pantheon-*`; edit these sources and
re-render.

- `manifest.yaml` — base-layer sections, all 10 roles (7 pure-Claude,
  including `engineering_orchestrator` for the mason/artisan seats since
  T-2892, + 3 Consultant, the latter stubbed pending the T-3333 charter),
  harness adapters.
  D2-protected; hand-curated.
- `role-deltas.yaml` — per-role delta content, pointing at ratified sources
  (`rulebook/`, `sop/standard/`) or explicit inline text where no ratified
  source exists yet (each such case carries a FIX-SOON note). D2-protected;
  hand-curated.
- `source-bindings.yaml` — **renderer write-only.** Records
  `{source_path: {git_sha, git_blob, sha256, byte_length}}` for every source
  file the last render read. Never hand-edit; re-render to refresh.
- SOP-skills (T-4465) live outside this directory, because the tools/ tree is where a
  worker can write: `tools/persona_orientation/sop-skills.yaml` (manifest) and
  `tools/persona_orientation/sop-skill-bindings.yaml` (renderer write-only stamps, same
  record format as `source-bindings.yaml`).

## Regenerating

```
python3 tools/persona_orientation/render.py --dry-run   # preview
python3 tools/persona_orientation/render.py             # write + stamp
python3 tools/persona_orientation/render.py --check     # verify (exit 2 on drift)
python3 tools/persona_orientation/check_stale.py        # freshness gate (exit 2 on drift)
```

Trigger is **manual, per ratified rulebook/SOP change** (RATIFY-POINT-2) —
not automated, to avoid a merge race rendering inconsistent orientation.
Run it after any change to a source this manifest names, then commit the
regenerated `source-bindings.yaml` alongside.

## SOP-skills (T-4465)

High-traffic SOPs render as skills that load when the task matches, instead of relying on
an agent to scan `sop/standard/README.md`. The SOP stays the single source.

- Manifest: `tools/persona_orientation/sop-skills.yaml` (entry fields and the
  whole-vs-excerpt rule are in its header). Stamps: `tools/persona_orientation/sop-skill-bindings.yaml`.
- Claude: `~/.claude/skills/pantheon-sop-<slug>/SKILL.md`. Codex:
  `$CODEX_HOME/skills/pantheon-sop-<slug>/SKILL.md` (default `~/.codex/skills`), with
  `metadata.short-description`.
- `render.py --harness {claude,codex,all}` (default `claude`; persona skills are
  Claude-only). A seat renders for itself only: `python3 tools/persona_orientation/render.py
  --harness all` as that user where the seat runs Codex, and `--check --harness all` to
  verify. `--check` also flags orphaned generated `pantheon-sop-*` dirs, and orphaned
  generated `pantheon-role-*` dirs (a role skill whose role the persona manifest no longer
  names; claude harness only, the codex skills dir is never scanned for role skills); a
  write-mode render prunes them (only dirs whose SKILL.md is a regular file carrying the
  GENERATED marker; symlinks and hand-written skills are reported, never touched; T-4557).
- Drift: `check_stale.py` covers the SOP stamps (blob vs HEAD, an entry with no stamp, a
  stamp for a retired entry). CI runs `tools/test_persona_orientation_sop_skills.py` on any
  `sop/standard/**` or `tools/**` change and nightly.
- After an SOP named in the manifest changes: re-render and commit `sop-skill-bindings.yaml`;
  the same PR as the SOP edit is fine. The stamp's `git_sha` is provenance only. The gates
  check that the stamped `git_blob` exists and equals the SOP's blob at HEAD, so a squash
  merge that drops the PR's commits does not break them. (The persona stamps in
  `source-bindings.yaml` keep their own reachability test, unchanged.)
- Propagating to peer seats follows the same pass as "Propagating to peer seats" below.

## Propagating to peer seats

The rendered skills live in each seat's own `~/.claude/skills/`, so a render
on one seat refreshes only that seat. `check_stale.py` reads the committed
stamps and cannot see a peer whose artifacts are behind (T-3997); use
`render.py --check` as the seat's user for that.

1. Render on the authoritative clone, then commit and push
   `source-bindings.yaml` if it changed.
2. For each peer seat, as that user: `git pull --ff-only origin main`, then
   `python3 tools/persona_orientation/render.py`. The stamp is the last
   commit touching each source, so a peer at the same HEAD writes a
   byte-identical `source-bindings.yaml` and its tree stays clean. A dirty
   tree afterwards means the peer was not at origin's HEAD or step 1 was
   skipped; `git checkout -- rulebook/persona-orientation/source-bindings.yaml`
   and redo step 1.
3. Verify on each peer: `render.py --check` exits 0 and `git status
   --porcelain` is empty.

Until the germination engine absorbs this as a typed sub-op, the pass is
HO-manual (T-3434).

## Phase status

Phase 1 (this landing): manifest + renderer + freshness detector + 6
pure-Claude role skills (7 since `engineering_orchestrator` landed, T-2892). Phase 2 (Consultant, T-3338): Codex-side render path.
Phase 3: structural tests requiring live subagent spawns. Phase 4:
role-boundary structural enforcement. Phase 5: Consultant role deltas, gated on
T-3333.
