# rulebook/persona-orientation/

Canonical, version-bound source for what every Pantheon persona/role sees at
spawn time, on both the Claude and Codex harnesses (T-3339). Rendered by
`tools/persona_orientation/render.py` — never hand-edit the generated
skill files under `~/.claude/skills/pantheon-*`; edit these sources and
re-render.

- `manifest.yaml` — base-layer sections, all 9 roles (6 pure-Claude + 3
  Cairn, the latter stubbed pending the T-3333 charter), harness adapters.
  D2-protected; hand-curated.
- `role-deltas.yaml` — per-role delta content, pointing at ratified sources
  (`rulebook/`, `sop/standard/`) or explicit inline text where no ratified
  source exists yet (each such case carries a FIX-SOON note). D2-protected;
  hand-curated.
- `source-bindings.yaml` — **renderer write-only.** Records
  `{source_path: {git_sha, git_blob, sha256, byte_length}}` for every source
  file the last render read. Never hand-edit; re-render to refresh.

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

## Phase status

Phase 1 (this landing): manifest + renderer + freshness detector + 6
pure-Claude role skills. Phase 2 (Cairn, T-3338): Codex-side render path.
Phase 3: structural tests requiring live subagent spawns. Phase 4:
role-boundary structural enforcement. Phase 5: Cairn role deltas, gated on
T-3333.
