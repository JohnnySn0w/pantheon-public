---
title: Bash heredoc + JSON payload with literal `$` characters → invalid JSON escape → ledger-validity guard rejects commit
symptom: "Appending a ledger JSONL line via an unquoted bash heredoc containing a literal $ makes the pre-commit hook reject it ('ledger JSON-validity violation ... Invalid escape') — the shell-escaped $ becomes an invalid JSON string escape"
fix: "Build the ledger JSON in Python via json.dumps (no escape-count guesswork), or use a quoted heredoc delimiter <<'EOF' when you don't need shell interpolation"
domain: bash / shell / json / ledger
severity: annoyance
first-observed: 2026-09-13
last-verified: 2026-09-13
verification: reproduced this session appending a ledger entry with `~$35` in the notes field via bash heredoc; commit hook rejected with `Invalid \escape: line 1 column 307 (char 306)` and the ledger-validity error format described below
---

## Symptom

Appending a JSON line to the ledger via bash heredoc:

```bash
cat >> ledger/YYYY-WNN/YYYY-WNN.jsonl <<EOF
{"id": "...", "notes": "MoniCard actual \\\$35 with case+stand bundle ..."}
EOF
git commit ...
```

...fails at commit time with the ledger-validity commit hook:

```
ledger JSON-validity violation — commit refused.

  ledger/YYYY-WNN/YYYY-WNN.jsonl: invalid JSON: Invalid \escape: line 1 column 307 (char 306) -- '...MoniCard actual \$35 with case+stand bundle...'

  A ledger line ADDED in this commit is not parseable JSON (or not a JSON object). Fix the entry, re-stage, and retry.
```

## Root cause

Bash heredoc with an **unquoted delimiter** (`<<EOF`, not `<<'EOF'`) performs shell parameter expansion. To emit a literal `$` in the output, the shell requires `\$`. But the JSON parser then sees `\$` inside a string and rejects it — JSON's valid string escapes are `\"`, `\\`, `\/`, `\b`, `\f`, `\n`, `\r`, `\t`, `\uXXXX`. `\$` is not a valid JSON escape.

The escape count for a literal `$` inside a JSON string emitted through an unquoted bash heredoc is delicate. Options that all have their own hazards:
- `\\\$` (three backslashes + `$`) — shell eats one backslash to escape the second, then the `\$` becomes literal `\$` — invalid JSON escape (this bug).
- `\\\\\$` (five backslashes + `$`) — emits `\\$` in JSON which parses to a literal backslash followed by `$` — but that's a shape change (unwanted extra backslash in your data).
- `\$` (one backslash + `$`) — shell interprets as escape-of-$, emits literal `$`, JSON parses cleanly. But then you can't have `\$` mean "literal dollar" from the *shell's* perspective in every escaped position — mixing with other `$` uses in the same heredoc is a maintenance hazard.

## Surgical fixes

**Preferred: build the JSON in Python via `json.dumps`.** Zero escape-count guesswork; `json.dumps` handles every quoting concern correctly. Pattern:

```bash
python3 -c "
import json
entry = {
    'id': '...',
    'notes': 'MoniCard actual ~\$35 with case+stand bundle ...',
}
with open('ledger/YYYY-WNN/YYYY-WNN.jsonl', 'a') as f:
    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
print('appended')
"
```

Notes on this pattern:
- Python single-quoted strings still need `\$` inside the outer `python3 -c "..."` shell double-quotes if you want a literal `$` — but Python doesn't care about `\$` (unknown escapes are kept literally in Python 3), and the JSON output is clean.
- `ensure_ascii=False` preserves any non-ASCII characters (Chinese, emoji) as-is rather than `\u` encoding.

**Alternative: use a quoted heredoc delimiter.** `<<'EOF'` disables shell expansion entirely, so `$` is literal:

```bash
cat >> ledger/YYYY-WNN/YYYY-WNN.jsonl <<'EOF'
{"id": "...", "notes": "MoniCard actual ~$35 ..."}
EOF
```

But this loses the ability to interpolate variables inside the heredoc (e.g. `${TS}`). If your entry needs a timestamp from a shell var, you're back to Python.

**Alternative: write to a scratch file, validate, then append.** Adds a step, useful for large entries where interactive validation matters.

## Detection

The commit hook's `ledger JSON-validity violation` message is the surfaced form. After fix:

```bash
# quick re-validate before re-commit
tail -1 ledger/YYYY-WNN/YYYY-WNN.jsonl | python3 -c "import sys, json; json.loads(sys.stdin.read()); print('valid')"
```

## Related

- `pantheon-config/tools/hooks/` — the pre-commit hook that catches this (search for the "ledger JSON-validity violation" string to find it).
- Reflex habit from `feedback_heredoc_breaks_and_chain_gate.md` (memory pin): heredoc breaking `&&` chains is a sibling shell-heredoc-hazard class — different mechanism (exit code capture), same broader lesson that heredocs are subtle.
- The bug is more general than ledger JSON: any tool that writes JSON via bash heredoc + interpolation + `$` in strings hits it. Ledger is just where our commit hook happens to catch it.

## Fix-in-tools potential

Not obviously worth a `tools/ledger_append.py` helper for one-off use — the Python-inline pattern above is already a two-second copy-paste and handles arbitrary payloads. If append-to-ledger becomes hot-pathed by multiple agents, a helper crystallizes then.
