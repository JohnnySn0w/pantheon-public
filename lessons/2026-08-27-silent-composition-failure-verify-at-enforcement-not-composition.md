# Lesson — silent composition failure: verify at the enforcement point, not the composition point

**Surfaced:** 2026-08-27, T-3274 Tranche D (planner_sandbox sandbox launcher, ORG-080 §Component contract C5 mechanical proof). Filed by Orchestrator (HO); the three distinct concrete instances landed as `bugs/` entries during the same commit sweep.

## The pattern

You compose a defense-in-depth mechanism from N primitives (systemd properties, seccomp filters, kernel-namespace flags, address-family denials). The composition-time surface (`--property=` calls, `.load()` calls, unit-start) reports **success** — no error, no warning, no non-zero exit. The mechanism nonetheless silently fails to enforce what the composition claimed: primitives get **dropped**, **degraded**, or **canceled** at a layer downstream of the surface that reported success.

The failure is invisible from the composition-point signal (exit code, unit-state, tool return) and only detectable at the **enforcement point** — the journal, `strace`, a canary probe that adversarially tries the thing the confinement is supposed to block. Without journal-reading / canary discipline, the sandbox **ships as false-positive "confinement"**: looks like it's enforcing, isn't.

## The tell

Any of these composition patterns should trigger the discipline:

- Passing **space-joined values** to a config setter that treats them as one token but silently accepts the malformed value (systemd `--property=SystemCallFilter=~@a ~@b ~@c ...` drops all but the first).
- **Sentinel values** valid in one parser and not another (systemd unit-file `RestrictAddressFamilies=none` vs `--property='RestrictAddressFamilies=none'` — the second fails LOUD, but sibling sentinels can fail silent).
- **Library defaults** that assume standalone-caller semantics and probe/install auxiliary state (libseccomp `.load()` self-installing `PR_SET_NO_NEW_PRIVS` via `prctl`, blocked by any composition that filters `prctl`).
- **Kernel-flag graceful-degrade** when the flag can't be honored (systemd `PrivateNetwork=yes`/`PrivateIPC=yes` degrading silently to "we lack privileges... proceeding without" under `apparmor_restrict_unprivileged_userns=1`).
- **Any "N-primitives → one composed enforcement"** where each primitive's failure would be independently invisible.

## Evidence (T-3274 Tranche D, three distinct instances in one arc)

1. **`SystemCallFilter=~@process ~@privileged ~@resources ~@mount ...` via a single `--property=`** silently drops 11 of 12 groups; unit reports clean start; only journal shows `System call ~@X is not known, ignoring.` (→ `bugs/systemd-run-systemcallfilter-space-joined-silently-drops-groups.md`).
2. **libseccomp `seccomp_load()`** returns `ECANCELED` under `SystemCallFilter=~@process` because it tries to self-install NNP via `prctl` (member of `@process`); the composition-time complete-and-load call fails opaquely; only `strace` identifies the offending syscall (→ `bugs/libseccomp-seccomp-load-nnp-self-install-blocked-by-syscallfilter-process.md`).
3. **`PrivateNetwork/PrivateIPC` silent degrade** under `apparmor_restrict_unprivileged_userns=1` — journal-visible warning only, unit still starts, network denial actually delegated to the sibling seccomp `RestrictAddressFamilies` filter (works, but the ADR's mechanism attribution is half-true on this host class).

All three needed journal + bisection to isolate. Prose iteration through the ADR would not have caught any of them — which is why T-3273 → T-3274 pivoted from prose-only iteration to mechanical reference-implementation against real host semantics.

## Mitigation

Before shipping any defense-in-depth composition:

1. **Verify at the enforcement point, not the composition point.** After the sandbox starts, run an adversarial **canary** that attempts the thing the confinement is supposed to block (spawn subprocess, connect socket, write outside scratch, escalate uid) and confirm the kernel refuses via EPERM / ECANCELED / typed error — not just that the composition-time surface returned 0.

2. **Read the journal on the happy path.** `journalctl --user -u <unit> --no-pager | grep -iE 'warn|error|ignor|not known|proceeding without|degrad'` — silent-drop and silent-degrade both surface here even when the unit-state and exit code are clean.

3. **Bisect any opaque failure.** ECANCELED / "Invalid argument" / EXIT_226 at composition time usually names a specific primitive. Per-property bisection or `strace` at the failure boundary pins it precisely — do this before rationalizing around it.

4. **Under-document nothing about the composition context.** If a fix requires disabling a library default (`SCMP_FLTATR_CTL_NNP=0`) or substituting a sibling primitive (`RestrictAddressFamilies=~AF_INET AF_INET6 ...` instead of `=none`), the code + docstring MUST name the ambient-environment precondition the substitution relies on — otherwise a future edit that changes the ambient loses the safety guarantee silently.

## Implication for reviewer discipline

Reviewer axes for any sandbox/composition arc should include:

- **Journal-read requirement:** "Did the implementer confirm the composition enforces via journal-clean-of-warn/degrade, not just via unit-state-active?"
- **Canary-probe presence:** "Does the test suite include at least one adversarial canary that tries the forbidden operation and asserts kernel refusal?"
- **Ambient-precondition documentation:** "Does any workaround that disables a library default explicitly name the systemd/kernel/apparmor state it depends on for safety?"

## Neighbours

- `2026-06-26-silent-no-op-wrong-subsystem-wiring.md` (different domain: control wired to wrong subsystem's apply/restart op; same "looks-fine, isn't-actually-doing-the-thing" shape at a different tier).
- `2026-06-26-ships-green-while-incomplete.md` (composition-cleanliness signal ≠ correctness signal, adjacent framing).

## Design implication (if this hardens)

Candidate directive shape: **D-CANDIDATE — defense-in-depth compositions require enforcement-point verification.** Any component contract whose `verify` block includes a sandbox/isolation/filtering primitive MUST have (a) at least one adversarial canary asserting the primitive's kernel-level enforcement, (b) a journal-check step in its acceptance smoke, (c) documented ambient-preconditions for any library-default disable. Not yet ratified — one arc of evidence is enough for a lesson, not enough for a directive. Re-evaluate if a second arc (echo-prismatic sandbox, another persona-installer isolation gap, etc.) surfaces the same pattern.
