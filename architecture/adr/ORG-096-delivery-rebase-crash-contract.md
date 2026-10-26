---
aliases: [ORG-096]
id: ORG-096
title: "Delivery-machine REBASE-crash contract — REBASE_INFLIGHT(prev_sha) durable state, three-way crash-recovery predicate, same-episode_id add/add classifier, controller↔ticket.py boundary (FOLDED INTO RFC-002)"
status: "Folded into RFC-002 on 2026-10-07 at the operator's direction (TG 11403-11405: it was RFC-level mechanism, not an ADR decision). The decision it served now sits under ORG-076, ratified 2026-10-07. Retained as a pointer; the full original text is RFC-002 'Appendix: design rationale (formerly ORG-096)'. Never ratified (previously Adopted by Recovery 2026-09-12, then Proposed 2026-10-07)."
date: 2026-09-12
author: Recovery
steward: recovery
relates: ORG-076, RFC-002
build_disposition: no-build-needed
---
# ORG-096 (folded into RFC-002)

This ADR covered the delivery machine's rebase edge (ORG-076 §2). It is folded into [[RFC-002]], which now implements [[ORG-076]]. The full original text, including D-1 onward, is in RFC-002's appendix "Design rationale (formerly ORG-096)". Folded 2026-10-07 at the operator's direction (TG 11403-11405).
