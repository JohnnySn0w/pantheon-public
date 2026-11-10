# Watch reports — 2026-11-02

Reports filed by the watch loop on 2026-11-02. One line per topic.

| Topic | Status | One-line summary |
|---|---|---|
| [anthropic-claude-code-changelog](anthropic-claude-code-changelog.md) | ok | TL;DR: Claude Code v2.1.304-2.1.316 shipped (10/19-10/31); no billing/classifier/persona-format change found; only dated item is Opus 4.8 fast mode removal 2026-11-30. |
| [agentic-harness-landscape](agentic-harness-landscape.md) | ok | TL;DR: Routine release churn across mapped harnesses (OpenClaw 2026.10.9 security dep patches, Codex 0.166, Hermes 0.22.5); one new entrant worth noting - Microsoft Copilot harness via Foundry (2026-10-28). |
| [llm-agent-failure-modes-research](llm-agent-failure-modes-research.md) | ok | TL;DR: Late-Oct arXiv shows a trend toward trajectory-level/repair-and-diagnosis evals (ReplayRCA, Prefix Executability, Dittany); nothing contradicts MAST or Huang et al. |
| [mcp-supply-chain](mcp-supply-chain.md) | ok | TL;DR: litellm 1.107.0 (PyPI, 2026-10-21) shipped a build-pipeline-injected credential stealer that targets ~/.claude and ~/.cursor configs; no litellm dependency found in Pantheon repo, but rotate-if-installed applies. Rest of the week is routine MCP CVE churn. (readback: missing) |
| [anthropic-policy-and-billing](anthropic-policy-and-billing.md) | ok | TL;DR: New Anthropic Usage Policy takes effect 2026-11-12 (announced 10-08); its subscription-routing clause targets resellers/proxies, novelty unverified; no classifier expansion or HERMES\.md-style pattern news found. |
| [anthropic-quota-interface](anthropic-quota-interface.md) | ok | TL;DR: Still no documented structured subscription-usage query (Claude Code 2.1.316; no `claude usage`, no API); status-line `rate_limits` stays post-response only, so the ping/pong probe stands. |
| [echo-stack-vendor-changes](echo-stack-vendor-changes.md) | ok | TL;DR: No breaking change for Echo this week; j17 shipped security-only 0.25.25/0.25.26, Speechmatics minor fixes, rumqtt unchanged, Haiku 4.5 still Active (floor only), Opus 4.8 fast mode deprecated (not Echo). |
| [context-engineering-research](context-engineering-research.md) | ok | TL;DR: AGENTS.md anchor (Gloaguen) now v3 (29 Sep 2026) with no change to the verdict; one small underpowered follow-up (Khatri, null) and one contrary efficiency study (Lulla); new agent-rot paper says step count, not context length, drives decay. |
| [context-management](context-management.md) | ok | TL;DR: Still no public Haiku 4.5 / Sonnet 4.6 / GPT-4o-mini long-context curve, but the MRCR v2 board now shows OpenAI mini/nano (0.33) far below flagships (0.74-0.92), which supports the operator's smaller-is-worse hypothesis within a family; no D10 change. |
| [voice-as-interface](voice-as-interface.md) | ok | TL;DR: Block announced Dessa (2026-10-23), a research-preview full-duplex speech-to-speech model trained on audio only, no weights/API/benchmarks; no Echo-affecting platform change found. |
| [grand-goal-competitor-watch](grand-goal-competitor-watch.md) | ok | TL;DR: No Grand-Goal-shape change this week; OpenClaw ~394k and Hermes ~260k GitHub stars, Nous $90M round (10-07) confirmed, one new indie "living agent" (Nodecode, 174 stars) worth a watch. |
| [glop-it-blog](glop-it-blog.md) | ok | TL;DR: Glop blog has no new posts in the past week; still the same two posts from 2026-09-18. |
| [free-frontier-adjacent-models](free-frontier-adjacent-models.md) | ok | TL;DR: Quiet week: no new free text models after Flint; Particle-1 (DeepSeek V4.1 Flash post-train, $0.05/$0.50) and GPT-6.1 Terra landed paid; GLM-5.3-Flash is now ~$0.03/$0.09 per M; Poolside Laguna free routes vanished from the catalog. |
| [flipper-one-launch-watch](flipper-one-launch-watch.md) | ok | TL;DR: No Flipper One launch change: $20/$379 reservation coupon still the only available variant; Kickstarter still undated. |
