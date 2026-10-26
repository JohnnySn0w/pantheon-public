---
symptom: "`tools/persona_orientation/check_stale.py` reports `OK -- N source binding(s) fresh` on a peer seat while that seat's rendered `~/.claude/skills/pantheon-*` artifacts are out of date (`render.py --check` exits 2). Spawned workers on that seat load stale orientation."
fix: "For per-seat freshness, run `render.py --check` as the seat's user, not `check_stale.py`. To refresh a peer: render and push stamps from the authoritative clone first, then on the peer `git pull --ff-only origin main && python3 tools/persona_orientation/render.py`, then confirm `render.py --check` = 0 and a clean `git status`. Procedure: `rulebook/persona-orientation/README.md` §Propagating to peer seats."
domain: "persona-orientation / fleet propagation"
---

# check_stale is blind to per-seat artifact drift

## Symptom
Specialist seat, 2026-09-25 (T-3997): `check_stale.py` returned rc=0 ("28 source bindings fresh"). On the same seat, `render.py --check` returned STALE for three artifacts: `pantheon-persona-orientation`, `pantheon-role-reviewer`, `pantheon-role-head-engineering-orchestrator`. The reviewer skill still described three review lenses, so it predated the fourth lens (T-3803) and the `head_engineering_orchestrator` rename (T-3814).

## Cause
There are two surfaces:

- **Stamps.** `source-bindings.yaml` records the provenance of each source file. It is committed, so every clone shares it.
- **Artifacts.** The rendered skills live in each seat's own `~/.claude/skills/`, outside git.

`check_stale.py` compares the committed stamps against HEAD. Once any one seat re-renders and pushes fresh stamps, every clone at that HEAD reads FRESH, whether or not its own artifacts were ever re-rendered. The 2026-09-16 "5-of-6 FRESH" sweep in T-3434 used `check_stale.py` and so could not see this.

A related trap: the 2026-09-14 T-3434 note said "render on orchestrator + push; don't cross-account render." Pushing refreshes the stamps but not any peer's artifacts. The real cause of the earlier dirty peer tree was ordering. The peers rendered before fresh stamps were on origin. The stamp is `git log -1` for each source file, so a peer at the same HEAD as origin writes byte-identical stamps.

## Fix
- Check each seat's freshness with `render.py --check`, run as that seat's user.
- Propagate in the order given in `rulebook/persona-orientation/README.md` §Propagating to peer seats: stamps pushed first, then each peer pulls and renders, then each peer is verified.
- `check_stale.py` now says in its docstring and OK line that it only checks repo-level stamps.

## Verification
Fleet-wide on 2026-09-27, before the T-3997 re-render: `check_stale.py` = OK on every clone, while `sudo -u <seat> -H bash -c 'cd ~/pantheon-config && python3 tools/persona_orientation/render.py --check'` = STALE on all five peers (claude-fallback, claude-research, suborch1, suborch2, exocortex). Re-run that pair on any seat to see it.

## Related
- T-3434: tracks the HO-manual propagation pass until the germination engine absorbs it.
- `architecture/adr/ORG-100-role-classes-naming-and-seating.md` §Compatibility says artifacts "auto-refresh per T-3432 propagation". That is inaccurate: T-3432 kept propagation HO-manual. The ADR is ratified, so the erratum is routed to the operator instead of being edited in place. *Outcome: the operator approved it (TG 10399, T-4036) and it landed 2026-09-27 as an inline erratum in ORG-100 §Compatibility.*
