---
symptom: "A shell-probing monitor reports a confident, specific CAUSE that turns out to be unmeasured — 'bridge died/never-started', 'process DEAD', 'mailbox not provisioned' — and the `rc != 0 -> inconclusive` guard sitting right above it never fires. The cause: `cmd 2>/dev/null || echo SENTINEL`. `echo` succeeds, so the pipeline's exit status is ALWAYS 0 no matter why `cmd` failed, making that guard unreachable dead code, and every distinct failure collapses into one sentinel. Measured on `cat`: a genuinely absent file, a mode-000 file, and a present file under a non-traversable parent all return rc=0 out=MISSING — byte-identical; only the healthy read differs. The consequence is MISDIRECTION rather than a crash: a blind read becomes a named diagnosis, the alert sends the reader after the wrong thing, and any remediation keyed off it acts on a healthy target."
fix: "Read the sentinel's NAME as the verdict. A don't-know word (UNKNOWN, DIRTY-OR-FAILED) is safe because the caller must branch on not-knowing; a conclusion word (MISSING, DEAD, ABSENT) is the defect, because a blind value becomes a category. Do not widen the sentinel vocabulary in place — PROVE the claim with a second probe on the ambiguous branch only: `if [ -e P ]; then echo PRESENT; elif [ -d \"$(dirname P)\" ]; then echo ABSENT; else echo UNKNOWN; fi`, tri-stated to True/False/None. For an exit-status question capture the real status instead of masking it: `o=$(ps -o args= -p $pid 2>/dev/null); r=$?` then treat r==0 as the answer, r==1 as ps's documented no-such-process, anything else as PSFAIL -> inconclusive. Find them with `grep -rnE '\\|\\|\\s*echo\\s+[A-Z_]{3,}' --include='*.py' .` and triage each with two questions in order: can the command fail for a reason other than the one the sentinel names, and does the caller turn that sentinel into a category or into a don't-know? A hit is a defect only when the answers are yes and 'a category'."
domain: "shell, subprocess, exit-status, sentinel, blind-predicate, misdirection, fail-open, monitor, alerting, probe, systemd, pantheon"
---

# `cmd || echo SENTINEL` masks the exit code, so the `rc != 0` guard is unreachable

**Grep keywords:** `|| echo MISSING`, `|| echo DEAD`, `|| echo ABSENT`, blind predicate,
misdirection, fail-open, sentinel, `rc != 0`, inconclusive, probe

**Found:** 2026-08-02 (T-2359, the MISDIRECTION half of T-2309's sweep).
**Fixed in:** `tools/channel_liveness_monitor.py` (two sites).

## The shape

A probe shells out and appends a fallback so it always gets *something* on stdout:

```python
rc, out = _run(sudo + ["bash", "-c", f"cat {path} 2>/dev/null || echo MISSING"])
if rc != 0:
    return "inconclusive", f"read rc={rc}"      # <-- CAN NEVER FIRE
if out == "MISSING":
    return "down", "... died/never-started"      # <-- a CAUSE, from a blind read
```

`echo` succeeds. So the pipeline's exit status is `echo`'s, always 0, **no matter why the
first command failed**. The `rc != 0 -> inconclusive` guard above it is dead code, and every
distinct failure collapses into one sentinel.

## Measured, not inferred

```
A genuinely absent               rc=0 out=MISSING
B present, mode 000              rc=0 out=MISSING
C present, dir not traversable   rc=0 out=MISSING
D healthy                        rc=0 out=777
```

A, B and C are byte-identical. Only D is distinguishable.

## Why this one is worse than an ordinary fail-open

A detector that fails *open* is safe when the caller keeps looking. This one fails into a
**confident wrong category** and then states a cause in its own voice — the alert said the
bridge "died/never-started" and pointed the reader at "Likely the single-poller 409" about a
bridge that may be running fine, and a flag-gated remediation would cycle a healthy session.
That is the T-2308 shape (an unreadable inbox presenting as *empty*, then alerting that the
agent had "NEVER received a message").

**"Fails open = safe" is too coarse. What matters is what it fails INTO, and whether anything
re-evaluates.**

## The tell, and the rule

The sentinel's *name* tells you whether the site is safe:

| sentinel | kind | verdict |
|---|---|---|
| `MISSING`, `DEAD`, `ABSENT` | a **conclusion** | unsafe — a blind value becomes a category |
| `UNKNOWN`, `DIRTY-OR-FAILED` | a **don't-know** | safe — caller must handle not-knowing |

**Rule: a fallback sentinel may only name what you have PROVEN. If the command can fail for
more than one reason, the sentinel must be a don't-know word, and the caller must have a
branch for it.**

The same function in `channel_liveness_monitor.py` contained *both* patterns — `UNKNOWN` for
the cwd/ppid probes (correct, with a documented fallback path) and `MISSING`/`DEAD` for the
pidfile and ps probes (the defect). Same author, same file. This is an easy thing to get right
in one branch and wrong in the next, which is why it is worth a grep rather than a memory.

`tools/deploy_hook.py:577` is the model to copy: its sentinel is literally `DIRTY-OR-FAILED`,
which refuses to distinguish two states it cannot distinguish, and the name says so.

## How to fix

Do not widen the sentinel vocabulary in place — *prove the thing you want to claim* with a
second probe on the ambiguous branch only:

```python
def _pidfile_is_provably_absent(cfg, sudo) -> bool | None:
    """True if provably absent, False if it EXISTS but was unreadable, None if
    the probe could not tell."""
    rc, out = _run(sudo + ["bash", "-c",
        f'if [ -e {p} ]; then echo PRESENT; '
        f'elif [ -d "$(dirname {p})" ]; then echo ABSENT; '
        f'else echo UNKNOWN; fi'])
    ...
```

For an exit-status question, capture the real status instead of masking it — `rc 1` from `ps`
is its documented "no such process"; anything else is ps failing to answer:

```bash
o=$(ps -o args= -p $pid 2>/dev/null); r=$?
if [ $r -eq 0 ]; then printf "%s" "$o"
elif [ $r -eq 1 ]; then echo NOSUCHPROC
else echo PSFAIL; fi
```

## Finding more of them

```
grep -rnE '\|\|\s*echo\s+[A-Z_]{3,}' --include='*.py' .
```

Then for each hit ask the two questions in order: **(1) can the command fail for a reason
other than the one the sentinel names?** and **(2) does the caller turn that sentinel into a
category, or into a don't-know?** A hit is only a defect when the answer to (1) is yes *and*
the answer to (2) is "a category".

## Related

- `T-2308` — the known instance (`mail_staleness_monitor._count` returning 0 on `OSError`,
  latching, so the surface went permanently blind).
- `T-2309` — the destructive half of the sweep (`artifact_gc`, unscannable procfs authorising
  an `rmtree`); its exit-enumeration method does **not** reach this class, because misdirection
  has no destructive exit to walk back from.
- `T-2288` — the same defect one layer up and in pure Python: an `else` branch naming "lease
  held by another consumer" for every skip shape the enumeration did not know about.
- `T-2393` — a prior correction to this very alert string, which removed a different unmeasured
  claim ("while session appears healthy"). The string was cleaned; the predicate feeding it was
  not.
