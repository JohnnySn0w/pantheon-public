"""harness-manager component planner (ORG-080 §2, §5; T-5090, germination tranche GF1).

``plan_configure`` is configure sub-op (b), the harness-runtime swap
(``germinate configure --system harness-manager --seat <account> --harness codex``).
It is an ADAPTER over the existing swap (``persona_bringup.operator_target_swap``
-> ``runtime_controller._operator_target_swap_resolved``), which it does not change
and does not import (ruling: the planner never imports runtime_controller). It
uses the leaf modules' pure functions only:

* ``harness_adapter.integration``: ``normalize_harness``,
  ``integration_for(h).receiver_direction`` and ``receiver_handoff_actions``
  (the exact action tuple the bridge handoff would run, recorded as data);
* ``harness_state.validate_runtime`` for runtime references.

Op order follows the existing code (T-5090 DECISION 6), not the ORG-057 §3 text:
stage the transition record -> receiver handoff "pre" (the authority-transfer
gate) -> replace the runtime (postcondition: readiness) -> receiver handoff
"post" -> mark the transition ``awaiting-successor-read``. The successor-read
prompt is send-keys into a model pane, which is off the engine path (D-N).

RE-STATEMENTS over observation data (the A-5 edge; each mirrors one check of
the existing code, which is fused with I/O there):
  R1 compensation-failed refusal        persona_bringup.py:612
  R2 already-active is a validated no-op persona_bringup.py:622-655 /
                                        runtime_controller.py:7733-7762
  R3 held destination refuses           persona_bringup.py:473-500 (no --override-hold: desired is closed)
  R4 destination in primary+fallbacks   persona_bringup.py:744-748
  R5 in-flight transition refuses       persona_bringup.py:657 (phase preparing/awaiting-successor-read)
  R6 receiver handoff only for telegram runtime_controller.py:7311 receiver_handoff_required
  R7 receiver must sit on the source     runtime_controller.py:7954-7961 (preflight _prove_receiver)

INSTALL LEG (T-5091 U1, GF2). ``plan_install`` is an ADAPTER over the live
installer's own pure plan, ``tools.persona_installer.PersonaInstaller.plan``,
called with an explicit ``source_revision`` (``desired.source_revision``, a full
commit id) so the planner never spawns git. It never imports runtime_controller,
persona_installer_apply or any host backend. One sub-run per seat, as that
seat's own account (HO ruling Q3, ORG-080 A-3): scope ``seat-<account>`` only.
Root needs are not planned here; they route through host-substrate (HO ruling
Q1, ORG-080 §Build plan (d) / A-4; T-5091 U2).

Mapping (``_INSTALL_KIND_MAP``): every installer kind is already a typed-op
registry kind, so the engine kind is the installer kind (identity; ORG-080 D-AC
forbids a component re-declaring or re-classing an engine-registry kind, so
``OP_KINDS`` gains nothing). Privilege is the registry's.

  installer kinds                                  engine privilege  crash_contract
  12 diagnostics.read checks (brief-, harness-,    none              none (side_effect_free)
    identity-, manager-, policy-, source-check,
    timer-enablement-absence-check, claude-mcp-
    server-check, codex-standing-instructions-
    {source,target,override}-check, codex-
    telegram-disabled-first)
  mkdir, atomic-write, user-unit-install,          seat              write-ahead-inverse-durable
    seat-periodic-unit-install, codex-standing-
    instructions-install (installer plans an inverse)
  seat-runtime-residue-quarantine,                 seat              write-ahead-inverse-durable
    seat-service-decommission (moves; preimage first)
  the other 17 activation/probe kinds (daemon-     seat              stageable
    reload, unit-fire, timer-enable, mailbox-probe, the seat_local
    registration / settings / periodic kinds, ...)
  (37 kinds in all: 12 none + 25 seat)

Layer, target, precondition, desired, inverse, owner and execution_scope pass
through unchanged. Installer ids (dotted) become the engine alphabet: dots to
``_``, over 64 chars a 47-char prefix + ``-`` + 16 hex of sha256; a collision
refuses. The installer id and privilege label stay in provenance.

SUPERVISOR CUTOVER (T-5830, T-5091 U4, GF2): configure sub-op (a). UNTESTED-LIVE,
built dark (HO ruling 2026-10-27): no seat has a live consumer, so its consumers are
new seats and seed bring-up. ``tools/persona_cutover.sh``, ``tools/persona_rollback.sh``
and ``tools/persona_cutover_resolve.py`` stay untouched and remain the operative path
(T-3444 is not retired). The first live fire is supervised; if an op ever needs root
it is reclassified (a host-substrate family), never widened (Recovery D15 (T-5830)).

``desired.supervisor`` selects it (ORG-080 §2 YAML contract ``desired_schema`` ``supervisor: {enum: [consume,
bespoke]}``); a request naming both ``supervisor`` and ``harness`` refuses (ORG-080 D-L:
two sub-ops, each its own run and gate). Path (b) above is unchanged.

* ``consume`` re-states persona_cutover.sh: snapshot the three bespoke files, the
  optional urgent drop-in (``desired.urgent_env``), daemon-reload, RETIRE the bespoke
  supervisor (gate ``supervisor-cutover``), stop each declared companion, an explicit
  require-quiesced barrier, the installer's own activation suffix
  (``plan.operations[activation_index:]`` through ``_install_ops``, U1's identity
  mapping), restart each companion, then ``harness.supervisor-commit`` carrying the
  soak-crit-4 checks as postconditions.
* ``bespoke`` re-states persona_rollback.sh: restore the files, daemon-reload,
  control-plane teardown, consume retire (the staged-receipt rollback, manifest-pinned
  or break-glass), reinstate the bespoke supervisor (same gate), restart companions,
  commit.

Two rollback paths (ORG-080 D-E / D-X): a FAILED attempt halts and ``rollback(run_id)``
runs the write-ahead inverses; restore after a SUCCESSFUL cutover (a SATISFIED run is
absorbing) is the ``bespoke`` forward plan (ORG-080 §5, "the same sub-op in reverse
with its own gate").

Identity comes from ``persona_cutover_resolve`` UNCHANGED (``resolve_persona`` /
``resolve_bespoke_identity`` with the observed unit text; read-only, proven under
PlannerGuard) over the planner checkout's committed instance.yaml, whose byte digest
must equal the observed target clone's (mason ruling Q2). Every op target is one of
the resolver-derived pins recorded in the plan (Recovery D15 (T-5830) (h)). The six
``harness.supervisor`` kinds are component-local (harness-manager_handlers.py OP_KINDS;
Recovery D15 (T-5830), GF1 ``harness.runtime`` precedent). Shared constants, the
postcondition checks and the step inventory live in harness_manager_supervisor.py.

RUN STATE (T-5858, T-5091 U5, GF2): fleet bringdown/restore as a configure desired state
(HO ruling Q2; ORG-080 A-2, no new leg or verb). UNTESTED-LIVE, built dark: ops run only
through the SIMULATED HarnessRunStateSim. The first live fire is supervised; if an op ever
needs root or another account it is reclassified (a host-substrate family), never widened
(Recovery D15 (T-5858)). ``sop/standard/fleet-daemon-bringdown-and-restore.md`` is untouched
and stays the operational surface (A-7).

``desired.run_state`` (paused | running) selects it, with ``desired.keep`` (exact unit
names left as they are), ``desired.anchor`` (``<principal>:<ref>``), ``stamp`` and
``source_revision``. Naming ``harness`` or ``supervisor`` too refuses (ORG-080 D-L).

* ``paused`` re-states SOP §3: the manifest (atomic + fsync, first), the ORG-106
  seat-state record (Recovery (p)), every timer disable-now (gate ``seat-run-state`` on the
  first), a bounded drain, every service disable-now, reset-failed, then the commit with
  the §4 checks as exact fields and the residue reported ('paused-with-residue').
* ``running`` re-states SOP §5 from the manifest only (never inferred): services then
  timers enable-now, the manifest retired, the commit (every unit back at its recorded
  pre-state, the consume timer scheduled), and the seat-state record cleared LAST.

The per-seat target set comes from data only (the installer's own plan, instance.yaml
companion_units and codex_telegram_bridge receivers, schedules/<persona>.yaml, and the
per-role run_state_units of T-5862); a live §2 unit that is not derivable refuses. The recovery tier (recovery, watchman) refuses; the HO
seat's pause needs a ``operator:`` anchor (Recovery D15 (T-5858) (o)). A failed pause or
restore halts and ``rollback(run_id)`` runs the write-ahead inverses; restore after a
SATISFIED pause is the ``running`` forward plan. Shared data and checks live in
harness_manager_run_state.py.

REMOVE LEG (T-5859, T-5091 U3, GF2): ``plan_remove``, the full-seat decommission (ORG-080
§2 ``remove: plan_remove``; D-E/D-O: decommission only, undoing an attempt is
``rollback(run_id)``). UNTESTED-LIVE, built dark: ops run only through the SIMULATED
HarnessDecommissionSim. The first live fire is supervised; if an op ever needs root or
another account it is reclassified (a host-substrate family), never widened (Recovery D15
(T-5859)). SEAT HALF ONLY (mason ruling (i)): a ``root_half: managed`` seat refuses
``root-half-pending`` (T-5873); an observe-only seat gets no root op and the manifest says
so. QUARANTINE, NEVER DELETE (ORG-052 §8): the inverse of the seat's bring-up (U1's
installer plan, classified artifact by artifact in harness_manager_remove.py) as a
manifest written first (gate ``decommission``, ORG-080 §2 YAML contract gates, with a ``operator:`` anchor for
every seat: Recovery D15 (T-5859) (v)), disable --now of every installer unit still up (timers,
services, the runtime controller last), a same-filesystem rename of every installer
artifact into ``~/.local/state/pantheon/quarantine/decommission/<stamp>/tree/``, a
daemon-reload, and the commit (every unit gone, every source absent, every quarantined
digest, no harness session; routing and cross-seat residue reported:
Recovery D15 (T-5859) (w)/(x)). The seat must already be paused (U5 ``run_state: paused``),
and every Recovery-side unit targeting it (context-fill / activity monitors, restart-drain /
handoff coverage) observed disarmed or absent, at plan and again at apply on the manifest
write (Recovery D15 (T-5859) (x2); disarming them is an operator step before the gate), and
fleet remediation must be vetoed for it (seat-maintenance armed or deep-sleep, the
seat_state.remediation_vetoed semantics; paused-intake alone does not), at plan and again on
the manifest write and the controller stop (Recovery D15 (T-5859) (x3)).
A halt at or after the controller stop escalates (EscalatingHalt); a stopped undo that
would leave the seat half-removed escalates (EscalatingRollbackStop).

Returns typed operation data only.
"""

from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

from tools import harness_state
from tools.germinate.components import harness_manager_remove as R
from tools.germinate.components import harness_manager_run_state as RS
from tools.germinate.components import harness_manager_supervisor as S
from tools.harness_adapter import integration

HANDLER = "harness-manager-seat"
GATE = "authority-transfer"
IN_FLIGHT = frozenset({"preparing", "awaiting-successor-read"})


class PlanRefused(RuntimeError):
    """The swap is refused at plan (a precondition of the existing code fails)."""


def _seat(request):
    observed = (request.get("observed") or {}).get(HANDLER)
    if not isinstance(observed, dict) or not isinstance(observed.get("seat"), dict):
        raise PlanRefused(f"request carries no {HANDLER} seat observation for scope {request.get('scope')!r}")
    return observed["account"], observed["seat"]


_SAME_AS_PRECONDITION = object()


def _op(
    op_id, kind, target, account, precondition, desired, *, crash_contract, provenance, gate=None,
    layer=2, inverse=_SAME_AS_PRECONDITION, privilege="seat", side_effect_free=False, execution_scope=None,
):
    """One engine op as plain data. The keyword defaults are exactly the
    plan_configure shape (T-5090); plan_install (T-5091 U1) overrides them."""

    op = {
        "operation_id": op_id[:64],
        "layer": layer,
        "kind": kind,
        "target": target,
        "precondition": precondition,
        "desired": desired,
        "inverse": dict(precondition) if inverse is _SAME_AS_PRECONDITION else inverse,
        "privilege": privilege,
        "owner": account,
        "provenance": provenance,
        "crash_contract": crash_contract,
        "side_effect_free": side_effect_free,
    }
    if gate is not None:
        op["gate"] = gate
    if execution_scope is not None:
        op["execution_scope"] = execution_scope
    return op


def _validate_active_noop(account, seat, destination, target_harness):
    runtime = seat.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("selected") != destination:
        raise PlanRefused(f"{account}: recorded active runtime does not match requested destination")
    communications = seat.get("communications")
    if isinstance(communications, dict) and communications.get("kind") == "telegram":
        receiver = seat.get("receiver")
        holder = receiver.get("holder") if isinstance(receiver, dict) else None
        if holder != target_harness:
            raise PlanRefused(
                f"{account}: active runtime receiver is held by {holder!r}, not target harness {target_harness!r}"
            )
    readiness = seat.get("readiness")
    if not isinstance(readiness, dict) or readiness.get(destination) is not True:
        raise PlanRefused(f"{account}: active runtime liveness was not positively proven")


