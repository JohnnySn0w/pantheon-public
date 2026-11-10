# Pattern guards in fail-open hooks need a linear-time gate

**Date authored:** 2026-09-30
**Author:** Orchestrator (Orchestrator), from mason's worker-guard arc digest (T-4145 / T-4317 / T-4154 / T-4328).
**Status:** Authored (ORG-015 non-ratifiable class). **Partially superseded 2026-11-02 (T-4560):** its first re-evaluate trigger fired the SAME day it was written. T-4392 (HO ruling 2026-09-30, now `rulebook/policies/hook-failure-direction.md`) made every security-gating PreToolUse hook fail CLOSED on its own clock (`tools/hooks/_failclosed.py`: an exception, ambiguity, a 15s deadline from process start, or an input cap all give an exit-2 deny). The Observation below describes the pre-T-4392 world.
**Re-evaluate when:** a security-gating hook is found NOT routed through `_failclosed` (then the original bypass claim applies to it again); or the harness changes PreToolUse timeout semantics; or a guard ships with an automated worst-case-time test in CI and an adversarial round still finds a super-linear path.

## Observation

> **Since T-4392 (2026-09-30):** a super-linear path in a security-gating hook is no longer a BYPASS. The
> hook's own deadline denies before the harness timeout, so the failure mode is now **latency and over-deny**
> (a legitimate call refused after ~15s; e.g. T-4483). It's still a defect, but its severity drops from
> security bypass to availability. The bypass claim below still holds for any hook that does NOT go through
> `_failclosed`, and for ADVISORY hooks, which stay fail-open by design. That's why each hook declaring its
> class matters (`rulebook/policies/hook-failure-direction.md`).

A Claude Code PreToolUse hook that exceeds its timeout does not block the tool call; the call proceeds. So a guard whose matching can go super-linear on crafted or merely unlucky input **fails open on exactly the inputs that make it slow**. Catastrophic regex backtracking (ReDoS) in a security guard is therefore not a performance bug. It is a bypass.

In the 2026-09-29/30 worker-guard hardening arc, a super-linear path was found **three times**, each only by adversarial review, never by the functional suite:
- T-4145 round 2: the gh allowlist token walk;
- T-4317: the git-identity rule backtracked past 10s;
- T-4154: the sudo/su/doas escalation walk.

Every one of these passed its correctness tests. Correctness suites use short, realistic commands, and ReDoS needs adversarial length and shape.

## Design implications

0. **Route every security-gating hook through `tools/hooks/_failclosed.py` (T-4392); that, not linear-time matching, is the bypass control.** Linear-time matching is now an availability property: keep it, to avoid over-deny and latency.
1. **Linear time is a review axis, not a nicety.** Any guard that pattern-matches untrusted text inside a fail-open hook needs an explicit "worst-case time" check in review:
   - nested quantifiers;
   - alternation under repetition;
   - backtracking token walks.
   Prefer a single-pass tokenizer or scanner over stacked regexes.
2. **Pin it with a test.** Keep a timed worst-case-input test (long, adversarially shaped inputs, asserted well under the hook timeout) next to the functional tests, so a later edit can't silently reintroduce the path.
3. **Adversarial probes must themselves be bounded.** One unbounded ReDoS probe loaded the shared host (Recovery triaged it). One reviewer ran the real `gh` binary instead of the guard (harmless only because no token was present). Probe the guard function, not the real binary, and wrap probes in `timeout`.
4. **A differential oracle beats enumeration for data-vs-code classification.** T-4328 settled "is this heredoc data or code?" by running real bash on harmless sentinels and comparing against the guard's verdict. That was more decisive than any hand-written case list.

## Related

- `structural-enforcement-retires-the-prose-rule.md`, design implication #3: structure must be *actually reliable*. A fail-open timeout is a quiet way for it not to be.
- The worker-guard hook: `tools/hooks/worker_guard_impl.py`.
