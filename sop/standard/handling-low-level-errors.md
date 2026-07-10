# Handling low-level errors — log, fix, cluster, surface

## When to use this

Whenever a direct-channel agent (Orchestrator / Recovery / Specialist) detects a low-level error in its own infrastructure (sync race, transient API failure, retry-able timeout, recoverable wedge, etc.). The org user does NOT need to see individual transient errors on Telegram/PWA. They need surfacing only when (a) the error genuinely requires their action, OR (b) accumulated cluster patterns reveal a higher-level architectural problem worth remediating.

## Procedure

### Tier 1 — Per-error: log + auto-fix

1. **Detect** — predicate fires (e.g., sync push race, wedge sentinel match, build retry-recoverable failure).
2. **Auto-fix if possible** — next-cycle retry (sync races resolve on the next pushing lifecycle op / inbox-consumer pull cycle); send-keys remediation (dev-channels dialog auto-resolves per `RECOVERY_AUTO_RESOLVE_ALLOWLIST`); etc.
3. **Record** — write a ledger entry (`agent` = your role, `action` shaped like `<class>_recorded` or `<class>_handled_silently`, `rationale` = the error excerpt + auto-fix attempted). NO Telegram message to the operator.
4. **Optional**: if the error matches a recurring pattern that has a documented fix in `bugs/`, cite the bug ID in the ledger entry's rationale.

### Tier 2 — Cluster detection: surface architectural pattern

Periodically (e.g., daily / weekly / per-sweep) scan the ledger for accumulated low-level errors and surface clusters worth higher-level remediation.

- **Threshold-based**: if N entries of the same `action`-class fire within a window (e.g., 20 push_failed entries in 7 days), file a ticket targeting HO with the cluster summary + proposed architectural fix (e.g., "20 push races this week → consider serializing the auto-commit fan-out").
- **Pattern-based**: if a new error class appears that doesn't match any existing `bugs/` entry, file a fresh `bugs/<keyword>.md` entry per the bugs-catalog discipline.

### Tier 3 — Genuine escalation: Telegram to the operator

Only escalate to the operator when:

- The error requires manual user action that no agent can perform (divergent git state needing reconciliation, secret/credential changes, third-party-service issue requiring user-side fix).
- The cluster-detection threshold fires and the architectural fix needs the operator ratification before HO dispatches.
- A genuinely-novel error class has no auto-fix path AND no documented pattern AND the runtime impact is user-visible.

In escalation messages, follow `sop/standard/how-to-message-the-org-user.md` — tight, no technical wall-of-text, decision-frame if it's a call-a-shot.

## Common failure modes

- **Spam-by-transient**: every push race fires a Telegram. Phone-hostile; the operator loses signal-to-noise. Fix: classify the error → if self-healing class → log only, no Telegram.
- **Silent rot**: error recorded silently but no one ever looks; accumulation invisible until the system actually breaks. Fix: Tier 2 cluster-detection cron MUST exist as the safety valve.
- **Premature escalation**: agent surfaces a low-level transient as an architectural ask before the cluster justifies it. Fix: respect the threshold; "this happened once" is not a pattern.
- **Over-broad self-healing**: classifying a genuine-action-needed error as "self-healing" silences something the operator needed. Fix: when in doubt about a NEW class, escalate the FIRST occurrence with a "is this a class we should silence going forward?" question.

## Boundary

- Applies to: agent-detected runtime errors in Pantheon infrastructure (sync, restart, wedge, build, etc.) — including kino-pwa server errors, Orchestrator's spawn failures, Specialist's research-watch failures.
- Does NOT apply to: business-logic errors the user needs to know about (ticket creation failures the user attempted, deploy failures during an operator-initiated push); those go through the normal call-a-shot escalation path.
- Does NOT apply to: incidents (one-shot postmortems live in `incidents/`); ADR-tier architectural decisions (`adr/`).

## Examples in current implementation

- **Consecutive-skip routing** (`tools/inbox_consumer.py` `check_consecutive_skips` → `_send_alert_via_recovery`): a repeated-skip cluster routes via Recovery's pane first (Recovery can remediate cross-account), then audit-Telegrams the operator. The self-healing-vs-escalate split lives in the routing helper. (The former `check_pantheon_config_sync_alert` was a second example of this severity split — push/pull/fetch_failed → ledger only, divergent → Telegram — but it was retired in T-1036 once the T-0365 pantheon-config-sync timer that fed it was removed.)
- **Wedge auto-resolve** (`tools/inbox_consumer.py` `RECOVERY_AUTO_RESOLVE_ALLOWLIST`): dev-channels confirm dialog → `1` + Enter auto-fix; sends informational Telegram. Other wedge sentinels → escalation Telegram. Reference: T-0142 + T-0201.
- **Bugs catalog** (`bugs/`): file per gotcha with surgical fix. Recurring patterns get filed here (one per pattern). Reference: `bugs/README.md` discipline.

## Cross-references

- `rulebook/directives.md` §D1 (bandwidth preservation) — this SOP is D1's error-channel calibration.
- `sop/standard/how-to-message-the-org-user.md` — escalation message shape when Tier 3 fires.
- `bugs/README.md` — recurring-pattern catalog (Tier 2 output target for novel classes).
- `incidents/` — one-shot postmortems for events that cross the escalation threshold.
- T-0213 (sync-alert plumbing implementation), T-0201 (wedge auto-resolve), the operator msg_1104 + msg_1108 + msg_1109 (this SOP's directive trail).

## Status

**Drafted 2026-05-12 by Recovery at the operator's directive (msg_1108 + msg_1109).** Authoring driven by the sync-alert severity-split work in commit 0cae3a9. Awaiting the operator ratification before becoming precedent agents autonomously cite.
