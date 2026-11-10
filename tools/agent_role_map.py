"""Persona-name <-> role-id resolution for Pantheon agents.

Pantheon uses THREE parallel namespaces:

  - Persona names       (canonical for tickets): orchestrator, specialist, recovery
  - Audit role-ids      (canonical for ledger):  head_engineering_orchestrator, head_research_orchestrator, head_system_orchestrator
                                                 (specialist = specialist's retired audit-id, kept as a legacy alias)
  - Instance role-keys  (portable config axis):  head-engineering-orchestrator, specialist, head-system-orchestrator,
                                                 peer-daemon-*, engineering-orchestrator-1/-2, head-memory-orchestrator

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

A FOURTH axis, OS account → persona, is derived from the same instance.yaml
bindings (T-2256). It is the axis every hand-carried fleet list in tools/ was
copying by hand, and the one every stale copy went stale on:

  - account_persona(account)     → persona | None   (None on unknown: an OS
                                    account is not a persona, so identity
                                    passthrough here manufactures a persona
                                    nothing carries — see the docstring)
  - account_persona_map()        → {account: persona}  (copy)
  - fleet_accounts()             → sorted tuple of every bound account

Persona names are also the human-facing surface used in systemd unit instances,
inject prompts, and operator prose.

This module is the single source of truth for persona↔audit-role-id mapping
(AGENT_ROLE_MAP / _ROLE_PERSONA_MAP). The persona↔instance-role-key mapping is
derived from instance.yaml (ORG-030 Phase 1); fallback to hardcoded defaults
preserves backward compatibility in test environments without a staged manifest.

To register a new agent pair, add one entry to instance.yaml AND update the
fallback maps here. (The prose doc is `sop/standard/agent-role-map.md`; the old
`notes/agent-role-map.md` path in this docstring was stale — no such file exists.
That doc covers the persona<->AUDIT-role-id table only, not instance role-keys.)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Audit role-id maps (persona ↔ audit role-id).
# These are immutable across persona renames — audit-chain continuity (P4).
# ---------------------------------------------------------------------------

AGENT_ROLE_MAP: dict[str, str] = {
    "orchestrator": "head_engineering_orchestrator",
    # T-3807 (operator-ratified roles vocabulary): specialist's forward audit role-id is
    # now head_research_orchestrator. `specialist` — the prior audit id — is RETAINED
    # as a backward-resolving LEGACY INPUT-ALIAS in PERSONA_ALIASES below (D1, P4
    # audit-continuity): historical [specialist] ledger entries,
    # resolve_persona("specialist"), and watch_runner --requester specialist must
    # still resolve to specialist. Forward normalization emits head_research_orchestrator;
    # the alias only resolves the legacy token backward.
    "specialist": "head_research_orchestrator",
    "recovery": "head_system_orchestrator",
    # Peer-daemon harnesses brought into the ticketing loop 2026-06-09 (the operator
    # directive, this thread): codex (ORG-018/ORG-031 codex-CLI pilot — a live,
    # self-supervised daemon). Registering it here makes it a valid ticket
    # target:/requester: value (_valid_target_values()). The-ancestor was also
    # registered then; it was retired (ORG-087 sec. 4 step 4, T-3421) and removed from
    # every live map here. Historical done tickets still carry `target: the-ancestor`
    # but done/ is never linted (ticket_lint.STATES_TO_LINT), and no ledger entry
    # carries it as an agent id, so no legacy alias is needed.
    # ORG-100 (T-3814) gave codex the ratified role-class audit-id head_auditor
    # (identity→role normalize).
    # codex's OLD audit-id IS its persona name, so historical [codex] ledger/tickets
    # ride persona-name identity passthrough (resolve_persona("codex") -> "codex"
    # unchanged) — no legacy alias needed (contrast orchestrator, whose old id != persona).
    "codex": "head_auditor",
    # Archivist — the second head (ORG-042 two-hemisphere topology), resident
    # on the `exocortex` account (persona ≠ account; account→persona lives in
    # mailbox_mcp._USER_TO_PERSONA per T-1824). ORG-100 (T-3814) gave her the
    # ratified role-class audit-id head_memory_orchestrator (was an identity
    # audit-id): registering the persona makes her a valid
    # ticket target:/requester: (SOP §10 step 8); it enables, not obligates —
    # her residency remains mailbox-first per ORG-042. Registered by recovery
    # 2026-07-20 (parity audit, the operator TG 3827).
    #
    # SUPERSEDED 2026-07-21 (corrected T-2318). This comment used to end: "her
    # instance role-key / repos.yaml binding stays deliberately unbound (ORG-042:
    # her config-repo shape is hers + the operator's call)". That was true for about
    # one day and false for the eight after it. She IS bound:
    #   resolve_instance_role("archivist")           -> head-memory-orchestrator
    #   _FALLBACK_PERSONA_TO_INSTANCE_ROLE           -> head-memory-orchestrator  (this file)
    #   repos.yaml                                    -> head-memory-orchestrator grants
    # (role-key renamed from `second-head` by the operator's T-3807 roles vocabulary.)
    # Bound by T-1922 / 7946c56d8 (2026-07-21, example-org); the stale claim was
    # c5b2ddad5 (2026-07-20, recovery).
    #
    # Recorded rather than overwritten, per the instance.yaml header's precedent,
    # because of WHERE it sat: a reader deciding whether archivist should have a
    # role-key binding would find a comment calling its absence DELIBERATE, three
    # lines above the map that binds her. A clean edit would leave nothing to warn
    # the next person that this file's prose has drifted from its own data before.
    # The ORG-042 residency point above is kept because it is still true and is
    # the useful half — it is only the binding clause that was wrong.
    "archivist": "head_memory_orchestrator",
    # Mason + Artisan — engineering-orchestrator seats (ORG-043), self-named 2026-07-21,
    # resident on suborch1/suborch2 (persona != account; account->persona in
    # mailbox_mcp._USER_TO_PERSONA + ticket._OS_USER_PERSONA per T-1896).
    # ORG-100 (T-3814) gave BOTH the shared ratified role-class audit-id
    # engineering_orchestrator (were identity audit-ids). The reverse map is
    # therefore non-injective (engineering_orchestrator -> mason OR artisan):
    # ACCEPTED by design (two seats, one role-class — operator-ratified). No reverse
    # owner-alias is registered for it — ENFORCED (T-4044): engineering_orchestrator
    # is in NON_INJECTIVE_ROLE_IDS and thus EXCLUDED from _ROLE_PERSONA_MAP below, so
    # resolve_persona() returns it unchanged and ticket.py refuses a bare target of it
    # (pointing at role:engineering_orchestrator) instead of silently picking artisan.
    # The legacy [mason]/[artisan] ledger tokens ride persona-name identity passthrough
    # (their OLD audit-ids ARE their persona names).
    # Registering makes them valid ticket target:/requester: values for the retask lane.
    "mason": "engineering_orchestrator",
    "artisan": "engineering_orchestrator",
    # Watchman (T-4884 B6): the codex-primary recovery seat on the codex-fallback
    # account, an ORG-100 §1 REGULAR in Recovery's System domain. Its audit role-id is the
    # regular role-class system_orchestrator (Recovery holds the head_ id). Single holder
    # today, so the reverse map stays injective; a second system_orchestrator seat
    # would make it non-injective exactly as engineering_orchestrator is (T-4044).
    "watchman": "system_orchestrator",
}

# T-4044: role-ids mapped by MORE THAN ONE persona (non-injective) — currently
# `engineering_orchestrator` (mason + artisan, ORG-100/T-3814). A naive
# `{v: k for k, v in AGENT_ROLE_MAP.items()}` reverse map would silently keep
# whichever persona was registered LAST (artisan), so `resolve_persona(
# "engineering_orchestrator")` misrouted to one arbitrary seat, and a bare
# `--target engineering_orchestrator` normalized to artisan with no warning. These
# ids have no single canonical persona, so they must NOT be reverse-mapped.
_ROLE_ID_VALUES: list[str] = list(AGENT_ROLE_MAP.values())
NON_INJECTIVE_ROLE_IDS: frozenset[str] = frozenset(
    v for v in _ROLE_ID_VALUES if _ROLE_ID_VALUES.count(v) > 1
)

# Inverse map: audit role-id → persona name (for ticket target/requester
# normalization). Derived from AGENT_ROLE_MAP; recovery is its own inverse. Built from
# INJECTIVE values ONLY (T-4044): a non-injective role-class id is deliberately
# absent, so `resolve_persona()` returns it UNCHANGED (passthrough) rather than
# picking an arbitrary seat — callers (ticket.py) refuse a bare non-injective
# role-class target and point at the ORG-088 `role:<R>` pool form instead.
_ROLE_PERSONA_MAP: dict[str, str] = {
    v: k for k, v in AGENT_ROLE_MAP.items() if v not in NON_INJECTIVE_ROLE_IDS
}


def personas_for_role_id(role_id: str) -> list[str]:
    """The persona seats registered under an audit role-id, sorted. Length > 1
    iff `role_id in NON_INJECTIVE_ROLE_IDS` (e.g. engineering_orchestrator ->
    ['artisan', 'mason']). Used to name the seats in a non-injective refusal."""
    return sorted(k for k, v in AGENT_ROLE_MAP.items() if v == role_id)


# ---------------------------------------------------------------------------
# Persona ALIASES — an alternate NAME for an already-registered persona, not a
# new identity. Distinct from the audit-role-id ↔ persona axis above: a role-id
# (head_engineering_orchestrator) and its persona (orchestrator) are two names the map already
# pairs; an alias is a THIRD kind of name that collapses onto a persona which
# already owns its role-id + account binding.
#
#   consultant → codex : consultant is the SELF-NAME of the codex peer-daemon
#     (project_consultant_is_codex_persona / reference_consultant_rename_doc_scope). codex
#     is the OS account + harness/CLI (ORG-018/ORG-031); consultant is the persona it
#     named itself. A ticket can legitimately carry requester/target: consultant
#     (e.g. T-3387), so consultant must be a VALID ticket target AND must normalize to
#     the codex account EVERYWHERE. Modeled as an alias, NOT an AGENT_ROLE_MAP
#     entry: codex already holds the audit role-id, instance-role, and account
#     binding; a parallel `consultant` persona would fork that identity and, lacking
#     an account binding, trip the account-map collision/None guards.
#     (T-3478: without this, ticket_lint's retarget-to-originator bounce of a
#     consultant-requested ticket shelled `ticket.py retarget --to consultant`, which
#     raised `unknown target 'consultant'`, so pantheon-ticket-lint exited 1 hourly.)
#
# To add an alias: one entry here. It is folded into _valid_target_values() (in
# ticket.py, via PERSONA_ALIASES keys) and into all three resolve_* functions
# below (via _dealias), so an alias never survives into a ticket target/
# requester, a ledger agent-id, or a repos.yaml grants lookup.
# ---------------------------------------------------------------------------
#   specialist → specialist : LEGACY INPUT-ALIAS (T-3807, D1). specialist was specialist's
#     AUDIT role-id before the operator-ratified roles rename made specialist's forward audit
#     id head_research_orchestrator (AGENT_ROLE_MAP above). It is retained here — not
#     hard-purged — for P4 audit-continuity: historical ledger entries carry
#     [specialist], and live consumers still feed the token backward
#     (resolve_persona("specialist") → specialist, watch_runner --requester specialist,
#     the s3star_audit_runner reverse map). Modeled as an alias, NOT a resurrected
#     AGENT_ROLE_MAP entry: specialist already holds the forward audit id, instance-role, and
#     account binding; a parallel specialist→persona pair would fork that identity.
#     Forward normalization never EMITS specialist (resolve_role("specialist") →
#     head_research_orchestrator); the alias only resolves the legacy token when it
#     arrives as input.
#   orchestrator → orchestrator : LEGACY INPUT-ALIAS (T-3814, D1 pattern). orchestrator
#     was orchestrator's AUDIT role-id before the operator-ratified ORG-100 rename made orchestrator's
#     forward audit id head_engineering_orchestrator (AGENT_ROLE_MAP above). It is retained
#     here — not hard-purged — for P4 audit-continuity: orchestrator's OLD audit-id is NOT his
#     persona name (unlike recovery/codex/archivist/mason/artisan, whose old audit-ids ARE their
#     persona names and so ride persona-name identity passthrough), so without this alias
#     resolve_persona("orchestrator") would break historical [orchestrator] ledger
#     entries and tickets. Modeled as an alias, NOT a resurrected AGENT_ROLE_MAP entry: orchestrator
#     already holds the forward audit id, instance-role, and account binding; a parallel
#     orchestrator→persona pair would fork that identity. Forward normalization never
#     EMITS orchestrator (resolve_role("orchestrator") → head_engineering_orchestrator); the
#     alias only resolves the legacy token when it arrives as input.
PERSONA_ALIASES: dict[str, str] = {
    "consultant": "codex",
    "specialist": "specialist",
    "orchestrator": "orchestrator",
}


def _dealias(name: str) -> str:
    """Collapse a persona ALIAS onto its canonical persona (consultant → codex);
    every non-alias name passes through unchanged. Applied as the FIRST step of
    the resolve_* functions so an alias is never emitted downstream."""
    return PERSONA_ALIASES.get(name, name)


def valid_target_values() -> set[str]:
    """The set of names accepted as a ticket `target:`/`requester:` — persona
    names + audit role-ids + persona aliases + `operator`. Single source of truth
    for ticket.py's target validation (previously it unioned the map keys/values
    inline and so did not know about aliases — the T-3478 gap)."""
    return set(AGENT_ROLE_MAP.keys()) | set(AGENT_ROLE_MAP.values()) | set(PERSONA_ALIASES.keys()) | {"operator"}


# ---------------------------------------------------------------------------
# Instance role-key maps (persona ↔ instance role-key).
# Loaded from instance.yaml at import time; fallback to hardcoded defaults.
# Instance role-keys are the portable axis from ORG-030 (hyphen-separated).
# ---------------------------------------------------------------------------

# Hardcoded fallback: matches the instance.yaml binding for pantheon-vps-0.
# Must stay in sync with instance.yaml when adding new agents.
#
# This is a SYNC MIRROR, not the source of truth: instance.yaml is read at import
# time below and wins whenever it is present and well-formed. This map is only
# reached in environments without a staged manifest. tools/test_instance_role_grants.py
# enforces the sync (T-1922) — previously the "must stay in sync" line above was a
# comment with nothing checking it.
_FALLBACK_PERSONA_TO_INSTANCE_ROLE: dict[str, str] = {
    "orchestrator": "head-engineering-orchestrator",
    "specialist": "specialist",
    "recovery": "head-system-orchestrator",
    # ORG-043 seats + ORG-042 head-memory seat, bound 2026-07-21 (T-1922); role-keys
    # carry the operator's T-3807 ratified roles vocabulary. Kept in exact sync with
    # instance.yaml's `roles:` keys (test_instance_role_grants enforces both directions).
    "mason": "engineering-orchestrator-1",
    "artisan": "engineering-orchestrator-2",
    "archivist": "head-memory-orchestrator",
    # Peer daemons (T-2281): role-keys copied from instance.yaml's `roles:` stanza.
    # They were absent here even after T-2256 completed the SIBLING mirror
    # (_FALLBACK_PERSONA_TO_BINDING) to all 8, which left the two mirrors of the
    # same manifest sitting at different completeness for no stated reason. Same
    # shape of gap, same consequence: harmless while the manifest loads, and in a
    # manifest-less env resolve_instance_role("codex") falls through to IDENTITY
    # ("codex", not "peer-daemon-codex") — the exact T-1922 silent-deny path, where
    # the repos.yaml grants lookup misses and ticket_lint reports a confusing
    # "no grant". It also made this file's own Guard A (every AGENT_ROLE_MAP persona
    # has a role-key) pass or fail depending on whether instance.yaml was present.
    # The sync guard checked fallback→manifest but not manifest→fallback; T-2281
    # adds that direction, as T-2256 did for the sibling.
    "codex": "peer-daemon-codex",
    "watchman": "system-orchestrator-1",  # T-4884 B6
}


# ---------------------------------------------------------------------------
# ONE parse of instance.yaml per process (T-4279).
#
# Measured by impl-t4230: this module used to yaml.safe_load the ~50KB manifest
# THREE times at import with the pure-Python SafeLoader (~0.34s+ CPU), a cost paid
# by every tool start that imports it (and ~5x per shell-suite SUT run). Two fixes:
#   1. libyaml's CSafeLoader when PyYAML was built with it, else SafeLoader. Parse
#      PARITY with SafeLoader is proven on the live instance.yaml by
#      tools/test_agent_role_map_yaml_cache.py, not assumed.
#   2. A per-process cache keyed by CONTENT: the file is READ on every call (a
#      50KB read is microseconds; the parse is the expensive part) and re-parsed
#      only when its text differs from the cached text. Freshness is therefore
#      exactly what it was before caching: current_mailbox_hub() (T-2693: a
#      long-lived MCP server must see a hub move) and the fail-closed membership
#      projection read (_resolve_membership: "freshness over caching") see every
#      edit. A stat-identity key (dev/ino/mtime_ns/size) was tried first and
#      rejected in review: a same-size in-place rewrite within one mtime tick
#      (e.g. a fixed-length sha256 expected_digest edit) served stale data.
#
# SHARED, READ-ONLY result: _parse_manifest() returns the cached object itself,
# not a copy (a deepcopy costs ~5ms per call, i.e. a large fraction of the libyaml
# parse it would guard). Every caller is a private loader in this module that only
# READS the document and builds fresh containers from it; the one loader that hands
# a sub-mapping out (_load_membership_projection) copies it. Do not mutate the
# returned object; the cache-integrity test pins that no public accessor leaks it.
# ---------------------------------------------------------------------------

_MANIFEST_CACHE: dict[str, tuple[str, object]] = {}  # abspath -> (text, parsed)


def _yaml_safe_loader_class():
    """libyaml-backed CSafeLoader when available, else the pure-Python SafeLoader."""
    import yaml  # lazy import: a missing PyYAML stays a caller-handled error

    return getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader


def _parse_manifest(manifest_path: Path) -> object:
    """Parse *manifest_path* as YAML (safe loader); re-parse only when its text changed.

    Reads the file on EVERY call and compares the text to the cached text, so the
    result is always what an uncached read+parse would return right now. Raises
    exactly what the uncached read+parse would (OSError on a missing/unreadable
    file or a symlink loop, yaml.YAMLError on bad YAML, UnicodeDecodeError), so
    each caller's existing fail-soft (or fail-closed) handling is unchanged; a
    failed parse is never cached. The returned object is SHARED with the cache:
    treat it as read-only."""
    import yaml  # lazy import

    path = Path(manifest_path)
    text = path.read_text(encoding="utf-8")
    # os.path.abspath, NOT Path.resolve(): no filesystem access, so it cannot turn
    # a symlink-loop OSError into RuntimeError (callers catch OSError). Symlinked
    # aliases of one file just get separate entries, which is harmless.
    key = os.path.abspath(path)
    hit = _MANIFEST_CACHE.get(key)
    if hit is not None and hit[0] == text:
        return hit[1]
    data = yaml.load(text, Loader=_yaml_safe_loader_class())
    _MANIFEST_CACHE[key] = (text, data)
    return data


def load_instance_document(manifest_path: Path) -> object:
    """PUBLIC: the parsed YAML document at *manifest_path*, as a private DEEP COPY
    the caller may mutate or embed freely. Same parse cache + CSafeLoader as the
    loaders here (T-4279), so a tool that already imported this module pays a
    ~5ms copy instead of a second full parse. Raises what read+parse would
    (OSError / yaml.YAMLError / UnicodeDecodeError)."""
    import copy

    return copy.deepcopy(_parse_manifest(manifest_path))


def _load_instance_role_map(manifest_path: Path) -> dict[str, str]:
    """Return persona→instance-role-key map from instance.yaml `roles:` stanza.

    Falls back to _FALLBACK_PERSONA_TO_INSTANCE_ROLE if the file is absent,
    unreadable, or structurally malformed. Fail-safe: a missing manifest in a
    test environment must not block the entire import chain.
    """
    try:
        data = _parse_manifest(manifest_path)  # T-4279: cached, CSafeLoader
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


# ---------------------------------------------------------------------------
# Instance role BINDINGS (persona → the role's account + restart-config fields).
# T-2071/T-2076: the fleet monitors (context_fill_monitor, restart_drain) build
# their per-agent config from instance.yaml instead of hardcoding accounts/paths,
# so a self-hoster configures accounts + services ONCE here. This returns the
# portable identity fields the monitors need; monitor-side TUNING (context_window,
# model_pin, observation_only, …) stays in the monitors, keyed on these personas.
# ---------------------------------------------------------------------------

# Fallback bindings — the pantheon-vps-0 values, a SYNC MIRROR of instance.yaml's
# roles (reached only in a manifest-less env, e.g. a test box without a staged
# instance.yaml). tools/test_instance_role_grants.py enforces the sync. Only the
# monitored agents carry restart-config; observation-only seats (mason/artisan) have
# just an account. `restart_pane` is the structured field that replaces parsing the
# freeform `session:` prose.
_FALLBACK_PERSONA_TO_BINDING: dict[str, dict] = {
    # T-2953 (2026-10-24): orchestrator cut over to the control plane; mirrors instance.yaml
    # roles.head-engineering-orchestrator (supervisor only, as recovery; T-5311).
    "orchestrator": {"account": "claude", "restart_service": "claude-telegram.service",
              "restart_mode": "user", "restart_pane": "claude-telegram-bot:0",
              "supervisor": "control-plane"},
    "specialist": {"account": "claude-research", "restart_service": "claude-telegram-research.service",
              "restart_mode": "user", "restart_pane": "claude-research-bot:0",
              # T-2648: mirrors instance.yaml roles.specialist.companion_units
              # exactly (TestBindingFallbackStaysInSync does exact dict
              # equality between this fallback and the live-loaded binding).
              "companion_units": "pantheon-acp-bridge@specialist.service=acp-bridge-specialist",
              # T-2658: mirrors instance.yaml roles.specialist.supervisor exactly
              # (same exact-dict-equality guard as companion_units above). Marks
              # specialist as living on the control-plane (T-2633 cutover) rather than
              # a bespoke systemd unit -- restart_drain.py / context_fill_monitor.py
              # branch the restart mechanism on this, not on restart_service
              # (which stays the RETIRED bespoke unit's documentation value).
              # T-5311: no expected_policy_skip (marker removed; a real block shows WARN).
              "supervisor": "control-plane"},
    # T-3026 (2026-09-29): recovery cut over to the control plane; mirrors instance.yaml
    # roles.head-system-orchestrator (supervisor + expected_policy_skip, as specialist).
    "recovery": {"account": "claude-fallback", "restart_service": "claude-telegram-fallback.service",
             "restart_mode": "user", "restart_pane": "claude-telegram-bot-fallback:0",
             "supervisor": "control-plane"},
    # T-5063: mirrors instance.yaml roles.head-memory-orchestrator (cut over 2026-10-18).
    "archivist": {"account": "exocortex", "restart_service": "claude-telegram-exocortex.service",
                  "restart_mode": "user", "restart_pane": "claude-telegram-bot-exocortex:0",
                  "supervisor": "control-plane"},
    # T-5311: mason's expected_policy_skip marker removed (its cited cause was stale;
    # in-window maintenance renders OK via T-5285).
    # T-5698: mason runs under its runtime controller (pantheon-runtime-controller@mason,
    # session suborch1-main) like artisan, so it carries the control-plane markers.
    # restart_service stays ABSENT (watchman's shape): it would make restart_drain build a
    # drain entry and flip context_fill_monitor's has_restart; the restart identity is
    # derived by restart_target instead.
    "mason": {"account": "suborch1", "restart_mode": "user",
              "restart_pane": "suborch1-main:0", "supervisor": "control-plane"},
    # T-3574 (orchestrator ruling (b), 2026-09-08) + T-5299 (2026-10-20): artisan mirrors
    # instance.yaml roles.engineering-orchestrator-2. T-5299 completed the control-plane
    # cutover: supervisor + restart binding added (archivist's shape). The earlier premature
    # marker (reverted T-3098/T-3082) made the monitors derive a consume oneshot that could
    # not cycle the session; T-4159's --supervisor control-plane kill-first branch fixed that.
    # restart_service is the real consume unit (documentation value; consumers derive it).
    # Kept in sync with the manifest per Guard D
    # (test_binding_fallback_never_contradicts_instance_yaml).
    "artisan": {"account": "suborch2", "restart_service": "pantheon-persona-consume@artisan.service",
              "restart_mode": "user", "restart_pane": "suborch2-main:0",
              "supervisor": "control-plane"},
    # Peer daemons (T-2256): account-only, matching instance.yaml — codex carries no
    # restart-config (it runs under codex-supervise.sh with Restart=always).
    # They were absent here until T-2256 even though instance.yaml binds them, which
    # made the FALLBACK a 6-of-8 mirror: harmless while the manifest is present, and
    # a silent under-count of the fleet the moment it is not. The sync guard in
    # tools/test_instance_role_grants.py only checked fallback→manifest (stale keys),
    # never manifest→fallback (missing keys), so nothing surfaced it — the same
    # one-directional-completeness shape T-2256 exists to close.
    "codex": {"account": "codex"},
    # T-4884 B6: mirrors instance.yaml roles.system-orchestrator-1 exactly. No
    # restart_service (no bespoke unit ever existed; see the manifest comment).
    "watchman": {"account": "codex-fallback", "restart_mode": "user",
                 "restart_pane": "codex-fallback-main:0", "supervisor": "control-plane"},
}

# The restart-config keys the monitors consume from a binding (account is separate).
# T-2658: "supervisor" joined this tuple alongside the three restart_* keys --
# same plain-string isinstance(v, str) loading path in _load_instance_bindings
# below, no special-casing needed (unlike companion_units, which is list-typed).
_BINDING_RESTART_KEYS = ("restart_service", "restart_mode", "restart_pane", "supervisor")

# T-3210: matches persona_bringup.py's coarse policy-skip counter reasons
# (backoff / runtime_transition / quota_breaker_open / runtime_viability_blocked /
# relaunch_window_exhausted, plus T-4179's prior_receiver_blocked, which no seat
# should ever list as expected)
# -- a reason-match TOKEN, not a free-text label, and validated (not merely
# isinstance(str)) at load, unlike _BINDING_RESTART_KEYS' shared plain-string
# loop below: this key's dict lives in the SAME binding BespokeIdentity.as_env()
# (tools/persona_cutover_resolve.py) prints as an UNQUOTED `KEY=value` line for
# persona_cutover.sh/persona_rollback.sh to `eval` directly (see
# _encode_companion_units' docstring just below). A future consumer that reads
# this key the way resolve_bespoke_identity() reads account/restart_service/
# companion_units today would inherit that eval unchanged, so a value with
# spaces or parens must never reach entry[...] -- restricted at the source,
# not left to each future reader to remember.
_EXPECTED_POLICY_SKIP_RE = re.compile(r"^[a-z0-9_-]+$")


def _validated_expected_policy_skip(raw: object) -> str | None:
    """Return *raw* if it is a non-empty string matching
    _EXPECTED_POLICY_SKIP_RE, else None (malformed entries are dropped, not
    raised -- matching this loader's overall fail-safe posture)."""
    if not isinstance(raw, str) or not raw:
        return None
    return raw if _EXPECTED_POLICY_SKIP_RE.match(raw) else None


def _encode_companion_units(raw: object) -> str:
    """Encode a `companion_units:` manifest list (``[{unit, tmux_session}, ...]``)
    as space-joined ``unit=session`` tokens (T-2648).

    NOT JSON: the caller (`_load_instance_bindings`) feeds this straight into a
    `BespokeIdentity` field that `as_env()` prints as an UNQUOTED `KEY=value`
    line for `persona_cutover.sh`/`persona_rollback.sh` to `eval` directly
    (see `AsEnvBashSafetyTest` in tools/test_persona_cutover_resolve.py) --
    JSON's braces/quotes/commas risk word-splitting or brace-expansion under
    that unquoted eval. Unit names and tmux session names never contain `=`
    or whitespace, so this delimiter is safe. Malformed entries (missing/
    non-string `unit` or `tmux_session`) are skipped rather than raised --
    matching this loader's overall fail-safe posture.
    """
    if not isinstance(raw, list):
        return ""
    pairs = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        unit, session = item.get("unit"), item.get("tmux_session")
        if isinstance(unit, str) and unit and isinstance(session, str) and session:
            pairs.append(f"{unit}={session}")
    return " ".join(pairs)


def _load_instance_bindings(manifest_path: Path) -> dict[str, dict]:
    """Return persona → {account, restart_service, restart_mode, restart_pane,
    companion_units, expected_policy_skip} from instance.yaml `roles:`. Only
    the keys present in a role are included. Falls back to
    _FALLBACK_PERSONA_TO_BINDING if the manifest is absent/unreadable/
    malformed (fail-safe — a missing manifest must not break the monitors'
    import)."""
    try:
        data = _parse_manifest(manifest_path)  # T-4279: cached, CSafeLoader
        roles = data.get("roles") if isinstance(data, dict) else None
        if not isinstance(roles, dict):
            return {p: dict(b) for p, b in _FALLBACK_PERSONA_TO_BINDING.items()}
        out: dict[str, dict] = {}
        for _role_key, binding in roles.items():
            if not isinstance(binding, dict):
                continue
            persona = binding.get("persona")
            account = binding.get("account")
            if not (isinstance(persona, str) and persona and isinstance(account, str) and account):
                continue
            entry = {"account": account}
            for k in _BINDING_RESTART_KEYS:
                v = binding.get(k)
                if isinstance(v, str) and v:
                    entry[k] = v
            # T-2648: companion_units is a LIST-of-dicts, not a scalar string,
            # so it can't ride _BINDING_RESTART_KEYS's isinstance(v, str) loop
            # above (a list would just be silently skipped there).
            companion_units = _encode_companion_units(binding.get("companion_units"))
            if companion_units:
                entry["companion_units"] = companion_units
            # T-3210: validated separately (regex, not bare isinstance(str)) --
            # see _validated_expected_policy_skip's docstring for why this
            # key can't ride the shared _BINDING_RESTART_KEYS loop above.
            expected_policy_skip = _validated_expected_policy_skip(
                binding.get("expected_policy_skip")
            )
            if expected_policy_skip:
                entry["expected_policy_skip"] = expected_policy_skip
            out[persona] = entry
        return out or {p: dict(b) for p, b in _FALLBACK_PERSONA_TO_BINDING.items()}
    except Exception:
        return {p: dict(b) for p, b in _FALLBACK_PERSONA_TO_BINDING.items()}


# ---------------------------------------------------------------------------
# Mailbox substrate identity (T-2405) — hub path + unix GROUP, from the same
# manifest the role bindings come from.
#
# These live here rather than in each consumer because the alternative was
# measured and it failed: the S3* audit runner hardcoded the group name, the
# group was RENAMED veneration -> pantheon-mailbox, and the runner went on
# querying a group that no longer exists for weeks. Its identity artifact
# exited 2 every cycle, which reads as "captured, degraded" rather than
# "asking the wrong question" — the audit's own 2026-08-01 cycle is what
# caught it. A hardcoded copy of an operational name decays silently; D10's
# one-canonical-source is the fix, not a more careful copy.
# ---------------------------------------------------------------------------

_FALLBACK_MAILBOX = {"hub": "/home/post-office/mailbox", "group": "pantheon-mailbox"}


def _load_instance_mailbox(manifest_path: Path) -> dict[str, str]:
    """Return the `persona_installer.mailbox` block (hub/group) from instance.yaml.

    Same fail-safe posture as the loaders above: any absence, unreadability, or
    structural surprise falls back to the pantheon-vps-0 values rather than
    breaking the import chain. Per-key, not all-or-nothing — a manifest that
    declares a hub but no group still contributes its hub.
    """
    out = dict(_FALLBACK_MAILBOX)
    try:
        data = _parse_manifest(manifest_path)  # T-4279: cached, CSafeLoader
        installer = data.get("persona_installer") if isinstance(data, dict) else None
        mailbox = installer.get("mailbox") if isinstance(installer, dict) else None
        if not isinstance(mailbox, dict):
            return out
        for key in ("hub", "group"):
            value = mailbox.get(key)
            if isinstance(value, str) and value:
                out[key] = value
        return out
    except Exception:
        return dict(_FALLBACK_MAILBOX)


# Resolve instance.yaml relative to this module's parent (repo root).
_MODULE_DIR = Path(__file__).resolve().parent
_INSTANCE_YAML = _MODULE_DIR.parent / "instance.yaml"

# persona → instance role-key  (e.g. "specialist" → "specialist")
_PERSONA_TO_INSTANCE_ROLE: dict[str, str] = _load_instance_role_map(_INSTANCE_YAML)

# persona → full role binding (account + restart-config) for the fleet monitors.
_PERSONA_TO_BINDING: dict[str, dict] = _load_instance_bindings(_INSTANCE_YAML)

# Mailbox hub path + unix group, from persona_installer.mailbox.
_MAILBOX: dict[str, str] = _load_instance_mailbox(_INSTANCE_YAML)


def mailbox_hub() -> str:
    """Absolute path of the ORG-026 mailbox hub, per instance.yaml."""
    return _MAILBOX["hub"]


def current_mailbox_hub() -> str:
    """mailbox_hub(), re-read from instance.yaml on EVERY call. For long-lived
    processes: mailbox_hub() is fixed at import, so a server started before a hub
    move stays bound to the dead path. T-2693: orchestrator's mailbox MCP consume read the
    retired hub and went silently deaf until its session restarted."""
    return _load_instance_mailbox(_INSTANCE_YAML)["hub"]


def mailbox_group() -> str:
    """Unix GROUP owning the mailbox hub, per instance.yaml.

    `veneration` is the RETIRED name; it no longer exists on the host (the gid
    was renamed, not recreated), so any surviving hardcoded reference asks
    getent a question with no answer. Read it from here.
    """
    return _MAILBOX["group"]


def instance_binding(persona: str) -> dict:
    """Return a COPY of *persona*'s instance.yaml binding — {account[, restart_service,
    restart_mode, restart_pane, companion_units, expected_policy_skip]} — or {} if the
    persona has no role binding. Used by the fleet monitors (T-2076) to source
    accounts/services from instance.yaml, by tools/persona_cutover_resolve.py (T-2648)
    for companion systemd units sharing a persona's default tmux socket, and by
    tools/fleet_health.py (T-3210) for the expected-not-cut-over policy-skip
    annotation."""
    return dict(_PERSONA_TO_BINDING.get(persona, {}))


def control_plane_consume_service(persona: str) -> str:
    """Return the pantheon-persona-consume@ unit name for *persona* (T-2658).

    Pure naming-convention function -- single source of truth for the
    template-unit → concrete-unit-name fact (tools/systemd/pantheon-persona-
    consume@.service, `%i` = persona), so restart_drain.py and
    context_fill_monitor.py both derive the control-plane restart target from
    the persona name rather than each hand-authoring the same f-string (or
    instance.yaml hand-authoring it as a second, driftable copy of
    restart_service). Callers gate on `instance_binding(persona).get(
    "supervisor") == "control-plane"` before using this -- it does not itself
    check supervisor status, since it has no binding to check against.
    """
    return f"pantheon-persona-consume@{persona}.service"


def control_plane_controller_service(persona: str) -> str:
    """The long-running pantheon-runtime-controller@ unit for *persona* (specialist shape).

    On a control-plane seat this is the unit whose lifecycle tracks the pane
    (Type=simple, Restart=always: it blocks until the tmux session dies). The
    consume unit is a 2-minute oneshot and is never is-active between fires, so
    "is the seat's session supervisor up?" checks must read THIS unit (T-4159)."""
    return f"pantheon-runtime-controller@{persona}.service"


def restart_target(persona: str) -> dict:
    """How to restart *persona*'s interactive session, derived from its binding (T-4159).

    {"supervisor": "bespoke"|"control-plane", "service", "mode", "pane", "session_unit"}.
    `service` is what handoff_and_restart.sh / self_restart.sh take as --service:
    the bespoke restart_service, or on a control-plane seat the persona-consume
    unit (that script's --supervisor control-plane branch requires it, kills the
    session first, and the controller relaunches). `session_unit` is the unit
    whose is-active says the session supervisor is up: the bespoke unit, or the
    runtime controller. Same derivation as context_fill_monitor's cfm entries, so
    the shell consumers (recovery_self_restart.sh, verify_startup.sh) can't drift from
    it. Raises KeyError when the persona has no restart binding: a caller that
    would fall back to a hardcoded bespoke unit is the exact hazard this replaces."""
    binding = instance_binding(persona)
    control_plane = binding.get("supervisor") == "control-plane"
    # T-5698: a control-plane seat's service is DERIVED (persona-consume@), so it
    # needs only restart_pane. watchman deliberately omits restart_service (it would
    # arm Claude-coupled restart-drain/context-fill), and mason carries none either.
    if "restart_pane" not in binding or (not control_plane and "restart_service" not in binding):
        raise KeyError(f"no restart binding for persona {persona!r}")
    if control_plane:
        return {"supervisor": "control-plane",
                "service": control_plane_consume_service(persona),
                "mode": binding.get("restart_mode", "user"),
                "pane": binding["restart_pane"],
                "session_unit": control_plane_controller_service(persona)}
    return {"supervisor": "bespoke",
            "service": binding["restart_service"],
            "mode": binding.get("restart_mode", "user"),
            "pane": binding["restart_pane"],
            "session_unit": binding["restart_service"]}


# ---------------------------------------------------------------------------
# OS account → persona (T-2256). THE axis every stale fleet list lives on.
#
# specialist's T-2255 inventory found ~20 hand-carried copies of this mapping in
# tools/, about half of them incomplete, and two measured casualties in one
# evening (deploy_hook covering 3 of 6 accounts while reporting success;
# ticket.py's WIP cap failing OPEN for exactly one seat). persona→account was
# already canonical here via _PERSONA_TO_BINDING; account→persona — the
# direction every one of those lists actually needs — had no home.
#
# DERIVED, never enumerated. A second literal here would satisfy the ticket's
# wording and reproduce its defect: a twenty-first hand-carried list, living in
# the file everyone agrees is canonical. instance.yaml stays the single surface
# a self-hoster edits, and test_instance_role_grants.py's sync guard extends to
# this axis for free.
# ---------------------------------------------------------------------------


def _invert_bindings(bindings: dict[str, dict]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return (account→persona, collisions) inverted from persona→binding.

    Two personas sharing one account is a manifest error that cannot be resolved
    correctly, only reported. Resolution is SORTED-FIRST-WINS (deterministic —
    dict insertion order would make the winner depend on YAML key order, so the
    same broken manifest would resolve differently on different reads), and every
    losing persona is recorded in the returned collisions map so a check can fail
    on it. Raising here instead would brick every ticket op, the mailbox MCP and
    every monitor on one bad manifest edit — the loader above is fail-safe by
    deliberate design and this must not be the one place that is not.
    """
    ordered: dict[str, list[str]] = {}
    for persona in sorted(bindings):
        account = bindings[persona].get("account")
        if isinstance(account, str) and account:
            ordered.setdefault(account, []).append(persona)
    resolved = {account: personas[0] for account, personas in ordered.items()}
    collisions = {a: p for a, p in ordered.items() if len(p) > 1}
    return resolved, collisions


# account → persona (e.g. "exocortex" → "archivist"), and any account claimed by
# more than one persona (must always be empty; pinned by test_agent_role_map_accounts).
_ACCOUNT_TO_PERSONA, _ACCOUNT_PERSONA_COLLISIONS = _invert_bindings(_PERSONA_TO_BINDING)

if _ACCOUNT_PERSONA_COLLISIONS:  # pragma: no cover - impossible with a well-formed manifest
    import warnings

    warnings.warn(
        "instance.yaml binds one OS account to multiple personas: "
        + "; ".join(f"{a} -> {', '.join(p)}" for a, p in sorted(_ACCOUNT_PERSONA_COLLISIONS.items()))
        + " — account→persona resolution picked the first alphabetically. Fix the manifest.",
        RuntimeWarning,
        stacklevel=2,
    )


def account_persona(account: str) -> str | None:
    """Return the persona resident on OS account *account*, or None if unknown.

    Mirrors instance_binding()'s shape (read-only accessor over the manifest-loaded
    map), inverted.

        account_persona("exocortex")  -> "archivist"
        account_persona("suborch2")   -> "artisan"
        account_persona("codex")      -> "codex"    (persona == account by design)
        account_persona("runner")     -> None

    UNKNOWN → None, NOT identity-passthrough, and the difference is load-bearing.
    resolve_role/resolve_persona/resolve_instance_role all pass unknown names
    through unchanged, which is right for those axes (a name that is already in
    the target namespace is a legitimate input). It is WRONG here: an OS account
    is not a persona, so handing back the account name manufactures a persona
    that no ticket, ledger entry or mailbox path carries. That is precisely the
    fail-OPEN signature test_ticket.test_no_account_resolves_to_itself exists to
    catch — the WIP cap keyed on "exocortex", a value nothing could ever match,
    and no seat was blocked and nothing was logged.

    None makes "I do not know this account" unrepresentable as a valid answer, and
    leaves the fail-open/fail-closed choice with the CALL SITE, where the posture
    is actually decided: `account_persona(u) or u` is an explicit fail-open (as
    ticket._claiming_persona wants), `if p is None: raise` an explicit fail-closed
    (as mailbox_mcp._caller_persona wants). Both are visible in the reading.
    """
    return _ACCOUNT_TO_PERSONA.get(account)


# Consume-plane seats: the persona-consume runtime launches these with
# WorkingDirectory=%h/pantheon-config (tools/systemd/pantheon-persona-consume@.service),
# so Claude Code slugifies their LIVE-session project dir to
# `-home-<account>-pantheon-config` rather than the plain `-home-<account>`. Membership is
# the instance ROLE-KEY axis (the portable config axis this module already owns), grounded
# in instance.yaml's "Consume-plane seats (specialist, recovery, mason, artisan) are woken by the
# consume runtime" (the wake_timer_personas complement). EXCLUDED, each verified against
# that seat's freshest live jsonl (T-2715): head-engineering-orchestrator (orchestrator — home-cwd
# claude-telegram.service), head-memory-orchestrator (archivist — mailbox-first at the operator's
# exocortex pane), peer-daemon-* (codex — own sandbox). A future consume seat (e.g.
# engineering-orchestrator-3) joins by adding its role-key here.
# Seats NOT marked `supervisor: control-plane` whose live session still launches
# cwd=<account>/pantheon-config: the bespoke-relaunch engineering orchestrators
# (mason/artisan, captured launch cwd, T-3829/T-3838).
_BESPOKE_PANTHEON_CONFIG_CWD_ROLE_KEYS = frozenset({
    "engineering-orchestrator-1",  # mason
    "engineering-orchestrator-2",  # artisan
})


def live_session_project_slug(account: str) -> str:
    """Claude Code's project-dir slug for *account*'s LIVE session (T-2682/T-2687/T-2715/T-4159).

    A control-plane seat launches cwd=<account>/pantheon-config (the runtime scope's
    WorkingDirectory), so Claude Code slugifies its live-session project dir to
    `-home-<account>-pantheon-config`, NOT the plain `-home-<account>` a home-cwd bespoke
    seat uses. A monitor that reads a seat's live jsonl (context_fill_monitor cap
    detectors, keep_going's nudge/cap feed, doc_traversal_report's boot-adherence) must
    watch the dir the session actually writes to, or it reads a stale/other project dir.

    T-4159: keyed on the LAUNCH SHAPE, not on role membership. `supervisor: control-plane`
    (the cutover-completion marker) or a bespoke-relaunch pantheon-config-cwd seat
    (mason/artisan) -> the -pantheon-config slug; everything else -> home. T-2715's
    role-membership set listed recovery as consume-plane, which stopped being true when
    T-3082 reverted recovery to bespoke (2026-08-23): recovery's live jsonl has been in
    -home-claude-fallback since, and the -pantheon-config dir holds none (checked
    2026-09-28), so keep_going/doc_traversal read an empty dir for recovery. It also
    hard-wired orchestrator to home, which a T-3650 hand-move would make wrong. Following
    the marker makes both flip exactly when commit B lands.
    """
    persona = account_persona(account)
    if persona and (
        instance_binding(persona).get("supervisor") == "control-plane"
        or resolve_instance_role(persona) in _BESPOKE_PANTHEON_CONFIG_CWD_ROLE_KEYS
    ):
        return f"-home-{account}-pantheon-config"
    return f"-home-{account}"


def live_session_projects_dir(account: str) -> str:
    """Full path to *account*'s live-session Claude Code projects dir --
    `/home/<account>/.claude/projects/<slug>`. See `live_session_project_slug`
    for the control-plane vs bespoke slug distinction (T-2682/T-2687)."""
    return f"/home/{account}/.claude/projects/{live_session_project_slug(account)}"


def account_persona_map() -> dict[str, str]:
    """Return a COPY of the full OS-account → persona map (all bound accounts).

    The replacement for a hand-carried `{"claude": "orchestrator", ...}` literal. Copy,
    not the live dict, so a caller that mutates its own view cannot corrupt every
    other importer in the process (same posture as instance_binding()).
    """
    return dict(_ACCOUNT_TO_PERSONA)


def fleet_accounts() -> tuple[str, ...]:
    """Return every OS account bound to a persona in instance.yaml, sorted.

    The replacement for a hand-carried tuple of account names (the shape
    disk_usage_monitor.AGENT_HOMES and session_reset_log.KNOWN_ACCOUNTS carry).
    Sorted for stable output/diffs, not by any priority.
    """
    return tuple(sorted(_ACCOUNT_TO_PERSONA))


# instance role-key → persona  (inverse; e.g. "specialist" → "specialist")
_INSTANCE_ROLE_TO_PERSONA: dict[str, str] = {v: k for k, v in _PERSONA_TO_INSTANCE_ROLE.items()}


# ---------------------------------------------------------------------------
# Public API — all three namespaces.
# ---------------------------------------------------------------------------


def resolve_role(persona: str) -> str:
    """Return the audit role-id for *persona*, or *persona* itself if not in the map.

    Identity fallback means unknown persona names pass through unchanged,
    preserving backward compatibility for any agent not yet registered.

    This returns the AUDIT role-id (underscored: head_engineering_orchestrator / head_research_orchestrator / head_system_orchestrator),
    which is the canonical form for ledger `agent:` fields. Do NOT use this for
    repos.yaml grants lookup — use resolve_instance_role() instead.

    A persona alias (consultant) is collapsed to its canonical persona (codex) first,
    so an alias never lands in a ledger `agent:` field either.
    """
    persona = _dealias(persona)
    return AGENT_ROLE_MAP.get(persona, persona)


def resolve_persona(role_or_persona: str) -> str:
    """Return the persona name for *role_or_persona*, or the input itself if not in the map.

    Inverse of resolve_role(). Used to normalize ticket `target:` and
    `requester:` fields from audit role-ids to persona names on write (T-0492/msg_1661).

    Examples:
        resolve_persona("head_engineering_orchestrator") -> "orchestrator"
        resolve_persona("orchestrator")         -> "orchestrator"  (legacy audit-id alias, T-3814)
        resolve_persona("head_research_orchestrator")-> "specialist"
        resolve_persona("specialist")              -> "specialist"  (legacy audit-id alias, D1)
        resolve_persona("recovery")                      -> "recovery"   (identity)
        resolve_persona("orchestrator")             -> "orchestrator"  (already persona; passthrough)
        resolve_persona("operator")           -> "operator" (not in map; passthrough)
        resolve_persona("consultant")             -> "codex"   (alias → canonical persona)

    Identity fallback preserves backward compatibility for unregistered names.
    Ledger `agent:` fields should NOT be passed through this function — they
    remain audit-role-id-canonical for audit-chain continuity (use resolve_role there).
    """
    role_or_persona = _dealias(role_or_persona)
    return _ROLE_PERSONA_MAP.get(role_or_persona, role_or_persona)


def resolve_instance_role(persona_or_audit_role: str) -> str:
    """Return the instance role-key for *persona_or_audit_role*.

    Used for repos.yaml grants lookup (ORG-030 Phase 1). Grants are keyed on
    instance role-keys (head-engineering-orchestrator / specialist / head-system-orchestrator) —
    the portable axis from ORG-030.

    Accepts both persona names AND audit role-ids (normalizes via resolve_persona
    first so callers don't need to pre-normalize ticket target fields).

    Examples:
        resolve_instance_role("specialist")          -> "specialist"
        resolve_instance_role("specialist")   -> "specialist"  (legacy audit-id alias, D1)
        resolve_instance_role("orchestrator")          -> "head-engineering-orchestrator"
        resolve_instance_role("head_engineering_orchestrator") -> "head-engineering-orchestrator"
        resolve_instance_role("recovery")           -> "head-system-orchestrator"
        resolve_instance_role("operator")        -> "operator"  (not in map; passthrough)

        resolve_instance_role("consultant")          -> "peer-daemon-codex"  (alias → codex)

    Identity fallback preserves behavior for unregistered names.
    """
    # Step 0: collapse a persona alias (consultant → codex) so it resolves to the
    # canonical persona's grants, not an unbound alias name.
    persona_or_audit_role = _dealias(persona_or_audit_role)
    # Step 1: normalize audit role-id → persona (e.g. head_research_orchestrator → specialist;
    # the legacy specialist token was already collapsed in step 0 via _dealias).
    persona = _ROLE_PERSONA_MAP.get(persona_or_audit_role, persona_or_audit_role)
    # Step 2: persona → instance role-key.
    return _PERSONA_TO_INSTANCE_ROLE.get(persona, persona)


def ledger_identity(persona: str) -> tuple[str, str]:
    """Return `(agent, executor)` for a ledger entry *persona* writes (ORG-001 as
    amended 2026-10-08, the operator TG 11474; T-4737): its ORG-100 role id and its
    instance.yaml seat slot.

        ledger_identity("artisan") -> ("engineering_orchestrator", "engineering-orchestrator-2")
        ledger_identity("specialist") -> ("head_research_orchestrator", "specialist")
    """
    persona = resolve_persona(persona)
    return resolve_role(persona), resolve_instance_role(persona)


def ledger_entry_by(entry: dict, seat: str) -> bool:
    """Whether ledger *entry* was written by *seat* (a persona, or a legacy ledger
    agent id such as `orchestrator`).

    Matches both forms of the ledger `agent` field (T-4737):

    - history (before 2026-10-08): `agent` is the seat's legacy id, i.e. *seat*
      itself, or its persona or role id;
    - new (ORG-001 as amended): `agent` is the seat's ORG-100 role id. When that
      role is shared by several seats (NON_INJECTIVE_ROLE_IDS), the entry is the
      seat's only if `executor` starts with the seat's slot; an entry of a shared
      role with no executor is attributed to no one.
    """
    agent = entry.get("agent")
    if not isinstance(agent, str) or not agent:
        return False
    persona = resolve_persona(seat)
    role, slot = ledger_identity(persona)
    if agent in (seat, persona):
        return True
    if agent != role:
        return False
    if role not in NON_INJECTIVE_ROLE_IDS:
        return True
    executor = entry.get("executor")
    if not isinstance(executor, str) or not executor.strip():
        return False
    return executor.split()[0] == slot


# ---------------------------------------------------------------------------
# L3 PersonaRoleMembership reader (ORG-085 L3 / ORG-088 D-2; T-3757).
#
# functional_roles(persona) / role_members(role_id) resolve OVER the L3 source
# (persona_roles.yaml, the SOLE authority for role_ids). This is a READER, not a
# fourth literal map — ORG-088 D-2: "agent_role_map.py gains a reader over the L3
# source, not a literal map." role_ids are the L2 functional-role ids of ORG-088
# D-2, as reshaped by the operator's T-3807 per-persona rename (head_engineering_orchestrator /
# head_memory_orchestrator / engineering_orchestrator / head_research_orchestrator /
# head_system_orchestrator / head_auditor; the universal `orchestrator` tier retired),
# a DISTINCT namespace from the audit-role-ids in AGENT_ROLE_MAP above and from the
# persona-orientation spawn-role names.
#
# TWO POSTURES THAT DO NOT MERGE. The instance.yaml BINDING loaders above are
# deliberately fail-SAFE (swallow errors, fall back to a hardcoded mirror) so a
# missing manifest cannot break the fleet import chain. This membership reader is
# fail-CLOSED per ORG-085 invariants 4 (mirrors are digest-bound; mismatch refuses)
# + 5 (missing revision/digest is a typed refusal): it RAISES rather than serving
# stale/unverified membership. A fail-safe fallback here would BE the second
# authority ORG-085 §Field ownership and authority bans. The two are reconciled by LAZINESS — the reader
# loads+verifies on CALL, never at import, so a source problem fails only the
# role-routing path, not every importer of this module.
# ---------------------------------------------------------------------------

_PERSONA_ROLES_YAML = _MODULE_DIR.parent / "persona_roles.yaml"

_MEMBERSHIP_LIFECYCLES = frozenset({"declared", "deprecated", "retired"})


class PersonaRoleMembershipError(Exception):
    """Base for fail-closed L3 PersonaRoleMembership refusals."""


class MembershipSourceMissing(PersonaRoleMembershipError):
    """The L3 source or the instance.yaml projection block is absent/unreadable."""


class MembershipDigestMismatch(PersonaRoleMembershipError):
    """The L3 source's recomputed digest != the projection's expected_digest
    (ORG-085 invariant 4: mirrors are digest-bound and mismatches refuse)."""


class MembershipStaleRevision(PersonaRoleMembershipError):
    """The L3 source's source_revision is missing, or != the projection's
    expected_revision (ORG-085 invariant 5: missing/stale revision is a refusal)."""


class MembershipMalformed(PersonaRoleMembershipError):
    """A persona record is structurally invalid: empty/absent role_ids, a
    default_role_id not in role_ids, an unknown lifecycle, or wrong types."""


def canonical_source_bytes(doc: dict, *, exclude: tuple[str, ...] = ("source_digest",)) -> bytes:
    """Canonical pre-image for a content digest: the document content EXCLUDING the
    fields named in *exclude*, as sorted-key compact ensure_ascii JSON. The digest is
    over PARSED content, so it is invariant to YAML comments/whitespace and binds the
    semantic content rather than the file layout — two clones with permuted layout
    compute byte-identical pre-images (the ORG-088 R3-H3 reproducibility property).

    ONE audited serialization (ORG-088 T2 Q4), used by the L3 membership digest
    contract:
      - the L3 membership reader below, at the DEFAULT exclude — the bytes are
        UNCHANGED from the pre-generalization _canonical_membership_bytes, so the
        persona_roles.yaml seed digest still verifies.
    (ORG-088 rev-2 / T-3794 retired the second consumer, tools/domain_registry.py,
    when the operator's Call #1 dropped the DomainRegistry; the `exclude` parameter is
    kept general but now serves the membership contract alone.)

    Raises MembershipMalformed (not a bare TypeError) when the content is not
    JSON-serializable (LOW-fix #3) — a typed refusal befitting a fail-closed reader.
    In practice yaml.safe_load only yields JSON-serializable scalars/containers, so
    this guard is defensive; it exists so a future non-safe loader cannot leak an
    untyped TypeError through the digest path."""
    content = {k: v for k, v in doc.items() if k not in exclude}
    try:
        return json.dumps(
            content, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    except TypeError as exc:
        raise MembershipMalformed(
            f"source content is not JSON-serializable for the canonical digest: {exc}"
        ) from exc


def _compute_source_digest(doc: dict) -> str:
    return hashlib.sha256(canonical_source_bytes(doc)).hexdigest()


def _load_membership_projection(manifest_path: Path) -> dict:
    """Return the `role_membership_projection` block (source / expected_revision /
    expected_digest) from instance.yaml. Fail-CLOSED: absence or malformation raises
    MembershipSourceMissing — a role-routing consumer must not silently proceed
    without the binding-layer attestation of the L3 source."""
    try:
        data = _parse_manifest(manifest_path)  # T-4279: cached, CSafeLoader
    except Exception as exc:
        raise MembershipSourceMissing(
            f"cannot read manifest {manifest_path}: {exc}"
        ) from exc
    projection = data.get("role_membership_projection") if isinstance(data, dict) else None
    if not isinstance(projection, dict):
        raise MembershipSourceMissing(
            f"instance.yaml carries no role_membership_projection block ({manifest_path})"
        )
    # Copy: _parse_manifest's document is shared with its cache (T-4279). A
    # SHALLOW copy is enough only because every projection value (source,
    # expected_revision, expected_digest) is a scalar; if a nested mapping/list is
    # ever added to this block, switch to copy.deepcopy.
    return dict(projection)


def _load_membership_source(source_path: Path, projection: dict) -> dict[str, dict]:
    """Read + verify the L3 source against *projection*, fail-closed. Returns
    {persona: {"role_ids": frozenset[str], "default_role_id": str, "lifecycle": str}}.

    Order: atomic single read -> parse -> DIGEST check (inv 4) -> REVISION check
    (inv 5) -> per-persona structural validation. The digest and revision checks
    gate DISTINCT branches so their falsifiers are independent (T-3757 F1 / F3):
    removing either check cannot mask the other."""
    # Atomic single-file read (one read syscall; git updates the file via atomic
    # rename). A torn/partial read fails the YAML parse or the digest check below,
    # so it is still fail-closed — no locking primitive is needed for a whole-file
    # digest over a single source.
    try:
        raw = source_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise MembershipSourceMissing(
            f"cannot read L3 membership source {source_path}: {exc}"
        ) from exc
    try:
        import yaml

        doc = yaml.safe_load(raw)
    except Exception as exc:
        raise MembershipMalformed(
            f"L3 membership source is not valid YAML ({source_path}): {exc}"
        ) from exc
    if not isinstance(doc, dict):
        raise MembershipMalformed(f"L3 membership source is not a mapping ({source_path})")

    # inv 4 — integrity: the recomputed digest must equal the pinned expected_digest.
    expected_digest = projection.get("expected_digest")
    actual_digest = _compute_source_digest(doc)
    if not isinstance(expected_digest, str) or actual_digest != expected_digest:
        raise MembershipDigestMismatch(
            f"L3 membership digest mismatch: source={actual_digest} "
            f"projection={expected_digest!r}"
        )

    # LOW-fix #1 (T-3758): SELF-consistency — the source's OWN source_digest field
    # must equal the recomputed digest. The projection check above binds the source to
    # instance.yaml; this binds the source to ITSELF, so a source edited without
    # recomputing its own source_digest is refused even when the projection happens to
    # still match (e.g. both left stale together). Mirrors the domain registry's
    # self-digest gate. MembershipMalformed, not DigestMismatch: a source inconsistent
    # with its OWN recorded digest is malformed, distinct from a source that disagrees
    # with the binding-layer projection.
    own_digest = doc.get("source_digest")
    if not isinstance(own_digest, str) or own_digest != actual_digest:
        raise MembershipMalformed(
            f"L3 membership source_digest self-inconsistent: recorded={own_digest!r} "
            f"computed={actual_digest}"
        )

    # inv 5 — freshness: source_revision must be present AND equal expected_revision.
    source_revision = doc.get("source_revision")
    expected_revision = projection.get("expected_revision")
    if source_revision is None or source_revision != expected_revision:
        raise MembershipStaleRevision(
            f"L3 membership revision stale/missing: source={source_revision!r} "
            f"expected={expected_revision!r}"
        )

    personas = doc.get("personas")
    if not isinstance(personas, dict):
        raise MembershipMalformed(f"L3 membership source has no personas mapping ({source_path})")
    out: dict[str, dict] = {}
    for persona, record in personas.items():
        if not isinstance(record, dict):
            raise MembershipMalformed(f"persona {persona!r} record is not a mapping")
        role_ids = record.get("role_ids")
        default_role_id = record.get("default_role_id")
        lifecycle = record.get("lifecycle")
        if not (
            isinstance(role_ids, list)
            and role_ids
            and all(isinstance(r, str) and r for r in role_ids)
        ):
            raise MembershipMalformed(f"persona {persona!r} has empty/invalid role_ids")
        if not (isinstance(default_role_id, str) and default_role_id in role_ids):
            raise MembershipMalformed(
                f"persona {persona!r} default_role_id {default_role_id!r} "
                f"not in role_ids {sorted(role_ids)}"
            )
        if lifecycle not in _MEMBERSHIP_LIFECYCLES:
            raise MembershipMalformed(f"persona {persona!r} has invalid lifecycle {lifecycle!r}")
        out[persona] = {
            "role_ids": frozenset(role_ids),
            "default_role_id": default_role_id,
            "lifecycle": lifecycle,
        }
    return out


def _resolve_membership(
    source_path: Path | None = None, manifest_path: Path | None = None
) -> dict[str, dict]:
    """Load + verify the L3 membership (LAZY, per-call — freshness over caching for
    a fail-closed contract; the source is tiny). Paths are injectable for hermetic
    tests and default to the live tree (_PERSONA_ROLES_YAML / _INSTANCE_YAML,
    resolved from module globals at call time so tests can repoint them)."""
    manifest_path = manifest_path or _INSTANCE_YAML
    projection = _load_membership_projection(manifest_path)
    if source_path is None:
        # LOW-fix #2 (T-3758): resolve the projection's DECLARED `source:` path
        # (relative to the repo root = the manifest's parent) rather than hardcoding
        # _PERSONA_ROLES_YAML. The projection is the binding-layer statement of WHERE
        # the L3 source lives; honoring it means a manifest that repoints `source:` is
        # followed rather than silently overridden by the hardcode. Falls back to the
        # hardcode only when the field is absent. (An absolute declared path makes the
        # `manifest_path.parent / declared` join return the absolute path unchanged,
        # so hermetic tests that pin an absolute source: still resolve correctly.)
        declared = projection.get("source")
        source_path = (
            manifest_path.parent / declared
            if isinstance(declared, str) and declared
            else _PERSONA_ROLES_YAML
        )
    return _load_membership_source(source_path, projection)


def _with_implied(role_ids: frozenset[str]) -> frozenset[str]:
    """Add the regular role each head role implies (T-4701; the operator TG 11444, 2026-10-07:
    "roles for heads implicitly are also non head roles"). ORG-100's naming pattern
    makes a key seat `head_<rest>` and its regulars `<rest>`, so a persona holding
    `head_engineering_orchestrator` also holds `engineering_orchestrator`, and
    `head_auditor` also holds `auditor`. Derived from the declared role_ids, NOT a second
    literal role list: persona_roles.yaml stays the sole authority for what is DECLARED."""
    implied = {r[len("head_"):] for r in role_ids if r.startswith("head_") and len(r) > len("head_")}
    return role_ids | frozenset(implied)


def functional_roles(persona: str) -> frozenset[str]:
    """Return the functional role_ids *persona* holds, resolved over the verified L3
    source. Unknown persona -> empty frozenset (a persona with no declared membership
    holds no functional roles — a truthful answer, not a source error). Raises
    PersonaRoleMembershipError (fail-closed) on missing source/projection, digest
    mismatch, or missing/stale revision."""
    record = _resolve_membership().get(persona)
    return _with_implied(record["role_ids"]) if record else frozenset()


def role_members(role_id: str) -> frozenset[str]:
    """Return the personas whose role_ids contains *role_id*, over the same verified
    L3 source. Unknown role_id -> empty frozenset. Same fail-closed raises on source
    problems as functional_roles()."""
    membership = _resolve_membership()
    return frozenset(p for p, rec in membership.items() if role_id in _with_implied(rec["role_ids"]))


def default_functional_role(persona: str) -> str | None:
    """Return *persona*'s default_role_id (guaranteed in its role_ids by load-time
    validation), or None if the persona has no declared L3 membership."""
    record = _resolve_membership().get(persona)
    return record["default_role_id"] if record else None


def known_role_ids() -> frozenset[str]:
    """Return the union of every declared functional role_id across the verified L3
    source — the functional-role membership VOCABULARY (ORG-088 Plane-A / T-3761).

    This is the single vocab the unknown-role checks resolve against: the CLI
    file/retarget WRITE-refusal and ticket_lint's role-target flag (fail-closed), and
    the list/ready/prime READ-visibility eligibility (degrade-graceful). DERIVED over
    the SAME already-verified reader as functional_roles/role_members
    (_resolve_membership) — NOT a second literal role list: persona_roles.yaml is the
    SOLE authority for role_ids (its header), and a hand-copied vocab here would BE the
    second authority ORG-085 §Field ownership and authority bans.

    Fail-CLOSED, matching the reader's existing contract (functional_roles' raises): a
    missing/unverifiable/malformed L3 source RAISES PersonaRoleMembershipError rather
    than returning an empty set. An empty-set-on-error would be wrong in BOTH
    directions — it would make every role read as "unknown" (a WRITE surface would
    refuse everything) AND make the vocab membership check vacuous (a source that
    merely failed to load would silently accept nothing/everything). The fail
    DIRECTION is the CALLER's to choose: a WRITE surface lets the raise refuse; a READ
    surface catches it and degrades to no-eligible-roles."""
    membership = _resolve_membership()
    roles: set[str] = set()
    for record in membership.values():
        roles |= _with_implied(record["role_ids"])
    return frozenset(roles)


def persona_pool_role(name: str) -> str | None:
    """Return the REGULAR pool role id a ticket for *name* should be routed to, or None.

    T-5239 / ORG-088 (the operator TG 12076/12077): tickets route by role only, never by
    persona. This is the ONE place a persona (or audit role-id / alias) is translated
    to the role pool that replaces it — for every code path that DERIVES a target
    (auto-spawned review/child tickets, filers) rather than receiving one from a
    caller. Rule: the persona's default functional role with a leading `head_`
    stripped (orchestrator/mason/artisan -> engineering_orchestrator, recovery/watchman ->
    system_orchestrator, archivist -> memory_orchestrator, specialist ->
    research_orchestrator), matching the 2026-10-19 re-pool. Read from the L3 source
    (persona_roles.yaml) via default_functional_role — no second role list. Returns
    None for `operator` and for any name with no declared membership. Raises
    PersonaRoleMembershipError (fail-closed) on an unverifiable source."""
    persona = resolve_persona(name)
    default = default_functional_role(persona)
    if not default:
        return None
    regular = default[len("head_"):] if default.startswith("head_") else default
    return regular if regular in known_role_ids() else default


def pool_roles_for(name: str) -> list[str]:
    """Role ids a caller may use instead of persona/role-id *name*, recommended first.

    Used to word the typed refusal for a persona `--target`. Empty if none apply."""
    persona = resolve_persona(name)
    roles = sorted(functional_roles(persona))
    pool = persona_pool_role(persona)
    if pool in roles:
        roles.remove(pool)
        roles.insert(0, pool)
    return roles


_SHELL_SAFE_VALUE = re.compile(r"^[A-Za-z0-9@._:/-]+$")


def _main(argv: list[str] | None = None) -> int:
    """`agent_role_map.py restart-target <persona>` -> KEY=VALUE lines for shell callers (T-4159).

    Keys: SUPERVISOR, SERVICE, MODE, PANE, SESSION_UNIT. Exit 2 on an unknown persona,
    a persona without a restart binding, or a value outside [A-Za-z0-9@._:/-] (the
    callers read the lines with `read`, never eval, but a malformed manifest value
    must still refuse rather than reach a systemctl/tmux argv)."""
    import sys
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "restart-target":
        print("usage: agent_role_map.py restart-target <persona>", file=sys.stderr)
        return 64
    try:
        target = restart_target(args[1])
    except KeyError as exc:
        print(f"agent_role_map: {exc.args[0]}", file=sys.stderr)
        return 2
    for key in ("supervisor", "service", "mode", "pane", "session_unit"):
        value = target[key]
        if not _SHELL_SAFE_VALUE.match(value):
            print(f"agent_role_map: unsafe {key} value {value!r}", file=sys.stderr)
            return 2
        print(f"{key.upper()}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
