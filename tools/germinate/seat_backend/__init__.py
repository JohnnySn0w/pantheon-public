"""The live SEAT-scope host backend for the germination engine (T-5940, seam S5). DARK.

EXECUTOR-TRUST code that would actuate a seat's own systemd --user manager and its
own state files, as the seat's own account (never root, never another account).
It lives OUTSIDE tools/germinate/engine/ (the engine safety lint bans process reach
there) and is injected through ``Engine.from_checkout(backend_factory=...)``.

* ``ports.py``: the per-seat literal pin ``LIVE_ACTUATION_ENABLED``, the fixed argv
  builders, and ``LiveSeatPort`` (refuses to construct while its seat is pinned off).
* ``backend.py``: ``LiveSeatBackend`` (the engine's ``HostBackend``), its seat
  binding, and ``live_seat_factory``.
* ``install_handlers.py`` (T-6167 PR-2): the live handlers for the 37 persona
  installer kinds, wrapping ``persona_installer.LocalHostBackend`` (ORG-080 D-I).

Everything stays dark while a seat's pin is False; the harness-manager run-state
live handlers are T-5867 scope.
"""
