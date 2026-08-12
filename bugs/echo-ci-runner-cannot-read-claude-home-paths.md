---
symptom: "A CI job on the self-hosted runner fails with PermissionError on /home/claude/... though the same command runs green locally as claude"
fix: "ci-runner cannot traverse /home/claude (mode 750) - reference the /srv bind-mount path instead of absolute /home/claude paths in workflow-reachable files"
domain: echo, ci, github-actions, self-hosted-runner, permissions
---
# Echo CI jobs cannot read paths under /home/claude — use the /srv bind-mount pattern

**Surfaced:** 2026-07-17, T-1749 (`adr-vault-lint` self-test failed on its own PR, run 29587726734).

## Symptom

A CI job on the self-hosted runner fails with `PermissionError: [Errno 13] Permission denied: '/home/claude/...'` even though the same command runs green locally as `claude`.

## Root cause

Echo CI runs as the `ci-runner` unix user on this VPS. `/home/claude` is mode 750, so ci-runner cannot traverse into it at all. Any workflow step that references an absolute path under `/home/claude` (vault checkouts, shared data, tool state) works in local verification and fails only in CI — reviewer/implementer smoke loops all run as `claude` and structurally cannot catch it.

## Why NOT to widen /home/claude

Traverse-only ACLs (`u:ci-runner:x`) look safe but are not: several files inside were group/world-readable at the time (e.g. `root.env` — the sudo password — was 664; now 600, but the class recurs). ci-runner also executes PR-branch code, so any traversal grant into the orchestrator's home is attack surface for a bad PR.

## Fix pattern (applied for the vault)

Read-only bind mount outside the home dir, persisted in fstab:

```
mount --bind /home/claude/echo_vault /srv/echo_vault
mount -o remount,ro,bind /srv/echo_vault
# /etc/fstab: /home/claude/echo_vault /srv/echo_vault none bind,ro 0 0
```

Permission checks on `/srv/echo_vault/...` start at `/srv`, so the home dir's 750 never applies; the vault's own 775/664 modes permit other-read. Verify both directions before declaring done: `sudo -u ci-runner ls /srv/echo_vault/...` succeeds AND `sudo -u ci-runner cat /home/claude/root.env` is denied (see the actual denial line — don't infer from empty output).

## Rule of thumb

Before wiring any absolute path into a workflow on the self-hosted runner, ask: can `ci-runner` read it? If it lives under `/home/claude`, the answer is no — bind-mount a read-only view under `/srv/` instead of loosening home-dir perms.
