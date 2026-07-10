---
symptom: "git push (or pull --rebase) fails mid-checkout with `cannot create directory at '<path>': Permission denied` — the path is a new subdir under a directory another pantheon-shared user (e.g. kino) committed without group-write"
fix: "`sudo chmod g+w <parent-dir> [<parent-dir>/...]` to restore the pantheon-shared multi-writer invariant; setgid is usually already set, only group-write is missing. Then re-run pull --rebase / push."
domain: git
---

## Symptom

A `git push` (whose `.githooks/pre-push` does a freshness-rebase) or a `git pull --rebase` in `pantheon-config` aborts during checkout:

```
fatal: cannot create directory at 'assets/stickers/specialist': Permission denied
pre-push: rebase FAILED — resolve conflicts, then retry push.
error: Please commit or stash them.
```

The repo is left mid-rebase (`.git/rebase-merge` present), your own commit intact but unpushed, `main` diverged (ahead 1, behind N).

## Cause

`pantheon-config` is a shared checkout — multiple unix users (claude/orchestrator, claude-research/specialist, kino) in the `pantheon-shared` group write to the same working tree. When another user's process commits a **new directory** with a non-group-writable mode, your checkout of subdirs/files under it fails because your user can't `mkdir` inside it.

Observed 2026-06-09: kino's process added `assets/stickers/` (PWA sticker assets) owned `kino:pantheon-shared` mode `drwxr-sr-x` — setgid set (so new files inherit the group) but **group lacks `w`**. Origin then had `assets/stickers/specialist/*.png`, `assets/stickers/codex/*.png` etc.; claude's rebase tried to create `assets/stickers/specialist/` inside the kino-owned dir and hit `Permission denied`.

This is the working-tree-permission sibling of `bugs/kino-uid-git-objects-collision-with-claude-uid.md` (that one is about `.git/objects` ownership; this is about working-tree dir modes). Root cause is the committing process's umask: a `022` umask produces `drwxr-xr-x`/`-rw-r--r--` (not group-writable); shared-repo writers need `002` (→ `drwxrwxr-x`/`-rw-rw-r--`).

## Fix

1. Get out of the broken rebase: `git rebase --abort` (your commit returns to local `main`, intact).
2. Restore group-write on the offending dir(s) — sudo because they're another uid's:
   ```
   sudo chmod g+w assets assets/stickers
   ```
   Adding `g+w` does NOT remove the owner's access; it just lets pantheon-shared members create entries. (The setgid `s` bit is usually already present — only `w` is missing.)
3. `git pull --rebase` (checkout now succeeds) → `git push`.

Sudo password: `cut -d= -f2 /home/claude/root.env`.

## Durable fix (DONE — kino, 2026-06-09, T-1038)

The committing process should write group-writable dirs — set its umask to `002` in its
systemd unit / launcher, or apply a default ACL (`setfacl -d -m g::rwx`) on shared asset
roots so every new dir is group-writable on creation.

**Applied for kino (T-1038):** `kino.service` now sets `UMask=0002`, so kino-created dirs
land `drwxrwxr-x`/`-rw-rw-r--` and no longer block claude's pre-push freshness-rebase. The
chmod above remains the per-incident unblock for any OTHER non-claude pantheon-shared writer
whose launcher doesn't yet set umask `002` (apply the same UMask/ACL fix to that writer's
unit to make it durable there too).

## Discovered

- 2026-06-09 (HO §5-sweep ledger push, entry 122) — kino's `assets/stickers/{specialist,codex,...}` sticker-asset commit blocked claude's pre-push freshness-rebase. Unblocked via `sudo chmod g+w assets assets/stickers`; push landed.

## Keywords

git, pantheon-config, shared checkout, pantheon-shared, multi-writer, cannot create directory, Permission denied, pre-push rebase, kino, umask 002, setgid, chmod g+w, working tree, assets/stickers
