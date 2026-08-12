"""Target-inventory discovery (T-2556 phase 2 / T-2559).

Walks a target persona's ground truth -- the instance.yaml role binding, the
persona's installed systemd units/timers, the hooks wired into its
``~/.claude/settings.json``, the mailbox hub subdir, and its Telegram channel
config -- and emits a :class:`~tools.bringup_acceptance.types.TargetInventory`
of every producer/delivery path into the daemon, each tagged with the
proof-module id that will later exercise it (T-2556 phase 3).

Every discovery function here is PURE: it takes already-fetched raw
observations (systemctl output text, a settings.json string, booleans for
presence checks) rather than shelling out itself.  Live gathering (sudo,
systemctl, cat) is a later wiring concern outside this module -- keeping
discovery pure is what makes it testable with plain fixtures, matching the
T-2382/T-2558 observation-injection convention (no live systemctl or account
reads in the test suite).

Q4 scope filter (per T-2559's ask): only paths delivering INTO the daemon are
in scope.  Host-hygiene timers (apt/disk/gc/worktree-sweeper, ...) are named
out-of-scope, not gaps -- EXCEPT when a host timer's ``OnFailure=`` routes its
failure alert into the mailbox (``pantheon-unit-failure-alert@.service``,
verified: it ``sg pantheon-mailbox``-writes via ``unit_failure_alert.py``),
in which case the host unit itself stays out-of-scope-named but its
alert-into-mailbox leg is captured as its own in-scope entry.

Parity-oracle capture (the operator's T-2556 addendum, feeds T-2578): for every
producer this module discovers, :data:`PARITY_ORACLES` records the existing
system's known-good trigger/injection-shape/dedupe-rate-limit/resume
behavior as the reference a new daemon bringup must match, each with a code
pointer so the oracle is traceable rather than asserted.

Two facts genuinely are NOT ground-truthable from files on disk -- whether a
persona has a dedicated Telegram test-chat / independent reader (Q3), and
whether a persona's keep-going kicker has actually been observed to fire.
Both are modeled as caller-supplied, defaulting to unknown, and the unknown
case is recorded as unknown rather than guessed -- per T-2556's fail-closed,
no-inferred-green rigor bar.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from tools.bringup_acceptance.evidence import RunPaths, write_inventory
from tools.bringup_acceptance.recorder import write_json_evidence
from tools.bringup_acceptance.types import InventoryPathEntry, TargetInventory


# ---------------------------------------------------------------------------
# Raw-observation shapes.  Each is a plain, fixture-friendly dataclass -- none
# of these are the landed phase-1 API (types.py); they exist only to carry
# already-fetched ground truth into the discovery functions below.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BindingFacts:
    """The instance.yaml role->persona binding (source a), pre-resolved.

    Shape mirrors ``persona_bringup.PersonaBinding`` + ``agent_role_map.
    instance_binding()`` narrowed to what discovery needs, decoupled from a
    real manifest so tests never need one.
    """

    persona: str
    account: str
    harness: str
    profile: str
    tmux_target: str
    fallbacks: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnitRecord:
    """One systemd unit as reported by ``systemctl --user --machine=<acct>@.host list-units``.

    ``on_failure`` is a separate ground-truth read (``systemctl show -p
    OnFailure --value <unit>``) merged in per-unit; None means not queried /
    not set.
    """

    unit: str
    active: str
    sub: str
    on_failure: str | None = None


@dataclass(frozen=True)
class TelegramObservation:
    """Ground truth for source (e), plus the Q3 finding input.

    ``dedicated_test_chat`` is True/False only when someone has actually
    confirmed it; None means "not determinable from ground truth" and must
    stay None all the way through -- never coerced to False.

    ``dedicated_test_chat_id`` (T-2582) is the confirmed chat's id, same
    None-until-confirmed discipline -- only meaningful once
    ``dedicated_test_chat`` is True, but this module does not enforce that
    pairing (that would be guessing at a caller invariant, not this
    module's job); it just carries whatever the caller supplies verbatim.
    """

    bridge_env_present: bool
    dedicated_test_chat: bool | None = None
    dedicated_test_chat_id: str | None = None


@dataclass(frozen=True)
class OutOfScopeUnit:
    """A Q4-excluded systemd unit, named rather than silently dropped."""

    unit: str
    reason: str


@dataclass(frozen=True)
class ParityOracle:
    """The known-good reference behavior a new daemon bringup must match."""

    trigger: str
    injection_shape: str
    dedupe_rate_limit: str
    resume_behavior: str
    source: str


@dataclass(frozen=True)
class TelegramFinding:
    """The Q3 answer, carried verbatim -- never guessed."""

    dedicated_test_chat_configured: bool | None
    note: str
    dedicated_test_chat_id: str | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    """Everything one discovery run produces: the landed inventory shape,
    plus the Q4/parity-oracle/Q3 context that doesn't fit inside it."""

    inventory: TargetInventory
    out_of_scope: tuple[OutOfScopeUnit, ...]
    parity_oracles: Mapping[str, ParityOracle]
    telegram_finding: TelegramFinding


# ---------------------------------------------------------------------------
# Module-id catalog.  New cross-phase naming contract: phase-3 proof modules
# key their ``AcceptanceModule.id`` against these tokens.  Flagged at plan
# time as needing t2556-orch/HO sign-off (P5) -- not purely a local call.
# ---------------------------------------------------------------------------

_KEEP_GOING_BASE = "pantheon-keep-going"
_ALERT_TARGET_PREFIX = "pantheon-unit-failure-alert@"

# Open, not a closed enum: this is the CORE catalog this module curates for
# the sources it discovers. orch (T-2556 phase 15+) attaches further ids
# (forced-cap-return, telegram-failure-modes, <core-id>-live suffixes,
# optional-tier ids) as later phases land -- capture_parity_oracles() below
# tolerates any id not (yet) in PARITY_ORACLES rather than rejecting it, so
# those to-come ids attach cleanly without a change here.
EVENT_MODULE_ID: dict[str, str] = {
    "SessionStart": "hook-session-start",
    "PreToolUse": "hook-pre-tool-use",
    "Stop": "hook-stop",
    "StopFailure": "hook-stop-failure",
    "SubagentStop": "hook-subagent-stop",
}

_UNIT_INSTANCE_RE = re.compile(r"^(?P<base>[^@]+)@(?P<instance>[^.]+)\.(?P<kind>service|timer|path)$")


def _parsed_instance(unit: str) -> tuple[str, str, str] | None:
    """(base, instance, kind) for a templated ``base@instance.kind`` unit, else None."""

    match = _UNIT_INSTANCE_RE.match(unit)
    if not match:
        return None
    return match.group("base"), match.group("instance"), match.group("kind")


def is_templated_for_persona(unit: str, persona: str) -> bool:
    """True iff ``unit`` is a ``@<persona>`` systemd instance."""

    parsed = _parsed_instance(unit)
    return parsed is not None and parsed[1] == persona


# ---------------------------------------------------------------------------
# Source (a): instance.yaml role->persona binding.
# ---------------------------------------------------------------------------


def discover_harness_runtime(binding: BindingFacts) -> tuple[InventoryPathEntry, ...]:
    """The live runtime/session path, plus one entry per configured fallback.

    No-sampling: every configured fallback reference is its own distinct
    path_id under the coarse ``harness-fallback-swap`` module_id, not
    collapsed to "a fallback exists".
    """

    runtime_entry = InventoryPathEntry(
        path=Path(binding.tmux_target),
        tier="core",
        status="applies",
        module_id="harness-runtime",
    )
    if binding.fallbacks:
        fallback_entries = tuple(
            InventoryPathEntry(
                path=Path(f"fallback:{reference}"),
                tier="core",
                status="applies",
                module_id="harness-fallback-swap",
            )
            for reference in binding.fallbacks
        )
    else:
        fallback_entries = (
            InventoryPathEntry(
                path=Path(f"fallback:{binding.persona}:none-configured"),
                tier="core",
                status="absent",
                module_id="harness-fallback-swap",
            ),
        )
    return (runtime_entry, *fallback_entries)


# ---------------------------------------------------------------------------
# Source (b): installed systemd units/timers.
# ---------------------------------------------------------------------------


def parse_list_units_output(
    text: str, *, on_failure: Mapping[str, str] | None = None
) -> tuple[UnitRecord, ...]:
    """Parse ``systemctl ... list-units --all --plain --no-legend`` text.

    Columns are ``UNIT LOAD ACTIVE SUB DESCRIPTION...`` (description is
    free text and may itself contain spaces, so only the first four columns
    are split out). ``on_failure`` merges in a separate ``systemctl show -p
    OnFailure --value <unit>`` read, keyed by unit name -- that property is
    not part of list-units' own columns.
    """

    on_failure = on_failure or {}
    records: list[UnitRecord] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 4)
        if len(parts) < 4:
            continue
        unit, _load, active, sub = parts[:4]
        records.append(
            UnitRecord(unit=unit, active=active, sub=sub, on_failure=on_failure.get(unit))
        )
    return tuple(records)


