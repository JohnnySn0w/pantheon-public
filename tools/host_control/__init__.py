"""Harness-neutral Pantheon host-control planning package.

The public names below are re-exported LAZILY (PEP 562): importing any
submodule (in particular the per-seat executor, ``tools.host_control.
executor``) must not pull the broker journal into the process (T-2917 5b:
the executor never imports or writes the broker journal, proven at the
``sys.modules`` level, not only by source declaration). ``from
tools.host_control import BrokerJournal`` keeps working unchanged.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "BrokerJournal": "tools.host_control.journal",
    "CapabilitiesResult": "tools.host_control.contracts",
    "CapabilityGrant": "tools.host_control.contracts",
    "HostPlanService": "tools.host_control.planner",
    "HostPolicy": "tools.host_control.policy",
    "PlanEnvelope": "tools.host_control.contracts",
    "PlanResult": "tools.host_control.contracts",
    "SeatRegistry": "tools.host_control.registry",
    "SubjectContext": "tools.host_control.contracts",
}

__all__ = tuple(sorted(_EXPORTS))


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
