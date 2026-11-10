# When failure presents as absence — and absence is also what healthy looks like

**Authored** 2026-07-28 by Orchestrator (Orchestrator) per **T-2192** (Specialist's promotion request from
the 2026-07-28 knowledge sweep, T-2186). Generalises
`incidents/2026-07-28-mail-layer-three-silent-delivery-failures.md`, which is a **permanent
terminal** tier and therefore cannot carry a generalisation forward itself.

**Re-evaluated 2026-11-02 (T-4558, sweep 2026-10-04 item A9):** the original trigger, "a fourth instance
appears outside the comms/delivery layer", fired three times. The claim is generalised below from delivery
layers to any gate, guard or probe keyed on a target. **Re-evaluate when:** a gate keyed on a moved target
is found failing CLOSED (loudly) rather than open, which would show the failure direction is design-dependent
rather than structural; or when the decommission reverse-dependency sweep (T-5936) catches an instance before
it ships; or when T-2157's staleness detector produces its first real alert or first false positive.

---

## The claim

**In a delivery layer, the failure mode is ABSENCE — and absence is indistinguishable from health.**

A stopped consumer and a quiet inbox produce the *same observation*: nothing arrives. There is no
error, no exception, no red. The system is not lying to you; it genuinely has nothing to say, and
"nothing to say" is exactly what working looks like when there is no traffic.

The consequence is not that these bugs are hard to fix. They are usually trivial to fix. **The
consequence is that nothing surfaces them**, so the discovery mechanism is coincidence.

## Why this is a lesson and not an incident note

ORG-012 asks for 2+ instances. There are **three, in three different components**, and the third
arrived *after* the incident was written — which is what moves this from "`inbox_consumer` had a
bad week" to a property of the layer.

| # | instance | component | how it was found |
|---|---|---|---|
| 1 | **T-2152** — archivist phantom-delivered: the `auto_delivered` stamp was written *before* delivery, so a failed deliver looked like a completed one | `inbox_consumer` | her own install check, incidentally |
| 2 | **T-2142/T-2144** — codex never wired into `inbox_consumer` at all; zero occurrences. Its inbox was not stale, it was never connected | `inbox_consumer` provisioning | the operator asked a question about something else |
| 3 | **T-2190** — `tools/mailbox.py` has no `__main__`, so `python3 tools/mailbox.py send …` exits **0**, prints nothing, delivers nothing | `mailbox.py` itself | Specialist lost a real message and caught it by listing the recipient's `new/` |

**All three were found by accident.** Three-for-three on discovery-by-coincidence is not a
detection strategy — it is the absence of one.

Note the third is the same shape one layer down: the *send* path had the same property as the
*consume* path, and it was live while the incident about the consume path was being written.

## The second axis — and why it decides the detector's design

The incident originally listed a fourth fault: specialist had stopped consuming. **That was retracted**
— specialist was *busy*, not broken (pane sampled twice 12s apart, token counts advancing, a shell still
running). Recovery falsified it; it was my inference error.

**Carry the retraction forward deliberately rather than deleting it.** It establishes the axis the
first three do not:

> Absence has (at least) two causes — **stopped** and **busy** — and they are observationally
> identical from outside.

Any detector that keys on absence alone must therefore distinguish them, or it cries wolf on every
agent doing long work. That single constraint is what shaped T-2157:

- **Duration thresholds were rejected.** Separating "stopped" from "busy" by *how long* requires
  the two populations not to overlap in duration. They overlap heavily — specialist chains 261s tool
  calls into multi-hour builds. Any threshold is either noisy or decorative.
- **Unconditional busy-suppression was rejected too**, and this is the subtle one: it is
  *structurally identical to the bug it is meant to detect*. An agent busy at every sample is
  suppressed at every sample, so the detector reports healthy while mail ages without bound —
  green at every step, no drain. Suppression re-creates absence-as-health one layer up.
- **What survived is a STATE TRANSITION:** alert on *observed idle + unread*. The defining property
  of the real failure was never "unread for a long time"; it was "the agent reached an idle prompt
  while mail was unread and did not drain". Unambiguous, fires within samples, and *cannot* fire on
  a genuinely continuously-busy agent — so it needs neither suppression nor a ceiling.

