"""Per-persona optional acceptance modules (T-2556 phase 11 / T-2561).

This package holds ONLY persona plug-in files, one per persona:
``tools/bringup_acceptance/optionals/<persona>.py``. There is no code in
this ``__init__`` on purpose -- :mod:`tools.bringup_acceptance.registry`
discovers plug-ins by dotted-import, never by walking this package's
contents, so a persona with no file here is simply "no optionals" (the
bare-daemon, core-only case), not an error.

The plug-in contract every ``<persona>.py`` file must implement::

    def build_optional_modules() -> tuple[AcceptanceModule, ...]:
        ...

Each returned module must be an
:class:`~tools.bringup_acceptance.types.AcceptanceModule` (declare/prove/
restore/report) with ``tier == "optional"``. See
:mod:`tools.bringup_acceptance.registry` for the discovery + applies|absent
self-declaration lifecycle that runs against whatever this returns.

No persona files ship in this ticket (T-2561) -- Recovery/Specialist/Orchestrator plug-ins
are T-2567/T-2565/T-2566, built against this contract as separate tickets.
"""

from __future__ import annotations
