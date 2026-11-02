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
Q1, ORG-080 :571 / A-4; T-5091 U2).

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
  (36 kinds in all: 12 none + 24 seat)

Layer, target, precondition, desired, inverse, owner and execution_scope pass
through unchanged. Installer ids (dotted) become the engine alphabet: dots to
``_``, over 64 chars a 47-char prefix + ``-`` + 16 hex of sha256; a collision
refuses. The installer id and privilege label stay in provenance.

Returns typed operation data only.
"""

from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

from tools import harness_state
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
