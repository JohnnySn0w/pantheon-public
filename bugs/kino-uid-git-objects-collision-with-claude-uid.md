---
symptom: "After kino-daemon (uid=kino) runs ticket.py against /home/claude/pantheon-config, new objects in .git/objects/ are kino-owned; subsequent git operations from claude-uid fail with 'insufficient permission for adding an object to repository database .git/objects' (git fetch / pull / commit unpack-objects failed)"
fix: "LANDED 2026-05-13 via T-0281: option (a) — pantheon-shared group covering claude + kino, applied to /home/claude/pantheon-config with core.sharedRepository=group + setgid on dirs. kino.service restarted to pick up new group. Future PWA-button writes inherit the shared group; structural fix, no per-collision chown-back remediation needed. Pre-fix short-term remediation (sudo chown -R claude:claude .git/objects after each kino write) retired."
domain: pantheon, kino, git, file-permissions, multi-user, daemon, ticket.py
---

## Symptom

After the [[reference_kino_safe_directory_config]] fix unblocked kino-daemon's git operations against `/home/claude/pantheon-config`, every PWA Claim/Resolve/Block/Unblock/Retarget action causes kino (uid=kino, gid=kino) to write new objects under `/home/claude/pantheon-config/.git/objects/`. Those objects (and the subdirectory inodes created for new prefix-bytes) inherit kino:kino ownership. The directories are created mode `drwxrwxr-x` — i.e., group-write, but no other-write.

When the claude user (HO's session) subsequently runs any git operation that needs to write to those subdirs, it fails:

```
$ python3 tools/ticket.py file ...
error: git fetch failed (cannot safely allocate ticket ID):
  error: insufficient permission for adding an object to repository database .git/objects
fatal: failed to write object
fatal: unpack-objects failed
```

The symptom surfaces specifically on writes into kino-owned prefix subdirs. The claude-owned prefix subdirs continue to work fine. Over time as both users write, the .git/objects/ tree becomes a checkerboard of claude-owned and kino-owned dirs.

## Cause

Two interacting facts:

1. Git creates per-prefix-byte subdirs (`.git/objects/XX/`) on first write to that prefix. The creating uid owns the dir.
2. Subsequent writers must have write permission on the dir. With kino-uid creating + `drwxrwxr-x kino:kino`, claude-uid (not in kino group) has only read+execute — cannot create new object files inside.

The kino safe.directory fix unblocked kino's READS + WRITES in the repo (the original "dubious ownership" check), but didn't change the filesystem-level uid:gid mismatch between the two users sharing the working tree.

## Short-term fix (applied 2026-05-12)

After detecting the symptom, chown all kino-owned objects back to claude:

```bash
cut -d= -f2 /home/claude/root.env | sudo -S \
  find .git/objects -not -user claude -exec chown claude:claude {} +
```

This unblocks the immediate operation. It is **not** a structural fix — every subsequent PWA-button click recreates the collision. Re-apply on demand until a structural fix lands.

## Long-term fix — option (A) LANDED 2026-05-13 (T-0281)

**(A) Shared group + core.sharedRepository=group.** Add claude + kino to a shared group (e.g., create `pantheon` group; add both users). Set `git config core.sharedRepository=group` on `/home/claude/pantheon-config`. Future objects land mode `drwxrwsr-x <user>:pantheon` so either user can write. Lowest-friction; requires one-time sudo to create the group + adduser; durable thereafter. Note the `s` setgid bit — must be enabled on existing dirs too via `chmod g+s` to make new objects inherit the group.

**Applied 2026-05-13T01:46Z via T-0281** (operator-ratified msg_1155, root command bundle in ticket body). Group name landed as `pantheon-shared` (not bare `pantheon` — per ticket recommendation). Membership: claude + kino only (Recovery + Specialist have separate clones under their own homes). kino.service restarted at 01:47:36 UTC so the running daemon picks up the new supplementary group (existing processes cache group membership at fork time). Verified: kino-daemon PID 1668089 Groups now include `1003` (pantheon-shared); `git hash-object -w` smoke as kino succeeded.

## Long-term fix candidates (rejected or unused)

**(B) Separate worktree for kino.** Give kino its own checkout (e.g., `/var/lib/kino/pantheon-config-worktree`) via `git worktree add`. Kino's ticket.py operations land in that worktree; a periodic sync (cron / inotify) merges into claude's working tree. Heavier; introduces a sync surface that can fail; bypasses the uid-conflict entirely.

**(C) Kino shells via sudo -u claude.** Adjust shell.ts in kino-daemon so the python3 ticket.py subprocess is launched as `sudo -u claude python3 ...`. Requires sudoers entry for kino → claude. Single-process scope; preserves the existing path layout; introduces a small startup cost per shell-out.

Lean: **(A)** is the cleanest if the operator accepts adding a shared `pantheon` group. **(C)** is the lowest-blast-radius if he doesn't want a new group. **(B)** is heaviest. Surface to the operator at ratification time.

## Discovered

- 2026-05-12 — first surface, ~3h after [[reference_kino_safe_directory_config]] applied. The operator clicked the PWA Claim button on T-0207 (the kino-deploy ticket?), kino's git mv + git add + git commit succeeded, wrote ~10 kino-owned objects. Next HO `ticket.py file` for T-0264 (Specialist rate-limit research) hit the permission error on git fetch's unpack-objects. Diagnosed via `find .git/objects -not -user claude -printf "%u %p\n"`.

## Pairs with

- [[reference_kino_safe_directory_config]] — the upstream unblock that this surfaced as a side-effect.
- [[reference_sudo_password_location]] — root.env path; HO uses for chown remediation + would use for any of fix-(A)/(B)/(C).
- `bugs/teamcreate-team-lead-slot-pre-allocation-collision.md` — sibling multi-uid filesystem-collision class.

## Keywords

pantheon, kino, kino-daemon, git, .git/objects, file ownership, uid:gid mismatch, multi-user, safe.directory, ticket.py, claude/kino split, core.sharedRepository, git worktree, sudo -u claude
