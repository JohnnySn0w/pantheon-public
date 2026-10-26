---
symptom: "A per-seat cross-account fleet tool that prepends `sudo -u <seat_account>` uniformly for EVERY seat fails on the ONE seat whose account == the tool runner's own account: sudo emits 'Sorry, user X is not allowed to execute ... as X'. The tool then fails-null/false-negatives for the runner's own seat (e.g. a currency/health/write-activity probe reads UNKNOWN or 'no activity' for the runner's own clone) while working fine for every other seat."
fix: "Special-case the runner's own seat: when seat_account == getpass.getuser(), run the command DIRECTLY (no sudo prefix) — you don't need sudo to act as yourself. Every OTHER seat goes through sudo as before. Python: adopt `from seat_exec import seat_argv; argv = seat_argv(seat.account, base)` (tools/seat_exec.py, T-4066). Shell: `if [ \"$(whoami)\" = \"$acct\" ]; then cmd; else sudo -n -u \"$acct\" cmd; fi`. Enforced at commit time by tools/self_sudo_guard_lint.py (.py + .sh)."
domain: pantheon, sudo, cross-account, fleet-tooling, monitor, self-seat, false-null, recovery
---

# `sudo -u <acct>` to the runner's OWN account fails — per-seat cross-account tools false-null on their own seat

**First observed:** 2026-08-03 (`delivery_reachability.py` fabricated a MISSING verdict for recovery's own timer — a seat whose fire had been watched an hour earlier). **Recurred:** 2026-09-14 (T-3709 `agent_committed_recently` smoke, run as `claude-fallback`); **2026-09-20** (T-3844 `cap_mute_driver.py` `read_breaker_open` — the recovery seat's own breaker read `None`/unknown instead of `False`/not-capped, so recovery would never be cap-muted; caught by the **supervised first-fire pre-check**, which showed recovery=None while every peer read False); **2026-09-26** (T-4009 `pantheon_metrics_gen.py` `_jsonl_fresh` — run as `claude-fallback` on the recovery row, self-sudo emptied stdout → `float('')` → False → the recovery tile on the personal-site fleet panel ALWAYS read `idle`, never `active`, even while recovery was mid-arc; fixed with the `_seat_argv` own-account-direct helper, verified live: recovery → `active`). FOUR instances across four unrelated per-seat tools — the class keeps re-biting fresh per-seat tooling whose authors didn't grep `bugs/` first, which is exactly why it belongs here. A recurring class, not a one-off.

**Symptom.** A fleet tool that iterates the seats and shells `sudo -nu <seat_account> <cmd>` (or `sudo -S -k -u <acct>`) for each works for the peer seats but FAILS for the seat whose unix account == the account the tool is running under. sudo prints:

```
Sorry, user claude-fallback is not allowed to execute '/usr/bin/git -C ... log ...' as claude-fallback on <host>.
```

The tool's per-seat probe then returns its failure sentinel for that one seat — a **false-null**: UNKNOWN currency, "no recent write", "cannot verify", etc. — even though the seat is perfectly healthy. Looks like an auth/permission problem specific to that seat; it is not.

## Cause

Recovery's (and each agent's) NOPASSWD sudo is scoped **as OTHER agent-users only** — sudoers has no rule permitting `<acct> → <acct>` (sudo-to-self), because acting as yourself needs no sudo. So a tool that blindly prepends the sudo factory for **all** seats hits an unconfigured sudo path on its own seat. The `-n` (non-interactive) flag turns the missing rule straight into a failure rather than a password prompt.

