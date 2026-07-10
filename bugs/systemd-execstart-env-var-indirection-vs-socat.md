---
symptom: "systemd ExecStart with `${VAR}` env-var indirection inside socat EXEC: arg produces malformed argv to the EXEC subprocess at runtime; works fine when invoked directly via bash"
fix: "drop the env-var indirection — write the value directly into the ExecStart line via per-instance drop-in (full ExecStart-override, not env-var substitution)"
domain: systemd, socat
---

> **Superseded as load-bearing by** [`sop/remediation/socket-activated-agent-daemon-gotchas.md`](../sop/remediation/socket-activated-agent-daemon-gotchas.md) — the live remediation guidance these symptoms taught now lives in that SOP (ORG-012 supersedure-trail, T-0832 PR-2 / T-0853). This entry remains the recorded symptom + diagnosis; act on the SOP for current guidance.

## Symptom

A systemd unit uses socat with `EXEC:` to bridge a socket to a subprocess, and uses an env-var to inject a per-instance value into the subprocess argv:

```ini
[Service]
Environment="KINO_SESSION_FLAG=--session-id 0c9c9cb7-f00d-4bbe-b995-15471f45701a"
ExecStart=/usr/bin/socat ACCEPT-FD:3 EXEC:"/path/to/binary -p ... ${KINO_SESSION_FLAG}"
```

`systemctl show <unit> -p Environment` shows the env-var set correctly. `systemctl show <unit> -p ExecStart` shows `${KINO_SESSION_FLAG}` unexpanded (expected — show reflects the unit definition; expansion happens at exec time).

Expected: at exec time, systemd expands `${KINO_SESSION_FLAG}` to `--session-id 0c9c9cb7-...`, socat receives the EXEC arg with the expanded value, splits on whitespace internally, forks the subprocess with `--session-id` and the UUID as separate argv tokens.

Actual: subprocess fails with malformed argv. In one observed case (claude -p), exits with `option '--session-id <uuid>' argument missing` despite the env-var being clearly set. Direct invocation of the same exact command line as the unix user via bash works fine.

## Cause

Not fully root-caused. Hypothesis: systemd's variable expansion within double-quoted `EXEC:"..."` produces a single argv token to socat (because the outer double quotes inhibit word-splitting per systemd's tokenization rules). socat then has to internally word-split the EXEC: argument to build the subprocess argv. Somewhere in that chain, the env-var-expanded portion's whitespace either doesn't get split correctly OR gets pre-quoted in a way that confuses the subprocess's CLI flag parser.

`man systemd.service` on variable expansion:

> If the value is enclosed in double quotes, only environment variable references are expanded; ... text within quotes is treated as one argument.

So inside the double-quoted `EXEC:"..."`, the entire contents are one argument to socat. socat's own EXEC: parser then has to re-split. The interaction of "systemd expanded one chunk" + "socat re-tokenizes the whole chunk" produces the bug.

## Fix

Drop the env-var indirection. Write the per-instance value directly into the ExecStart line via a per-instance drop-in that overrides ExecStart entirely:

```ini
# /etc/systemd/system/kino-agent@claude.service.d/session-id.conf
[Service]
ExecStart=
ExecStart=/usr/bin/socat ACCEPT-FD:3 EXEC:"/path/to/binary -p ... --session-id 0c9c9cb7-f00d-4bbe-b995-15471f45701a"
```

Empty `ExecStart=` clears the inherited base ExecStart; the second `ExecStart=` sets the override. The UUID is now a literal in the unit definition — no env-var expansion, no quoted-span tokenization gotcha.

Cost: the per-instance drop-in must be regenerated whenever the per-instance value changes. For systems where a setup script writes per-instance drop-ins anyway (e.g. via `ExecStartPre=+` running as root), this is no extra work.

## Discovered

- T-0114 (kino Phase 2.7 deploy-fix arc) — initial design used `Environment="KINO_SESSION_FLAG=..."` + `ExecStart=... ${KINO_SESSION_FLAG}` per the brief. systemd Environment quoting was correct; subprocess still got malformed argv. ~1 hour iterating different quoting/escape patterns. Pivoted to per-instance ExecStart-override; worked first-fire because the manual-override-with-inlined-UUID shape had been verified during T-0113 testing.
- pantheon-pwa commit 65f6a77 (the path-(α) refactor dropping env-var indirection), 2026-05-07.

Pairs with `systemd-environment-whitespace-split.md` — that one fixes the Environment= directive itself; this one drops the indirection that the env-var was meant to enable. After fixing both, the right pattern is per-instance ExecStart-override (no env-var), not "fix the quoting and keep the indirection."

## Keywords

systemd, ExecStart, ${VAR}, env-var expansion, socat, EXEC:, ACCEPT-FD, argv tokenization, malformed argv, double-quoted argument, drop-in override, ExecStart= clear-then-set, per-instance, variable substitution gotcha, claude -p, --session-id argument missing