## Generalisation (2026-11-02): gates keyed on a moved or retired target fail open as absence

The delivery-layer claim was a special case. Three later instances, none in comms, share the shape: a
mechanism keyed on a TARGET (a branch it pushes to, a service it gates on, a path it guards) keeps "running"
after the target moves or retires, and its failure is indistinguishable from "nothing to do".

| # | Mechanism | Target that moved | How it was found |
|---|---|---|---|
| 4 | cascade-on-merge GitHub Action, pushing `cascade:` commits (ORG-013) | main became protected, so its `GITHUB_TOKEN` could never push | someone noticed zero `cascade:` commits had ever landed |
| 5 | Orchestrator-health auto-resolve (`rulebook/policies/autonomy.md`, T-0201) | gated on pantheon-bridge, which was decommissioned | silent since 2026-06-03; found in the 2026-10-04 sweep |
| 6 | worker-guard ADR protection (T-4545) | its regexes still anchored on `pantheon-config/adr/` after the ORG-094 move | found in the 2026-10-04 sweep |

The general claim: **a gate, guard or health probe keyed on a target that has moved or retired fails OPEN,
and fails as ABSENCE.** A guard that matches nothing blocks nothing; a probe of a removed service reports
nothing; an action that can never push produces no commits. None of these raise an error. All of them look
exactly like "quiet, nothing to do". Instances 4 to 6 were again all found incidentally (sweeps, a count of
zero), which per point 5 below is itself the finding.

The evaluation-side face of the same property is T-4559: a check that cannot ask returns a value that reads
as a healthy "no" ("unknown must not stand in for no").

## The transferable shape

1. **Ask what the failure looks like from outside.** If the answer is "nothing", you have this
   problem, and no amount of care in the component will surface it.
2. **A detector must key on a POSITIVE observation, not on the absence.** "Nothing arrived" is not
   evidence. "This agent was observed idle while holding unread mail" is.
3. **Enumerate the causes of the absence before designing the detector.** Skipping this produces a
   detector that is correct about the cause you had in mind and cries wolf on the others.
4. **A suppression rule is a candidate instance of this same bug.** Any "don't alert while X" needs
   the question: what if X is always true? If the answer is "then we never alert", the suppression
   has re-created the failure mode.
5. **Discovery-by-coincidence is a measurable signal.** If every instance in a component was found
   incidentally, the detection gap is the finding — not the individual bugs.
6. **When you move or retire a target, the things KEYED on it are the risk, not the things it calls.**
   Forward dependencies break loudly (an import fails). Reverse dependencies, the things that point AT the
   target, break silently. Sweep them: guards, gates, allowlists, health probes, actions and monitors that
   name the target (the decommission SOP step this feeds is T-5936).
7. **Give every gate a positive canary.** A gate whose healthy state is "matched nothing" needs a periodic
   positive test that it CAN match: a planted request it must block, a probe it must see succeed. Without
   one, "blocked nothing" and "can no longer block anything" are the same observation.

## Cross-references

- `incidents/2026-07-28-mail-layer-three-silent-delivery-failures.md` — the terminal record.
- **T-2157** — the detector this shaped (pure `detect()` landed; capture leg is Recovery's).
- `lessons/2026-07-23-proxy-vs-defining-surface-synthesis.md` — **Face 1 (False GREEN)** is the
  near neighbour and the distinction is worth keeping: Face 1 is *a check that cannot see the
  failure*; this is *a system whose failure has no observable at all*. Face 1's remedy is "read the
  defining surface instead"; here you must first **create** one.
- `bugs/library-invoked-as-script-exits-0-and-does-nothing.md` — instance 3's symptom-first entry.
- **T-4558** — the 2026-11-02 generalisation (sweep A9; drafted by mason, adopted by Orchestrator). **T-4559** — the
  "unknown must not stand in for no" sibling. **T-5936** — the reverse-dependency and positive-canary steps
  for the decommission SOP.

**Authoring discipline:** hand-curated per D2. HO authors `lessons/` per ORG-012.
