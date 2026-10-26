# echo-tap-recorder (T-5260)

A standing, passive, read-only recorder of the live Echo tap. Only the `claude`
account can reach the live broker, so until now every post-deploy look at tap
traffic went through orchestrator (T-3571, T-5169/T-5259). A systemd `--user` service
on the claude account runs `echo_tap_recorder.py` all the time. It writes
hourly JSONL files that `pantheon-shared` members (artisan and mason among them)
can read:

| File (in `/var/lib/pantheon/echo-evidence/tap/`) | Content |
|---|---|
| `tap-YYYYMMDDTHH.jsonl` | Every message received on `echo/+/tap/#` and `echo/stt/final` during that UTC hour, one JSON object per line. |

The recorder subscribes at QoS 0 and never publishes. The module has no code
that builds an MQTT PUBLISH packet: the only packets it sends are CONNECT,
SUBSCRIBE, PINGREQ and DISCONNECT, and a test checks every byte it writes to
the socket. It has no j17 path and no config-control path. It needs no new
credential (HO ruling on T-5260, option (a)).

The `tap/` subdir is created by the script, owned by claude, mode `2750`.
The parent `/var/lib/pantheon/echo-evidence` is group-writable, so any
`pantheon-shared` member could replace a file there; inside `tap/` members
can read but cannot replace or delete the files. The subdir inherits the
setgid `pantheon-shared` group from the parent. The script refuses to start
if `tap/` is a symlink, is not a directory, or is not owned by claude, and it
opens each file with `O_NOFOLLOW`, so a symlink planted in place of a file is
refused too.

## What is recorded and what is excluded

Recorded: every topic under `echo/<ctx>/tap/` (vad, stt_partial, stt, speaker,
auth, guest, intent, action, utterance, tts, tts_progress, tts_raw_eos,
conversation, perceived_latency, stt_label_outcome, barge_in_*, and any new
tap a later daemon adds) plus `echo/stt/final`. Payloads are written as they
arrived, with no redaction: on-host taps are internal (ORG-105 section 3,
ratified), and transcripts in a `pantheon-shared` file read by our own seats
are not a finding.

Excluded:

- Raw audio. `tts_raw` carries base64 s16le TTS PCM in `data.pcm`
  (echo-prismatic `crates/echo-adapter-speechmatics-tts/src/lib.rs`,
  `publish_tts_raw`). Its messages are counted in the stats line and never
  written. `tts_raw_eos` has no audio and is kept. Microphone PCM
  (`echo/input/raw_pcm`, `echo/input/pcm_stamped`) is not under either
  subscription, so it never reaches the recorder. `ECHO_TAP_EXCLUDE` holds the
  list (default `tts_raw`); it replaces the default, so keep `tts_raw` in it.
- Oversize payloads. A payload over `ECHO_TAP_MAX_PAYLOAD_BYTES` (default
  64 KiB) is not written; a stub takes its place:
  `{"ts": ..., "topic": ..., "dropped": "oversize", "bytes": N}`. The largest
  real tap message seen so far is about 11 KB (a terminal `tap/utterance`).

## Record format

One JSON object per line, keys in this order:

```json
{"ts":"2026-10-17T00:32:18.060417Z","topic":"echo/stt/final","payload":{"transcript":"That's fine.", "...": "..."}}
```

- `ts`: the recorder's receive time, UTC, with microseconds. It is not the
  producer's timestamp; that is inside `payload` (`envelope.producer_timestamp`
  or `producer_timestamp`).
- `topic`: the MQTT topic.
- `payload`: the parsed JSON payload. A payload that is not JSON becomes
  `{"_non_json": "<the bytes as UTF-8, invalid bytes replaced>"}`.
- `retained`: present (`true`) only when the broker delivered a retained
  message, i.e. a stored value replayed on subscribe. Its `ts` is then the
  time of the (re)subscribe, not of the original publish. The key is absent
  for live messages.

Each line goes to the file in a single `O_APPEND` write, so a reader tailing
the file sees it at once. The file is fsynced every `ECHO_TAP_FSYNC_S`
seconds (default 60), on rotation and on shutdown, so a host crash can lose
up to about a minute of lines; a recorder crash loses nothing already written.

A local write error (disk full, I/O error, permission) drops that one message
and counts it in `write_errors`; it never ends the broker session. The file is
closed and reopened for the next message. If the error left a partial line,
it is truncated away (or, if truncating fails, ended with a newline), so the
next record starts on a clean line. The error is logged at most once per
stats interval.

