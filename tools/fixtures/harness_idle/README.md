# harness_idle fixtures (T-5793)

Session-log windows for `tools/harness_idle.py`, cut on 2026-11-01 from real
captures on the artisan seat (`suborch2`): Claude Code transcripts under
`~/.claude/projects/-home-suborch2-pantheon-config/` and codex cli 0.147.0
rollouts under `~/.codex/sessions/`.

CONTENT-STRIPPED. Each record keeps only the structural fields the classifier
reads: `type`, `subtype`, `timestamp`, `turnOrigin`,
`pendingBackgroundAgentCount`, assistant `message.stop_reason`,
`toolUseResult.backgroundTaskId`; codex `payload.type`, `turn_id`,
`completed_at`, `reason`, and on `session_meta` only `id`, `source`,
`originator`, `parent_thread_id`, `cli_version`. Mid-turn task-notification
attachments keep `attachment.type` (`queued_command`), `attachment.commandMode`
and a `prompt` skeleton (`<task-id>` + `<status>` only). Message text, tool inputs and
outputs, and every other field are dropped. User `message.content` survives
only as a structural marker: a bare `tool_result` block, a task-notification
skeleton (`<task-id>` + `<status>` only), or the literal
`[Request interrupted by user]`. Record order and timestamps are as captured.

## Cut from real captures (contiguous record windows)

| file | source | case |
|---|---|---|
| `claude/idle.jsonl` | `66c41242-…` records 445-453 | human turn, end_turn, turn_duration, trailing untimestamped metadata |
| `claude/working_prompt.jsonl` | `66c41242-…` records 445-454 | the idle turn, then the next human prompt |
| `claude/mid_tool.jsonl` | `384ca4a1-…` records 895-902 | a complete turn, then a human prompt and an assistant `tool_use` with no result yet |
| `claude/bg_agent.jsonl` | `0dd4b7b3-…` records 10110-10113 | turn ends with `pendingBackgroundAgentCount: 1` |
| `claude/bg_shell_open.jsonl` | `d8e354bf-…` records 534-543 | turn launches background shell `bbv2hefe2`, then ends |
| `claude/bg_shell_closed.jsonl` | `d8e354bf-…` records 534-556 | the above plus the task-notification for `bbv2hefe2` and that turn's end |
| `claude/bg_shell_attachment_closed.jsonl` | `81abedc4-…` records 2749-2860 | a turn launches background shell `bzeog0dnb` and ends; in the next turn its completion arrives mid-turn as an `attachment` `queued_command` task-notification, then that turn ends |
| `claude/interrupt.jsonl` | `757ea709-…` records 2816-2838 | a complete turn, then a turn ended by `[Request interrupted by user]` (no turn_duration) |
| `codex/idle.jsonl` | `rollout-2026-10-28T01-58-11-01a1803a-…` line 0 + records 151-169 | task_started ... task_complete |
| `codex/working.jsonl` | same rollout, line 0 + records 151-172 | the idle turn, then a new task_started |
| `codex/turn_aborted.jsonl` | same rollout, line 0 + records 170-192 | turn ended by `turn_aborted` (reason `interrupted`) |
| `codex/exec_rollout.jsonl` | `rollout-2026-08-12T20-37-54-019ff7b2-…` whole file | a `codex exec` rollout (`source: exec`), excluded by the locator |

## Hand-derived (edge cases with no natural capture)

- `claude/no_turn_duration.jsonl`: `claude/idle.jsonl` with the
  `system/turn_duration` record removed (schema-drift guard -> UNKNOWN).
- `codex/subagent_rollout.jsonl`: `codex/idle.jsonl` minus its trailing
  `thread_settings_applied`, every timestamp shifted +10 h, and line 0
  replaced by a `session_meta` naming `parent_thread_id` = the main rollout's
  id (a placeholder id of its own). It is newer than the main rollout and
  ends in `task_complete`: the wait_agent trap the locator must skip.

Tests build the remaining edge cases (stale log, mtime rewritten while idle,
recent turn end, background past the max) from these files with a fake reader
and an injected `now`; they never read seat homes or `/tmp`.

Cutter scripts (not committed): cut_fixtures.py + derive_fixtures.py in the
T-5793 implementer scratchpad.
