# Shared-repo multi-writer permissions — credential + mode propagation discipline

**Status:** Adopted 2026-06-16 by Orchestrator (HO) — autonomous per msg-3733 (operational discipline, below goal-level). Codifies the recurring multi-uid shared-group permission bug-class surfaced by the 2026-06-15 ORG-012 sweep (T-1200). The operator override-available.

> **Note — kino is no longer a shared writer (2026-09-28, Orchestrator, T-4106).** The PWA now writes its own clone at `/var/lib/kino/pantheon-config`, kept fresh by a kino-owned one-minute `ff-pull` timer (`kino-pantheon-config-pull.timer`), and `kino` is in neither `pantheon-shared` nor `claude`. It keeps only a traverse ACL on `/home/claude` for read access to the repos the PWA displays. So the shared-writer set for `/home/claude/pantheon-config` is the agent seats only, and the kino-specific shared-group and UMask retrofit (T-0281) is superseded. Where the text below names kino as a writer, read it as the historical topology; the bug-class and the discipline still apply to the remaining seat writers.

When more than one unix uid writes the same repo or path through a shared group (Pantheon's `pantheon-shared` topology: `claude`, `kino`, and other agents all writing `/home/claude/pantheon-config`), **neither a writer's credentials (its supplementary-group set) nor the file modes it produces (its umask) propagate automatically** when the writer set changes. The shared-writer invariant — every writer can read+write every other writer's objects via the group — silently breaks, and the next cross-uid git op fails with `Permission denied`. This SOP is the onboarding + group-change checklist that keeps the invariant intact. It is the structural fix; the three bugs below are the evidence it earns its keep.

## The bug-class (one root, three faces)

All three trace to one root: **a pantheon-shared writer whose group membership / file modes / process credentials don't auto-propagate across the multi-uid shared topology.** They split along two propagation axes:

- **Credential propagation (the running process carries a stale group set).** `bugs/systemd-user-manager-stale-supplementary-groups.md` — after `adduser <user> <group>`, that user's *already-running* `systemd --user` manager (and every timer/service it spawns) still carries the OLD supplementary-group set; group-gated paths return `Permission denied` until the manager is restarted.
- **Mode propagation (objects written without group/other-write).** `bugs/shared-repo-checkout-fails-on-non-group-writable-dirs-from-other-uid.md` — another shared-writer committed subdirs without group-write (umask drift), so a checkout from a different uid aborts mid-rebase with `cannot create directory: Permission denied`. And `bugs/kino-uid-git-objects-collision-with-claude-uid.md` — `kino`-written `.git/objects/` subdirs inherit `kino:kino` mode `drwxrwxr-x` (no other-write), so subsequent `claude`-uid git ops fail with `insufficient permission for adding an object to repository database`.

**NOT in this class:** the `sudo -S` heredoc-on-same-command bug is a DISTINCT root (shell stdin plumbing, not credential/mode propagation) — do not group it here. See `reference_sudo_password_location` for that one.

## The discipline (three structural rules)

### (a) Group change → restart that user's running daemons/managers

Adding a uid to a group (or removing it) does NOT reach already-running processes. After any `adduser`/`gpasswd`/group edit affecting a shared-writer:

1. **Enumerate** that user's running units: `sudo -u <user> systemctl --user list-units --type=service,timer --state=running` (+ the `user@<uid>.service` manager itself).
2. **Restart** them so they re-read the group set. The blunt instrument is `sudo systemctl restart user@<uid>.service` — **but that is DESTRUCTIVE for an agent whose live session / poller is hosted there** (kills the session). For an agent uid, prefer restarting the specific affected service units, and schedule the manager restart for a safe window (or fold it into a planned handoff-restart).
3. **In-session workaround that needs no restart:** `sg <group> -c '<cmd>'` runs one command with the new group, as long as `/etc/group` already lists the user for that group. Use this to unblock immediately; the manager restart is still owed for durable propagation.

### (b) Every pantheon-shared writer's systemd unit MUST set `UMask=0002`

A shared writer that runs with the default `0022` umask creates group-readable-but-not-group-writable objects, breaking the multi-writer invariant for the next uid. Set `UMask=0002` in the `[Service]` section of **every** systemd unit that writes a pantheon-shared path, **enforced at onboarding** (add it to the unit when a new shared-writer is stood up — see `onboarding-a-new-harness.md`). This is what was retrofitted for `kino` (T-0281); the rule generalizes it so the next shared-writer doesn't reproduce the class.

### (c) Post-group-add / post-onboard verification

Before declaring a shared-writer change clean, verify the invariant holds — don't assume:

- **Credential:** `sudo -u <user> id` shows the new group, AND a process under the *running* manager can traverse the group-gated path (`sudo -u <user> sg <group> -c 'test -w <path> && echo ok'`, or check after the manager restart).
- **Mode:** the repo carries the shared-repo settings — `git config core.sharedRepository` = `group`, setgid bit on dirs (`find <repo>/.git/objects -maxdepth 1 -type d ! -perm -g+w` returns nothing), and a write from the new uid followed by a read from another uid both succeed.
- If a pre-existing dir already drifted, the one-shot repair is `sudo chmod -R g+w <path>` (group-write restore; setgid is usually already present) — but the durable fix is rules (a)+(b), not repeated chmod-back.

## Boundary

- This SOP covers the **shared-group multi-writer topology** (many uids → one repo/path via `pantheon-shared`). It is a DIFFERENT axis from `cross-account-ops-discipline.md`, which covers Recovery's asymmetric repair-sudo bridging the three agent accounts. They compose (a cross-account op may also touch a shared path) but the failure modes are distinct.
- The umask/group rules apply to **systemd-launched** shared-writers. An interactive shell writing a shared path inherits the login umask; `umask 0002` in that session is the analogue, but the durable surface is the daemon units.
- The sudoers configuration + group definitions themselves are admin-tier (the operator owns); this SOP is the operational discipline around them, not a grant to edit them.

## Cross-references

- **Bugs (the evidence):** `bugs/systemd-user-manager-stale-supplementary-groups.md`, `bugs/shared-repo-checkout-fails-on-non-group-writable-dirs-from-other-uid.md`, `bugs/kino-uid-git-objects-collision-with-claude-uid.md`.
- **Sibling SOPs:** `cross-account-ops-discipline.md` (the Recovery-sudo-bridge axis); `onboarding-a-new-harness.md` (where rule (b) `UMask=0002` is enforced at stand-up).
- **Reference pins:** `reference_kino_safe_directory_config`, `reference_sg_pantheon_shared_git_writes` (the `sg`-wrap in-session workaround), `reference_sudo_password_location` (the DISTINCT `sudo -S` bug, explicitly not in this class).
- **Principles + directives:** P1 (architecture over policy — the invariant is enforced by group+setgid+umask structure, not by remembering to chmod); D10 (one SOP for the class, not a per-bug scatter).
- **Tickets:** T-0281 (kino shared-group + UMask retrofit — the precedent rule (b) generalizes); T-1200 (this codification).

## Authoring discipline

Hand-curated per D2 (HO authors). Refresh if a fourth face of the class surfaces, or when a new shared-writer topology (beyond `pantheon-shared`) is added.
