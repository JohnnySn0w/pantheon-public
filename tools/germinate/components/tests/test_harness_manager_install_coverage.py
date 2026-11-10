"""T-6167 PR-2: the coverage tripwire for harness-manager's install leg.

Every op kind the persona installer can plan has a live-seat handler, the engine's
kind map, the installer's ``OperationKind`` literal and the live handlers agree
exactly, and the read-only (``none``) pin equals the registry's ``none`` set. A new
installer kind (or a re-privileged one) fails here until a handler and a reviewed
pin change land together. Read-only: it plans, it never actuates.
"""

from __future__ import annotations

import typing
from pathlib import Path

import pytest

from tools import persona_installer as PI
from tools.germinate.engine.components import load_repo_op_registry
from tools.germinate.seat_backend import install_handlers as IH
from tools.germinate.seat_backend.backend import READ_ONLY_NONE_KINDS

from tools.germinate.components.tests import test_harness_manager_install as HMI

REPO_ROOT = Path(__file__).resolve().parents[4]
REV = HMI.REV

#: operator actions are NOT ops: the engine ignores them and the operator performs them.
#: Pinned so a new action category is a reviewed addition (credential actions are per reference).
PINNED_OPERATOR_ACTIONS = {"operator.host-identity", "operator.user-systemd", "operator.mailbox-acl"}
PINNED_OPERATOR_ACTION_PREFIX = "operator.credential."


def _plans():
    manifest = HMI._manifest()
    installer = PI.PersonaInstaller()
    return [installer.plan(manifest, persona, source_revision=REV) for persona in HMI.PERSONAS]


def _live_kinds():
    return IH.SEAT_INSTALL_KINDS | READ_ONLY_NONE_KINDS


def test_every_persona_plan_kind_has_a_live_handler():
    plans = _plans()
    assert len(plans) == 8
    planned = {op.kind for plan in plans for op in plan.operations}
    assert planned, "no ops planned"
    assert planned <= _live_kinds(), sorted(planned - _live_kinds())
    ro, seat = IH.ReadOnlyInstallHandler.kinds, IH.MutatingInstallHandler.kinds
    assert not (ro & seat) and (ro | seat) == _live_kinds()


def test_kind_sets_are_equal():
    literal = set(typing.get_args(PI.OperationKind))
    engine_map = set(HMI.HM._INSTALL_KIND_MAP)
    assert literal == engine_map == set(_live_kinds())
    assert len(literal) == 37 and len(READ_ONLY_NONE_KINDS) == 12 and len(IH.SEAT_INSTALL_KINDS) == 25


def test_read_only_pin_equals_registry_none_set():
    registry = load_repo_op_registry(REPO_ROOT)
    none_kinds = {k for k, e in registry.entries.items() if e.privilege == "none"}
    assert none_kinds == set(READ_ONLY_NONE_KINDS)
    seat_kinds = {k for k, e in registry.entries.items() if e.privilege == "seat"}
    assert set(IH.SEAT_INSTALL_KINDS) <= seat_kinds


def test_read_only_pin_is_the_exact_literal_twelve():
    assert READ_ONLY_NONE_KINDS == frozenset({
        "brief-check", "claude-mcp-server-check", "codex-standing-instructions-override-check",
        "codex-standing-instructions-source-check", "codex-standing-instructions-target-check",
        "codex-telegram-disabled-first", "harness-check", "identity-check", "manager-check",
        "policy-check", "source-check", "timer-enablement-absence-check",
    })


def test_operator_action_pending_allowlist_is_pinned():
    seen = {action.action_id for plan in _plans() for action in plan.operator_actions}
    exact = {a for a in seen if not a.startswith(PINNED_OPERATOR_ACTION_PREFIX)}
    assert exact == PINNED_OPERATOR_ACTIONS
    assert any(a.startswith(PINNED_OPERATOR_ACTION_PREFIX) for a in seen)


def test_op_privileges_map_to_pinned_sets():
    for kind, (engine_kind, privilege, _crash) in HMI.HM._INSTALL_KIND_MAP.items():
        assert engine_kind == kind
        assert (privilege == "none") == (kind in READ_ONLY_NONE_KINDS)
        assert (privilege == "seat") == (kind in IH.SEAT_INSTALL_KINDS)
    registry = load_repo_op_registry(REPO_ROOT)
    for kind in _live_kinds():
        assert registry.get(kind).privilege == HMI.HM._INSTALL_KIND_MAP[kind][1]


@pytest.mark.parametrize("persona", HMI.PERSONAS)
def test_every_planned_target_is_inside_the_manifest_derived_roots(persona):
    """The bridge pins roots from the manifest; the real plans must satisfy them lexically, so
    no kind's installer path reaches outside the seat home / mailbox hub / chronicle export."""

    manifest = HMI._manifest()
    plan = PI.PersonaInstaller().plan(manifest, persona, source_revision=REV)
    roots = PI._derive_allowed_roots(manifest, plan.account, Path("/")).split(",")
    assert PI._target_path_violations(plan.operations, roots) == []
    assert {op.provenance.get("install_root") for op in plan.operations} == {"/"}
