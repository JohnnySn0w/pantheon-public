# Pantheon — knowledge

Env-knowledge artifacts: everything Specialist and Orchestrator have learned about the environment. Two child shapes, one search root.

## Children

- [`episodic/`](episodic/) — commissioned research arcs (ad-hoc, multi-subagent, deep deliverables). Run when a substantial design question opens; closed when synthesis lands. Was `research/` at repo root pre-T-0412.
- [`watch/`](watch/) — continuous-S4 layer (cron'd researcher dispatches, standing topics, daily digest). Reports land per-cadence from the watch loop. Was `watch/` at repo root pre-T-0412.
- **the operator's studio company materials** (formation, operating agreement, contract templates, payment rails, invoice tool) — **RELOCATED OUT of pantheon-config** to the firm-level business repo **`studio-operator/company/`** (T-2174, 2026-08-11). Firm-level business/financial state does not live in this shared repo, per the business-info-routing convention (firm-level → `studio-operator`; engagement-scoped → `sentient-internal`).

## Placement rule

Living compliance or legal records (consent grants, regulatory state) never live inside dated `episodic/` dirs — episodic is closed point-in-time arcs; standing state lives at `knowledge/` top level. (HO-ratified 2026-07-21, T-1918.)

## Reference files

- [`consent-records.md`](consent-records.md) — standing TCPA prior-express-consent grants for Echo phone-call interface. Append-only; never overwrite. Relocated from episodic/ per T-1918.
- [`pantheon-glossary.md`](pantheon-glossary.md) — the named things in Pantheon (personas, daemon session names, account paths, historical vocabulary, stability-naming quirks). The index a fresh agent or operator can grep when a name in the repo doesn't make sense. Authored 2026-06-03 by Specialist per the T-0751 bringup-doc revise pass.
- [`infra-abstractions/`](infra-abstractions/) — the **infra-abstractions reference map**: a descriptive catalogue of the recurring infrastructure *component families* across all three agents (Monitors, Pollers/Wakers, Due-detectors+Filers, Runners, Remediators, Guards, Digesters) + the cross-cutting patterns they share. Answers "where does a new tool fit / what pattern should it follow." Glossary = terms; this map = components and shapes. Drafted 2026-06-09 by Recovery (T-0999).

## When to look where

- **"What do we know about topic X?"** — start here, then drill into `episodic/` for deep prior surveys or `watch/` for recent cadence-driven findings.
- **"What has the watch loop found on topic X recently?"** — `watch/reports/<YYYY-MM-DD>/<slug>.md`; see [`watch/README.md`](watch/README.md) for reading order.
- **"What is the archived deep study on X?"** — `episodic/<date>-<topic>/README.md`.

## ADR

Topology ratified in [[ORG-017]] (T-0412 migration, 2026-05-17). Historical refs in `ledger/` and `tickets/done/` preserve the pre-migration paths as written; that is intentional per the audit-chain historical-immutability rule (ORG-001 + ORG-003).
