# echo-status-export (T-4420)

A read-only export of the live Echo daemon's status and journal. The daemon runs
under the `claude` account, and `/home/claude/echo-data` is not readable by other
seats. A systemd `--user` timer on the claude account runs
`echo_status_export.py` every 2 minutes. The script writes three files that
`pantheon-shared` members (artisan among them) can read:

| File (in `/var/lib/pantheon/echo-evidence/status/`) | Content |
|---|---|
| `echo-status.json` | The sanitized daemon status snapshot, or `{"error": ...}` when anything failed. Always rewritten, so it never goes stale silently. |
| `echo-status.last-good.json` | The most recent successful snapshot. It is rewritten only on success. A failed rewrite of it is logged as a warning and does not fail the run, because `echo-status.json` is already current. |
| `boots/echo-status.boot-<started_at>.json` | The final good snapshot of each earlier daemon boot (T-4442; see [Per-boot archive](#per-boot-archive)). The newest 30 are kept. |
| `echo-journal-summary.json` | The unique WARN/ERROR/PANIC signatures from the daemon's journal for its current or most recent run (the unit's current or last invocation), with counts and first/last seen. It holds no payload text. |

All three are written atomically: a tmp file in the same dir, then chmod
`0640`, fsync, rename, and a dir fsync. Leftover tmp files from a killed run
are removed on the next run once they are 10 minutes old.

The `status/` subdir is created by the script, owned by claude, mode `2750`.
The parent `/var/lib/pantheon/echo-evidence` is group-writable, so any
`pantheon-shared` member could replace a file there; inside `status/` members
can read but cannot replace or delete the exports. The subdir inherits the
setgid `pantheon-shared` group from the parent. The script refuses to write
into a `status/` that is a symlink or is not owned by claude.

A fourth file, `.echo-bin-hash-cache.json`, caches the binary's sha256 keyed on
(path, inode, size, mtime_ns), so the binary is re-hashed only after a deploy.

## How the status is fetched

`echo status` has no CLI subcommand. The `echo` binary accepts only `--config` and
`--log-format` (echo-prismatic `crates/echo/src/main.rs`). The status is a
config-control request (ADR-216/ADR-217):

- Request: `{"request_id": "echo-status-export-<uuid>", "op": "get", "resource": "status"}`
  goes out on `echo/config/request`.
- Reply: `{"request_id": <same>, "ok": true, "data": <StatusSnapshot>}` comes back on
  `echo/config/reply`. The reply topic is shared by every config client, so the
  script matches on `request_id`.
- Source: `crates/echo/src/config_control.rs` (`REQUEST_TOPIC`, `REPLY_TOPIC`,
  `ConfigRequest`, `ConfigReply`, `handle_status_get`). The same call is made by
  `crates/echo-demo-app/src/lib.rs` `config_status_get`.
- Topics (T-4431). `ECHO_STATUS_REQUEST_TOPIC` and `ECHO_STATUS_REPLY_TOPIC`
  select one of exactly two allowed pairs: `echo/config/request` /
  `echo/config/reply` above (the default), or the daemon's read-only status
  pair `echo/config/status/request` / `echo/config/status/reply` (T-4431
  Layer 2: accepts only `op=get resource=status`, same wire format and
  `request_id` correlation, QoS 1). See Configure. The export's
  `source.transport` names the request and reply topics that were used.

The script has its own minimal MQTT 3.1.1 client and needs only the stdlib.
`paho` is not installed on the host. The `mosquitto_*` clients would put the
broker password in argv, where other accounts can read it in `/proc`. The
client waits for SUBACK before it publishes, so it cannot miss the reply. If
no reply arrives within `ECHO_STATUS_TIMEOUT_S` (10 s by default, capped at
20 s so the whole run fits the unit's `TimeoutStartSec=120`: one `systemctl
show` call up to 10 s, journal read up to 75 s, status up to 20 s, plus
margin), the script
writes `{"error": "no reply"}`. Any other failure also becomes an error record:
a broker error or a connection reset mid-exchange (`broker protocol/io
error (...)`), a malformed packet, a bad `ECHO_MQTT_PORT` /
`ECHO_STATUS_TIMEOUT_S` or a status topic pair that is not one of the two
allowed pairs (`bad config: ...`), and, as a last resort, any
unexpected exception (`exporter error (<type>)`). The status export and the
journal export run independently, so a failure in one never skips the other.
The unit exits non-zero only when a file could not be written at all.

The request is hard-coded to `op=get resource=status`. The exporter is
read-only toward Echo.

The status snapshot has no version or build field (`crates/echo/src/status.rs`).
Set `ECHO_BIN` to the deployed binary and the export adds
`daemon_binary: {path, sha256, mtime}` as the build identity.

## What is exported and what is excluded

The snapshot is `StatusSnapshot` (`crates/echo/src/status.rs`). Its fields are
`started_at`, `subsystems[]` (name, policy, restart_attempts, last_restart_at,
healthy), `broker` (connected, host, port, last_reconnect_at), `active_speaker`,
`recent_activity` (utterances_last_5m, last_utterance_at), `pipeline[]` (stage,
provider, state, operating_point), `metrics` (stage_latency,
classify_latency_by_model keyed by model id, perceived_latency,
resolution_counts, identity_warmup keyed by capture-context id, the
`*_count` / `stt_label_*` counters, and `stt_last_reconnect_at`), and
`tool_policy` (drift_held tool/rule/pointer, never_assessed_count, updated_at).
`pointer` is an RFC 6901 pointer such as `/properties/brightness_pct/anyOf/1/maximum`.

Reconnect stamps: `metrics.stt_reconnect_count` and `metrics.stt_last_reconnect_at`
record Speechmatics STT session reconnects (e.g. the 1h no-audio `idle_timeout`).
`broker.last_reconnect_at` is reserved for a real MQTT reconnect signal and is
currently always null. Daemons before echo T-4495 stamped STT reconnects into
`broker.last_reconnect_at`, so in older boot archives that field means an STT
reconnect, not an MQTT bounce.

Utterance text, transcripts and credentials are not in the snapshot:

- The internal sliding windows are `#[serde(skip)]`: `RecentActivity::window`,
  `LatencyStats::window`, and the flip-cluster map.
- Broker credentials live in `MqttCredentials` (`echo-adapter-mqtt`), which is
  not part of the snapshot. `BrokerStatus` carries only host and port.

The script still sanitizes every reply, because ADR-217's extension pattern
serializes any newly added field automatically:

1. **Dropped path:** `active_speaker.speaker_id`. This is the enrolled identity
   of whoever spoke last. Deploy verification does not need it, and a
   group-readable file should not record who is speaking. `tier` and `since`
   are kept. Dropped paths are listed in `sanitizer.dropped_paths`.
2. **Dropped keys, at any depth and case-insensitive (`DENY_KEYS`):**
   - text keys: `text`, `transcript`, `utterance`, `raw_text`, `content`,
     `prompt`, `response`, `words`, `audio`, and similar
   - credential keys: `password`, `secret`, `token`, `api_key`,
     `access_token`, `authorization`, `credentials`, `private_key`, `cookie`,
     and similar

   None of these keys exist in the snapshot today.
3. **Secret scan, per value:** after the drops, every key and string value is
   checked.
   - **Refuse the whole snapshot** on a high-confidence credential pattern:
     PEM private key, `sk-…`, JWT, URL userinfo `user:pass@`, `Bearer …`.
     The script writes `{"error": "sanitizer refused: credential-like value at <path>"}`,
     naming the path, never the value, and leaves `last-good` alone.
   - **Redact that value only** on a lower-confidence match: GitHub/GitLab/Slack
     token prefixes, URLs with `?token=` / `key=` / `sig=` parameters, or a run
     of 32 or more token characters (`A-Za-z0-9+_=-`; `/` is not one, so
     RFC 6901 pointers split into short runs) that mixes upper case, lower case
     and digits with Shannon entropy of at least 4.0 bits per character. The
     value becomes `"<redacted>"` and its dotted path is listed in
     `sanitizer.redacted_paths`; a secret-like key drops its entry and is
     listed as `<parent>.<key>`. The export still succeeds.

   Lowercase-hex uuids, hashes, timestamps, model ids and schema pointers do
   not trip either check. Because `/` is not a token character, the entropy
   check can miss a standard-alphabet base64 secret that `/` splits into runs
   shorter than 32 characters. That is accepted: the entropy check is the
   low-confidence tier, and the known credential formats are matched by the
   explicit patterns above. A realistic snapshot covering every field group
   exports with zero redactions (tested).

The broker `host:port`, the status request/reply topics and the unit name are
the only config values written to the export. A rejected topic value is not
written. The username and password are never written.

### Per-boot archive

The status counters (`metrics.*_count` and so on) are per daemon boot, so
every restart or deploy resets them to zero. To let a counter be summed
across deploys (for example the T-4337 held/final label ratio), each good
export compares the new snapshot's `status.started_at` with the one in
`echo-status.last-good.json`. If they differ, the daemon restarted, and the
old last-good (the previous boot's final good reading) is first written to
`boots/echo-status.boot-<started_at>.json`. Only then is last-good replaced.

- The name is the old `started_at` in UTC, with the fraction dropped and `:`
  replaced by `-`, e.g. `echo-status.boot-2026-09-30T13-31-03Z.json`. A
  `started_at` that is not a UTC RFC 3339 timestamp is not archived.
- The write is atomic and uses the same helper as the other files (mode
  `0640`). `boots/` is created on the first archive with the same owner and
  mode (`2750`) as `status/`.
- If `started_at` is missing on either side, nothing is archived and a
  warning is logged. A failed archive is also a warning: last-good still
  refreshes.
- If the boot's file already exists, it is replaced only when the old
  last-good has a later `exported_at`.
- Retention: after each archive, only the 30 newest boot files are kept,
  ordered by the `started_at` in the name. Older ones are deleted.

**Caveat:** the archived snapshot is the last good export before the restart,
so it can be up to one timer interval (2 minutes) old. Counts from the final
2 minutes or less of a boot can be missed. Ratios across boots are
still sound; absolute totals are a slight undercount.

Read the archive with `--boots`. It is read-only and meant for any
`pantheon-shared` reader:

```sh
python3 ~/pantheon-config/tools/echo-status-export/echo_status_export.py --boots
python3 ~/pantheon-config/tools/echo-status-export/echo_status_export.py --boots --key metrics.guest_count --key metrics.stt_reconnect_count
```

It prints one row per archived boot and a row for the current boot, oldest
first. Each row shows `started_at`, the `exported_at` of that snapshot, and
each `--key` value. A key is a dotted path under `status`. The current boot
comes from `echo-status.json`, or from `echo-status.last-good.json` when the
current record is an error. The `TOTAL` row aggregates a key only where that
means something, judged by the key's last segment: one with a `max` word,
such as `stt_label_hold_max_ms`, takes the maximum; otherwise one ending in
`_count` is summed across boots. Any other key (`p50_ms`, `p95_ms`, a mean,
a gauge) shows `-` in `TOTAL`, since a sum of those is meaningless. A missing
or non-numeric value also shows as `-`.

If the current record is an error and `echo-status.last-good.json` is missing
or fails a trust check, the current boot cannot be read. `--boots` then
prints a warning to stderr, shows a last row
`current: unavailable (<reason>)` with every value `-`, and labels the total
`TOTAL (N boots; current unavailable)`, where N counts only the archived
boots. It still exits 0 because the archived boots were read and trusted.

The default keys are `metrics.stt_label_final_count`,
`metrics.stt_label_held_count` and `metrics.stt_label_hold_max_ms`.
`--dir` and `--owner` work as they do for `--verify`. Every dir and file
read goes through the same trust checks as `--verify` (not a symlink, owned
by `claude`, not group- or world-writable, opened with `O_NOFOLLOW`). Any
other failure (the export dir, `boots/`, an archived boot file, or
`echo-status.json` itself) prints the reason and exits 2. A missing `boots/` just means no boot
has been archived yet.

### Journal signatures

The summary covers the daemon's current or most recent run: the unit's
current or last invocation, not the system boot. `-b` is never used. The host
stays up for months and the daemon logs at DEBUG, so a whole-boot `--grep`
scan ran past the 60 s kill on every run (measured on the install) and cost
about 60 s of journal I/O every 2 minutes.

The script first asks systemd which run to scan with one call, using the same
`--user` flag as `ECHO_JOURNAL_SCOPE` and a 5 s timeout:
`systemctl [--user] show --timestamp=unix -p InvocationID -p ActiveState -p
ActiveEnterTimestamp $ECHO_JOURNAL_UNIT`, read as `key=value` lines.

1. If `InvocationID` is a 32-hex id, the scan matches
   `_SYSTEMD_INVOCATION_ID=<id>`. That field tags the unit's own output for
   both system and `--user` units (verified on a `--user` probe unit;
   `USER_INVOCATION_ID` tags only the user manager's own lines about the unit,
   such as `Started ...`). systemd keeps the id after the unit stops or fails,
   so a stopped daemon's last run is still scanned.
2. If the id is empty: `ActiveEnterTimestamp`, which `--timestamp=unix` prints
   as `@<epoch>`, and the scan uses `--since=@<epoch>`. Any value that is not
   `@` followed by digits counts as unavailable.
3. If both are unavailable, the unit never started under this manager, or
   `ECHO_JOURNAL_UNIT` or `ECHO_JOURNAL_SCOPE` is wrong, and nothing is
   scanned. The summary is written as `{"exported_at": ..., "error": "unit
   never started (or wrong ECHO_JOURNAL_UNIT/SCOPE)"}`. A failed or timed-out
   `systemctl` call writes `"error": "systemctl: ..."`.

The window used is recorded in the summary as `window`:
`{"invocation_id": "<id>", "active_state": "<state>"}` or
`{"since": "@<epoch>", "active_state": "<state>"}`. `active_state` is the
unit's `ActiveState` when the summary was taken (`active`, `inactive`,
`failed`, ...), so a summary of a run that has already ended says so. The scan
is then `journalctl [--user] -u $ECHO_JOURNAL_UNIT <_SYSTEMD_INVOCATION_ID=<id>
| --since=@<epoch>> --grep='WARN|ERROR|panicked' -o json
--output-fields=MESSAGE,PRIORITY,__REALTIME_TIMESTAMP`, so journald filters
candidate lines server-side, and the script reads the output one line at a
time. It is killed after 60 s. A `--grep` with no matches exits 1 with no
output; that counts as zero entries.
The level must be the tracing header's level token (the first token, or the
second after an RFC 3339 timestamp), so an INFO line whose message contains
"ERROR" is not counted, and neither is a Pretty-format continuation line such
as `Status: ERROR ...`. For tracing's JSON format the `level` field decides. Rust panics
become `PANIC` entries that carry the file location only. At most 200
signatures are written, most frequent first; `truncated` counts the rest, and
`totals` covers all of them. `entries_matched` is the number of entries
journald returned for the `--grep`, before level parsing. Each line is reduced
to `level + target + template`. Most echo WARN/ERROR sites read
`<static text>: {e}` or `<subsystem>: <static text>: {e}`, and the error value
`{e}` can carry user-derived text, so the template keeps only the static part:

- The message is cut at the first em-dash clause (` — `), field separator
  (`, key: `, or a trailing tracing field ` key=value`), quote, `=`, `{`, `[`,
  `<` or `(`. The `(` drops a parenthesised error: `enrollment sweep: CSM query
  failed ({e}) — kept` becomes `enrollment sweep: CSM query failed`.
- At most the text before the second `": "` is kept.
- If the first kept segment ends in a failure word (`failed`, `failure`,
  `failures`, `error`, `errors`, `errored`, `err`), the text after its `": "`
  is the error value and is dropped: `profile persist failed: {e}` becomes
  `profile persist failed`. A later failure word is kept:
  `mqtt audio source: resubscribe after broker reconnect failed: {e}` keeps
  everything up to `failed`, and `j17-bridge: {n} cumulative append failures —
  …` becomes `j17-bridge: <n> cumulative append failures`.
- Uuids, timestamps, hex ids and numbers are replaced with `<uuid>`, `<ts>`,
  `<hex>` and `<n>`. Dotted or underscored identifiers (entity ids such as
  `light.bedroom_lamp`, which can carry room or person names) become `<id>`.
  The exception is a one-token leading label before the first `": "`, such as
  `claude.classify` or `reference_wrapper`, which stays readable unless it is
  shaped like a Home Assistant entity id (`light.…`, `person.…`, and so on).
- A signature that still looks secret-like is replaced with
  `<redacted: secret-like>`.

The tests run the normalizer over 16 real echo call sites (echo-prismatic
`origin/main`) with several error values each. Each site must reduce to the
same signature whatever the error value, with no payload words left.

For the JSON log format, only `fields.message` is used. Other fields are never
read.

## Lifecycle (ORG-068)

### Install (orchestrator, on the claude account)

This assumes the claude account's `~/pantheon-config` clone is at a commit that
contains this directory.

The script is installed as a copy, so a `git pull` of the clone never changes
what the timer runs.

```sh
install -D -m 755 ~/pantheon-config/tools/echo-status-export/echo_status_export.py ~/.local/lib/echo-status-export/echo_status_export.py
mkdir -p ~/.config/systemd/user
cp ~/pantheon-config/tools/echo-status-export/echo-status-export.{service,timer} ~/.config/systemd/user/
install -m 600 ~/pantheon-config/tools/echo-status-export/echo-status-export.env.example ~/.config/echo-status-export.env
$EDITOR ~/.config/echo-status-export.env   # broker host/port/creds from echo.toml [mqtt]; ECHO_BIN; ECHO_JOURNAL_UNIT/SCOPE
systemctl --user daemon-reload && systemctl --user enable --now echo-status-export.timer
systemctl --user start echo-status-export.service && journalctl --user -u echo-status-export.service -n 5 --no-pager
ls -ld /var/lib/pantheon/echo-evidence/status        # expect drwxr-s--- claude pantheon-shared
ls -l /var/lib/pantheon/echo-evidence/status/echo-*.json   # expect -rw-r----- claude pantheon-shared
# Journal access: this must print recent echo daemon lines from the current
# or most recent run. Use the same ECHO_JOURNAL_UNIT and scope as the env
# file: for a system unit, drop --user on BOTH the systemctl and journalctl
# lines.
UNIT=$(grep '^ECHO_JOURNAL_UNIT=' ~/.config/echo-status-export.env | cut -d= -f2-)
INV=$(systemctl --user show -p InvocationID --value "$UNIT")
test -n "$INV" || echo "unit has no invocation — not started / wrong unit or scope"
journalctl --user -u "$UNIT" _SYSTEMD_INVOCATION_ID="$INV" -n 5 --no-pager
```

If the last command prints nothing (or `-- No entries --`), the unit name, the
scope or the claude account's journal access is wrong, and the journal summary
will be empty without reporting an error.

### Configure

Set everything in `~/.config/echo-status-export.env`. The keys are documented
in `echo-status-export.env.example`. Two values must match how the daemon
actually runs:

- `ECHO_JOURNAL_UNIT` / `ECHO_JOURNAL_SCOPE`: the daemon's unit, and whether it
  is a `--user` unit (the default, `echo.service`) or a system unit. A system
  unit needs the claude account to be able to read the system journal (the
  `systemd-journal` or `adm` group).
- `ECHO_BIN`: the binary that `run-echo.sh` execs.

Status topics (T-4431). Leave `ECHO_STATUS_REQUEST_TOPIC` /
`ECHO_STATUS_REPLY_TOPIC` unset or empty to use the shared config-control pair
`echo/config/request` / `echo/config/reply` (the default). When the deployed
daemon serves the read-only status pair and the broker ACL limits the
exporter's MQTT user to it, set both:

```sh
ECHO_STATUS_REQUEST_TOPIC=echo/config/status/request
ECHO_STATUS_REPLY_TOPIC=echo/config/status/reply
```

Set them only after the daemon has the new topics; against an older daemon the
request goes unanswered and every run records `{"error": "no reply"}`. Only
these two pairs are accepted. Any other value, or a mixed pair (one variable
set, or one status topic with one shared topic), is rejected before connecting
as `bad config: ...`, so the request can never go to another topic, such as a
command topic. The rejected value is not written to the export. Check the
switch with `source.transport` in `echo-status.json`.

### Monitor (artisan or any pantheon-shared reader)

Read `/var/lib/pantheon/echo-evidence/status/echo-status.json`.

- Trust and freshness: run the script's reader-side check. It writes
  nothing and needs only read access, so any `pantheon-shared` member can
  run it from their own clone:

  ```sh
  python3 ~/pantheon-config/tools/echo-status-export/echo_status_export.py --verify --max-age 300
  ```

  It `lstat`s `status/` and `status/echo-status.json` and checks that neither
  is a symlink, that they are a directory and a regular file owned by the
  `claude` uid (`pwd.getpwnam("claude").pw_uid`), and that neither is group-
  or world-writable. It then opens the file with `O_NOFOLLOW` and checks that
  `exported_at` is at most `--max-age` seconds old (the timer runs every 2
  minutes; `--max-age` must be a finite number greater than 0, and an
  `exported_at` more than 60 s in the future is also an alert). Exit 0 means trusted and fresh, 1 means trusted and fresh but the
  record carries an `error`, and 2 is an alert: missing, not a regular
  file or directory, symlinked, wrong owner, writable, malformed or stale. It
  prints a one-line reason. Treat exit 2 as an alert and do not use the file.
  `--dir` and `--owner` override the defaults.
- Parent sticky bit (orchestrator decision, not done here): the parent
  `/var/lib/pantheon/echo-evidence` is group-writable, so a member could
  rename `status/` away and put their own dir in its place. `--verify` catches
  that through the owner check. `chmod +t` on the parent would also stop
  members from renaming or deleting entries they do not own. That is a host
  change for orchestrator to decide.
- Daemon health: an `error` key in `echo-status.json` means the daemon did not
  answer (`no reply`), the broker was unreachable or misbehaved, the config is
  bad, the sanitizer refused, or the exporter hit an unexpected error.
  `echo-status.last-good.json` holds the last good reading. A non-empty
  `sanitizer.redacted_paths` means a value was redacted; the rest of the
  snapshot is still valid.
- Journal summary: `entries_matched: 0` with no signatures usually means a
  clean run since the daemon last started (`window` says which run, and its
  `active_state` whether that run is still going). It can also mean no journal
  access for the claude account. A stopped or failed daemon is not an error:
  its last run is still scanned. An `error` of `unit never started (or wrong
  ECHO_JOURNAL_UNIT/SCOPE)` means the unit has never run under this manager,
  or `ECHO_JOURNAL_UNIT` or `ECHO_JOURNAL_SCOPE` is wrong. The Install step's
  `journalctl` check tells these apart.
- Exporter health on the claude account:
  `systemctl --user status echo-status-export.timer echo-status-export.service`.

### Update

`ExecStart` runs the installed copy in `~/.local/lib/echo-status-export/`, so a
change reaches the timer only by an explicit re-install. After the claude
account's clone is at the new commit:

```sh
install -D -m 755 ~/pantheon-config/tools/echo-status-export/echo_status_export.py ~/.local/lib/echo-status-export/echo_status_export.py
cp ~/pantheon-config/tools/echo-status-export/echo-status-export.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start echo-status-export.service && journalctl --user -u echo-status-export.service -n 5 --no-pager
```

The env file is per-instance config and is never overwritten. When upgrading
from the first version, which wrote to the parent dir, set
`ECHO_EXPORT_DIR=/var/lib/pantheon/echo-evidence/status` in the env file (or
delete the line to use that default) and remove the three old files from
`/var/lib/pantheon/echo-evidence/`.

### Remove

```sh
systemctl --user disable --now echo-status-export.timer
systemctl --user stop echo-status-export.service
rm -f ~/.config/systemd/user/echo-status-export.{service,timer}
systemctl --user daemon-reload && systemctl --user reset-failed
rm -f ~/.config/echo-status-export.env
rm -rf ~/.local/lib/echo-status-export
rm -rf /var/lib/pantheon/echo-evidence/status
```

To roll back, re-run Install.

## Tests

```sh
cd tools/echo-status-export && python3 -m unittest test_echo_status_export
# or, from the repo root, as CI does:
python3 -m pytest tools/echo-status-export -q
```

The tests cover the sanitizer (including a realistic snapshot of every field
group that must export with zero redactions), the journal normalizer (against
real echo call sites) and reader, the reader-side `--verify` (fresh, stale,
symlinked, wrong owner, writable, daemon error), the per-boot archive (a
boot change archives, the same boot does not, a missing `started_at` does not,
overwrite only if newer, 30-file retention) and the `--boots` reader (totals,
max, current-error fallback, untrusted dir or file refused), the timeout cap, a failed
last-good write, the atomic write, the export subdir, the hash cache, every error path
(no reply, connection reset, short packet, bad config, unexpected exception),
the status topic allowlist (default pair, empty means default, the T-4431 status
pair, mixed pairs and any other topic rejected before connecting,
`source.transport` naming the topics used), and MQTT round trips against an
in-process fake broker, one with a reply over 16 KB. They need no network, broker or journal.
