"""Persona-name <-> role-id resolution for Pantheon agents.

Pantheon uses THREE parallel namespaces:

  - Persona names       (canonical for tickets): orchestrator, specialist, recovery
  - Audit role-ids      (canonical for ledger):  orchestrator, specialist, recovery
  - Instance role-keys  (portable config axis):  orchestrator, specialist, recovery,
                                                 peer-daemon-*, sub-orchestrator-1/-2, second-head

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

import re
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
    # Mnemosyne — the second head (ORG-042 two-hemisphere topology), resident
    # on the `exocortex` account (persona ≠ account; account→persona lives in
    # mailbox_mcp._USER_TO_PERSONA per T-1824). Identity audit role-id per the
    # codex/the-ancestor precedent: registering the persona makes her a valid
    # ticket target:/requester: (SOP §10 step 8); it enables, not obligates —
    # her residency remains mailbox-first per ORG-042. Registered by recovery
    # 2026-07-20 (parity audit, the operator TG 3827).
    #
    # SUPERSEDED 2026-07-21 (corrected T-2318). This comment used to end: "her
    # instance role-key / repos.yaml binding stays deliberately unbound (ORG-042:
    # her config-repo shape is hers + the operator's call)". That was true for about
    # one day and false for the eight after it. She IS bound:
    #   resolve_instance_role("mnemosyne")           -> second-head
    #   _FALLBACK_PERSONA_TO_INSTANCE_ROLE           -> second-head  (this file)
    #   repos.yaml                                    -> 4 second-head grants
    # Bound by T-1922 / 7946c56d8 (2026-07-21, example-org); the stale claim was
    # c5b2ddad5 (2026-07-20, recovery).
    #
    # Recorded rather than overwritten, per the instance.yaml header's precedent,
    # because of WHERE it sat: a reader deciding whether mnemosyne should have a
    # role-key binding would find a comment calling its absence DELIBERATE, three
    # lines above the map that binds her. A clean edit would leave nothing to warn
    # the next person that this file's prose has drifted from its own data before.
    # The ORG-042 residency point above is kept because it is still true and is
    # the useful half — it is only the binding clause that was wrong.
    "mnemosyne": "mnemosyne",
    # Metis + Atlas — sub-orchestrator seats (ORG-043), self-named 2026-07-21,
    # resident on suborch1/suborch2 (persona != account; account->persona in
    # mailbox_mcp._USER_TO_PERSONA + ticket._OS_USER_PERSONA per T-1896).
    # Identity audit role-ids per the mnemosyne precedent; registering makes
    # them valid ticket target:/requester: values for the retask lane.
    "metis": "metis",
    "atlas": "atlas",
}

# Inverse map: audit role-id → persona name (for ticket target/requester normalization).
# Derived from AGENT_ROLE_MAP; recovery is its own inverse.
_ROLE_PERSONA_MAP: dict[str, str] = {v: k for k, v in AGENT_ROLE_MAP.items()}


# ---------------------------------------------------------------------------
# Persona ALIASES — an alternate NAME for an already-registered persona, not a
# new identity. Distinct from the audit-role-id ↔ persona axis above: a role-id
# (orchestrator) and its persona (orchestrator) are two names the map already
# pairs; an alias is a THIRD kind of name that collapses onto a persona which
# already owns its role-id + account binding.
#
#   cairn → codex : cairn is the SELF-NAME of the codex peer-daemon
#     (project_cairn_is_codex_persona / reference_cairn_rename_doc_scope). codex
#     is the OS account + harness/CLI (ORG-018/ORG-031); cairn is the persona it
#     named itself. A ticket can legitimately carry requester/target: cairn
#     (e.g. T-3387), so cairn must be a VALID ticket target AND must normalize to
#     the codex account EVERYWHERE. Modeled as an alias, NOT an AGENT_ROLE_MAP
#     entry: codex already holds the audit role-id, instance-role, and account
#     binding; a parallel `cairn` persona would fork that identity and, lacking
#     an account binding, trip the account-map collision/None guards.
#     (T-3478: without this, ticket_lint's retarget-to-originator bounce of a
#     cairn-requested ticket shelled `ticket.py retarget --to cairn`, which
#     raised `unknown target 'cairn'`, so pantheon-ticket-lint exited 1 hourly.)
#
# To add an alias: one entry here. It is folded into _valid_target_values() (in
# ticket.py, via PERSONA_ALIASES keys) and into all three resolve_* functions
# below (via _dealias), so an alias never survives into a ticket target/
# requester, a ledger agent-id, or a repos.yaml grants lookup.
# ---------------------------------------------------------------------------
PERSONA_ALIASES: dict[str, str] = {
    "cairn": "codex",
}


def _dealias(name: str) -> str:
    """Collapse a persona ALIAS onto its canonical persona (cairn → codex);
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
    "orchestrator": "orchestrator",
    "specialist": "specialist",
    "recovery": "recovery",
    # ORG-043 seats + ORG-042 second head, bound 2026-07-21 (T-1922).
    "metis": "sub-orchestrator-1",
    "atlas": "sub-orchestrator-2",
    "mnemosyne": "second-head",
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
    "the-ancestor": "peer-daemon-the-ancestor",
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
# monitored agents carry restart-config; observation-only seats (metis/atlas) have
# just an account. `restart_pane` is the structured field that replaces parsing the
# freeform `session:` prose.
_FALLBACK_PERSONA_TO_BINDING: dict[str, dict] = {
    # T-2953 R31 (reverted T-3098, 2026-08-23): mirrors instance.yaml
    # roles.orchestrator's updated comment. `supervisor` stays ABSENT
    # here pre-T-2953 cutover -- orchestrator's interactive session still lives on
    # the bespoke claude-telegram.service below, so a control-plane marking
    # made restart_drain.py / context_fill_monitor.py derive the oneshot
    # consume unit as the restart target, which cannot cycle that session.
    # restart_service below is the truthful pre-cutover target. T-2953 (the
    # full control-plane cutover) re-adds this field as its own completion
    # step.
    "orchestrator": {"account": "claude", "restart_service": "claude-telegram.service",
              "restart_mode": "user", "restart_pane": "claude-telegram-bot:0"},
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
              "supervisor": "control-plane",
              # T-3210: mirrors instance.yaml roles.specialist.expected_policy_skip
              # exactly (same exact-dict-equality guard). specialist is expected
              # not-cut-over (stale 08-14 runtime.json -> every consume fire is
              # runtime_viability_blocked); fleet_health renders OK+expected
              # while the seat's coarse skip reason matches this, WARN if it
              # drifts. Absent here, a manifest-less env would render WARN.
              "expected_policy_skip": "runtime_viability_blocked"},
    # T-2684 (reverted T-3082, 2026-08-23): mirrors instance.yaml
    # roles.recovery's updated comment. `supervisor` stays ABSENT
    # here pre-T-3026 -- recovery's control-plane cutover (T-2590) moved only
    # the consume plane; the interactive session still lives on the bespoke
    # claude-telegram-fallback.service below, so a control-plane marking
    # made restart_drain.py / context_fill_monitor.py derive the oneshot
    # consume unit as the restart target, which cannot cycle that session.
    # restart_service below is the truthful pre-cutover target. T-3026 (the
    # interactive-session cutover) re-adds this field as its own completion
    # step.
    "recovery": {"account": "claude-fallback", "restart_service": "claude-telegram-fallback.service",
             "restart_mode": "user", "restart_pane": "claude-telegram-bot-fallback:0"},
    "mnemosyne": {"account": "exocortex", "restart_service": "claude-telegram-exocortex.service",
                  "restart_mode": "user", "restart_pane": "claude-telegram-bot-exocortex:0"},
    # T-3210: metis mirrors instance.yaml roles.sub-orchestrator-1.expected_policy_skip
    # (controller never onboarded metis -> every consume fire is
    # runtime_viability_blocked; expected not-cut-over). Same guard as specialist above.
    "metis": {"account": "suborch1", "expected_policy_skip": "runtime_viability_blocked"},
    # T-3574 (orchestrator ruling (b), 2026-09-08): atlas mirrors instance.yaml
    # roles.sub-orchestrator-2.expected_policy_skip — atlas is not-cut-over
    # (controller never onboarded atlas -> initial-viability-state-not-selected;
    # expected not-cut-over). DELIBERATELY NOT supervisor:control-plane — that
    # premature marker was reverted twice (T-3098 orchestrator / T-3082 recovery) because it
    # makes the monitors derive a consume mailbox-drain unit that cannot cycle the
    # session. Kept in sync with the manifest per Guard D
    # (test_binding_fallback_never_contradicts_instance_yaml). Same shape as metis.
    "atlas": {"account": "suborch2", "expected_policy_skip": "runtime_viability_blocked"},
    # Peer daemons (T-2256): account-only, matching instance.yaml — neither carries
    # restart-config (codex runs under codex-supervise.sh with Restart=always; the
    # ancestor is a veneration daemon + mailbox hub, not a monitored session).
    # They were absent here until T-2256 even though instance.yaml binds them, which
    # made the FALLBACK a 6-of-8 mirror: harmless while the manifest is present, and
    # a silent under-count of the fleet the moment it is not. The sync guard in
    # tools/test_instance_role_grants.py only checked fallback→manifest (stale keys),
    # never manifest→fallback (missing keys), so nothing surfaced it — the same
    # one-directional-completeness shape T-2256 exists to close.
    "codex": {"account": "codex"},
    "the-ancestor": {"account": "the-ancestor"},
}

