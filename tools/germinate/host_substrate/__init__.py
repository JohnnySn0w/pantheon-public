"""host-substrate root helper (ORG-080 §Build plan (c); T-5084, germination tranche GE).

EXECUTOR-TRUST code: this package actuates. It is the generalization of the
ORG-052 narrow root helper (T-2918) and the supervised root bootstrap (T-2920)
behind the ORG-080 component contract, kept small and argv-only.

* ``families``  -- the CLOSED root op enum (exactly the ORG-082 §1 substrate
  families), the typed per-family parameters, and the server-resolved manifest.
* ``helper``    -- ``RootHelper``: context admission (ORG-082 §1, ORG-080 A-3),
  resolution of every value from the manifest, sudoers discipline (ORG-052 §8).
* ``ports``     -- ``SimulatedSubstratePort`` (the only one wired) and the DARK
  ``HostSubstratePort`` (fixed argv over an injected ``CommandRunner``; its
  constructor refuses while ``LIVE_ACTUATION_ENABLED`` is False).
* ``invariants`` -- the CURRENT verify invariant list (co-author recovery; D15 approved)
  and its evaluator, UNWIRED (engine seam gap S1, T-5135).
* ``gate32a``   -- the ORG-082 gate 32a same-registry-digest check, sim tier.

Engine seam gaps this package works around without editing the engine
(reported on T-5135, not patched here):

* S5 backend selection: ``Engine.from_checkout`` / the CLI build only the
  simulated backend, so no engine-selectable root backend exists.
* S6 invocation context: the engine carries no operator/scheduled/daemon
  context and never reads ``execution_scope``; A-3 and staged cutover are
  enforced here, component-side, only.
* S7 privilege at actuation: the executor dispatches by kind and never reads
  ``privilege``; the root/seat split is enforced here, component-side, only.
"""
