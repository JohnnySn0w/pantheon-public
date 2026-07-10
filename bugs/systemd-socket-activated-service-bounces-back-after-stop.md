---
symptom: "`systemctl stop` of a socket-activated `.service` returns warning `Stopping ... but its triggering units are still active: <name>.socket`; the service immediately bounces back to active on the next connection attempt"
fix: "stop + disable the `.socket` BEFORE stopping the `.service`. If a stop attempt has already trip-failed the service (non-zero exit when stopped mid-run), `systemctl reset-failed <unit>` clears the state to `inactive (dead)`."
domain: systemd, socket-activation
---

> **Superseded as load-bearing by** [`sop/remediation/socket-activated-agent-daemon-gotchas.md`](../sop/remediation/socket-activated-agent-daemon-gotchas.md) — the live remediation guidance these symptoms taught now lives in that SOP (ORG-012 supersedure-trail, T-0832 PR-2 / T-0853). This entry remains the recorded symptom + diagnosis; act on the SOP for current guidance.

## Symptom

Service unit (e.g. `kino-agent@claude.service`) is paired with a socket unit (`kino-agent@claude.socket`) that activates it on first connection. Operator runs:

```
systemctl stop kino-agent@claude.service
systemctl stop kino-agent@claude.socket
systemctl disable kino-agent@claude.socket
```

The first command emits:

```
Stopping 'kino-agent@claude.service', but its triggering units are still active:
kino-agent@claude.socket
```

After the loop completes, the socket is correctly `disabled` + `inactive`, but `systemctl is-active kino-agent@claude.service` still returns `active`. The service bounced back: between the service-stop and the socket-stop, a queued/inflight connection retriggered the socket, which respawned the service.

## Cause

Socket activation is a respawn primitive — the socket is the source of truth for "should this service exist." Stopping the service while the socket is still listening just hands the work back to the socket on the next accept().

## Fix

Reorder: socket first, service second.

```
for u in claude claude-research claude-fallback; do
  sudo systemctl stop kino-agent@$u.socket
  sudo systemctl disable kino-agent@$u.socket
  sudo systemctl stop kino-agent@$u.service
done
```

If the first (wrong-order) pass already ran and `systemctl is-active <unit>.service` now reports `failed` (the service was killed mid-run and exited non-zero):

```
sudo systemctl reset-failed kino-agent@$u.service
```

Brings the unit state from `failed` to `inactive (dead)` per typical acceptance criteria. Doesn't restart the service.

## Discovered

T-0353 (2026-05-15) — Orchestrator stopping kino-agent@*.service trio per the operator msg_1361+1368. First pass (service-then-socket) bounced back active; second pass (socket-disable-then-service-stop + reset-failed) clean. Resolve at pantheon-config@f07d47c.

## Keywords

systemd, socket activation, stop, disable, bounces back, still active, triggering units, reset-failed, kino-agent, socket-activated service won't stop, service active after stop
