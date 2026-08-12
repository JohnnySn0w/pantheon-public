---
symptom: "A codex-user process gets Permission denied listing /home/the-ancestor/mailbox/<persona> for any persona other than codex"
fix: "Intended T-1733 ACL remediation, not breakage - do NOT chmod g+rwx or add codex ACLs to other personas' dirs; inspect with getfacl"
domain: pantheon, mailbox, acl, security, permissions
---
# Mailbox "Permission denied" for codex is BY DESIGN (T-1733 ACLs), not breakage

**Symptom:** a codex-user process gets `Permission denied` listing/reading `/home/the-ancestor/mailbox/<persona>` for any persona other than `codex`.

**Cause (intentional):** T-1733 (S3STAR-2026-07-13-002 remediation, 2026-07-20) replaced the blanket `veneration`-group rwx on the per-persona mailbox dirs with named-user POSIX ACLs (access + default, recursive). Trusted set — now FIVE (exocortex added per the operator TG 3846 via Recovery, same day, after Mnemosyne's outbound sends bounced): `claude`, `claude-fallback`, `claude-research`, `the-ancestor`, `exocortex` keep full send/consume on all six inboxes (incl. `mailbox/mnemosyne`, T-1824); `codex` has rwx only on `mailbox/codex` — the codex read-deny is the T-1733 finding itself and is unchanged. Owning-group class is `---`; the `mailbox/` parent keeps group `--x` (traverse-only) so codex reaches its own inbox.

**Do NOT** "fix" by chmod g+rwx or by adding codex ACLs to other personas' dirs — that reverts a security remediation. If codex mailbox comms to other agents ever get wired (ORG-031 inbound work), the deliberate reversal is `setfacl -R -b` per dir + re-evaluating the finding with the S3* auditor.

**Inspect:** `getfacl /home/the-ancestor/mailbox/orchestrator` (as root). New files/subdirs inherit via default ACLs; `mailbox.py`'s best-effort `chmod` on subdirs only moves the ACL mask, not the named entries.

## Scoped exception — `mailbox/recovery` DOES grant codex (T-2382 persona-host), 2026-08-03

The codex-only posture above has ONE deliberate, scoped exception: `mailbox/recovery` grants `codex` full ACL (dir + default + files). This is intentional and load-bearing — under T-2382 the codex account *hosts the recovery persona* (`pantheon-persona-consume@recovery` on the codex user manager), so codex must read/wake recovery's mailbox to run the persona. It is NOT a T-1733 reversal: codex stays denied on `orchestrator`/`specialist`/`mnemosyne`/`the-ancestor`; only `recovery` (codex's recovery-host persona) is granted. **Do NOT strip codex from `mailbox/recovery` while `persona-consume@recovery` is the recovery recovery mechanism — that breaks it.** (Note: as of the same day the codex Recovery timer was *parked* pending a canonical same-account swap; the ACL grant stays regardless, so a parked timer is not a reason to remediate the ACL.)

**Sub-gotcha — pre-existing files miss the named-user inheritance.** A file created in `mailbox/recovery` *before* codex was added to the dir's default ACL keeps the old ACL (no `user:codex`) — a later in-place rewrite preserves it, so codex's persona-consume hits `PermissionError [Errno 13]` on that one file while every sibling works. Observed 2026-08-03: `pantheon-persona-consume@recovery` intermittently RED (exit 5, `"skipped": "transport_error"`) writing `recovery/.wake_marker` — that file uniquely lacked `user:codex` (mtime predated the grant). Fix: `setfacl -m u:codex:rw /home/the-ancestor/mailbox/recovery/.wake_marker` (matches siblings + the dir default; reverse with `setfacl -x u:codex`). codex additionally landed a code fix (atomic same-parent replacement) so rewrites re-inherit the dir default ACL and can't reintroduce the gap. Diagnose an intermittent per-persona-consume EACCES by `getfacl`-diffing the failing file against a working sibling — the gap is a missing named-user entry, not `group::`/`other::`.

Refs: `tickets/active/T-1733.md` (remediation note), ledger `2026-07-20T00:18:40Z-ho-t1733-mailbox-read-acl-remediation`, ORG-038; `tickets/active/T-2382.md` (persona-host + the `.wake_marker` incident), ledger `2026-08-03T22:56:00Z-orchestrator-t2382-wakemarker-acl-fix-plus-dual-authority-audit`.
