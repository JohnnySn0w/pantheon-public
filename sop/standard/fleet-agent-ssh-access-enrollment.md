# Fleet-agent SSH-access enrollment SOP

**Purpose.** Enroll a Pantheon-fleet agent's SSH key on one of the operator's
personal devices (currently: his W11 laptop `operator-laptop` at
`100.110.28.146`), using the toggle-gated operator-owned-sshd pattern
originally proven for Recovery under T-1953. The SOP generalizes T-1953's
Recovery-specific runbook into a repeatable enrollment protocol so future
per-agent enrollments (mason/artisan/archivist/specialist on demand) do not
recapitulate the design.

**Status:** Adopted 2026-09-13 by Orchestrator (HO) under principles-as-ratification
(below-goal-level engineering-practice per ORG-093; the operator override-available).
Co-drive reviewed by Recovery 2026-09-13 (T-3664 Phase 2). Second-agent proof of
pattern (Orchestrator enrollment 2026-09-13). Full end-to-end verified 2026-09-13T23:22Z
per the operator TG 9763: `Stop-Service sshd` severs confirmed (ssh probe timed out
at 10s per documented Windows Firewall DROP behavior), toggle mechanism proven
in both directions for both enrolled agents.

Original runbook lives at
[`notes/2026-07-22-operator-w11-laptop-control-runbook.md`](../../notes/2026-07-22-operator-w11-laptop-control-runbook.md)
and remains authoritative for the device-side setup that the operator has
already completed once. This SOP handles the per-agent enrollment ON TOP
of that setup.

## Preconditions (verify BEFORE requesting an operator-window)

- The operator's W11 laptop is on the tailnet at `operator-laptop` (100.110.28.146).
- OpenSSH server is installed + running (Recovery has verified since T-1953
  resolve 2026-08-28); the operator's admin user is `operator`.
- The `C:\ProgramData\ssh\administrators_authorized_keys` file exists
  with strict ACL (`NT AUTHORITY\SYSTEM:(F)`, `BUILTIN\Administrators:(F)`,
  no inheritance) and at least one enrolled key.
- The operator has consented to this specific agent's enrollment (TG or other
  direct channel). Second-hand relayed consent must be validated at the
  trust boundary before the enrolling agent (typically Recovery) touches the
  laptop — see [T-3664 Recovery-side validation precedent](../../ledger/2026-W37/2026-W37.jsonl)
  (`grep t3664 ledger/2026-W37/2026-W37.jsonl`).

If any of the above are unmet, this SOP does not apply — fall back to
the T-1953 runbook for first-time device setup.

## Design invariants (do not violate)

1. **One key per (agent, endpoint).** Never reuse deploy keys across
   endpoints or across agents. Filename convention:
   `~/.ssh/id_ed25519_operator_w11` on the enrolling agent's account,
   with a comment identifying the agent + ticket.
2. **Enrollment stays on the operator-owned side.** The operator (or Recovery
   acting as validated intermediary) appends the pubkey to
   `administrators_authorized_keys`. Agents never write to the operator's
   laptop directly during enrollment.
3. **Toggle-gate is preserved.** The operator's `Start-Service sshd` /
   `Stop-Service sshd` remains the operational access boundary; the
   enrolled key does nothing while sshd is stopped.
4. **ACL preservation over rewrite.** Append (`Add-Content`) with
   `-Encoding ascii`; never re-create the file, which would drop the
   strict ACL and reset inheritance (see Gotchas §BOM + §ACL).
5. **Revocation stays cheap.** A single-line delete from
   `administrators_authorized_keys` retires the enrollment.

## Enrollment protocol

### Phase 0: agent generates keypair

On the enrolling agent's account:

```bash
ssh-keygen -t ed25519 \
  -f ~/.ssh/id_ed25519_operator_w11 \
  -N "" \
  -C "<agent>@pantheon-vps -> operator-w11 (T-NNNN)"
```

Where `<agent>` is the persona name (orchestrator/specialist/recovery/…) and `T-NNNN`
is this enrollment's tracking ticket.

Emit the public key (`cat ~/.ssh/id_ed25519_operator_w11.pub`) for handoff.
Never emit the private key.

### Phase 1: pubkey handoff

Two supported shapes:

- **Direct** — enrolling agent hands the pubkey to the operator via their own
  Telegram channel. Simplest; use when the enrolling agent has an
  outbound Telegram channel.
- **Recovery-relayed** — enrolling agent mailboxes the pubkey to Recovery with
  the T-ticket + the operator's consent citation; Recovery relays to the operator on
  her own working channel and drives the append. Use when the enrolling
  agent lacks a suitable direct channel, or when the operator has explicitly
  asked for Recovery-mediated handoff. Recovery must independently validate the
  consent at the trust boundary before writing.

