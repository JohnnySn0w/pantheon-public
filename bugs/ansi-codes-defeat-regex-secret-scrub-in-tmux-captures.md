---
symptom: "Raw tmux capture-pane output interleaves ANSI cursor/control sequences inside tokens (e.g. sshpass -p 'PW' becomes sshpass<ESC>[16G-p<ESC>[19G'PW'), so line/token-based regex secret-scrubbing never matches and a secret-scan reports clean while the literal secret survives in the archive."
fix: "Strip ANSI/control sequences before pattern matching, add a literal-string pass for any known secret in addition to the broadened pattern pass, and verify by searching for the literal secret post-scrub rather than trusting a 'scrub ran green' result."
domain: pantheon, tmux, ansi, regex, secret-scrubbing, security, pty, chronicle, graveyard
---

# ANSI cursor codes defeat regex secret-scrubbing in tmux pane captures

**Surfaced:** 2026-07-11, T-1683 (graveyard pane-log credential scrub, Recovery). First-cut
scrub reported success while the client password was still present in 3 archives.

## The gotcha

Raw tmux `capture-pane` output interleaves ANSI cursor/control sequences INSIDE tokens:
`sshpass -p 'PW'` was persisted as `sshpass<ESC>[16G-p<ESC>[19G'PW'`. Any line-based or
token-based regex (`sshpass -p\s+'\S+'`) never matches — the secret survives while the
scrub reports clean. Applies to ANY secret-scanning/sanitization over PTY-derived logs
(tmux captures, script(1) output, CI console dumps), not just this tool.

## The rules

1. **Strip ANSI/control sequences BEFORE pattern matching**, always. (Side benefit:
   readable archives.)
2. **Pattern scrubs of a KNOWN secret get a literal-string pass too** (`--also-literal`
   in tools/graveyard scrub) — the literal pass caught a 5th archive the broadened
   pattern pass still called clean.
3. **Verify by searching for the literal secret post-scrub** — "scrub ran green" is not
   "secret gone" (feedback_verify_negative_claims_from_logs applies: know the
   success-path signature).
4. Enumerate the *actual* forms in the scrollback (here: `echo PW | sudo -S`, bare-pw
   lines) rather than only canonical `tool -p PW` forms.

## Fix landed

`0ebbd3c0` / `250ee705` / `2bcac5b9` (T-1683): write-time scrub is ANSI-aware; retro tool
takes `--also-literal`; retro sweep verified 0 occurrences across all archives.

Prevention upstream: keep secrets off argv entirely (`sshpass -f` / `-e`), then there's
nothing to scrub — the scrub is defense-in-depth, not the fix.

## Keywords

tmux, capture-pane, ANSI escape codes, ESC[, cursor control sequences, regex, secret
scrub, secret-scrubbing, sshpass, credential leak, PTY, script(1), CI console dumps,
literal-string pass, --also-literal, graveyard, T-1683, false-clean scan,
secret-scanning, pattern matching defeat, sanitization, argv secrets, sshpass -f
