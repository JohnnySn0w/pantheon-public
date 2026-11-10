"""harness-manager component: executor-side registration (ORG-080 §2; T-5090, GF1).

``OP_KINDS`` is literal data (read with ast.literal_eval): seat privilege, in the
component's own non-reserved classes ``harness.runtime`` (sub-op b) and
``harness.supervisor`` (sub-op a, T-5830; one isolated block, Recovery D15 (T-5830)).

``HANDLERS`` holds FIVE handlers, all SIMULATED (CI tier, nothing live; the
actuation boundary of the HO ruling, T-3296/T-5106): ``HarnessSeatSim`` for the
configure (b) swap below, ``HarnessInstallSim`` for the install leg's
installer kinds (T-5091 U1; see its docstring, which records the P6 tension of
claiming registry kinds in the shared simulated backend), and
``HarnessSupervisorSim`` for configure (a), the supervisor cutover (T-5830,
UNTESTED-LIVE; see its docstring), and ``HarnessRunStateSim`` for configure run_state,
fleet bringdown/restore (T-5858, UNTESTED-LIVE; see its docstring), and
``HarnessDecommissionSim`` for the remove leg, the full-seat decommission (T-5859,
UNTESTED-LIVE; see its docstring). ``HarnessSeatSim`` keeps,
per seat account, JSON stand-ins for what the existing swap writes:

* ``runtime``    -- runtime-controller/runtime.json's selection
                    (runtime_controller.py:7255, ``_replace_runtime``);
* ``receiver``   -- which harness holds the Telegram receiver
                    (the bridge handoff, runtime_controller.py:7794);
* ``transition`` -- runtime-controller/transition.json's record
                    (runtime_controller.py:7963-7999 / :8048);
* ``readiness``  -- the simulated result of ``prove_runtime_readiness`` per runtime;
* plus read-only plan inputs (role block, holds, communications) and the verify
  ``facts`` block harness_manager_verify evaluates.

Every op rewrites exactly one slot (``transition`` | ``receiver`` | ``runtime``).
The write-ahead inverse is the slot's JSON before the op; ``can_rollback`` tells
not-applied (slot == before) / owned (slot == the op's own result) / drifted.
"""

from __future__ import annotations

import copy
import hashlib
import json

from tools.germinate.components import harness_manager_remove as R
from tools.germinate.components import harness_manager_run_state as RS
from tools.germinate.components import harness_manager_supervisor as S
from tools.germinate.engine.escalation import EscalatingHalt, EscalatingRollbackStop, rollback_command
from tools.persona_installer import MutationRecord, OperationEvidence, OperationFailed, PreflightResult

OP_KINDS = {
    "harness.transition-stage": ("harness.runtime", "seat"),
    "harness.receiver-handoff": ("harness.runtime", "seat"),
    "harness.runtime-replace": ("harness.runtime", "seat"),
    "harness.transition-commit": ("harness.runtime", "seat"),
    # --- configure sub-op (a), the supervisor cutover (T-5830). ONE isolated block so a
    # reclassification is a small diff (mason ruling Q1). Component-local class
    # harness.supervisor, seat privilege, the seat's own account: Recovery D15 (T-5830)
    # approved on the GF1 harness.runtime precedent; no reserved class is taken.
    "harness.supervisor-file": ("harness.supervisor", "seat"),
    "harness.supervisor-unit": ("harness.supervisor", "seat"),
    "harness.companion-unit": ("harness.supervisor", "seat"),
    "harness.control-plane-teardown": ("harness.supervisor", "seat"),
    "harness.consume-retire": ("harness.supervisor", "seat"),
    "harness.supervisor-commit": ("harness.supervisor", "seat"),
    # --- configure run_state, fleet bringdown/restore (T-5858). ONE isolated block, as
    # above. Component-local class harness.run-state, seat privilege, the seat's own
    # account, DARK: Recovery D15 (T-5858) approved with conditions (o)-(u).
    "harness.run-state-manifest": ("harness.run-state", "seat"),
    "harness.run-state-seat-state": ("harness.run-state", "seat"),
    "harness.run-state-unit": ("harness.run-state", "seat"),
    "harness.run-state-drain": ("harness.run-state", "seat"),
    "harness.run-state-reset-failed": ("harness.run-state", "seat"),
    "harness.run-state-commit": ("harness.run-state", "seat"),
    # --- the remove leg, full-seat decommission (T-5859). ONE isolated block, as above.
    # Component-local class harness.decommission, seat privilege, the seat's own account,
    # DARK, seat half only: Recovery D15 (T-5859) approved with conditions (v)-(z).
    "harness.decommission-record": ("harness.decommission", "seat"),
    "harness.decommission-unit": ("harness.decommission", "seat"),
    "harness.decommission-move": ("harness.decommission", "seat"),
    "harness.decommission-commit": ("harness.decommission", "seat"),
}

_SLOT_OF = {
    "harness.transition-stage": "transition",
    "harness.transition-commit": "transition",
    "harness.receiver-handoff": "receiver",
    "harness.runtime-replace": "runtime",
}
_SEAT_PREFIX = "seat-"


def _canon(value):
    return json.dumps(value, sort_keys=True)


def _fingerprint(value):
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


class HarnessSeatSim:
    name = "harness-manager-seat"
    kinds = frozenset(_SLOT_OF)

    def __init__(self):
        self.state = {"seats": {}}

    # --- slots -----------------------------------------------------------------

    @staticmethod
    def _split(target):
        account, _, slot = target.partition(":")
        return account, slot

    def _seat(self, account):
        seat = self.state["seats"].get(account)
        if seat is None:
            raise KeyError(f"no simulated seat {account!r}")
        return seat

    def _slot(self, op_or_mutation):
        account, slot = self._split(op_or_mutation.target)
        if slot != _SLOT_OF[op_or_mutation.kind]:
            raise KeyError(f"{op_or_mutation.kind} cannot write slot {slot!r}")
        return self._seat(account), slot

    @staticmethod
    def _result(op):
        """The slot value the op leaves (a pure function of the op)."""

        if op.kind == "harness.runtime-replace":
            return {"selected": op.desired["selected"]}
        if op.kind == "harness.receiver-handoff":
            holder = op.desired["holder"]
            return {"holder": None if holder == "none" else holder, "last_handoff": f"{op.desired['direction']}:{op.desired['phase']}"}
        return {k: op.desired[k] for k in ("transition_id", "outgoing", "incoming", "phase")}

    @staticmethod
    def _matches(slot, value, precondition):
        if slot == "transition":
            if precondition.get("phase") == "none":
                return value is None
            return isinstance(value, dict) and all(value.get(k) == v for k, v in precondition.items())
        if slot == "receiver":
            holder = (value or {}).get("holder") or "none"
            return holder == precondition.get("holder")
        return (value or {}).get("selected") == precondition.get("selected")

    # --- EffectHandler ---------------------------------------------------------

    def observe(self, component, scope):
        if not scope.startswith(_SEAT_PREFIX):
            return {"account": None, "seat": None}
        account = scope[len(_SEAT_PREFIX):]
        seat = self.state["seats"].get(account)
        return {"account": account, "seat": copy.deepcopy(seat)}

    def preflight(self, op):
        try:
            seat, slot = self._slot(op)
        except KeyError as exc:
            return PreflightResult(False, self.name, str(exc))
        ok = self._matches(slot, seat.get(slot), op.precondition)
        return PreflightResult(ok, self.name, f"{slot} at its precondition" if ok else f"{slot} moved from its precondition")

    def rollback_intent(self, op):
        seat, slot = self._slot(op)
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, {"slot": _canon(seat.get(slot))}, _fingerprint(self._result(op)))

    def execute(self, op):
        mutation = self.rollback_intent(op)
        seat, slot = self._slot(op)
        seat[slot] = self._result(op)
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"simulated {op.kind}")

    def _postcondition(self, op, value):
        """The op's postcondition as a predicate that stays true once later ops of
        the same swap advance the slot (engine verify re-checks every receipted op):
        a staged record holds for its transition at or past staging; the "pre"
        handoff holds while the source no longer holds the receiver (the
        probe-<source>-absent action)."""

        if op.kind == "harness.transition-stage":
            ok = isinstance(value, dict) and all(value.get(k) == op.desired[k] for k in ("transition_id", "outgoing", "incoming"))
            return ok and value.get("phase") in ("preparing", "awaiting-successor-read")
        if op.kind == "harness.receiver-handoff" and op.desired["phase"] == "pre":
            return ((value or {}).get("holder") or "none") != op.precondition["holder"]
        return value == self._result(op)

    def verify_operation(self, op):
        seat, slot = self._slot(op)
        if not self._postcondition(op, seat.get(slot)):
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, f"{slot} does not meet the op's postcondition")
        if op.kind == "harness.runtime-replace":
            # Stand-in for integration_for(h).prove_runtime_readiness (runtime_controller.py:8028).
            readiness = seat.get("readiness")
            ready = readiness.get(op.desired["selected"]) if isinstance(readiness, dict) else None
            if ready is not True:
                detail = (
                    f"{op.desired['selected']} not input-ready"
                    if ready is False
                    else f"{op.desired['selected']} readiness proof is missing or not true"
                )
                return OperationEvidence(op.operation_id, op.layer, op.kind, False, detail)
        return OperationEvidence(op.operation_id, op.layer, op.kind, True, f"{slot} verified")

    def can_rollback(self, mutation) -> PreflightResult:
        try:
            seat, slot = self._slot(mutation)
        except KeyError as exc:
            return PreflightResult(False, "drifted", str(exc))
        current = seat.get(slot)
        if _canon(current) == mutation.before["slot"]:
            return PreflightResult(True, "not-applied", f"{slot} still at its preimage")
        if _fingerprint(current) == mutation.after_fingerprint:
            return PreflightResult(True, "owned", f"{slot} holds this op's result")
        return PreflightResult(False, "drifted", f"{slot} moved since this op")

    def rollback_mutation(self, mutation):
        seat, slot = self._slot(mutation)
        seat[slot] = json.loads(mutation.before["slot"])

    def dump_state(self):
        return copy.deepcopy(self.state)

    def load_state(self, state):
        self.state = {"seats": copy.deepcopy(dict(state.get("seats") or {}))}


