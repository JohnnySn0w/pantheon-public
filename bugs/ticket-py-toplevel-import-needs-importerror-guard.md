# Gotcha: a new top-level import in `tools/ticket.py` needs a `try/except ImportError` guard

**Surfaced:** 2026-06-13/14 during T-0761 component-4 (the `provision_repos` import added to `ticket.py`).

## The trap

`tools/ticket.py` is **spawned as a subprocess from isolated git worktrees** by some tests (canonically `TestRetargetDetachedHeadPush`, which runs `ticket.py` from a throwaway worktree like `/tmp/<…>/lint_wt/`). A fresh worktree checked out at an older commit — or any checkout that predates a newly-added `tools/` module — **does not contain the new file**. A bare top-level `from tools.<newmodule> import …` then raises `ImportError` at module-import time *inside that subprocess*, breaking the test (and, in production, any cross-worktree `ticket.py` invocation) before `main()` ever runs.

## The fix (pattern to copy)

Guard every new top-level import that `ticket.py` adds:

```python
try:
    from tools.provision_repos import provision_repos
except ImportError:
    provision_repos = None  # type: ignore[assignment]
```

…and have the call site skip gracefully when the name is `None`:

```python
if rc == 0 and provision_repos is not None:
    ...
```

Use a **module-level name** (not a lazy import inside the function) so test patches (`patch("tools.ticket.provision_repos", …)`) resolve cleanly — the lazy-import alternative can't be patched at that path.

## Why it recurs

`ticket.py` is the load-bearing ticket-lifecycle entrypoint; new features bolt new `tools/` modules onto it over time, and each new top-level import re-opens this trap. The worktree-subprocess isolation is invisible in a normal single-checkout test run, so the failure only shows up in the cross-worktree tests — easy to miss until CI/smoke catches it.

**Before adding a top-level import to `ticket.py`, apply the guard.** Grep this file (`bugs/`) is part of the pre-flight; this entry is here so the pattern is found before the test breaks, not after.
