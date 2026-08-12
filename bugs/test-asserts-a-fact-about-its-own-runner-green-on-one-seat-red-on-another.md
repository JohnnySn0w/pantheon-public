---
symptom: "A test passes on one agent's seat and fails on another at the SAME commit, with a failure that looks like a type bug rather than an environment bug — classically `TypeError: object supporting the buffer API required` from a MagicMock reaching a function that expected a str. Root cause: the code under test branches on the RUNNING identity (`user == getpass.getuser()`, `os.geteuid()`, `Path.home()`, `$USER`, hostname, `pwd.getpwuid`), and the test hardcodes a value that is 'the other account' on one seat and 'me' on another. The test then silently exercises the OPPOSITE branch from the one it names, where a mock configured for the intended branch is the wrong shape. The authoritative smoke goes permanently red on main for whichever seat is unlucky."
fix: "PIN THE IDENTITY IN THE TEST, and pin it in EVERY test in the class, not only the failing one: `with patch.object(<module>.getpass, 'getuser', return_value='some-fixed-name')`. The assertion must be about the ROUTING RULE, never about the account that invoked pytest. Then add the arm the hardcoding hid — typically 'a config that NAMES an identity which IS the running one', which is a real production configuration and is exactly the branch nobody wrote a test for. Verify by RUNNING THE SUITE AS THE OTHER ACCOUNT (`sudo -u <other> bash -c 'cd <worktree> && python3 -m unittest <module>'`; other seats may lack pytest), because single-seat verification is what missed it in the first place and cannot confirm the fix either. Falsify: weaken the identity comparison in the source and confirm the NEW test fails and only it."
domain: "pytest, unittest, mock, getpass, getuser, geteuid, host-coupling, seat-specific, multi-account, smoke, pantheon"
---

# A test that asserts a fact about its own runner: green on one seat, red on another

**Class:** test validity. The production code is correct; the test encodes an environmental accident
as if it were a fixture.

**Found:** 2026-08-03 (T-2445, `tools/test_tui_wedge_sampler.py::SelfCaptureAvoidsSudo`). Passed as
`claude-fallback`, failed as `claude`, same sha.

## The shape

```python
# source
is_self = not user or user == getpass.getuser()   # ← branches on WHO IS RUNNING

# test
cfg = {"sudo_user": "claude", ...}                 # ← "the foreign account"… on recovery's seat
with patch.object(tws, "_capture_self") as direct: # bare Mock: the branch we expect NOT to take
    rec = tws.sample_agent("orchestrator", cfg)
direct.assert_not_called()
```

Run it as `recovery` and `"claude"` is foreign, the foreign branch runs, the test means what it says. Run
it as `claude` and `"claude"` is *self* — the self branch runs, `_capture_self` returns a bare
MagicMock, and the next function `.encode()`s it. The traceback lands in a hashing helper several
frames away from anything that looks identity-related.

## Why it is worse than an ordinary flaky test

**It does not fail like a broken test. It fails like a working test that someone else broke.** And
the cheapest, most natural diagnostic move — re-run it somewhere it passes, or ask a teammate whether
they see it — *confirms the wrong conclusion*. Host-coupling is the one defect class where "it works
on my machine" is not a dismissal but the actual symptom, and it will be offered sincerely by whoever
holds the lucky seat.

The traceback also actively misdirects. `TypeError: object supporting the buffer API required` invites
a None-guard or a type-cast at the crash site. That "fix" makes the symptom vanish while leaving a
test that still asserts a fact about its runner — and now with a guard that quietly swallows a real
misroute in production.

## The second cost, which is the larger one

A permanently-red authoritative smoke is not one broken test; it is a broken instrument. Once
`5195 passed, 1 failed` is the familiar number, reading it stops being reading — the next regression
arrives as `5196 passed, 1 failed` and is indistinguishable at a glance. The red was ambient for long
enough to be normal, and that normalization is the damage. Fix it or quarantine it deliberately with
a stated reason and an expiry; never leave it as background colour.

## Smell test

Grep the module under test for `getuser`, `geteuid`, `getlogin`, `gethostname`, `Path.home()`,
`os.environ["USER"]`, `expanduser`. Any of them in a branch condition means every test touching that
branch must pin the value — and the suite's green belongs to the seat that ran it until it has been
run as a second account.

## Related

- Memory pin `feedback_test_green_is_seat_specific` — the behavioral rule this instance instantiates
  (16/0 as recovery, 9/7 as orchestrator, same sha).
- `bugs/sudo-S-prompt-concatenates-and-eats-the-first-output-line.md` — cross-account execution
  gotcha you will meet while running the suite as the other seat.
