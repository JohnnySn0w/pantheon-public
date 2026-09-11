---
symptom: "The Claude quota-failover swap silently never fires when a seat is capped — the seat sits stuck at 100% instead of swapping to its codex fallback. No error; the probe just returns status 'unknown' and decide_failover takes no action (fail-safe to no-swap). Root cause is upstream: Anthropic changed the wording of the capped-state TUI banner, so the probe's regex no longer matches."
fix: "The cap-detection in tools/claude_quota_probe.py is a UI-SCRAPE: it launches a throwaway Claude TUI and matches CAP_SENTINELS against the rendered pane — currently the literal banner `you've hit your (session|weekly|opus) limit · resets <time>`. It does NOT call a usage API. If Claude's banner text drifts, update the CAP_SENTINELS regex to match the new wording. Verify against a REAL cap (or a captured capped-pane fixture), never assume the banner is stable. Add a monitor that alerts on banner-drift (a probe returning 'unknown' at a time a cap is expected, or a periodic fixture-match check)."
domain: "quota, failover, codex-swap, claude-quota-probe, ui-scrape, banner-drift, silent-failure, consume-control-plane, pantheon"
---

# The quota-probe cap-detection is a UI-scrape of Claude's exact banner text — banner drift silently breaks failover

**Class:** silent-failure via upstream UI dependency. The failover fires on a *scraped string*, not an API contract, so an Anthropic wording change disables it with no error.

## Mechanism (verified at source 2026-08-12)

`tools/claude_quota_probe.py::run_probe` reads a seat's quota by launching a temporary Claude TUI in tmux and reading what it *renders* — the statusline JSON (`rate_limits`) and the pane text. Cap is detected two ways:

1. **Fresh structured quota** — the statusline's `rate_limits` payload with a remaining %.
2. **The cap sentinel** — `_cap_sentinel()` matches `CAP_SENTINELS` against the pane. The one pattern (as of this writing) is the literal banner:
   `you've hit your (session|weekly|opus) limit · resets <time>`

When a seat is fully capped, Claude displays that banner, the sentinel matches, `run_probe` returns `status="capped"`, and `runtime_controller.forced_swap` fires. **So the probe DOES work at 100% — being capped is precisely the signal it reads** (not a blindspot, which is the intuitive worry).

## The real fragility

Because cap-detection is a **scrape of Claude's exact banner wording**, three failure modes silently disable failover:

1. **Banner drift.** Anthropic changes the wording ("reached" vs "hit", punctuation, structure) → the regex misses → probe times out → `status="unknown"` → `decide_failover` returns "unknown" → **no swap**. The seat stays capped and stuck.
2. **TUI won't launch when capped.** If a fully-capped account can't render the TUI far enough to show the banner, same "unknown", no swap.
3. **Fail-safe is fail-to-no-action.** On any unreadable state the design does NOT swap (correct — never swap on a blind guess), so a probe failure and a healthy-not-capped seat look the same to the controller.

## What to do

- Treat `CAP_SENTINELS` as a live dependency on Claude's UI, not a constant. Re-verify against a real cap or a captured capped-pane fixture.
- Monitor for banner-drift: alert if the probe returns "unknown" when a cap is otherwise expected, or run a periodic fixture-match self-check.
- This is the strongest argument for validating the cap→swap end-to-end against a real cap (the T-2574 rehearsal), rather than trusting the mechanism blind.

## Verification

`grep -n 'CAP_SENTINELS' -A6 tools/claude_quota_probe.py` shows the current banner regex. Compare it against what Claude actually displays on a capped account today; if they diverge, failover is silently broken. Observed as a design fragility during the T-2927 codex-failover-readiness work (the operator's question: "does the hook work if usage is effectively 100%?").
