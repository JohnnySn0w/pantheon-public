---
symptom: "A ticket body or transition-log entry is WRONG in one of two ways and the CLI reported success either time. DELETION form: a phrase is missing and the sentence still parses (`catches bare  (line 215)`). INSERTION form: the body contains output nobody wrote — a `ps` listing, a directory listing, a stray TUI string — interleaved with the real prose, sometimes recursively quoting the filing command itself. Cause in both cases: the text was passed through a shell, which interpreted backticks / `$VAR` / `$(cmd)` / `<angles>` / `!history` inside it before `ticket.py` ever saw it. Nine witnesses across sessions AND agents, at file, annotate, resolve, MAILBOX-SEND, and LEDGER-APPEND time; witness #8 had genuine side effects (a substituted backtick actually ran a tools/notify_policy.py command); witness #9 was the exit-127 shape (dropped-text-with-no-side-effects) in a `python3 -c` ledger-append."
fix: "Never let a shell see --ask/--context/--note/--with text. Pass it as an argv element from Python: `python3 - <<'PYEOF'` (quoted heredoc → literal) then `subprocess.run([sys.executable, 'tools/ticket.py', 'annotate', 'T-NNNN', '--note', note], capture_output=True, text=True)`. A LIST argv never invokes a shell, so the text arrives byte-for-byte. This is safe by construction; 'avoid meta-characters' is not, and has failed six times — markdown backticks around identifiers are the natural thing to type in a technical note. Two properties make it worse than it sounds: (1) the CLI's success line is NOT evidence the payload survived, so if you must use a shell, grep the written file for a phrase you expect; (2) substitution that SUCCEEDS is worse than substitution that fails — a failed command deletes a phrase (recoverable, obviously wrong), a successful one inserts authored-looking text no future reader can distinguish from what the author wrote."
domain: "ticket.py, shell-quoting, command-substitution, heredoc, durable-record, pantheon"
---

# `ticket.py` text arguments passed through a shell get substituted

**Symptom, deletion form:** a ticket or transition-log entry is missing a phrase. The sentence still
parses. The CLI reported success.

**Symptom, insertion form:** a ticket body contains output that nobody wrote — a process listing, a
directory listing, a stray TUI string — interleaved with the real prose.

## The rule

**Never let a shell see `--ask`, `--context`, `--note` or `--with` text.** Pass it as an argv
element from Python:

```python
python3 - <<'PYEOF'
import subprocess, sys
note = """...anything at all: `backticks`, <angles>, $VARS, !bangs, | pipes..."""
r = subprocess.run([sys.executable, "tools/ticket.py", "annotate", "T-NNNN", "--note", note],
                   capture_output=True, text=True)
print(r.returncode, r.stdout.strip() or r.stderr.strip())
PYEOF
```

The heredoc is `'PYEOF'` (quoted → literal) and `subprocess.run` with a **list** never invokes a
shell, so the text reaches `ticket.py` byte-for-byte. This is the only form that is safe *by
construction* rather than by remembering to avoid characters.

## Why "just avoid meta-chars" does not work

It has failed six times across sessions and agents. Markdown backticks around identifiers are the
natural thing to type in a technical note — the hazard is aimed exactly at the habit it needs you to
break. And the shell interprets these even inside double quotes in the common heredoc form:
`` `cmd` ``, `$VAR`, `$(cmd)`, `<foo>`, `!history`.

## Two properties that make it worse than it sounds

1. **The success line is not evidence the payload survived.** `ticket.py` sees a well-formed (if
   mangled) argument and reports success. If you must use a shell, grep the written file for a
   phrase you expect.
2. **Substitution that SUCCEEDS is worse than substitution that fails.** A failing command deletes a
   phrase — recoverable, and obviously wrong to a reader. A *succeeding* one inserts
   authored-looking text that no future reader can distinguish from what the author actually wrote.

## The sixth witness, 2026-08-02 (T-2239) — the insertion form

A filing whose `--ask` contained `` `ps -eo user,pid,etimes,args | grep mailbox_mcp` `` inside a
bash-quoted string. The shell ran it and spliced live `ps` output into the ticket body — including
the filing command itself, which was in the process table at that moment, quoting its own `--ask`
recursively, several times over. The delivered body ran to thousands of characters of interleaved
process listings with the real prose fragmented across them.

