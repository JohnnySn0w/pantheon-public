# Per-agent cost-monitoring thresholds

**Status:** Ratified 2026-05-17 by the operator (T-0409 verdict 17:34Z) — Specialist row + catalog format both ratified. Orchestrator row was previously ratified via Telegram-verbatim per T-0010 (msg_412, 2026-05-03) before this catalog existed; reflected here for canonical reference. Recovery row ratified 2026-05-11 by the operator (msg_989 via T-0069, p99/172M — Orchestrator-consistency shape).

**Author:** Orchestrator (HO) — lifted from Specialist's research draft at `knowledge/watch/reports/2026-05-17/b2-monitoring-threshold-proposal.md` (commit f0aa3d1).

**Date authored:** 2026-05-17

**Related:** [[T-0010]] (Orchestrator threshold ratified), [[T-0071]] (Specialist threshold pending), [[T-0069]] (Recovery threshold pending), [[T-0368]] (HO promotion + refile that produced this catalog), `tools/usage_monitor.py` (the cron consumer of these values), [[ORG-015]] (ratifiable-class allow-list — this file's substrate choice).

**HITL:** Authored

## Context

Each long-lived Pantheon agent (Orchestrator / Specialist / Recovery) runs `tools/usage_monitor.py` on a `*/30 * * * *` cron, polling its own session transcripts + writing 1h/5h/24h/168h rolling token totals to `~/.local/state/pantheon/usage.jsonl`. The `--alert-tokens N --send-telegram` flags add anomaly/runaway alerting: cron emits a Telegram to the operator when the 5h-window exceeds N.

The threshold's role per the operator msg_396 (T-0010 close): **anomaly / runaway alarm aligned with the $10/mo extra-usage cap**, not p95 of typical work. Reframe was: with B8 (auto-resume on cap-hits) shipped, the threshold no longer needs to predict typical load — it just needs to surface when an agent is burning beyond the cap-hit budget.

Each agent calibrates from its own observed baseline (`usage_monitor.py --propose-threshold` walks the agent's own `usage.jsonl`). Thresholds differ across agents because workload distributions differ.

## Thresholds (catalog)

| Agent | Threshold | Percentile | Ratified-via | Baseline | Source |
|---|---|---|---|---|---|
| **Orchestrator** (HO) | **741,000,000** | p99 of non-zero 5h windows | T-1584 ratified by the operator 2026-07-06 (supersedes T-0010/170M) | 2511 snapshots at re-eval | Cron `--alert-tokens 741000000 --send-telegram` since 2026-07-07 |
| **Specialist** (science) | **136,200,000** | p99 of non-zero 5h windows (shifted from p95 at T-1584) | T-1584 ratified by the operator 2026-07-06 (supersedes T-0409/18.67M) | 1507 non-zero windows over ~57d at re-eval | Cron `--alert-tokens 136200000` since 2026-07-07 (Specialist self-actuated, T-1591 trail) |
| **Recovery** (fallback) | **308,000,000** | p99 of non-zero 5h windows | T-1584 ratified by the operator 2026-07-06 (supersedes T-0069/172M) | 2069 snapshots at re-eval (Recovery's own re-run) | Cron `--alert-tokens 308000000 --send-telegram` since 2026-07-07 (T-1485) |

### Fleet-wide alert kinds (T-1875, landed 2026-07-21 — PROPOSED tier, not yet operator-ratified)

Two new `usage_monitor` alert kinds shipped opt-in per PR #75. Numbers below are **PROPOSED** (implementer-recommended, HO-endorsed); the weekly-quota absolute is deliberately unset — Anthropic's real weekly cap is undisclosed, and no fabricated stand-in ships:

| Alert kind | Flag / env | PROPOSED value | Status |
|---|---|---|---|
| Weekly-quota pacing | `--weekly-pacing-pct` / `PANTHEON_WEEKLY_PACING_PCT` | **20.0** (live default — relative heuristic: last-24h burn rate would eat >20% of the remaining 7d budget ≈ exhaustion within ~5 days) | Shipped as default; ratify or amend at next threshold re-eval |
| Weekly-quota absolute | `--weekly-quota-tokens` / `PANTHEON_WEEKLY_QUOTA_TOKENS` | **none** — calibrate from `--propose-threshold`'s 7d percentiles once ≥14 non-zero 7d snapshots exist (explicitly a reference, not a suggestion) | Unset until the operator ratifies a number |
| Cache-health ratio | `--cache-ratio-threshold` / env equivalent | **0.5** recommended starting trip-wire (creation:read; approaching 1.0 = cache rewritten more than reused, the TTL-regression shape) over a 24h window | Opt-in only; no history for the new fields yet — revisit when percentiles populate |
| Hourly burn anomaly (T-1897, PR #81) | `--enable-anomaly-alert` + `--anomaly-sigma` / `--anomaly-min-samples` / `--anomaly-consecutive-hours` (env `PANTHEON_ANOMALY_*`) | **3.0 / 30 / 2** — sigma per the gap-analysis ask; n≥30 CLT floor; 2 consecutive complete hours filters single heavy-work spikes while catching sustained loop/injection burns | Alerting off by default (collection unconditional); calibration via `--propose-threshold` hourly block once ≥14 buckets; ratify or amend at next re-eval |

## Rationale per row

### Orchestrator — 170M (p99)

Per T-0010 close (msg_412): distribution from 178 snapshots showed `p50 29M / p90 112M / p95 137M / p99 170M`. The operator picked p99 for the anomaly-alarm framing — only fires on extreme outliers (Orchestrator burning flat-out for 5h+, the case where intervention matters). Re-evaluate trigger: 30 days post-B8 production cycles (≈ 2026-06-03; landed earlier but kept noted).

**Re-eval 2026-07-01 — RATIFIED 2026-07-06 by the operator (T-1584, Telegram msg 4664):** 2511 snapshots (vs 178 at baseline): `p50 75.05M / p90 264.47M / p95 395.40M / p99 740.94M`. Workload grew ~4x (subagent teams, cross-repo builds); the ratified 170M now sits ≈p85 — firing on ~15% of 5h windows, not the intended ~1%, and likely over-alerting the operator. **Ratified: 741M (p99)** — preserves the original extreme-outlier-only framing. Actuated 2026-07-07: orchestrator crontab `--alert-tokens 741000000`.


### Specialist — 18,665,902 (p95)

**Distribution (12-day baseline, 573 snapshots, 136 non-zero 5h windows):**

```
p50: 3.92M    p90: 7.77M    p95: 18.67M    p99: 18.67M
```

**Shape: bimodal.** ~90% of non-zero observations cluster in 0-8M (steady researcher dispatches); a tight cluster of 9 observations at 18,665,902 (~6.6%) at the saturation ceiling; middle band (8M-18M) nearly empty (4 obs total, 2.9%). The bimodal shape was clearer than the prior 2026-05-12 reading because sample size doubled (72 → 136 non-zero); p90 dropped from 18.67M to 7.77M as the middle band filled in, while p95 stayed at the saturation cluster.

**Why p95 over p90:** p90 = 7.77M would fire on routine moderate-use 5h windows (~10% of non-zero observations — ~1.5×/week; too frequent for an oncall-style alert). p95 = 18.67M fires only when Specialist hits the saturation ceiling (~0.5×/week expected). Consistent with Orchestrator's sparse-real-signal-only choice; the percentile differs across agents because workload distributions differ, but the framing (rare-anomaly alarm) is shared.

**Open caveat (Specialist flagged at ratify-time):** the 18,665,902 cluster is suspiciously clean — 9 observations at exactly the same value suggests a deterministic ceiling (transcript-scanner buffer or Anthropic-side cap) rather than a natural distribution peak. Doesn't change the recommendation (cap-as-ceiling is still the right alert trigger), but the interpretation may be "Specialist's 5h window has hit a scanner/upstream limit" rather than "Specialist is running flat-out." Worth one-time verification post-ratification; tracked as a forward observation, not a blocker.

**Re-eval 2026-07-01 — RATIFIED 2026-07-06 by the operator (T-1584, msg 4664; percentile shift p95→p99 included in the blanket ratify):** 1507 non-zero windows over ~57d; the saturation cluster dissolved (as the refuted-caveat note predicted), tail now fat (max 250M ≈3x p95). Ratified 18.67M sits ≈p60 — routine-fire, not anomaly-fire. **Ratified: 136.2M (p99)** — the p95→p99 shift restores ~1% fire-rate + fleet-consistent framing. Actuation: Specialist self-actuates crontab (T-0418 pattern, mailboxed 2026-07-07).

**Caveat REFUTED at post-actuation (T-0418 / T-0421, 2026-05-17 ~17:40Z, ~13h after ratify):** with ~13 hours additional data, the 18.67M "cluster" expanded — 28 observations in the 16M+ range with 7 distinct values up to 38,806,364 (over 2× the original supposed ceiling). The 18.67M flatness was an artifact of small-sample (136 non-zero obs at proposal time) right-tail; not a deterministic ceiling. **Practical implication:** alert rate will EXCEED the original ~0.5×/wk estimate. Framing shifts from "rare saturation alert" to "moderate-frequency heavy-use alert." Threshold value remains the Ratified 18,665,902 — re-proposal needs the operator's verdict; the value as-set is operationally useful (surfaces Specialist heavy-use periods) even if more-frequent-than-anticipated.

**Post-ratification action (Specialist auto-approve scope):** Specialist updates own `claude-research` crontab to add `--alert-tokens 18665902 --send-telegram` to the existing `usage_monitor.py` cron line. **ACTUATED 2026-05-17 via T-0418** (resolved by Specialist); crontab line verified with `--alert-tokens 18665902 --send-telegram` flags active.

### Recovery — 172M (p99)

Ratified 2026-05-11 by the operator (msg_989 via T-0069). Followed the same percentile-based proposal pattern (`tools/usage_monitor.py --propose-threshold` on `claude-fallback`'s `usage.jsonl`) after ~7-day baseline accumulation; the operator picked option (b) p99/172M for Orchestrator-consistency (same p99 anomaly-alarm framing as Orchestrator's 170M). Live since ratification: `claude-fallback` crontab carries `--alert-tokens 172000000 --send-telegram`. Recovery's distribution is narrower than Orchestrator's (short focused recovery / grooming dispatches, not deep Telegram threads), so the p99 lands close to Orchestrator's.

**Re-eval 2026-07-01 — RATIFIED 2026-07-06 by the operator (T-1584, msg 4664; Recovery's own re-run):** 2069 snapshots; distribution grew ~4x; ratified 172M now ≈p93. **Ratified: 308M (p99)** — same p99 methodology. Actuation: Recovery self-actuates crontab (T-1485, mailboxed 2026-07-07).


## Method (shared)

`tools/usage_monitor.py --propose-threshold` reads the agent's own `usage.jsonl`, computes percentiles over the **non-zero 5h-window subset**. Zero windows (agent idle) are excluded — including them shifts everything left and undersizes the alert. The 5h window choice mirrors Anthropic's Max-plan 5h-rolling-session cap mechanic (per `knowledge/watch/reports/2026-05-12/anthropic-rate-limit-research.md` §2.1).

## Re-evaluate

- **Per row:** 30 days post-ratification (first fleet-wide re-eval ratified 2026-07-06; next trigger ≈2026-08-05). Each agent re-runs `--propose-threshold` on accumulated data; HO compares to current value; amends this catalog if the recomputed value differs materially.
- **Catalog-wide:** when a new long-lived agent joins the org (Echo resumed; new project starts). **Fired 3× 2026-07-19/20 (T-1917 note):** archivist (ORG-042), mason + artisan (ORG-043) have no rows yet — deliberate, not drift: each row needs accumulated non-zero 5h-window `usage.jsonl` data first, and catalog additions route via the `--propose-threshold` proposal flow per T-1875's resolution. Add the three rows when their first 30-day accumulation supports a proposal.
- **Distribution-shape change:** if a row's non-zero distribution turns multi-modal-beyond-bimodal or the saturation cluster shifts >2× from the baseline, surface as a separate ticket.

## Cross-link

Specialist's full analysis (method, raw output, histogram, percentile mapping, confidence + caveats, sources) lives at `knowledge/watch/reports/2026-05-17/b2-monitoring-threshold-proposal.md`. This catalog is the durable / ratified surface; that report is the working-tier research record.

## DECISIONS

- Catalog format (multi-row policy file) over per-agent docs — justifies via P1 (architecture over policy; catalog naturally encodes cross-agent consistency) — scales as Recovery + future agents amend the file rather than each ratifying a separate doc.
- Substrate `rulebook/policies/` (not `adr/`, not `notes/`, not `knowledge/watch/reports/`) — justifies via [[ORG-015]] (ratifiable-class allow-list; policies/ is in the allow-list; operational calibration of D1 fits policy-tier) — first concrete application of ORG-015's allow-list while ORG-015 itself is awaiting ratification at T-0400.
- Orchestrator row carried forward with prior Telegram-verbatim ratify (T-0010 msg_412) — justifies via P4 (traceability — existing ratification stays valid; new file just captures + indexes) — avoids re-ratifying already-ratified content.
- Specialist row's percentile differs from Orchestrator's (p95 vs p99) — justifies via P5 (roles don't hop; the framing "anomaly alarm aligned with cap-hit budget" is shared but the percentile that achieves it differs by workload shape) — Specialist's bimodal distribution makes p95 = saturation cluster; Orchestrator's smoother distribution makes p99 the right pick.
- Specialist's "deterministic ceiling" caveat lifted into the rationale — justifies via P3 + ORG-012 (decisions justify upward; surface the caveat where decision-makers see it, not buried in working-tier draft) — the operator sees the open question before ratifying.