**Why it silently false-NULLs (the part that actually bites):** the denial (`Sorry, user X is not allowed …`) goes to **stderr**, the command exits **non-zero**, and **stdout is EMPTY** — byte-identical to a successful probe that legitimately found nothing. A tool that keys its verdict off `stdout` alone (empty ⇒ "not found / no activity / MISSING") reports a fabricated negative for the runner's own seat. Two rules: (1) **skip sudo when the target account is our own**; (2) **never treat empty stdout as evidence of absence — only `rc == 0` with empty stdout means absent; non-zero rc with EMPTY stdout is unaskable** (an instance of `feedback_unknown_must_not_stand_in_for_no`). Do NOT over-correct to "discard stdout on any non-zero rc": some probes print a valid answer AND exit non-zero — `du -sh` prints the total with rc=1 on an unreadable subdir (discarding it was the T-3441 miss), `systemctl is-active` prints `inactive` with a non-zero rc. The rule is about the EMPTY case. The 2026-08-03 `delivery_reachability.py` case was alarm-biased (invented a MISSING) — the "better" direction and still wrong: a monitor that cries wolf gets ignored.

Cross-refs: the "Recovery's NOPASSWD is as-agent-users-only" note in `bugs/sudo-S-heredoc-stdin-collision-false-auth-failure.md`; memory `reference_recovery_sudo_scope_all_pantheon_accounts` (thorough recovery-private write-up — read "all pantheon accounts" as "all OTHER pantheon accounts, not self"; this bugs/ entry is its fleet-shared, grep-able form for any agent's tool, not just recovery's).

## Surgical fix

Special-case the runner's own seat — run the command directly, no sudo. In Python, use the shared helper (T-4066), which does exactly this in one place:

```python
from seat_exec import seat_argv          # tools/seat_exec.py
argv = seat_argv(seat.account, base_cmd)  # base_cmd if own account, else sudo -n -u <acct> + base_cmd
result = subprocess.run(argv, ...)        # sudo= overrides the prefix, e.g. ("sudo", "-S", "-k", "-u")
```

Adopters: `pantheon_metrics_gen`, `fleet_clone_currency_check`, `parity_remediate`, `doc_traversal_report`, `session_reset_reconcile`. `tools/cap_mute_driver.py` / `cap_modal_reaper.py` keep a hand-rolled guard on purpose — their self branch is a direct file read (`if account == getpass.getuser(): open(...)`), which seat_argv does not model. In shell, follow `parity_render`'s generated `run_as` (`[ "$(whoami)" = "$account" ]` → direct, else `sudo -n -u`). Any NEW per-seat cross-account tool must do the same or it will silently mis-read its own seat.

## Enforcement (T-4032 / T-4058 / T-4066)

`tools/self_sudo_guard_lint.py` runs in the pre-commit gate over staged `tools/**/*.py` (AST) and `tools/**/*.sh` (quote/heredoc-masked regex), and refuses a per-seat `sudo -u <var>` in a file that never reaches for an own-account primitive. Rule (2) is deliberately NOT gated: of the 9 cross-account probes that read stdout without an rc check (T-4066 survey), 6 were correct by design — fail-closed/None-on-empty, or the rc-agnostic du/systemctl shapes above — so a "must check rc" lint would flag correct code and push authors toward the T-3441 regression.

## Verification

Reproduce (run as any agent account, targeting its own account):

```
sudo -nu "$(whoami)" true ; echo rc=$?
# → "Sorry, user <you> is not allowed to execute '/usr/bin/true' as <you> ..."
```

The direct form works: `true; echo rc=$?` → `rc=0`. And cross-account still works: `sudo -nu claude true; echo rc=$?` → `rc=0` (from recovery). So a per-seat tool reads the runner's seat correctly ONLY if it skips sudo for that seat.

## Scope note (T-3709)

`activity_monitor.py`'s `agent_committed_recently` (T-3709) does NOT special-case self — but it is safe today because `AGENT_CONFIG` covers orchestrator/artisan/mason only (never recovery, the runner). Its fail-CLOSED contract means that even if a `recovery` self-seat were added, the self-sudo failure would degrade to "no recent write" → the P1.b poke still fires (current behavior), never a false silence. If recovery self-monitoring is ever added there, apply the own-account-direct pattern above so the write-activity gate actually evaluates.
