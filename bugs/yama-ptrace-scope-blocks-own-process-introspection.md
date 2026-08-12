---
symptom: "With kernel.yama.ptrace_scope=1 (the VPS default), a same-uid process that is not your direct child (e.g. a systemd --user daemon) denies strace -p (PTRACE_SEIZE EPERM), readlink /proc/PID/fd/*, and cat /proc/PID/environ with Permission denied — these look like sandbox restrictions or elevated credentials and can be misattributed as evidence of the process's health (e.g. a false 'zero sockets open' reading and a wrong 'session-sandbox artifact' claim that survived into a ticket)."
fix: "Treat these denials on a same-uid non-child daemon as normal and uninformative about health; cross-check with sources that don't require ptrace access (ss -tnp, broker/peer logs) instead of deriving claims from denied /proc reads; use sudo (sudo cat /proc/PID/environ, sudo strace -p) to bypass Yama when real introspection is needed; and check cat /proc/sys/kernel/yama/ptrace_scope first as the 2-second test before theorizing."
domain: pantheon, linux, yama, ptrace, process-introspection, systemd, echo
---

# Yama ptrace_scope=1 blocks introspection of your OWN processes — don't misattribute the denials

**Surfaced:** 2026-07-12, T-1686 investigation (echo daemon journal-darkness).

## The gotcha

With `kernel.yama.ptrace_scope=1` (the default on this VPS and most modern distros), a
same-uid process that is NOT your direct child refuses: `strace -p` (PTRACE_SEIZE EPERM),
`readlink /proc/PID/fd/*` (Permission denied), `cat /proc/PID/environ` (Permission denied).
These look exactly like sandbox restrictions or like the process having elevated/hardened
credentials — during T-1686 this produced a false "zero sockets open" reading and a wrong
"session-sandbox artifact" attribution that survived into a ticket before being corrected.

## The rules

1. A same-uid daemon (systemd --user service, background task) is NOT your child — expect
   these denials as NORMAL; they carry zero information about the process's health.
2. Never derive claims like "no sockets" / "no env" from denied /proc reads. Cross-check with
   sources that don't need ptrace access-mode: `ss -tnp` (own), broker/peer logs, `sudo` reads.
3. Root bypasses Yama: `sudo cat /proc/PID/environ`, `sudo strace -p` work fine — use them
   before theorizing.
4. Check first, speculate second: `cat /proc/sys/kernel/yama/ptrace_scope` is the 2-second
   test that explains all of the above.

## Keywords

yama, ptrace_scope, PTRACE_SEIZE, EPERM, strace -p, /proc/PID/fd, /proc/PID/environ,
Permission denied, same-uid daemon, systemd --user, sudo bypass, ss -tnp,
zero sockets open, session-sandbox artifact, T-1686, echo daemon, journal-darkness,
misattribution, introspection denial
