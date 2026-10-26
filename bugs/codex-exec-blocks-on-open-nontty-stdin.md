---
symptom: "A direct `codex exec` invocation (the T-1775 workaround path, not the MCP wrapper) hangs for its entire timeout and produces no output file; its log's last line is 'Reading additional input from stdin...' — codex detects a non-tty stdin and blocks reading it, and a background/harness shell that leaves stdin open but writes nothing never delivers EOF, so codex waits forever and dies on the outer `timeout` with exit 124."
fix: "Always close stdin explicitly on direct codex exec runs: append `< /dev/null` (or pipe real intended input). The MCP wrapper (`mcp/pantheon-mcp-codex/server.py`) is now SAFE too as of claude-config commit ba38d9d (T-3174, 2026-08-24) — but was PREVIOUSLY only-partially-safe: it always passed `input=` to `communicate()` but set `stdin=subprocess.PIPE if stdin_text else None` at Popen time, so the no-context path (empty stdin_text) left stdin=None which INHERITS the MCP server's own stdio channel to Claude Code — codex read that and hung indefinitely. Symptom on the wrapper side: `codex_second_opinion` with no `context_path` times out at the requested `timeout_s` (60/180/300s all observed); ~/.local/state/pantheon/codex_calls.jsonl records `ERROR: timeout after Ns waiting for codex`. Symptom signature to grep on the direct-exec side: 'Reading additional input from stdin' as the exec log's last line + exit 124 + missing -o output file."
domain: pantheon, codex, subprocess, stdin, direct-exec, tooling
---

# codex exec blocks forever on an open non-tty stdin

## Observed

2026-07-15 ~05:38Z: the T-1762 targeted design review ran via direct `codex exec` in a harness
background shell (the T-1775 direct-exec workaround). It burned its full 600s `timeout` bound and
exited 124 with no `-o` output file. `t1762_exec.log`'s last line: `Reading additional input from
stdin...` — codex saw stdin as non-tty (harness background shell), decided to read supplementary
input from it, and the shell never wrote nor closed the descriptor. An earlier identical-shape run
(the T-1767 review, 02:15Z same day) succeeded only because its stdin state happened to deliver EOF.

## Fix / rule

`< /dev/null` on EVERY direct `codex exec` invocation that doesn't intend to pipe stdin. Retry with
that redirect succeeded immediately after.

## MCP-wrapper instance of the same class (T-3174, 2026-08-24)

The MCP wrapper (`mcp/pantheon-mcp-codex/server.py` in claude-config) is exactly the same failure class when the `stdin_text` branch is False (no `context_path` passed to `codex_second_opinion`):

```python
# OLD (broken for no-context):
proc = subprocess.Popen(
    args,
    stdin=subprocess.PIPE if stdin_text else None,  # None inherits parent stdin
    ...
)
proc.communicate(input=stdin_text.encode(...) if stdin_text else None)  # input=None doesn't close inherited stdin

# NEW (fixed):
proc = subprocess.Popen(
    args,
    stdin=subprocess.PIPE if stdin_text else subprocess.DEVNULL,
    ...
)
```

`stdin=None` in Popen means "inherit parent stdin," which for the MCP server IS the stdio channel talking to Claude Code — codex reads it and blocks forever. Fixed in claude-config commit `ba38d9d` (T-3174) with `stdin=subprocess.DEVNULL` in the no-context path.

Empirical evidence pre-fix: three consecutive `codex_second_opinion` health probes with no context_path timed out at 60s, 180s, 300s from the claude seat, while Consultant's direct codex probe from the codex seat (bash-child, stdin naturally closed) returned in 17.16s.

## Relation

- `tickets/done/T-1775.md` — the MCP-wrapper outage that made direct-exec the workaround path.
- `tickets/done/T-3173.md` — the coverage+liveness diagnostic (Specialist-filed) that surfaced the current instance.
- `tickets/done/T-3174.md` — the two-line surgical fix (`stdin=subprocess.DEVNULL` in the no-context path).
- The wrapper's post-T-1775 form parametrizes its timeout — that piece stays valid; T-3174 adds the stdin discipline.

## Keywords

codex exec, stdin, non-tty, Reading additional input from stdin, exit 124, timeout, hang,
background shell, direct exec, ORG-041 review, -o output file missing, EOF