## Rotation and retention

Files rotate on the UTC hour: `tap-20261021T04.jsonl` holds 04:00:00 to
04:59:59 UTC. A restart within the hour appends to the same file.

Retention runs at startup and on every rotation:

1. Delete files whose hour ended more than `ECHO_TAP_RETENTION_DAYS` ago
   (default 7).
2. Then, oldest first, delete files until the total is at most
   `ECHO_TAP_MAX_BYTES` (default 2 GiB).

The current hour's file is never deleted. Only regular files in `tap/` whose
name matches `tap-YYYYMMDDTHH.jsonl` exactly are ever deleted; symlinks,
directories and any other name are left alone.

Sizing: the 2026-10-17 wet-run capture (orchestrator's passive recorder, 838
messages, 45 utterances, no answered turns) was 1.12 MB over 3.1 hours, about
0.36 MB per hour of active use. An always-on ambient day at that rate is about
9 MB; doubling it for answered turns (tts, tts_progress, action) gives about
18 MB per day, or about 125 MB for 7 days. The 2 GiB cap is a backstop at
about 16 times that, against 44 GB free on `/` (2026-10-21).

## Reading it (artisan or any pantheon-shared reader)

```sh
ls -l /var/lib/pantheon/echo-evidence/tap/           # expect -rw-r----- claude pantheon-shared
F=/var/lib/pantheon/echo-evidence/tap/tap-$(date -u +%Y%m%dT%H).jsonl
tail -F "$F"                                          # live; follows the file within the hour
# Post-deploy check, e.g. the error_kind shape on tap/action since a deploy:
jq -c 'select(.topic|endswith("/tap/action")) | {ts, results: .payload.data.action_results}' "$F"
# One utterance end to end:
cat /var/lib/pantheon/echo-evidence/tap/tap-*.jsonl | jq -c 'select(.payload.envelope.correlation_id=="<id>" or .payload.correlation_id=="<id>") | [.ts, .topic]'
# Oversize stubs:
jq -c 'select(.dropped)' "$F"
```

`tail -F` does not move to the next hour's file by itself; re-run it after the
hour turns, or tail a glob in a loop.

Recorder health is in the claude account's journal: a stats line every
`ECHO_TAP_STATS_S` seconds (default 600):
`echo-tap-recorder: stats written=N excluded=N oversize=N write_errors=N reconnects=N file=tap-...jsonl`,
plus one line for every lost connection and reconnect. A rising
`write_errors` means messages are being dropped on the claude side (disk full
or the like), not that the broker is down. If the current hour's
file is missing during active use, ask orchestrator to check
`systemctl --user status echo-tap-recorder.service` on the claude account. A
missing file during an idle hour is normal: a file is created only when the
first recorded message of that hour arrives.

## Connection behaviour

The script connects with a clean session and a unique client id
(`echo-tap-recorder-<host>-<pid>`), sends PINGREQ every 30 seconds, and treats
a missing PINGRESP within 30 seconds as a dead connection. On any loss it
reconnects with exponential backoff from 1 s, capped at 60 s; the backoff
resets after a session that subscribed. Messages published while it is
disconnected are not recorded (QoS 0, clean session): the stats line and the
reconnect log lines show such gaps. systemd restarts the process after a
crash (`Restart=always`, `RestartSec=10`). A bad config (exit 2) or an unsafe
`tap/` dir (exit 1) is not restarted (`RestartPreventExitStatus=1 2`): the
unit stays failed with the reason in the journal until the cause is fixed and
the unit is restarted. On stop the script notices within a quarter second,
sends DISCONNECT and fsyncs the file (`TimeoutStopSec=35` covers a 5-second
connect to an unreachable broker).

## Lifecycle (ORG-068)

### Install (orchestrator, on the claude account)

This assumes the claude account's `~/pantheon-config` clone is at a commit that
contains this directory.

The script is installed as a copy, so a `git pull` of the clone never changes
what the service runs.