def plan_configure(request):
    if (request.get("desired") or {}).get("run_state") is not None:  # run_state, T-5858
        return plan_run_state(request)
    if (request.get("desired") or {}).get("supervisor") is not None:  # sub-op (a), T-5830
        return plan_supervisor(request)
    if request.get("scope") == "system":
        raise PlanRefused("configure (b) requires a seat scope")
    harness = (request.get("desired") or {}).get("harness")
    if not harness:
        raise PlanRefused("configure (b) needs --harness")
    account, seat = _seat(request)
    role = seat.get("harness_runtime_role") or {}
    primary, fallbacks, profiles = role.get("primary"), list(role.get("fallbacks") or []), role.get("profiles") or {}
    current = (seat.get("runtime") or {}).get("selected")
    transition = seat.get("transition") or {}
    phase = transition.get("phase", "none")
    target_harness = integration.normalize_harness(harness)
    if target_harness not in profiles:
        raise PlanRefused(f"{account}: the role declares no {target_harness} profile")
    destination = harness_state.validate_runtime(f"{target_harness}/{profiles[target_harness]}")
    harness_state.validate_runtime(current or "")
    if phase == "compensation-failed":  # R1
        raise PlanRefused(f"{account}: runtime transition compensation failed; manual recovery required")
    if destination == current:  # R2
        _validate_active_noop(account, seat, destination, target_harness)
        return []
    if destination not in [primary, *fallbacks]:  # R4
        raise PlanRefused(f"{account}: destination {destination} is not the primary or a configured fallback")
    if destination in (seat.get("holds") or {}):  # R3
        raise PlanRefused(f"{account}: {destination} is operator-held; refusing the swap")
    if phase in IN_FLIGHT:  # R5
        raise PlanRefused(f"{account}: a runtime transition is already {phase}")

    source_harness = current.split("/", 1)[0]
    direction = integration.integration_for(target_harness).receiver_direction(
        SimpleNamespace(harness=source_harness), SimpleNamespace(harness=target_harness)
    ) or "none"
    communications = seat.get("communications")
    telegram = isinstance(communications, dict) and communications.get("kind") == "telegram"  # R6
    binding = SimpleNamespace(communications=communications)
    seed = json.dumps({"seat": seat, "destination": destination, "account": account}, sort_keys=True)
    transition_id = "t-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    provenance = {
        "adapter_of": "tools.persona_bringup.operator_target_swap -> tools.runtime_controller._operator_target_swap_resolved",
        "pure_functions": "tools.harness_adapter.integration.receiver_direction/receiver_handoff_actions; tools.harness_state.validate_runtime",
        "transition_id": transition_id,
        "source_runtime": current,
        "destination": destination,
    }
    record = {"transition_id": transition_id, "outgoing": current, "incoming": destination}
    ops = [
        _op(
            f"stage-{account}", "harness.transition-stage", f"{account}:transition", account,
            {"phase": phase}, dict(record, phase="preparing"),
            crash_contract="stageable", provenance=provenance,
        )
    ]
    holder = (seat.get("receiver") or {}).get("holder") or "none"
    if telegram and direction != "none" and holder != source_harness:  # R7
        raise PlanRefused(f"{account}: the Telegram receiver is held by {holder!r}, not the source runtime {source_harness!r}")
    if telegram and direction != "none":
        pre = ",".join(integration.receiver_handoff_actions(binding, direction, "pre"))
        ops.append(
            _op(
                f"receiver-pre-{account}", "harness.receiver-handoff", f"{account}:receiver", account,
                {"holder": holder}, {"holder": "none", "direction": direction, "phase": "pre", "actions": pre},
                crash_contract="write-ahead-inverse-durable", provenance=provenance, gate=GATE,
            )
        )
    ops.append(
        _op(
            f"runtime-{account}", "harness.runtime-replace", f"{account}:runtime", account,
            {"selected": current}, {"selected": destination},
            crash_contract="write-ahead-inverse-durable", provenance=provenance,
            gate=None if telegram and direction != "none" else GATE,
        )
    )
    if telegram and direction != "none":
        post = ",".join(integration.receiver_handoff_actions(binding, direction, "post"))
        ops.append(
            _op(
                f"receiver-post-{account}", "harness.receiver-handoff", f"{account}:receiver", account,
                {"holder": "none"}, {"holder": target_harness, "direction": direction, "phase": "post", "actions": post},
                crash_contract="write-ahead-inverse-durable", provenance=provenance,
            )
        )
    ops.append(
        _op(
            f"commit-{account}", "harness.transition-commit", f"{account}:transition", account,
            dict(record, phase="preparing"), dict(record, phase="awaiting-successor-read"),
            crash_contract="stageable", provenance=provenance,
        )
    )
    return ops


# --- install leg (T-5091 U1) ---------------------------------------------------

#: Installer op kind -> (engine kind, engine privilege, crash_contract). Literal
#: data; see "INSTALL LEG" in the module docstring.
_INSTALL_KIND_MAP = {
    # read-only checks (registry class diagnostics.read)
    "brief-check": ("brief-check", "none", None),
    "claude-mcp-server-check": ("claude-mcp-server-check", "none", None),
    "codex-standing-instructions-override-check": ("codex-standing-instructions-override-check", "none", None),
    "codex-standing-instructions-source-check": ("codex-standing-instructions-source-check", "none", None),
    "codex-standing-instructions-target-check": ("codex-standing-instructions-target-check", "none", None),
    "codex-telegram-disabled-first": ("codex-telegram-disabled-first", "none", None),
    "harness-check": ("harness-check", "none", None),
    "identity-check": ("identity-check", "none", None),
    "manager-check": ("manager-check", "none", None),
    "policy-check": ("policy-check", "none", None),
    "source-check": ("source-check", "none", None),
    "timer-enablement-absence-check": ("timer-enablement-absence-check", "none", None),
    # mutating kinds the installer plans with an inverse: preimage first
    "atomic-write": ("atomic-write", "seat", "write-ahead-inverse-durable"),
    "codex-standing-instructions-install": ("codex-standing-instructions-install", "seat", "write-ahead-inverse-durable"),
    "mkdir": ("mkdir", "seat", "write-ahead-inverse-durable"),
    "seat-periodic-unit-install": ("seat-periodic-unit-install", "seat", "write-ahead-inverse-durable"),
    "user-unit-install": ("user-unit-install", "seat", "write-ahead-inverse-durable"),
    # moves (quarantine / decommission): the moved-from state is the preimage
    "seat-runtime-residue-quarantine": ("seat-runtime-residue-quarantine", "seat", "write-ahead-inverse-durable"),
    "seat-service-decommission": ("seat-service-decommission", "seat", "write-ahead-inverse-durable"),
    # activation / probe kinds: staged behind the installed (disabled) units, verified, then activated
    "claude-mcp-server-registration": ("claude-mcp-server-registration", "seat", "stageable"),
    "codex-telegram-mcp-check": ("codex-telegram-mcp-check", "seat", "stageable"),
    "codex-telegram-source-check": ("codex-telegram-source-check", "seat", "stageable"),
    "daemon-reload": ("daemon-reload", "seat", "stageable"),
    "direct-fire": ("direct-fire", "seat", "stageable"),
    "mailbox-probe": ("mailbox-probe", "seat", "stageable"),
    "mailbox-writable-root-check": ("mailbox-writable-root-check", "seat", "stageable"),
    "seat-claude-settings-env": ("seat-claude-settings-env", "seat", "stageable"),
    "seat-codex-hook-config": ("seat-codex-hook-config", "seat", "stageable"),
    "seat-claude-workspace-trust": ("seat-claude-workspace-trust", "seat", "stageable"),
    "seat-periodic-daemon-reload": ("seat-periodic-daemon-reload", "seat", "stageable"),
    "seat-periodic-timer-enable": ("seat-periodic-timer-enable", "seat", "stageable"),
    "seat-periodic-timer-observe": ("seat-periodic-timer-observe", "seat", "stageable"),
    "seat-service-enable-observe": ("seat-service-enable-observe", "seat", "stageable"),
    "stale-unit-failure-reset": ("stale-unit-failure-reset", "seat", "stageable"),
    "timer-enable": ("timer-enable", "seat", "stageable"),
    "timer-observe": ("timer-observe", "seat", "stageable"),
    "unit-fire": ("unit-fire", "seat", "stageable"),
}

_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_OP_ID_BAD = re.compile(r"[^A-Za-z0-9_-]")
_SEAT_SCOPE_PREFIX = "seat-"


def _engine_op_id(installer_id):
    """Installer ids use dots; the engine (WAL) alphabet is [A-Za-z0-9_-]{1,64}."""

    cleaned = _OP_ID_BAD.sub("_", installer_id)
    if len(cleaned) <= 64:
        return cleaned
    return cleaned[:47] + "-" + hashlib.sha256(installer_id.encode("utf-8")).hexdigest()[:16]


def _persona_for_account(manifest, account):
    for role in manifest.roles.values():
        persona = role.get("persona") if isinstance(role, dict) else getattr(role, "persona", None)
        if persona and manifest.binding(persona).account == account:
            return persona
    raise PlanRefused(f"no persona in {manifest.path} is bound to account {account!r}")


def _install_ops(plan, revision):
    # Provenance never overwrites an installer key (its own ``source_revision`` is the
    # manifest's declared framework revision). Plan-level facts ride on the first op.
    plan_facts = {
        "adapter_of": "tools.persona_installer.PersonaInstaller.plan",
        "installer_plan_digest": plan.digest,
        "installer_operator_actions": str(len(plan.operator_actions)),
    }
    ops, seen = [], {}
    for index, item in enumerate(plan.operations):
        mapped = _INSTALL_KIND_MAP.get(item.kind)
        if mapped is None:
            raise PlanRefused(f"installer op kind {item.kind!r} has no engine mapping")
        kind, privilege, crash_contract = mapped
        op_id = _engine_op_id(item.operation_id)
        if op_id in seen:
            raise PlanRefused(f"installer ops {seen[op_id]!r} and {item.operation_id!r} collide as engine id {op_id!r}")
        seen[op_id] = item.operation_id
        added = {
            "installer_operation_id": item.operation_id,
            "installer_privilege": item.privilege,
            "plan_source_revision": revision,
        } | (plan_facts if index == 0 else {})
        clash = sorted(set(added) & set(item.provenance))
        if clash:
            raise PlanRefused(f"installer op {item.operation_id!r} already carries provenance {clash}")
        provenance = dict(item.provenance) | added
        ops.append(
            _op(
                op_id, kind, item.target, item.owner, dict(item.precondition), dict(item.desired),
                crash_contract=crash_contract, provenance=provenance, layer=item.layer,
                inverse=None if item.inverse is None else dict(item.inverse), privilege=privilege,
                side_effect_free=privilege == "none", execution_scope=item.execution_scope,
            )
        )
    return ops


def plan_install(request):
    scope = request.get("scope") or ""
    if not scope.startswith(_SEAT_SCOPE_PREFIX):
        raise PlanRefused("install runs one sub-run per seat as that seat's account: it requires a seat scope")
    account = scope[len(_SEAT_SCOPE_PREFIX):]
    desired = request.get("desired") or {}
    revision = desired.get("source_revision")
    if not isinstance(revision, str) or not _FULL_COMMIT.fullmatch(revision):
        raise PlanRefused("install needs desired.source_revision, a full 40-hex commit id (the planner never asks git)")
    # The installer's own checkout and manifest (the same pair its CLI defaults to),
    # whichever checkout this planner file was loaded from.
    from tools.persona_installer import REPO_ROOT, Manifest, PersonaInstaller

    manifest = Manifest.load(REPO_ROOT / "instance.yaml")
    persona = _persona_for_account(manifest, account)
    plan = PersonaInstaller(repo_root=REPO_ROOT).plan(
        manifest, persona, harness_override=desired.get("harness"), source_revision=revision
    )
    return _install_ops(plan, revision)


# --- configure sub-op (a): the supervisor cutover (T-5830, T-5091 U4) ------------

#: The simulated observation handler for sub-op (a) (harness-manager_handlers.py).
SUPERVISOR_HANDLER = "harness-manager-supervisor"
SUPERVISOR_GATE = "supervisor-cutover"
_SUPERVISOR_SHAPES = ("consume", "bespoke")
_SUPERVISOR_KINDS = frozenset({
    "harness.supervisor-file", "harness.supervisor-unit", "harness.companion-unit",
    "harness.control-plane-teardown", "harness.consume-retire", "harness.supervisor-commit",
})
#: desired fields that name a unit / file (target pinning, Recovery D15 (T-5830) (h)).
_PINNED_FIELDS = frozenset({
    "backup_path", "service", "session_unit", "receiver_unit", "scope_unit", "slice_unit",
    "timer", "consume_service", "link_path", "dropin_path",
})


def _supervisor_seat(request):
    observed = (request.get("observed") or {}).get(SUPERVISOR_HANDLER)
    if not isinstance(observed, dict) or not isinstance(observed.get("seat"), dict):
        raise PlanRefused(f"request carries no {SUPERVISOR_HANDLER} seat observation for scope {request.get('scope')!r}")
    return observed["seat"]


