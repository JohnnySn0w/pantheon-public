---
title: bun.exe (WinGet Links shim path) blocked via SSH — "path cannot be traversed because it contains an untrusted mount point"
symptom: "bun invoked by name over non-interactive SSH on Windows fails: 'path cannot be traversed because it contains an untrusted mount point' (NativeCommandFailed); the same bun works in an interactive local shell"
fix: "Invoke bun via its full WinGet Packages-dir path (not the Links shim) inside a cmd /c wrapper, not raw PowerShell; Unblock-File does not help"
domain: windows / winget / bun / ssh / cross-account-ops
severity: annoyance
first-observed: 2026-09-14
last-verified: 2026-09-14
verification: reproduced this session pushing sseed-presence carthing v0.3.0 to the operator's laptop via `ssh operator-w11 'bun install ...'`; workaround (full Packages-path invocation via cmd wrapper) confirmed working end-to-end (build succeeded, dist produced)
---

## Symptom

Invoking bun on a Windows host via non-interactive SSH (PowerShell or cmd) fails with:

```
Program 'bun.exe' failed to run: The path cannot be traversed because it contains an untrusted mount point
    + CategoryInfo          : ResourceUnavailable: (:) [], ApplicationFailedException
    + FullyQualifiedErrorId : NativeCommandFailed
```

Happens regardless of shell choice (PowerShell direct, `cmd /c "..."`) as long as bun is invoked by name from `PATH`. `Unblock-File` on the resolved bun.exe does not fix it — the Zone.Identifier stream isn't the actual gate.

Reproduced when the operator's `bun` on `PATH` resolves to `C:\Users\micha\AppData\Local\Microsoft\WinGet\Links\bun.exe` (the WinGet Links shim), which itself points at `C:\Users\micha\AppData\Local\Microsoft\WinGet\Packages\Oven-sh.Bun_Microsoft.Winget.Source_8wekyb3d8bbwe\bun-windows-x64\bun.exe`.

Same user, same box, interactive local shell: `bun` works fine. Only fails in non-interactive SSH context — different security token, different mount-trust state.

## Root cause

WinGet 1.6+ installs some packages under an "untrusted mount point" per Windows Defender / Attack Surface Reduction rules; the Links directory contains re-parse-point shims that cross that mount boundary. Windows blocks reparse-point traversal from a non-interactive SSH-launched process token (different SID/session context than the interactive user's shell), even when both share the file-permission ACL. `Unblock-File` clears Mark-of-the-Web on downloaded files, not the WinGet mount-trust classification.

Not specific to bun — any WinGet-installed CLI whose Links shim crosses this mount boundary can trip it. Bun happens to be the case we hit.

## Surgical fixes

**Workaround (verified 2026-09-14):** invoke bun via the full Packages-directory path inside a `cmd /c` wrapper, bypassing the Links shim entirely:

```bash
BUN='C:\Users\micha\AppData\Local\Microsoft\WinGet\Packages\Oven-sh.Bun_Microsoft.Winget.Source_8wekyb3d8bbwe\bun-windows-x64\bun.exe'
ssh operator-w11 "cmd /c \"cd C:\\path\\to\\project && $BUN install --frozen-lockfile\""
```

Two moving parts both matter:
1. **Full path, not the Links shim.** Points directly at the executable, not the reparse-point stub. The mount-trust check doesn't fire for a direct-path invocation.
2. **`cmd /c` wrapper, not raw PowerShell.** PowerShell hits a different failure mode on the same invocation ("The system cannot execute the specified program") — the `cmd` layer normalizes the exec context.

**Root fix (deferred, not attempted this arc):** either install bun via non-WinGet path (Scoop, direct download, npm), or configure the WinGet mount-trust classification (Group Policy — requires admin on the operator's box). Neither is worth doing until this bites often; the workaround is one line.

## When this recurs

- Any cross-account op that shells `bun` (or another WinGet-shim'd tool) into the operator's laptop from the VPS.
- Not just bun: same shape for any WinGet 1.6+ installed CLI whose Links directory crosses the mount boundary. Test with a direct-path invocation before assuming the tool itself is broken.
- Symptom is distinctive enough to grep for ("untrusted mount point" + "NativeCommandFailed") — that string in your terminal is the flag to reach for this file.

## Related

- The Phase-A `push.ts` local patches on the operator's laptop (`UserKnownHostsFile=/dev/null` → `NUL`, `SKIP_PUSH=1` short-circuit around the Windows tar-over-ssh hang) — sibling class of Windows-hostility issues for the sseed-presence carthing push chain. See T-3664 close-note + Stage 1/3 client-push ledger entries for the full inventory.
