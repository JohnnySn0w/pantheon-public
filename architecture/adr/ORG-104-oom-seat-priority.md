---
aliases: [ORG-104]
id: ORG-104
title: "OOM containment: protect a list, a kill means a hold, only system orchestrators restart themselves"
status: "Accepted 2026-10-19 by the operator (TG 12034 'looks good', TG 12035 'let's adopt and build'). Proposed by Recovery; requested in TG 8588. Three-rule shape per TG 12002/12003. Rules 2 and 3 approved in TG 12005. Rule 1 v3 ('protect a list, not a lineage') approved in TG 12029, with the session hard line from TG 12020/12021. Installer-first per TG 12008/12010. Build: T-5226, T-5227."
date: 2026-10-19
author: Recovery (head_system_orchestrator)
steward: recovery
appetite: 5
riskiest_assumption: "That the protected list can be derived reliably and kept current: what each seat's harness controller started in its tmux pane, plus the tmux server and plumbing, across restarts and harness swaps. A stale list either leaves a live session unprotected (it gets killed) or protects something that should be killable. The killer is new code running as root on every host; a bug in it is a box-level fault."
build_disposition: needs-build
scoping_ticket: T-5222
relates: ORG-026, ORG-068
no_registry_row_waiver: "Host-level memory-protection policy for the already-registered host-substrate component (ORG-080 F4). Registers no new component; the killer and restart drop-ins it builds (T-5226, T-5227) are host-substrate tooling under tools/oom/."
ratifiable_files: [pantheon-config:architecture/adr/ORG-104-oom-seat-priority.md]
---

# ORG-104: OOM containment

**Status:** Accepted 2026-10-19 by the operator (TG 12034/12035). Build: T-5226, T-5227.

## Decision

1. **Protect a list, not a lineage.** The protected set is exactly what each seat's harness controller started in its tmux pane, plus the tmux server and the plumbing sessions need (user managers, dbus, the mail hub). Everything else is killable, whoever spawned it. Before the kernel's OOM, one early killer of ours kills the largest process that's not on the list. A protected session above a hard memory line drops off the list and becomes killable like anything else.
2. **A kill means a hold.** Each kill is traced up its ancestry to the seat that launched it. That seat goes on hold: no new launches and no resumed work until a system orchestrator or the operator releases it. A leaking task can't be respun automatically.
3. **Only system orchestrators restart themselves.** Recovery and Watchman are the only seats with a root-level restart, with back-off, and only while memory is clear. They restore every other seat, Orchestrator included, one at a time and in priority order. A system orchestrator put on hold on purpose is not restarted.

One killer replaces the layering of earlyoom, systemd-oomd and inherited shields. The kernel's own OOM protection stays on listed sessions, as the last line. Every install verifies that the list matches its live sessions, and seed installs get all three rules from the same installer we run here.

## Why

**2026-10-19, two OOMs in 12 hours** (T-5222; `incidents/2026-10-19-fleet-oom-control-plane-loss/`):

- **The cause was unbounded work.** Both times, a mason review was running probes against the worker-guard lexer, and they ballooned to 6–10 GiB (T-5140 probes; the lexer fix is T-5224).
- **Protection by lineage was upside down.** The probe inherited its session's shield, so it was the last process the killers would pick. Recovery wasn't on the shield's pattern at all, so it was the kernel's first kill. The kernel then killed user managers across seven accounts.
- **Restarts made it worse.** Controllers restarted into the ongoing OOM until they hit their start limit, and nothing inside the fleet could bring the recovery seats back. The operator had to.
- **The same work came back.** The second OOM ran the same class of probe as the first.
- **Sessions themselves stayed small.** At peak pressure no session was above about 0.35 GB of RAM plus swap. The hog was a single 9.8 GB process.

The earlier layers each fixed one symptom, and together they let a protected leak take down everything around it. These rules replace them with one list, one killer, one stop and one restarter. None of them depends on how a particular harness spawns its tools.

## Alternatives considered

- **Keep layering protections** (shield patterns, descendant sweeps, kill-order lists, restart locks). They overlap, and their interactions were the failure.
- **Cap work per task or per seat.** This is brittle, because nobody knows in advance how big a legitimate task needs to be (the operator, TG 12005).
- **Separate work from sessions at launch** (a wrapper on every tool call, plus per-call labels). This depends on each harness's internals, and codex has no launch hook (the operator, TG 12016).
- **Configure earlyoom.** The installed version can neither rank by size alone nor exclude a list of processes.
- **Add RAM or swap.** This buys time but changes nothing; it's a cost call for the operator, separate from this ADR.

## Consequences

- **This contains a leak; it doesn't fix one.** A leaking program still leaks. It dies, and its seat stops until someone looks. Fixing the program is its own ticket.
- **The largest unprotected process dies, even if it's legitimate.** Other work running at the same time may be killed too. That costs work, never sessions.
- A held seat stays idle until it's released. That costs time, never the box.
- Linux with systemd only.
- The mechanism is in T-5222's build note and the build tickets (T-5226 for rule 3, T-5227 for rules 1 and 2), not here: the killer, how the list is derived, the hard line, the hold, the restart drop-ins, and the installer with its uninstall.
