---
symptom: "Running tools/spine_index_groom.py --commit-prod interactively (not via the cron) while you hold UNPUSHED local commits leaves your clone diverged — git status shows 'ahead N, behind M' — and the index it just pushed to prod is regenerated from ORIGIN's tree, so it OMITS whatever your unpushed commits added (e.g. a brand-new ADR file's index row). The groomer's own commits land on origin but never on your local HEAD, so the two histories fork."
fix: "Before running spine_index_groom.py --commit-prod by hand, PUSH your local commits first (or just let recovery's cron groom the index — content files are auto-indexed on the next cron fire, so pushing the ADR/bug/note file and doing nothing else is the normal path). If already diverged: git pull --rebase (your file-add commit rebases cleanly on top — no overlap with the index region), git push, THEN re-run --commit-prod so it regenerates the index from an origin that now contains your file, then git pull --rebase once more to sync the groomer's index commit back to local."
domain: pantheon, spine_index_groom, git, divergence, commit-prod, adr, index, cron-tool
---

# spine_index_groom.py --commit-prod commits to prod against ORIGIN, diverging an unpushed local clone

**First observed:** 2026-08-10 (authoring ORG-069 as ORG-047 Split D).
**Symptom:** after `git commit`-ing a new ADR locally (unpushed) and running `python3 tools/spine_index_groom.py --commit-prod` to refresh the README index, `git status -sb` shows `ahead 1, behind 12`; the pushed `adr/README.md` on origin does NOT contain the new ADR's row (`grep -c ORG-069 adr/README.md` == 0 on origin); the groomer's `spine(index): groom …` commits are on origin but the local HEAD is still the ADR commit. Local and origin have forked.

## Cause

`--commit-prod` is a **cron-path** tool: it fetches origin, regenerates the sentinel index regions **from origin's tree**, commits, and pushes — independent of your working HEAD. So (1) it reveals any commits that landed on origin while you worked (the `behind M`), and (2) because your new content file was only committed **locally** (unpushed), origin's tree lacks it, and the regenerated index omits its row. The groomer's commits go to origin, not your local branch, leaving `ahead N` (your unpushed commit) + `behind M` (origin's advance incl. the groomer's own push). `--no-push` without `--commit-prod` is effectively a dry-run: it prints "wrote N rows" but persists nothing to disk (`git status` stays clean).

## Rule

- **Push content before grooming.** The index is derived + auto-maintained by recovery's cron; the normal path is: commit the ADR/bug/note file, `git push`, and let the cron index it. Only run `--commit-prod` by hand if you need the index refreshed immediately — and only **after** your commits are on origin.
- **Do not** run `--commit-prod` interactively on top of unpushed local commits — it will diverge you.
- **Reconcile if diverged:** `git pull --rebase` (a new-file commit rebases cleanly — it never overlaps the index sentinel region), `git push`, re-run `--commit-prod` (now origin has the file, so the index includes it), `git pull --rebase` to sync the groomer's index commit back to local. Verify: `git status -sb` clean (no ahead/behind) + the new row present (`grep -c <id> adr/README.md`).

## Cross-refs

2026-08-10: ORG-069 authoring — diverged ahead-1/behind-12 (origin had specialist spine grooms + another orchestrator session's T-2819–2822 info-flow tickets), reconciled via rebase→push→re-groom→pull (final `92c1242900` groom-adr, `6e72f20b61` ORG-069). Sibling gotcha: [[pantheon-config-push-pr-advisory-noise]] (the branch-protection bypass line is normal for HO pushes).

## Keywords

spine_index_groom, --commit-prod, --no-push, dry-run, cron-tool, ahead behind,
diverged clone, unpushed local commit, adr/README.md, sentinel region, AUTOGEN,
git pull --rebase, reconcile, recovery cron grooming, index regeneration, ORG-069,
spine index, auto-index, push-content-before-grooming
