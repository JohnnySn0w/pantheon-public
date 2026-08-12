---
symptom: "A direct `codex exec` invocation (the T-1775 workaround path, not the MCP wrapper) hangs for its entire timeout and produces no output file; its log's last line is 'Reading additional input from stdin...' — codex detects a non-tty stdin and blocks reading it, and a background/harness shell that leaves stdin open but writes nothing never delivers EOF, so codex waits forever and dies on the outer `timeout` with exit 124."
fix: "Always close stdin explicitly on direct codex exec runs: append `< /dev/null` (or pipe real intended input). The MCP wrapper is unaffected (it passes input= explicitly, which closes the pipe). Symptom signature to grep: 'Reading additional input from stdin' as the exec log's last line + exit 124 + missing -o output file."
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

## Relation

- `tickets/done/T-1775.md` — the MCP-wrapper outage that made direct-exec the workaround path; the
  wrapper itself always passed `input=` (closed pipe) and never hits this.
- The wrapper's post-T-1775 form also parametrizes its timeout — prefer it once the MCP server
  process has restarted with the new code; direct-exec + `< /dev/null` is the interim shape.

## Keywords

codex exec, stdin, non-tty, Reading additional input from stdin, exit 124, timeout, hang,
background shell, direct exec, ORG-041 review, -o output file missing, EOF
