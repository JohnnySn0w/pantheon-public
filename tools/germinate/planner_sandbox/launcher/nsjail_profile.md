# nsjail fallback equivalence table (documentary only)

Per `architecture/adr/ORG-080-germination-engine.md` Component contract (L109-111): "On a
system without systemd-user available, an in-process **nsjail** fallback...
the exact nsjail equivalence table is checked in at
`tools/germinate/planner_sandbox/nsjail_profile.md`."

## Status on the T-3295 implementation host

**No runnable nsjail leg.** `nsjail` is not installed and not packaged for
Ubuntu 24.04 (noble) — `apt-cache search nsjail` returns nothing, and there
is no `nsjail` candidate under any pocket (`noble`, `noble-updates`,
`noble-security`). Building from source needs a protobuf-compiler +
libprotobuf-dev + flex/bison (kafel) toolchain, out of scope for this
tranche. `tools/germinate/planner_sandbox/tests/test_launcher_c5.py`
carries one typed-SKIP test proving this is detected and reported with a
concrete reason (never a bare "skipped") rather than silently omitted.

This document is the equivalence table only — the mapping a future
implementation needs when nsjail actually is available.

## Equivalence table

| systemd `--property=` | nsjail flag(s) | Notes |
|---|---|---|
| `SystemCallFilter=~@group` (×12, one per group) | `--seccomp_policy=<inline kafel policy>` + `--seccomp_syscall_errno=EPERM` | nsjail has no named syscall groups (`@process`, `@privileged`, ...) — the systemd group→syscall-number expansion (`systemd-analyze syscall-filter @<group>`) must be flattened into an explicit kafel policy file enumerating each syscall. |
| `RestrictAddressFamilies=~AF_X` (×N, one per family) | same kafel seccomp policy | AF-family checks compile into the same BPF program as the syscall filter under nsjail; no separate nsjail primitive. |
| `NoNewPrivileges=yes` | on by default under nsjail's confinement mode | No explicit flag needed — nsjail always sets `PR_SET_NO_NEW_PRIVS` for its jailed children. |
| `AmbientCapabilities=` (empty) | `--disable_no_new_privs=false` (default) + no `--cap_add` flags | nsjail drops all capabilities by default unless explicitly re-added via `--cap_add`. |
| `CapabilityBoundingSet=` | *(omitted for user-scope on this host — see `systemd_run_shape.py` deviation 3)* | nsjail's own `--cap_add`/default-empty capability handling is the equivalent when running as an unprivileged user; the same `CAP_SETPCAP` constraint that blocks the systemd property applies equally to any unprivileged nsjail invocation attempting the same restriction. |
| `TemporaryFileSystem=/:mode=755` + `BindReadOnlyPaths=`/`BindPaths=` | `--mount ro:<src>:<dst>` (repeated, one per read-only path) + `--mount rw:<scratch>:<scratch>` for the writable scratch region | nsjail has no single "everything absent by default" primitive equivalent to `TemporaryFileSystem=/` — its default root is either a fresh empty overlay (`--disable_clone_newns=false`, the default) or bind-mounted explicitly; must be paired with an explicit deny-by-default posture (nsjail's default IS deny-by-default for mounts, matching the intent). |
| `RestrictNamespaces=yes` | `--use_cgroupv2` + default namespace unsharing (nsjail unshares mount/PID/UTS/IPC/net namespaces by default unless `--disable_*` flags are passed) | nsjail's default posture is closer to "namespace everything" than systemd's opt-in `RestrictNamespaces=`; the equivalence is "don't pass any `--disable_clone_new*` flag." |
| `PrivateUsers=yes` | `--user_ns_uid_map <inside_uid>:<outside_uid>:1` + `--user_ns_gid_map` | Explicit UID/GID mapping into a single-user namespace, standing in for the "map only my own uid" behavior `PrivateUsers=yes` gives under systemd. |
| `PrivateNetwork=yes` | default (nsjail unshares the network namespace unless `--disable_clone_newnet` is passed) | Same silent-degrade risk as the systemd property on hosts with `apparmor_restrict_unprivileged_userns=1` — the address-family seccomp policy above is the real backstop either way, not the namespace. |
| `PrivateMounts=yes` / `PrivateTmp=yes` | default (nsjail always uses a private mount namespace + a jail-local `/tmp` per its `--tmpfsmount` option) | |
| `RuntimeMaxSec=` | `--time_limit <seconds>` | |
| Inherited fds (stdin/stdout/stderr only) | default (nsjail does not forward extra fds unless `--pass_fd` is used) | |

## Follow-up

A live nsjail leg (build-from-source or a future Ubuntu package) is a
follow-up ticket, not blocking T-3295 — the 5 required C5 canaries all
exercise the systemd-run --user path per PO approval (T-3295 Phase 1 open
question 3).
