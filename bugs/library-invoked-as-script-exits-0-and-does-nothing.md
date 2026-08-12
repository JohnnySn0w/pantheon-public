---
symptom: "you run a tools/ module as a script (e.g. `python3 tools/mailbox.py send --to orchestrator ...`), it prints NOTHING, exits 0, and the action silently did not happen — no message delivered, no file written, no error anywhere"
fix: "the module is a LIBRARY with no `__main__` block, so Python imported it, defined everything, hit EOF and exited 0. Import it and call the function instead. Structural fix: give the library a `__main__` block that sys.exit()s with the correct incantation (landed for tools/mailbox.py 2026-07-28)"
domain: "tools / python-invocation"
---

## Symptom

A `tools/` module is invoked as a script with plausible-looking CLI arguments:

```
python3 tools/mailbox.py send --to orchestrator --subject "..." --payload "..."
```

**Nothing is printed. The exit code is 0. The action did not happen.** No message in the
recipient's `new/`, no traceback, no warning. Every observable says success.

## Cause

The module has **no `if __name__ == "__main__":` block**. Python does exactly what it was
asked: it executes the file top-to-bottom, which defines the functions and constants, reaches
end-of-file, and exits **0**. The `argv` you passed is never read by anything — `argparse` is
only reached from a `main()` that is never called.

This is not specific to `mailbox.py`. **Any** library-shaped module in `tools/` behaves this
way, and the more CLI-plausible its name, the likelier someone tries it.

## Why it is worse than a normal failure

The failure mode is a **green signal from an instrument that cannot fail**. There is no
distinction, from the caller's side, between:

- the send succeeded, and
- the send was never attempted.

Both are "exit 0, no output". Specialist lost an inter-agent message this way and caught it only
by listing the recipient's `new/` and finding it empty — i.e. by checking the *delivery
surface* rather than the *invocation result*.

Cross-reference: this is Face 6 (**dead guard** — the check exists and does not check) in
`lessons/2026-07-23-proxy-vs-defining-surface-synthesis.md`, the "exit 0 from a library
invoked as a script" row.

## Fix

**Immediate:** import the module and call the function. For `mailbox` specifically, use the
**absolute** tools path — a relative one silently imports Python's stdlib `mailbox` instead:

```
python3 -c "import sys; sys.path.insert(0,'/home/claude/pantheon-config/tools'); \
import mailbox; print(mailbox.send('/home/the-ancestor/mailbox/<to>', sender='<from>', \
subject='...', payload='...'))"
```

**Structural (landed 2026-07-28 for `tools/mailbox.py`):** give the library a `__main__` block
that fails loudly and names the right invocation:

```python
if __name__ == "__main__":  # pragma: no cover
    import sys as _sys
    _sys.exit("mailbox.py is a LIBRARY and has no CLI. Import it instead: ...")
```

`sys.exit(str)` writes to **stderr** and exits **1**, so the silent-success path is closed.
Verified: real exit code 1, message on stderr, stdout empty, import path untouched.

## Generalising

Any `tools/*.py` with no `__main__` has this hole. A worthwhile sweep is:

```
for f in tools/*.py; do grep -q '__main__' "$f" || echo "no __main__: $f"; done
```

Not every hit needs a guard — a module nobody would plausibly invoke does not. Prioritise by
**how CLI-shaped the name is** rather than by count.

**Sweep run 2026-07-28 — 7 modules (excluding `test_*` and `_*`):**

| module | CLI-shaped? | verdict |
|---|---|---|
| `provision_repos.py` | **yes — reads exactly like a command** | **the standout; most worth a guard** |
| `model_stepper.py` | ambiguous | worth one if it grows a `main()` |
| `agent_role_map.py` | no — reads as a lookup table | leave |
| `repo_registry.py` | no — reads as a lookup | leave |
| `ledger_shard.py` | no — library helper | leave |
| `mail_staleness.py` | no — pure predicate, T-2157 | leave (deliberately I/O-free) |
| `conftest.py` | n/a — pytest collects it, nobody runs it | n/a |

So the actionable residue is **one** module, not seven. Filed rather than fixed here: adding a
guard to `provision_repos.py` is a different change from documenting the class, and a repo-
provisioning tool is exactly the kind where a silent no-op deserves its own review.

## Watch out for

When *testing* such a guard, `python3 tools/x.py ... | head -3; echo $?` reports **head's**
exit status, not python's — so it prints `0` and looks like the guard failed to fire. Check
the exit code without a pipe. (I hit exactly this while verifying the fix for exit-code
blindness.)
