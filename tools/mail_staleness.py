"""Per-inbox mail-staleness detection — the PURE half (T-2157).

The mail layer's failure mode is ABSENCE, and absence is also what healthy looks
like: a stopped consumer and a quiet inbox are the same observation. Three
independent faults surfaced on 2026-07-28 with three different root causes and one
identical symptom, and all three were found by coincidence
(incidents/2026-07-28-mail-layer-three-silent-delivery-failures.md).

WHAT THIS MODULE IS. The detection POLICY, and nothing else: no I/O, no clock, no
tmux, no filesystem. The caller (Recovery's half, on his account — cross-account pane
capture is his sudo) enumerates inboxes BY EXPLICIT NAME, captures panes, calls
`pane_idle_state`, precomputes ages, and hands the result here. That keeps `now`
entirely in the impure half and makes this half deterministic under test.

WHY CENTRALIZED, since it determines who hosts it: the failure mode IS "this agent
is not processing its mail". A detector running inside the non-processing agent is
subject to the identical silence — specialist idle at its prompt with 12 unread was not
running anything that could have noticed, and could not have been. Self-reported
liveness fails exactly when it matters, which is the same reason "dead consumer" and
"quiet inbox" are indistinguishable from the inside.

DETECTION IS NOT REMEDIATION. This module reports; it never kicks. Kicking stays in
`mailbox_wake`, the one actor that legitimately does it. The operator disarmed automated
remediation (TG 6520: "detection on, remediation off, do not re-arm without his
say-so"), and a detector that fired kicks would be a back-door re-arm of exactly
that. It is also better design independently: two actors kicking one pane on
different triggers is how you manufacture kick spam.

THE SIGNAL IS A STATE TRANSITION, NOT A DURATION. Earlier designs alerted on
backlog age past a threshold, with busy-suppression. Both were discarded: a duration
threshold has to separate two populations that OVERLAP in duration (specialist chains
261s tool calls into multi-hour builds), and unconditional busy-suppression is
structurally identical to the T-2158 defect — an agent busy at every sample is
suppressed at every sample, so the detector reports healthy while mail ages without
bound, green at every step. The defining property of the failure was never "unread
for a long time"; it was "the agent reached an IDLE PROMPT while mail was unread and
did not drain". That is unambiguous, fires within samples of the transition, and
never fires on a genuinely continuously-busy agent — so it needs neither suppression
nor a ceiling to bound it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

# Alert kinds.
KIND_IDLE_WITH_UNREAD = "idle_with_unread"
KIND_NEVER_RECEIVED = "never_received"
KIND_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class InboxObservation:
    """One sample of one inbox. All fields are supplied by the impure caller.

    `pane_idle` comes from `inbox_consumer.pane_idle_state()` and is THREE-state:
    True = idle at prompt, False = generating, None = UNKNOWN (unreadable pane,
    foreign substrate, modal, timeout). None must never drive an alert — the same
    contract `session_key()` documents, so a flaky query cannot cause spam.
    """

    unread: int
    in_flight: int
    oldest_unread_age_s: float | None
    pane_idle: bool | None
    ever_received: bool
    # T-2310: could the caller actually READ this inbox? False means the counts
    # above are NOT observations — they are placeholders. Defaults True so every
    # existing caller and fixture is unchanged.
    readable: bool = True
    # T-2283: is this inbox's single-consumer lease currently held by a LIVE
    # consumer? Supplied by the impure caller (read .consumer.lock, check the
    # pid). Defaults False so every existing caller and fixture is unchanged.
    consumer_lease_held: bool = False


@dataclass(frozen=True)
class InboxState:
    """Carried across samples. Owned and persisted by the caller; this module
    treats it as an opaque value, so the on-disk format can change freely."""

    idle_unread_samples: int = 0
    samples_since_alert: int | None = None
    never_received_alerted: bool = False
    unreadable_alerted: bool = False


@dataclass(frozen=True)
class Alert:
    inbox: str
    kind: str
    unread: int
    oldest_age_s: float | None
    samples: int
    text: str


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)}min"
    return f"{minutes / 60:.1f}h"


def detect(
    observations: Mapping[str, InboxObservation],
    prior: Mapping[str, InboxState],
    *,
    idle_samples_required: int,
    realert_after_samples: int,
) -> tuple[list[Alert], dict[str, InboxState]]:
    """Evaluate one sample across all inboxes.

    Returns (alerts, next_state). Pure: same inputs always give the same outputs.

    ON `idle_samples_required` — it is a PARAMETER, not a constant, and the caller
    must set it so that `idle_samples_required * sample_interval` EXCEEDS the
    maximum wake latency. Otherwise this alerts on mail that has simply not been
    kicked yet.

    That latency is governed by `mailbox_wake.decide()`, NOT by the timer period —
    reading the timer instead is a proxy error that cost us a design round. decide()
    returns "wake" on only four conditions (newer message; marker predates session
    tracking; kicked session gone; kick older than REKICK_TTL_SECONDS) and DEBOUNCES
    everything else. So an agent that goes idle after a swallowed kick, same session
    and no new mail, matches none of the first three and waits out REKICK_TTL —
    1800s by default (verified at mailbox_wake.py:111). At a 60s sample interval
    that means N >= 31; use 32.

    Note REKICK_TTL_SECONDS is ENV-OVERRIDABLE
    (`PANTHEON_MAILBOX_REKICK_TTL_SECONDS`), so 1800 is a default and not a
    constant. DERIVE N from the value actually in force rather than hardcoding it
    against the default — raising that env var without raising N silently turns this
    detector into a false-positive generator, and the alert would look exactly like
    a real stall.

    The 30-minute floor is a KNOWN CONSEQUENCE of T-2158's condition 5 being
    unbuilt, not a tuning choice. When condition 5 lands ("session went idle since
    the last kick, mail still unread -> wake"), max wake latency becomes the timer
    period (300s) and N >= 6 at the same interval. Do not preserve 32 as a derived
    number once that lands.
    """
    if idle_samples_required < 1:
        raise ValueError("idle_samples_required must be >= 1")
    if realert_after_samples < 1:
        raise ValueError("realert_after_samples must be >= 1")

    alerts: list[Alert] = []
    next_state: dict[str, InboxState] = {}

    for inbox in sorted(observations):
        obs = observations[inbox]
        state = prior.get(inbox, InboxState())

        # --- unreadable: the counts are not observations at all (T-2310) -------
        # An inbox we cannot read reported 0/0, which made ever_received False,
        # which fired the never-RECEIVED wiring alert — text that says "a kick
        # cannot fix it" about mail that is sitting right there, latched so it
        # became the last thing that inbox ever emitted. Reproduced by specialist
        # with a control: readable -> idle_with_unread; chmod 000 -> never_received
        # 60s later; then silence forever.
        #
        # Gate FIRST and return: while blind we emit one honest alert and touch
        # the staleness ledger not at all. Holding rather than resetting keeps
        # whatever evidence we had before the inbox went dark.
        if not obs.readable:
            if not state.unreadable_alerted:
                alerts.append(
                    Alert(
                        inbox=inbox,
                        kind=KIND_UNREADABLE,
                        unread=0,
                        oldest_age_s=None,
                        samples=0,
                        text=(
                            f"mailbox: {inbox} is UNREADABLE — its new/ or cur/ could not "
                            f"be listed, so depth is unknown rather than zero. This is a "
                            f"permissions/mount fault, NOT staleness and NOT a wiring "
                            f"defect; mail may be queued and invisible (T-2310)"
                        ),
                    )
                )
                state = replace(state, unreadable_alerted=True)
            next_state[inbox] = state
            continue
        # Recovered: re-arm so a second outage alerts again.
        if state.unreadable_alerted:
            state = replace(state, unreadable_alerted=False)

        # --- never_received: a DIFFERENT fault with the same symptom -----------
        # An inbox that has never had traffic is a WIRING defect, not staleness —
        # staleness logic can never surface it, because there is nothing sitting
        # there to age. Live instance: codex (T-2142/T-2144), where delivery was
        # never provisioned. Fires once per inbox; no kick will ever fix it.
        if not obs.ever_received and not state.never_received_alerted:
            alerts.append(
                Alert(
                    inbox=inbox,
                    kind=KIND_NEVER_RECEIVED,
                    unread=obs.unread,
                    oldest_age_s=obs.oldest_unread_age_s,
                    samples=0,
                    text=(
                        f"mailbox: {inbox} has NEVER received a message — this is a "
                        f"delivery-wiring defect, not staleness; a kick cannot fix it "
                        f"(cf. T-2142 codex)"
                    ),
                )
            )
            state = replace(state, never_received_alerted=True)

        # --- the staleness ledger ---------------------------------------------
        # Exactly three outcomes, and HOLD is the load-bearing one:
        #   drained                  -> RESET (and clear any alert cooldown)
        #   idle + unread            -> INCREMENT (the only thing that adds evidence)
        #   busy/unknown + unread    -> HOLD
        #
        # HOLD rather than RESET on busy is deliberate and not the obvious choice.
        # An agent idle-with-unread for many samples that then picks up unrelated
        # work has not invalidated the evidence that it REACHED IDLE without
        # draining; resetting would discard it, and an agent flickering
        # busy/idle without ever draining would never accumulate and would escape
        # detection entirely. Unknown gets the same treatment because it is not
        # evidence in either direction — strictly stronger than "does not alert".
        # T-2283 adds a FOURTH case that resolves to HOLD, and it belongs here
        # rather than as a suppression bolted on after the fact:
        #   lease held by a live consumer -> HOLD
        #
        # `consume()` takes a per-inbox EXCLUSIVE lease, calls the handler, and
        # releases it only in a `finally` AFTER the drain returns. The ACP
        # bridge's handler blocks on a full agent turn (session/prompt, up to
        # prompt_timeout — deliberately 1800s on specialist per T-1985). So for the
        # length of that turn the agent CANNOT ack its own mail: consume()
        # returns the lease-held skip, and a read-only triage pass is the only
        # action available from the main seat.
        #
        # Measured live on specialist 2026-07-29 (T-2283): lease held 47 minutes,
        # 12 unread, pane idle at a prompt — and this detector fired twice,
        # correctly by its own rules and wrongly about the world. Under a held
        # lease, "idle with unread" is NOT evidence about the transport; it is
        # evidence about the agent's TURN LENGTH. A healthy bridge delivering a
        # long turn was indistinguishable from a wedged one, so the failure
        # direction was crying wolf at the BUSIEST seat — the one least able to
        # afford a spurious kick.
        #
        # HOLD, not RESET, for the same reason busy/unknown holds: the evidence
        # that this agent previously reached idle without draining is not
        # invalidated by a lease being taken now. Nothing is discarded; the
        # ledger simply stops accumulating while the agent is structurally
        # unable to act.
        if obs.unread == 0:
            state = replace(state, idle_unread_samples=0, samples_since_alert=None)
        elif obs.pane_idle is True and not obs.consumer_lease_held:
            state = replace(state, idle_unread_samples=state.idle_unread_samples + 1)
        # else: HOLD — no change to idle_unread_samples.

        # --- alert decision ---------------------------------------------------
        # Age the cooldown BEFORE testing it, so `realert_after_samples=k` means
        # "re-alert exactly k samples later" rather than k+1. Checking before
        # incrementing is the off-by-one that reading looks right and counting
        # does not.
        if state.samples_since_alert is not None:
            state = replace(state, samples_since_alert=state.samples_since_alert + 1)

        ready = state.idle_unread_samples >= idle_samples_required
        cooled = (
            state.samples_since_alert is None
            or state.samples_since_alert >= realert_after_samples
        )
        if ready and cooled:
            alerts.append(
                Alert(
                    inbox=inbox,
                    kind=KIND_IDLE_WITH_UNREAD,
                    unread=obs.unread,
                    oldest_age_s=obs.oldest_unread_age_s,
                    samples=state.idle_unread_samples,
                    text=(
                        f"mailbox: {inbox} idle with {obs.unread} unread across "
                        f"{state.idle_unread_samples} consecutive samples "
                        f"(oldest {_format_age(obs.oldest_unread_age_s)}"
                        + (f", {obs.in_flight} in flight" if obs.in_flight else "")
                        + ") — pane was OBSERVED idle, so the agent reached a prompt "
                        "without draining"
                    ),
                )
            )
            state = replace(state, samples_since_alert=0)

        next_state[inbox] = state

    return alerts, next_state