Either shape: the handoff carries the pubkey + the T-ticket + the
operator-consent citation (TG msg id or equivalent).

### Phase 2: operator-side append (with Recovery or the operator executing)

```powershell
# Append the new pubkey to the admin file, preserving strict ACL.
# CRITICAL: -Encoding ascii avoids the UTF-16/BOM footgun that breaks sshd parsing.
Add-Content -Encoding ascii `
  -Path C:\ProgramData\ssh\administrators_authorized_keys `
  -Value "ssh-ed25519 AAAA... <agent>@pantheon-vps -> operator-w11 (T-NNNN)"

# Verify line count went up by exactly 1 and both agents' comments present:
Get-Content C:\ProgramData\ssh\administrators_authorized_keys | Measure-Object -Line
Get-Content C:\ProgramData\ssh\administrators_authorized_keys | Select-String -Pattern "T-\d+"

# ACL should still be intact after append (no new file created); verify:
(Get-Acl C:\ProgramData\ssh\administrators_authorized_keys).AccessToString
# Expected: NT AUTHORITY\SYSTEM Allow FullControl + BUILTIN\Administrators Allow FullControl, no inheritance.
```

If sshd was already running from a prior session, no restart is needed
— `administrators_authorized_keys` is re-read per connection attempt.

### Phase 3: agent-side ssh-config alias

On the enrolling agent's account:

```bash
cat >> ~/.ssh/config <<'EOF'

# T-NNNN: the operator's W11 laptop — toggle-gated via the operator's sshd Start-Service/Stop-Service
Host operator-w11
    HostName 100.110.28.146
    User operator
    IdentityFile ~/.ssh/id_ed25519_operator_w11
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
EOF
```

`IdentitiesOnly yes` prevents ssh from trying every key in the agent's
`~/.ssh/` (some SSH configs otherwise leak identity-selection to the
first matching key, which causes MaxAuthTries lockouts on unrelated
endpoints).

### Phase 4: end-to-end verification

The operator's sshd should be running (Recovery or the operator confirms
`Get-Service sshd` shows `Running`).

```bash
# 1. Connection + admin context sanity:
ssh operator-w11 'hostname; whoami; Get-Date'
# Expected: operator-laptop + operator-laptop\operator + current time

# 2. COM enumeration (runbook §Verification step 2):
ssh operator-w11 'Get-CimInstance Win32_SerialPort | Format-List Name, DeviceID'
# Empty result is valid if no serial device attached; the point is the query executes.
```

Then coordinate the toggle-severs test with the operator:

```bash
# the operator: Stop-Service sshd
# Agent:
ssh -o ConnectTimeout=5 operator-w11 'echo alive'
# Expected: connection times out (DROP behavior of Windows Firewall — not
# RST/refused; the runbook §Recorded 2026-08-28 documented this).
# the operator: Start-Service sshd
# Agent: retry the ssh command, expect it to succeed again.
```

Record the enrollment in this SOP's `## Enrolled agents` table (below)
+ annotate the tracking ticket + append a ledger anchor with the merge
sha.

## Revocation

Cheap. On the operator-side:

```powershell
# Remove the agent's line from the admin file (preserve others).
# CRITICAL: match on the agent's ACTUAL comment string from the
# `## Enrolled agents` table (regex-escaped), NOT the Invariant-1 template —
# pre-convention keys (e.g. Recovery's) will NOT match the template and would be
# silently retained. Regex-special chars in the comment ( ( ) . -> ) must be
# escaped or use -notmatch [regex]::Escape(...).
$file = "C:\ProgramData\ssh\administrators_authorized_keys"
$comment = "recovery-fleet->operator-w11-laptop (T-1953)"   # <- exact string from the table
(Get-Content $file) | Where-Object { $_ -notmatch [regex]::Escape($comment) } | Set-Content -Encoding ascii $file