#: T-5091 U1: the installer kinds HarnessInstallSim simulates. Every one is an
#: existing typed-op registry kind (NOT declared in OP_KINDS: ORG-080 D-AC), and
#: mkdir / atomic-write are absent because the engine's built-in
#: InstallerFileHandler already serves them.
_INSTALL_READ_ONLY_KINDS = frozenset({
    "brief-check", "claude-mcp-server-check", "codex-standing-instructions-override-check",
    "codex-standing-instructions-source-check", "codex-standing-instructions-target-check",
    "codex-telegram-disabled-first", "harness-check", "identity-check", "manager-check",
    "policy-check", "source-check", "timer-enablement-absence-check",
})
_INSTALL_MUTATING_KINDS = frozenset({
    "claude-mcp-server-registration", "codex-standing-instructions-install", "codex-telegram-mcp-check",
    "codex-telegram-source-check", "daemon-reload", "direct-fire", "mailbox-probe",
    "mailbox-writable-root-check", "seat-claude-settings-env", "seat-claude-workspace-trust",
    "seat-codex-hook-config",
    "seat-periodic-daemon-reload", "seat-periodic-timer-enable", "seat-periodic-timer-observe",
    "seat-periodic-unit-install", "seat-runtime-residue-quarantine", "seat-service-decommission",
    "seat-service-enable-observe", "stale-unit-failure-reset", "timer-enable", "timer-observe",
    "unit-fire", "user-unit-install",
})


class HarnessInstallSim:
    """SIMULATED installer kinds for plan_install (T-5091 U1; CI tier, nothing live).

    State, per seat account (the op's ``owner``)::

        {"slots": {operation_id: json}, "facts": {target: bool}, "facts_default": bool}

    Installer plans reuse targets (two daemon-reloads of one manager; a check and
    a registration of one ``.claude.json``), so each MUTATING op owns one slot keyed
    by its operation id. The value it leaves is a pure function of the op (its
    kind, target and canonical desired). The write-ahead inverse is the slot's JSON
    before the op; ``can_rollback`` tells not-applied (slot == before) / owned
    (slot == this op's result) / drifted (anything else: refused). Preflight admits
    an absent slot or one already at this op's result (idempotent re-entry) and
    refuses a foreign value.

    READ-ONLY (diagnostics.read) kinds write nothing; preflight and verify read
    the seeded ``facts[target]`` (``facts_default`` when unseeded).

    P6 tension, recorded not resolved: this handler claims 22 broker-envelope /
    seat-local registry kinds (34 with the read-only checks, i.e. every installer
    kind but mkdir / atomic-write) in the ONE shared simulated backend, where a
    kind has exactly one handler. Promote it to an engine built-in (beside
    InstallerFileHandler) if a second component needs these kinds.
    """

    name = "harness-manager-install"
    kinds = _INSTALL_READ_ONLY_KINDS | _INSTALL_MUTATING_KINDS

    def __init__(self):
        self.state = {"seats": {}}

    def add_seat(self, account, *, facts_default=True):
        self.state["seats"][account] = {"slots": {}, "facts": {}, "facts_default": facts_default}

    def _seat(self, account):
        seat = self.state["seats"].get(account)
        if seat is None:
            raise KeyError(f"no simulated install seat {account!r}")
        return seat

    @staticmethod
    def _result(op):
        return {"kind": op.kind, "target": op.target, "desired": dict(op.desired)}

    def _fact(self, seat, op):
        return seat["facts"].get(op.target, seat["facts_default"]) is True

    # --- EffectHandler ---------------------------------------------------------

    def observe(self, component, scope):
        if not scope.startswith(_SEAT_PREFIX):
            return {"account": None, "install": None}
        account = scope[len(_SEAT_PREFIX):]
        return {"account": account, "install": copy.deepcopy(self.state["seats"].get(account))}

    def preflight(self, op):
        try:
            seat = self._seat(op.owner)
        except KeyError as exc:
            return PreflightResult(False, self.name, str(exc))
        if op.kind in _INSTALL_READ_ONLY_KINDS:
            ok = self._fact(seat, op)
            return PreflightResult(ok, self.name, f"{op.kind} {'passes' if ok else 'fails'} on {op.target}")
        current = seat["slots"].get(op.operation_id)
        ok = current is None or current == self._result(op)
        return PreflightResult(ok, self.name, "slot absent or already this op's result" if ok else "slot holds a foreign value")

    def rollback_intent(self, op):
        seat = self._seat(op.owner)
        before = {"slot": _canon(seat["slots"].get(op.operation_id))}
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, before, _fingerprint(self._result(op)))

    def execute(self, op):
        seat = self._seat(op.owner)
        if op.kind in _INSTALL_READ_ONLY_KINDS:
            mutation = MutationRecord(op.operation_id, op.kind, op.target, op.owner, False, {}, "")
            return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"simulated read {op.kind}")
        mutation = self.rollback_intent(op)
        seat["slots"][op.operation_id] = self._result(op)
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"simulated {op.kind}")

    def verify_operation(self, op):
        seat = self._seat(op.owner)
        if op.kind in _INSTALL_READ_ONLY_KINDS:
            ok = self._fact(seat, op)
        else:
            ok = seat["slots"].get(op.operation_id) == self._result(op)
        detail = f"{op.kind} verified" if ok else f"{op.kind} does not meet its postcondition"
        return OperationEvidence(op.operation_id, op.layer, op.kind, ok, detail)

    def can_rollback(self, mutation):
        try:
            seat = self._seat(mutation.owner)
        except KeyError as exc:
            return PreflightResult(False, "drifted", str(exc))
        current = seat["slots"].get(mutation.operation_id)
        if _canon(current) == mutation.before.get("slot"):
            return PreflightResult(True, "not-applied", "slot still at its preimage")
        if _fingerprint(current) == mutation.after_fingerprint:
            return PreflightResult(True, "owned", "slot holds this op's result")
        return PreflightResult(False, "drifted", "slot moved since this op")

    def rollback_mutation(self, mutation):
        seat = self._seat(mutation.owner)
        before = json.loads(mutation.before["slot"])
        if before is None:
            seat["slots"].pop(mutation.operation_id, None)
        else:
            seat["slots"][mutation.operation_id] = before

    def dump_state(self):
        return copy.deepcopy(self.state)

    def load_state(self, state):
        self.state = {"seats": copy.deepcopy(dict(state.get("seats") or {}))}


#: T-5830: the sub-op (a) kinds, read from the ONE OP_KINDS block above.
_SUPERVISOR_KINDS = frozenset(k for k, (klass, _p) in OP_KINDS.items() if klass == "harness.supervisor")
#: Ops whose target is a ``<account>:<slot>`` record, not a pinned unit / file.
_RECORD_TARGET_KINDS = frozenset({"harness.supervisor-commit", "harness.control-plane-teardown"})
_PINNED_FIELDS = (
    "backup_path", "service", "session_unit", "receiver_unit", "scope_unit", "slice_unit",
    "timer", "consume_service", "link_path", "dropin_path",
)


