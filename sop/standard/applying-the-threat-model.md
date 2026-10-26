# Applying the threat model to a security or privacy finding

**Status:** Adopted 2026-10-20 by Orchestrator (HO), autonomous per msg-3733. It is a below-goal-level process SOP that puts ORG-105 into practice. The operator ratified ORG-105 (TG 12174) and asked for it to be "always present" (TG 12164). The operator override-available. T-5288.

The substance lives in `architecture/adr/ORG-105-threat-model.md`. This SOP says how to use it; it does not restate it. Section numbers below are ORG-105's.

## When it fires

Before you file, rate, rule on or act on a security or privacy finding, check it against the ORG-105 §4 bar: a complete chain of an in-scope foothold, a path that foothold can reach, and an impact on a protected asset.

It fires on:
- a reviewer's MED or HIGH security or privacy note, your own or one you're rating;
- any write-up that starts "an attacker could…";
- a redaction, masking or privacy ask;
- a hardening ask with no stated attacker;
- "cheap protective action" offered as the reason to act. Cheap and reversible justify *how* to act, never *whether* the threat is real.

## Procedure

1. **Name the chain.** Write down three things:
   - the foothold, one of the positions in §2;
   - the path that foothold can actually reach, possibly through our own outputs (a secret in a log, then pasted into a ticket that reaches GitHub);
   - the impact on an asset in §1.
2. **Check §3.** If the foothold is out of scope (an existing shell or root, a targeted well-resourced attacker, physical or provider access, the operator himself), stop: the finding is out of scope. Our own logs, journald, taps and debug output are internal, and reading them is not a finding. Their content matters only when a path carries it outward (to GitHub, a mirror or a third party) or across a seat boundary that an in-scope foothold could exploit.
3. **Check the §7 matrix.** A "—" cell means no realistic path is known. The author must show one before the finding is rated.
4. **An incomplete chain is not rated.** Send it back to its author to complete. If no in-scope chain can be named, note it on the ticket as out of scope under ORG-105 and leave it alone. "Defense in depth" alone doesn't make a chain.
5. **A complete chain is ranked by the asset at stake** (the §1 order), not by how clever it is. Then work it like any other ticket.
6. **Keep observability readable (§5).** If the fix is a redaction or a privacy feature, it ships as an opt-in setting, default off, unless this finding is the one that clears §4 for it.

## Cross-references

- `cve-response.md` (skill `pantheon-sop-cve-response`): a surfaced CVE is first verified against a primary source and ticketed under that SOP. Our exposure to it is then rated here. Both apply; they don't conflict.
- `rulebook/policies/trust-posture.md` and `access-by-operation-risk.md`: who may touch secrets, money and outward acts. That governance is separate from this threat model.

## Boundary

Not for ordinary operational errors, outages or flaky tests. Not for deciding who holds which credential or permission (trust-posture). Not a reason to re-open ratified designs: a finding that shows a real path where the §7 matrix says "—" is grounds to amend ORG-105, through the operator.
