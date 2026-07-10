# auto-commit cron scripts — default-safe + `--commit-prod` contract

## When to use this

Whenever you write a new script that:
- Runs from a cron / systemd timer.
- Auto-commits to a Pantheon git repo (`pantheon-config`, `echo-prismatic`,
  `kino-pwa`, etc.).
- Pushes to `origin/main` without operator review of each commit.

The contract scopes to these scripts. Interactive scripts (`ticket.py`,
debugging tools) are out of scope — those run only at human invocation.

## Procedure

### 1. The flag set

Every auto-commit cron script accepts at least these mode flags:

| Flag | Behavior |
|---|---|
| `--commit-prod` | Real run: do all the work, commit, push to prod. |
| `--dry-run` | Report-only: no side effects, just print what would happen. |
| Tmp-target flag (per script) | E.g., `--ledger-dir <path>` or `--repo-root <path>`: redirect output to a tmp dir; safe for smoke. |

### 2. The default refuses

If invoked with NO mode flag (or with multiple conflicting modes), the script
**refuses with `exit 64`** and a clear error message listing the accepted
modes. No silent fallback. No "default to safe" — explicit *refusal* is louder
than silent no-op and prevents accidental misuse on the cron timer side
(missing flag in the systemd unit fails loudly in the journal instead of
silently never committing).

Reference implementation: `tools/session_reset_reconcile.py` →
`_enforce_default_safe()` returns `64` when neither `--commit-prod` nor a
safe-mode flag is set.

**Invariant (no-prod-default).** Absence of a mode flag MUST resolve to
*refusal* — never to a prod-writing default. The only path to a prod write is an
explicit `--commit-prod`; there is no implicit, inferred, or fallback prod mode.
A script that, when un-flagged, does anything other than refuse (or select a
clearly safe read-only mode) violates the contract — even if its current callers
all happen to pass the flag. The guarantee must hold at the *script* boundary,
not rely on every unit getting the flag right.

### 3. The systemd unit passes `--commit-prod` explicitly

```
ExecStart=/usr/bin/python3 %h/pantheon-config/tools/foo.py --commit-prod
```

Operator-facing: NEVER write a systemd unit for an auto-commit script
without the `--commit-prod` flag in `ExecStart`. The flag is the explicit
authorization the script's contract requires.

### 4. Smoke discipline

Smoke runs at the CLI ALWAYS use a tmp target or `--dry-run`:

```
python3 tools/foo.py --dry-run                    # show what would land
python3 tools/foo.py --repo-root /tmp/test-repo   # real work, tmp target
python3 tools/foo.py --commit-prod                # REFUSED in interactive contexts;
                                                  # reserved for systemd cron
```

If smoke MUST exercise the commit + push path, point at a tmp git repo via
the script's tmp-target flag. Never aim `--commit-prod` at the real repo from
a terminal.

### 5. Integration tests use `tools/_test_fixtures.py`

The shared test scaffold provides:
- `setup_integration_env()` — tmp git repo + bare origin + tmp state dir
- `run_script(name, args, env=...)` — invoke real subprocess against the env
- `snapshot_real_repo()` + `assert_real_repo_untouched()` — guard against
  the test accidentally committing to the real pantheon-config

Every auto-commit cron script SHOULD have:
1. A test that runs the script with no flags and asserts `rc=64`.
2. A test that runs with `--dry-run` and asserts no side effects.
3. A test that runs with the tmp-target flag + asserts work lands in tmp.
4. The test class calls `assert_real_repo_untouched` in `tearDown`.

Reference: `tools/test_session_reset_reconcile.py` →
`TestDefaultSafeContract` + `TestLayer3Integration`.

## Common failure modes

- **Forgot to add `--commit-prod` to a new systemd unit.** Symptom: cron fires,
  journal shows "ERROR: refusing to run without explicit mode", no commits.
  Fix: add `--commit-prod` to the unit's `ExecStart`, daemon-reload, restart.
- **Smoke run with `--commit-prod` accidentally lands in prod.** This is what
  the contract prevents — if you typed `--commit-prod` at the terminal, that
  was an explicit decision. The contract does not prevent intentional misuse;
  it prevents *accidental* misuse via missing-flag-defaults.
- **Test calls `run_script("foo", ["--commit-prod"])` and pollutes prod.** The
  integration-test fixture's `assert_real_repo_untouched` catches this in
  tearDown. Always use the fixture for integration tests; if you need
  `--commit-prod` in a test, pair it with `--repo-root <tmp>` so the commit
  lands in tmp.

## Boundary

- This SOP covers auto-commit *cron* scripts. Interactive scripts like
  `ticket.py` (which commit on operator invocation) are out of scope — their
  authorization model is "operator typed the command" rather than "systemd
  fired the timer".
- This SOP does NOT mandate identical flag names across scripts (some scripts
  already had `--dry-run`; the contract is the *semantics*, not the *spelling*).
- **Known exception — `tools/pantheon_config_sync.py`.** It pushes to
  `origin/main` from a timer-class context but is exempt from the
  `--commit-prod` gate because it *authors no commits*: there is no `git add`
  step — it only `git push`es already-committed local-ahead state and
  `git pull --ff-only`s remote-ahead state (the T-0471 worktree opt-out
  documents the same no-`git add` property). The contract's risk model (an
  unreviewed *new* commit landing on a timer) therefore does not apply. Its
  60s timer was additionally retired (T-0365 → pull-on-demand at reader), so it
  is no longer a live cron. Documented here so it does not read as an
  unaccounted-for contract violation.
- **Non-main branches.** The `--commit-prod` gate governs any auto-commit push
  to a *shared* ref — not only `origin/main`. Pushing to `origin/main` is the
  canonical guarded case, but a cron that auto-commits to a non-main shared
  branch (a deploy branch, a report branch, an `agent/<task>` branch it also
  pushes) is equally in scope and equally requires `--commit-prod`: the gated
  unit is *the push of a fresh auto-authored commit to a shared ref*, not the
  branch name. A purely local branch that never reaches a shared remote isn't
  the risk this contract addresses; the contract engages the moment the cron
  pushes a new commit anywhere another reader/agent will pull it.
- The "exit 64" convention follows the BSD `EX_USAGE` precedent — non-zero,
  non-conflicting with common scripting exit codes.

## Cross-references

- T-0259 — the originating ticket (Layer-3 integration scaffold + default-safe
  retrofit).
- T-0825 — the operator's in-place ratification of this SOP (2026-06-10). T-1073 —
  the scope-wording tighten follow-up (no-prod-default invariant + non-main
  branches). T-1072 — the open follow-up to make the contract structurally
  enforced (a `ticket_lint` scope-level check).
- `tools/_test_fixtures.py` — the integration-test scaffold.
- `tools/session_reset_reconcile.py` — the reference implementation.
- `rulebook/policies/trust-posture.md` — auto-approve scope context.
- `notes/2026-05-12-staging-environment-deferred.md` — the layered testing
  approach this SOP is the Layer-3 building block of.

---

**Status:** Ratified 2026-06-10 by the operator (via Telegram). Drafted 2026-05-12
by Recovery per T-0259.
