---
symptom: "In a test process, any later real git_mutation_lock() call crashes on mkdir ('.git' exists as a FILE) or acquires a lock in the wrong place — because tools/_git_utils.py's _git_common_dir() caches its resolution module-globally on FIRST call, and if that first caller had subprocess.run mocked with a generic empty-stdout return, the resolver silently caches the REPO_ROOT/'.git' fallback, which in a linked-worktree checkout is a file (gitdir: pointer), not a directory; the poisoned cache then applies to every subsequent unmocked caller in the same process, making the failure test-run-ORDER-dependent (a real git call warming the cache first hides it)."
fix: "Test-side: any test class whose code path can reach git_mutation_lock() sets PANTHEON_GIT_MUTATION_LOCKFILE to a per-test tempfile in setUp (pop in tearDown) — the env override bypasses _git_common_dir entirely (pattern established in tools/test_git_mutation_lock.py, retrofitted to test_watch_runner classes in T-1774). Primitive-side hardening LANDED: (a) fallback not cached — T-1816; (b) resolution cached only when confirmed a directory — T-1931."
domain: pantheon, git, _git_utils, module-global-cache, test-isolation, mock, worktree, lockfile
---

# _git_common_dir's module-global cache is poisoned by a mocked-subprocess first caller

## Mechanism

`tools/_git_utils.py::_git_common_dir()` runs `git rev-parse --git-common-dir` once and caches the
result module-globally. The guard is `if res.returncode == 0 and res.stdout.strip():` — a test that
mocks `subprocess.run` with a generic `returncode=0, stdout=""` sends it to the fallback branch,
which caches `REPO_ROOT / ".git"`. In a linked worktree, `.git` is a **file** (`gitdir: ...`), so
the next real `git_mutation_lock()` in the same process does `mkdir()` on a file's "parent" shape
and crashes — or, in a main checkout, silently locks at the fallback path even where the real
common dir differs.

## Why it hides

Test-discovery order: if any module with REAL git calls runs first (e.g. `test_git_utils_integ`
alphabetically before `test_watch_runner`), the cache is warmed with the true value and the
poisoning path never executes. Running the later module in isolation (a legitimate targeted smoke)
exposes it. Found in T-1774 when wrapping watch_runner's pipeline in the lock broke a pre-existing,
unrelated test only under isolated-module runs.

## Standing fix + open hardening

- Test-side isolation (env override per test class) is the landed fix — see the `fix:` line.
- The primitive-side hardening is now BOTH LANDED (bug fully addressed):
  - (a) **don't cache the fallback** — landed T-1816 (`f144b0f3`); the fallback branch returns
    `REPO_ROOT/".git"` and caches nothing (a `test_ticket` three-`rev-parse` assertion pins the
    uncached re-resolution).
  - (b) **validate-is-dir before caching** — landed T-1931; `_git_common_dir()` caches only a
    resolution confirmed to be a directory, declining to cache (not changing the returned value)
    otherwise, with the `is_dir()` check wrapped so it degrades to re-resolve rather than raising
    inside `git_mutation_lock()`. First direct tests the primitive has ever had (+4 non-vacuous
    guards, incl. the never-cache over-correction guard).

## Keywords

_git_common_dir, module-global cache, cache poisoning, mocked subprocess.run, empty stdout,
git rev-parse --git-common-dir, linked worktree, .git file, gitdir pointer, mkdir FileExistsError,
test order dependent, PANTHEON_GIT_MUTATION_LOCKFILE, git_mutation_lock, T-1774, T-1770

## Third door (T-2336, 2026-08-02): non-str stdout materializes a `MagicMock/` tree

T-1816 closed empty-stdout (fallback, uncached); T-1931 closed non-empty-wrong-str (return but
don't cache). The remaining door: a broadly-mocked `subprocess.run` with NO `stdout=` kwarg hands
back a **child MagicMock** — `.strip()` is truthy, `os.path.isabs()` is False, and `REPO_ROOT / raw`
resolves through the mock's fspath into a fabricated RELATIVE path. The first `git_mutation_lock`
under such a test then `mkdir(parents=True)`s a literal `MagicMock/mock.stdout.strip()/<id>/` tree
in the repo root and takes the lock inside it. Found when T-2336 added the lock to
`inbox_consumer`'s stamp/clear paths and a pre-existing test (`patch("tools.inbox_consumer.
subprocess.run")` — which patches the GLOBAL module) started traversing it.

Standing fix part c: `isinstance(res.stdout, str)` guard before trusting the resolution — a real
`subprocess(text=True)` stdout is always `str`, so non-str can only be a test double; it takes the
same uncached fallback as empty stdout. Regression test:
`test_git_utils.CommonDirMockedStdoutTest`. The general lesson stands one door wider: **every field
read off a mocked subprocess result is attacker-controlled by the test's laziness — validate TYPE,
not just truthiness, at the trust boundary.**
