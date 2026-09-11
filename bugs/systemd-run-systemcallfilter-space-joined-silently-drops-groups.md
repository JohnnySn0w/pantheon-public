---
symptom: "systemd-run --user --property='SystemCallFilter=~@process ~@privileged ~@resources ...' starts cleanly (rc=0, unit reports fine) but silently enforces ONLY the first syscall group in the space-joined value. Every remaining group is dropped with a journalctl warning 'System call ~@<X> is not known, ignoring.' — visible in the journal, invisible to exit code / stdout / stderr / unit-state. Consequence: the sandbox ships as false-positive confinement — unit starts, looks fine, isn't actually enforcing what the caller claims. Exactly the 'silently and gracefully turned off' failure systemd.exec(5) warns about, but the warning surfaces only in a journal a caller typically doesn't read on happy-path."
fix: "Use one --property=SystemCallFilter= per syscall group — do NOT space-join values into a single --property=. E.g. --property='SystemCallFilter=~@process' --property='SystemCallFilter=~@privileged' --property='SystemCallFilter=~@resources' ...one line per group. Same rule applies to any other --property= value that syntactically supports a space-joined list in a unit-file (BindReadOnlyPaths=, BindPaths=, ReadWritePaths=, ReadOnlyPaths=, InaccessiblePaths=, etc.) — the unit-file text-parser understands space-joined lists; the --property= KEY=VALUE surface does NOT reliably split them (systemd bug shape: the property setter treats the whole VALUE as one token for a subset of property types). Reference impl: tools/germinate/planner_sandbox/launcher/systemd_run_shape.py build_properties() emits one --property= per syscall group + per bind-mount, deliberately not concatenating."
domain: pantheon, systemd-run, syscall-filter, silent-degradation, sandbox-composition, planner-sandbox, false-positive-confinement
---

# `systemd-run --property=SystemCallFilter=` — space-joined syscall groups silently drop 11 of 12

**Symptom.** A `systemd-run --user --property='SystemCallFilter=~@process ~@privileged ~@resources ~@mount ~@debug ~@reboot ~@raw-io ~@swap ~@cpu-emulation ~@clock ~@module ~@obsolete'` invocation **starts cleanly** (rc=0, unit reports fine) but silently enforces ONLY the first syscall group in the space-joined value. Every remaining group is dropped with `journalctl` warning `"System call ~@<X> is not known, ignoring."` — visible in the journal, invisible to exit code / stdout / stderr / unit-state.

Consequence: **the sandbox ships as false-positive "confinement"** — unit starts, looks fine, isn't actually enforcing what the caller claims. Exactly the "silently and gracefully turned off" failure `systemd.exec(5)` warns about, but the warning surfaces only in the journal a caller typically doesn't read on happy-path.

## Repro

```bash
# Silent-drop shape (11 of 12 groups dropped):
systemd-run --user --scope --wait --pipe \
  --property='SystemCallFilter=~@process ~@privileged ~@resources ~@mount ~@debug ~@reboot ~@raw-io ~@swap ~@cpu-emulation ~@clock ~@module ~@obsolete' \
  --unit=probe-space-joined \
  -- /bin/true
# Exit: 0. Unit clean. But:
journalctl --user -u probe-space-joined --no-pager | grep -i 'not known'
# System call ~@privileged is not known, ignoring.
# System call ~@resources is not known, ignoring.
# ... (11 total dropped)
```

## Verification

Verified 2026-08-27 during T-3295 (Tranche D of T-3274 planner_sandbox reference-implementation) on Ubuntu 24.04 KVM guest, systemd 255.4-1ubuntu8.17. See T-3295 impl DECISIONS (annotation on `tickets/done/T-3295.md`) + T-3274 arc-close annotation (`tickets/done/T-3274.md`) + ledger `led_20260827T005756Z` for the empirical isolation. Independently corroborated by the T-3295 reviewer (`rev-` subagent) on a separate probe.

## Fix

**Use one `--property=SystemCallFilter=` per syscall group** — do NOT space-join values into a single `--property=`.

```bash
# Correct shape — one --property= per group:
systemd-run --user --scope --wait --pipe \
  --property='SystemCallFilter=~@process' \
  --property='SystemCallFilter=~@privileged' \
  --property='SystemCallFilter=~@resources' \
  --property='SystemCallFilter=~@mount' \
  --property='SystemCallFilter=~@debug' \
  --property='SystemCallFilter=~@reboot' \
  --property='SystemCallFilter=~@raw-io' \
  --property='SystemCallFilter=~@swap' \
  --property='SystemCallFilter=~@cpu-emulation' \
  --property='SystemCallFilter=~@clock' \
  --property='SystemCallFilter=~@module' \
  --property='SystemCallFilter=~@obsolete' \
  --property='SystemCallErrorNumber=EPERM' \
  --unit=probe-correct \
  -- /bin/true
```

Same rule applies to any other `--property=` value that syntactically supports a space-joined list in a unit-file — `BindReadOnlyPaths=`, `BindPaths=`, `ReadWritePaths=`, `ReadOnlyPaths=`, `InaccessiblePaths=`, etc. — when passed through `systemd-run --property=`. The unit-file text-parser understands space-joined lists; the `--property=` KEY=VALUE surface does NOT reliably split them (systemd bug shape: the property setter treats the whole VALUE as one token for a subset of property types).

Reference in the ref-impl: `tools/germinate/planner_sandbox/launcher/systemd_run_shape.py` `build_properties(...)` emits one `--property=` per syscall group + per bind-mount, deliberately not concatenating.

## Related bugs

- `restrictaddressfamilies-none-rejected-via-property-flag-use-denylist-form.md` (sibling; another `systemd-run --property=` parsing gotcha found in the same T-3274 arc)
- `libseccomp-seccomp-load-nnp-self-install-blocked-by-syscallfilter-process.md` (composing gotcha; when the workaround here is properly in place, libseccomp still fails without `SCMP_FLTATR_CTL_NNP=0`)

## Escalation path

Not a Pantheon bug — upstream systemd behavior. Filing here for grep-transfer to any future arc that uses `systemd-run --property=` for sandbox composition. If upstream ever fixes the `--property=` parser to split space-joined values consistently, this entry retires.

## Discovered by

T-3295 implementer (2026-08-27) via journal + bisection — invisible from exit codes alone. Discovery required a `journalctl --user -u <unit>` read on a happy-path unit start, which is not a default sanity check for a passing test. **T-3274 arc took this as evidence for the D11 discipline: never trust "unit started OK" as evidence of what the unit is actually enforcing — verify at the journal.**
