# Pantheon — knowledge

Env-knowledge artifacts: everything Specialist and Orchestrator have learned about the environment. Two child shapes, one search root.

## Children

- [`episodic/`](episodic/) — commissioned research arcs (ad-hoc, multi-subagent, deep deliverables). Run when a substantial design question opens; closed when synthesis lands. Was `research/` at repo root pre-T-0412.
- [`watch/`](watch/) — continuous-S4 layer (cron'd researcher dispatches, standing topics, daily digest). Reports land per-cadence from the watch loop. Was `watch/` at repo root pre-T-0412.
- [`company/`](company/) — the operator's studio company materials (revenue-project entity layer): formation checklist, operating-agreement draft, S-corp groundwork. Certificate of Formation filed 2026-06-29; downstream (EIN → Operating Agreement → bank → W-9) staged pending TX SoS approval, back-burnered until the LLC is active (T-1452 arc). Populated 2026-06-30.

## Reference files

- [`pantheon-glossary.md`](pantheon-glossary.md) — the named things in Pantheon (personas, daemon session names, account paths, historical vocabulary, stability-naming quirks). The index a fresh agent or operator can grep when a name in the repo doesn't make sense. Authored 2026-06-03 by Specialist per the T-0751 bringup-doc revise pass.
- [`infra-abstractions/`](infra-abstractions/) — the **infra-abstractions reference map**: a descriptive catalogue of the recurring infrastructure *component families* across all three agents (Monitors, Pollers/Wakers, Due-detectors+Filers, Runners, Remediators, Guards, Digesters) + the cross-cutting patterns they share. Answers "where does a new tool fit / what pattern should it follow." Glossary = terms; this map = components and shapes. Drafted 2026-06-09 by Recovery (T-0999).
- [`vsm-instantiation.md`](vsm-instantiation.md) — the role set (`orchestrator`, the optional `sub-orchestrator` N+ tier, `specialist`, `recovery`, and the `memory-hub`/`third-party-auditor` opt-in extras) presented explicitly as a Viable System Model instantiation, with per-role/extra viability-gap notes.
- [`vsm-deployment-self-check.md`](vsm-deployment-self-check.md) — Beer's four diagnostic questions (verbatim, 1984), as a standing "is your deployment still viable" checklist to re-run when the role/extra set changes.

## When to look where

- **"What do we know about topic X?"** — start here, then drill into `episodic/` for deep prior surveys or `watch/` for recent cadence-driven findings.
- **"What has the watch loop found on topic X recently?"** — `watch/reports/<YYYY-MM-DD>/<slug>.md`; see [`watch/README.md`](watch/README.md) for reading order.
- **"What is the archived deep study on X?"** — `episodic/<date>-<topic>/README.md`.

## ADR

Topology ratified in [[ORG-017]] (T-0412 migration, 2026-05-17). Historical refs in `ledger/` and `tickets/done/` preserve the pre-migration paths as written; that is intentional per the audit-chain historical-immutability rule (ORG-001 + ORG-003).
