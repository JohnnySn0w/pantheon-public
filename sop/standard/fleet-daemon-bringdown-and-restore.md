# Fleet daemon bringdown and restore (token-minimization pause)

**Status: Drafted by Orchestrator (HO) 2026-08-01, at the operator's request (Telegram 6701) immediately after
performing the bringdown it documents.** Written from a real execution, including the parts that went
wrong: the first pass left **10 failed units** across four seats and fired 5 spurious failure-alerts.
The ordering rule in §3 is the fix for that and is the load-bearing part of this document. sop/ is an
HO-written surface per D2.

## 1. When to use this

The operator wants fleet token burn reduced without decommissioning anything — "turn the daemons off
for everyone except X, Y, Z" (the originating instance: the operator, TG 6694/6697, during a spend-cap
incident). Also applies to: pausing one misbehaving seat, quieting the fleet during a migration, or
holding seats down while a shared-substrate change lands.

**This is a PAUSE, not a decommission.** Accounts, homes, mailboxes, tickets and git state are all
left intact. If you are actually retiring a seat, this is the wrong document — see T-2219's runner
retirement for the destructive shape, which requires deregistration before file removal.

## 2. What actually spends tokens (the target set)

Only wake paths cost money. Enumerate per seat, do not recall:

```
systemctl --user -M <account>@ list-timers  --all --no-legend
systemctl --user -M <account>@ list-units --type=service --state=running --no-legend
```

| category | examples | pause? |
|---|---|---|
| **wake timers** | `pantheon-keep-going@P`, `pantheon-inbox-consumer@P`, `pantheon-mailbox-wake@P`, `pantheon-acp-bridge-backlog@P` | **yes — these are the spend** |
| **inbound bridges** | `claude-telegram-<seat>`, `pantheon-acp-bridge@P` | **yes** — an inbound message wakes a turn |
| **dispatch loops** | `pantheon-watch`, `pantheon-knowledge-sweep`, `pantheon-watch-digest` | **yes** — these fire *subagents*, the largest single burn |
| **seat apps** | `exocortex.service` | **judgement** — an app is not a wake path and may cost nothing. Ask, or pause and flag it. |
| **OS plumbing** | `dbus`, `gpg-agent`, `launchpadlib-cache-clean` | **never** |
| **slices** | `app-pantheon-*.slice` | **never** — cgroup containers; they stay `active` holding nothing. Do not chase them. |

**Mail is not a daemon.** The hub under `/home/the-ancestor/mailbox/` is a filesystem: delivery needs
no running service. Pausing a seat's *consumer* leaves mail arriving and queueing normally — it just
stops arrival from waking anything. Never stop the-ancestor to "stop mail"; you will break delivery
for the seats you are keeping up while saving nothing.

## 3. Procedure — ORDER IS THE WHOLE POINT

**3.0 — Record the manifest BEFORE touching anything.** Restore is only as good as this list.

```
OUT=/home/claude/daemon-pause-manifest-$(date -u +%Y%m%dT%H%M%SZ).txt
# per account: append the timer + service unit names you are about to disable
```

**3.1 — Stop TIMERS first, all of them, before any service.** A timer left armed for even a few
seconds fires a oneshot into a half-stopped seat.

**3.2 — Let in-flight oneshots drain.** Check for running `pantheon-*` services once more after the
timers are down. A oneshot killed mid-run records as `failed`.

**3.3 — Then stop long-running services** (bridges, `acp-bridge@P`, `specialist-a2a`, seat apps).

**3.4 — `disable --now`, not `stop`.** `stop` alone leaves the unit enabled, so it returns at the next
boot or `daemon-reload` and the pause silently ends.

**3.5 — `reset-failed` afterwards, per seat.** Even done gracefully, expect some residue.

> **WHY 3.1–3.3 EXIST.** Doing it in one undifferentiated `disable --now` sweep — timers and services
> together — produced, measured: `pantheon-acp-bridge-backlog@archivist`, `pantheon-acp-bridge@specialist`,
> `pantheon-acp-bridge-backlog@specialist`, `pantheon-mailbox-wake@{specialist,mason,artisan}` all in `failed`,
> **plus a `pantheon-unit-failure-alert@…` instance for each** — 10 failed units and 5 spurious alerts
> across four seats. Nothing was broken, but the fleet's own health surface then reported a fault that
> the operator caused. **A bringdown that leaves the health surface lying is not a graceful bringdown.**

**3.6 — Leave live processes alone.** Seats mid-turn should finish; they go inert once nothing wakes
them. Killing them loses work and gains nothing. Surface the residue to the operator as their call —
do not decide it silently.

## 4. Verification — and the trap that will bite you

```
# WRONG — "inactive" contains "active", so this counts the units you just stopped
systemctl --user -M <acct>@ list-units --all | grep -c active

# RIGHT — let systemd filter, or read the field
systemctl --user -M <acct>@ list-units --state=active --no-legend
systemctl --user -M <acct>@ list-units --all --no-legend | awk '{print $1, $3"/"$4}'
```

This is not hypothetical: the first verification pass of the originating incident reported "4 active
wake units" on a seat that was fully stopped, purely from `grep -c active` matching `inactive`. Same
class as the rest of the 2026-07-29 cluster — see
`lessons/2026-07-29-proxy-reported-in-the-things-voice.md`.

Verify **three** things, by enumeration, never by return code:

1. paused seats: **zero** active wake units;
2. the keep-list seats: their timers still listed (`list-timers`), naming them — a bringdown that
   quietly catches a seat you meant to keep is the expensive failure;
3. failed-unit count is **zero** on every seat you touched.

## 5. Restore

```
systemctl --user -M <acct>@ enable --now <unit>     # for each line in the manifest
```

Then re-verify with §4. Restoring the *app* without the wake path (or vice versa) is a legitimate
partial — the originating incident restored `exocortex.service` alone while archivist stayed paused.

## 6. Variations

- **Keep-list bringdown** (the default shape): pause everyone except a named few. Confirm the
  keep-list explicitly in §4 step 2 — the risk is over-reach, not under-reach.
- **Single-seat pause:** same procedure, one account. Prefer this for a misbehaving seat.
- **Pause but keep mail flowing:** already the default. Pausing consumers does not stop delivery.
- **Emergency / seat is burning tokens right now:** invert §3 — kill the wake timers first and accept
  the failed units, then `reset-failed`. Speed beats tidiness when spend is actively accruing; note in
  the ledger that you took the ungraceful path deliberately.
- **Pause during an incident:** check whether a *kept* seat runs monitors pointed at *paused* seats.
  Recovery runs `pantheon-context-fill-monitor@specialist` and its `restart-drain` covers specialist, so there is a
  live resurrection path. Nothing fires while a paused seat produces no jsonl growth and no restart
  tombstone, but if a paused seat comes back to life, that is where to look.

## 7. Recording it

Ledger the pause with the manifest path, the exact unit count, what you deliberately left running,
and any judgement calls (seat apps, live processes). An operator-directed fleet change is an
audit-chain event: the instruction came from outside the repo, so the ledger entry is the only place
the *why* survives.

**Post-arc: move the manifest out of the home dir.** The home dir is only where 3.0 writes the
manifest during the pause (a fixed, obvious path while things are down). Once the fleet is restored,
copy it to `knowledge/episodic/<date>-fleet-daemon-pause/`, commit, and point the ledger entry at the
committed copy. A home-dir file is not a shared record (`sop/standard/mailbox-is-not-the-shared-record.md`),
and the next pause would strand it. Precedent: the 2026-08-01 pause (`knowledge/episodic/2026-08-01-fleet-daemon-pause/`, T-3842).