# Set-Content overwrites the file IN PLACE (does not delete+recreate), so the
# strict ACL is preserved — unlike New-Item -Force (see Invariant 4 / Gotchas).
# Verify anyway, matching the enrollment's rigor:
(Get-Acl $file).AccessToString
# Expected unchanged: NT AUTHORITY\SYSTEM Allow FullControl + BUILTIN\Administrators Allow FullControl, no inheritance.
```

Or the softer form: leave sshd stopped indefinitely with
`Set-Service sshd -StartupType Manual; Stop-Service sshd` (revokes ALL
fleet access to the laptop until the operator restores).

The retired agent should also remove its ssh-config alias +
`~/.ssh/id_ed25519_operator_w11` on its own account (defense in depth).

## Gotchas

- **BOM footgun** (T-1953 finding): Powershell's default `Set-Content`
  encodes as UTF-16-LE with a BOM, which sshd cannot parse. ALWAYS pass
  `-Encoding ascii`. `Add-Content` inherits the same default; specify
  `-Encoding ascii` on it too.
- **Admin-user auth file location** (T-1953 finding): Windows OpenSSH
  routes an admin-user's authorized_keys to
  `C:\ProgramData\ssh\administrators_authorized_keys`, NOT to
  `~\.ssh\authorized_keys`. Writing to the user-scoped file is
  silently ignored.
- **ACL preservation** (T-1953 finding): the admin file's strict ACL
  (SYSTEM:F + Administrators:F, no inheritance) is required by sshd.
  Append (`Add-Content`) preserves it; re-create (`New-Item -Force` +
  `Set-Content`) does NOT and requires re-applying with `icacls
  /inheritance:r /grant ...`.
- **PowerShell nested-$ escape** (T-3664 Recovery finding, 2026-09-13):
  when running PowerShell as the ssh command, `powershell -Command
  "...$x..."` gets the `$x` expanded by the OUTER shell before it
  reaches PowerShell. Run PS directly as the ssh command, or pass data
  as base64. Saves the round-trip Recovery paid.
- **Mixed-quote one-liners silently no-op** (T-3664 Recovery finding, 2026-09-13):
  a complex ssh one-liner mixing single/double quotes + WQL `-Filter
  "Name='powershell.exe'"` can produce ZERO output AND fail to write, with no
  error — the quoting collapses before the command runs. Do discrete steps and
  verify each (line count, ACL, process count) rather than one clever
  compound command. Prefer `Where-Object { $PSItem.Name -eq "..." }` over
  `-Filter "Name='...'"` to keep single-quotes out of the bash/PS quoting fight.
- **Firewall DROP vs REJECT** (T-1953 finding): when sshd is stopped
  the Windows Firewall silently DROPs the connection rather than
  RST/refuse, so the client sees a timeout not a "connection refused".
  Set `ConnectTimeout` on the verification probe to keep the sever-test
  bounded.
- **Multiple keys in ssh-config** (T-3664 preventive): use
  `IdentitiesOnly yes` in the `Host operator-w11` block to avoid ssh
  trying every key in `~/.ssh/` on connect — this prevents
  MaxAuthTries lockouts on unrelated endpoints that share the tailnet
  IP range.

## Enrolled agents (append-only ledger)

| Agent | Ticket | Enrolled | Key comment | Notes |
|---|---|---|---|---|
| recovery | T-1953 | 2026-08-28 | `recovery-fleet->operator-w11-laptop (T-1953)` | First enrollment; original runbook author. **Legacy comment format** — predates the Invariant-1 `<agent>@pantheon-vps -> operator-w11 (T-NNNN)` convention. Revocation must match on THIS literal string, not the template. |
| orchestrator | T-3664 | 2026-09-13 | `orchestrator@pantheon-vps -> operator-w11 (T-3664)` | Second enrollment; SOP-generalizing arc. Recovery-relayed handoff. End-to-end verified 2026-09-13T23:22Z including toggle-severs (the operator TG 9763). |

> **The comment strings in this table are the source of truth for revocation.**
> Existing keys may use pre-convention formats (see Recovery's row). Always copy the
> exact string from here into the Revocation match pattern; the Invariant-1
> template only governs NEW enrollments going forward.

## Cross-references

- **Original runbook** (device-first setup, Recovery-specific):
  [`notes/2026-07-22-operator-w11-laptop-control-runbook.md`](../../notes/2026-07-22-operator-w11-laptop-control-runbook.md)
- **Adoption arc:** T-3664 (Orchestrator enrollment 2026-09-13, SOP author; Recovery co-drive review Phase 2).
- **Original enrollment arc:** T-1953 (Recovery enrollment 2026-08-28).
- **Per-agent per-endpoint deploy-key rule:** memory hook
  `feedback_github_per_repo_deploy_keys` (Orchestrator's memory) — same-shape rule for
  GitHub deploy keys (per-repo, per-agent), applied here as per-endpoint
  per-agent. (Referenced by hook name only: agent memories live under
  700-mode homes and are not repo-navigable — do not link them by filesystem
  path, which escapes the repo and 404s / EACCES for every other agent.)
- **Tailscale + admin console pattern:** memory hook
  `reference_tailscale_service_subdomain_pattern` (Orchestrator's memory) — context on
  the operator's tailnet convention (not directly applicable to bare-SSH; SSH runs
  over the tailnet IP, not a serve subdomain).
