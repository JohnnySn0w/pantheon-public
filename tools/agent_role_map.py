"""Persona-name <-> role-id resolution for Pantheon agents.

Pantheon uses THREE parallel namespaces:

  - Persona names       (canonical for tickets): orchestrator, specialist, recovery
  - Audit role-ids      (canonical for ledger):  orchestrator, specialist, recovery
  - Instance role-keys  (portable config axis):  orchestrator, specialist, recovery

Per T-0492/msg_1661: persona names are now the canonical namespace for ticket
`target:` and `requester:` fields. Audit role-ids remain canonical for ledger
`agent:` fields (audit-chain continuity across persona renames, pinned in
CLAUDE.md).

Instance role-keys are the stable, portable axis introduced by ORG-030 (Q4).
They key `repos.yaml` grants (and future framework-layer config). The binding
from instance role-key → persona is supplied by `instance.yaml` at the repo root;
`_INSTANCE_ROLE_PERSONA_MAP` (loaded at import time) reflects that binding.

  - resolve_role(persona)             → audit role-id  (persona→ledger id; for ledger writes)
  - resolve_persona(role_or_persona)  → persona        (audit-id→persona; for ticket writes)
  - resolve_instance_role(persona_or_role_id) → instance role-key
                                                (for repos.yaml grants lookup)

Persona names are also the human-facing surface used in systemd unit instances,
inject prompts, and operator prose.

This module is the single source of truth for persona↔audit-role-id mapping
(AGENT_ROLE_MAP / _ROLE_PERSONA_MAP). The persona↔instance-role-key mapping is
derived from instance.yaml (ORG-030 Phase 1); fallback to hardcoded defaults
preserves backward compatibility in test environments without a staged manifest.

To register a new agent pair, add one entry to instance.yaml AND update the
fallback maps here + notes/agent-role-map.md.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Audit role-id maps (persona ↔ audit role-id).
# These are immutable across persona renames — audit-chain continuity (P4).
# ---------------------------------------------------------------------------

AGENT_ROLE_MAP: dict[str, str] = {
    "orchestrator": "orchestrator",
    "specialist": "specialist",
    "recovery": "recovery",
    # Peer-daemon harnesses brought into the ticketing loop 2026-06-09 (the operator
    # directive, this thread): codex (ORG-018/ORG-031 codex-CLI pilot — a live,
    # self-supervised daemon) and the-ancestor (fresh account; full claude-code
    # bringup pending — see sop/standard/pantheon-bringup.md). Identity audit
    # role-ids (no separate role-id); persona == account for both, which the
    # bringup SOP §4 explicitly permits. Registering them here makes them valid
    # ticket target:/requester: values (_valid_target_values()).
    "codex": "codex",
    "the-ancestor": "the-ancestor",
}

# Inverse map: audit role-id → persona name (for ticket target/requester normalization).
# Derived from AGENT_ROLE_MAP; recovery is its own inverse.
_ROLE_PERSONA_MAP: dict[str, str] = {v: k for k, v in AGENT_ROLE_MAP.items()}


# ---------------------------------------------------------------------------
# Instance role-key maps (persona ↔ instance role-key).
# Loaded from instance.yaml at import time; fallback to hardcoded defaults.
# Instance role-keys are the portable axis from ORG-030 (hyphen-separated).
# ---------------------------------------------------------------------------

# Hardcoded fallback: matches the instance.yaml binding for pantheon-vps-0.
# Must stay in sync with instance.yaml when adding new agents.
_FALLBACK_PERSONA_TO_INSTANCE_ROLE: dict[str, str] = {
    "orchestrator": "orchestrator",
    "specialist": "specialist",
    "recovery": "recovery",
}


def _load_instance_role_map(manifest_path: Path) -> dict[str, str]:
    """Return persona→instance-role-key map from instance.yaml `roles:` stanza.

    Falls back to _FALLBACK_PERSONA_TO_INSTANCE_ROLE if the file is absent,
    unreadable, or structurally malformed. Fail-safe: a missing manifest in a
    test environment must not block the entire import chain.
    """
    try:
        import yaml  # lazy import; matches ticket_lint.py pattern

        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        roles = data.get("roles") if isinstance(data, dict) else None
        if not isinstance(roles, dict):
            return dict(_FALLBACK_PERSONA_TO_INSTANCE_ROLE)
        result: dict[str, str] = {}
        for role_key, binding in roles.items():
            if isinstance(binding, dict):
                persona = binding.get("persona")
                if isinstance(persona, str) and persona:
                    result[persona] = role_key
        if not result:
            return dict(_FALLBACK_PERSONA_TO_INSTANCE_ROLE)
        return result
    except Exception:
        return dict(_FALLBACK_PERSONA_TO_INSTANCE_ROLE)


# Resolve instance.yaml relative to this module's parent (repo root).
_MODULE_DIR = Path(__file__).resolve().parent
_INSTANCE_YAML = _MODULE_DIR.parent / "instance.yaml"

# persona → instance role-key  (e.g. "specialist" → "specialist")
_PERSONA_TO_INSTANCE_ROLE: dict[str, str] = _load_instance_role_map(_INSTANCE_YAML)

# instance role-key → persona  (inverse; e.g. "specialist" → "specialist")
_INSTANCE_ROLE_TO_PERSONA: dict[str, str] = {v: k for k, v in _PERSONA_TO_INSTANCE_ROLE.items()}


# ---------------------------------------------------------------------------
# Public API — all three namespaces.
# ---------------------------------------------------------------------------


def resolve_role(persona: str) -> str:
    """Return the audit role-id for *persona*, or *persona* itself if not in the map.

    Identity fallback means unknown persona names pass through unchanged,
    preserving backward compatibility for any agent not yet registered.

    This returns the AUDIT role-id (underscored: orchestrator / specialist / recovery),
    which is the canonical form for ledger `agent:` fields. Do NOT use this for
    repos.yaml grants lookup — use resolve_instance_role() instead.
    """
    return AGENT_ROLE_MAP.get(persona, persona)


def resolve_persona(role_or_persona: str) -> str:
    """Return the persona name for *role_or_persona*, or the input itself if not in the map.

    Inverse of resolve_role(). Used to normalize ticket `target:` and
    `requester:` fields from audit role-ids to persona names on write (T-0492/msg_1661).

    Examples:
        resolve_persona("orchestrator") -> "orchestrator"
        resolve_persona("specialist")      -> "specialist"
        resolve_persona("recovery")              -> "recovery"   (identity)
        resolve_persona("orchestrator")             -> "orchestrator"  (already persona; passthrough)
        resolve_persona("operator")           -> "operator" (not in map; passthrough)

    Identity fallback preserves backward compatibility for unregistered names.
    Ledger `agent:` fields should NOT be passed through this function — they
    remain audit-role-id-canonical for audit-chain continuity (use resolve_role there).
    """
    return _ROLE_PERSONA_MAP.get(role_or_persona, role_or_persona)


def resolve_instance_role(persona_or_audit_role: str) -> str:
    """Return the instance role-key for *persona_or_audit_role*.

    Used for repos.yaml grants lookup (ORG-030 Phase 1). Grants are keyed on
    instance role-keys (orchestrator / specialist / recovery) —
    the portable axis from ORG-030.

    Accepts both persona names AND audit role-ids (normalizes via resolve_persona
    first so callers don't need to pre-normalize ticket target fields).

    Examples:
        resolve_instance_role("specialist")          -> "specialist"
        resolve_instance_role("specialist")   -> "specialist"  (audit-id alias)
        resolve_instance_role("orchestrator")          -> "orchestrator"
        resolve_instance_role("orchestrator") -> "orchestrator"
        resolve_instance_role("recovery")           -> "recovery"
        resolve_instance_role("operator")        -> "operator"  (not in map; passthrough)

    Identity fallback preserves behavior for unregistered names.
    """
    # Step 1: normalize audit role-id → persona (e.g. specialist → specialist).
    persona = _ROLE_PERSONA_MAP.get(persona_or_audit_role, persona_or_audit_role)
    # Step 2: persona → instance role-key.
    return _PERSONA_TO_INSTANCE_ROLE.get(persona, persona)
