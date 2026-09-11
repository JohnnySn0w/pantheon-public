---
aliases: [ORG-077]
title: The Health Plane — operator-facing fleet-health surface + per-persona notification control (FOLDED INTO ORG-076)
status: Folded into ORG-076 §7 "Operator plane" (2026-08-18, at the operator's direction TG 6102). Retained as a pointer — this was briefly a standalone ADR (2026-08-18) before being consolidated into the ORG-076 fleet-health-system ADR as its operator/notification leg (same Recovery steward). No standalone content remains here.
date: 2026-08-18
author: Recovery (recovery)
steward: recovery
relates: ORG-076
build_disposition: no-build-needed
---
# ORG-077 — The Health Plane (folded into ORG-076)

**This ADR was consolidated into [ORG-076 §7 "Operator plane"](ORG-076-fleet-health-and-repair-system.md) on 2026-08-18**, at the operator's direction (TG 6102, "yeah that's preferable"), after he noted the fleet already has health ADR(s) (TG 6100). Rather than maintain a parallel health ADR, its content — the per-persona notification mute (registry + `is_muted` consulted by all alert paths), the per-persona health view, and the kino Health Plane section — now lives as the **operator leg** of the ORG-076 fleet-health system (§1–6 = the autonomous repair leg; §7 = this operator leg; same Recovery steward).

Build tickets **T-2978** (mute backend → recovery) and **T-2979** (kino Health Plane section → orchestrator) now serve ORG-076 §7.

See ORG-076 §7 for the design. This file is a pointer only, so references to `ORG-077` still resolve.
