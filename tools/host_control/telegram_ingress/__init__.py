"""Separately supervised Telegram human-approval ingress (ORG-052 §5
"Human authority ingress", T-2919).

Its own process, its own Unix identity (``pantheon-hostapproval-ingress``),
its own bot credential (an ORG-051 lease), and its own provider cursor.
Flow: the approval service records ONE pending human card (digest, target,
families, redacted effects, expiry, single-use CSPRNG nonce) and calls
``HostApprovalTelegram1.Ingress.Present`` (approval uid only); this ingress
sends one bounded plain-text card whose two buttons carry a SHA-256 tag over
(review handle, nonce, decision); it long-polls ``getUpdates`` (no listening
socket) and accepts only a callback passing ``core.CALLBACK_CHECKS``; it
then calls ``HostApproval1.HumanIngress.HumanDecision`` (ingress uid only),
where the service maps the uid to the provider + human subject from
root-owned config and consumes the nonce atomically with the decision.

Isolation: this package imports NOTHING from the agent-facing Telegram
integrations and — deliberately — nothing from
``tools.host_control.contracts`` or the approval package either: that
module imports ``tools.persona_installer``, whose import chain loads
``tools.codex_telegram_bridge`` and ``tools.mailbox``. The import closure is
proven at the ``sys.modules`` level by
tools/test_host_control_telegram_ingress.py.

Gate 8 (ORG-052) — proven here vs unsupported:

- PROVEN (fixture / private bus): the E14 spoof matrix red then the real
  callback green; nonce single use, idempotent replay and the three crash
  windows; HumanDecision only from the ingress uid and Present only from
  the approval uid; E21 leak canaries (logs, argv, environ, staged units,
  adapter output, bus signals, provider requests); E22 mailbox/ticket
  content never mints; credential + cursor isolation.
- FIXTURE ONLY: Bot API shapes (loopback fake, reconciled with the
  coordinator's verified reading of the Bot API reference).
- UNSUPPORTED until live proof (T-2924 / HO, gate 38): real Telegram
  delivery and callback authenticity, real getUpdates offset semantics,
  rate limits and network faults; keyboard removal via an edit with an
  empty inline keyboard (correctness never depends on it); Telegram's
  exact text normalization and length counting (the fake trims surrounding
  whitespace and counts UTF-16 code units; the card digest is taken from
  the RETURNED text so normalization cannot break a genuine tap); the
  approval-side Present/HumanDecision stall is recovered, not prevented
  (async calls are the T-2920 main-loop wiring); the real bot,
  token, ingress account, system-bus policy and unit hardening (staged
  only); the operator tapping a real card (``interactive-required``).
"""