class HarnessSupervisorSim:
    """SIMULATED configure sub-op (a) (T-5830; CI tier, UNTESTED-LIVE, nothing actuates).

    State, per seat account::

        {"uid": int, "user_bus": bool, "pins": {"units": [...], "sessions": [...], "files": [...]},
         "units": {name: {"loaded", "enabled", "active"}}, "sessions": {name: True},
         "files": {path: {"kind": "file" | "symlink", "sha256": hex}}, "supervisor": str,
         "facts": {...post-activation probe results...}, plus the read-only plan inputs
         (bespoke_unit_text, target_manifest_sha256, seat_state, install_precondition,
         staged, manifest_loads) and the bookkeeping "writers" / "verdicts"}

    Each op rewrites a fixed set of CELLS (``units/<name>``, ``sessions/<name>``,
    ``files/<path>``, ``supervisor``); the value it leaves is a function of the op and
    the state at its write-ahead intent. The inverse is the cells' JSON before the op;
    ``can_rollback`` tells not-applied / owned / drifted (refused).

    Binding conditions, each enforced at preflight (Recovery D15 (T-5830)):
    (h) the target and every unit / file / session the op names must be in the seat's
        resolver-derived ``pins``; (i) ``systemd_manager`` is ``user``, the account is
        the op owner and ``xdg_runtime_dir`` is ``/run/user/<the seat's uid>``; (j) every
        tmux target is the exact ``=<session>`` form (a bare target refuses); (k) a
        file op refuses a symlinked path or backup and records an atomic no-follow
        write; (m) the require-quiesced and reinstate gates are preconditions.

    sv-retire's undo is COMPOSITE (mason ruling, review F1; Recovery D15 (m)): its write-ahead
    record carries the seat's control-plane targets, and the undo tears them down (the
    exact ``=<session>``, the seat's one codex receiver, the runtime scope; the consume
    slice soft), PROVES them gone, and only then reinstates bespoke. It needs no
    activation inverse to run first (it tears down whatever activation left), though
    the engine's newest-first order runs those inverses before it anyway. Whether a stop
    takes is learnt AT ATTEMPT TIME (the SIM fault ``teardown_stuck``): a failure after
    anything was torn down raises EscalatingRollbackStop ("control plane not torn down:
    <which> ..."), bespoke is NOT started, and the engine writes the durable
    ``rollback_incomplete.escalations`` marker and ONE ``escalation.page`` to recipient
    "recovery" (Recovery D15 (n); recorded, not delivered). A failure before anything was torn
    down is a plain OperationFailed (the control plane still supervises the seat).
    Recovery: clear the blocker, re-run ``rollback(run_id)`` (ROLLBACK_INCOMPLETE is
    rollback-admissible, ORG-080 D-Y).

    The commit's checks read ``facts``: the installer's activation runs in
    HarnessInstallSim, which this handler cannot see, so a test seeds the probe
    results the live activation would produce (the GF1 readiness stand-in pattern).
    Engine ``verify`` re-checks receipted ops: an op whose cells a LATER op of the same
    run rewrote (a companion stopped, then started) reports its execution-time verdict.
    """

    name = "harness-manager-supervisor"
    kinds = _SUPERVISOR_KINDS

    def __init__(self):
        self.state = {"seats": {}}

    def _seat(self, account):
        seat = self.state["seats"].get(account)
        if seat is None:
            raise KeyError(f"no simulated supervisor seat {account!r}")
        return seat

    # --- cells -----------------------------------------------------------------

    @staticmethod
    def _get(seat, key):
        area, _, name = key.partition("/")
        if area == "supervisor":
            return seat.get("supervisor")
        value = (seat.get(area) or {}).get(name)
        return copy.deepcopy(value)

    @staticmethod
    def _set(seat, key, value):
        area, _, name = key.partition("/")
        if area == "supervisor":
            seat["supervisor"] = value
            return
        bucket = seat.setdefault(area, {})
        if value is None:
            bucket.pop(name, None)
            if area == "companion_prestate" and not bucket:
                seat.pop(area)  # T-6047: a rolled-back record leaves no empty bucket (preimage equality)
        else:
            bucket[name] = copy.deepcopy(value)

    @staticmethod
    def _sessions(op):
        return [S.session_of(t) for t in op.desired.get("tmux_targets", "").split(",") if t]

    def _effects(self, op, seat):
        """The cell values ``op`` leaves, from the current state (a pure function)."""

        d = op.desired
        get = lambda key: self._get(seat, key)  # noqa: E731
        if op.kind == "harness.supervisor-file":
            src, dst = (op.target, d["backup_path"]) if d["action"] == "snapshot" else (d["backup_path"], op.target)
            return {f"files/{dst}": get(f"files/{src}")}
        if op.kind == "harness.supervisor-unit":
            if d["action"] == "require-quiesced":
                return {}
            unit, [session] = d["service"], self._sessions(op)
            on = d["action"] == "reinstate"
            return {f"units/{unit}": {"loaded": True, "enabled": on, "active": on}, f"sessions/{session}": True if on else None}
        if op.kind == "harness.companion-unit":
            unit, [session] = d["session_unit"], self._sessions(op)
            on = d["action"] == "start"
            current = get(f"units/{unit}") or {"loaded": True, "enabled": True}
            out = {f"units/{unit}": dict(current, active=on), f"sessions/{session}": True if on else None}
            snap = d.get("companion_snapshot")
            if snap:  # T-6047: first record per unit wins (companion_snapshot_write); a later stop never overwrites
                key = f"companion_prestate/{snap}"
                recorded = get(key) or {}
                if unit not in recorded:
                    recorded[unit] = "active" if current.get("active") is True else "inactive"
                out[key] = recorded
            return out
        if op.kind == "harness.control-plane-teardown":
            out = {f"sessions/{s}": None for s in self._sessions(op)}
            for field in ("receiver_unit", "scope_unit", "slice_unit"):
                current = get(f"units/{d[field]}")
                out[f"units/{d[field]}"] = None if current is None else dict(current, active=False)
            return out
        if op.kind == "harness.consume-retire":
            return {f"units/{d['timer']}": None, f"units/{d['consume_service']}": None,
                    f"files/{d['link_path']}": None, f"files/{d['dropin_path']}": None}
        return {"supervisor": d["supervisor"]}  # harness.supervisor-commit

    def _cells(self, op, seat):
        return sorted(self._effects(op, seat))

    # --- preconditions ---------------------------------------------------------

    @staticmethod
    def _gate_refusal(pre, units, live, files):
        """(m) the scripts' ordering gates, as explicit preconditions (None = open)."""

        for target in filter(None, pre.get("sessions_absent", "").split(",")):
            if live.get(S.session_of(target)) is True:
                return f"gate: session {target} is still present"
        for unit in filter(None, pre.get("units_inactive", "").split(",")):
            if (units.get(unit) or {}).get("active") is True:
                return f"gate: {unit} is still active"
        for unit in filter(None, pre.get("units_not_found", "").split(",")):
            if unit in units:
                return f"gate: {unit} is still loaded"
        for path in filter(None, pre.get("files_absent", "").split(",")):
            if path in files:
                return f"gate: {path} still exists"
        return None

    def _refusal(self, op, seat):
        """Why ``op`` may not run on ``seat`` (None = admissible). Recovery D15 (T-5830)."""

        d, pre = op.desired, op.precondition
        # (i) the seat's OWN user manager only
        if d.get("systemd_manager") != S.SYSTEMD_MANAGER or d.get("account") != op.owner:
            return "not addressed to the seat's own user manager"
        if d.get("xdg_runtime_dir") != S.user_runtime_dir(seat.get("uid", -1)):
            return f"xdg_runtime_dir {d.get('xdg_runtime_dir')!r} is not the seat's own user runtime dir"
        if seat.get("user_bus") is not True:
            return "the seat's user bus is unreachable"
        # (j) exact tmux targets only
        try:
            sessions = self._sessions(op)
        except ValueError as exc:
            return str(exc)
        # (h) resolver-derived pins only
        pins = seat.get("pins") or {}
        named = [] if op.kind in _RECORD_TARGET_KINDS else [op.target]
        named += [d[f] for f in _PINNED_FIELDS if f in d]
        foreign = [n for n in named if n not in set(pins.get("units", ())) | set(pins.get("files", ()))]
        foreign += [s for s in sessions if s not in set(pins.get("sessions", ()))]
        if foreign:
            return f"names {foreign}, outside the resolver-derived pins"
        files = seat.get("files") or {}
        units = seat.get("units") or {}
        live = seat.get("sessions") or {}
        # (k) and (m) run BEFORE the re-entry short-circuit (review F3): an op already "at its
        # result" never skips the symlink refusal or an ordering gate
        if op.kind == "harness.supervisor-file":
            for path in (op.target, d["backup_path"]):
                if (files.get(path) or {}).get("kind") == "symlink":
                    return f"{path} is a symlink; refusing to follow it"
            if d.get("write") != "atomic-no-follow":
                return "a supervisor file write must be atomic-no-follow"
        if op.kind == "harness.supervisor-unit" and d["action"] in ("require-quiesced", "reinstate"):
            gate = self._gate_refusal(pre, units, live, files)
            if gate:
                return gate
        effects = self._effects(op, seat)
        if effects and all(_canon(self._get(seat, k)) == _canon(v) for k, v in effects.items()):
            return None  # idempotent re-entry: already this op's result
        if op.kind == "harness.supervisor-file":
            src, dst = (op.target, d["backup_path"]) if d["action"] == "snapshot" else (d["backup_path"], op.target)
            want_src = pre["path_sha256"] if d["action"] == "snapshot" else pre["backup_sha256"]
            want_dst = pre["backup_sha256"] if d["action"] == "snapshot" else pre["path_sha256"]
            if (files.get(src) or {}).get("sha256") != want_src or (files.get(src) or {}).get("kind") != "file":
                return f"{src} is not the planned regular file"
            current = (files.get(dst) or {}).get("sha256", "absent") if files.get(dst) else "absent"
            if current != want_dst:
                return f"{dst} moved from its precondition"
            return None
        if op.kind == "harness.supervisor-unit" and d["action"] in ("require-quiesced", "reinstate"):
            return None  # its gates ran above
        if op.kind in ("harness.supervisor-unit", "harness.companion-unit"):
            unit = units.get(d.get("service") or d.get("session_unit")) or {}
            if op.kind == "harness.companion-unit" and d["action"] == "stop" and unit.get("active") is not True:
                return None  # T-6047: stopping an already-inactive companion is a no-op (systemctl stop is idempotent)
            for key in ("enabled", "active"):
                if key in pre and unit.get(key) is not (pre[key] == "true"):
                    return f"{key} moved from its precondition"
            return None
        if op.kind == "harness.supervisor-commit" and seat.get("supervisor") != pre["supervisor"]:
            return f"supervisor is {seat.get('supervisor')!r}, not {pre['supervisor']!r}"
        return None

    # --- EffectHandler ---------------------------------------------------------

    def observe(self, component, scope):
        if not scope.startswith(_SEAT_PREFIX):
            return {"account": None, "seat": None}
        account = scope[len(_SEAT_PREFIX):]
        seat = self.state["seats"].get(account)
        return {"account": account, "seat": copy.deepcopy(seat)}

    def preflight(self, op):
        try:
            seat = self._seat(op.owner)
            refusal = self._refusal(op, seat)
        except (KeyError, ValueError) as exc:
            return PreflightResult(False, self.name, str(exc))
        return PreflightResult(refusal is None, self.name, refusal or f"{op.kind} admissible")

    def rollback_intent(self, op):
        seat = self._seat(op.owner)
        effects = self._effects(op, seat)
        writers = seat.get("writers") or {}
        before = {
            "cells": _canon({k: self._get(seat, k) for k in effects}),
            "writers": _canon({k: writers.get(k) for k in effects}),
            "verdict": _canon((seat.get("verdicts") or {}).get(op.operation_id)),
        }
        if op.desired.get("undo") == "teardown-then-reinstate":
            # mason ruling (F1): the write-ahead inverse carries its own teardown plan, so the
            # undo needs nothing but this record (targets pinned in the op, Recovery (h)/(i)/(j)/(l))
            [session] = self._sessions(op)
            before["undo"] = _canon({
                "service": op.desired["service"],
                "session": session,
                "receiver_unit": op.desired["receiver_unit"],
                "scope_unit": op.desired["scope_unit"],
                "slice_unit": op.desired["slice_unit"],
                "service_result": effects[f"units/{op.desired['service']}"],
                "recipient": op.desired["escalation_recipient"],
            })
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, bool(effects), before, _fingerprint(effects))

    # --- the composite undo of sv-retire (mason ruling F1, Recovery D15 (m) / (n)) --------

    @staticmethod
    def _control_plane_live(seat, undo):
        """Which of this seat's control-plane pieces are still up (the hard teardown set)."""

        live = []
        if (seat.get("sessions") or {}).get(undo["session"]) is True:
            live.append(f"session ={undo['session']}")
        for field in ("receiver_unit", "scope_unit"):
            if ((seat.get("units") or {}).get(undo[field]) or {}).get("active") is True:
                live.append(undo[field])
        return live

    def _composite_can_rollback(self, seat, mutation, undo):
        """Predicts only not-applied / drifted / owned. Whether the teardown will take is
        learnt at ATTEMPT time (review R2-1: a live handler cannot know a stop will fail
        until it tries), so prediction and attempt can never disagree about a stop."""

        cells = json.loads(mutation.before["cells"])
        service_key = f"units/{undo['service']}"
        if _canon({k: self._get(seat, k) for k in cells}) == mutation.before["cells"]:
            return PreflightResult(True, "not-applied", "bespoke supervisor still at its preimage")
        if _canon(self._get(seat, service_key)) != _canon(undo["service_result"]):
            return PreflightResult(False, "drifted", f"{undo['service']} moved since the retire")
        return PreflightResult(True, "owned", "composite undo: tear down the control plane, then reinstate bespoke")

    def _composite_teardown(self, seat, mutation, undo):
        """Tear down THIS seat's control plane, in order: the exact session, its one codex
        receiver, the runtime scope (all hard), then the consume slice (soft). A piece named
        in the SIM fault ``teardown_stuck`` ("session" / "receiver" / "scope" / "slice")
        refuses its stop AT ATTEMPT TIME.

        A hard failure that leaves the seat with NO supervisor (anything torn down in this
        attempt, or the control-plane session already gone from an earlier one, while
        bespoke is retired): EscalatingRollbackStop, so the engine marks it durably and
        pages once per run (Recovery D15 (T-5830) (n); review R2-1 (c)). Only a failure while
        the control plane still supervises the seat (session live, nothing torn) is a
        plain OperationFailed."""

        # DECISION (review R3-3, P6): session -> receiver -> scope -> slice, while
        # persona_rollback.sh runs scope (:301), receiver (:306), slice (:317), session
        # (:324). Deliberately NOT reordered: the escalation outcome is equivalent (any
        # hard failure after a teardown escalates), and the proof below enforces Recovery (m)
        # (nothing of the control plane live) before bespoke can start, whatever the order.
        stuck = set(seat.get("teardown_stuck") or ())
        # judged on the SEAT, not on this attempt: after an earlier attempt killed the
        # session, a retry that tears nothing new still leaves the seat unsupervised
        supervised = self._get(seat, f"sessions/{undo['session']}") is True
        steps = (
            ("session", "sessions", undo["session"], f"session ={undo['session']}", True),
            ("receiver", "units", undo["receiver_unit"], undo["receiver_unit"], True),
            ("scope", "units", undo["scope_unit"], undo["scope_unit"], True),
            ("slice", "units", undo["slice_unit"], undo["slice_unit"], False),
        )
        torn = []
        for name, area, target, label, hard in steps:
            current = self._get(seat, f"{area}/{target}")
            live = current is True if area == "sessions" else isinstance(current, dict) and current.get("active") is True
            if not live:
                continue
            if name in stuck:
                if not hard:
                    continue  # the slice stop is soft (persona_rollback.sh:317-318): recorded, not asserted
                reason = f"control plane not torn down: {label}"
                if torn or not supervised:
                    gone = ", ".join(torn) if torn else "the control-plane session (earlier attempt)"
                    raise EscalatingRollbackStop(
                        f"{reason} (already torn down: {gone}); {undo['service']} NOT started, "
                        f"seat {mutation.owner} has no supervisor",
                        recipient=undo["recipient"], subject=mutation.owner,
                    )
                raise OperationFailed(f"{reason} (nothing torn down; the control plane still supervises the seat)")
            self._set(seat, f"{area}/{target}", None if area == "sessions" else dict(current, active=False))
            torn.append(label)
        # the proof (hard): never restart bespoke over a live control plane
        live = self._control_plane_live(seat, undo)
        if live:
            raise EscalatingRollbackStop(
                f"control plane not torn down: {', '.join(live)}; {undo['service']} NOT started",
                recipient=undo["recipient"], subject=mutation.owner,
            )

    def _view(self, seat):
        return {k: seat.get(k) for k in ("units", "sessions", "files", "facts")}

    def _evaluate(self, op, seat):
        try:
            checks = S.decode(op.desired.get("postconditions", "[]"))
        except ValueError as exc:
            return False, f"malformed postconditions: {exc}"
        return S.verdict(S.evaluate(checks, self._view(seat)))

    def execute(self, op):
        mutation = self.rollback_intent(op)
        seat = self._seat(op.owner)
        effects = self._effects(op, seat)
        for key, value in effects.items():
            self._set(seat, key, value)
            seat.setdefault("writers", {})[key] = op.operation_id
        seat.setdefault("verdicts", {})[op.operation_id] = list(self._evaluate(op, seat))
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"simulated {op.kind} {op.desired.get('action', '')}".strip())

    def verify_operation(self, op):
        seat = self._seat(op.owner)
        writers = seat.get("writers") or {}
        cells = self._cells(op, seat)
        if op.kind == "harness.supervisor-commit" or all(writers.get(k) == op.operation_id for k in cells):
            ok, detail = self._evaluate(op, seat)
        else:
            recorded = (seat.get("verdicts") or {}).get(op.operation_id)
            if not (isinstance(recorded, list) and len(recorded) == 2):
                return OperationEvidence(op.operation_id, op.layer, op.kind, False, "superseded with no execution-time verdict")
            ok, detail = recorded[0] is True, f"superseded by a later op; execution-time verdict: {recorded[1]}"
        return OperationEvidence(op.operation_id, op.layer, op.kind, ok, detail)

    def can_rollback(self, mutation):
        try:
            seat = self._seat(mutation.owner)
            cells = json.loads(mutation.before["cells"])
        except (KeyError, ValueError) as exc:
            return PreflightResult(False, "drifted", str(exc))
        if "undo" in mutation.before:
            return self._composite_can_rollback(seat, mutation, json.loads(mutation.before["undo"]))
        current = {k: self._get(seat, k) for k in cells}
        if _canon(current) == mutation.before["cells"]:
            return PreflightResult(True, "not-applied", "cells still at their preimage")
        if _fingerprint(current) == mutation.after_fingerprint:
            return PreflightResult(True, "owned", "cells hold this op's result")
        return PreflightResult(False, "drifted", "cells moved since this op")

    def rollback_mutation(self, mutation):
        seat = self._seat(mutation.owner)
        if "undo" in mutation.before:
            # 1-2. tear down THIS seat's control plane and prove it, or stop typed
            self._composite_teardown(seat, mutation, json.loads(mutation.before["undo"]))
            # 3. only then reinstate bespoke (the Recovery (m) gate holds by the proof), below
        for key, value in json.loads(mutation.before["cells"]).items():
            self._set(seat, key, value)
        writers = seat.setdefault("writers", {})
        for key, value in json.loads(mutation.before["writers"]).items():
            if value is None:
                writers.pop(key, None)
            else:
                writers[key] = value
        verdict = json.loads(mutation.before["verdict"])
        if verdict is None:
            seat.setdefault("verdicts", {}).pop(mutation.operation_id, None)
        else:
            seat.setdefault("verdicts", {})[mutation.operation_id] = verdict

    def dump_state(self):
        return copy.deepcopy(self.state)

    def load_state(self, state):
        self.state = {"seats": copy.deepcopy(dict(state.get("seats") or {}))}