def _systemd_module_id(base: str, kind: str, kinds_for_instance: set[str]) -> str:
    """Classify one persona-scoped unit into its coarse proof-module id.

    ``keep-going`` is its own distinguished producer regardless of kind (it
    has a dedicated proof module per the T-2578 addendum). Otherwise the
    split follows the parent ticket's OWN taxonomy, which lists "cron/
    systemd timer emissions" and "polling/watch loops" as two distinct
    producer categories: a ``.path`` unit -- or a ``.service`` unit with a
    sibling ``.path`` in the SAME discovered batch (the service is the
    watch's payload, e.g. mailbox-wake's service triggered by its path
    unit) -- is watch-driven, not schedule-driven, so it gets its own
    ``watch-producer`` id rather than being folded into ``timer-cron``
    (which would silently misdescribe it as schedule-triggered).
    """

    if base == _KEEP_GOING_BASE:
        return "keep-going-kicker"
    if kind == "path" or (kind == "service" and "path" in kinds_for_instance):
        return "watch-producer"
    return "timer-cron"


def discover_systemd(
    units: Sequence[UnitRecord], *, persona: str
) -> tuple[tuple[InventoryPathEntry, ...], tuple[OutOfScopeUnit, ...]]:
    """Apply the Q4 filter and tag every in-scope unit with its module_id.

    No-sampling: every discovered unit is its own InventoryPathEntry row --
    N persona timers under ``timer-cron`` means N rows, never one collapsed
    row for the module_id (the module_id is the later PROVER, not a
    dedupe key; each configured path gets nonce-proven separately).
    """

    # Precompute the kind-set per (base, instance) so a bare .service can see
    # whether a sibling .path/.timer exists in THIS SAME discovered batch.
    kinds_by_instance: dict[tuple[str, str], set[str]] = {}
    for record in units:
        parsed = _parsed_instance(record.unit)
        if parsed is not None:
            base, instance, kind = parsed
            kinds_by_instance.setdefault((base, instance), set()).add(kind)

    entries: list[InventoryPathEntry] = []
    out_of_scope: list[OutOfScopeUnit] = []
    for record in units:
        parsed = _parsed_instance(record.unit)
        if parsed is None:
            # Host-level (non-templated) unit: out of Q4 scope by default.
            out_of_scope.append(
                OutOfScopeUnit(unit=record.unit, reason="host-hygiene timer, out of Q4 scope")
            )
            if record.on_failure and record.on_failure.startswith(_ALERT_TARGET_PREFIX):
                # ... except its alert-into-mailbox leg IS delivery into the daemon.
                entries.append(
                    InventoryPathEntry(
                        path=Path(record.unit),
                        tier="optional",
                        status="applies",
                        module_id="unit-failure-alert-into-mailbox",
                    )
                )
            continue
        base, instance, kind = parsed
        if instance != persona:
            # Templated for a different instance -- shouldn't appear once
            # scoped to one account's `--machine=`, but guard anyway rather
            # than silently misclassify it as this persona's.
            out_of_scope.append(
                OutOfScopeUnit(
                    unit=record.unit,
                    reason="templated instance not addressed to this persona",
                )
            )
            continue
        module_id = _systemd_module_id(base, kind, kinds_by_instance[(base, instance)])
        entries.append(
            InventoryPathEntry(
                path=Path(record.unit), tier="core", status="applies", module_id=module_id
            )
        )
    return tuple(entries), tuple(out_of_scope)