# The restart-config keys the monitors consume from a binding (account is separate).
# T-2658: "supervisor" joined this tuple alongside the three restart_* keys --
# same plain-string isinstance(v, str) loading path in _load_instance_bindings
# below, no special-casing needed (unlike companion_units, which is list-typed).
_BINDING_RESTART_KEYS = ("restart_service", "restart_mode", "restart_pane", "supervisor")

# T-3210: matches persona_bringup.py's four coarse policy-skip counter reasons
# (backoff / runtime_transition / quota_breaker_open / runtime_viability_blocked)
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
        import yaml  # lazy import; matches the pattern above
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
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

_FALLBACK_MAILBOX = {"hub": "/home/the-ancestor/mailbox", "group": "pantheon-mailbox"}


def _load_instance_mailbox(manifest_path: Path) -> dict[str, str]:
    """Return the `persona_installer.mailbox` block (hub/group) from instance.yaml.

    Same fail-safe posture as the loaders above: any absence, unreadability, or
    structural surprise falls back to the pantheon-vps-0 values rather than
    breaking the import chain. Per-key, not all-or-nothing — a manifest that
    declares a hub but no group still contributes its hub.
    """
    out = dict(_FALLBACK_MAILBOX)
    try:
        import yaml  # lazy import; matches _load_instance_role_map

        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
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


