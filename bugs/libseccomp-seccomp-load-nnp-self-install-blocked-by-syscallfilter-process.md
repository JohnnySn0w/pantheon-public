---
symptom: "In-process libseccomp (pyseccomp binding) SyscallFilter().load() fails with ECANCELED when running inside a systemd unit whose SystemCallFilter=~@process denies the prctl syscall — even though systemd has already applied NoNewPrivileges=yes before ExecStart, so the BPF program itself would install fine. Root cause: libseccomp self-installs PR_SET_NO_NEW_PRIVS via prctl(PR_SET_NO_NEW_PRIVS, 1) as a safety measure BEFORE loading the BPF filter; prctl is in @process, so ~@process denial blocks it and seccomp_load() returns ECANCELED. Consequence: defense-in-depth composition (systemd filters system calls, in-process seccomp adds execve/execveat closure) gets a fail-closed refusal at startup."
fix: "Set SCMP_FLTATR_CTL_NNP attribute to 0 on the filter BEFORE calling .load() — tells libseccomp to skip its own NNP-set call because systemd has already handled it (f.set_attr(seccomp.Attr.CTL_NNP, 0) before f.load()). Load-bearing safety context: this is correct because systemd already set PR_SET_NO_NEW_PRIVS=1 via NoNewPrivileges=yes; NNP is a one-way sticky bit, so re-setting is a no-op and skipping libseccomp's re-set is not a weakening. If NNP were NOT already set by systemd, this fix would be a real hole — set NoNewPrivileges=yes on the unit + SCMP_FLTATR_CTL_NNP=0 in the code. Reference impl: tools/germinate/planner_sandbox/launcher/launcher.py _install_execve_closure()."
domain: pantheon, seccomp, systemd, syscall-filter, security-hardening, defense-in-depth, no-new-privileges, planner-sandbox
---

# libseccomp `seccomp_load()` self-installs `PR_SET_NO_NEW_PRIVS` via `prctl`, blocked by `SystemCallFilter=~@process`

**Symptom.** In-process `libseccomp` (`pyseccomp` binding) `SyscallFilter().load()` fails with `ECANCELED` when running inside a systemd unit whose `SystemCallFilter=~@process` denies the `prctl` syscall — even though systemd itself has already applied `NoNewPrivileges=yes` before the process starts, so the BPF program itself would install fine.

Root cause: libseccomp's `seccomp_load()` defaults to self-installing `PR_SET_NO_NEW_PRIVS` via a `prctl(PR_SET_NO_NEW_PRIVS, 1)` call as a safety measure BEFORE loading the BPF filter. `prctl` is a member of `@process` per `systemd-analyze syscall-filter @process`, so a `SystemCallFilter=~@process` denial blocks the `prctl` call, which returns `ECANCELED` up through `seccomp_load()` even though the actual BPF filter load (which uses `seccomp(SECCOMP_SET_MODE_FILTER, ...)`) would succeed.

Consequence: a caller composing an in-process seccomp closure ON TOP of a systemd-set filter (e.g. the ADR-standard "systemd filters system calls, in-process seccomp adds execve/execveat closure") gets a fail-closed refusal at process startup, blocking legitimate defense-in-depth composition.

## Repro

```bash
# From inside a systemd-run --user unit with SystemCallFilter=~@process:
python3 -c "
import pyseccomp as seccomp
f = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
f.add_rule(seccomp.ERRNO(1), 'execve')
f.load()
"
# libseccomp: __sys_arch_native: prctl(PR_SET_NO_NEW_PRIVS): ECANCELED
```

## Verification

Verified 2026-08-27 during T-3295 (Tranche D of T-3274 planner_sandbox reference-implementation) via per-group bisection of `SystemCallFilter=` value + `strace` on the libseccomp `seccomp_load()` call. See `tickets/done/T-3295.md` DECISIONS + T-3274 arc-close annotation.

## Fix

Set `SCMP_FLTATR_CTL_NNP` attribute to `0` on the filter BEFORE calling `.load()` — tells libseccomp to skip its own NNP-set call because systemd has already handled it:

```python
import pyseccomp as seccomp

f = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
f.set_attr(seccomp.Attr.CTL_NNP, 0)  # <-- systemd already set NNP; don't re-set
f.add_rule(seccomp.ERRNO(1), 'execve')
f.add_rule(seccomp.ERRNO(1), 'execveat')
f.load()
# rc=0; BPF program installed; execve/execveat return EPERM as intended.
```

**Load-bearing safety context:** disabling libseccomp's own NNP-set attempt is correct HERE (not a weakening) because:
1. Systemd set `PR_SET_NO_NEW_PRIVS=1` before this process's `ExecStart` per `NoNewPrivileges=yes` on the unit.
2. `PR_SET_NO_NEW_PRIVS` is a one-way sticky bit: once set, it stays set for the process + descendants.
3. Attempting to re-set an already-set NNP bit is a no-op in kernel semantics (redundant, not weakening).
4. What libseccomp is trying to prevent (running BPF filter without NNP guarding it) is already fully prevented — systemd's earlier set is the guarantee.

If NNP were NOT already set by systemd, this fix would be a real safety hole. In that case fix the composition instead: set `NoNewPrivileges=yes` on the unit + `SCMP_FLTATR_CTL_NNP=0` in the code.

Reference in the ref-impl: `tools/germinate/planner_sandbox/launcher/launcher.py` `_install_execve_closure()`.

## Related bugs

- `systemd-run-systemcallfilter-space-joined-silently-drops-groups.md` (sibling; when the SystemCallFilter shape is right, THIS bug is what you hit next)
- `restrictaddressfamilies-none-rejected-via-property-flag-use-denylist-form.md` (sibling; found in the same T-3274 arc)

## Escalation path

Not a Pantheon bug — libseccomp behavior is designed for the standalone-caller case (where the caller IS the one setting NNP). Composing seccomp on top of systemd's sandbox is a valid pattern the default doesn't account for. If libseccomp ever adds an auto-detect for "NNP already set, skip re-set," this entry retires.

## Discovered by

T-3295 implementer (2026-08-27) via `strace` on libseccomp `seccomp_load()` + per-group `SystemCallFilter=` bisection. Invisible from exit codes alone — needed strace to identify the offending `prctl` call and the group it belongs to.
