---
symptom: "After a test suite runs, `tmux list-buffers` on a live agent's server shows `send_keys_submit: 1132 bytes: You have 1 unread inter-agent message...`: a wake prompt nobody sent, left in the buffer name real wakes use"
fix: "A test that drives the REAL send_keys_submit.sh (or mw.inject) must never reach the ambient tmux server: unset TMUX, point TMUX_TMPDIR at a private short /tmp dir, start a throwaway server there with `tmux -f /dev/null new-session -d`, and kill-server on cleanup"
domain: pantheon, tmux, tests, send_keys_submit, mailbox_wake, isolation
---

# A real-tmux test leaves a send_keys_submit buffer on the live server

**Surfaced:** 2026-09-29, T-4320 (rev-recovery-batch's note on T-4313; reproduced by Recovery).

## The gotcha

`tools/send_keys_submit.sh` delivers text with `load-buffer -b send_keys_submit` and then
`paste-buffer -d -b send_keys_submit -t <pane>`. When the paste fails (a dead target is the
whole point of some tests), `-d` never runs and the buffer stays on whatever server the
client reached. A test run from inside an agent's tmux pane inherits `TMUX`, so that server is
the agent's LIVE one. The buffer name is shared with every real wake on that server, so the
residue is a cross-talk hazard: it holds a real-looking wake prompt.

Nothing fails. The test is green, and the residue only shows in `tmux list-buffers`.

## The fix

**Structural, since T-4433:** the `tools/conftest.py` autouse fixture `_isolate_tmux_server` gives every pytest test a private `TMUX_TMPDIR` and unsets `TMUX`/`TMUX_PANE`, so a test can no longer reach the live server by default. `tools/test_conftest_tmux_isolation.py` pins this. The general rule is in `sop/standard/testing-depth-and-scope.md` §Isolation discipline item 4 (T-4564). The recipe below is still the per-test method for anything outside that fixture's reach: a test run outside pytest, or `sudo -u <other> tmux …`, which resets the environment.

Isolate the tmux server, don't rely on cleanup:

```python
tmp = tempfile.mkdtemp(prefix="t4320", dir="/tmp")  # short: socket paths are length-limited
self.addCleanup(shutil.rmtree, tmp, True)
env = {k: v for k, v in os.environ.items() if k != "TMUX"}
env["TMUX_TMPDIR"] = tmp
subprocess.run(["tmux", "-f", "/dev/null", "new-session", "-d", "-s", "throwaway"], env=env, check=True)
self.addCleanup(subprocess.run, ["tmux", "kill-server"], env=env, capture_output=True)
p = mock.patch.dict(os.environ, env, clear=True); p.start(); self.addCleanup(p.stop)
```

- `-f /dev/null`: the user's tmux.conf can carry hooks (pane logging) you don't want firing.
- Keep the throwaway server if the test needs tmux's `can't find session` (session_gone) form.
  With no server at all you get the no-server forms instead (see T-4313's
  `_DEAD_TARGET_FORMS` in tools/test_mailbox_wake.py).
- Skip, not fail, if the private server can't start: that measures the box, not the code.

Reference implementation: `WakeTest.test_real_inject_into_a_dead_seat_fails_and_leaves_the_mail_unread`.

## Verification

From an agent's tmux pane, on a commit before T-4320:
`tmux list-buffers` (empty), then
`python3 -m unittest tools.test_mailbox_wake.WakeTest.test_real_inject_into_a_dead_seat_fails_and_leaves_the_mail_unread`,
then `tmux list-buffers` shows `send_keys_submit: 1132 bytes: "You have 1 unread inter-agent message...`.
After T-4320, the same sequence leaves it empty. Clear any residue with `tmux delete-buffer -b send_keys_submit`.
