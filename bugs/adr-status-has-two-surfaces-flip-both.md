---
symptom: "ADR frontmatter status: says Ratified but the rendered document still opens with a bold DRAFT Status paragraph — readers/agents act on the stale surface"
fix: "every status transition edits BOTH surfaces in the same commit: frontmatter status: AND the body **Status:** line; grep -n -i 'status' adr/ORG-NNN-*.md before pushing"
domain: adr, ratification, frontmatter
---

# ADR status lives on TWO surfaces — flipping only the frontmatter leaves a lying body

## Verification
Repro: `git show 5a68cd3770:adr/ORG-063-harness-manager-action-broker.md | sed -n '4p;14p'` — frontmatter says Ratified, body line 14 still says DRAFT (caught + fixed at `7fb2eed040` the same night). Detection: `tools/test_bugs_frontmatter_lint.py`-style paired grep, or eyeball both hits of `grep -n -i '^status:\|^\*\*Status' adr/<file>`.

**Symptom:** an ADR is ratified; the frontmatter `status:` says `Ratified ...` but the document still *reads* as a draft — because the body carries its own `**Status:** **DRAFT ...**` bold line (usually the first paragraph under the H1), and only the frontmatter got flipped. Anyone reading the rendered document (kino, GitHub, an agent doing a doc traversal) sees DRAFT and may act on it — e.g. decline to treat the decision as binding, or re-surface it for ratification.

**Where it bit:** ORG-063 ratification execution, 2026-08-12 (~04:08 UTC). The scripted flip did `re.sub(r'^status:.*$', ...)` on the frontmatter and pushed; the body line 14 `**Status:** **DRAFT (rewrite 2026-08-11) — awaiting the operator's ratification.**` survived and was only caught on a post-commit re-read. Fixed in a follow-up commit (`7fb2eed040`).

**Why it exists:** the body Status paragraph is a deliberate convention (it carries prose the one-line frontmatter can't: HITL provenance, what the draft supersedes, direction cites) — so the duplication is load-bearing, not accidental. The two surfaces just don't share a source.

**Surgical fix / discipline:** every ratification (or any status transition) edits BOTH:
1. frontmatter `status:` (machine-read: adr-scoping lint, build_disposition sweeps, doc traversal reports);
2. the body `**Status:**` line (human/agent-read in the rendered doc).

Grep before pushing: `grep -n -i 'status' adr/ORG-NNN-*.md | head` — two hits should both say the new state. If only one surface exists (some short ADRs), fine; if both exist, both flip in the SAME commit.

**Structural fix candidate (not built):** a lint that warns when frontmatter `status:` says Ratified but the body contains `DRAFT — awaiting` (or vice versa). Cheap regex pair; belongs in ticket_lint's ADR checks or the adr-scoping sweep. File it if this recurs.