#: T-5858: the run_state kinds, read from the ONE OP_KINDS block above.
_RUN_STATE_KINDS = frozenset(k for k, (klass, _p) in OP_KINDS.items() if klass == "harness.run-state")
_RS_RECORD_KINDS = frozenset({"harness.run-state-drain", "harness.run-state-reset-failed", "harness.run-state-commit"})
#: What an enabled timer reports as its next elapse in the sim (a seat fault overrides it).
_RS_NEXT_ELAPSE = "Mon 2026-11-02 05:00:00 UTC"


class HarnessRunStateSim:
    """SIMULATED configure run_state (T-5858; CI tier, UNTESTED-LIVE, nothing actuates).

    State, per seat account::

        {"uid": int, "user_bus": bool, "pins": {"units": [...], "files": [...]},
         "units": {name: {"LoadState", "ActiveState", "SubState", "UnitFileState",
                          "NextElapseUSecRealtime"?}},
         "manifest": doc | None, "seat_state": record | None, "seat_state_tombstones": [...],
         "run_state": str | None, "target_manifest_sha256": hex,
         plus the SIM faults "unit_stuck" ({unit: "disable-now" | "enable-now"}, learnt at
         ATTEMPT time), "next_elapse" ({timer: value} on enable) and "fails_during"
         ({op_id: [unit, ...]}: a unit the op does not own fails while it runs), and the bookkeeping
         "writers" / "verdicts"}

    Each op rewrites a fixed set of CELLS (``units/<name>``, ``manifest``, ``seat_state``,
    ``seat_state_tombstones``, ``run_state``); the value it leaves is a function of the op
    and the state at its write-ahead intent. The inverse is the cells' JSON before the op;
    ``can_rollback`` tells not-applied / owned / drifted (refused).

    Binding conditions, enforced at preflight: the seat's OWN user manager only (Recovery D15
    (T-5858) / U4 (i)); every unit / file an op names is a data-derived pin (U4 (h)); a
    unit op's observed fields equal its precondition, read as exact systemd property
    values (SOP §4). The drain (Recovery (r)) is the bounded wait, learnt at ATTEMPT time: a
    oneshot still in flight times it out and the pause is ABORTED (nothing is stopped or
    killed); ``rollback(run_id)`` re-enables the timers already disabled and removes the
    manifest, newest first. The drain op declares ``rollback_on_halt`` (T-5866), so the
    engine runs that rollback at once; the seat-state record and manifest are only removed
    once every manifest unit is back (``rollback_mutation``'s guard, Recovery (r1)).

    Recovery D15 (T-5858) (r1): ANY halt of a pause after its ORG-106 record is written (drain
    timeout, a unit that will not disable, an unmet apply-time postcondition) is the
    darkness case. The paused-intake record is KEPT (never cleared) and replaced in one write
    by the ORG-106 halted record (T-5868 Phase B, ``on_halted``); a restore refuses while it is
    present, and ``rollback(run_id)`` removes it with the rest of the pause. The halt
    raises EscalatingHalt, so the engine writes ``halted.escalations`` and ONE page naming
    run_id and the rollback command. The monitors read the halted record as an incident
    (notes s10). A restore that halts leaves the record untouched (the
    seat is still paused, and authorised).

    The ORG-106 seat-state write and clear (Recovery (p)) are modelled as typed ops on the
    record cell in the L1 writer's own record shape (``tools/harness.py``
    ``_do_pause_unlocked`` / ``_do_wake``: the clear appends a tombstone first). The live
    writer is never called from here.

    Rollback could leave a seat DARK when re-enabling a unit fails (SIM fault ``unit_stuck``
    on enable-now) and NO wake timer of the seat is then active. The engine undoes newest
    first and carries on past a stop, so darkness is decided ONLY at the last-undone wake
    timer, the pause's first timer op (the planner marks it ``undo_decides_dark``), after
    every other timer undo was attempted (review R1-1): if its re-enable fails and no wake
    timer is active, it raises EscalatingRollbackStop to recipient "recovery" (Recovery D15
    (T-5830) (n), reused unchanged: the durable marker and ONE page per (run, op), so one
    per dark seat, retries included). Every other inverse failure is a plain
    OperationFailed (the engine records ROLLBACK_INCOMPLETE; re-run rollback).
    """

    name = "harness-manager-run-state"
    kinds = _RUN_STATE_KINDS

    def __init__(self):
        self.state = {"seats": {}}
        self._just_executed = None  # in-process only: which op's apply-time verify comes next

    def _seat(self, account):
        seat = self.state["seats"].get(account)
        if seat is None:
            raise KeyError(f"no simulated run-state seat {account!r}")
        return seat

    # --- cells -----------------------------------------------------------------

    @staticmethod
    def _get(seat, key):
        area, _, name = key.partition("/")
        if not name:
            return copy.deepcopy(seat.get(area))
        return copy.deepcopy((seat.get(area) or {}).get(name))

    @staticmethod
    def _set(seat, key, value):
        area, _, name = key.partition("/")
        if not name:
            seat[area] = copy.deepcopy(value)
            return
        bucket = seat.setdefault(area, {})
        if value is None:
            bucket.pop(name, None)
        else:
            bucket[name] = copy.deepcopy(value)

    @staticmethod
    def _csv(value):
        return [v for v in (value or "").split(",") if v]

    def _effects(self, op, seat):
        """The cell values ``op`` leaves, from the current state (a pure function)."""

        d = op.desired
        if op.kind == "harness.run-state-manifest":
            return {"manifest": json.loads(d["document"])}
        if op.kind == "harness.run-state-seat-state":
            if d["action"] == "set":
                return {"seat_state": json.loads(d["document"])}
            current = seat.get("seat_state")
            tombstones = list(seat.get("seat_state_tombstones") or [])
            if current is None:
                return {"seat_state": None}
            return {"seat_state": None, "seat_state_tombstones": tombstones + [json.loads(d["tombstone"])]}
        if op.kind == "harness.run-state-unit":
            unit = d["unit"]
            current = self._get(seat, f"units/{unit}") or {"LoadState": "loaded"}
            timer = unit.endswith(".timer")
            if d["action"] == "disable-now":
                after = dict(current, UnitFileState="disabled", ActiveState="inactive", SubState="dead")
                if timer:
                    after["NextElapseUSecRealtime"] = "n/a"
            else:
                after = dict(current, UnitFileState="enabled", ActiveState="active", SubState="waiting" if timer else "running")
                if timer:
                    after["NextElapseUSecRealtime"] = (seat.get("next_elapse") or {}).get(unit, _RS_NEXT_ELAPSE)
            return {f"units/{unit}": after}
        if op.kind == "harness.run-state-drain":
            return {}
        if op.kind == "harness.run-state-reset-failed":
            out = {}
            for unit in self._csv(d["units"]):
                current = self._get(seat, f"units/{unit}")
                if isinstance(current, dict) and current.get("ActiveState") == "failed":
                    out[f"units/{unit}"] = dict(current, ActiveState="inactive", SubState="dead")
            return out
        return {"run_state": d["run_state"]}  # harness.run-state-commit

    def _cells(self, op, seat):
        return sorted(self._effects(op, seat))

    # --- preconditions ---------------------------------------------------------

    def _refusal(self, op, seat):
        """Why ``op`` may not run on ``seat`` (None = admissible)."""

        d, pre = op.desired, op.precondition
        if d.get("systemd_manager") != RS.SYSTEMD_MANAGER or d.get("account") != op.owner:
            return "not addressed to the seat's own user manager"
        if d.get("xdg_runtime_dir") != S.user_runtime_dir(seat.get("uid", -1)):
            return f"xdg_runtime_dir {d.get('xdg_runtime_dir')!r} is not the seat's own user runtime dir"
        if seat.get("user_bus") is not True:
            return "the seat's user bus is unreachable"
        pins = seat.get("pins") or {}
        named = [] if op.kind in _RS_RECORD_KINDS else [op.target]
        named += [d["unit"]] if "unit" in d else []
        named += self._csv(d.get("units")) + self._csv(d.get("oneshots"))
        foreign = [n for n in named if n not in set(pins.get("units", ())) | set(pins.get("files", ()))]
        if foreign:
            return f"names {foreign}, outside the data-derived pins"
        units = seat.get("units") or {}
        if op.kind == "harness.run-state-drain":
            return None  # Recovery (r): the bounded wait is learnt at ATTEMPT time (execute), never at preflight
        effects = self._effects(op, seat)
        if effects and all(_canon(self._get(seat, k)) == _canon(v) for k, v in effects.items()):
            return None  # idempotent re-entry: already this op's result
        if op.kind == "harness.run-state-unit":
            unit = units.get(d["unit"])
            fields = {k: unit.get(k) for k in ("UnitFileState", "ActiveState")} if isinstance(unit, dict) else None
            want = {k: pre[k] for k in ("UnitFileState", "ActiveState")}
            if fields != want:
                return f"{d['unit']} is {fields}, moved from its precondition {want}"
            return None
        if op.kind in ("harness.run-state-manifest", "harness.run-state-seat-state"):
            cell = "manifest" if op.kind == "harness.run-state-manifest" else "seat_state"
            current = seat.get(cell)
            sha = "absent" if current is None else RS.digest(current)
            want = pre[f"{cell}_sha256"]
            if sha != want:
                return f"{cell} sha256 {sha[:12]} moved from its precondition {want[:12]}"
            return None
        if op.kind == "harness.run-state-commit" and (seat.get("run_state") or pre["run_state"]) != pre["run_state"]:
            return f"run_state is {seat.get('run_state')!r}, not {pre['run_state']!r}"
        return None

    # --- EffectHandler ---------------------------------------------------------

    def observe(self, component, scope):
        if not scope.startswith(_SEAT_PREFIX):
            return {"account": None, "seat": None}
        account = scope[len(_SEAT_PREFIX):]
        seat = self.state["seats"].get(account)
        return {"account": account, "seat": copy.deepcopy(seat)}

    def preflight(self, op):
        try:
            seat = self._seat(op.owner)
            refusal = self._refusal(op, seat)
        except (KeyError, ValueError) as exc:
            return PreflightResult(False, self.name, str(exc))
        return PreflightResult(refusal is None, self.name, refusal or f"{op.kind} admissible")

    def rollback_intent(self, op):
        seat = self._seat(op.owner)
        effects = self._effects(op, seat)
        writers = seat.get("writers") or {}
        before = {
            "cells": _canon({k: self._get(seat, k) for k in effects}),
            "writers": _canon({k: writers.get(k) for k in effects}),
            "verdict": _canon((seat.get("verdicts") or {}).get(op.operation_id)),
        }
        if op.kind == "harness.run-state-unit":
            # the undo is the opposite action on this one unit, learnt to fail at attempt time
            before["undo"] = _canon({"unit": op.desired["unit"],
                                     "action": "enable-now" if op.desired["action"] == "disable-now" else "disable-now",
                                     "recipient": RS.ESCALATION_RECIPIENT,
                                     "decides_dark": op.desired.get("undo_decides_dark") == "true"})
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, bool(effects), before, _fingerprint(effects))

    def _view(self, seat):
        return {k: seat.get(k) for k in ("units", "manifest", "seat_state", "run_state")}

    def _evaluate(self, op, seat):
        try:
            checks = RS.decode(op.desired.get("postconditions", "[]"))
        except ValueError as exc:
            return False, f"malformed postconditions: {exc}"
        return RS.verdict(RS.evaluate(checks, self._view(seat)))

    def _halted_pause(self, op, seat):
        """True when ``op`` belongs to a PAUSE run whose ORG-106 record this run already wrote:
        a halt there is Recovery D15 (T-5858) (r1)'s darkness case. Review R2 F1: the run is read
        from the planner's explicit ``provenance.run`` (``pause`` | ``restore``), never
        inferred from ``run_state`` (a partial restore also commits run_state "paused")."""

        provenance = getattr(op, "provenance", None) or {}
        pause_id = provenance.get("pause_id")
        record = seat.get("seat_state")
        return (provenance.get("run") == "pause" and bool(pause_id) and isinstance(record, dict)
                and record.get("pause_id") == pause_id
                and op.kind not in ("harness.run-state-manifest", "harness.run-state-seat-state"))

    def _halt(self, op, seat, reason):
        """Fail ``op``. On a halted pause (Recovery D15 (T-5858) (r1)) the paused-intake record is KEPT, never
        cleared (a cleared record would let a later restore take the running-seat no-op path), and the halt
        escalates: EscalatingHalt -> the engine's durable ``halted.escalations`` marker and a page naming
        run_id and the rollback, to recovery and the record's owner (T-5868 Q3). The record becomes the ORG-106
        halted one in :meth:`on_halted`, which the engine calls with the run_id this interface lacks."""

        if not self._halted_pause(op, seat):
            raise OperationFailed(reason)
        record = seat["seat_state"]
        owner = RS.owner_recipient(record.get("owner"))
        raise EscalatingHalt(
            f"{reason}; pause {record['pause_id']} HALTED at {op.operation_id} with wake paths disabled: the seat-state "
            "record becomes halted (a restore refuses); roll the run back to re-enable them",
            recipient=RS.ESCALATION_RECIPIENT, subject=op.owner, recipients=(owner,) if owner else (),
        )

    def on_halted(self, run_id, op, exc):
        """T-5868 Phase B: the single atomic replace, paused-intake -> halted, with every notes-s10 key
        (the L1 writer's own validation gates it: an invalid record is refused and raises, so the engine
        pages that the halt was not recorded). Only a halted pause's own record is touched."""

        seat = self._seat(op.owner)
        if not self._halted_pause(op, seat):
            return
        from tools import seat_state
        from tools.germinate.engine.run_state import now_iso

        halted = RS.halted_record(seat["seat_state"], run_id=run_id, op_id=op.operation_id, halted_at=now_iso()[:19] + "Z")
        problems = seat_state.validate_record(halted)
        if problems:
            raise OperationFailed("refusing to write an invalid halted seat-state record: " + "; ".join(problems))
        seat["seat_state"] = halted

    def execute(self, op):
        mutation = self.rollback_intent(op)
        seat = self._seat(op.owner)
        self._just_executed = None
        refusal = self._refusal(op, seat)
        if refusal is not None:
            raise OperationFailed(refusal)
        if op.kind == "harness.run-state-drain":  # Recovery (r): the bounded wait; nothing is ever stopped
            busy = [u for u in self._csv(op.desired["oneshots"])
                    if ((seat.get("units") or {}).get(u) or {}).get("ActiveState") in RS.IN_FLIGHT]
            if busy:
                self._halt(op, seat, f"drain timeout after {op.desired.get('window_s')}s: {busy} still in flight; "
                                     "pause ABORTED (no oneshot is stopped or killed)")
        if op.kind == "harness.run-state-unit" and (seat.get("unit_stuck") or {}).get(op.desired["unit"]) == op.desired["action"]:
            self._halt(op, seat, f"{op.desired['action']} {op.desired['unit']} did not take (simulated)")
        effects = self._effects(op, seat)
        for key, value in effects.items():
            self._set(seat, key, value)
            seat.setdefault("writers", {})[key] = op.operation_id
        for unit in (seat.get("fails_during") or {}).get(op.operation_id, ()):
            # SIM fault: a unit this op does not own fails while it runs (not undone by rollback)
            seat.setdefault("units", {})[unit] = {"LoadState": "loaded", "ActiveState": "failed", "SubState": "failed",
                                                  "UnitFileState": "enabled"}
        seat.setdefault("verdicts", {})[op.operation_id] = list(self._evaluate(op, seat))
        self._just_executed = op.operation_id
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"simulated {op.kind} {op.desired.get('action', '')}".strip())

    def verify_operation(self, op):
        seat = self._seat(op.owner)
        writers = seat.get("writers") or {}
        cells = self._cells(op, seat)
        if op.kind == "harness.run-state-commit" or all(writers.get(k) == op.operation_id for k in cells):
            ok, detail = self._evaluate(op, seat)
        else:
            recorded = (seat.get("verdicts") or {}).get(op.operation_id)
            if not (isinstance(recorded, list) and len(recorded) == 2):
                return OperationEvidence(op.operation_id, op.layer, op.kind, False, "superseded with no execution-time verdict")
            ok, detail = recorded[0] is True, f"superseded by a later op; execution-time verdict: {recorded[1]}"
        just_executed, self._just_executed = getattr(self, "_just_executed", None) == op.operation_id, None
        if not ok and just_executed and self._halted_pause(op, seat):  # apply-time check on a pause (r1)
            self._halt(op, seat, f"postcondition unmet: {detail}")
        return OperationEvidence(op.operation_id, op.layer, op.kind, ok, detail)  # else the engine halts plain

    def can_rollback(self, mutation):
        try:
            seat = self._seat(mutation.owner)
            cells = json.loads(mutation.before["cells"])
        except (KeyError, ValueError) as exc:
            return PreflightResult(False, "drifted", str(exc))
        # Recovery (r1): a halt annotation on the seat-state record is this run's own; the record
        # is still the op's result, so the annotation does not read as drift
        current = {k: self._unannotated(k, self._get(seat, k)) for k in cells}
        if _canon(current) == mutation.before["cells"]:
            return PreflightResult(True, "not-applied", "cells still at their preimage")
        if _fingerprint(current) == mutation.after_fingerprint:
            return PreflightResult(True, "owned", "cells hold this op's result")
        return PreflightResult(False, "drifted", "cells moved since this op")

    @staticmethod
    def _unannotated(key, value):
        return RS.unhalted(value) if key == "seat_state" else value

    @staticmethod
    def _dark(seat):
        """No wake timer of the seat is active (exact ActiveState): it cannot wake."""

        return not [n for n, u in (seat.get("units") or {}).items()
                    if RS.is_wake_timer(n) and isinstance(u, dict) and u.get("ActiveState") == "active"]

    @staticmethod
    def _units_not_back(seat):
        """Manifest units (every entry's pre-state is enabled/active) not yet back at it."""

        manifest = seat.get("manifest")
        entries = manifest.get("entries") if isinstance(manifest, dict) else None
        units = seat.get("units") or {}
        return sorted(
            e["unit"] for e in entries or () if isinstance(e, dict) and isinstance(e.get("unit"), str)
            and {k: (units.get(e["unit"]) or {}).get(k) for k in RS.UP} != RS.UP
        )

    def rollback_mutation(self, mutation):
        seat = self._seat(mutation.owner)
        if mutation.kind in ("harness.run-state-manifest", "harness.run-state-seat-state"):
            cell = "manifest" if mutation.kind == "harness.run-state-manifest" else "seat_state"
            if json.loads(mutation.before["cells"]).get(cell, "keep") is None:
                # T-5866, Recovery D15 (T-5858) (r1) as tightened: undoing a pause never removes its
                # seat-state record (or the manifest) while a manifest unit is still down, so a
                # half-undone pause stays on record, annotated, and a restore keeps refusing.
                # Plain failure: the op stays outstanding and a retry clears it once all are back.
                down = self._units_not_back(seat)
                if down:
                    raise OperationFailed(f"kept the pause's {cell}: manifest units not yet restored: {down}")
        if "undo" in mutation.before:
            undo = json.loads(mutation.before["undo"])
            if (seat.get("unit_stuck") or {}).get(undo["unit"]) == undo["action"]:
                reason = f"rollback {undo['action']} {undo['unit']} did not take (simulated)"
                # review R1-1: the engine undoes newest first and carries on past a stop, so
                # darkness is decided only by the LAST-undone wake timer (the planner marks
                # it), once every other timer undo has been attempted; an earlier stuck timer
                # fails plain. One op decides, so one marker and one page per dark seat.
                if undo.get("decides_dark") and undo["action"] == "enable-now" and self._dark(seat):
                    raise EscalatingRollbackStop(
                        f"{reason}; every wake-timer undo has been attempted and no wake timer of seat "
                        f"{mutation.owner} is active: the seat is dark",
                        recipient=undo["recipient"], subject=mutation.owner,
                    )
                raise OperationFailed(reason)
        for key, value in json.loads(mutation.before["cells"]).items():
            self._set(seat, key, value)
        writers = seat.setdefault("writers", {})
        for key, value in json.loads(mutation.before["writers"]).items():
            if value is None:
                writers.pop(key, None)
            else:
                writers[key] = value
        verdict = json.loads(mutation.before["verdict"])
        if verdict is None:
            seat.setdefault("verdicts", {}).pop(mutation.operation_id, None)
        else:
            seat.setdefault("verdicts", {})[mutation.operation_id] = verdict

    def dump_state(self):
        return copy.deepcopy(self.state)

    def load_state(self, state):
        self.state = {"seats": copy.deepcopy(dict(state.get("seats") or {}))}


