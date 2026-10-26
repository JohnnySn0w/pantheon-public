# SOP: Deploying the live echo daemon

**Status:** Adopted 2026-09-30 by Orchestrator (HO) under msg-3733 principles-as-ratification (below-goal HO-practice methodology; ORG-012: HO authors SOPs). Filed under T-4465 so it can render as an auto-triggering skill. It consolidates the HO memory pins for the echo live deploy and the shared cargo target, corrected by a 2026-09-30 accuracy review. The procedure has run at least ten times on 2026-09-29/30 (`/home/claude/echo-data/echo.bin-*`). **HO seat only:** other seats request a deploy from the HO. The operator override-available.

## When to use this

An echo-prismatic change has merged (or, exceptionally, is reviewed and gate-verified on an identical tree) and has to reach the live daemon on this VPS. Echo is a proof of concept: its data carries no real-world consequence, so a deploy is a routine, reversible restart. It still interrupts a live conversation, so check idleness.

## Procedure

1. **Source tree.** `cd /home/claude/echo-prismatic && git pull --ff-only origin main`. This checkout is the build source; the daemon runs a COPIED binary, so pulling while it runs is safe.
   - Shipping before the merge is allowed only when the tree is byte-identical to what `main` will be after the squash (the branch already has `main` merged in and `main` hasn't moved), the change is reviewed, and a full workspace smoke passed on that tree. Name the binary after the tree's sha (not `main-…`), merge promptly after, then confirm `git rev-parse HEAD^{tree}` on main equals the deployed tree.
2. **Build (background, raw log to a file):**
   ```
   env -u CARGO_TARGET_DIR ~/pantheon-config/tools/echo-cargo.sh build --release -p echo --bin echo
   ```
   The wrapper finds cargo in `~/.cargo/bin`, sets the shared target and `umask 002`, and first re-owns the other seats' build-script OUT files (`echo_target_adopt.py`). Bare `cargo` against this target fails with EPERM in `aws-lc-sys` or `libsqlite3-sys` once another seat built last (T-4874). `env -u` drops any inherited private target so the build lands in the shared one, where step 5 copies from. Budget 15-40 min on the loaded 4-core host. Cargo serializes concurrent builders on its own lock. The shared target's build policy is in `/var/lib/pantheon/README-echo-target.md`: release builds and dev loops only, while APPROVE-gating smokes run isolated (echo `CLAUDE.md`, T-4122).
3. **Pre-state capture:** the status snapshot (below) and `journalctl --user -u echo.service --since "-2h" -o cat` to a file, as the WARN baseline.
4. **Idle check:** the last ~10 min of the journal should show only health pings, rumqttc Pingreq, and speechmatics idle timeouts. Never inject utterances into the live daemon.
5. **Swap, from a script file** (fill in NEW and OLD; OLD is the binary `run-echo.sh` execs now):
   ```
   #!/bin/bash
   set -euo pipefail; umask 002
   D=/home/claude/echo-data; NEW=echo.bin-main-<sha>; OLD=<current binary name>
   STAMP=$(date -u +%Y%m%dT%H%M%SZ)
   grep -q "exec $D/$OLD " $D/run-echo.sh || { echo "run-echo.sh does not exec $OLD; refusing"; exit 3; }
   cp /var/lib/pantheon/echo-target/release/echo $D/$NEW && chmod 0755 $D/$NEW
   cmp -s /var/lib/pantheon/echo-target/release/echo $D/$NEW || { echo "copy mismatch; refusing"; exit 4; }
   cp -p $D/run-echo.sh $D/run-echo.sh.bak-pre-$NEW-$STAMP
   sed -i "s#exec $D/$OLD #exec $D/$NEW #" $D/run-echo.sh
   echo "rollback file: $D/run-echo.sh.bak-pre-$NEW-$STAMP"
   systemctl --user restart echo.service
   sleep 45
   systemctl --user is-active echo.service
   systemctl --user show echo.service -p MainPID,ActiveEnterTimestamp,NRestarts
   ```
6. **Verify:**
   - 0 `ERROR` lines since the restart. Strip ANSI (`\x1b\[[0-9;]*m`) before grepping.
   - The unique WARN set matches the previous boot. Expected baseline (2026-09-30): the `drift-held` tool-policy lines (compare the TOOL SET; the count follows Home Assistant's tool list), the never-assessed quarantine line, `erasure: prod: unconfigured`, and one MQTT "closed by peer" at the restart. Each deliberate subsystem restart re-parses the config and logs `config declares [event_store.*].aggregate_type_map … retired (T-1742)` while the live TOML still carries that key. Anything new goes to the owner.
   - `j17 event store configured` and `utterance intake subscribed to echo/stt/final` are present, and liveness pings resume.
   - If the change touches a live control path, exercise it through config-control. Example: the T-4463 supervisor fix was verified with repeated `{"op":"apply_restart","resource":"setting","payload":{"subsystem":"silero-vad"}}` (and `speechmatics-stt`), each replying `ok:true`.
7. **Report:** mail artisan (echo owner) the deployed sha, the restart stamp, the rollback file, and any WARN delta. Note it on the ticket. Tell the operator in one line only if he's waiting on the change.

## Reading live status

- **HO seat (read-only):** there's no `status` subcommand. Subscribe first, then publish:
  `mosquitto_sub -h 127.0.0.1 -p 1883 -t echo/config/reply -C 5 -W 8 &` then
  `mosquitto_pub -h 127.0.0.1 -p 1883 -q 1 -t echo/config/request -m '{"request_id":"<id>","op":"get","resource":"status"}'`,
  and match the reply by `request_id`. Metrics are under `data.metrics`.
- **Other seats:** `/home/claude/echo-data` isn't readable. Read the T-4420 export in `/var/lib/pantheon/echo-evidence/status/` (`echo-status.json`, `echo-journal-summary.json`, and per-boot archives in `boots/`), refreshed every 2 min (`tools/echo-status-export/README.md`).
- **Live event stream (any seat):** the passive tap recorder writes Echo's MQTT tap topics (no audio) as hourly JSONL to `/var/lib/pantheon/echo-evidence/tap/`, kept 7 days. Read it for post-deploy checks and per-utterance traces instead of asking the HO to record (`tools/echo-tap-recorder/README.md`, T-5260).

## Rollback

`cp -p /home/claude/echo-data/run-echo.sh.bak-pre-<NEW>-<STAMP> /home/claude/echo-data/run-echo.sh && systemctl --user restart echo.service`, then verify as in step 6. Earlier binaries stay in `echo-data/`, so the chain of `.bak` files walks back further.

## Arming and disarming the audio tap (ADR-252, T-4814)

The supervised-session audio tap (echo ADR-252) records raw audio for replay. Agents never arm it on their own initiative. Arming and disarming are HO operator acts on this path, and this section must exist before the tap's first live arming (PR-G).

`<data_dir>` is the daemon's data directory. Confirm it before you start. Today the TOML sets no `data_dir`, so it is the default `$HOME/.config/echo`; `/home/claude/echo-data` holds only the binaries, `run-echo.sh` and the TOML (verified 2026-10-15, T-4951), because the tap's directory is always `<data_dir>/audio-tap` and can't be configured.

**Arm** (once per supervised session):
1. Receive two things from artisan (the echo owner): the `echo-demo.toml` diff that sets `[audio_tap] enabled = true`, and a **new** `session_label` matching `[a-z0-9-]{1,48}`. The operator must be present for the session; the tap exists only for supervised sessions.
2. Confirm the label is unused. Run `grep -qx "<label>" <data_dir>/audio-tap/.used-labels`, and check that no session directory under `<data_dir>/audio-tap/` ends in that label. If either matches, refuse and ask for a new label. (The daemon would refuse too, reporting `label_already_used`, but catch it before the restart.)
3. Write a ledger entry (ledger-append SOP): the label, who is present, the planned duration (`max_session_mins`, default 30, never above 120), and the ticket.
4. Back up the TOML (`cp -p echo-demo.toml echo-demo.toml.bak-pre-tap-<STAMP>`), apply the diff, run the idle check (Procedure step 4), then `systemctl --user restart echo.service`.
5. Read the status snapshot (below). The tap field must show `armed` with the label, and elapsed time and bytes must be rising once audio flows.

**Disarm**, straight after the supervised session ends, even if `max_session_mins` already auto-stopped it:
1. Set `enabled = false` (or restore the `.bak-pre-tap` TOML) and restart.
2. The status snapshot must show the tap `idle`. Then confirm the session directory's manifest is finalized (it has a stop reason). An unfinalized manifest means a crash mid-session; note it on the ticket.
3. Add the ledger outcome: stop reason, bytes, drops, and any last error.

**Any `data_dir` change** (TOML edit, migration or restore): before restarting on the new `data_dir`, carry the old `<old data_dir>/audio-tap/` across. It is the permanent bench corpus (ADR-252, kept indefinitely; there is no retention sweep), and the daemon's inventory only sees the current `data_dir`, so a corpus left in the old one is orphaned. Move the session directories **and** `.used-labels` into the new `<data_dir>/audio-tap/` (keep mode 0700 and the files 0600) and record the move in the ledger. Do the same for `<old data_dir>/utterance-samples/`, the ADR-253 bench samples (permanent, no sweep; T-4951): move every sample `.wav` and its `.json` sidecar into the new `<data_dir>/utterance-samples/` (directories 0700, files 0600), then confirm after the restart that the `bench_samples` status shows the same `samples_on_disk`. Never leave it orphaned, and never delete it as part of a deploy; deleting corpus files is a separate, deliberate operator act.

Rollback for arming is disarming. The tap writes only under `<data_dir>/audio-tap`, and a disabled tap never touches the audio path.

## Common failure modes

- `cargo: command not found` (exit 127) buried in a background log, from a missing PATH line.
- A peer seat can't overwrite build artifacts: the build ran without `umask 002`.
- A binary named after `main`'s tip when it was built from an earlier sha, so the deployed version is misreported.
- Trusting a WARN count instead of the set.
- A deploy mid-conversation. Check idle first.

## Boundary

- **HO seat only.** `echo.service` is a user unit on the `claude` account, and `/home/claude/echo-data` is private to it. Other seats (artisan, mason, recovery) request a deploy from the HO with the merged sha and what to verify.
- Config changes to `echo-demo.toml` are not binary deploys. Some settings apply live through config-control (`setting/set`, `setting/apply_restart`); others need a restart. Don't hand-edit the live TOML without the owner.
- CI has its own cargo target. Never point CI at `/var/lib/pantheon/echo-target`.

## Cross-references

- `merging-an-agent-branch-pr.md` (how the change reached `main`).
- echo-prismatic `sop/debugging-conversation-pipeline.md` (when the verify step finds a pipeline problem).
- `/var/lib/pantheon/README-echo-target.md` (shared target substrate, T-3777); `tools/echo-status-export/README.md` (T-4420).
