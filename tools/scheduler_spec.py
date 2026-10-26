"""tools/scheduler_spec.py — ORG-064 General Scheduler, phase 1: the `schedules/`
spec schema + loader + merge (T-2896).

WHAT THIS OWNS. The declarative source of truth for fleet scheduling policy: a
`schedules/` directory holding one `global.yaml` (host + all-persona entries) plus
zero or more `<persona>.yaml` files (that persona's own per-persona entries, and its
exceptions to all-persona entries). This module loads + validates that directory into
a `Spec` and computes the effective per-persona / per-entry schedule. It renders
NOTHING and touches no systemd state — rendering + diffing is `scheduler_reconcile.py`.

ENTRY SCHEMA (ADR ORG-064 §1a). Every entry carries: `name` (the systemd unit
basename, e.g. "pantheon-watch" — both `<name>.service` and `<name>.timer` are
implied), `scope` (host | all-persona | per-persona), `calendar` (an OnCalendar=
string — see the KNOWN GAP note below), `unit` (the payload ref: either a `template:`
pointing at an existing `tools/systemd/<name>.service`, taken VERBATIM, or an inline
exec spec for a net-new schedule with no hand-authored template yet), `on_failure`
("standard" wires `pantheon-unit-failure-alert@%n.service`, "none" wires nothing —
the ORG-020/T-2178 precedent for root/system units that user-scoped alerting cannot
see), `owner` (steward persona), `serves` (one-line why), and optionally `system`
(the ORG-081 registry id, i.e. the registry/systems.yaml row, this entry's cadence
realizes; the ORG-068 matrix is now rendered from that registry. Legacy human titles
are accepted only as ORG-081 D-13 aliases. OPTIONAL-WITH-WARN: absence is recorded in
`Spec.warnings`, never a load error, because not every schedule maps cleanly onto one
registry row).

`persona` is deliberately NOT a settable field on a per-persona entry — it is
DERIVED from the filename (`schedules/specialist.yaml` → persona "specialist"). Letting it
also be a body field would let the two disagree; there would be no principled way
to decide which one is authoritative, and whichever a reader picked would be a coin
flip. So the loader treats an explicit `persona:` key in a persona file as an error,
not as confirmation.

EXCEPTION SCHEMA (ADR ORG-064 §1b). Declared under a persona file's `exceptions:`
list; each references a **global all-persona** entry by `name` and is EXACTLY one of:
  - **opt-out** — `reason` REQUIRED (waiver-with-rationale; never a silent absence).
  - **time-override** — `calendar` REQUIRED (same job, shifted).
Exceptions may touch WHEN or WHETHER, never WHAT: the loader rejects any exception
body carrying a field outside {entry, kind, reason, calendar} (in particular, no
`unit`, `owner`, `on_failure`, `system`, `persistent`) and rejects any exception
whose target entry is not scope=all-persona (a per-persona or host entry has no
fleet default to opt out of).

NOT-YET-MIGRATED vs OPTED-OUT — a real distinction the ADR does not itself name,
found while seeding this phase's exemplars (T-2896 plan review). An opt-out
EXCEPTION asserts a deliberate, reasoned policy deviation from a fleet-wide default
that DOES apply to this persona in general. The ABSENCE of any entry for a persona
asserts nothing at all — it is simply the default state of a schedule that has not
yet been decided as fleet-wide policy, or has not yet been migrated into the spec
(the T-2662 census: 51 hand-authored timers, most not yet represented here). Do not
manufacture opt-out records to "cover" a persona a schedule merely hasn't reached
yet — that fabricates a decision nobody made and misreports rollout-in-progress as
policy. (This is why `pantheon-memory-lint` below is modeled scope=per-persona:orchestrator
rather than all-persona + six manufactured opt-outs — see scheduler_reconcile.py's
seed data / the T-2896 report for the worked example.) T-2662's migration inherits
this distinction: promoting a per-persona entry to all-persona is a real policy
decision, not a schema mechanism, and every persona not yet covered stays silent
until that decision is made — never opted-out by default.

KNOWN GAP (phase 1, honest scope limit). `calendar` is OnCalendar= syntax only, per
ADR §1a's literal wording. The 7-unit "per-persona runtime" cluster from the T-2662
census (persona-consume, keep-going, mailbox-wake, inbox-consumer,
context-fill-monitor, activity-monitor, acp-bridge-backlog) are monotonic
OnUnitActiveSec/OnBootSec/OnActiveSec timers, not OnCalendar — this schema does not
model them. That is a real finding, not an oversight papered over: T-2662's
migration either extends this schema with an interval-timer variant or treats that
cluster as structurally distinct. Left open here on purpose.

Persona-name validation (per-persona scope, exception `entry`/persona-file naming)
resolves against `agent_role_map.AGENT_ROLE_MAP` — the fleet's canonical persona
namespace (T-0492/msg_1661) — not a second hand-carried list.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_role_map as arm  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEDULES_DIR = REPO_ROOT / "schedules"
UNIT_TEMPLATE_DIR = REPO_ROOT / "tools" / "systemd"
GLOBAL_FILE = "global.yaml"

SCOPE_HOST = "host"
SCOPE_ALL_PERSONA = "all-persona"
SCOPE_PER_PERSONA = "per-persona"
_SCOPES = frozenset({SCOPE_HOST, SCOPE_ALL_PERSONA, SCOPE_PER_PERSONA})

ON_FAILURE_STANDARD = "standard"
ON_FAILURE_NONE = "none"
_ON_FAILURE_VALUES = frozenset({ON_FAILURE_STANDARD, ON_FAILURE_NONE})

KIND_OPT_OUT = "opt-out"
KIND_TIME_OVERRIDE = "time-override"
_EXCEPTION_KINDS = frozenset({KIND_OPT_OUT, KIND_TIME_OVERRIDE})

_REQUIRED_ENTRY_FIELDS = ("name", "scope", "calendar", "unit", "on_failure", "owner", "serves")
_OPTIONAL_ENTRY_FIELDS = frozenset({"system", "persistent"})
_KNOWN_ENTRY_FIELDS = frozenset(_REQUIRED_ENTRY_FIELDS) | _OPTIONAL_ENTRY_FIELDS

_INLINE_REQUIRED_UNIT_FIELDS = frozenset({"description", "exec_start"})
_INLINE_OPTIONAL_UNIT_FIELDS = frozenset({
    "working_directory", "environment", "type", "timeout_start_sec", "after", "wants",
})
_INLINE_UNIT_FIELDS = _INLINE_REQUIRED_UNIT_FIELDS | _INLINE_OPTIONAL_UNIT_FIELDS

_EXCEPTION_ALLOWED_FIELDS = frozenset({"entry", "kind", "reason", "calendar"})


class SpecError(ValueError):
    """A `schedules/` spec failed to load or validate. Always carries the source
    filename (and entry/exception name where known) in its message — this is a
    fail-fast config error, not a runtime condition a caller should catch and
    route around."""


@dataclass(frozen=True)
class UnitPayload:
    """What an entry runs. Exactly one of `template` (verbatim passthrough of an
    existing tracked `tools/systemd/<template>.service`) or the inline fields is
    populated — never both, never neither (`_parse_unit_payload` enforces this)."""

    template: str | None = None
    description: str | None = None
    exec_start: str | None = None
    working_directory: str | None = None
    environment: tuple[str, ...] = ()
    unit_type: str = "oneshot"
    timeout_start_sec: str | None = None
    after: tuple[str, ...] = ()
    wants: tuple[str, ...] = ()

    @property
    def is_template(self) -> bool:
        return self.template is not None


@dataclass(frozen=True)
class Entry:
    name: str
    scope: str
    calendar: str
    unit: UnitPayload
    on_failure: str
    owner: str
    serves: str
    system: str | None = None
    persistent: bool = True
    persona: str | None = None  # set by the loader from the filename; None for host/all-persona
    source_file: str = ""


@dataclass(frozen=True)
class ScheduleException:
    entry: str
    kind: str
    persona: str
    reason: str | None = None
    calendar: str | None = None
    source_file: str = ""


@dataclass(frozen=True)
class Spec:
    entries: dict[str, Entry]
    exceptions: dict[str, list[ScheduleException]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EffectiveEntry:
    """One concrete (entry, persona) pairing after exception merge — the calendar
    a time-override exception won, or the entry's own calendar unchanged."""

    entry: Entry
    persona: str | None
    calendar: str
    persistent: bool


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SpecError(f"{path.name}: invalid YAML — {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError(f"{path.name}: top-level YAML must be a mapping")
    extra = set(data) - {"entries", "exceptions"}
    if extra:
        raise SpecError(f"{path.name}: unknown top-level key(s): {', '.join(sorted(extra))}")
    return data


def _parse_unit_payload(raw: object, *, source_file: str, name: str, template_dir: Path) -> UnitPayload:
    if not isinstance(raw, dict):
        raise SpecError(f"{source_file}: entry '{name}' unit must be a mapping")
    keys = set(raw)
    if "template" in keys:
        if keys != {"template"}:
            raise SpecError(
                f"{source_file}: entry '{name}' unit.template is exclusive — "
                f"no other unit field allowed alongside it"
            )
        template = raw["template"]
        svc_path = template_dir / f"{template}.service"
        if not svc_path.is_file():
            raise SpecError(
                f"{source_file}: entry '{name}' unit.template '{template}' has no "
                f"{svc_path.name} under {template_dir}"
            )
        return UnitPayload(template=template)

    missing = _INLINE_REQUIRED_UNIT_FIELDS - keys
    if missing:
        raise SpecError(
            f"{source_file}: entry '{name}' unit is missing required field(s) "
            f"{', '.join(sorted(missing))} (or set unit.template instead)"
        )
    extra = keys - _INLINE_UNIT_FIELDS
    if extra:
        raise SpecError(f"{source_file}: entry '{name}' unit has unknown field(s): {', '.join(sorted(extra))}")
    return UnitPayload(
        description=raw["description"],
        exec_start=raw["exec_start"],
        working_directory=raw.get("working_directory"),
        environment=tuple(raw.get("environment", [])),
        unit_type=raw.get("type", "oneshot"),
        timeout_start_sec=raw.get("timeout_start_sec"),
        after=tuple(raw.get("after", [])),
        wants=tuple(raw.get("wants", [])),
    )


def _parse_entry(
    raw: object,
    *,
    source_file: str,
    persona: str | None,
    template_dir: Path,
    warnings: list[str],
) -> Entry:
    if not isinstance(raw, dict):
        raise SpecError(f"{source_file}: entry must be a mapping, got {type(raw).__name__}")
    if persona is not None and "persona" in raw:
        raise SpecError(
            f"{source_file}: entry '{raw.get('name', '<unnamed>')}' sets 'persona' explicitly — "
            f"persona is implicit from the filename, remove the field"
        )
    missing = [f for f in _REQUIRED_ENTRY_FIELDS if f not in raw]
    if missing:
        raise SpecError(
            f"{source_file}: entry '{raw.get('name', '<unnamed>')}' missing required "
            f"field(s): {', '.join(missing)}"
        )
    extra = set(raw) - _KNOWN_ENTRY_FIELDS
    if extra:
        name = raw.get("name", "<unnamed>")
        raise SpecError(f"{source_file}: entry '{name}' has unknown field(s): {', '.join(sorted(extra))}")

    name = raw["name"]
    scope = raw["scope"]
    if scope not in _SCOPES:
        raise SpecError(
            f"{source_file}: entry '{name}' has invalid scope '{scope}' "
            f"(must be one of {sorted(_SCOPES)})"
        )
    on_failure = raw["on_failure"]
    if on_failure not in _ON_FAILURE_VALUES:
        raise SpecError(
            f"{source_file}: entry '{name}' has invalid on_failure '{on_failure}' "
            f"(must be one of {sorted(_ON_FAILURE_VALUES)})"
        )
    persistent = raw.get("persistent", True)
    if not isinstance(persistent, bool):
        raise SpecError(f"{source_file}: entry '{name}' persistent must be a bool")
    system = raw.get("system")
    if not system:
        warnings.append(
            f"{source_file}: entry '{name}' has no 'system' (ORG-081 registry id) set "
            f"— optional-with-warn per ORG-064 §1a"
        )
    unit = _parse_unit_payload(raw["unit"], source_file=source_file, name=name, template_dir=template_dir)

    return Entry(
        name=name,
        scope=scope,
        calendar=raw["calendar"],
        unit=unit,
        on_failure=on_failure,
        owner=raw["owner"],
        serves=raw["serves"],
        system=system,
        persistent=persistent,
        persona=persona,
        source_file=source_file,
    )


def _parse_exception(raw: object, *, persona: str, source_file: str) -> ScheduleException:
    if not isinstance(raw, dict):
        raise SpecError(f"{source_file}: exception must be a mapping")
    extra = set(raw) - _EXCEPTION_ALLOWED_FIELDS
    if extra:
        raise SpecError(
            f"{source_file}: exception has field(s) outside WHEN/WHETHER — "
            f"{', '.join(sorted(extra))} not allowed (ADR ORG-064 §1b: exceptions may touch "
            f"WHEN or WHETHER, never WHAT)"
        )
    if "entry" not in raw or "kind" not in raw:
        raise SpecError(f"{source_file}: exception missing required field(s) 'entry'/'kind'")
    entry_name = raw["entry"]
    kind = raw["kind"]
    if kind not in _EXCEPTION_KINDS:
        raise SpecError(
            f"{source_file}: exception for '{entry_name}' has invalid kind '{kind}' "
            f"(must be one of {sorted(_EXCEPTION_KINDS)})"
        )
    reason = raw.get("reason")
    calendar = raw.get("calendar")
    if kind == KIND_OPT_OUT:
        if not reason:
            raise SpecError(
                f"{source_file}: opt-out exception for '{entry_name}' requires a non-empty "
                f"'reason' (waiver-with-rationale, ADR ORG-064 §1b)"
            )
        if calendar is not None:
            raise SpecError(
                f"{source_file}: opt-out exception for '{entry_name}' must not set 'calendar' "
                f"(opt-out and time-override are exclusive)"
            )
    else:
        if not calendar:
            raise SpecError(f"{source_file}: time-override exception for '{entry_name}' requires 'calendar'")

    return ScheduleException(
        entry=entry_name, kind=kind, persona=persona, reason=reason, calendar=calendar, source_file=source_file,
    )


def _validate_exception_target(exc: ScheduleException, entries: dict[str, Entry], source_file: str) -> None:
    target = entries.get(exc.entry)
    if target is None:
        raise SpecError(f"{source_file}: exception references unknown entry '{exc.entry}'")
    if target.scope != SCOPE_ALL_PERSONA:
        raise SpecError(
            f"{source_file}: exception references entry '{exc.entry}' with scope={target.scope} — "
            f"exceptions may only target scope={SCOPE_ALL_PERSONA} entries"
        )


def _add_entry(entries: dict[str, Entry], entry: Entry) -> None:
    if entry.name in entries:
        raise SpecError(
            f"duplicate entry name '{entry.name}' ({entries[entry.name].source_file} vs {entry.source_file})"
        )
    entries[entry.name] = entry


def load_spec(schedules_dir: Path = SCHEDULES_DIR, template_dir: Path = UNIT_TEMPLATE_DIR) -> Spec:
    """Load + validate every file under `schedules_dir` into a `Spec`.

    Raises `SpecError` on any structural/semantic violation (missing fields, bad
    scope placement, an exception that touches WHAT, etc.) — this is fail-fast
    config validation, not a partial-success API. `Spec.warnings` carries
    non-fatal findings (currently: entries with no `system` field) that a caller
    may choose to surface without failing the load.
    """
    if not schedules_dir.is_dir():
        raise SpecError(f"schedules dir not found: {schedules_dir}")

    entries: dict[str, Entry] = {}
    exceptions: dict[str, list[ScheduleException]] = {}
    warnings: list[str] = []

    global_path = schedules_dir / GLOBAL_FILE
    if global_path.exists():
        raw = _read_yaml(global_path)
        for raw_entry in raw.get("entries", []) or []:
            entry = _parse_entry(
                raw_entry, source_file=GLOBAL_FILE, persona=None, template_dir=template_dir, warnings=warnings,
            )
            if entry.scope == SCOPE_PER_PERSONA:
                raise SpecError(
                    f"{GLOBAL_FILE}: entry '{entry.name}' has scope={SCOPE_PER_PERSONA}; "
                    f"per-persona entries belong in schedules/<persona>.yaml, not {GLOBAL_FILE}"
                )
            _add_entry(entries, entry)
        if raw.get("exceptions"):
            raise SpecError(f"{GLOBAL_FILE}: exceptions may only be declared in a persona file")

    for path in sorted(schedules_dir.glob("*.yaml")):
        if path.name == GLOBAL_FILE:
            continue
        persona = path.stem
        if persona not in arm.AGENT_ROLE_MAP:
            raise SpecError(
                f"{path.name}: filename '{persona}' is not a known persona "
                f"(see agent_role_map.AGENT_ROLE_MAP)"
            )
        raw = _read_yaml(path)
        for raw_entry in raw.get("entries", []) or []:
            entry = _parse_entry(
                raw_entry, source_file=path.name, persona=persona, template_dir=template_dir, warnings=warnings,
            )
            if entry.scope != SCOPE_PER_PERSONA:
                raise SpecError(
                    f"{path.name}: entry '{entry.name}' has scope={entry.scope}; "
                    f"only scope={SCOPE_PER_PERSONA} entries belong in a persona file"
                )
            _add_entry(entries, entry)
        persona_exceptions: list[ScheduleException] = []
        for raw_exc in raw.get("exceptions", []) or []:
            exc = _parse_exception(raw_exc, persona=persona, source_file=path.name)
            _validate_exception_target(exc, entries, path.name)
            if any(e.entry == exc.entry for e in persona_exceptions):
                raise SpecError(
                    f"{path.name}: duplicate exception for entry '{exc.entry}' (persona {persona} "
                    f"already has one)"
                )
            persona_exceptions.append(exc)
        if persona_exceptions:
            exceptions[persona] = persona_exceptions

    return Spec(entries=entries, exceptions=exceptions, warnings=warnings)


# ---------------------------------------------------------------------------
# Merge — persona-level wins over all-persona (ADR §1)
# ---------------------------------------------------------------------------


def fleet_personas() -> tuple[str, ...]:
    """Every persona in the canonical fleet namespace, sorted. The all-persona
    expansion universe — NOT a second hand-carried list (T-2256 precedent)."""
    return tuple(sorted(arm.AGENT_ROLE_MAP))


def _exception_for(spec: Spec, persona: str, entry_name: str) -> ScheduleException | None:
    for exc in spec.exceptions.get(persona, ()):
        if exc.entry == entry_name:
            return exc
    return None


def host_entries(spec: Spec) -> list[Entry]:
    return [e for e in spec.entries.values() if e.scope == SCOPE_HOST]


def effective_schedule(spec: Spec, persona: str) -> list[EffectiveEntry]:
    """Every entry that actually applies to `persona`, calendar already merged
    (an opt-out drops the entry entirely; a time-override replaces the calendar;
    everything else passes its all-persona calendar through unchanged)."""
    out: list[EffectiveEntry] = []
    for entry in spec.entries.values():
        if entry.scope == SCOPE_HOST:
            continue
        if entry.scope == SCOPE_PER_PERSONA:
            if entry.persona == persona:
                out.append(EffectiveEntry(entry, persona, entry.calendar, entry.persistent))
            continue
        exc = _exception_for(spec, persona, entry.name)
        if exc is not None and exc.kind == KIND_OPT_OUT:
            continue
        calendar = exc.calendar if (exc is not None and exc.kind == KIND_TIME_OVERRIDE) else entry.calendar
        out.append(EffectiveEntry(entry, persona, calendar, entry.persistent))
    return sorted(out, key=lambda ee: ee.entry.name)


def targets(spec: Spec, entry: Entry) -> list[EffectiveEntry]:
    """Every (persona, calendar) this entry renders for. Empty for scope=host
    (no persona axis); the entry-centric mirror of `effective_schedule`'s
    persona-centric view, sharing the same opt-out/override lookup."""
    if entry.scope == SCOPE_HOST:
        return []
    if entry.scope == SCOPE_PER_PERSONA:
        return [EffectiveEntry(entry, entry.persona, entry.calendar, entry.persistent)]
    out: list[EffectiveEntry] = []
    for persona in fleet_personas():
        exc = _exception_for(spec, persona, entry.name)
        if exc is not None and exc.kind == KIND_OPT_OUT:
            continue
        calendar = exc.calendar if (exc is not None and exc.kind == KIND_TIME_OVERRIDE) else entry.calendar
        out.append(EffectiveEntry(entry, persona, calendar, entry.persistent))
    return out