_DECOMMISSION_KINDS = frozenset(k for k, (klass, _p) in OP_KINDS.items() if klass == "harness.decommission")


def _record_vetoes(record, observed_at):
    """Recovery D15 (T-5859) (x3): ``tools.seat_state.parse_record`` (pure: no I/O) at the observed
    time, the same parse the planner uses (only an unexpired, valid deep-sleep record vetoes)."""

    from datetime import datetime, timezone

    from tools import seat_state

    now = datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return bool(seat_state.parse_record(record, now=now).remediation_vetoed)


class HarnessDecommissionSim:
    """SIMULATED remove leg, the full-seat decommission (T-5859; CI tier, UNTESTED-LIVE,
    nothing actuates; seat half only).

    State, per seat account::

        {"uid": int, "user_bus": bool, "target_manifest_sha256": hex,
         "units": {name: {"LoadState", "ActiveState", "SubState", "UnitFileState"}},
         "files": {abs path: {"kind": "file" | "dir" | "symlink", "sha256"?, "size"?, "document"?}},
         "sessions": {name: {"scope": unit}},
         "peer_units": {unit: {"account", "ActiveState", "ExecStart", "WorkingDirectory"}},
         plus the SIM faults "unit_stuck" ({unit: "disable-now" | "enable-now"}),
         "move_fail" ([source]: the rename fails at attempt time), "rename_back_fail"
         ([source]: the undo rename fails), "scope_survives" (the controller stop leaves
         its tmux scope and session up), and the bookkeeping "writers" / "verdicts"}

    Each op rewrites a fixed set of CELLS (``units/<name>``, ``files/<path>``,
    ``sessions/<name>``); the value it leaves is a function of the op and the state at its
    write-ahead intent. A quarantine MOVE re-keys the source subtree under its destination
    (a rename: no entry is dropped); the decommission manifest is a file entry carrying
    its document. The inverse is the cells' JSON before the op, with ONE exception that
    keeps the leg delete-free: undoing the manifest write never removes it, it marks it
    ``status: undone`` (so a retry needs a fresh stamp). ``can_rollback`` tells
    not-applied / owned / drifted (refused).

    Binding conditions, enforced at preflight (U4 (i), (h); Recovery D15 (T-5859)): the seat's
    OWN user manager only; every path an op names is inside the seat's own home; a unit op's
    observed fields equal its precondition (exact systemd property values); a move's source
    digest equals the planned one, its destination is absent, its source is no symlink, and
    no LIVE peer unit's ExecStart / WorkingDirectory resolves inside the source (Recovery D15
    (T-5859) (x), the apply-time half of the plan refusal); and the manifest write, the run's
    first op, re-checks Recovery D15 (T-5859) (x2): every Recovery-side unit targeting the persona
    (``peer_coverage``) must be observed disarmed (not enabled, not in flight) or absent
    (LoadState not-found AND inactive); an armed or unobserved one refuses before any mutation.
    Both also re-check Recovery D15 (T-5859) (x3): fleet remediation vetoed for the seat
    (``remediation``: seat-maintenance armed or deep-sleep, as seat_state.remediation_vetoed).
    The runtime controller stop re-checks (x2) once more (review R2-2), and a per-child move of
    the carved persona state root re-checks (x) against the root as well (review R2-3).

    Escalation (D-6, mason ruling): an op the planner marks ``escalate_on_halt`` (the
    runtime controller stop and every op after it) defers its preflight to the attempt, and
    any failure there, or an unmet apply-time postcondition, raises EscalatingHalt: the
    seat is half-removed and unsupervised, so the engine writes ``halted.escalations`` and
    pages ONE recipient ("recovery") with the rollback command. Rollback runs newest first; the
    controller's undo is COMPOSITE ("daemon-reload-then-enable", U4 F1 precedent): it
    re-enables the controller only once every quarantined source is back, and if one is
    not (a stopped rename-back) or the re-enable does not take, it raises
    EscalatingRollbackStop to "recovery" (the durable marker and ONE page). Every other inverse
    failure is a plain OperationFailed (the engine records ROLLBACK_INCOMPLETE).
    """

    name = "harness-manager-decommission"
    kinds = _DECOMMISSION_KINDS

    def __init__(self):
        self.state = {"seats": {}}
        self._just_executed = None

    def _seat(self, account):
        seat = self.state["seats"].get(account)
        if seat is None:
            raise KeyError(f"no simulated decommission seat {account!r}")
        return seat

    # --- cells -----------------------------------------------------------------

    @staticmethod
    def _get(seat, key):
        area, _, name = key.partition("/")
        return copy.deepcopy((seat.get(area) or {}).get(name))

    @staticmethod
    def _set(seat, key, value):
        area, _, name = key.partition("/")
        bucket = seat.setdefault(area, {})
        if value is None:
            bucket.pop(name, None)  # the cell is vacated (a rename's source, a stopped scope)
        else:
            bucket[name] = copy.deepcopy(value)

    @staticmethod
    def _unit_dir(account):
        return f"{R.home_of(account)}/.config/systemd/user"

    @staticmethod
    def _veto_refusal(seat, desired):
        """Recovery D15 (T-5859) (x3) at apply: an op marked ``remediation_veto: required`` needs
        fleet remediation vetoed for the seat (seat-maintenance armed or deep-sleep), observed."""

        if desired.get("remediation_veto") != "required":
            return None
        vetoed = R.remediation_vetoed(seat.get("remediation"), _record_vetoes)
        if vetoed is True:
            return None
        return ("remediation-not-vetoed: " + ("not observed (could not ask)" if vetoed is None else "not vetoed")
                + "; arm seat-maintenance for the seat (or deep-sleep) first")

    def _effects(self, op, seat):
        """The cell values ``op`` leaves, from the current state (a pure function)."""

        d = op.desired
        files = seat.get("files") or {}
        if op.kind == "harness.decommission-record":
            document = dict(json.loads(d["document"]), status=d["status"])
            raw = d["document"].encode("utf-8")
            entry = {"kind": "file", "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "document": document}
            return {f"files/{op.target}": entry}
        if op.kind == "harness.decommission-commit":
            entry = self._get(seat, f"files/{d['record_path']}")
            if not isinstance(entry, dict) or not isinstance(entry.get("document"), dict):
                return {}
            return {f"files/{d['record_path']}": dict(entry, document=dict(entry["document"], status=d["status"]))}
        if op.kind == "harness.decommission-move":
            out = {}
            for rel, entry in R.subtree(files, d["source"]).items():
                suffix = f"/{rel}" if rel else ""
                out[f"files/{d['source']}{suffix}"] = None
                out[f"files/{d['destination']}{suffix}"] = entry
            return out
        if d.get("action") == "daemon-reload":
            out = {}
            for name, unit in sorted((seat.get("units") or {}).items()):
                fragment = R.fragment_of(name, self._unit_dir(op.owner))
                if (isinstance(unit, dict) and unit.get("ActiveState") == "inactive"
                        and unit.get("LoadState") != "not-found" and not R.subtree(files, fragment)):
                    out[f"units/{name}"] = dict(unit, LoadState="not-found")
            return out
        unit = d["unit"]  # disable-now (+ reset-failed)
        current = self._get(seat, f"units/{unit}") or {"LoadState": "loaded"}
        out = {f"units/{unit}": dict(current, UnitFileState="disabled", ActiveState="inactive", SubState="dead")}
        if d.get("scope_unit") and not seat.get("scope_survives"):
            out[f"units/{d['scope_unit']}"] = None  # the controller's tmux scope ends with it
            if d.get("session"):
                out[f"sessions/{d['session']}"] = None
        return out

    # --- preconditions ---------------------------------------------------------

    def _refusal(self, op, seat):
        d, pre = op.desired, op.precondition
        if d.get("systemd_manager") != R.SYSTEMD_MANAGER or d.get("account") != op.owner:
            return "not addressed to the seat's own user manager"
        if d.get("xdg_runtime_dir") != S.user_runtime_dir(seat.get("uid", -1)):
            return f"xdg_runtime_dir {d.get('xdg_runtime_dir')!r} is not the seat's own user runtime dir"
        if seat.get("user_bus") is not True:
            return "the seat's user bus is unreachable"
        home = R.home_of(op.owner)
        paths = [p for p in (op.target, d.get("source"), d.get("destination"), d.get("record_path")) if isinstance(p, str) and p.startswith("/")]
        outside = [p for p in paths if not R.under(p, home) or p == home]
        if outside:
            return f"names {outside}, outside the seat's own home"
        effects = self._effects(op, seat)
        if effects and all(_canon(self._get(seat, k)) == _canon(v) for k, v in effects.items()):
            return None  # idempotent re-entry: already this op's result
        files = seat.get("files") or {}
        if op.kind == "harness.decommission-record":
            # Recovery D15 (T-5859) (x2) / (x3), at apply: re-checked here, before any mutation of the run
            coverage = [u for u in (d.get("peer_coverage") or "").split(",") if u]
            armed, unknown = R.peer_coverage_state(seat.get("peer_units"), coverage)
            if armed:
                return f"peer-coverage-armed: {'; '.join(armed)}"
            if unknown:
                return f"peer-coverage-could-not-ask: {', '.join(unknown)}"
            vetoed = self._veto_refusal(seat, d)
            if vetoed is not None:
                return vetoed
            return None if self._get(seat, f"files/{op.target}") is None else f"{op.target} already exists"
        if op.kind == "harness.decommission-commit":
            entry = self._get(seat, f"files/{d['record_path']}") or {}
            status = (entry.get("document") or {}).get("status")
            return None if status == pre["record_status"] else f"manifest status is {status!r}, not {pre['record_status']!r}"
        if op.kind == "harness.decommission-move":
            top = files.get(d["source"])
            if isinstance(top, dict) and top.get("kind") not in ("file", "dir"):
                return f"{d['source']} is a symlink or not a regular file/dir; refusing to follow it"
            got = R.tree_digest(files, d["source"])
            if got != pre["source_sha256"]:
                return f"{d['source']} digest {str(got)[:12]} moved from its precondition {pre['source_sha256'][:12]}"
            if R.subtree(files, d["destination"]):
                return f"destination {d['destination']} exists; nothing is overwritten"
            # review R2-3: a per-child move of the carved state root re-checks the root too
            # (minus the retained overrides/, review R2-4)
            refs = [d["source"]] + ([d["referenced_root"]] if d.get("referenced_root") else [])
            peer = R.peer_refs_inside(seat.get("peer_units"), refs,
                                      [d["referenced_exclude"]] if d.get("referenced_exclude") else [])
            if peer:  # Recovery D15 (T-5859) (x), at apply
                return f"live peer unit(s) resolve inside {d['source']}: {'; '.join(peer)}"
            return None
        if d.get("action") == "daemon-reload":
            return None
        if d.get("peer_coverage"):  # review R2-2: (x2) again on the controller stop
            armed, unknown = R.peer_coverage_state(seat.get("peer_units"), [u for u in d["peer_coverage"].split(",") if u])
            if armed:
                return f"peer-coverage-armed: {'; '.join(armed)}"
            if unknown:
                return f"peer-coverage-could-not-ask: {', '.join(unknown)}"
        vetoed = self._veto_refusal(seat, d)  # Recovery D15 (T-5859) (x3), alongside R2-2
        if vetoed is not None:
            return vetoed
        unit = (seat.get("units") or {}).get(d["unit"])
        fields = {k: unit.get(k) for k in ("UnitFileState", "ActiveState")} if isinstance(unit, dict) else None
        want = {k: pre[k] for k in ("UnitFileState", "ActiveState")}
        return None if fields == want else f"{d['unit']} is {fields}, moved from its precondition {want}"

    # --- EffectHandler ---------------------------------------------------------

    def observe(self, component, scope):
        if not scope.startswith(_SEAT_PREFIX):
            return {"account": None, "seat": None}
        account = scope[len(_SEAT_PREFIX):]
        return {"account": account, "seat": copy.deepcopy(self.state["seats"].get(account))}

    @staticmethod
    def _escalates(op):
        return op.desired.get("escalate_on_halt") == "true"

    def preflight(self, op):
        try:
            seat = self._seat(op.owner)
            if self._escalates(op):  # D-6: refused at the ATTEMPT, so the halt can escalate
                return PreflightResult(True, self.name, f"{op.kind}: checked at attempt (escalating)")
            refusal = self._refusal(op, seat)
        except (KeyError, ValueError) as exc:
            return PreflightResult(False, self.name, str(exc))
        return PreflightResult(refusal is None, self.name, refusal or f"{op.kind} admissible")

    def rollback_intent(self, op):
        seat = self._seat(op.owner)
        effects = self._effects(op, seat)
        writers = seat.get("writers") or {}
        before = {
            "cells": _canon({k: self._get(seat, k) for k in effects}),
            "writers": _canon({k: writers.get(k) for k in effects}),
            "verdict": _canon((seat.get("verdicts") or {}).get(op.operation_id)),
        }
        if op.kind == "harness.decommission-record":
            before["record"] = _canon({"path": op.target})
        if op.kind == "harness.decommission-unit" and op.desired.get("action") == "disable-now":
            before["undo"] = _canon({
                "unit": op.desired["unit"], "composite": op.desired.get("undo") == "daemon-reload-then-enable",
                "requires_restored": [s for s in (op.desired.get("requires_restored") or "").split(",") if s],
                "recipient": op.desired.get("escalation_recipient") or R.ESCALATION_RECIPIENT,
            })
        if op.kind == "harness.decommission-move":
            before["rename_back"] = _canon({"source": op.desired["source"]})
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, bool(effects), before, _fingerprint(effects))

    def _view(self, op, seat):
        files = seat.get("files") or {}
        path = op.target if op.kind == "harness.decommission-record" else op.desired.get("record_path")
        entry = files.get(path) if path else None
        record = entry.get("document") if isinstance(entry, dict) else None
        return {"units": seat.get("units") or {}, "files": files, "sessions": seat.get("sessions") or {}, "record": record}

    def _evaluate(self, op, seat):
        try:
            checks = R.decode(op.desired.get("postconditions", "[]"))
        except ValueError as exc:
            return False, f"malformed postconditions: {exc}"
        return R.verdict(R.evaluate(checks, self._view(op, seat)))

    def _halt(self, op, reason):
        if self._escalates(op):
            raise EscalatingHalt(
                f"{reason}; decommission HALTED at {op.operation_id} with seat {op.owner} half-removed and its "
                "runtime controller down: roll the run back to restore it",
                recipient=R.ESCALATION_RECIPIENT, subject=op.owner,
            )
        raise OperationFailed(reason)

    def execute(self, op):
        mutation = self.rollback_intent(op)
        seat = self._seat(op.owner)
        self._just_executed = None
        refusal = self._refusal(op, seat)
        if refusal is not None:
            self._halt(op, refusal)
        if op.kind == "harness.decommission-unit" and (seat.get("unit_stuck") or {}).get(op.desired.get("unit")) == "disable-now":
            self._halt(op, f"disable-now {op.desired['unit']} did not take (simulated)")
        if op.kind == "harness.decommission-move" and op.desired["source"] in (seat.get("move_fail") or ()):
            self._halt(op, f"rename {op.desired['source']} failed (simulated)")
        for key, value in self._effects(op, seat).items():
            self._set(seat, key, value)
            seat.setdefault("writers", {})[key] = op.operation_id
        seat.setdefault("verdicts", {})[op.operation_id] = list(self._evaluate(op, seat))
        self._just_executed = op.operation_id
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"simulated {op.kind} {op.desired.get('action', '')}".strip())

    def verify_operation(self, op):
        seat = self._seat(op.owner)
        writers = seat.get("writers") or {}
        cells = sorted(self._effects(op, seat))
        if op.kind == "harness.decommission-commit" or all(writers.get(k) == op.operation_id for k in cells):
            ok, detail = self._evaluate(op, seat)
        else:
            recorded = (seat.get("verdicts") or {}).get(op.operation_id)
            if not (isinstance(recorded, list) and len(recorded) == 2):
                return OperationEvidence(op.operation_id, op.layer, op.kind, False, "superseded with no execution-time verdict")
            ok, detail = recorded[0] is True, f"superseded by a later op; execution-time verdict: {recorded[1]}"
        just_executed, self._just_executed = self._just_executed == op.operation_id, None
        if not ok and just_executed:
            self._halt(op, f"postcondition unmet: {detail}")
        return OperationEvidence(op.operation_id, op.layer, op.kind, ok, detail)

    def can_rollback(self, mutation):
        try:
            seat = self._seat(mutation.owner)
            cells = json.loads(mutation.before["cells"])
        except (KeyError, ValueError) as exc:
            return PreflightResult(False, "drifted", str(exc))
        current = {k: self._get(seat, k) for k in cells}
        if _canon(current) == mutation.before["cells"]:
            return PreflightResult(True, "not-applied", "cells still at their preimage")
        if _fingerprint(current) == mutation.after_fingerprint:
            return PreflightResult(True, "owned", "cells hold this op's result")
        if "record" in mutation.before:  # an undone manifest is this run's own, kept by design
            path = json.loads(mutation.before["record"])["path"]
            entry = self._get(seat, f"files/{path}") or {}
            if (entry.get("document") or {}).get("status") == "undone":
                return PreflightResult(True, "not-applied", "manifest already marked undone")
        return PreflightResult(False, "drifted", "cells moved since this op")

    def rollback_mutation(self, mutation):
        seat = self._seat(mutation.owner)
        if "record" in mutation.before:  # never removed: marked undone (the leg stays delete-free)
            path = json.loads(mutation.before["record"])["path"]
            entry = self._get(seat, f"files/{path}")
            if isinstance(entry, dict) and isinstance(entry.get("document"), dict):
                self._set(seat, f"files/{path}", dict(entry, document=dict(entry["document"], status="undone")))
            self._restore_bookkeeping(seat, mutation, keep_cells=True)
            return
        if "rename_back" in mutation.before:
            source = json.loads(mutation.before["rename_back"])["source"]
            if source in (seat.get("rename_back_fail") or ()):
                raise OperationFailed(f"rename-back of {source} failed (simulated)")
        if "undo" in mutation.before:
            undo = json.loads(mutation.before["undo"])
            files = seat.get("files") or {}
            missing = [s for s in undo["requires_restored"] if not R.subtree(files, s)]
            stuck = (seat.get("unit_stuck") or {}).get(undo["unit"]) == "enable-now"
            if undo["composite"] and (missing or stuck):
                why = f"quarantined source(s) {missing} not restored" if missing else f"re-enable of {undo['unit']} did not take"
                raise EscalatingRollbackStop(
                    f"{why}; seat {mutation.owner} is left half-removed with its runtime controller down",
                    recipient=undo["recipient"], subject=mutation.owner,
                )
            if stuck:
                raise OperationFailed(f"rollback enable-now {undo['unit']} did not take (simulated)")
        self._restore_bookkeeping(seat, mutation, keep_cells=False)

    def _restore_bookkeeping(self, seat, mutation, *, keep_cells):
        if not keep_cells:
            for key, value in json.loads(mutation.before["cells"]).items():
                self._set(seat, key, value)
        writers = seat.setdefault("writers", {})
        for key, value in json.loads(mutation.before["writers"]).items():
            if value is None:
                writers.pop(key, None)
            else:
                writers[key] = value
        verdict = json.loads(mutation.before["verdict"])
        if verdict is None:
            seat.setdefault("verdicts", {}).pop(mutation.operation_id, None)
        else:
            seat.setdefault("verdicts", {})[mutation.operation_id] = verdict

    def dump_state(self):
        return copy.deepcopy(self.state)

    def load_state(self, state):
        self.state = {"seats": copy.deepcopy(dict(state.get("seats") or {}))}


