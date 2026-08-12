# Harness event fixtures

These fixtures pin only fields used by the adapter parsers. They are derived
from the first-party structured-output examples named below and intentionally
exclude prompts, credentials, and user content.

- `codex/success.jsonl`: Codex CLI `exec --json` event vocabulary, verified
  against the installed CLI help and an ephemeral fixed-output capture on
  2026-07-30.
- `claude/success.jsonl`: Claude Code headless `stream-json` result vocabulary,
  refreshed from the first-party headless-mode documentation on 2026-07-29.

The fixtures are contract examples, not claims that the two native event
streams are identical.
