"""pantheon-health/v1 leg for the PersonaController1 emitter (T-5137, ORG-068 E10).

Reports one seat against the harness-manager invariant list
(`tools.germinate.components.harness_manager_verify`, ORG-080 A-6) in the
five-state shape (ready / degraded / unhealthy / not_configured / suppressed).
Pure leaf: never imports `tools.runtime_controller`; `tools.germinate` is imported
lazily so a controller with the flag off never loads it.

DARK, honestly: flag-on today reads ``unhealthy`` because most invariants are
unsourced; activation waits on fact sourcing. A fact is supplied ONLY where this
process holds an honest source whose meaning matches the invariant body exactly;
every other invariant has no observation, evaluates to ``alarm`` ("could not ask"),
and `rollup` maps it to its `on_fail` severity (ORG-099 section 8: alarm is never a
pass). Do not fabricate facts here -- source them, then add them to
`_source_facts`.

Sourced today:
  * ORG-099/ticket-persona-registered -- the axis check is literally
    ``persona in AGENT_ROLE_MAP``; we evaluate the same expression.
Deliberately NOT sourced: ORG-099/mailbox-inbox-exists (the axis needs the hub dir
AND a persona-account ACL grant via getfacl; a bare stat is a weaker claim).

Fail-open: any exception yields Status ``degraded`` with reason
"health leg evaluation failed"; the nine base properties are computed elsewhere and
are unaffected.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping

#: Mirrors `tools.host_control.health.CONTRACT_REVISION`. Mirrored, not imported:
#: that module pulls persona_installer and friends into the controller. A test pins
#: the two together.
CONTRACT_REVISION = "pantheon-health/v1"
FAILED_REASON = "health leg evaluation failed"
_SOURCED_PERSONA_REGISTERED = "ORG-099/ticket-persona-registered"


def invariant_list_ref() -> str:
    from tools.germinate.components import harness_manager_verify as v

    return f"{v.LIST_ID}@{v.list_digest()}"


def _load_binding(persona: str, manifest_path: Path | None = None) -> Any:
    """Primary PersonaBinding, or None when it cannot be loaded (no seat context)."""
    try:
        from tools import persona_bringup

        return persona_bringup.load_binding(manifest_path or persona_bringup.DEFAULT_MANIFEST, persona)
    except Exception:
        return None


def _is_muted(persona: str) -> bool:
    from tools import notify_policy

    return notify_policy.is_muted(persona)


def _default_role_map() -> Mapping[str, str]:
    from tools.agent_role_map import AGENT_ROLE_MAP

    return AGENT_ROLE_MAP


def _source_facts(persona: str, role_map: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    return {
        _SOURCED_PERSONA_REGISTERED: {"verdict": "PASS" if persona in role_map else "FAIL"},
    }


def build_observation(
    persona: str,
    binding: Any,
    *,
    role_map: Mapping[str, str] | None = None,
    muted_fn: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """harness_manager_verify observation for this seat (see that module's docstring)."""
    observation: dict[str, Any] = {
        "facts": _source_facts(persona, _default_role_map() if role_map is None else role_map),
        "suppressed": (muted_fn or _is_muted)(persona) is True,
    }
    if binding is not None:
        observation["context"] = {
            "adapter": binding.selected_harness,
            "role": binding.role,
            "systemd_runtime": bool(os.environ.get("INVOCATION_ID")),
        }
    return observation


def _evaluate(observation: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from tools.germinate.components import harness_manager_verify as v

    results = v.evaluate(v.load_list(), observation)
    return results, v.rollup(results, observation)


def project(results: list[Mapping[str, Any]], rolled: Mapping[str, Any], ref: str) -> dict[str, Any]:
    subs: dict[str, str] = {}
    for r in results:
        if r["status"] == "pass":
            subs[r["id"]] = "ready"
        elif r["status"] == "vacuous":
            subs[r["id"]] = "not_configured"
        else:  # fail | alarm
            subs[r["id"]] = r["on_fail"]
    return {
        "ContractRevision": CONTRACT_REVISION,
        "Status": rolled["state"],
        "Subcomponents": subs,
        "Reasons": {str(k): str(v) for k, v in rolled["reasons"].items()},
        "Revisions": {"invariant_list": ref},
        "InvariantListRef": ref,
        "SecurityDrift": bool(rolled["security_drift"]),
    }


def _failed(ref: str) -> dict[str, Any]:
    return {
        "ContractRevision": CONTRACT_REVISION,
        "Status": "degraded",
        "Subcomponents": {},
        "Reasons": {"health-leg": FAILED_REASON},
        "Revisions": {},
        "InvariantListRef": ref,
        "SecurityDrift": False,
    }


def snapshot(
    persona: str,
    *,
    binding: Any = None,
    manifest_path: Path | None = None,
    role_map: Mapping[str, str] | None = None,
    muted_fn: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """The seven appended PersonaController1 properties. Fresh on every call, never raises."""
    ref = ""
    try:
        ref = invariant_list_ref()
        if binding is None:
            binding = _load_binding(persona, manifest_path)
        observation = build_observation(persona, binding, role_map=role_map, muted_fn=muted_fn)
        results, rolled = _evaluate(observation)
        return project(results, rolled, ref)
    except Exception:
        return _failed(ref)
