# mini-SWE-agent as a simplicity discipline-anchor for Pantheon tools

**Date authored:** 2026-05-19 (T-0386 reflection per the operator msg_1469+1470 + Specialist's 2026-05-17 3rd-party-harnesses survey §4.3).
**Author:** Orchestrator (HO).
**Status:** Drafted; un-ratified. Audit-prompt anchor — used at tool-design + tool-review time as a calibration question, not as a hard rule.
**Re-evaluate when:** a Pantheon-tools complexity-creep argument surfaces and someone reaches for this note as the anchor (a 2nd citation promotes "useful in theory" → "load-bearing in practice"); or when mini-SWE-agent itself materially evolves past its current ~100-LoC bash-only shape.

## The reference

[mini-SWE-agent v2](https://github.com/SWE-agent/mini-swe-agent) — Princeton+Stanford academic project. Released March 2026, 4.4k stars, MIT-licensed. Core agent loop at `src/minisweagent/agents/default.py`: **155 lines total, 137 lines of code**. Bash-only execution model (no tool-calling API). **74%+ on SWE-bench Verified with Gemini 3 Pro.** Enterprise adoption: Meta, NVIDIA, Essential AI, IBM, Nebius, Anyscale.

What makes the 137-LoC core work:

- **Pluggable interfaces**: `Environment` (bash exec), `Model` (LLM client) are injected. Core loop is agent-shape-only.
- **One control flow**: `run()` is a `while True` over `step()` until a message has `role == "exit"`. `step()` = `query() ◦ execute_actions()`. That's it.
- **Pydantic config + Jinja2 templates**: configuration surface is type-safe + reusable.
- **JSON trajectory persistence**: serialize/save methods preserve full conversation + metadata.

The "100 lines of bash-only Python at 74% SWE-bench" datapoint (Specialist's framing) is the hard reference: a serious autonomous-coding-agent loop fits on roughly one screen.

## The comparison — and the category-mismatch caveat

Pantheon's substrate tools (sizes today):

| Tool | LoC | Primary responsibilities |
|---|---|---|
| `tools/ticket.py` | 1478 | 8-subcommand lifecycle CLI (file/claim/block/resolve/retarget/list/...), frontmatter parser, README rebuilder, transition log appender, auto-commit pipeline with HERMES guard + scope guard + pull-first + push-with-retry. |
| `tools/inbox_consumer.py` | 1345 | Queue walk + filter (target/depends_on/not_before/interactive-required), dispatch heuristic, cross-account tmux inject (sudo wrapper + paste-mode delay), sentinel detection, recovery-pane alert routing, skip-reason vocabulary. |
| `tools/watch_runner.py` | 1190 | Cron-based topic dispatch + per-topic cadence, researcher-agent invocation, auto-commit + auto-push with HERMES guard, retry/back-off. |
| `tools/ticket_grooming.py` | 899 | Daily cron staleness scanner, blocked-revisit digest (now retired post-ORG-019), retarget logic, grooming digest send. |
| **total** | **~4900** | 4 tools, each ~9-13× mini-SWE-agent's core size; combined ~36× the reference. |

**Category-mismatch caveat (load-bearing):** these tools are NOT Pantheon's agent. The agent is **HO** — orchestrator running `claude` in its own loop, with Anthropic's Claude Code as the substrate. The tools are *infrastructure that HO operates on*. The right counterpart of mini-SWE-agent's 137-LoC core in Pantheon is Claude Code itself (Anthropic's loop, not in this repo). Comparing Pantheon-substrate-tools' line counts to mini-SWE-agent's agent-loop line count is apples to oranges.

So the anchor isn't "Pantheon's tools should fit on 100 lines." That comparison fails categorically.

## What the anchor IS for

The useful audit-prompt at tool-design / tool-review time:

> **"What is the per-tool core logic, separated from substrate plumbing (HERMES guard, scope guard, auto-commit, pull-first, push retry, cross-account sudo wrapper, Telegram send, ledger entry)? Does THAT fit on one screen?"**

Concrete examples applied to current tools (rough estimates from grepping):

- `ticket.py cmd_block` (post-ORG-019): the new frontmatter-edit logic is ~60 LoC; the surrounding `_commit_and_push` pipeline it routes through is ~150 LoC of substrate plumbing. Cmd_block itself fits on one screen. **Pass.**
- `inbox_consumer.py pick_ticket`: the eligibility filter logic is ~80 LoC; the surrounding cron-execute + sentinel-detect + Recovery-route plumbing is the rest. Pick logic fits on one screen. **Pass.**
- `watch_runner.py main`: the topic-dispatch logic is ~150 LoC; HERMES guard + auto-commit are ~200 LoC additional. Borderline — the per-topic dispatch is starting to bleed into the cron-shape. **Watch (no action).**
- `ticket_grooming.py main` (post-ORG-019): blocked-revisit retired, ~300 LoC of dead-weight scanned out. Current grooming-pass logic is ~200 LoC; auto-commit substrate is the rest. **Pass post-retirement.**

The anchor is the audit prompt itself, used at periodic review or when adding a new responsibility to an existing tool. Not a hard line-count cutoff.

## When the substrate plumbing IS load-bearing

Some Pantheon-tools complexity is genuinely required and would be reckless to optimize away:

- **HERMES byte-sequence guard** — anti-classifier risk per [knowledge/episodic/2026-04-29-anthropic-flagging-diy-harnesses/notes.md](../knowledge/episodic/2026-04-29-anthropic-flagging-diy-harnesses/notes.md). Every auto-commit path runs it. ~30-50 LoC per script; not optional.
- **Scope guard** at `_commit_and_push` — audit-chain integrity per P4 (T-0004 root cause was a single mis-routed `git add` letting cross-ticket bundling slip through). ~30 LoC; not optional.
- **Pull-first discipline** at write-paths — per T-0244, prevents divergent-state races between claude/recovery/specialist instances. ~20 LoC; not optional.
- **Cross-account sudo + paste-mode delay** for tmux send — per the inbox_consumer fix that landed today (commit aa78e42, msg_1593). The cross-account topology is the substrate; the wrapper is what makes it reliable. ~20 LoC; not optional.

Total non-negotiable substrate-plumbing per script: ~100-150 LoC. Anything above that is the script's own logic, where the audit prompt fires.

## How to invoke this anchor in practice

When considering a tool change (refactor, new feature, new responsibility), pause and ask:

1. **Is the new logic a NEW responsibility for this tool, or an extension of an existing one?** New responsibility → consider whether it belongs in a separate tool sharing the `_git_utils.py` substrate.
2. **After the change, does this tool's per-responsibility logic still fit on one screen with substrate plumbing factored out?** If no, factor or split.
3. **Is the new logic actually shape-distinct from existing logic, or am I duplicating?** Often the answer is "duplicate; share."
4. **Would mini-SWE-agent (or any radical-simplicity reference) make the same call here?** Usually yes if the answer to #1 is "extension"; usually no if "new responsibility."

The anchor doesn't say "rewrite Pantheon to be simpler." It says "before adding the N+1th responsibility to a tool, check if there's a simpler structural shape."

## Related disciplines

- [`stacking-mechanisms-may-mean-the-substrate-is-the-problem.md`](stacking-mechanisms-may-mean-the-substrate-is-the-problem.md) — sibling lesson: when you're adding multiple amendments to make a substrate work, the substrate may be the problem. T-0453 retirement of `tickets/blocked/` is the canonical case. This mini-SWE-agent anchor is the *forward-looking* form (catch before stacking); the substrate-stacking lesson is the *retrospective* form (catch after stacking has already happened).
- [`org-churn-is-constant-methodology-must-be-concise.md`](org-churn-is-constant-methodology-must-be-concise.md) — adjacent class on methodology-tier simplicity.
- ORG-012 §context-frugality (D10) — minimum-traversal-to-precise-info. Tools that exceed the audit prompt accumulate context cost for every reader.

## Cross-references

- [`knowledge/watch/reports/2026-05-17/3rd-party-harnesses-survey-delta.md`](../knowledge/watch/reports/2026-05-17/3rd-party-harnesses-survey-delta.md) §4.3 — Specialist's originating survey finding.
- [T-0386](../tickets/done/T-0386.md) — this lesson's authoring ticket.
- Telegram refs: msg_1469 (the operator's "above-the-bar finding lifted"), msg_1470 (file ticket).
- External: https://github.com/SWE-agent/mini-swe-agent — source.