```sh
install -D -m 755 ~/pantheon-config/tools/echo-tap-recorder/echo_tap_recorder.py ~/.local/lib/echo-tap-recorder/echo_tap_recorder.py
mkdir -p ~/.config/systemd/user
cp ~/pantheon-config/tools/echo-tap-recorder/echo-tap-recorder.service ~/.config/systemd/user/
# Copy the example only if there is no env file yet, so a re-run never overwrites the real one.
[ -e ~/.config/echo-tap-recorder.env ] || install -m 600 ~/pantheon-config/tools/echo-tap-recorder/echo-tap-recorder.env.example ~/.config/echo-tap-recorder.env
$EDITOR ~/.config/echo-tap-recorder.env   # broker host/port/creds from echo.toml [mqtt]; leave the creds empty while the broker is anonymous
systemctl --user daemon-reload && systemctl --user enable --now echo-tap-recorder.service
journalctl --user -u echo-tap-recorder.service -n 5 --no-pager   # expect "recording ..." then "connected to ..., subscribed ..."
ls -ld /var/lib/pantheon/echo-evidence/tap            # expect drwxr-s--- claude pantheon-shared
# After the next tap message (speak to Echo, or wait for VAD):
ls -l /var/lib/pantheon/echo-evidence/tap/            # expect -rw-r----- claude pantheon-shared tap-YYYYMMDDTHH.jsonl
```

From another seat, confirm read access:
`tail -n 1 /var/lib/pantheon/echo-evidence/tap/tap-*.jsonl`.

### Configure

Set everything in `~/.config/echo-tap-recorder.env`. The keys are documented
in `echo-tap-recorder.env.example`. The broker keys use the same names as
`echo-status-export.env`. The password is read only from the env file; it is
never on a command line and never logged.

### Update

`ExecStart` runs the installed copy in `~/.local/lib/echo-tap-recorder/`, so a
change reaches the service only by an explicit re-install. After the claude
account's clone is at the new commit:

```sh
install -D -m 755 ~/pantheon-config/tools/echo-tap-recorder/echo_tap_recorder.py ~/.local/lib/echo-tap-recorder/echo_tap_recorder.py
cp ~/pantheon-config/tools/echo-tap-recorder/echo-tap-recorder.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user restart echo-tap-recorder.service
journalctl --user -u echo-tap-recorder.service -n 5 --no-pager
```

The env file is per-instance config and is never overwritten.

### Remove

```sh
systemctl --user disable --now echo-tap-recorder.service
rm -f ~/.config/systemd/user/echo-tap-recorder.service
systemctl --user daemon-reload && systemctl --user reset-failed
rm -f ~/.config/echo-tap-recorder.env
rm -rf ~/.local/lib/echo-tap-recorder
rm -rf /var/lib/pantheon/echo-evidence/tap
```

To roll back to an earlier version, check out that commit in the claude
account's clone and run the Update steps; the env file is kept. After a full
Remove, run Install again.

## Tests

```sh
cd tools/echo-tap-recorder && python3 -m unittest test_echo_tap_recorder
# or, from the repo root, as CI does:
python3 -m pytest tools/echo-tap-recorder -q
```

The fixture `fixtures/t3571-tap-sample.log` is 14 real tap lines (one
utterance end to end plus a conversation state change) from the 2026-10-17
wet-run capture. The tests cover the config (defaults, empty values, bad
numbers, the password kept out of `repr`), classification (`tts_raw` dropped
and counted, `tts_raw_eos` kept, oversize stub, excluded wins over oversize),
the record format (JSON, non-JSON, empty payload, microsecond UTC `ts`, the
`retained` flag), the tap subdir (created, repeated start, symlink, regular
file and foreign owner refused), the writer (0640 files, per-line visibility
to a reader, hourly rotation, append on restart, a symlinked current file
refused, fsync cadence), write errors (ENOSPC on write and EACCES on open
dropped and counted with recovery on the next message, a partial write
truncated away or ended with a newline, the error log rate-limited, no
reconnect in a live session), the packet parser (fed one byte at a time with
a QoS 1 PUBLISH and a 3-byte length, a malformed 5-byte length, a topic
length past the body), stop during the handshake,
retention (age, total bytes oldest first, the current file never deleted,
non-matching names, directories and symlinks never touched, runs at startup
and on rotation), and live sessions against an in-process fake broker:
records written, a clean DISCONNECT on stop, reconnect after a broker drop and
after a missed PINGRESP, answered pings keeping the session, CONNACK refusal
and SUBACK failure retried with backoff, an unreachable broker, the password
absent from stderr, the stats line, and a scan of every byte the client sent
for packet types other than CONNECT, SUBSCRIBE, PINGREQ and DISCONNECT. They
need no network, broker or journal.