# ---------------------------------------------------------------------------
# Source (c): hooks wired in the account's settings.json.
# ---------------------------------------------------------------------------


def _event_commands(event_value: object) -> list[str]:
    """Every command string under one event's value, across the shapes it
    takes -- generalized from deploy_hook.py's T-2320 "scan every event"
    widening (a bare string, a group with a top-level command, or a group
    with a nested ``hooks`` list of ``{"type": "command", "command": ...}``).
    """

    commands: list[str] = []
    if not isinstance(event_value, list):
        return commands
    for group in event_value:
        if isinstance(group, str):
            commands.append(group)
            continue
        if not isinstance(group, dict):
            continue
        if isinstance(group.get("command"), str):
            commands.append(group["command"])
        inner = group.get("hooks")
        if isinstance(inner, list):
            for item in inner:
                if isinstance(item, dict) and isinstance(item.get("command"), str):
                    commands.append(item["command"])
    return commands


def discover_hooks(settings_json_text: str | None, account: str) -> tuple[InventoryPathEntry, ...]:
    """One entry per target event (SessionStart/PreToolUse/Stop/StopFailure/
    SubagentStop), tier optional (persona-specific customization).

    ``InventoryStatus`` has no UNKNOWN state (only applies/absent) -- an
    unreadable or unparseable settings.json therefore folds to "absent" for
    every event, the same posture deploy_hook.py's own decision table uses
    for a genuinely-absent settings.json (a lint bug or a locked-down file
    must never brick discovery).
    """

    events: dict[str, list[str]] = {}
    if settings_json_text:
        try:
            data = json.loads(settings_json_text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            hooks = data.get("hooks")
            if isinstance(hooks, dict):
                for event, value in hooks.items():
                    commands = _event_commands(value)
                    if commands:
                        events[event] = commands

    entries: list[InventoryPathEntry] = []
    for event, module_id in EVENT_MODULE_ID.items():
        commands = events.get(event, [])
        if not commands:
            entries.append(
                InventoryPathEntry(
                    path=Path(f"/home/{account}/.claude/settings.json#{event}"),
                    tier="optional",
                    status="absent",
                    module_id=module_id,
                )
            )
            continue
        # No-sampling: every registered command under this event is its own
        # distinct path_id -- module_id stays the coarse per-event prover,
        # but two hooks wired to the same event are two rows, not one.
        for command in commands:
            entries.append(
                InventoryPathEntry(
                    path=Path(command), tier="optional", status="applies", module_id=module_id
                )
            )
    return tuple(entries)


# ---------------------------------------------------------------------------
# Source (d): mailbox hub subdir.
# ---------------------------------------------------------------------------


def discover_mailbox(persona: str, hub: Path, mailbox_present: bool) -> InventoryPathEntry:
    return InventoryPathEntry(
        path=hub / persona,
        tier="core",
        status="applies" if mailbox_present else "absent",
        module_id="mailbox-roundtrip",
    )


# ---------------------------------------------------------------------------
# Source (e): Telegram channel config, plus the Q3 finding.
# ---------------------------------------------------------------------------


def discover_telegram(
    account: str, observation: TelegramObservation
) -> tuple[tuple[InventoryPathEntry, ...], TelegramFinding]:
    path = Path(f"/home/{account}/.claude/channels/telegram/.env")
    status = "applies" if observation.bridge_env_present else "absent"
    entries = (
        InventoryPathEntry(path=path, tier="core", status=status, module_id="telegram-inbound"),
        InventoryPathEntry(path=path, tier="core", status=status, module_id="telegram-outbound"),
    )
    if observation.dedicated_test_chat is True:
        note = "dedicated test-chat / independent reader confirmed configured"
    elif observation.dedicated_test_chat is False:
        note = "confirmed NO dedicated test-chat / independent reader is configured"
    else:
        note = (
            "unknown -- not determinable from ground truth files; requires a "
            "manual confirmation before this can be called proven either way"
        )
    finding = TelegramFinding(
        dedicated_test_chat_configured=observation.dedicated_test_chat,
        note=note,
        dedicated_test_chat_id=observation.dedicated_test_chat_id,
    )
    return entries, finding


# ---------------------------------------------------------------------------
# Parity-oracle catalog (the operator's addendum, feeds T-2578).
# ---------------------------------------------------------------------------


def _hook_oracle(event: str) -> ParityOracle:
    return ParityOracle(
        trigger=f"harness-native {event} lifecycle event (Claude Code hook contract, not Pantheon-authored)",
        injection_shape="registered command string in ~/.claude/settings.json, invoked by the harness itself",
        dedupe_rate_limit="none observed at this layer -- the harness owns firing cadence",
        resume_behavior="n/a -- a hook does not carry session state across runs",
        source="tools/deploy_hook.py (parse_registration), tools/hook_allowlist_lint.py",
    )


PARITY_ORACLES: dict[str, ParityOracle] = {
    "harness-runtime": ParityOracle(
        trigger="mailbox_wake.decide() on unread mail in new/, gated on pane idle",
        injection_shape="tmux pane inject + submit-verify via send_keys_submit.sh",
        dedupe_rate_limit=(
            "one wake per marker generation; IDLE_REKICK_REASON re-kicks only if the "
            "pane went idle since the last kick with mail still unread"
        ),
        resume_behavior=(
            "session_id persisted across a wake; continuation_prompt vs "
            "orientation_prompt chosen on whether a session_id is on file"
        ),
        source="tools/mailbox_wake.py, tools/persona_bringup.py",
    ),
    "harness-fallback-swap": ParityOracle(
        trigger="decide_failover() on a fresh quota_snapshot vs the manifest FailoverPolicy",
        injection_shape="runtime_controller.planned_swap()/forced_swap() to the configured fallback binding",
        dedupe_rate_limit=(
            "one swap per runtime_controller transition_state phase; "
            "assert_drain_allowed bars a second handler mid-transition"
        ),
        resume_behavior="primary_recovery_evidence() requires a fresh reset+health proof before returning to primary",
        source="tools/persona_bringup.py, tools/runtime_controller.py",
    ),
    "timer-cron": ParityOracle(
        trigger="systemd --user timer unit templated @<persona>",
        injection_shape="varies per unit -- generic catch-all for persona-scoped timers not separately catalogued",
        dedupe_rate_limit="per-unit; not modeled generically at the inventory layer",
        resume_behavior="per-unit; not modeled generically at the inventory layer",
        source="tools/systemd/",
    ),
    "keep-going-kicker": ParityOracle(
        trigger="keep_going.py --agent <persona>, gated on is_idle_at_prompt() AND has_queued_work()",
        injection_shape="pane inject via the same send_keys_submit.sh path as the mailbox waker",
        dedupe_rate_limit="decide_nudge() adaptive backoff state on disk; a new session_id resets the backoff",
        resume_behavior="a plain pane nudge, not a mailbox message -- no ack/consume leg of its own",
        source="tools/keep_going.py",
    ),
    "unit-failure-alert-into-mailbox": ParityOracle(
        trigger="systemd OnFailure=pantheon-unit-failure-alert@%n.service on the referring unit",
        injection_shape="sg pantheon-mailbox -c python3 unit_failure_alert.py --unit %i, mailbox.send() into this account's own inbox",
        dedupe_rate_limit=(
            "leaf unit, deliberately no OnFailure of its own and no RemainAfterExit "
            "(T-1868) -- a failing alert must never storm"
        ),
        resume_behavior="one-shot; not correlated to a runtime session",
        source="tools/systemd/pantheon-unit-failure-alert@.service, tools/unit_failure_alert.py",
    ),
    "watch-producer": ParityOracle(
        trigger="inotify .path unit watching new/ for a file-drop (systemd path-unit, not a timer)",
        injection_shape=(
            "the .path unit activates its paired .service on any change under the "
            "watched dir; same tmux pane inject as harness-runtime's wake leg"
        ),
        dedupe_rate_limit=(
            "mailbox_wake.decide()/deliverable() dedupe via .wake_marker + "
            "IDLE_REKICK_REASON, same mechanism as harness-runtime"
        ),
        resume_behavior=(
            "event-driven, not schedule-driven -- fires on the NEXT filesystem "
            "change after the watch is (re)armed, not on a clock"
        ),
        source="tools/systemd/pantheon-mailbox-wake@.path, tools/mailbox_wake.py",
    ),
    "mailbox-roundtrip": ParityOracle(
        trigger="new/ readdir rescan on every consume() call (never trusts inotify to enumerate)",
        injection_shape="Maildir-style file drop tmp/->new/, at-least-once ack via new/->cur/",
        dedupe_rate_limit=(
            "handler MUST be idempotent; DLQ after dlq_max_attempts (default 3) identical-"
            "failure retries; a mailbox_transient-flagged exception exempts from DLQ"
        ),
        resume_behavior=(
            "unread mail simply sits in new/ across a restart/cap-out; next consume() "
            "call picks it up -- durability is the filesystem's, not a daemon's"
        ),
        source="tools/mailbox.py",
    ),
    "telegram-inbound": ParityOracle(
        trigger="Telegram Bot API long-poll/webhook via the plugin bridge",
        injection_shape='channel envelope <channel source="telegram" chat_id=...> delivered into the session',
        dedupe_rate_limit="single-poller rule (see reference_telegram_channel_diagnosis)",
        resume_behavior="not session-correlated; each inbound message is its own turn trigger",
        source="tools/channel_liveness_monitor.py",
    ),
    "telegram-outbound": ParityOracle(
        trigger="mcp__plugin_telegram_telegram__reply call",
        injection_shape=(
            "Bot API sendMessage; a sent receipt is NOT the same proof as user-visible "
            "delivery (T-2556's own stated gap)"
        ),
        dedupe_rate_limit="none observed at this layer",
        resume_behavior="n/a -- outbound, not resumable",
        source="tools/channel_liveness_monitor.py, reference_telegram_botapi_sendmessage_fallback",
    ),
    **{module_id: _hook_oracle(event) for event, module_id in EVENT_MODULE_ID.items()},
}


_UNCATALOGUED_ORACLE = ParityOracle(
    trigger="not yet catalogued",
    injection_shape="not yet catalogued",
    dedupe_rate_limit="not yet catalogued",
    resume_behavior="not yet catalogued",
    source="none -- module_id has no PARITY_ORACLES entry yet",
)


def capture_parity_oracles(module_ids: Iterable[str]) -> dict[str, ParityOracle]:
    """Return the curated oracle for every id in ``module_ids`` (order-stable, deduped).

    PARITY_ORACLES is deliberately an OPEN catalog, not a closed enum: later
    T-2556 phases (forced-cap-return, telegram-failure-modes, <core-id>-live
    suffixes, optional-tier ids) attach further module_ids over time. An id
    with no curated entry yet gets :data:`_UNCATALOGUED_ORACLE` rather than
    a KeyError -- discovery must keep mapping the ids that actually exist on
    a target even before every one of them has a written-up oracle.
    """

    return {
        module_id: PARITY_ORACLES.get(module_id, _UNCATALOGUED_ORACLE)
        for module_id in dict.fromkeys(module_ids)
    }


# ---------------------------------------------------------------------------
# Top-level combinator + evidence-tree write.
# ---------------------------------------------------------------------------


def build_inventory(
    *,
    binding: BindingFacts,
    units: Sequence[UnitRecord],
    settings_json_text: str | None,
    mailbox_hub: Path,
    mailbox_present: bool,
    telegram: TelegramObservation,
) -> DiscoveryResult:
    """Combine every source into one :class:`DiscoveryResult`."""

    harness_entries = discover_harness_runtime(binding)
    systemd_entries, out_of_scope = discover_systemd(units, persona=binding.persona)
    hook_entries = discover_hooks(settings_json_text, binding.account)
    mailbox_entry = discover_mailbox(binding.persona, mailbox_hub, mailbox_present)
    telegram_entries, telegram_finding = discover_telegram(binding.account, telegram)

    all_entries = (
        *harness_entries,
        *systemd_entries,
        *hook_entries,
        mailbox_entry,
        *telegram_entries,
    )
    inventory = TargetInventory(persona=binding.persona, entries=all_entries)
    oracles = capture_parity_oracles(entry.module_id for entry in all_entries)
    return DiscoveryResult(
        inventory=inventory,
        out_of_scope=out_of_scope,
        parity_oracles=oracles,
        telegram_finding=telegram_finding,
    )


def write_discovery_result(paths: RunPaths, result: DiscoveryResult) -> None:
    """Write ``inventory.json`` via the landed schema, plus a sibling
    ``inventory_discovery.json`` for the Q4/parity-oracle/Q3 context that
    doesn't fit inside :class:`TargetInventory`'s fixed shape.
    """

    write_inventory(paths, result.inventory)
    write_json_evidence(
        paths.run_dir / "inventory_discovery.json",
        {
            "schema": "pantheon-bringup-acceptance-inventory-discovery/v1",
            "persona": result.inventory.persona,
            "out_of_scope": [
                {"unit": entry.unit, "reason": entry.reason} for entry in result.out_of_scope
            ],
            "parity_oracles": {
                module_id: {
                    "trigger": oracle.trigger,
                    "injection_shape": oracle.injection_shape,
                    "dedupe_rate_limit": oracle.dedupe_rate_limit,
                    "resume_behavior": oracle.resume_behavior,
                    "source": oracle.source,
                }
                for module_id, oracle in result.parity_oracles.items()
            },
            "telegram_finding": {
                "dedicated_test_chat_configured": result.telegram_finding.dedicated_test_chat_configured,
                "note": result.telegram_finding.note,
                "dedicated_test_chat_id": result.telegram_finding.dedicated_test_chat_id,
            },
        },
    )
