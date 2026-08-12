"""Reusable framework for proving a newly-brought-up Pantheon daemon works.

This package is the harness/persona-neutral core described by T-2556: a
runner-agnostic scaffolding (:mod:`tools.bringup_acceptance.types`), a
tamper-evident evidence recorder generalized from T-2382's live-acceptance
helper (:mod:`tools.bringup_acceptance.recorder`), an on-disk evidence
schema (:mod:`tools.bringup_acceptance.evidence`), and a closure-matrix
emitter (:mod:`tools.bringup_acceptance.closure`).

Per-subsystem acceptance modules (mailbox delivery, Telegram routing,
harness-swap, keep-going kicker, and so on) are later phases; this phase
only stands up the shapes every module plugs into.  Nothing here talks to
live tmux, systemd, or mailbox state.
"""

from __future__ import annotations
