---
symptom: "A systemd user unit is written and installed from an agent worktree, `systemctl --user enable --now` succeeds, `list-timers` shows it armed — and the first real fire dies. The ExecStart path is `%h/<repo>/tools/<script>` (or any absolute path into the CANONICAL clone), so the timer executes MAIN's copy of the file, not the worktree's. If the change added a CLI flag, the failure is an argparse `unrecognized arguments` exit 2; if it added a module or function, it is an ImportError or AttributeError. Because monitoring units carry `OnFailure=pantheon-unit-failure-alert@%n.service`, the first casualty is a spurious alert about a monitor that is in fact fine — routed at whoever owns that unit's failure lane, who then triages a phantom."
fix: "ARM AFTER MERGE, never before — the two are ordered operations. Install and enable the timer only once the code its ExecStart points at is on main and the canonical clone has been fast-forwarded. If you have already armed it, `systemctl --user disable --now <timer>` immediately (that is the cheap direction), merge, `git -C ~/<repo> merge --ff-only origin/main`, re-enable, then `systemctl --user start <unit>` once by hand and check `systemctl --user show <unit> -p Result -p ExecMainStatus` plus the journal BEFORE trusting the timer. To verify the coupling rather than assume it: `systemctl --user cat <unit> | grep ExecStart` and confirm the path is the canonical clone — if it is, the worktree is irrelevant to what will actually run."
domain: "systemd, systemd-user, timer, worktree, agent-branch, ExecStart, OnFailure, deployment-ordering, pantheon"
---

# A systemd unit installed from a worktree runs MAIN's copy of the code

**Class:** deployment ordering. Not a systemd bug and not a git bug — a coupling between two things
that feel independent, in a workflow that deliberately keeps them separate.

**Found:** 2026-08-03 (T-2441, arming the CI-audit tip-coverage probe). Caught in the six-minute
window before the timer's first fire, so it cost nothing; it is written down because the near-miss
was luck, not process.

## The shape

The agent-branch convention puts work in `/tmp/<repo>-<ticket>` on `agent/<ticket>`. Unit files live
in the repo (`tools/systemd/*.service`), so the natural sequence while still on the branch is:

```bash
cp /tmp/<repo>-T-NNNN/tools/systemd/<unit>.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now <unit>.timer     # ← armed, and pointed at main
```

Everything reports success. `list-timers` shows the unit with a NEXT time. But the unit body says:

```ini
ExecStart=/usr/bin/python3 %h/<repo>/tools/<script>.py --mode <new-flag>
```

`%h/<repo>` is the **canonical clone**, not the worktree. The unit FILE came from the branch; the
CODE it invokes did not. Until the merge lands, `--mode <new-flag>` does not exist in that file.

## Why it is worse than an ordinary premature-deploy

**The alert fires at the wrong person about the wrong thing.** Monitoring units carry
`OnFailure=pantheon-unit-failure-alert@%n.service`, and a failure alert for a *monitor* reads as "the
monitor is broken" — which is the single most expensive false signal a monitoring change can emit,
because it discredits the instrument at the exact moment it is being introduced. The triaging agent
sees a red monitor, not a red deployment order.

It is also invisible to every check that feels like the right check. The unit file is correct. The
code is correct. The tests are green. `systemctl --user status` before the first fire says nothing is
wrong, because a `oneshot` unit that has never run has no result yet. The defect exists only in the
*relationship* between two artifacts, and nothing inspects relationships.

## The generalizable form

**Installing a unit does not deploy the code it runs.** Any absolute path in an `ExecStart` —
`%h/...`, `/home/...`, `/usr/local/bin/...` — resolves against the live filesystem at fire time, not
against the checkout you authored in. The same trap applies to anything else that references code by
absolute path from outside the worktree: cron entries, `.path` units, wrapper scripts in `~/.local/bin`,
and hook configs.

Corollary worth keeping: the fix direction is asymmetric. Disabling a timer is instant and harmless;
letting one fire against absent code costs a false alert and someone else's attention. When the
ordering is in doubt, disarm.

## Related

- `bugs/worker-preflight-passes-then-absolute-path-drift-to-main-checkout.md` — the same
  worktree/canonical-clone confusion, arriving through an absolute path in a worker's own commands.
- `bugs/a-worktree-holding-main-strands-the-canonical-clone-detached.md` — sibling worktree hazard.
