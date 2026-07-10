# Visual verification for UI tickets

## When to use this

Any ticket whose acceptance includes a UI/UX outcome (kino-pwa layouts,
screenshots, component renders, mobile/desktop responsive behavior, error
toasts, form states, color/contrast). Applies to both Orchestrator (HO) and any
implementer that lands UI work.

## Procedure

### 1. Before declaring done

The implementer (or HO doing implementer-tier work) must produce VISUAL
EVIDENCE that the acceptance criteria are met. Not "the diff looks right";
not "the unit tests pass"; not "the code compiles". Actual rendered output.

Acceptable evidence (any one):
- Playwright e2e screenshot (per T-0238 conventions — once landed).
- Manual screenshot on the target viewport (mobile = iPhone-class width;
  desktop = laptop-class width).
- For multi-viewport tickets: BOTH viewports captured.
- Embedded in the ticket close note OR linked to a path under
  `pantheon-config/screenshots/T-NNNN/` (committed alongside the close).

### 2. Reviewer check

Reviewer (in a b9 cycle) treats the visual evidence as part of the spec.
- Missing evidence on a UI ticket = REBOUND with `visual_evidence_missing`
  in the reviewer feedback.
- Evidence present but contradicts acceptance = REBOUND with the
  contradicting screenshot referenced.

### 3. The deploy carrier ticket is NOT the visual verification gate

Pantheon-pwa redeploys (T-0265, T-0285, T-0287 pattern) lift code from
`pantheon-pwa@<tip>` to live kino. The CARRIER ticket's acceptance is
"build succeeded + kino restarted active". The CARRIED ticket's visual
acceptance is owned by the original UI ticket — done at the implementer
cycle, before the deploy is dispatched.

### 4. Smoke after deploy, not before

After a deploy carrier lands, the operator (the operator typically) does a
real-device smoke. If visual reality contradicts the prior visual evidence,
it's a precedent for diff between dev-env render and production render —
catalog that drift class separately.

### 5. Deploy-carrier pattern (any service-bearing repo)

§3-4 frame the pattern in pantheon-pwa terms, but the discipline
generalizes: **for any service whose code lives in a repo and runs from a
deployment copy (rsync + systemd unit, or any equivalent ship-from-source
shape), the ticket landing the code change must NOT declare done at "merged
to main".** Merge ≠ deploy. The acceptance criteria must include:

- **Deploy step actuated** — the deployment-copy is in sync with the
  merged ref. For rsync-shaped deploys, this is `rsync src/ /opt/<svc>/src/`
  (under sudo if the service runs as a non-claude user). For other shapes,
  whatever the equivalent ship-from-source action is.
- **Service restarted** — the running process must be restarted so it
  picks up the new code. Typically `systemctl restart <svc>.service` (user
  or system scope as appropriate). Verify with `systemctl is-active` +
  `ActiveEnterTimestamp` post-restart.
- **Verified live** — at minimum, confirm the deployed code is what's
  running (e.g., `grep '<new-identifier>' /opt/<svc>/src/...` returns
  matches). Ideally, exercise the new behavior end-to-end through the
  service's normal entry point (HTTP request, MQTT publish, etc.).

Pattern carriers observed (precedents):
- **pantheon-pwa** (per §3) — kino backend, T-0265/T-0285/T-0287 carriers.
- **echo-recorder** (T-0393, msg_1665) — slider commit landed but service
  was never restarted; the new code sat on disk for a day before catching
  it. Restart + verify pattern applies.
- **/opt/pantheon-pwa** for the kino daemon (T-0428) — async-push fix
  required rsync to `/opt/pantheon-pwa/src/` + `systemctl restart kino.service`
  after merge. Carrier-acceptance-style note in the ticket close captured
  the deploy state explicitly.

Reviewer check (in a b9 cycle) for any service-bearing-repo ticket:
- Acceptance criteria explicitly name the deploy step → APPROVE-eligible.
- Acceptance silent on deploy → REBOUND with `deploy_step_missing`. The
  implementer doesn't have to *do* the deploy if it requires sudo HO can't
  delegate, but the acceptance must name who does it and when.

This avoids the "write-shipped ≠ deploy-shipped" gap class: a commit on
origin/main reflects authorial intent; a running service reflects
operational reality. The acceptance must verify the second.

## Common failure modes

- **CSS rule looks correct in diff, but rendered output is wrong.** Catch:
  always run the actual UI before declaring done. T-0263 / T-0287 precedent:
  `min-width:0 on flex chain` was sound per the rule, but the chat container's
  root max-width was missing, so the bubble overflow persisted. Diff-review
  alone missed it.
- **Implementer tested on desktop, regression bites mobile.** Catch: explicit
  mobile viewport test or Playwright per T-0238.
- **Snapshot baseline drifted; tests pass against stale baseline.** Catch:
  human eyeball check at acceptance time even when automated tests pass.

## Boundary

Backend-only tickets (API contract changes without UI surface) are exempt.
CLI changes that affect terminal output: visual-verification is the operator
running the CLI + sharing the output. Not all "user-facing" work is
graphical; the spirit is "rendered behavior matches the spec, verified
empirically", not "screenshot exists for screenshot's sake".

## Cross-references

- T-0263 / T-0287 / T-0288 — the precedent that surfaced this SOP need
  (chat bubble width fix passed code review but failed visual reality).
- T-0238 — Playwright e2e for kino-pwa; the structural automation that
  makes visual verification scalable. Until landed, manual screenshots
  are the discipline.
- T-0393 (echo-recorder slider) + T-0428 (kino-pwa async push) — the
  precedents that motivated §5 amendment (write-shipped ≠ deploy-shipped
  gap surfaced twice in one session).
- `sop/standard/how-to-message-the-org-user.md` — the operator's reports include
  visual evidence when relevant; this SOP is the producer-side counterpart.

---

**Status:** Drafted 2026-05-12 by Recovery per the operator msg_1170 ('You should have
a review methodology that includes visual tests'). **Ratified 2026-05-12 by
The operator (msg_1174 — "Sounds ratifiable").** **Amended 2026-05-18 by Orchestrator
(HO) per the operator msg_1667 ('amend the existing one'): §5 generalizes the
deploy-carrier pattern from pantheon-pwa-specific to any service-bearing
repo; precedents added to §Cross-references.**