The finding inside it survived only because it was restated in clean prose at the end.

## Prior witnesses

- T-0018 (2026-05-03), T-0045 (2026-05-03) — `<angle>` placeholders eaten by input redirection.
- T-0046 (2026-05-03) — a backticked `systemctl --user enable --now …` was **executed**, and the
  command text vanished from the resolution log.
- T-2333 (2026-07-29) — a backticked `` `file` `` was substituted; `file` writes usage to stderr and
  produces empty stdout, so the word was simply deleted.
- T-2226 (2026-08-02) — `` `except Exception` `` substituted; bash printed `except: command not
  found`, the CLI printed success on the next line, and the ticket landed with a sentence missing
  its subject.
- T-3644 (2026-09-11) — `` `id` `` in a `ticket.py file --title` + `--ask` + `--context` arg;
  bash ran `id` and interpolated its output (`uid=1000(claude) gid=1003(pantheon-shared) …`)
  into the title AND across four paragraphs in the body. The ticket was for a *different* bug
  entirely (the ledger-append `id` field gap); the meta of a ticket about `id` being corrupted
  by `id` was noticed only on `head tickets/inbox/T-3644.md` post-file. Fix: `sed -i` in place
  + commit as `tickets: fix T-3644 backtick-id corruption in title + body`. Seventh witness.
- T-3026 (2026-09-11) — the **not-a-command insertion form, with real side effects**.
  A `python3 -c "…mailbox.send(payload='''…re-arm with \`python3 tools/notify_policy.py enter-maintenance recovery --minutes 30\`…''')"` heredoc: the triple-quote-Python-heredoc PROTECTS the inner `'''…'''` from Python, but bash still parsed the OUTER `bash -c` line and command-substituted the backticks BEFORE Python ever saw the string. Bash executed `python3 tools/notify_policy.py enter-maintenance recovery --minutes 30` for real (as claude, not the intended cross-account claude-fallback), armed a recovery-maintenance window I did not intend to arm at that instant, and replaced the backticked text in the payload with the tool's stdout (`recovery: maintenance ARMED until …`). Mailbox delivered a message that reads a plausible sentence with the wrong command in it. Two lessons: (a) a Python triple-quoted string INSIDE a bash `python3 -c "…"` does not shield backticks — bash sees the outer double-quoted string first; (b) the insertion here was ACTUAL STATE CHANGE, not just wrong text — the substituted command RAN. Fix path (b1 from the rule above) is unchanged and would have prevented both: pass the payload as an argv element from a QUOTED heredoc `python3 - <<'PYEOF'` (single-quoted → literal, bash performs no substitution), never via `python3 -c "…"`. Eighth witness. Side-effect impact bound in this instance (the accidental re-arm was beneficial for T-3026's timing), but the pattern is unbounded — the same construction could as easily have run `rm`, `curl`, or a mailbox-send with a wrong subject.
- 2026-09-12 (T-3026 gate-a session close ledger append, orchestrator) — a `python3 -c "…"` invoked with backticks around a literal `--launch-audit` inside the Python string. Same shape as #8 but WITHOUT side effects: bash tried to run `--launch-audit` as a command, exit 127 "command not found", stdout empty, so the substituted text was simply dropped. The ledger entry landed with the literal command name silently clipped — reader relies on surrounding context to reconstruct it. Bash-visible in real time as the `--launch-audit: command not found` line in the tool output, so caught within the same turn (unlike #7's post-write discovery), but the append had already happened; ledger is append-only so a corrective supersede would be over-correction for a cosmetic clip. Ninth witness. Fix path (b1) unchanged: single-quote the outer (`python3 -c '…'`), or use `python3 - <<'PYEOF'` heredoc — either would have made bash see the backticks as literal.

## See also

- `sop/standard/mailbox-is-not-the-shared-record.md` — the durable-record discipline this defeats.
- `tickets/README.md` — direct edits to `tickets/done/<id>.md` are forbidden; the CLI is the only
  write path, which is why a corrupted write is expensive to repair.