HANDLERS = [HarnessSeatSim(), HarnessInstallSim(), HarnessSupervisorSim(), HarnessRunStateSim(), HarnessDecommissionSim()]


def VERIFY(list_doc, observed, scope):  # noqa: N802 -- the engine's registration name (components.py)
    """T-5135 S1: evaluate the ONE invariant list over the seat's simulated observation.

    The observation (``context`` / ``facts`` / ``carve_outs`` / ``suppressed``,
    harness_manager_verify's shape) is read from the seat sim's
    ``verify_observation`` slot. A seat with none yields no context, so every
    invariant alarms (could not ask) and the rollup is ``not_configured``.
    """

    from tools.germinate.components import harness_manager_verify as V

    seat = (observed.get(HarnessSeatSim.name) or {}).get("seat") or {}
    observation = seat.get("verify_observation") or {}
    results = V.evaluate(list_doc, observation)
    return {"results": results, "state": V.rollup(results, observation)["state"]}


def PREFLIGHT(ctx):  # noqa: N802 -- the engine's registration name (components.py, T-5921)
    """T-5921: harness-manager's T-3191 (viii) component checks for ``plan --preflight``;
    read-only, through ``ctx.probe`` only (see harness_manager_preflight)."""

    from tools.germinate.components import harness_manager_preflight as P

    return P.preflight(ctx)
