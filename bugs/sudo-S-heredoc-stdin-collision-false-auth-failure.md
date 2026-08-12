---
symptom: "Piping a password into 'sudo -S' while the same command carries a heredoc fails with 'Sorry, try again' three times even when the password is correct"
fix: "The heredoc replaces the pipe as sudo's stdin so script lines are read as password attempts - never combine sudo -S with a heredoc; pass the script via file and the password via the pipe"
domain: bash, sudo, stdin, false-alarm
---
# `pipe | sudo -S cmd <<heredoc` — heredoc steals stdin, sudo reads script lines as passwords

**Symptom.** `cut/tr/cat <pw-file> | sudo -S bash -s <<'EOF' ... EOF` fails with `Sorry, try again` ×3 / `3 incorrect password attempts` even when the password is correct. Looks exactly like a rotated/broken password. Cost: a full false-alarm arc 2026-06-11/12 (ledger -181/-182/-183) — "broken password / no autonomous root path" narrative, an operator-lane ticket, two correction digests — when the password was valid the whole time.

**Mechanism.** The `<<heredoc` redirection on the command REPLACES the pipe as sudo's stdin. `sudo -S` then reads the heredoc's first lines (`set -euo pipefail`, …) as password attempts. The pipe's content is never seen. Each invocation burns 3 failed attempts → faillock exposure on retries.

**Surgical fix.** Never combine `sudo -S` with a heredoc/stdin-redirect on the same command. Either:

```
# script via file, password via pipe (stdin stays the pipe)
tr -d '\n' < /home/claude/root.env | sudo -S -k bash /tmp/script.sh

# or script as argument
tr -d '\n' < /home/claude/root.env | sudo -S -k bash -c '<commands>'
```

**Diagnostic rule (the general half).** When sudo rejects a password you believe is right: STOP after one batch (faillock), then run the isolating test — `tr -d '\n' < <pw-file> | sudo -S -k true` with nothing else on the command line. If THAT fails, the password is wrong; if it succeeds, your plumbing was eating stdin. Per `feedback_isolating_test_before_root_cause_claim`: run the falsifying test BEFORE building the rotation/breakage narrative.

**Cross-refs.** Ledger 2026-W24 -181 (false alarm) / -183 (correction); T-1101; memory pin `reference_sudo_password_location.md`. True findings that survived the false alarm: Recovery's NOPASSWD is as-agent-users-only (prime's password is the org's only autonomous root path); `/etc/claude-recovery` known-goods need re-baselining whenever settings evolve.