# account → persona (e.g. "exocortex" → "mnemosyne"), and any account claimed by
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

        account_persona("exocortex")  -> "mnemosyne"
        account_persona("suborch2")   -> "atlas"
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
# in instance.yaml's "Consume-plane seats (specialist, recovery, metis, atlas) are woken by the
# consume runtime" (the wake_timer_personas complement). EXCLUDED, each verified against
# that seat's freshest live jsonl (T-2715): orchestrator (orchestrator — home-cwd
# claude-telegram.service), second-head (mnemosyne — mailbox-first at the operator's exocortex
# pane), peer-daemon-* (codex — own sandbox). A future consume seat (e.g. sub-orchestrator-3)
# joins by adding its role-key here.
_CONSUME_PLANE_ROLE_KEYS = frozenset({
    "specialist",          # specialist
    "recovery",   # recovery
    "sub-orchestrator-1",  # metis
    "sub-orchestrator-2",  # atlas
})


def live_session_project_slug(account: str) -> str:
    """Claude Code's project-dir slug for *account*'s LIVE session (T-2682/T-2687/T-2715).

    Consume-plane seats launch cwd=<account>/pantheon-config via the persona-consume
    runtime scope (WorkingDirectory=%h/pantheon-config), so Claude Code slugifies their
    live-session project dir to `-home-<account>-pantheon-config` -- NOT the plain
    `-home-<account>` a home-cwd seat (the HO; a mailbox-first head) uses. A monitor that
    reads a seat's live jsonl (context_fill_monitor cap detectors, keep_going's nudge/cap
    feed, doc_traversal_report's boot-adherence) must watch the dir the session actually
    writes to, or it reads a stale/other project dir.

    Single source of truth (T-2687), keyed on the instance ROLE (consume-plane membership)
    rather than the binding's `supervisor` field. T-2687 first used supervisor=="control-
    plane", which is correct for recovery/specialist but MISSES the sub-orchestrator seats
    (metis/atlas): they are consume-plane (cwd=pantheon-config, verified) yet supervisor is
    None -- the documented T-2687 gap this closes (T-2715). Keying on the consume-plane role
    set covers all four seats and any future consume seat; each seat's mapping was verified
    against its freshest live jsonl dir.
    """
    persona = account_persona(account)
    if persona and resolve_instance_role(persona) in _CONSUME_PLANE_ROLE_KEYS:
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

    This returns the AUDIT role-id (underscored: orchestrator / specialist / recovery),
    which is the canonical form for ledger `agent:` fields. Do NOT use this for
    repos.yaml grants lookup — use resolve_instance_role() instead.

    A persona alias (cairn) is collapsed to its canonical persona (codex) first,
    so an alias never lands in a ledger `agent:` field either.
    """
    persona = _dealias(persona)
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
        resolve_persona("cairn")             -> "codex"   (alias → canonical persona)

    Identity fallback preserves backward compatibility for unregistered names.
    Ledger `agent:` fields should NOT be passed through this function — they
    remain audit-role-id-canonical for audit-chain continuity (use resolve_role there).
    """
    role_or_persona = _dealias(role_or_persona)
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

        resolve_instance_role("cairn")          -> "peer-daemon-codex"  (alias → codex)

    Identity fallback preserves behavior for unregistered names.
    """
    # Step 0: collapse a persona alias (cairn → codex) so it resolves to the
    # canonical persona's grants, not an unbound alias name.
    persona_or_audit_role = _dealias(persona_or_audit_role)
    # Step 1: normalize audit role-id → persona (e.g. specialist → specialist).
    persona = _ROLE_PERSONA_MAP.get(persona_or_audit_role, persona_or_audit_role)
    # Step 2: persona → instance role-key.
    return _PERSONA_TO_INSTANCE_ROLE.get(persona, persona)
