---
symptom: "systemd-run --property='RestrictAddressFamilies=none' fails at unit-property-set time with 'Failed to set unit properties: Invalid argument'. The bareword 'none' (documented systemd.exec(5) value that denies ALL address families) is only parsed correctly in a unit-file text context, NOT via the --property= runtime setter. Consequence: a caller trying to compose an all-network-blocked sandbox via systemd-run --property= gets a unit that never starts, breaking any fail-fast path that expected RestrictAddressFamilies=none to be the strongest available network denial."
fix: "Use the deny-list form — enumerate address families to deny, prefixed with ~: --property='RestrictAddressFamilies=~AF_INET AF_INET6 AF_UNIX AF_NETLINK AF_PACKET AF_BLUETOOTH AF_VSOCK AF_XDP'. This form parses correctly via --property= and gives equivalent semantics: denying every listed family effectively bans all reachable network + local-socket surface (AF_INET/AF_INET6 for network; AF_UNIX for D-Bus + socket-activated services; AF_NETLINK/AF_PACKET/AF_BLUETOOTH/AF_VSOCK/AF_XDP for defense-in-depth against less common families). Reference impl: tools/germinate/planner_sandbox/launcher/systemd_run_shape.py uses the enumerated deny-list form deliberately, not 'none'."
domain: pantheon, systemd-run, sandbox, network-restriction, planner-sandbox, silent-property-rejection
---

# `systemd-run --property='RestrictAddressFamilies=none'` — rejected; use deny-list form instead

**Symptom.** `systemd-run --property='RestrictAddressFamilies=none'` fails at unit-property-set time with `Failed to set unit properties: Invalid argument`. The bareword `none` (which is the documented `systemd.exec(5)` value that denies ALL address families) is only parsed correctly in a unit-file text context, NOT via the `--property=` runtime setter.

Consequence: a caller trying to compose an all-network-blocked sandbox via `systemd-run --property=` gets a unit that never starts, breaking any fail-fast path that expected `RestrictAddressFamilies=none` to be the strongest available network denial.

## Repro

```bash
systemd-run --user --scope --wait --pipe \
  --property='RestrictAddressFamilies=none' \
  --unit=probe-raf-none \
  -- /bin/true
# Failed to set unit properties: Invalid argument
```

## Verification

Verified 2026-08-27 during T-3295 (Tranche D of T-3274 planner_sandbox reference-implementation) on Ubuntu 24.04 KVM guest, systemd 255.4-1ubuntu8.17. See `tickets/done/T-3295.md` DECISIONS + T-3274 arc-close annotation + ledger `led_20260827T005756Z`.

## Fix

Use the **deny-list form** — enumerate address families to deny, prefixed with `~`:

```bash
systemd-run --user --scope --wait --pipe \
  --property='RestrictAddressFamilies=~AF_INET AF_INET6 AF_UNIX AF_NETLINK AF_PACKET AF_BLUETOOTH AF_VSOCK AF_XDP' \
  --unit=probe-raf-denylist \
  -- /bin/true
# rc=0; RestrictAddressFamilies actually applied.
```

The deny-list form parses correctly via `--property=` and gives equivalent semantics: denying every address family listed effectively bans all reachable network + local-socket surface (AF_INET/AF_INET6 for network; AF_UNIX for D-Bus + socket-activated services; add AF_NETLINK/AF_PACKET/AF_BLUETOOTH/AF_VSOCK/AF_XDP for defense-in-depth against less common families).

Reference in the ref-impl: `tools/germinate/planner_sandbox/launcher/systemd_run_shape.py` uses the enumerated deny-list form deliberately, not `none`.

## Related bugs

- `systemd-run-systemcallfilter-space-joined-silently-drops-groups.md` (sibling; another `systemd-run --property=` parsing gotcha found in the same T-3274 arc — WORSE shape because it fails SILENTLY rather than loudly)

## Escalation path

Not a Pantheon bug — upstream systemd `--property=` parser handles the `none` sentinel differently than unit-file text. If upstream ever unifies parsing between the two surfaces, this entry retires.

## Discovered by

T-3295 implementer (2026-08-27) during systemd-run property composition. Loud failure (unlike the SystemCallFilter sibling gotcha which fails silent). Caught immediately by the unit-start refusal path.