def _companions(encoded):
    """agent_role_map's ``unit=session`` tokens (space-joined), as (unit, session) pairs."""

    pairs = []
    for token in (encoded or "").split():
        unit, sep, session = token.partition("=")
        if not (sep and unit and session):
            raise PlanRefused(f"malformed companion token {token!r}")
        pairs.append((unit, session))
    return pairs


def _file_entry(seat, path, what):
    entry = (seat.get("files") or {}).get(path)
    if entry is None:
        raise PlanRefused(f"{what} {path} is missing")
    if not isinstance(entry, dict) or entry.get("kind") == "symlink":
        # Recovery D15 (T-5830) (k): never follow a symlink (the scripts' cp -p does)
        raise PlanRefused(f"{what} {path} is a symlink or not a regular file; refusing to follow it")
    if entry.get("kind") != "file" or not isinstance(entry.get("sha256"), str):
        raise PlanRefused(f"{what} {path} is not a regular file")
    return entry


def _common_supervisor_refusals(seat, persona):
    if seat.get("seat_state") == "deep-sleep":  # cutover.sh:139-141 / rollback.sh:118-120
        raise PlanRefused(f"{persona} is in deep-sleep; wake it first")
    if seat.get("seat_state") != "awake":
        raise PlanRefused(f"{persona}: seat_state launch-check is not proven awake")
    if seat.get("user_bus") is not True:  # cutover.sh:133-134 / rollback.sh:111-112
        raise PlanRefused(f"{persona}: the seat's user bus is not proven reachable")


def _sv_op(op_id, kind, target, account, identity, precondition, desired, *, crash, provenance, inverse, gate=None):
    """One sub-op (a) op. Every op is pinned to the seat's OWN user manager (Recovery D15
    (T-5830) (i)) and carries its postconditions as data (desired["postconditions"])."""

    pinned = {
        "systemd_manager": S.SYSTEMD_MANAGER,
        "account": account,
        "xdg_runtime_dir": str(identity.xdg_runtime_dir),
    }
    return _op(
        op_id, kind, target, account, {"user_bus": "reachable"} | precondition, pinned | desired,
        crash_contract=crash, provenance=provenance, gate=gate, inverse=inverse,
    )


def _files(identity):
    """persona_cutover.sh:171-175 / persona_rollback.sh:122-126, in that order."""

    return (str(identity.service_unit_path), str(identity.supervise_wrapper_path), str(identity.settings_json_path))


def _pins(identity, companions, stamp, dropin_dir, dropin_file, link_path):
    """Recovery D15 (T-5830) (h): every target an op may name, derived ONLY from the resolver
    over the committed instance.yaml (plus the scripts' fixed unit literals)."""

    files = _files(identity)
    return {
        "units": sorted(
            {identity.service, identity.consume_timer, identity.consume_service, S.CODEX_RECEIVER_UNIT,
             S.RUNTIME_SCOPE_UNIT, S.RUNTIME_CONSUME_SLICE, *(u for u, _ in companions)}
        ),
        "sessions": sorted({identity.tmux_session, *(s for _, s in companions)}),
        "files": sorted({*files, *(S.backup_path(f, stamp) for f in files), dropin_dir, dropin_file, link_path}),
    }


def _assert_pinned(ops, pins):
    """Planner-side half of (h): a target outside the recorded pins refuses the plan."""

    named = set(pins["units"]) | set(pins["files"]) | {S.exact_target(s) for s in pins["sessions"]}
    for op in ops:
        if op["kind"] not in _SUPERVISOR_KINDS:
            continue
        refs = [] if op["kind"] in ("harness.supervisor-commit", "harness.control-plane-teardown") else [op["target"]]
        refs += [v for k, v in op["desired"].items() if k in _PINNED_FIELDS]
        refs += [t for t in op["desired"].get("tmux_targets", "").split(",") if t]
        foreign = [r for r in refs if r not in named]
        if foreign:
            raise PlanRefused(f"op {op['operation_id']} names {foreign}, outside the resolver-derived pins")


def _resolve(persona, unit_text, *, bespoke_only):
    from tools import persona_cutover_resolve as R
    from tools.persona_installer import REPO_ROOT

    try:
        if bespoke_only:
            return R.resolve_bespoke_identity(persona, unit_text=unit_text)
        return R.resolve_persona(persona, manifest_path=REPO_ROOT / "instance.yaml", unit_text=unit_text)
    except (ValueError, KeyError, R.ManifestPreconditionError) as exc:
        raise PlanRefused(f"{persona}: persona resolution failed -- {exc}") from None


