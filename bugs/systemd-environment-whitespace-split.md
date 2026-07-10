---
symptom: "systemd Environment= directive value with whitespace splits into orphan tokens; only the first word becomes the env-var value, rest is silently dropped"
fix: "wrap the whole `KEY=value` in double quotes — `Environment=\"KEY=value with spaces\"` — systemd treats quoted span as one atomic env entry"
domain: systemd
---

> **Superseded as load-bearing by** [`sop/remediation/socket-activated-agent-daemon-gotchas.md`](../sop/remediation/socket-activated-agent-daemon-gotchas.md) — the live remediation guidance these symptoms taught now lives in that SOP (ORG-012 supersedure-trail, T-0832 PR-2 / T-0853). This entry remains the recorded symptom + diagnosis; act on the SOP for current guidance.

## Symptom

A systemd unit file has an `Environment=` directive with a value containing whitespace:

```ini
[Service]
Environment=KINO_SESSION_FLAG=--session-id 0c9c9cb7-f00d-4bbe-b995-15471f45701a
```

Expected: env-var `KINO_SESSION_FLAG` set to `--session-id 0c9c9cb7-f00d-4bbe-b995-15471f45701a`.

Actual: `systemctl show <unit> -p Environment` shows:

```
Environment=HOME=/home/claude KINO_SESSION_FLAG=--session-id 0c9c9cb7-f00d-4bbe-b995-15471f45701a
```

— but at process exec time, only `KINO_SESSION_FLAG=--session-id` is set. The UUID after the space becomes an orphan token (silently dropped). The downstream process sees `--session-id` with no value.

For example, when the unit's ExecStart is `... claude -p ... ${KINO_SESSION_FLAG}`, claude exits with:

```
error: option '--session-id <uuid>' argument missing
```

## Cause

systemd's `Environment=` parser splits on whitespace at the directive level. Each space-separated token must be a complete `KEY=value` pair. A value containing whitespace is interpreted as multiple env entries — the second token after the space becomes an orphan key (with no `=`) and is silently dropped.

This is documented in `man systemd.exec`:

> Environment=
>   This option supports specifying the environment variables that should be set for executed processes. ... Variable expansion is not performed inside the strings; ... Single or double quotes may be used to embed whitespace into the value of a variable.

The "may be used" language is misleading — it's _required_ for any whitespace-containing value, not optional.

## Fix

Wrap the whole `KEY=value` span in double quotes:

```ini
[Service]
Environment="KINO_SESSION_FLAG=--session-id 0c9c9cb7-f00d-4bbe-b995-15471f45701a"
```

systemd treats the quoted span as a single env entry; whitespace inside is preserved.

`systemctl show <unit> -p Environment` after the fix:

```
Environment=HOME=/home/claude "KINO_SESSION_FLAG=--session-id 0c9c9cb7-f00d-4bbe-b995-15471f45701a"
```

— the quotes propagate through to the show output, signaling the atomic-value treatment.

## Discovered

- T-0114 (kino Phase 2.7 deploy-fix arc) — kino-drop-in-writer.sh's first version wrote the unquoted form. claude-p exited status 1 in restart loop with the "argument missing" error. ~1 hour to root-cause because journal didn't surface the actual claude-p stderr (restart-loop velocity ate the error message); had to direct-test claude-p as the unix user with the env-var manually exported to confirm the env-var-was-being-truncated hypothesis.
- pantheon-pwa commit c54b0bd (the printf-format quoted-Env= fix), 2026-05-07.

## Keywords

systemd, Environment=, whitespace split, env-var orphan token, KEY=value quoting, systemd.exec, drop-in conf, --session-id argument missing, claude -p, ExecStart variable substitution, single space silently dropped