def plan_supervisor(request):
    """Configure sub-op (a): ``desired.supervisor`` consume (the cutover) or bespoke (the
    restore). UNTESTED-LIVE; see "SUPERVISOR CUTOVER" in the module docstring.

    instance.yaml is read from the PLANNER's tree (``persona_installer.REPO_ROOT``), the
    committed manifest at the plan revision. What binds it to the seat is the Q2 check:
    the observed target clone's instance.yaml must be byte-identical (sha256), so every
    resolver-derived pin is the target's own (Recovery D15 (T-5830) (h))."""

    desired = request.get("desired") or {}
    if desired.get("harness"):
        raise PlanRefused("configure sub-ops (a) and (b) are separate runs, each with its own gate (ORG-080 D-L)")
    shape = desired.get("supervisor")
    if shape not in _SUPERVISOR_SHAPES:
        raise PlanRefused(f"desired.supervisor must be one of {list(_SUPERVISOR_SHAPES)}")
    scope = request.get("scope") or ""
    if not scope.startswith(_SEAT_SCOPE_PREFIX):
        raise PlanRefused("configure (a) runs as the seat's own account: it requires a seat scope")
    account = scope[len(_SEAT_SCOPE_PREFIX):]
    stamp = desired.get("stamp")
    if not isinstance(stamp, str) or not S.STAMP_RE.fullmatch(stamp):
        raise PlanRefused("configure (a) needs desired.stamp matching [A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
    seat = _supervisor_seat(request)
    from tools.persona_installer import REPO_ROOT, Manifest

    manifest_sha = hashlib.sha256((REPO_ROOT / "instance.yaml").read_bytes()).hexdigest()
    manifest = Manifest.load(REPO_ROOT / "instance.yaml")
    persona = _persona_for_account(manifest, account)
    unit_text = seat.get("bespoke_unit_text")
    if not isinstance(unit_text, str):
        raise PlanRefused(f"{persona}: no observed bespoke unit text")
    identity = _resolve(persona, unit_text, bespoke_only=shape == "bespoke")
    if identity.account != account:
        raise PlanRefused(f"{persona} resolves to account {identity.account!r}, not the scope's {account!r}")
    _common_supervisor_refusals(seat, persona)
    companions = _companions(identity.companion_units)
    home = str(identity.home)
    dropin_dir = f"{home}/.config/systemd/user/{identity.consume_service}.d"
    dropin_file = f"{dropin_dir}/urgent.conf"
    link_path = f"{home}/.config/systemd/user/timers.target.wants/{identity.consume_timer}"
    pins = _pins(identity, companions, stamp, dropin_dir, dropin_file, link_path)
    base = {
        "resolver": "tools.persona_cutover_resolve." + ("resolve_bespoke_identity" if shape == "bespoke" else "resolve_persona"),
        "pin_basis": f"resolver over instance.yaml sha256 {manifest_sha}",
        "instance_yaml_sha256": manifest_sha,
        "untested_live": "true",
    }
    paths = (dropin_dir, dropin_file, link_path)
    if shape == "consume":
        ops = _plan_cutover(desired, manifest, persona, manifest_sha, seat, account, identity, companions, stamp, pins, base, paths)
    else:
        ops = _plan_restore(desired, seat, account, identity, companions, stamp, pins, base, paths)
    _assert_pinned(ops, pins)
    ids = [op["operation_id"] for op in ops]
    if len(set(ids)) != len(ids):
        raise PlanRefused("sub-op (a) op ids collide with the installer's activation ops")
    return ops


def _companion_op(op_id, account, identity, unit, session, action, provenance, stamp=None):
    """Stop / start ONE declared companion unit (cutover.sh:262-288, rollback.sh:382-386)."""

    target = S.exact_target(session)
    running = str(action == "start").lower()
    stopped = str(action != "start").lower()
    checks = [S.check("unit", unit=unit, active=running), S.check("session", target=target, present=running)]
    desired = {"action": action, "session_unit": unit, "tmux_targets": target, "active": running,
               "session_present": running, "postconditions": S.encode(checks)}
    if action == "stop" and stamp is not None:
        # T-6047 (T-5956 parity): the stop records the unit's pre-stop ActiveState in the
        # companion pre-state cell, keyed (persona, stamp) as the script's .companions file is
        desired["companion_snapshot"] = S.companion_snapshot_name(identity.persona, stamp)
    return _sv_op(
        op_id, "harness.companion-unit", unit, account, identity, {"active": stopped},
        desired,
        crash="write-ahead-inverse-durable", provenance=provenance, inverse={"active": stopped},
    )


def _plan_cutover(desired, manifest, persona, manifest_sha, seat, account, identity, companions, stamp, pins, base, paths):
    dropin_dir, dropin_file, _link = paths
    revision = desired.get("source_revision")
    if not isinstance(revision, str) or not _FULL_COMMIT.fullmatch(revision):
        raise PlanRefused("the cutover needs desired.source_revision, a full 40-hex commit id")
    urgent = desired.get("urgent_env", "0")
    if urgent not in ("0", "1"):
        raise PlanRefused("desired.urgent_env must be '0' or '1'")
    if seat.get("target_manifest_sha256") != manifest_sha:  # mason ruling Q2
        raise PlanRefused(
            f"{persona}: the target clone's instance.yaml (sha256 {seat.get('target_manifest_sha256')!r}) is not the "
            f"plan's ({manifest_sha}); the consume unit would read stale config"
        )
    install = seat.get("install_precondition") or {}
    if install.get("ok") is not True:  # cutover.sh:151-155
        raise PlanRefused(f"{persona}: install-done precondition failed (not control-plane-ready): {install.get('detail')!r}")
    if seat.get("supervisor") != "bespoke":
        raise PlanRefused(f"{persona}: the recorded supervisor is {seat.get('supervisor')!r}, not bespoke")
    service = (seat.get("units") or {}).get(identity.service) or {}
    if service.get("enabled") is not True or service.get("active") is not True:
        raise PlanRefused(f"{persona}: bespoke {identity.service} is not enabled+active")
    from tools.persona_installer import ACTIVATION_OPERATION_KINDS, REPO_ROOT, PersonaInstaller

    # cutover.sh:89-100: --harness claude is hardcoded (a bespoke supervisor is claude-only)
    plan = PersonaInstaller(repo_root=REPO_ROOT).plan(manifest, persona, harness_override="claude", source_revision=revision)
    staged = seat.get("staged") or {}
    if staged.get("stamp") != stamp or staged.get("plan_digest") != plan.digest or staged.get("receipt_valid") is not True:
        raise PlanRefused(f"{persona}: no valid staged checkpoint for stamp {stamp!r} at installer plan digest {plan.digest}")
    index = next((i for i, op in enumerate(plan.operations) if op.kind in ACTIVATION_OPERATION_KINDS), len(plan.operations))
    suffix = SimpleNamespace(digest=plan.digest, operator_actions=plan.operator_actions, operations=plan.operations[index:])

    prov = base | {"adapter_of": "tools/persona_cutover.sh (configure sub-op a, supervisor consume)", "plan_source_revision": revision}
    session = S.exact_target(identity.tmux_session)
    ops = []
    for i, path in enumerate(_files(identity)):  # cutover.sh:223-226
        entry = _file_entry(seat, path, "bespoke file")
        backup = S.backup_path(path, stamp)
        if (seat.get("files") or {}).get(backup) is not None:
            raise PlanRefused(f"backup {backup} already exists; choose a fresh stamp")
        ops.append(_sv_op(
            f"sv-snapshot-{i}", "harness.supervisor-file", path, account, identity,
            {"path_sha256": entry["sha256"], "backup_sha256": "absent"},
            {"action": "snapshot", "backup_path": backup, "sha256": entry["sha256"], "write": "atomic-no-follow",
             "postconditions": S.encode([S.check("file", path=backup, sha256=entry["sha256"])])},
            crash="write-ahead-inverse-durable", provenance=prov, inverse={"backup_sha256": "absent"},
        ))
    if urgent == "1":  # cutover.sh:228-232
        content = S.URGENT_DROPIN_CONTENT
        ops.append(_op(
            "sv-urgent-dir", "mkdir", dropin_dir, account, {"state": "absent-or-directory"}, {"mode": "0755"},
            crash_contract="write-ahead-inverse-durable", provenance=prov, inverse={"state": "remove-if-created-and-empty"},
        ))
        ops.append(_op(
            "sv-urgent-dropin", "atomic-write", dropin_file, account, {"state": "absent-matching-or-managed"},
            {"content": content, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "mode": "0644"},
            crash_contract="write-ahead-inverse-durable", provenance=prov, inverse={"state": "receipt-snapshot"},
        ))
    ops.append(_op(  # cutover.sh:234
        "sv-daemon-reload", "daemon-reload", account, account, {}, {"reason": "supervisor-cutover", "manager": S.SYSTEMD_MANAGER},
        crash_contract="stageable", provenance=prov, inverse=None,
    ))
    retire_checks = [
        S.check("unit", unit=identity.service, enabled="false", active="false"),
        S.check("session", target=session, present="false"),
    ]
    ops.append(_sv_op(  # cutover.sh:239 + :247-250 (exact target, Recovery D15 (j))
        "sv-retire", "harness.supervisor-unit", identity.service, account, identity,
        {"enabled": "true", "active": "true"},
        {"action": "retire", "service": identity.service, "enabled": "false", "active": "false", "tmux_targets": session,
         # mason ruling (F1) + Recovery D15 (m): the undo is COMPOSITE, mirroring the restore
         # path: tear down THIS seat's control plane (these pinned targets, exact session),
         # prove it, and only then reinstate bespoke. A stop after a partial teardown is an
         # EscalatingRollbackStop to escalation_recipient (Recovery D15 (n)).
         "receiver_unit": S.CODEX_RECEIVER_UNIT, "scope_unit": S.RUNTIME_SCOPE_UNIT, "slice_unit": S.RUNTIME_CONSUME_SLICE,
         "undo": "teardown-then-reinstate", "escalation_recipient": S.ESCALATION_RECIPIENT,
         "postconditions": S.encode(retire_checks)},
        crash="write-ahead-inverse-durable", provenance=prov, gate=SUPERVISOR_GATE,
        inverse={"enabled": "true", "active": "true", "undo": "control-plane-teardown-then-reinstate"},
    ))
    for i, (unit, comp_session) in enumerate(companions):  # cutover.sh:262-271
        ops.append(_companion_op(f"sv-companion-stop-{i}", account, identity, unit, comp_session, "stop", prov, stamp))
    quiesced = [session, *(S.exact_target(s) for _, s in companions)]
    ops.append(_sv_op(  # Recovery D15 (T-5830) (m): session-gone-before-apply, as an explicit precondition
        "sv-require-quiesced", "harness.supervisor-unit", identity.service, account, identity,
        {"sessions_absent": ",".join(quiesced), "units_inactive": ",".join([identity.service, *(u for u, _ in companions)])},
        {"action": "require-quiesced", "service": identity.service, "tmux_targets": ",".join(quiesced),
         "postconditions": S.encode([])},
        crash="stageable", provenance=prov, inverse={},
    ))
    ops.extend(_install_ops(suffix, revision))  # cutover.sh:273-278
    for i, (unit, comp_session) in enumerate(companions):  # cutover.sh:284-288
        ops.append(_companion_op(f"sv-companion-start-{i}", account, identity, unit, comp_session, "start", prov))
    checks = [S.check("control-plane-session", target=session)]
    for unit, comp_session in companions:  # cutover.sh:313-329 (bounded 10x1s)
        checks.append(S.check("unit", unit=unit, active="true", window_s="10", interval_s="1"))
        checks.append(S.check("session", target=S.exact_target(comp_session), present="true", window_s="10", interval_s="1"))
    checks += [
        S.check("scope-owns-tmux-server"),
        S.check("runtime-harness-declared"),
        S.check("codex-profile-viability"),
        S.check("claude-launch-argv", channels=identity.channels, model=identity.model),
    ]
    if urgent == "1":
        checks.append(S.check("urgent-env", assignment=f"{S.URGENT_ENV}=1"))
    checks += [
        S.check("composer-ready"),
        S.check("telegram-poller", window_s="30", interval_s="3"),
        S.check("single-receiver"),  # Recovery D15 (T-5830) (l): no second poller
    ]
    ops.append(_sv_op(
        "sv-commit", "harness.supervisor-commit", f"{account}:supervisor", account, identity,
        {"supervisor": "bespoke"}, {"supervisor": "consume", "postconditions": S.encode(checks)},
        crash="stageable", provenance=prov | {"pinned_targets": json.dumps(pins, sort_keys=True)},
        inverse={"supervisor": "bespoke"},
    ))
    return ops


def _plan_restore(desired, seat, account, identity, companions, stamp, pins, base, paths):
    _dir, dropin_file, link_path = paths
    break_glass = desired.get("break_glass", "0")
    if break_glass not in ("0", "1"):
        raise PlanRefused("desired.break_glass must be '0' or '1'")
    if break_glass == "1":
        pin_mode, reason = "break-glass", "desired.break_glass=1 (explicit)"
    elif seat.get("manifest_loads") is True:
        pin_mode, reason = "manifest-pinned", "the seat's manifest loads"
    else:  # rollback.sh:343-345, made loud: the mode and its reason are in the plan digest
        pin_mode, reason = "break-glass", "the seat's manifest is missing or does not load"
    recorded = seat.get("supervisor")
    if recorded not in _SUPERVISOR_SHAPES:
        raise PlanRefused(f"{identity.persona}: no recorded supervisor shape")
    prov = base | {"adapter_of": "tools/persona_rollback.sh (configure sub-op a, supervisor bespoke)", "staged_rollback_pin_reason": reason}
    # rollback.sh:129-132: every backup must exist before anything is touched (never partial)
    backups = [(path, _file_entry(seat, S.backup_path(path, stamp), "backup")) for path in _files(identity)]
    session = S.exact_target(identity.tmux_session)
    ops = []
    for i, (path, backup) in enumerate(backups):  # rollback.sh:278-281
        current = _file_entry(seat, path, "live file") if (seat.get("files") or {}).get(path) is not None else None
        before = current["sha256"] if current else "absent"
        ops.append(_sv_op(
            f"sv-restore-{i}", "harness.supervisor-file", path, account, identity,
            {"path_sha256": before, "backup_sha256": backup["sha256"]},
            {"action": "restore", "backup_path": S.backup_path(path, stamp), "sha256": backup["sha256"],
             "write": "atomic-no-follow", "postconditions": S.encode([S.check("file", path=path, sha256=backup["sha256"])])},
            crash="write-ahead-inverse-durable", provenance=prov, inverse={"path_sha256": before},
        ))
    # rollback.sh:283. Engine receipts are per run, so reusing an op id across runs is safe
    # for the engine; this one is distinct only because the SIMULATED HarnessInstallSim keys
    # its slots by op id across runs and would read the cutover's slot as foreign.
    ops.append(_op(
        "sv-restore-daemon-reload", "daemon-reload", account, account, {}, {"reason": "supervisor-restore", "manager": S.SYSTEMD_MANAGER},
        crash_contract="stageable", provenance=prov, inverse=None,
    ))
    teardown_checks = [
        S.check("session", target=session, present="false"),
        S.check("unit", unit=S.CODEX_RECEIVER_UNIT, active="false"),
        # Recovery D15 (m), DECISION (P6): the runtime scope is HARD here, deliberately stricter
        # than persona_rollback.sh:293-299 (soft `|| true` there)
        S.check("unit", unit=S.RUNTIME_SCOPE_UNIT, active="false"),
    ]
    ops.append(_sv_op(  # rollback.sh:300-328; only the consume slice stop stays soft (recorded, not asserted)
        "sv-teardown", "harness.control-plane-teardown", f"{account}:control-plane", account, identity, {},
        {"receiver_unit": S.CODEX_RECEIVER_UNIT, "scope_unit": S.RUNTIME_SCOPE_UNIT, "slice_unit": S.RUNTIME_CONSUME_SLICE,
         "soft": "slice_unit", "tmux_targets": session, "postconditions": S.encode(teardown_checks)},
        crash="write-ahead-inverse-durable", provenance=prov, inverse={},
    ))
    consume_checks = [
        S.check("file", path=link_path, sha256="absent"),
        S.check("unit-not-found-dead", unit=identity.consume_timer),
        S.check("unit-not-found-dead", unit=identity.consume_service),
        S.check("file", path=dropin_file, sha256="absent"),
    ]
    ops.append(_sv_op(  # rollback.sh:330-356, plus :371-372 (drop-in removal moved here)
        "sv-consume-retire", "harness.consume-retire", identity.consume_timer, account, identity, {},
        {"timer": identity.consume_timer, "consume_service": identity.consume_service, "link_path": link_path,
         "dropin_path": dropin_file, "staged_rollback": pin_mode, "stamp": stamp, "postconditions": S.encode(consume_checks)},
        crash="write-ahead-inverse-durable", provenance=prov, inverse={},
    ))
    reinstate_checks = [
        S.check("unit", unit=identity.service, enabled="true", active="true"),
        S.check("session", target=session, present="true"),
    ]
    ops.append(_sv_op(  # rollback.sh:361-365; Recovery D15 (T-5830) (m): scope-torn-down-before-bespoke-restart
        "sv-reinstate", "harness.supervisor-unit", identity.service, account, identity,
        {"sessions_absent": session, "units_inactive": f"{S.RUNTIME_SCOPE_UNIT},{S.CODEX_RECEIVER_UNIT}",
         "units_not_found": f"{identity.consume_timer},{identity.consume_service}", "files_absent": link_path},
        {"action": "reinstate", "service": identity.service, "enabled": "true", "active": "true", "tmux_targets": session,
         "postconditions": S.encode(reinstate_checks)},
        crash="write-ahead-inverse-durable", provenance=prov, gate=SUPERVISOR_GATE, inverse={},
    ))
    # T-6047 (T-5956 parity): restore only companions the cutover did not record as not-active;
    # no record (a pre-T-6047 / pre-T-5956 cutover) restores every declared companion
    prestate = (seat.get("companion_prestate") or {}).get(S.companion_snapshot_name(identity.persona, stamp))
    restored = [(i, u, s) for i, (u, s) in enumerate(companions) if S.companion_should_restore(prestate, u)]
    for i, unit, comp_session in restored:  # rollback.sh:382-386
        ops.append(_companion_op(f"sv-companion-start-{i}", account, identity, unit, comp_session, "start", prov))
    checks = [
        S.check("unit", unit=identity.service, active="true"),
        S.check("unit", unit=identity.service, enabled="true"),
        S.check("session", target=session, present="true"),
        S.check("scope-clean", scope_unit=S.RUNTIME_SCOPE_UNIT),
        S.check("process-present", which="claude"),
        S.check("process-present", advisory=True, which="bun"),
        *(S.check("unit", unit=unit, active="true") for _i, unit, _s in restored),
        S.check("single-receiver"),  # Recovery D15 (T-5830) (l): no second poller
    ]
    ops.append(_sv_op(
        "sv-commit", "harness.supervisor-commit", f"{account}:supervisor", account, identity,
        {"supervisor": recorded}, {"supervisor": "bespoke", "postconditions": S.encode(checks)},
        crash="stageable", provenance=prov | {"pinned_targets": json.dumps(pins, sort_keys=True)},
        inverse={"supervisor": recorded},
    ))
    return ops


# --- configure run_state: fleet bringdown / restore (T-5858, T-5091 U5) ------------

#: The simulated observation handler for run_state (harness-manager_handlers.py).
RUN_STATE_HANDLER = "harness-manager-run-state"
RUN_STATE_GATE = "seat-run-state"
_RUN_STATE_KINDS = frozenset({
    "harness.run-state-manifest", "harness.run-state-seat-state", "harness.run-state-unit",
    "harness.run-state-drain", "harness.run-state-reset-failed", "harness.run-state-commit",
})
#: Ops whose target is an ``<account>:<slot>`` record, not a pinned unit / file.
_RUN_STATE_RECORD_KINDS = frozenset({"harness.run-state-drain", "harness.run-state-reset-failed", "harness.run-state-commit"})
#: The HO seat's instance.yaml role key (Recovery D15 (T-5858) (o): a pause needs an operator anchor).
_HO_ROLE = "head-engineering-orchestrator"
#: Recovery D15 (T-5858) (r): the drain's bounded wait (data on the op; a live handler polls).
_DRAIN_WINDOW_S, _DRAIN_INTERVAL_S = "120", "5"


def _is_recovery_role(role_key):
    """Recovery D15 (T-5858) (o): the recovery tier, by instance.yaml role key (recovery: the
    head-system-orchestrator; watchman: system-orchestrator-1)."""

    return role_key == "head-system-orchestrator" or role_key.startswith("system-orchestrator-")


def _role_key(manifest, persona):
    for key, role in manifest.roles.items():
        if isinstance(role, dict) and role.get("persona") == persona:
            return key, role
    raise PlanRefused(f"{persona}: no instance.yaml role binds this persona")


def _rs_paths(account, persona):
    """The pause manifest (the seat's OWN state dir, D-6) and the ORG-106 seat-state record
    (harness_state.seat_state_path under the seat's default state root)."""

    from pathlib import Path

    state_root = f"/home/{account}/.local/state/pantheon"
    seat_state = str(harness_state.seat_state_path(persona, root=Path(state_root)))
    return f"{state_root}/harness-manager/run-state-manifest.json", seat_state


def _rs_targets(manifest, persona, revision, role):
    """The per-seat §2 target set, from DATA only (DECISION D-3): the installer's own plan
    (wake timers), instance.yaml companion_units and codex_telegram_bridge receivers,
    schedules/<persona>.yaml dispatch loops, and instance.yaml run_state_units (T-5862). Returns (targets {unit: source},
    oneshots {timer: service}, residue [str])."""

    from tools import scheduler_spec
    from tools.persona_installer import REPO_ROOT, ManifestError, PersonaInstaller

    targets, oneshots, residue = {}, {}, []
    try:
        plan = PersonaInstaller(repo_root=REPO_ROOT).plan(manifest, persona, source_revision=revision)
    except ManifestError as exc:
        raise PlanRefused(f"{persona}: installer plan failed -- {exc}") from None
    for item in plan.operations:
        if item.kind not in ("timer-enable", "seat-periodic-timer-enable"):
            continue
        unit = item.target
        stem = unit.rpartition(".")[0].split("@", 1)[0]
        if unit == RS.RESTART_DRAIN_TIMER:
            residue.append(f"seat-local {unit} stays armed (resurrection path, excluded: mason ruling F3)")
        if stem in RS.INSTALLER_WAKE_STEMS:
            targets[unit] = "installer plan"
            oneshots[unit] = item.desired.get("service") or unit[: -len(".timer")] + ".service"
    for companion in role.get("companion_units") or ():
        unit = companion.get("unit") if isinstance(companion, dict) else None
        if not isinstance(unit, str):
            raise PlanRefused(f"{persona}: malformed companion_units entry {companion!r}")
        targets[unit] = "instance.yaml companion_units"
    bridge = ((manifest.installer.get("codex_telegram_bridge") or {}).get("personas") or {}).get(persona) or {}
    if isinstance(bridge.get("codex_receiver_unit"), str):
        targets[bridge["codex_receiver_unit"]] = "instance.yaml codex_receiver_unit"
    if isinstance(bridge.get("claude_receiver_unit"), str):
        if bridge.get("claude_receiver_topology") == "systemd-user":
            targets[bridge["claude_receiver_unit"]] = "instance.yaml claude_receiver_unit (systemd-user)"
        else:  # mason ruling F2: in-pane, so no unit op can pause it (SOP 3.6)
            residue.append("the claude Telegram receiver is runtime-owned (in the live pane): "
                           "an inbound Telegram message still wakes the session; NOT paused")
    spec = scheduler_spec.load_spec(REPO_ROOT / "schedules", REPO_ROOT / "tools" / "systemd")
    for effective in scheduler_spec.effective_schedule(spec, persona):
        if effective.entry.name in RS.DISPATCH_NAMES:
            timer = f"{effective.entry.name}.timer"
            targets[timer] = f"schedules/{persona}.yaml"
            oneshots[timer] = f"{effective.entry.name}.service"
    # T-5862: the remaining §2 units, declared per role (one source per unit, never two)
    try:
        declared = RS.parse_run_state_units(role.get("run_state_units"), persona=persona)
    except ValueError as exc:
        raise PlanRefused(f"instance.yaml {exc}") from None
    for unit, oneshot in declared:
        if unit in targets:
            raise PlanRefused(f"{persona}: run_state_units {unit} is already declared by {targets[unit]} (one source per unit)")
        targets[unit] = "instance.yaml run_state_units"
        if oneshot is not None:
            oneshots[unit] = oneshot
    for unit in targets:
        if not RS.UNIT_RE.fullmatch(unit) or not RS.is_pause_category(unit):
            raise PlanRefused(f"{persona}: derived target {unit!r} is not a §2 pause-category unit")
        instance = RS.instance_of(unit)
        if instance is not None and instance != persona:
            raise PlanRefused(f"{persona}: derived target {unit} belongs to {instance!r}, not this seat")
    return targets, oneshots, residue


def _recovery_residue(persona):
    """Recovery D15 (T-5858) (s): Recovery-side monitors that can resurrect a paused seat (SOP §6),
    enumerated from tools/systemd/expected-units.yaml. Pure reporting; no cross-account op."""

    import yaml
    from tools.persona_installer import REPO_ROOT

    doc = yaml.safe_load((REPO_ROOT / "tools" / "systemd" / "expected-units.yaml").read_text(encoding="utf-8")) or {}
    names = {f"{stem}@{persona}.timer" for stem in RS.RECOVERY_MONITOR_STEMS}
    found = sorted(e["unit"] for e in doc.get("recovery") or () if isinstance(e, dict) and e.get("unit") in names)
    out = [f"Recovery-side {unit} stays armed (cross-account; hand SOP §6)" for unit in found]
    # expected-units.yaml lists only some seats yet: an undeclared monitor is named, never assumed absent
    out += [f"Recovery-side {unit} is not declared in tools/systemd/expected-units.yaml: check it by hand (SOP §6)"
            for unit in sorted(names - set(found))]
    return out


def _fields(unit):
    if not isinstance(unit, dict):
        return None
    return {"UnitFileState": unit.get("UnitFileState"), "ActiveState": unit.get("ActiveState")}


def _rs_op(op_id, kind, target, account, uid, precondition, desired, *, crash, provenance, inverse, gate=None):
    """One run_state op, pinned to the seat's OWN user manager (Recovery D15 (T-5858) / U4 (i))."""

    pinned = {"systemd_manager": RS.SYSTEMD_MANAGER, "account": account, "xdg_runtime_dir": f"/run/user/{uid}"}
    return _op(
        op_id, kind, target, account, {"user_bus": "reachable"} | precondition, pinned | desired,
        crash_contract=crash, provenance=provenance, gate=gate, inverse=inverse,
    )


def _assert_rs_pinned(ops, pins):
    """Planner-side pin check: every unit / file an op names is a data-derived pin."""

    named = set(pins["units"]) | set(pins["files"])
    for op in ops:
        refs = [] if op["kind"] in _RUN_STATE_RECORD_KINDS else [op["target"]]
        if "unit" in op["desired"]:
            refs.append(op["desired"]["unit"])
        for key in ("units", "oneshots"):
            refs += [u for u in op["desired"].get(key, "").split(",") if u]
        foreign = [r for r in refs if r not in named]
        if foreign:
            raise PlanRefused(f"op {op['operation_id']} names {foreign}, outside the data-derived pins")


def plan_run_state(request):
    """Configure ``desired.run_state``: paused (SOP §3 bringdown) or running (SOP §5 restore).
    UNTESTED-LIVE; see "RUN STATE" in the module docstring."""

    desired = request.get("desired") or {}
    if desired.get("harness") or desired.get("supervisor"):
        raise PlanRefused("configure run_state is its own run with its own gate; it never rides sub-op (a) or (b) (ORG-080 D-L)")
    shape = desired.get("run_state")
    if shape not in RS.RUN_STATES:
        raise PlanRefused(f"desired.run_state must be one of {list(RS.RUN_STATES)}")
    scope = request.get("scope") or ""
    if not scope.startswith(_SEAT_SCOPE_PREFIX):
        raise PlanRefused("run_state runs one sub-run per seat as that seat's own account: it requires a seat scope (Q3)")
    account = scope[len(_SEAT_SCOPE_PREFIX):]
    stamp = desired.get("stamp")
    try:
        ts = RS.stamp_ts(stamp)
    except ValueError:
        raise PlanRefused("run_state needs desired.stamp as YYYYMMDDTHHMMSSZ (it also stamps the records)") from None
    revision = desired.get("source_revision")
    if not isinstance(revision, str) or not _FULL_COMMIT.fullmatch(revision):
        raise PlanRefused("run_state needs desired.source_revision, a full 40-hex commit id (the planner never asks git)")
    anchor = desired.get("anchor")
    if not isinstance(anchor, str) or not RS.ANCHOR_RE.fullmatch(anchor):
        raise PlanRefused("run_state needs desired.anchor as <principal>:<ref> (who directed it)")
    observed = (request.get("observed") or {}).get(RUN_STATE_HANDLER)
    if not isinstance(observed, dict) or not isinstance(observed.get("seat"), dict):
        raise PlanRefused(f"request carries no {RUN_STATE_HANDLER} seat observation for scope {scope!r}")
    seat = observed["seat"]
    from tools.persona_installer import REPO_ROOT, Manifest

    manifest_sha = hashlib.sha256((REPO_ROOT / "instance.yaml").read_bytes()).hexdigest()
    manifest = Manifest.load(REPO_ROOT / "instance.yaml")
    persona = _persona_for_account(manifest, account)
    role_key, role = _role_key(manifest, persona)
    if _is_recovery_role(role_key):  # Recovery D15 (T-5858) (o)
        raise PlanRefused(f"{persona} ({role_key}) is the recovery tier: pausing it removes recovery (Recovery D15 (T-5858) (o))")
    if shape == "paused" and role_key == _HO_ROLE and not anchor.startswith("operator:"):
        raise PlanRefused(f"{persona} is the HO seat: a pause needs an operator:<ref> anchor (Recovery D15 (T-5858) (o))")
    if seat.get("target_manifest_sha256") != manifest_sha:  # mason ruling Q2 (the U4 rule)
        raise PlanRefused(f"{persona}: the seat clone's instance.yaml is not the plan's (sha256 {manifest_sha})")
    if seat.get("user_bus") is not True:
        raise PlanRefused(f"{persona}: the seat's user bus is not proven reachable")
    uid = seat.get("uid")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise PlanRefused(f"{persona}: no observed seat uid")
    units = seat.get("units")
    if not isinstance(units, dict):
        raise PlanRefused(f"{persona}: no unit observation for the seat's user manager")

    targets, oneshots, residue = _rs_targets(manifest, persona, revision, role)
    residue += _recovery_residue(persona)
    manifest_path, seat_state_path = _rs_paths(account, persona)
    pins = {"units": sorted(set(targets) | set(oneshots.values())), "files": sorted({manifest_path, seat_state_path})}
    for name, unit in sorted(units.items()):
        fields = _fields(unit) or {}
        live = fields.get("UnitFileState") == "enabled" or fields.get("ActiveState") in RS.IN_FLIGHT
        if RS.is_pause_category(name) and name not in pins["units"] and live:  # F1 / D-3
            raise PlanRefused(f"{name}: §2 wake path not derivable from instance.yaml; restore could not know it")
        if RS.classify(name) == ("seat apps", RS.JUDGEMENT) and fields.get("ActiveState") == "active":
            residue.append(f"seat app {name} left running (SOP §2 judgement)")
    for unit in targets:
        fields = _fields(units.get(unit))
        if fields is None:
            continue  # not loaded in this seat's manager: nothing to pause or restore
        if fields["UnitFileState"] not in ("enabled", "disabled"):
            raise PlanRefused(
                f"{unit}: UnitFileState {fields['UnitFileState']!r} (masked, static, linked ...) cannot be paused and restored faithfully"
            )
        if fields not in (RS.UP, RS.DOWN):
            raise PlanRefused(f"{unit}: mixed or transitional state {fields}; settle it (reset-failed) before a run_state change")
    keep = [u for u in (desired.get("keep") or "").split(",") if u]
    if len(set(keep)) != len(keep) or not all(RS.UNIT_RE.fullmatch(u) for u in keep):
        raise PlanRefused(f"desired.keep must be distinct exact unit names, got {keep}")
    baseline = ",".join(sorted(n for n, u in units.items() if (_fields(u) or {}).get("ActiveState") == "failed"))
    ctx = SimpleNamespace(
        seat=seat, units=units, account=account, persona=persona, uid=uid, stamp=stamp, ts=ts, anchor=anchor,
        revision=revision, keep=keep, targets=targets, oneshots=oneshots, residue=residue, pins=pins,
        manifest_path=manifest_path, seat_state_path=seat_state_path, baseline=baseline,
        base={
            "adapter_of": "sop/standard/fleet-daemon-bringdown-and-restore.md (configure run_state)",
            "instance_yaml_sha256": manifest_sha,
            "plan_source_revision": revision,
            "untested_live": "true",
        },
    )
    ops = _plan_rs_pause(ctx) if shape == "paused" else _plan_rs_restore(ctx)
    _assert_rs_pinned(ops, pins)
    return ops


def _manifest_ours(ctx, doc):
    """(problems, seat-state record) for an observed manifest, checked against the seat-state
    record and the data-derived target set (Recovery D15 (T-5858) (q): never inferred)."""

    problems = RS.manifest_problems(doc, persona=ctx.persona, account=ctx.account)
    record = ctx.seat.get("seat_state")
    if not problems:
        if not isinstance(record, dict):
            problems.append("no ORG-106 seat-state record carries this pause")
        elif record.get("pause_id") != doc["pause_id"] or record.get("persona") != ctx.persona:
            problems.append(f"seat-state pause_id {record.get('pause_id')!r} != manifest pause_id {doc['pause_id']!r}")
        elif record.get("plan_revision") != doc["plan_revision"]:  # review R1-3: matched, not just present
            problems.append(f"seat-state plan_revision {record.get('plan_revision')!r} != manifest plan_revision {doc['plan_revision']!r}")
        foreign = [e["unit"] for e in doc["entries"] if e["unit"] not in ctx.targets]
        if foreign:
            problems.append(f"manifest names {foreign}, outside the data-derived target set")
    return problems, record


def _plan_rs_pause(ctx):
    keep = ctx.keep
    foreign = [u for u in keep if u not in ctx.targets]
    if foreign:
        raise PlanRefused(f"desired.keep names {foreign}, not in the seat's target set {sorted(ctx.targets)}")
    open_manifest = ctx.seat.get("manifest")
    if open_manifest is not None:  # Recovery D15 (T-5858) (q): never a second pause
        problems, _record = _manifest_ours(ctx, open_manifest)
        settled = (
            not problems and ctx.seat.get("run_state") == "paused" and sorted(open_manifest["keep"]) == sorted(keep)
            and all(_fields(ctx.units.get(e["unit"])) == RS.DOWN for e in open_manifest["entries"])
            and not [u for u in ctx.targets if u not in keep and _fields(ctx.units.get(u)) == RS.UP]
        )
        if settled:
            return []  # a committed pause, exactly as desired: a validated no-op
        pause_id = open_manifest.get("pause_id") if isinstance(open_manifest, dict) else "unreadable"
        raise PlanRefused(
            f"a pause manifest is open (pause_id {pause_id!r}): recover a halted pause with rollback(run_id) "
            "or a restore, never a second pause (Recovery D15 (T-5858) (q))"
        )
    if ctx.seat.get("seat_state") is not None:
        raise PlanRefused(f"{ctx.persona} already carries an ORG-106 seat-state record; wake it first (one pause owner)")
    up = [u for u in sorted(ctx.targets) if u not in keep and _fields(ctx.units.get(u)) == RS.UP]
    if not up:
        raise PlanRefused(f"{ctx.persona}: no running target to pause")
    timers = [u for u in up if u.endswith(".timer")]
    services = [u for u in up if not u.endswith(".timer")]
    pause_id = f"rs-{ctx.account}-{ctx.stamp}"
    doc = {
        "schema": RS.MANIFEST_SCHEMA, "persona": ctx.persona, "account": ctx.account, "pause_id": pause_id,
        "plan_revision": ctx.revision, "stamp": ctx.stamp, "anchor": ctx.anchor, "keep": sorted(keep),
        "entries": [{"unit": u, "phase": "timer" if u.endswith(".timer") else "service", "pre": dict(RS.UP)} for u in timers + services],
    }
    record = {  # the ORG-106 L1 record shape (tools/harness.py _do_pause_unlocked), plus pause_id and anchor
        "schema": harness_state.SCHEMA_SEAT_STATE, "persona": ctx.persona, "state": "paused-intake", "phase": "set",
        "phase_ts": ctx.ts, "set_ts": ctx.ts, "set_by": "germinate:harness-manager run_state",
        "owner": ctx.anchor.split(":", 1)[0], "reason": f"fleet bringdown: harness-manager run_state=paused ({pause_id})",
        "ticket": None, "expires": None, "indefinite": True, "telegram_dark_ack": False, "pre_expiry_notified": False,
        "ledger_entry": None, "pause_id": pause_id, "anchor": ctx.anchor, "plan_revision": ctx.revision,
    }
    from tools import seat_state

    problems = seat_state.validate_record(record)
    if problems:  # the L1 writer would refuse it too (harness_state.write_seat_state)
        raise PlanRefused("the ORG-106 seat-state record would be invalid: " + "; ".join(problems))
    prov = ctx.base | {"pause_id": pause_id, "run": "pause"}  # review R2 F1: explicit, read by the handler
    ops = [
        _rs_op(  # SOP 3.0 + Recovery (q): the manifest, atomic + fsync, before ANY mutation
            "rs-manifest", "harness.run-state-manifest", ctx.manifest_path, ctx.account, ctx.uid,
            {"manifest_sha256": "absent"},
            {"action": "record", "document": RS.canonical(doc), "sha256": RS.digest(doc), "write": "atomic-fsync",
             "postconditions": RS.encode([RS.check("document", cell="manifest", sha256=RS.digest(doc))])},
            crash="write-ahead-inverse-durable", provenance=prov, inverse={"manifest_sha256": "absent"},
        ),
        _rs_op(  # Recovery (p): the authorised-no, in the L1 record shape, before the first unit op
            "rs-seat-state", "harness.run-state-seat-state", ctx.seat_state_path, ctx.account, ctx.uid,
            {"seat_state_sha256": "absent"},
            {"action": "set", "document": RS.canonical(record), "sha256": RS.digest(record), "pause_id": pause_id,
             "anchor": ctx.anchor,
             "postconditions": RS.encode([RS.check("document", cell="seat_state", sha256=RS.digest(record))])},
            crash="write-ahead-inverse-durable", provenance=prov, inverse={"seat_state_sha256": "absent"},
        ),
    ]

    def unit_op(op_id, unit, phase):
        return _rs_op(
            op_id, "harness.run-state-unit", unit, ctx.account, ctx.uid, dict(RS.UP),
            {"action": "disable-now", "unit": unit, "phase": phase,
             "postconditions": RS.encode([RS.check("unit-fields", unit=unit, **RS.DOWN)])},
            crash="write-ahead-inverse-durable", provenance=prov, inverse=dict(RS.UP),
        )

    for i, unit in enumerate(timers):  # SOP 3.1: every timer first
        ops.append(unit_op(f"rs-timer-{i}", unit, "timer"))
    if timers:  # review R1-1: undone LAST (newest first), so only its undo may decide the seat is dark
        ops[2]["desired"]["undo_decides_dark"] = "true"
    if timers:  # SOP 3.2 + Recovery (r): a bounded wait; it never stops a oneshot
        drained = ",".join(ctx.oneshots[t] for t in timers)
        drain = _rs_op(
            "rs-drain", "harness.run-state-drain", f"{ctx.account}:drain", ctx.account, ctx.uid,
            {"oneshots_settled": drained},
            {"oneshots": drained, "window_s": _DRAIN_WINDOW_S, "interval_s": _DRAIN_INTERVAL_S, "on_timeout": "abort",
             "postconditions": RS.encode([])},
            crash="stageable", provenance=prov, inverse={},
        )
        # T-5866 (Recovery D15 T-5858 (r)/(r2)): a drain timeout ABORTS the pause, so the engine
        # rolls the timers back at once instead of waiting for an operator rollback(run_id)
        drain["rollback_on_halt"] = True
        ops.append(drain)
    for i, unit in enumerate(services):  # SOP 3.3
        ops.append(unit_op(f"rs-service-{i}", unit, "service"))
    reset = sorted(set(up) | {ctx.oneshots[t] for t in timers})
    ops.append(_rs_op(  # SOP 3.5
        "rs-reset-failed", "harness.run-state-reset-failed", f"{ctx.account}:reset-failed", ctx.account, ctx.uid,
        {}, {"units": ",".join(reset), "postconditions": RS.encode([])},
        crash="stageable", provenance=prov, inverse={},
    ))
    first = next(op for op in ops if op["kind"] == "harness.run-state-unit")
    first["gate"], first["desired"]["anchor"] = RUN_STATE_GATE, ctx.anchor  # D-10 + Recovery (o)
    allow = set(keep) | {ctx.oneshots[u] for u in keep if u in ctx.oneshots}
    checks = [RS.check("unit-fields", unit=u, **RS.DOWN) for u in up]  # SOP §4 verify 1, as fields
    for unit in keep:  # SOP §4 verify 2: the kept units, exactly as they were
        fields = _fields(ctx.units.get(unit))
        if fields is not None:
            checks.append(RS.check("unit-fields", unit=unit, **fields))
    checks += [
        RS.check("no-active-wake", allow=",".join(sorted(allow))),
        RS.check("no-new-failed", baseline=ctx.baseline),  # SOP §4 verify 3
        RS.check("document", cell="manifest", sha256=RS.digest(doc)),
        RS.check("document", cell="seat_state", sha256=RS.digest(record)),
    ]
    outcome = "paused-with-residue" if ctx.residue else "paused"
    if ctx.residue:  # SOP 3.6 / Recovery (s): reported, never claimed as paused
        checks.append(RS.check("residue", advisory=True, outcome=outcome, residue="; ".join(ctx.residue)))
    ops.append(_rs_op(
        "rs-commit", "harness.run-state-commit", f"{ctx.account}:run-state", ctx.account, ctx.uid,
        {"run_state": ctx.seat.get("run_state") or "running"},
        {"run_state": "paused", "pause_id": pause_id, "outcome": outcome, "residue": "; ".join(ctx.residue),
         "postconditions": RS.encode(checks)},
        crash="stageable", provenance=prov | {"pinned_targets": json.dumps(ctx.pins, sort_keys=True)},
        inverse={"run_state": ctx.seat.get("run_state") or "running"},
    ))
    return ops


def _plan_rs_restore(ctx):
    doc = ctx.seat.get("manifest")
    if doc is None and ctx.seat.get("seat_state") is None and ctx.seat.get("run_state") != "paused":
        return []  # review R1-2 (mason ruling): a running seat with no pause at all is a no-op, not an inference
    if doc is None:  # Recovery D15 (T-5858) (q): never infer what to restore
        raise PlanRefused(f"{ctx.persona}: no pause manifest; restore only what a pause recorded (never inferred)")
    problems, record = _manifest_ours(ctx, doc)
    if problems:
        raise PlanRefused(f"{ctx.persona}: the pause manifest is malformed or mismatched: {'; '.join(problems)}")
    if record.get("state") == RS.STATE_HALTED:  # Recovery D15 (T-5858) (r1): a halted pause is undone, never restored past its half-done state
        raise PlanRefused(
            f"{ctx.persona}: pause {doc['pause_id']} HALTED at {record.get('op_id')}; recover it with rollback(run_id) "
            f"({record.get('rollback_cmd')}), not a restore"
        )
    recorded = [e["unit"] for e in doc["entries"]]
    foreign = [u for u in ctx.keep if u not in recorded]
    if foreign:
        raise PlanRefused(f"desired.keep names {foreign}, not in the pause manifest {recorded}")
    pause_id = doc["pause_id"]
    restore = [e for e in doc["entries"] if e["unit"] not in ctx.keep]
    remainder = [e for e in doc["entries"] if e["unit"] in ctx.keep]
    todo = []
    for entry in restore:
        fields = _fields(ctx.units.get(entry["unit"]))
        if fields == entry["pre"]:
            continue  # already back at its recorded pre-state (a re-run after a partial restore)
        if fields != RS.DOWN:
            raise PlanRefused(f"{entry['unit']}: observed {fields}, neither its paused state nor its recorded pre-state")
        todo.append(entry)
    prov = ctx.base | {"pause_id": pause_id, "run": "restore"}  # review R2 F1: explicit, read by the handler
    ops = []

    def unit_op(op_id, entry):
        return _rs_op(
            op_id, "harness.run-state-unit", entry["unit"], ctx.account, ctx.uid, dict(RS.DOWN),
            {"action": "enable-now", "unit": entry["unit"], "phase": entry["phase"],
             "postconditions": RS.encode([RS.check("unit-fields", unit=entry["unit"], **entry["pre"])])},
            crash="write-ahead-inverse-durable", provenance=prov, inverse=dict(RS.DOWN),
        )

    # D-5: services before timers (the inverse of SOP §3), so no timer fires into a seat whose
    # bridge is still down
    for i, entry in enumerate([e for e in todo if e["phase"] == "service"]):
        ops.append(unit_op(f"rs-restore-service-{i}", entry))
    for i, entry in enumerate([e for e in todo if e["phase"] == "timer"]):
        ops.append(unit_op(f"rs-restore-timer-{i}", entry))
    after = dict(doc, entries=remainder) if remainder else None
    after_sha = RS.digest(after) if after else "absent"
    ops.append(_rs_op(
        "rs-manifest-retire", "harness.run-state-manifest", ctx.manifest_path, ctx.account, ctx.uid,
        {"manifest_sha256": RS.digest(doc)},
        {"action": "retire", "document": RS.canonical(after), "sha256": after_sha, "write": "atomic-fsync",
         "postconditions": RS.encode([RS.check("document", cell="manifest", sha256=after_sha)])},
        crash="write-ahead-inverse-durable", provenance=prov, inverse={"manifest_sha256": RS.digest(doc)},
    ))
    # Recovery (t): every restored unit back at its RECORDED pre-state and the consume timer with a
    # scheduled next fire; the checks read observed fields only, never a re-enabled unit's work
    checks = [RS.check("unit-fields", unit=e["unit"], **e["pre"]) for e in restore]
    checks += [RS.check("unit-fields", unit=e["unit"], **RS.DOWN) for e in remainder]
    checks += [RS.check("next-elapse", unit=e["unit"]) for e in restore if e["unit"].startswith(RS.CONSUME_STEM + "@")]
    checks += [RS.check("no-new-failed", baseline=ctx.baseline), RS.check("document", cell="manifest", sha256=after_sha)]
    ops.append(_rs_op(
        "rs-restore-commit", "harness.run-state-commit", f"{ctx.account}:run-state", ctx.account, ctx.uid,
        {"run_state": ctx.seat.get("run_state") or "paused"},
        {"run_state": "paused" if remainder else "running", "pause_id": pause_id,
         "outcome": "partially-restored" if remainder else "running", "residue": "", "postconditions": RS.encode(checks)},
        crash="stageable", provenance=prov | {"pinned_targets": json.dumps(ctx.pins, sort_keys=True)},
        inverse={"run_state": ctx.seat.get("run_state") or "paused"},
    ))
    if not remainder:  # Recovery (p) / (t): seat-state cleared LAST, only after the commit checks passed
        tomb = dict(record, ended_ts=ctx.ts, ended_by=f"germinate:harness-manager run_state=running ({ctx.anchor})")
        ops.append(_rs_op(
            "rs-seat-state-clear", "harness.run-state-seat-state", ctx.seat_state_path, ctx.account, ctx.uid,
            {"seat_state_sha256": RS.digest(record)},
            {"action": "clear", "document": "null", "sha256": "absent", "pause_id": pause_id, "tombstone": RS.canonical(tomb),
             "postconditions": RS.encode([RS.check("document", cell="seat_state", sha256="absent")])},
            crash="write-ahead-inverse-durable", provenance=prov, inverse={"seat_state_sha256": RS.digest(record)},
        ))
    first = next((op for op in ops if op["kind"] == "harness.run-state-unit"), None)
    if first is None:  # nothing left to enable: the run_state change is still operator-approved
        first = next(op for op in ops if op["kind"] == "harness.run-state-commit")
    first["gate"], first["desired"]["anchor"] = RUN_STATE_GATE, ctx.anchor
    return ops


# --- remove leg: the full-seat decommission (T-5859, T-5091 U3) ----------------------

#: The simulated observation handler for the remove leg (harness-manager_handlers.py).
DECOMMISSION_HANDLER = "harness-manager-decommission"
DECOMMISSION_GATE = R.GATE
_DECOMMISSION_KINDS = frozenset(R.KINDS)


def _refuse(code, message):
    """A typed refusal: the code leads the message so it survives the engine's PlanError."""

    return PlanRefused(f"[{code}] {message}")


def _dc_op(op_id, kind, target, account, uid, precondition, desired, *, crash, provenance, inverse, gate=None):
    """One decommission op, pinned to the seat's OWN user manager (Recovery D15 (T-5859) / U4 (i))."""

    pinned = {"systemd_manager": R.SYSTEMD_MANAGER, "account": account, "xdg_runtime_dir": f"/run/user/{uid}"}
    return _op(
        op_id, kind, target, account, {"user_bus": "reachable"} | precondition, pinned | desired,
        crash_contract=crash, provenance=provenance, gate=gate, inverse=inverse,
    )


def _dc_id(prefix, name):
    return f"rm-{prefix}-{hashlib.sha256(name.encode('utf-8')).hexdigest()[:10]}"


def _dc_paused(seat, persona, account):
    """D-3 (mason ruling): the seat must carry U5's committed pause. The pause's own residue
    (restart-drain, the quota timer, ...) is what this leg retires, so 'paused-with-residue'
    is admitted; a HALTED pause is not."""

    if not isinstance(seat, dict):
        return "no U5 run-state observation"
    if seat.get("run_state") != "paused":
        return f"run_state is {seat.get('run_state')!r}, not 'paused'"
    doc = seat.get("manifest")
    problems = RS.manifest_problems(doc, persona=persona, account=account)
    if problems:
        return "no usable U5 pause manifest: " + "; ".join(problems)
    record = seat.get("seat_state")
    if isinstance(record, dict) and record.get("state") == RS.STATE_HALTED:
        return "the pause HALTED (the seat-state record is halted); roll it back first"
    if not isinstance(record, dict) or record.get("state") != "paused-intake":
        return "no ORG-106 paused-intake seat-state record"
    if record.get("pause_id") != doc["pause_id"]:
        return f"seat-state pause_id {record.get('pause_id')!r} != manifest pause_id {doc['pause_id']!r}"
    return None


def _dc_inventory(plan, account, persona):
    """Classify every installer op of the seat's bring-up (harness_manager_remove tables).
    Returns (moves [source], units {unit: source}, controller, session, retained [dict])."""

    home = R.home_of(account)
    moves, units, retained, covered = [], {}, [], []
    controller = session = state_root = None
    for item in plan.operations:
        try:
            what = R.disposition(item.kind)
            if what == R.MKDIR:
                what = {R.SHARED: R.RETAINED, R.MOVE_DIR: R.MOVE, R.INSIDE: R.COVERED}[R.mkdir_disposition(item.operation_id)]
        except R.Unclassified as exc:
            raise _refuse("unclassified-artifact", str(exc)) from None
        target = item.target
        if what == R.MOVE:
            parent = target.rsplit("/", 1)[0]
            if item.kind in ("user-unit-install", "seat-periodic-unit-install"):
                if parent.endswith(".d"):  # a drop-in: its whole .d dir goes
                    moves.append(parent)
                else:
                    moves.append(target)
                    units[R.instance_unit(R.unit_name_of(target), persona)] = item.operation_id
            else:
                moves.append(target)
                if item.kind == "seat-runtime-residue-quarantine":
                    state_root = target
        elif what == R.UNIT:
            units[target] = item.operation_id
            if target.startswith(R.CONTROLLER_STEM + "@"):
                controller = target
        elif what == R.RETAINED:
            if item.kind == "mkdir":
                retained.append({"item": target, "reason": R.MKDIR_RETAIN_REASON.get(item.operation_id, R.DEFAULT_MKDIR_RETAIN_REASON)})
            else:
                name = item.desired.get("name") or item.desired.get("env_key") or item.desired.get("key") or ""
                retained.append({"item": f"{target} {item.kind} {name}".strip(),
                                 "reason": "a key inside a shared seat document; a key removal is deletion-shaped (D-2)"})
        elif what == R.COVERED:
            covered.append((item, item.desired.get("quarantine_dir") or target))
        elif item.kind == "mailbox-probe":
            retained.append({"item": target, "reason": "the seat's mailbox dir: not installer-created (mailbox component), "
                                                       "never moved by a seat op (D-2)"})
        tmux = item.desired.get("tmux_target")
        if session is None and isinstance(tmux, str) and tmux:
            session = tmux.split(":", 1)[0]
    moves = sorted(set(moves))
    outside = [m for m in moves if not R.under(m, home) or m == home]
    if outside:
        raise _refuse("unclassified-artifact", f"installer artifact(s) {outside} lie outside the seat's home {home}")
    moves = [m for m in moves if not any(m != o and R.under(m, o) for o in moves)]
    for item, path in covered:
        if not any(R.under(path, m) for m in moves):
            raise _refuse("unclassified-artifact", f"installer op {item.operation_id} ({item.kind}) writes {path}, "
                                                   "outside every quarantined tree")
    return moves, units, controller, session, retained, state_root


def _dc_carve_out_records(moves, files, state_root, retained, absent):
    """Review F2 (ORG-106): replace the persona state root as ONE move by a move per direct
    child except ``overrides/`` (the ORG-106 records dir, harness_state.py seat_state_path /
    holds), which stays in place so the U5 paused-intake record stays effective; it is listed
    as retained when present, as absent when not (review R2-5). A carve-out, never a
    deletion: the root dir itself stays (it still holds overrides/)."""

    if state_root is None or state_root not in moves:
        return moves
    top = files.get(state_root)
    if top is not None and (not isinstance(top, dict) or top.get("kind") != "dir"):
        raise _refuse("source-symlink", f"{state_root} is a symlink or not a directory; refusing to follow it")
    children = sorted({p[len(state_root) + 1:].split("/", 1)[0] for p in files if p.startswith(state_root + "/")})
    overrides = f"{state_root}/{R.ORG106_DIR}"
    if R.subtree(files, overrides):
        retained.append({"item": overrides, "reason": "ORG-106 record keeps a decommissioned seat's intake blocked"})
    else:
        absent.append(overrides)
    return sorted([m for m in moves if m != state_root] + [f"{state_root}/{c}" for c in children if c != R.ORG106_DIR])


def dc_record_vetoes(record, observed_at):
    """Recovery D15 (T-5859) (x3): does this seat-state record veto remediation at ``observed_at``?
    ``tools.seat_state.parse_record`` itself (pure: no I/O), so the semantics are the live
    reader's (only an unexpired, valid deep-sleep record vetoes)."""

    from datetime import datetime, timezone

    from tools import seat_state

    now = datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return bool(seat_state.parse_record(record, now=now).remediation_vetoed)


def _dc_docs():
    """The routing / cross-seat documents (Recovery D15 (T-5859) (w)/(x)/(x2)): read from the
    PLANNER's checkout (``persona_installer.REPO_ROOT``, the tree U5's ``_recovery_residue`` reads),
    which is bound to the seat by the instance.yaml digest check (mason ruling Q2), not
    re-read at ``source_revision``. YAML parsed, the rest as text, None when absent. No live
    read of any host."""

    import yaml
    from tools.persona_installer import REPO_ROOT

    kinds = {p: k for p, k, _w in R.ROUTING_SOURCES + R.CROSS_SEAT_SOURCES}
    docs = {}
    for path in R.SOURCE_PATHS:
        file = REPO_ROOT / path
        if not file.is_file():
            docs[path] = None
            continue
        text = file.read_text(encoding="utf-8")
        docs[path] = yaml.safe_load(text) if kinds[path].startswith("yaml") else text
    return docs


def plan_remove(request):
    """The ``remove`` leg: the full-seat decommission, SEAT HALF ONLY. UNTESTED-LIVE; see
    "REMOVE LEG" in the module docstring."""

    scope = request.get("scope") or ""
    if not scope.startswith(_SEAT_SCOPE_PREFIX):
        raise _refuse("scope-refused", "remove runs one sub-run per seat as that seat's own account: it requires a seat scope (Q3)")
    account = scope[len(_SEAT_SCOPE_PREFIX):]
    desired = request.get("desired") or {}
    foreign = sorted(k for k in desired if k not in ("stamp", "anchor", "source_revision"))
    if foreign:
        raise _refuse("inputs-refused", f"remove takes stamp, anchor and source_revision only, not {foreign} (ORG-080 D-L)")
    stamp = desired.get("stamp")
    if not isinstance(stamp, str) or not R.STAMP_RE.fullmatch(stamp):
        raise _refuse("inputs-refused", "remove needs desired.stamp as YYYYMMDDTHHMMSSZ (it names the quarantine tree)")
    anchor = desired.get("anchor")
    if not isinstance(anchor, str) or not R.OPERATOR_ANCHOR_RE.fullmatch(anchor):
        raise _refuse("operator-anchor-required", "retiring a persona is org-shape: every seat's decommission needs "
                                                 "desired.anchor operator:<ref> (Recovery D15 (T-5859) (v))")
    revision = desired.get("source_revision")
    if not isinstance(revision, str) or not _FULL_COMMIT.fullmatch(revision):
        raise _refuse("inputs-refused", "remove needs desired.source_revision, a full 40-hex commit id (the planner never asks git)")
    observed = request.get("observed") or {}
    dc = (observed.get(DECOMMISSION_HANDLER) or {}).get("seat")
    if not isinstance(dc, dict):
        raise _refuse("observation-missing", f"request carries no {DECOMMISSION_HANDLER} seat observation for scope {scope!r}")

    from tools.germinate.components import harness_manager_root as HR
    from tools.persona_installer import REPO_ROOT, Manifest, ManifestError, PersonaInstaller

    manifest_sha = hashlib.sha256((REPO_ROOT / "instance.yaml").read_bytes()).hexdigest()
    manifest = Manifest.load(REPO_ROOT / "instance.yaml")
    persona = _persona_for_account(manifest, account)
    role_key, role = _role_key(manifest, persona)
    if _is_recovery_role(role_key):  # Recovery D15 (T-5859): the recovery tier is never decommissioned here
        raise _refuse("role-protected", f"{persona} ({role_key}) is the recovery tier")
    try:
        identity = HR.identities(HR.load_instance(REPO_ROOT / "instance.yaml")).get(role_key)
    except HR.IdentityRefused as exc:
        raise _refuse("identity-refused", str(exc)) from None
    if identity is None:
        raise _refuse("identity-missing", f"roles.{role_key} has no identity block: root_half is unknown")
    if identity.root_half == "managed":  # mason ruling (i)
        raise _refuse("root-half-pending", f"{persona} is root_half: managed; the per-seat root retire and the "
                                           f"seat-before-root ordering on remove are {R.ROOT_HALF_TICKET}")
    if role.get("supervisor") == "bespoke":
        raise _refuse("bespoke-supervisor", f"{persona} runs a bespoke supervisor; cut it over (configure sub-op (a)) first")
    if dc.get("target_manifest_sha256") != manifest_sha:  # mason ruling Q2 (the U4 rule)
        raise _refuse("manifest-mismatch", f"{persona}: the seat clone's instance.yaml is not the plan's (sha256 {manifest_sha})")
    if dc.get("user_bus") is not True:
        raise _refuse("user-bus-unreachable", f"{persona}: the seat's user bus is not proven reachable")
    uid = dc.get("uid")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise _refuse("observation-missing", f"{persona}: no observed seat uid")
    why = _dc_paused((observed.get(RUN_STATE_HANDLER) or {}).get("seat"), persona, account)
    if why is not None:
        raise _refuse("seat-not-paused", f"{persona}: {why}; pause it (configure run_state=paused) first (D-3)")
    runtime_seat = (observed.get(HANDLER) or {}).get("seat")
    if not isinstance(runtime_seat, dict):  # review F5: refuse like every other check
        raise _refuse("observation-missing", f"{persona}: no {HANDLER} observation (the runtime transition cannot be ruled out)")
    transition = runtime_seat.get("transition") or {}
    if transition.get("phase") in IN_FLIGHT:
        raise _refuse("transition-in-flight", f"{persona}: a runtime transition is {transition.get('phase')}")
    units_seen, files, sessions = dc.get("units"), dc.get("files"), dc.get("sessions")
    if not (isinstance(units_seen, dict) and isinstance(files, dict) and isinstance(sessions, dict)):
        raise _refuse("observation-missing", f"{persona}: units, files and sessions must all be observed")

    try:
        plan = PersonaInstaller(repo_root=REPO_ROOT).plan(manifest, persona, source_revision=revision)
    except ManifestError as exc:
        raise _refuse("installer-plan-refused", f"{persona}: {exc}") from None
    moves, candidates, controller, session, retained, state_root = _dc_inventory(plan, account, persona)
    carved_absent = []
    moves = _dc_carve_out_records(moves, files, state_root, retained, carved_absent)
    root = R.quarantine_root(account, stamp)
    if R.subtree(files, root):
        raise _refuse("quarantine-exists", f"{root} already exists; choose a fresh stamp (nothing is overwritten)")
    if any(R.under(root, m) for m in moves):  # structural; the tables keep it unreachable
        raise _refuse("quarantine-inside-source", f"{root} lies inside a tree being quarantined")
    unit_dir = f"{R.home_of(account)}/.config/systemd/user"
    for name in sorted(units_seen):  # an observed unit loaded from a file this leg moves
        if any(R.under(R.fragment_of(name, unit_dir), m) for m in moves):
            candidates.setdefault(name, "observed (fragment quarantined)")
    # the carved persona state root counts as a referenced tree too: its children move, so a
    # unit running from it (or from anything under it) would break (review F1/F2); its retained
    # overrides/ never moves, so a unit running from there is no hit (review R2-4)
    carved = state_root if state_root and R.subtree(files, state_root) else None
    referenced = sorted(set(moves) | ({carved} if carved else set()))
    exclude = [f"{state_root}/{R.ORG106_DIR}"] if carved else []
    peer = R.peer_refs_inside(dc.get("peer_units"), referenced, exclude)
    if peer:  # Recovery D15 (T-5859) (x)
        raise _refuse("peer-reference-live", f"live peer unit(s) resolve inside a tree being quarantined: {'; '.join(peer)}")
    # review F4: the seat's OWN live services outside the installer plan (they are not stopped
    # here) must not execute from a tree being quarantined; their ExecStart must be observed
    triggered = R.triggered_by_live_timers(units_seen)
    own = {n: u for n, u in units_seen.items() if n.endswith(".service") and n not in candidates
           and isinstance(u, dict) and (R.unit_live(u) or n in triggered)}
    blind = sorted(n for n, u in own.items() if "ExecStart" not in u)
    if blind:
        raise _refuse("observation-missing", f"{persona}: no ExecStart observed for live seat unit(s) {blind}")
    inside = R.live_refs_inside(own, referenced, account=account, exclude=exclude)
    if inside:
        raise _refuse("seat-unit-inside-quarantine", f"live seat unit(s) outside the installer plan execute from a tree "
                                                     f"being quarantined: {'; '.join(inside)}")
    docs = _dc_docs()
    coverage = R.peer_coverage_units(docs.get("tools/systemd/expected-units.yaml"), persona)
    armed, unknown = R.peer_coverage_state(dc.get("peer_units"), coverage)
    if armed:  # Recovery D15 (T-5859) (x2): disarming them is an operator step before the gate
        raise _refuse("peer-coverage-armed", f"Recovery-side unit(s) targeting {persona} are enabled or active: "
                                             f"{'; '.join(armed)}; disable them (cross-account, by hand) first")
    if unknown:  # (x2): fail closed when the observation cannot say
        raise _refuse("peer-coverage-could-not-ask", f"no observation proves these peer units targeting {persona} "
                                                     f"are disarmed or absent: {', '.join(unknown)}")
    vetoed = R.remediation_vetoed(dc.get("remediation"), dc_record_vetoes)
    if vetoed is not True:  # Recovery D15 (T-5859) (x3): fleet remediation could re-enable the seat's timers
        why = "remediation is not observed (could not ask)" if vetoed is None else "remediation is not vetoed"
        raise _refuse("remediation-not-vetoed", f"{persona}: {why}; arm seat-maintenance for {persona} (or put it in "
                                                "deep-sleep) first, an operator step before the gate; paused-intake "
                                                "alone does not veto pantheon-parity-remediate")

    stop = []
    for name in sorted(candidates):
        unit = units_seen.get(name)
        if not isinstance(unit, dict) or unit.get("LoadState") == "not-found":
            continue
        fields = {"UnitFileState": unit.get("UnitFileState"), "ActiveState": unit.get("ActiveState")}
        if fields == R.UP:
            stop.append(name)
        elif not (fields["ActiveState"] == "inactive" and fields["UnitFileState"] in R.SETTLED_FILE_STATES):
            raise _refuse("unit-state-unfaithful", f"{name}: {fields} is neither up nor settled; settle it first (U5 (u))")
    stop.sort(key=lambda u: (u == controller, not u.endswith(".timer"), u))  # timers, services, controller LAST
    if session in sessions:
        owner = (sessions[session] or {}).get("scope") if isinstance(sessions[session], dict) else None
        if owner != R.RUNTIME_SCOPE_UNIT or controller not in stop:
            raise _refuse("session-unowned", f"tmux session {session!r} (scope {owner!r}) would survive the controller "
                                             "stop; a seat op never kills a session directly")
    move_entries = []
    for source in moves:
        entries = R.subtree(files, source)
        if not entries:
            continue  # nothing there: surfaced in the manifest as absent, never silently skipped
        top = entries.get("")
        if not isinstance(top, dict) or top.get("kind") not in ("file", "dir"):
            raise _refuse("source-symlink", f"{source} is a symlink or not a regular file/dir; refusing to follow it")
        move_entries.append({"source": source, "destination": R.destination(root, account, source),
                             "kind": top["kind"], "sha256": R.tree_digest(files, source),
                             "bytes": R.tree_bytes(files, source)})
    absent = [m for m in moves if not R.subtree(files, m)] + carved_absent
    if state_root is not None and not R.subtree(files, state_root):
        absent.append(state_root)

    routing = R.routing_residue(docs, persona=persona, role_key=role_key)
    cross = R.cross_seat_residue(docs, persona=persona, role_key=role_key)
    bridge = ((manifest.installer.get("codex_telegram_bridge") or {}).get("personas") or {}).get(persona) or {}
    secrets, underivable = R.secrets_retained(account=account, persona=persona, role=role, bridge=bridge,
                                              moves=[(e["source"], e["destination"]) for e in move_entries])
    run_state_seat = (observed.get(RUN_STATE_HANDLER) or {}).get("seat") or {}
    manifest_path, _seat_state_path = _rs_paths(account, persona)
    retained.append({"item": manifest_path, "reason": "the U5 pause manifest: evidence of the pause this leg rests on"})
    residue = routing + cross
    outcome = "decommissioned-with-residue" if residue else "decommissioned"
    body = {
        "schema": R.RECORD_SCHEMA, "persona": persona, "account": account, "role": role_key, "stamp": stamp,
        "anchor": anchor, "plan_revision": revision, "instance_yaml_sha256": manifest_sha, "quarantine_root": root,
        "pause_id": (run_state_seat.get("manifest") or {}).get("pause_id"),
        "units": [{"unit": u, "pre": dict(R.UP)} for u in stop],
        "moves": move_entries, "absent": absent, "retained": retained,
        "secrets_retained": secrets, "secrets_underivable": underivable,
        "routing_residue": routing, "cross_seat_residue": cross, "outcome": outcome,
        "peer_coverage": coverage,
        "root_half": identity.root_half,
        "root_ops": "none: observe-only (account, home, linger and memberships retained untouched)",
    }
    prov = {
        "adapter_of": "the inverse of plan_install (tools.persona_installer.PersonaInstaller.plan), remove leg",
        "instance_yaml_sha256": manifest_sha, "plan_source_revision": revision, "untested_live": "true",
        "root_half": identity.root_half, "root_ops": "none (observe-only)", "quarantine_root": root,
    }
    ops = [_dc_op(  # first, before any mutation: the manifest is the undo's map (gate + operator anchor)
        "rm-record", "harness.decommission-record", R.record_path(root), account, uid, {"record_sha256": "absent"},
        {"action": "record", "document": R.canonical(body), "sha256": R.digest(body), "status": "staged",
         "write": "atomic-fsync", "anchor": anchor, "peer_coverage": ",".join(coverage),
         "remediation_veto": "required",  # Recovery D15 (T-5859) (x3), re-checked at apply
         "postconditions": R.encode([R.check("record", sha256=R.digest(body), status="staged")])},
        crash="write-ahead-inverse-durable", provenance=prov, inverse={"record_status": "undone"}, gate=DECOMMISSION_GATE,
    )]
    for name in stop:
        checks = [R.check("unit-fields", unit=name, **R.DOWN)]
        extra = {}
        if name == controller:
            checks += [R.check("unit-gone", unit=R.RUNTIME_SCOPE_UNIT)]
            if session is not None:
                checks.append(R.check("session-absent", session=session))
            extra = {"undo": "daemon-reload-then-enable", "requires_restored": ",".join(e["source"] for e in move_entries),
                     "escalation_recipient": R.ESCALATION_RECIPIENT, "scope_unit": R.RUNTIME_SCOPE_UNIT,
                     "session": session or "",
                     # review R2-2: (x2) is re-checked on the op the context-fill restart path targets
                     "peer_coverage": ",".join(coverage), "remediation_veto": "required"}
        ops.append(_dc_op(
            _dc_id("unit", name), "harness.decommission-unit", name, account, uid, dict(R.UP),
            {"action": "disable-now", "unit": name, "phase": "timer" if name.endswith(".timer") else "service",
             "reset_failed": "true", "postconditions": R.encode(checks)} | extra,
            crash="write-ahead-inverse-durable", provenance=prov,
            inverse=dict(R.UP) | ({"undo": "daemon-reload-then-enable"} if name == controller else {}),
        ))
    for entry in move_entries:
        # review R2-3: a per-child move of the carved state root carries the root (and the
        # retained overrides/) so the apply-time (x) re-check covers a unit running from the root
        root_ref = ({"referenced_root": carved, "referenced_exclude": exclude[0]}
                    if carved and R.under(entry["source"], carved) else {})
        ops.append(_dc_op(
            _dc_id("move", entry["source"]), "harness.decommission-move", entry["source"], account, uid,
            {"source_sha256": entry["sha256"], "destination": "absent"},
            {"action": "quarantine", "source": entry["source"], "destination": entry["destination"],
             "entry_kind": entry["kind"], "sha256": entry["sha256"], "bytes": str(entry["bytes"]),
             "rename": "same-filesystem", "follow_symlinks": "never", **root_ref,
             "postconditions": R.encode([R.check("path-absent", path=entry["source"]),
                                         R.check("path-digest", path=entry["destination"], sha256=entry["sha256"])])},
            crash="write-ahead-inverse-durable", provenance=prov,
            inverse={"source_sha256": entry["sha256"], "undo": "rename-back"},
        ))
    gone = sorted(n for n in candidates if isinstance(units_seen.get(n), dict))
    ops.append(_dc_op(
        "rm-reload", "harness.decommission-unit", f"{account}:daemon-reload", account, uid, {},
        {"action": "daemon-reload", "postconditions": R.encode([R.check("unit-gone", unit=u) for u in gone])},
        crash="stageable", provenance=prov, inverse={},
    ))
    checks = [R.check("unit-gone", unit=u) for u in gone]
    checks += [R.check("path-absent", path=e["source"]) for e in move_entries]
    checks += [R.check("path-digest", path=e["destination"], sha256=e["sha256"]) for e in move_entries]
    if session is not None:
        checks.append(R.check("session-absent", session=session))
    checks.append(R.check("record", sha256=R.digest(body), status="committed"))
    if residue:  # Recovery D15 (T-5859) (w)/(x): never silent
        checks.append(R.check("residue", advisory=True, outcome=outcome, residue="; ".join(residue)))
    ops.append(_dc_op(
        "rm-commit", "harness.decommission-commit", f"{account}:decommission", account, uid, {"record_status": "staged"},
        {"status": "committed", "record_path": R.record_path(root), "outcome": outcome, "residue": "; ".join(residue),
         "postconditions": R.encode(checks)},
        crash="stageable", provenance=prov, inverse={"record_status": "staged"},
    ))
    # D-6: from the controller stop on (or from the first mutation when the controller is
    # already down) a halt leaves the seat half-removed and unsupervised: it escalates
    first = next((i for i, op in enumerate(ops) if op["target"] == controller), 1)
    for op in ops[first:]:
        op["desired"]["escalate_on_halt"] = "true"
    _assert_dc_scoped(ops, account, root)
    return ops


def _assert_dc_scoped(ops, account, root):
    """Planner-side scope check: every path an op names is inside the seat's own home, and
    every destination inside this run's quarantine tree."""

    home = R.home_of(account)
    for op in ops:
        d = op["desired"]
        if op["kind"] == "harness.decommission-move":
            if not (R.under(d["source"], home) and d["source"] != home and R.under(d["destination"], root + "/tree")):
                raise _refuse("scope-refused", f"op {op['operation_id']} moves {d['source']} -> {d['destination']}")
        if op["kind"] == "harness.decommission-record" and not R.under(op["target"], root):
            raise _refuse("scope-refused", f"op {op['operation_id']} writes {op['target']} outside {root}")
